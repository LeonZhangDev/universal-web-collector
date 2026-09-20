import inspect
import json
import logging
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from collectors import get_collector
from core import events
from core.cancel import TaskCancelled  # noqa: F401  (下载层要识别它, 在这里重导出)
from core.config import settings
from core.disk import ensure_free
from core.errors import CollectorError, DiskFullError, describe
from core.filters import Filters, probe_size
from core.imageinfo import fmt_dimensions, image_dimensions
from core.manifest import manifest_enabled, write_manifest, write_sidecar
from core.naming import build_context, render, safe_relative
from downloaders import ratelimit
from downloaders.base import discard_partial
from downloaders.file import FileDownloader
from downloaders.image import ImageDownloader
from downloaders.text import TextDownloader
from downloaders.video import VideoDownloader
from . import database as db

logger = logging.getLogger("uwc")

DOWNLOADS_DIR = settings.download_dir


def _eta(count, url):
    """按当前站点节奏粗估"至少"要多久。

    只是下界: 传输时间、重试、冷却都不算在内。写出来是为了让用户一眼看出
    "300 个资源 × 0.5s 间隔 = 至少 2 分半", 而不是盯着进度条猜是不是卡了。
    """
    try:
        lim = ratelimit._limiter(ratelimit.site_key(url))
        secs = count * lim.interval
    except Exception:
        return "未知"
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{secs / 60:.1f} 分钟"

# 删除任务时, 等 worker 收拾现场的上限(秒)。取消只置标志位, worker 需要时间
# 退出; 但 HTTP 请求不能因为一个卡死的 worker 一直挂着, 所以给个上限。
DELETE_SETTLE_SECONDS = 5.0


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    SUCCESS = "success"
    # 部分失败: 有资源成功也有资源失败。既不是全成, 也不该算整体失败
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # 暂停: 任务被用户主动停下, 但已下载的文件与资源记录全部保留, 可 resume 续跑
    PAUSED = "paused"


ACTIVE_STATES = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.EXTRACTING,
    TaskStatus.DOWNLOADING,
)

# 合法状态迁移表; retry: partial/failed/cancelled -> pending
TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.PAUSED},
    TaskStatus.RUNNING: {TaskStatus.EXTRACTING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED},
    TaskStatus.EXTRACTING: {TaskStatus.DOWNLOADING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED},
    TaskStatus.DOWNLOADING: {
        TaskStatus.SUCCESS,
        TaskStatus.PARTIAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.SUCCESS: set(),
    TaskStatus.PARTIAL: {TaskStatus.PENDING},
    TaskStatus.FAILED: {TaskStatus.PENDING},
    TaskStatus.CANCELLED: {TaskStatus.PENDING},
    # 暂停: 不是失败, 也不丢任何文件。保留资源与已下好的文件, 之后可 resume
    # 从 PENDING 续跑。只有终态/取消态才能进 paused, 避免对已完成任务误操作。
    TaskStatus.PAUSED: {TaskStatus.PENDING},
}

DOWNLOADERS = {
    "image": ImageDownloader,
    "video": VideoDownloader,
    "audio": FileDownloader,
    "doc": FileDownloader,
    "text": TextDownloader,
}


def task_base_dir(task):
    """任务的下载根目录(不含 task_id 子层)。

    优先任务创建时指定的自定义目录, 回退全局 settings.download_dir。
    API 层用它做文件访问的越权校验, 保证与任务实际输出目录一致。
    """
    custom = task["download_dir"] if task and task["download_dir"] else None
    return Path(custom) if custom else DOWNLOADS_DIR


def file_size(path):
    """取文件大小(字节), 文件不存在时返回 None。"""
    try:
        return Path(path).stat().st_size
    except Exception:
        return None


def can_transition(current, new):
    return new in TRANSITIONS.get(current, set())


# 进程内所有活着的 TaskManager 实例。
#
# 看门狗判断"这个活动任务真的没有 worker 了吗"时, 只看自己那本 `self._active`
# 是不够的: 只要进程里存在**第二个** TaskManager(测试、诊断脚本、
# 后台工具都会创建), 单例的看门狗就会看到 DB 里有个活动任务、而自己的
# _active 里没有它, 于是把它判成"服务重启遗留"直接标 failed ——
# 而那个任务的 worker 正在正常下载。所以必须问过所有实例。
#
# ⚠️ 这只解决单进程内的多实例。若以 uvicorn --workers N 多进程部署, 各进程
# 仍会互删任务(此时需要给 tasks 加 worker_id 列来区分归属)。
_LIVE_MANAGERS = []
_LIVE_LOCK = threading.Lock()


def _register_manager(mgr):
    with _LIVE_LOCK:
        _LIVE_MANAGERS.append(mgr)


def _unregister_manager(mgr):
    with _LIVE_LOCK:
        try:
            _LIVE_MANAGERS.remove(mgr)
        except ValueError:
            pass


def _owned_by_any_manager(task_id):
    """该任务是否正被进程内某个 TaskManager 持有。"""
    with _LIVE_LOCK:
        managers = list(_LIVE_MANAGERS)
    return any(m._entry(task_id) is not None for m in managers)


def recover_orphans(stale_timeout=None):
    """启动补偿: 把"上次那个进程没活到收尾"的活动任务复位成可操作状态。

    ⚠️ 为什么必须有这一步: worker 和看门狗都活在进程内存里。后端重启/崩溃后,
    库里 `running`/`extracting`/`downloading` 的任务**没有任何 worker 持有**,
    看门狗也随旧进程一起没了 —— 它们会永远停在"运行中", 进度条一动不动,
    用户既看不到报错也没法继续, 只能手动删掉。这是最像"程序坏了"的一类问题。

    ⚠️ 判活必须看库里的 `tasks.hb`, 与看门狗用同一把尺子。只按状态扫会把
    **另一个进程正在跑**的任务一并误伤(那个任务的 worker 好端端在下着),
    这就是"症状在状态机、根因在别处"的翻版。

    ⚠️ 复位成 paused 还是 failed, 取决于**资源清单落地了没有**:
      * 有清单 -> paused, 点「继续」能从断点接着下, 已下好的不浪费;
      * 没清单 -> failed, 因为续跑一个空清单只会得到"成功但 0 资源",
        那比报错难查得多。日志里直接告诉用户改点「重试」。
    """
    stale = stale_timeout if stale_timeout is not None else settings.stale_task_timeout
    now = time.time()
    recovered = []
    for t in db.list_active_tasks():
        tid = t["id"]
        if _owned_by_any_manager(tid):
            continue
        hb = t["hb"]
        if hb and now - float(hb) <= stale:
            # 心跳新鲜: 别的进程/实例正在跑它, 别碰
            continue
        if db.get_resources(tid):
            db.transition_task_from_any(tid, TaskStatus.PAUSED, ACTIVE_STATES)
            db.update_task(tid, error="上次进程中断, 已复位为暂停")
            db.add_log(tid, "启动补偿: 检测到上次进程中断, 已复位为「暂停」,"
                            " 点「继续」可从断点接着下", "warn")
        else:
            db.transition_task_from_any(tid, TaskStatus.FAILED, ACTIVE_STATES)
            db.update_task(tid, error="上次进程中断于资源发现阶段, 请重试该任务")
            db.add_log(tid, "启动补偿: 上次进程中断时还没采到任何资源, 无清单可续,"
                            " 已标记失败 —— 请改用「重试」重新采集", "error")
        recovered.append(tid)
    return recovered


