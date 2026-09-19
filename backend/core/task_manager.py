import inspect
import json
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from collectors import get_collector
from core import events
from core.cancel import TaskCancelled  # noqa: F401  (下载层要识别它, 在这里重导出)
from core.config import settings
from core.filters import Filters, probe_size
from core.manifest import manifest_enabled, write_manifest
from core.naming import build_context, render, safe_relative
from downloaders.file import FileDownloader
from downloaders.image import ImageDownloader
from downloaders.text import TextDownloader
from downloaders.video import VideoDownloader
from . import database as db

DOWNLOADS_DIR = settings.download_dir

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
        e = self._entry(task_id)
        if e:
            e["hb"] = time.monotonic()

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
            kwargs["log"] = lambda m: self._safe_log(task_id, m)
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

        futures = [
            self._download_executor.submit(
                self._download_one, task_id, r, referer, out_dir, filters
            )
            for r in resources
        ]
        for fut in as_completed(futures):
            fut.result()
            self._heartbeat(task_id)
            with counter_lock:
                counter[0] += 1
                self._set_progress(task_id, 20 + int(80 * counter[0] / total))

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
        if failed and not done:
            return TaskStatus.FAILED
        if failed:
            return TaskStatus.PARTIAL
        return TaskStatus.SUCCESS

    def _download_one(self, task_id, r, referer, out_dir, filters=None):
        rid = r["id"]
        filters = filters or Filters()
        if self._cancelled(task_id):
            db.update_resource(rid, status="skipped")
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
            if existing and existing["local_path"] != str(path):
                Path(path).unlink(missing_ok=True)
                final_path = existing["local_path"]
                db.update_resource(rid, status="done", hash=sha, local_path=final_path, **meta)
                self._safe_log(task_id, f"dedup {r['url']} -> {final_path}")
            else:
                final_path = str(path)
                db.update_resource(rid, status="done", hash=sha, local_path=final_path, **meta)
                self._safe_log(task_id, f"done {r['url']}")
            size = file_size(final_path)
            if size is not None:
                db.update_resource(rid, size=size)
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
        except Exception as e:
            db.update_resource(rid, status="failed")
            self._publish_resource(task_id, rid, "failed")
            self._safe_log(task_id, f"fail {r['url']}: {e}", "error")

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
        for p in (Path(out_dir) / name,):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

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