class TaskManager:
    def __init__(self, max_workers=None, download_workers=None):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers or settings.max_task_workers,
            thread_name_prefix="task",
        )
        self._download_executor = ThreadPoolExecutor(
            max_workers=download_workers or settings.max_download_workers,
            thread_name_prefix="dl",
        )
        self._active = {}  # task_id -> {"future", "cancel", "hb"}
        self._active_lock = threading.Lock()
        _register_manager(self)
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="watchdog"
        )
        self._watchdog.start()
        # 订阅巡检线程: 与看门狗分开, 互不拖累
        self._scheduler_stop = threading.Event()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="scheduler"
        )
        self._scheduler.start()

    # ---- 对外接口 ----

    def submit(self, task_id, resume=False):
        entry = {"future": None, "cancel": threading.Event(), "hb": time.monotonic()}
        with self._active_lock:
            self._active[task_id] = entry
        entry["future"] = self._executor.submit(self._run, task_id, resume)

    def retry(self, task_id):
        task = db.get_task(task_id)
        if not task:
            return None, "task not found"
        if not can_transition(task["status"], TaskStatus.PENDING):
            return False, f"cannot retry task in status {task['status']}"

        db.update_task(
            task_id,
            retry_count=task["retry_count"] + 1,
            error=None,
            progress=0,
        )
        db.delete_task_resources(task_id)
        if not db.transition_task(task_id, TaskStatus.PENDING, task["status"]):
            return False, "task state changed concurrently"
        self._safe_log(task_id, f"retry #{task['retry_count'] + 1}")
        self.submit(task_id)
        self._publish_task(task_id)
        return True, None

    def cancel(self, task_id):
        task = db.get_task(task_id)
        if not task:
            return None, "task not found"
        if task["status"] not in ACTIVE_STATES:
            return False, f"cannot cancel task in status {task['status']}"

        entry = self._entry(task_id)
        if entry:
            entry["cancel"].set()
            # 只有当 worker 尚未启动时才需要手动回收: future 已被取消,
            # _run 不会执行, 也就不会走它的 finally 清理。
            # 反之(返回 False)说明 worker 正在跑, 由 _run 的 finally 负责 pop
            # ——绝不能在这里无条件 pop: 一旦 pop, _cancelled() 就再也读不到
            # 取消标志, worker 会把剩余资源全部下完才退出。
            if entry["future"] is not None and entry["future"].cancel():
                with self._active_lock:
                    self._active.pop(task_id, None)
        ok = db.transition_task_from_any(task_id, TaskStatus.CANCELLED, ACTIVE_STATES)
        if ok:
            self._safe_log(task_id, "task cancelled", "warn")
            self._publish_task(task_id)
        return ok, None

    def pause(self, task_id):
        """暂停: 停下 worker, 但**保留已下载的文件与资源记录**。

        与 cancel 的区别: cancel 是"不要了"(状态 cancelled); pause 是"先停一下"
        (状态 paused), 之后 resume 能从断点接着下, 已下好的不浪费。
        实现上先置取消标志让 worker 在下一个资源边界退出, 再把状态标成 paused
        —— 复用 cancel 的退出路径, 只是落点不同。
        """
        task = db.get_task(task_id)
        if not task:
            return None, "task not found"
        if task["status"] not in ACTIVE_STATES:
            return False, f"cannot pause task in status {task['status']}"

        entry = self._entry(task_id)
        if entry:
            entry["cancel"].set()
            if entry["future"] is not None and entry["future"].cancel():
                with self._active_lock:
                    self._active.pop(task_id, None)
        ok = db.transition_task_from_any(task_id, TaskStatus.PAUSED, ACTIVE_STATES)
        if ok:
            self._safe_log(task_id, "task paused", "warn")
            self._publish_task(task_id)
        return ok, None

    def resume(self, task_id):
        """续跑: 把未完成(pending/failed/skipped)的资源标回待下载, 从断点接着下。

        已 done 的资源不动; 已 filtered 的资源也不动(规则明确不要的)。
        不再重新采集 —— 直接复用上次枚举出来的资源清单(_run 走 resume 分支),
        这样暂停期间下好的文件全部保留, 只补下缺失的那部分。
        """
        task = db.get_task(task_id)
        if not task:
            return None, "task not found"
        if task["status"] != TaskStatus.PAUSED:
            return False, f"can only resume a paused task, current={task['status']}"

        # 把被暂停打断的资源(downloading/skipped)与之前失败的标回 pending
        for r in db.get_resources(task_id):
            if r["status"] in ("pending", "failed", "skipped", "downloading"):
                db.update_resource(r["id"], status="pending", note=None)
        db.update_task(task_id, error=None, progress=0)
        if not db.transition_task(task_id, TaskStatus.PENDING, TaskStatus.PAUSED):
            return False, "task state changed concurrently"
        self._safe_log(task_id, "resume from pause")
        self.submit(task_id, resume=True)
        self._publish_task(task_id)
        return True, None

    def delete(self, task_id, with_files=False):
        """删除任务, 返回 {"files": 已删文件数, "bytes": 释放的字节数}。

        * `with_files=False`(默认) —— 只删数据库记录, **磁盘文件保留**。
          上级目录里那一堆下载好的图片不会因为"从列表里划掉一个任务"而消失。
        * `with_files=True` —— 连同该任务的下载目录一起删除。

        顺序有讲究: 必须先取消再删文件, 否则正在跑的 worker 会把文件写回来。
        取消只是置标志位(见 cancel), worker 还要收拾现场, 所以这里要等它
        真正退出再动手删 —— 但等不到也不能把 HTTP 请求挂住, 故设上限。
        """
        info = {"files": 0, "bytes": 0}
        task = db.get_task(task_id)
        if not task:
            return info
        # 只置取消标志, 让 worker 在下一个资源边界自行退出;
        # _active 条目的回收交给 _run 的 finally(见 cancel 里的说明)。
        self.cancel(task_id)
        if with_files:
            deadline = time.monotonic() + DELETE_SETTLE_SECONDS
            while self.is_active(task_id) and time.monotonic() < deadline:
                time.sleep(0.05)
            info = self._purge_files(task)
        db.delete_task(task_id)
        return info

    def _purge_files(self, task):
        """删除某个任务的下载目录, 返回 {"files": n, "bytes": n}。

        ⚠️ 只删 `<下载根目录>/<task_id>/` 这一层, 且做了双重越界校验 ——
        下载根目录可由用户自定义, 路径算错就等于删用户的目录, 必须防。
        另外: 内容 hash 去重时别的任务可能引用本任务目录里的文件(A 复用 B 的
        同一张图), 删掉会让那些任务的 manifest 指向不存在的文件。这是去重的
        固有代价, 所以**默认不删文件**, 由用户显式选择。
        """
        try:
            base = task_base_dir(task).resolve()
        except OSError:
            return {"files": 0, "bytes": 0}
        target = base / str(task["id"])
        # 校验 1: 必须是 base 的直接子目录(不能是 base 本身, 也不能更深)
        if target.parent != base or not target.is_dir():
            return {"files": 0, "bytes": 0}
        # 校验 2: 解析后的真实路径仍须落在 base 内(防软链接指到外面)
        try:
            real = target.resolve()
        except OSError:
            return {"files": 0, "bytes": 0}
        if real == base or not real.is_relative_to(base):
            return {"files": 0, "bytes": 0}

        count = 0
        total = 0
        for p in real.rglob("*"):
            try:
                if p.is_file():
                    count += 1
                    total += p.stat().st_size
            except OSError:
                continue
        shutil.rmtree(real, ignore_errors=True)
        return {"files": count, "bytes": total}

    def submit_resource(self, task_id, resource_id):
        """资源级重试: 单个 failed/skipped 资源重新下载, 不影响任务状态。"""
        task = db.get_task(task_id)
        r = db.get_resource(resource_id)
        if not task or not r or r["task_id"] != task_id:
            return None, "resource not found"
        if task["status"] not in (
            TaskStatus.SUCCESS,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            return False, "task still running"
        if r["status"] not in ("failed", "skipped", "filtered"):
            return False, f"resource status is {r['status']}"

        db.update_resource(resource_id, status="pending", note=None)
        out_dir = self._out_dir(task_id)
        self._safe_log(task_id, f"resource retry: {r['url']}")
        # 单资源重试视为用户"强制下载"这一个资源, 不再套用过滤规则
        self._download_executor.submit(
            self._download_one, task_id, r, task["url"], out_dir
        )
        return True, None

    # ---- 订阅巡检 ----

    def run_watch(self, watch_id):
        """立即跑一次订阅源(手动触发或到期), 返回 task_id。

        订阅任务**强制 incremental**: 一个每 6 小时跑一次的任务如果每轮都全量
        重下几百张图, 那就不叫巡检叫骚扰 —— 站点也会很快把你封掉。
        """
        w = db.get_watch(watch_id)
        if not w:
            return None
        if not db.claim_watch(watch_id, w["interval_minutes"]):
            return None  # 已被别的线程抢走, 不重复创建任务
        try:
            opts = json.loads(w["options"] or "{}")
        except (TypeError, ValueError):
            opts = {}
        opts["incremental"] = True
        opts["watch_id"] = watch_id
        task_id = db.create_task(w["url"], w["collector"], opts.get("download_dir"), opts)
        self._safe_log(task_id, f"watch #{watch_id} 触发巡检 (每 {w['interval_minutes']} 分钟)")
        self.submit(task_id)
        self._publish_task(task_id)
        return task_id

    def _scheduler_loop(self):
        while not self._scheduler_stop.wait(settings.watch_interval):
            try:
                for w in db.due_watches():
                    self.run_watch(w["id"])
            except Exception:
                pass

    def _settle_watch(self, task_id):
        """巡检任务收尾: 回填本次实际新增的资源数。

        只数 resolved_url 非空的 —— 增量复用的(url status=done 但本次没发过
        请求)不算新增, 否则 hits 会随每轮巡检虚涨。
        """
        wid = self._options(task_id).get("watch_id")
        if not wid:
            return
        try:
            db.finish_watch_run(wid, task_id, db.count_downloaded(task_id))
        except Exception:
            pass

    def shutdown(self, wait=False):
        """停掉后台线程。

        `wait=True` 会**等所有 worker 真正退出**才返回。测试与脚本必须用它 ——
        否则上个用例里没跑完的 worker 会继续在**下一个用例**里写库(测试的 DB
        连接是模块级、可被 monkeypatch 替换的), 症状是"别的用例的任务被写进
        莫名其妙的错误信息", 表现为随机失败, 极难定位。
        """
        self._watchdog_stop.set()
        # 退出登记: 本实例不再持有任何任务, 它留下的活动任务应可被回收
        _unregister_manager(self)
        self._scheduler_stop.set()
        if wait:
            self._executor.shutdown(wait=True)
            self._download_executor.shutdown(wait=True)

    # ---- 任务配置辅助 ----

    def _filters(self, task_id):
        """读取任务的过滤规则。任务被删或 options 非法时返回空规则(放行一切)。"""
        task = db.get_task(task_id)
        if not task or not task["options"]:
            return Filters()
        return Filters(task["options"])

    def _options(self, task_id):
        """把任务的原始 options(JSON 字符串)读成 dict, 供采集器消费。

        options 是平铺结构: 过滤条件与采集器参数(如 quality) 同层存放,
        Filters 只取自己认识的键, 多余的键对它没有影响。
        """
        task = db.get_task(task_id)
        raw = task["options"] if task else None
        if not raw:
            return {}
        try:
            return json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            return {}

    def _incremental(self, task_id):
        """任务是否走增量模式(options.incremental=true)。"""
        return bool(self._options(task_id).get("incremental"))

    def _resource_name(self, task_id, r, seq, suggested=None):
        """决定资源的输出相对路径。

        优先级: 任务级 name_template > 采集器建议 filename > 全局模板。
        之所以把任务级模板放在最前: 它是用户这一次显式指定的意愿, 应当压过
        采集器的默认习惯; 而采集器建议又压过全局默认 `{name}`
        —— 谁了解这个站点, 谁说的算。

        `{album}` 的取值顺序: 资源自带的相册名(采集器查到的相册标题) >
        options.album(用户显式指定) > 站点标识(见 naming.build_context)。
        """
        opts = self._options(task_id)
        # _row_field 同时兼容采集器内存字典与 sqlite3.Row
        album = self._row_field(r, "album") or opts.get("album")
        template = opts.get("name_template")
        if template:
            ctx = build_context(task_id, r["url"], r["type"], seq, album=album)
            return render(template, ctx, r["url"], r["type"])
        if suggested:
            # 采集器给的相对路径同样要过一遍安全清洗, 防止 URL 里的奇怪字符
            # 拼出 `../` 逃出任务目录
            cleaned = safe_relative(suggested)
            if cleaned:
                return cleaned
        ctx = build_context(task_id, r["url"], r["type"], seq, album=album)
        return render(getattr(settings, "name_template", "") or "{name}",
                      ctx, r["url"], r["type"])

    def _write_manifest(self, task_id):
        """把任务的产出清单写到输出目录。

        无论成功/部分失败/取消都写: 取消时用户最需要知道"哪几个下好了"。
        """
        task = db.get_task(task_id)
        if not task:
            return
        if not manifest_enabled(self._options(task_id)):
            return
        out_dir = task_base_dir(task) / str(task_id)
        try:
            write_manifest(
                task,
                db.get_resources(task_id),
                out_dir,
                stats=db.count_resources(task_id),
                error=task["error"],
                log=lambda m, level="info": self._safe_log(task_id, m, level),
            )
        except Exception as e:
            # manifest 是附属产物, 写不出来不能让任务失败
            self._safe_log(task_id, f"manifest 生成失败: {e}", "warn")

    def _write_sidecar(self, task_id, resources):
        """把"这次采集采的是什么"写成 album.json(集合级元数据)。

        与 manifest.json 的分工: manifest 是文件级(一行一个资源), 这份是集合级
        (目录名/厂牌/标签/实际生效的资源根)。采集器把描述挂在每条资源的
        `task_meta` 上(同一个 dict 引用), 这里取第一份即可。

        落盘归任务层管是刻意的 —— 采集器的职责只有"发现"。见 README 的铁律。
        """
        meta = None
        for r in resources or []:
            meta = r.get("task_meta")
            if meta:
                break
        if not meta:
            return None
        task = db.get_task(task_id)
        if not task:
            return None
        try:
            return write_sidecar(
                task, meta, self._out_dir(task_id), resources=resources,
                log=lambda m, level="info": self._safe_log(task_id, m, level),
            )
        except Exception as e:
            self._safe_log(task_id, f"album.json 生成失败: {e}", "warn")
            return None

    def _out_dir(self, task_id):
        """任务输出目录: 优先任务自定义目录, 回退全局 download_dir。

        无论哪种情况都再套一层 task_id 子目录, 避免不同任务的文件混在一起。
        """
        return task_base_dir(db.get_task(task_id)) / str(task_id)

    # ---- 任务主流程 ----

    def _entry(self, task_id):
        with self._active_lock:
            return self._active.get(task_id)

    def is_active(self, task_id):
        """是否还有 worker 在这个任务上跑。

        注意与 DB 里的 status 是两回事: cancel() 会**立刻**把状态写成
        cancelled 好让界面秒响应, 但 worker 还要收拾现场(清半成品、写
        manifest), 那期间 is_active 仍为 True。要断言"已经收拾干净了",
        等的是这里, 不是 status。
        """
        with self._active_lock:
            return task_id in self._active

    def _cancelled(self, task_id):
        e = self._entry(task_id)
        return bool(e and e["cancel"].is_set())

    def _check_cancel(self, task_id):
        if self._cancelled(task_id):
            raise TaskCancelled()

    def _heartbeat(self, task_id):
        """刷新心跳: 内存一份(本进程判活用), 库里一份(别的进程判活用)。

        只写内存是不够的 —— `_LIVE_MANAGERS` 是进程内列表, 两个实例共用一个
        SQLite 时, A 进程看不到 B 进程正在跑的任务, 会把它判成"重启遗留"直接
        标 failed, 而那个任务其实跑得好好的(实测: 一个正在枚举 114 张图的任务
        就这么被杀掉了)。库里的 hb 是跨进程的真相: 还在跳就是活的。
        """
        now = time.time()
        e = self._entry(task_id)
        if e:
            e["hb"] = time.monotonic()
        try:
            db.update_task(task_id, hb=now)
        except Exception:
            pass

    def _run(self, task_id, resume=False):
        try:
            task = db.get_task(task_id)
            if not task or task["status"] != TaskStatus.PENDING:
                return
            url = task["url"]
            if not db.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.PENDING):
                return
            self._heartbeat(task_id)
            self._safe_log(task_id, f"start {url}")
            self._publish_task(task_id)

            if resume:
                # 续跑: 不重新采集, 直接下载被暂停打断时标回 pending 的未完成资源。
                # 已 done 的文件原样保留, 只补下缺失的那部分 —— 这是 pause/resume
                # 相对于 cancel+retry 的核心价值(后者会全量重下)。
                if not db.get_resources(task_id):
                    # 续跑一个空清单会走到"0 个资源 -> 全成功 -> success", 也就是
                    # "什么也没下到"和"全部下好了"长得一模一样。宁可直接报错。
                    raise ValueError(
                        "续跑失败: 上次没有留下任何资源清单(中断发生在资源发现阶段),"
                        " 请改用「重试」重新采集"
                    )
                self._set_progress(task_id, 20)
                self._transition(task_id, TaskStatus.RUNNING, TaskStatus.DOWNLOADING)
                self._safe_log(task_id, f"resume output dir: {self._out_dir(task_id)}")
                self._download_all(task_id, url)
                self._check_cancel(task_id)
                final = self._final_status(task_id)
                self._transition(task_id, TaskStatus.DOWNLOADING, final)
                self._set_progress(task_id, 100)
                self._safe_log(task_id, f"task {final}")
                return

            spider = get_collector(task["collector"])
            self._set_progress(task_id, 5)
            self._transition(task_id, TaskStatus.RUNNING, TaskStatus.EXTRACTING)
            resources = self._crawl(spider, url, task_id, self._options(task_id))
            self._check_cancel(task_id)
            if not resources:
                # 采集器一个资源都没发现 -> 直接报失败。
                # 旧行为是照常走完下载阶段(0 个资源)并判定 success, 于是
                # "什么也没下到"和"全部下好了"在界面上长得一模一样。
                # 真实事故: 相册页 URL 被解析出错误的图集 ID, 枚举了 3 个不存在
                # 的序号后停止, 任务显示 success 而 0 个资源, 用户无从判断原因。
                raise ValueError(
                    "未发现任何资源: 请确认 URL 形态与所选采集器匹配, "
                    "并查看上方日志中采集器的解析/探测结果"
                )
            filters = self._filters(task_id)
            filtered = 0
            reused = 0
            for seq, r in enumerate(resources, 1):
                # 增量续采: 同一个 URL 历史上已经成功下载过 -> 直接复用磁盘上的
                # 那份, 不再发请求。这是"相册更新后再跑一遍"能落地的前提:
                # 否则每次重跑都是全量重下, 增量就只剩个名字。
                prior = db.find_done_resource(r["url"]) if self._incremental(task_id) else None
                name = self._resource_name(task_id, r, seq, r.get("filename"))
                rid = db.add_resource(
                    task_id,
                    r["type"],
                    r["url"],
                    r.get("headers"),
                    mirrors=r.get("mirrors"),
                    size=r.get("size"),
                    filename=name,
                    status="done" if prior else "pending",
                    local_path=prior["local_path"] if prior else None,
                    hash_value=prior["hash"] if prior else None,
                )
                if prior:
                    reused += 1
                    db.update_resource(rid, size=prior["size"])
                    self._publish_resource(task_id, rid, "done")
                    continue
                reason = filters.match_resource(r["type"], r["url"], r.get("size"))
                if reason:
                    filtered += 1
                    db.update_resource(rid, status="filtered", note=reason)
            self._safe_log(task_id, f"extracted {len(resources)} resources")
            # 任务名: 从首个资源的输出路径推断相册名/标题 ——
            # 图集资源形如 "约啪.../00001.jpg" -> 取首段目录名; 视频资源形如
            # "base.mp4" -> 取去扩展名的 base。这样列表里一眼能看出采的是哪个相册,
            # 而不是一排相同的 URL。提取阶段之后才写, 避免无资源时写空名。
            name = self._infer_name(resources)
            if name:
                db.update_task(task_id, name=name)
                self._publish_task(task_id)
            # 采集已完成, 此刻就知道"采的是什么"了 —— 不必等下载结果,
            # 这样即使后面下载全失败, sidecar 里仍有排查所需的资源根信息。
            self._write_sidecar(task_id, resources)
            if reused:
                self._safe_log(task_id, f"incremental: 复用已下载 {reused} 个, 跳过重复传输")
            if filters.active:
                self._safe_log(
                    task_id,
                    f"filter [{filters.summarize()}] -> "
                    f"kept {len(resources) - filtered}, filtered {filtered}",
                )
            self._set_progress(task_id, 20)

            self._transition(task_id, TaskStatus.EXTRACTING, TaskStatus.DOWNLOADING)
            self._safe_log(task_id, f"output dir: {self._out_dir(task_id)}")
            # 让"上限 2 req/s"这类阈值**看得见**。看不见的阈值会被反复误调 ——
            # 用户以为把并发调大就会更快, 而真正顶住吞吐的是请求间隔。
            pacing = ratelimit.describe(url)
            if pacing:
                self._safe_log(task_id, f"站点节奏: {pacing}")
            if resources:
                self._safe_log(
                    task_id,
                    f"待下载 {len(resources)} 个资源, 按当前节奏预计至少 "
                    f"{_eta(len(resources), url)}",
                )
            self._download_all(task_id, url)

            self._check_cancel(task_id)
            final = self._final_status(task_id)
            self._transition(task_id, TaskStatus.DOWNLOADING, final)
            self._set_progress(task_id, 100)
            self._safe_log(task_id, f"task {final}")
        except TaskCancelled:
            self._safe_log(task_id, "task cancelled by user", "warn")
        except Exception as e:
            self._safe_log(task_id, f"task failed: {e}", "error")
            self._safe_log(task_id, traceback.format_exc(limit=3).strip(), "error")
            db.update_task(task_id, error=str(e)[:500])
            cur = db.get_task(task_id)
            if cur and can_transition(cur["status"], TaskStatus.FAILED):
                db.update_task_status(task_id, TaskStatus.FAILED)
            self._publish_task(task_id)
        finally:
            # 无论成败都产出 manifest: 取消/部分失败时, 用户最需要知道
            # "到底哪几个下好了"
            self._write_manifest(task_id)
            self._settle_watch(task_id)
            with self._active_lock:
                self._active.pop(task_id, None)
            self._publish_task(task_id)

    def _crawl(self, spider, url, task_id, options=None):
        """调用采集器。

        按采集器实际支持的参数按需注入, 老采集器不受影响:
          * log     —— 探测进度回调(图集枚举会输出每条序号的判定过程)
          * options —— 任务级参数(目前是图集画质档 quality)

        是否注入只看 crawl 的签名, 所以新增可选参数不会破坏既有采集器。
        """
        try:
            params = inspect.signature(spider.crawl).parameters
        except (TypeError, ValueError):
            params = {}
        accepts_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        kwargs = {}
        if accepts_kw or "log" in params:
            # 采集阶段的日志同时当心跳用: 图集枚举可能连跑几百个序号、
            # 十几分钟不碰一次 _set_progress, 期间心跳不刷新就会被看门狗当成
            # "卡住了"取消掉。节流到 5 秒一次, 避免每条日志都写库。
            beat = [0.0]

            def crawl_log(m, level="info"):
                # ⚠️ 必须收第二个可选参数: 采集器会用 `log(msg, "warn")` 表达
                # "这条要显眼一点"(如"有 3 个子页面没展开")。只收一个参数的话,
                # 那行日志会在**采集全部做完之后**抛 TypeError 把整个任务搞崩 ——
                # 明明资源都发现了, 用户却看到 failed。宁可丢掉等级也不能丢任务。
                #
                # ⚠️ 采集阶段也要能被叫停: 逐张枚举几百个序号要跑十几分钟, 而取消
                # 检查点原本只装在下载循环里 —— 用户点了"停止", 界面已经变灰,
                # 后台却还在把剩下的序号一个个探完。log 是采集器在枚举循环里唯一
                # 稳定调用的东西, 借它当检查点最及时。
                #
                # ⚠️ 只在"当前没有异常正在传播"时才检查: 采集器常常在 `except`
                # 块里调 log 报告错误, 那时抛 TaskCancelled 会把真正的原因顶掉
                # —— 又是"报错离真相很远"那一类。
                if sys.exc_info()[0] is None:
                    self._check_cancel(task_id)
                self._safe_log(task_id, m, level)
                now = time.time()
                if now - beat[0] >= 5:
                    beat[0] = now
                    self._heartbeat(task_id)

            kwargs["log"] = crawl_log
        if options and (accepts_kw or "options" in params):
            kwargs["options"] = options
        return spider.crawl(url, **kwargs) if kwargs else spider.crawl(url)

    def _download_all(self, task_id, referer):
        # 只下载通过 URL 层过滤的资源(filtered 的已在提取阶段排除)
        resources = [r for r in db.get_resources(task_id) if r["status"] == "pending"]
        total = len(resources)
        if total == 0:
            # 区分"没找到"与"被规则挡掉": 前者是故障, 后者是用户自己的选择
            stat = self._resource_stat(task_id)
            if stat:
                self._safe_log(
                    task_id, "no resource to download (全部命中过滤/已复用, 无需下载)"
                )
            else:
                self._safe_log(task_id, "no resource to download (采集器未发现任何资源)")
            return

        out_dir = self._out_dir(task_id)
        filters = self._filters(task_id)
        counter = [0]
        counter_lock = threading.Lock()

        # 环境级失败(如磁盘满)的广播通道: 一个资源撞上, 同批剩下的直接跳过,
        # 而不是各自走完重试链 —— 那样几百个资源就是长时间空转。
        abort = threading.Event()
        futures = [
            self._download_executor.submit(
                self._download_one, task_id, r, referer, out_dir, filters, abort
            )
            for r in resources
        ]
        for fut in as_completed(futures):
            fut.result()
            self._heartbeat(task_id)
            with counter_lock:
                counter[0] += 1
                self._set_progress(task_id, 20 + int(80 * counter[0] / total))

        if abort.is_set():
            # 把"为什么只下了一部分"写进任务本身 —— 否则用户看到 partial 却
            # 没有任何解释, 只会以为程序下漏了。
            db.update_task(task_id, error="磁盘空间不足, 部分资源未下载(清理后可继续)")
            self._safe_log(task_id, "磁盘空间不足: 已停止剩余资源的下载", "error")

        stat = self._resource_stat(task_id)
        self._safe_log(
            task_id,
            "summary: " + ", ".join(f"{k}={v}" for k, v in sorted(stat.items())),
        )

    @staticmethod
    def _resource_stat(task_id):
        """按状态统计资源数量。"""
        stat = {}
        for r in db.get_resources(task_id):
            stat[r["status"]] = stat.get(r["status"], 0) + 1
        return stat

    def _final_status(self, task_id):
        """按资源实际结果决定任务终态。

        * 全部资源都失败           -> failed
        * 有失败也有成功           -> partial(部分失败)
        * 其余(含全部被过滤/跳过)  -> success

        过滤与跳过不计为失败: 那是用户规则或能力缺失的结果, 不是任务出错。
        旧行为是不看资源结果一律 success, 于是"56 张全部下载失败"也显示成功。
        """
        stat = self._resource_stat(task_id)
        done = stat.get("done", 0)
        failed = stat.get("failed", 0)
        # ⚠️ 一个资源都没有却走到这里 = 这次运行什么也没产出。空集在"全部成功"
        # 的判定下会被算成 success, 于是"什么也没下到"与"全部下好了"在界面上
        # 长得一模一样。正常路径已在采集后判空抛错, 这里兜住其余入口。
        if not stat:
            return TaskStatus.FAILED
        if failed and not done:
            return TaskStatus.FAILED
        if failed:
            return TaskStatus.PARTIAL
        return TaskStatus.SUCCESS

    def _download_one(self, task_id, r, referer, out_dir, filters=None, abort=None):
        rid = r["id"]
        filters = filters or Filters()
        if self._cancelled(task_id):
            db.update_resource(rid, status="skipped")
            self._publish_resource(task_id, rid, "skipped")
            return
        if abort and abort.is_set():
            # 同批里已经有资源撞上环境级失败(磁盘满): 剩下的注定一样, 直接跳过。
            # ⚠️ 不标 failed —— 它们没有失败, 只是没轮到; 标成 failed 会让
            # "一个盘满"看起来像"几百个资源都坏了"。
            db.update_resource(rid, status="skipped", note="磁盘空间不足, 未下载")
            self._publish_resource(task_id, rid, "skipped")
            return
        self._heartbeat(task_id)
        try:
            downloader = DOWNLOADERS[r["type"]]()
        except KeyError:
            db.update_resource(rid, status="skipped")
            self._publish_resource(task_id, rid, "skipped")
            self._safe_log(task_id, f"skip [{r['type']}] no downloader: {r['url']}")
            return

        def tick():
            """下载过程中的心跳: 顺带检查取消, 让停止操作立刻生效。

            只检查资源边界是不够的 —— 一个 500MB 的视频会一路下完才退出,
            用户点了"停止"却要等几分钟。借 progress_cb 在每个数据块后判断,
            代价是每个 chunk 一次 Event.is_set()(纳秒级)。
            """
            self._heartbeat(task_id)
            self._check_cancel(task_id)

        try:
            headers = json.loads(r["headers"] or "{}")
            # 备用下载点由采集器给出(如同一张图的多个尺寸变体), 主 URL 失败时切换
            mirrors = db.get_mirrors(r)

            # 大小预检: 超限直接跳过, 不浪费带宽下载大文件。
            # 优先用**采集阶段已经拿到的** size —— 图集枚举的 probe 顺带返回了
            # Content-Length, 相册页又直接给出每段视频的体积, 所以图集任务在
            # 这条规则上是零额外请求的; 只有 size 未知时才补一次 HEAD。
            if filters.need_size:
                size = r["size"]
                if isinstance(size, str) and size.strip().isdigit():
                    size = int(size)
                elif not isinstance(size, int):
                    size = None
                if size is None:
                    size = probe_size(r["url"], headers)
                reason = filters.match_resource(r["type"], r["url"], size)
                if reason:
                    db.update_resource(rid, status="filtered", note=reason, size=size)
                    self._publish_resource(task_id, rid, "filtered")
                    self._safe_log(task_id, f"filtered {r['url']}: {reason}")
                    return

            # 磁盘水位: 满盘之后再下就是纯空转(每个资源都要走完一整条重试链)
            ensure_free(out_dir)

            db.update_resource(rid, status="downloading")
            self._publish_resource(task_id, rid, "downloading")
            # 下载层回填实际生效的下载点与响应类型, 供 manifest 溯源
            info = {}
            path, sha = downloader.download(
                r["url"],
                referer=referer,
                save_dir=out_dir,
                headers=headers,
                progress_cb=tick,
                mirrors=mirrors,
                log=lambda m: self._safe_log(task_id, m),
                filename=self._row_field(r, "filename"),
                info=info,
            )
            meta = {"resolved_url": info.get("resolved_url"),
                    "content_type": info.get("content_type")}
            meta = {k: v for k, v in meta.items() if v}
            existing = db.find_by_hash(sha)
            owned = True
            # 感知比对只在**任务内**进行, 所以字节级命中别的任务时, 下面那次解码
            # 一定比不出任何东西 —— 白 spawn 一次 ffmpeg。跨任务命中就跳过它。
            perceptual_worthwhile = True
            if existing and existing["local_path"] != str(path):
                Path(path).unlink(missing_ok=True)
                final_path = existing["local_path"]
                # 复用别的任务的文件: 不能因为本任务的新规则去删它
                owned = False
                # ⚠️ sqlite3.Row 没有 .get(); 直接用会抛 AttributeError, 而这个
                # 位置在 try 里 —— 异常会被当成"下载失败", 报得离真相很远
                keys = existing.keys() if hasattr(existing, "keys") else ()
                if "task_id" in keys and existing["task_id"] != task_id:
                    perceptual_worthwhile = False
                db.update_resource(rid, status="done", hash=sha, local_path=final_path, **meta)
                self._safe_log(task_id, f"dedup {r['url']} -> {final_path}")
            else:
                final_path = str(path)
                db.update_resource(rid, status="done", hash=sha, local_path=final_path, **meta)
                self._safe_log(task_id, f"done {r['url']}")
            size = file_size(final_path)
            if size is not None:
                db.update_resource(rid, size=size)

            # 尺寸终检(可选): 广告横幅(728x90)、按钮(88x31)、信标(1x1)的文件名
            # 和体积都可能"正常", 只有量宽高才认得出。放在**下载后**是有意的 ——
            # 那时手上是完整文件, 零额外请求、零误判; 下载前用 Range 抓头部遇到
            # progressive JPEG 会读不到 SOF, 于是广告照样落盘, 等于没做。
            if filters.need_dimensions and (r["type"] or "").lower() == "image":
                dims = image_dimensions(final_path)
                reason = filters.match_dimensions(*dims) if dims else None
                if reason:
                    if owned:
                        Path(final_path).unlink(missing_ok=True)
                    db.update_resource(
                        rid, status="filtered", local_path=None,
                        note=f"{reason}; 实测 {fmt_dimensions(dims)}",
                    )
                    self._publish_resource(task_id, rid, "filtered")
                    self._safe_log(
                        task_id,
                        f"filtered {r['url']}: {reason} (尺寸终检, 已删除文件)",
                    )
                    return

            # 感知去重(可选, 默认开): sha256 认不出"换尺寸/重压缩后仍是同一张",
            # 而图集站上这正是最常见的重复形态。**只标记不删除** —— 指纹会误判,
            # 删文件是不可逆的, 出错的代价由用户承担(见 core/phash.py 三条约束)。
            # 放在尺寸终检**之后**: 被判为广告的图已经删了, 不必再为它解码一次。
            if (filters.dedup_perceptual and perceptual_worthwhile
                    and (r["type"] or "").lower() == "image"):
                self._mark_perceptual_dup(
                    task_id, rid, final_path, r["url"], filters.dedup_threshold
                )

            self._publish_resource(task_id, rid, "done")
        except TaskCancelled:
            # 下载中途被叫停: 半成品留着没意义且会被误认为已完成, 直接删掉
            self._discard_partial(out_dir, r)
            db.update_resource(rid, status="skipped", note="cancelled mid-download")
            self._publish_resource(task_id, rid, "skipped")
        except NotImplementedError:
            db.update_resource(rid, status="skipped")
            self._publish_resource(task_id, rid, "skipped")
            self._safe_log(task_id, f"skip (not implemented): {r['url']}")
        except DiskFullError as e:
            # 环境级失败: 剩下的资源注定也写不进去, 让同批其它 worker 直接跳过。
            # ⚠️ 不标 cancelled —— 已经下好的文件是真实成果, 终态交给
            # _final_status 判成 partial/failed。
            if abort:
                abort.set()
            # 残片在满盘时只是占位, 清掉还给磁盘一点空间
            self._discard_partial(out_dir, r)
            db.update_resource(rid, status="failed", note=str(e))
            self._publish_resource(task_id, rid, "failed")
            self._safe_log(task_id, str(e), "error")
        except CollectorError as e:
            # 外部世界的问题(站点/URL/环境): 消息本身就是写给用户看的,
            # 不打堆栈 —— 堆栈会把"接下来怎么办"挤到屏幕外。
            db.update_resource(rid, status="failed", note=str(e)[:500])
            self._publish_resource(task_id, rid, "failed")
            self._safe_log(task_id, f"{r['url']}: {e}", "error")
        except Exception as e:
            db.update_resource(rid, status="failed")
            self._publish_resource(task_id, rid, "failed")
            self._safe_log(task_id, f"fail {r['url']}: {describe(e)}", "error")
            # ⚠️ 走到这里说明**分类之外**: 不是站点的问题, 也不是环境的问题,
            # 那就是我们的 bug。只在日志里留堆栈, 界面上仍是一句人话 ——
            # 没有这一段, 这类 bug 会表现成"某个资源莫名失败", 复盘时无从下手。
            logger.debug("unclassified failure on %s\n%s", r["url"],
                         traceback.format_exc())

    def _mark_perceptual_dup(self, task_id, rid, path, url, threshold):
        """算 dHash, 并在**本任务内**找出最接近的一张, 只做标记。

        绝不删文件、绝不改 status —— 见 `core/phash.py` 的约束 1。标记到
        `resources.duplicate_of`, 详情页与 manifest 会显示出来, 由人决定。
        整个函数**不允许抛异常**: 指纹是优化, 不是流程的一部分, 一个附件能力
        把下载搞挂是本项目反复踩过的坑, 所以这里自己兜住所有分支。
        """
        try:
            from core import phash

            got = phash.dhash(path)
            if not got:
                # 算不出(ffmpeg 不在 / 解码失败)就当作"没有指纹", 静默放行。
                # 这里刻意**不写日志**: 一个 300 张的相册会刷 300 行同样的
                # "算不出指纹", 真正的错误会被淹没。要排查就单跑 phash.dhash()。
                return
            known = db.task_phashes(task_id, exclude_id=rid)
            db.update_resource(rid, phash=got)
            hit = phash.find_duplicate(got, known, threshold=threshold)
            if not hit:
                return
            other_id, dist = hit
            db.update_resource(rid, duplicate_of=other_id)
            self._safe_log(
                task_id,
                f"perceptual dup {url}: 与资源 #{other_id} 疑似同一张"
                f"(dHash 距离 {dist}/{64}, 阈值 {threshold}; 文件已保留未删除)",
                "warn",
            )
        except TaskCancelled:
            # 与其它分支一致: 取消信号必须原样穿透, 不能被 except Exception 吞掉
            raise
        except Exception:
            return

    @staticmethod
    def _infer_name(resources):
        """从资源输出路径推断任务展示名: 图集取目录首段, 视频取去扩展名 base。"""
        from pathlib import Path

        for r in resources:
            fn = r.get("filename") if isinstance(r, dict) else None
            if not fn:
                # sqlite3.Row 没有 .get, 用下标容错
                try:
                    fn = r["filename"]
                except (IndexError, KeyError, TypeError):
                    fn = None
            if not fn:
                continue
            if "/" in fn:
                return fn.split("/", 1)[0]
            return Path(fn).stem
        return None

    @staticmethod
    def _row_field(row, key):
        """读 sqlite3.Row 的可选列: 旧库缺列时返回 None 而不是抛 IndexError。"""
        try:
            return row[key]
        except (IndexError, KeyError):
            return None

    @staticmethod
    def _discard_partial(out_dir, r):
        """清理取消时可能留下的半成品。

        m3u8 的分片缓存目录 `.<stem>.parts` 要保留 —— 那是续传的资本,
        重跑时能省下已下好的分片; 但半截的单文件媒体没有价值。
        """
        name = TaskManager._row_field(r, "filename")
        if not name:
            return
        # ⚠️ 半成品现在落在 `<name>.part`(见 downloaders/base.py 原子落盘),
        # 中断时目标位置反而是干净的 —— 所以必须连同 .part 一起清, 否则用户
        # 目录里会留下一堆 xxx.jpg.part, 下次续传还会接着这些残片继续写。
        discard_partial(Path(out_dir) / name)

    def _transition(self, task_id, expected, new):
        self._check_cancel(task_id)
        if not db.transition_task(task_id, new, expected):
            cur = db.get_task(task_id)
            if cur and cur["status"] == TaskStatus.CANCELLED:
                raise TaskCancelled()
            raise RuntimeError(
                f"invalid transition {expected} -> {new} for task {task_id}"
            )
        self._safe_log(task_id, f"status -> {new}")
        self._publish_task(task_id)

    def _set_progress(self, task_id, progress):
        db.update_task(task_id, progress=progress)
        self._heartbeat(task_id)
        self._publish_task(task_id)

    # ---- 事件/日志 ----

    def _publish_task(self, task_id):
        t = db.get_task(task_id)
        if t:
            events.publish("task.updated", dict(t))

    def _publish_resource(self, task_id, rid, status):
        events.publish(
            "resource.updated", {"task_id": task_id, "id": rid, "status": status}
        )

    def _safe_log(self, task_id, message, level="info"):
        try:
            db.add_log(task_id, message, level)
        except Exception:
            pass  # 任务可能已被删除(FK), 忽略
        events.publish(
            "task.log",
            {
                "task_id": task_id,
                "level": level,
                "message": message,
                "time": time.strftime("%H:%M:%S"),
            },
        )

    # ---- 看门狗 ----

    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(settings.watchdog_interval):
            try:
                self._watchdog_pass()
            except Exception:
                pass

    def _watchdog_pass(self):
        """处理两类僵死任务: 无 worker 的(服务重启遗留) 与 无进度的。"""
        for t in db.list_active_tasks():
            tid = t["id"]
            entry = self._entry(tid)
            if entry is None and _owned_by_any_manager(tid):
                # 别的 TaskManager 实例正在跑它, 不是遗留任务
                continue
            if entry is None:
                # 本进程没在跑它 —— 可能是别的进程在跑, 也可能是真死了。
                # 判据只能是**库里的心跳**: 还在跳就说明有别的工作进程在推进,
                # 这时候标 failed 就是误杀(多实例/多 worker 共用库时会踩到)。
                hb = self._row_field(t, "hb")
                if hb and time.time() - float(hb) <= settings.stale_task_timeout:
                    continue
                db.update_task(tid, error="stale task: no active worker (server restarted?)")
                db.transition_task_from_any(
                    tid, TaskStatus.FAILED,
                    (TaskStatus.RUNNING, TaskStatus.EXTRACTING, TaskStatus.DOWNLOADING),
                )
                self._safe_log(tid, "watchdog: stale task marked failed", "error")
                self._publish_task(tid)
                continue
            if time.monotonic() - entry["hb"] > settings.stale_task_timeout:
                entry["cancel"].set()
                db.update_task(tid, error="task stalled (no progress), cancelled by watchdog")
                db.transition_task_from_any(
                    tid, TaskStatus.CANCELLED,
                    (TaskStatus.RUNNING, TaskStatus.EXTRACTING, TaskStatus.DOWNLOADING),
                )
                self._safe_log(tid, "watchdog: stalled task cancelled", "warn")
                self._publish_task(tid)


task_manager = TaskManager()
