import inspect
import io
import json
import queue
import zipfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from collectors import COLLECTORS, get_collector, resolve_collector
from collectors.gallery_base import (
    ALBUM_TITLE_MODES,
    DEFAULT_ALBUM_TITLE,
    DEFAULT_MEDIA,
    MEDIA_KEYS,
    QUALITY_KEYS,
    shape_warning,
)
from core import database as db
from core import events
from core.config import settings
from core.filters import parse_size
from core.manifest import read_manifest
from core.task_manager import (
    ACTIVE_STATES,
    DOWNLOADERS,
    TaskStatus,
    task_base_dir,
    task_manager,
)
from models.schemas import ResourceOut, TaskCreateIn, TaskCreateOut, TaskDetail, TaskOut

router = APIRouter()


def _file_url(task, local_path):
    """把本地路径转成 /files/{task_id}/... 的可访问 URL。

    路径不在该任务下载根目录内时返回 None(例如 hash 去重复用了别的目录的文件)。
    """
    if not local_path:
        return None
    try:
        base = task_base_dir(task).resolve()
        rel = Path(local_path).resolve().relative_to(base)
    except (ValueError, OSError):
        return None
    return f"/files/{task['id']}/" + quote(str(rel).replace("\\", "/"))


def _resource_out(r, task):
    d = dict(r)
    d["file_url"] = _file_url(task, d.get("local_path"))
    return d


def _validate_download_dir(value):
    """校验自定义下载目录。空值返回 None(回退全局默认)。"""
    if not value or not value.strip():
        return None
    p = Path(value.strip())
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="下载目录必须是绝对路径")
    if any(part == ".." for part in p.parts):
        raise HTTPException(status_code=400, detail="下载目录不允许包含 '..'")
    if len(p.parts) <= 1:
        raise HTTPException(status_code=400, detail="不允许把盘符根目录作为下载目录")
    return str(p)


def _gallery_options(filters=None, quality=None, media=None, album_title=None,
                     album_tags_dir=None, max_items=None, aggregate_depth=None):
    """收集并校验采集器相关 options(**创建任务与预览共用**)。

    共用是有意的: 否则"预览通过、创建却被拒"(或反过来)这种不一致
    会非常难排查。options 平铺存放, Filters 只读它认识的键。
    """
    options = {}
    if filters is not None:
        options = filters.model_dump(exclude_none=True)
        for key in ("min_size", "max_size"):
            v = options.get(key)
            if v is not None and parse_size(v) is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} 格式非法: {v}. 示例: 500KB / 2MB / 1048576",
                )
    if quality:
        if quality not in QUALITY_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"quality 非法: {quality}. 可选: {', '.join(QUALITY_KEYS)}",
            )
        options["quality"] = quality
    if media:
        if media not in MEDIA_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"media 非法: {media}. 可选: {', '.join(MEDIA_KEYS)}",
            )
        options["media"] = media
    if album_title:
        if album_title not in ALBUM_TITLE_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"album_title 非法: {album_title}. "
                       f"可选: {', '.join(ALBUM_TITLE_MODES)}",
            )
        options["album_title"] = album_title
    if album_tags_dir is not None:
        options["album_tags_dir"] = bool(album_tags_dir)
    # 聚合页闸门。上界是保护而不是限制意志: 一个索引页挂着上百个模特, depth=3
    # 就是几百次页面读取, 站点会先把我们封掉。要更多就分批来。
    if max_items is not None:
        n = int(max_items)
        if not 1 <= n <= AGGREGATE_MAX_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=f"max_items 应在 1~{AGGREGATE_MAX_ITEMS} 之间: {max_items}",
            )
        options["max_items"] = n
    if aggregate_depth is not None:
        d = int(aggregate_depth)
        if not 1 <= d <= AGGREGATE_MAX_DEPTH:
            raise HTTPException(
                status_code=400,
                detail=f"aggregate_depth 应在 1~{AGGREGATE_MAX_DEPTH} 之间: {aggregate_depth}",
            )
        options["aggregate_depth"] = d
    return options


#: 聚合页单次展开上限。这不是"限制用户", 而是防止一个索引页(上百个模特)
#: 乘上 depth 之后变成几百次页面读取 —— 那会先把站点惹毛, 结果是全都采不到。
AGGREGATE_MAX_ITEMS = 500
AGGREGATE_MAX_DEPTH = 3


#: 采集器处的 "auto" = 让后端按 URL 挑一个。
#: ⚠️ 库里**永远存解析后的真实采集器名**, 否则重放/订阅巡检时
#: "同一个 auto 指向了不同采集器", 任务行为就不再可复现了。
AUTO_COLLECTOR = "auto"


def _manual_warning(name, url):
    """手选采集器时的**软**提示(目前只有"ID 形状不像本站")。没有则返回 None。

    刻意不做成 400: 手选是用户已表过的态, 站点可能刚换 ID 格式而我们比用户
    知道得晚(`core.filters` / `shape_warning` 里有完整理由)。但也刻意不塞进
    `resolved` —— 那个字段专指**自动识别的结论**, 界面靠 `auto` 区分显示方式,
    把两种性质不同的东西混进同一个字段, 以后谁都说不清它到底代表什么。
    """
    cls = COLLECTORS.get(name)
    site = getattr(cls, "site", None) if cls else None
    if site is None:
        return None
    try:
        return shape_warning(site, url)
    except Exception:
        return None      # 软提示不该有能力让创建/预览失败


def _pick_collector(url, chosen):
    """确定本次实际使用的采集器。

    返回 `(采集器名, 识别结论, 软提示)`:

    - **自动识别**: 第二项是 `resolve_collector` 的结论(界面用它显示"已识别为 X"),
      第三项为 None —— 自动识别形状不符时是**不认领**, 轮不到软提示。
    - **手动指定**: 第二项为 None(没走识别, 不该回显识别结论), 第三项可能给出
      形状软提示。库里存的永远是解析后的真实采集器名, 与 `auto` 无关。
    """
    name = (chosen or "").strip()
    if name and name != AUTO_COLLECTOR:
        if name not in COLLECTORS:
            raise HTTPException(status_code=400, detail=f"unknown collector: {name}")
        return name, None, _manual_warning(name, url)
    got = resolve_collector(url, fallback=None)
    if not got["collector"]:
        raise HTTPException(status_code=400, detail=got["reason"])
    return got["collector"], got, None


@router.post("/tasks/create", response_model=TaskCreateOut)
def create(payload: TaskCreateIn):
    collector, resolved, warning = _pick_collector(payload.url, payload.collector)
    download_dir = _validate_download_dir(payload.download_dir)
    options = _gallery_options(
        filters=payload.filters,
        quality=payload.quality,
        media=payload.media,
        album_title=payload.album_title,
        album_tags_dir=payload.album_tags_dir,
        max_items=payload.max_items,
        aggregate_depth=payload.aggregate_depth,
    )

    task_id = db.create_task(payload.url, collector, download_dir, options)
    task_manager.submit(task_id)
    # resolved 非 None 时把识别结论一并回显, 界面可以显示"已识别为 X";
    # warning 是**软**提示(任务已创建, 只是提醒多半粘错了链接), 界面不要当错误显示。
    return TaskCreateOut(task_id=task_id, status="pending", resolved=resolved,
                         warning=warning)


class PreviewIn(BaseModel):
    url: str
    collector: str = AUTO_COLLECTOR
    quality: Optional[str] = None
    media: Optional[str] = None
    album_title: Optional[str] = None
    album_tags_dir: Optional[bool] = None
    # 抽样上限: 预览不该因为"想看全"把接口拖成几分钟
    max_items: int = 12
    # 聚合页采集器: 还能再往下钻几层(模特索引 -> 模特 -> 相册 = 2 层)
    aggregate_depth: Optional[int] = None


@router.post("/tasks/preview")
def preview(payload: PreviewIn):
    """创建前预告: **只发现、不下载、不写库**, 告诉用户这次会采到什么。

    图集类采集器优先用相册页的站点自报数据(张数 + 每段视频体积), 通常一次
    页面读取就能返回; 页面拿不到(没装浏览器 / Cloudflare 拦住)才退化成受限
    枚举, 此时 `sampled=true`, 数量只是下限。

    存在的意义: 一个相册可能是"12 张图 + 4 段视频共 260MB", 让用户在**创建
    之前**就看到体积, 而不是等它默默下完(见 media / max_size 选项)。
    """
    collector, resolved, warning = _pick_collector(payload.url, payload.collector)
    spider = get_collector(collector)
    fn = getattr(spider, "preview", None)
    if not callable(fn):
        raise HTTPException(
            status_code=400,
            detail=f"采集器 {collector} 不支持预览(仅图集类采集器支持)",
        )
    options = _gallery_options(
        quality=payload.quality,
        media=payload.media,
        album_title=payload.album_title,
        album_tags_dir=payload.album_tags_dir,
        aggregate_depth=payload.aggregate_depth,
    )
    limit = max(1, min(int(payload.max_items or 12), 50))
    logs = []

    def sink(msg, level="info"):
        """收集预览日志。

        ⚠️ 必须收第二个可选参数: 采集器会用 `log(msg, "warn")` 标出"这条要显眼"。
        只收一个参数的话, 那行日志会抛 TypeError, 把整个预览请求变成 500 ——
        **日志不该有能力让功能失败**。
        """
        logs.append(f"[{level}] {msg}" if level != "info" else str(msg))

    # 按签名注入, 与 task_manager._crawl 同一套思路: 预览不是图集类独有的能力,
    # 硬编码参数会让新采集器(如聚合页)一接进来就 TypeError。
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {}
    if "options" in params:
        kwargs["options"] = options
    if "log" in params:
        kwargs["log"] = sink
    if "max_items" in params:
        kwargs["max_items"] = limit
    try:
        data = fn(payload.url, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    data["logs"] = logs
    if resolved:
        data["resolved"] = resolved
    # 手选采集器的形状软提示要出现在**用户正在看的那块面板**里。前端已经会渲染
    # 预告顶层的 `warning`(采不到资源时给的就是"下一步怎么做"), 这里顺势放进去。
    # 用 setdefault: 采集器自己给的 warning 更贴近本次结果, 不能被形状提示顶掉。
    if warning:
        data.setdefault("warning", warning)
    # 兼容: 采集器自己也可能往 resolved 里塞 warning(旧路径), 一并提上来
    if resolved and resolved.get("warning"):
        data.setdefault("warning", resolved["warning"])
    return data


@router.get("/collectors/resolve")
def resolve_collector_api(url: str = ""):
    """URL -> 采集器, 不做任何网络请求。

    存在的意义: 自动识别必须**先给用户看见再生效**。这里是纯 CPU 的字符串
    判定, 前端可以在输入框失焦时立刻回显"已识别为 X", 让用户有机会改。
    """
    return resolve_collector((url or "").strip(), fallback=None)


@router.get("/collectors")
def list_collectors():
    return [
        {"name": n, "supports_preview": hasattr(COLLECTORS[n], "preview")}
        for n in sorted(COLLECTORS)
    ]


@router.get("/config")
def get_config():
    """前端初始化用: 默认下载目录 + 资源类型 + 图集画质档/媒体/命名方式。"""
    return {
        "download_dir": str(settings.download_dir),
        "resource_types": sorted(DOWNLOADERS.keys()),
        "collectors": sorted(COLLECTORS),
        "auto_collector": AUTO_COLLECTOR,
        "qualities": list(QUALITY_KEYS),
        "medias": list(MEDIA_KEYS),
        "default_media": DEFAULT_MEDIA,
        "album_titles": list(ALBUM_TITLE_MODES),
        "default_album_title": DEFAULT_ALBUM_TITLE,
        # 聚合页闸门的可选上限, 前端拿它渲染输入框的 max 属性。
        # 与后端校验共用同一个常量, 避免"前端允许 999 后端只收 500"。
        "aggregate_max_items": AGGREGATE_MAX_ITEMS,
        "aggregate_max_depth": AGGREGATE_MAX_DEPTH,
    }


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks():
    return [dict(t) for t in db.list_tasks()]


# ⚠️ 必须声明在 /tasks/{task_id} **之前**: FastAPI 按注册顺序匹配, 否则
# "storage" 会被当成 task_id 去解析成 int 而返回 422。
@router.get("/tasks/storage")
def storage_overview():
    """任务与产出的整体占用概览, 供"一键清理"界面预检。"""
    by_status = db.count_tasks_by_status()
    row = db.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes"
        " FROM resources WHERE status='done'"
    )[0]
    finished = sum(
        by_status.get(s, 0)
        for s in (TaskStatus.SUCCESS, TaskStatus.PARTIAL,
                  TaskStatus.FAILED, TaskStatus.CANCELLED)
    )
    return {
        "tasks": sum(by_status.values()),
        "by_status": by_status,
        "finished_tasks": finished,
        "done_resources": row["n"],
        "recorded_bytes": row["bytes"],
        "active_states": list(ACTIVE_STATES),
    }


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def get_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    resources = [_resource_out(r, task) for r in db.get_resources(task_id)]
    return TaskDetail(**dict(task), resources=resources)


@router.get("/tasks/{task_id}/logs")
def get_logs(task_id: int):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return [dict(l) for l in db.get_logs(task_id)]


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
def retry(task_id: int):
    ok, err = task_manager.retry(task_id)
    if ok is None:
        raise HTTPException(status_code=404, detail=err)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return dict(db.get_task(task_id))


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: int):
    """停止任务但保留已下载的文件与任务记录。

    与 DELETE 的区别就在这里: 删除是"没下过", 取消是"下到一半我不想要了"。
    已经跑完的任务再点停止没有意义, 返回 409 而不是把它改成 cancelled
    —— 那样会把成功的结果伪装成没做完。
    """
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    ok, err = task_manager.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail=err)
    return dict(db.get_task(task_id))


@router.post("/tasks/{task_id}/pause", response_model=TaskOut)
def pause_task(task_id: int):
    """暂停: 停下 worker, 但保留已下载文件与资源记录, 之后可 resume 续跑。

    与 cancel 的区别: cancel 是"不要了"(状态 cancelled); pause 是"先停一下"
    (状态 paused), 已下好的不浪费、之后从断点接着下。非运行中的任务返回 409。
    """
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    ok, err = task_manager.pause(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail=err)
    return dict(db.get_task(task_id))


@router.post("/tasks/{task_id}/resume", response_model=TaskOut)
def resume_task(task_id: int):
    """续跑: 把暂停时打断的未完成资源标回待下载, 复用已下好的文件从断点接着下。

    与 retry 的区别: retry 会删除全部资源记录重头来过(适合"规则改了/站点变了");
    resume 只补缺失的那部分 —— 暂停期间下好的文件全部保留。
    """
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    ok, err = task_manager.resume(task_id)
    if not ok:
        raise HTTPException(status_code=409, detail=err)
    return dict(db.get_task(task_id))


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, with_files: bool = False):
    """删除任务。

    with_files=True 时**同时删除该任务下载目录下的文件**(默认只删记录)。
    默认不删文件是刻意的: 去重机制下别的任务可能正在引用这些文件, 且
    "从列表里划掉一行"和"把已下载的东西删掉"应该分开决定。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    info = task_manager.delete(task_id, with_files=with_files)
    return {"deleted": task_id, "with_files": with_files, **info}


class BulkDeleteIn(BaseModel):
    """批量清理。默认作用于所有**已结束**的任务(成功/部分/失败/已取消)。"""

    statuses: List[str] = [
        TaskStatus.SUCCESS,
        TaskStatus.PARTIAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ]
    with_files: bool = False


@router.post("/tasks/bulk-delete")
def bulk_delete(payload: BulkDeleteIn):
    """一键清理: 按状态批量删除任务。

    正在运行的任务不会被删(即使状态被显式写进 statuses), 会列在 skipped 里
    返回 —— 让用户知道"有一个还在跑"比悄悄跳过更好。
    """
    invalid = [s for s in payload.statuses if s in ACTIVE_STATES]
    wanted = [s for s in payload.statuses if s not in ACTIVE_STATES]
    if not wanted:
        raise HTTPException(status_code=400, detail="没有可删除的状态(不能停止运行中的任务)")

    deleted = []
    skipped = []
    files = 0
    freed = 0
    for t in db.iter_tasks_with_status(wanted):
        info = task_manager.delete(t["id"], with_files=payload.with_files)
        deleted.append(t["id"])
        files += info.get("files", 0)
        freed += info.get("bytes", 0)
    for t in db.list_tasks():
        if t["status"] in ACTIVE_STATES:
            skipped.append(t["id"])

    return {
        "deleted": deleted,
        "skipped_active": skipped,
        "with_files": payload.with_files,
        "files": files,
        "bytes": freed,
        # 显式告知调用方哪些状态被忽略了(例如误传了 running)
        "ignored_statuses": invalid,
    }


@router.post("/tasks/{task_id}/resources/{resource_id}/retry", response_model=ResourceOut)
def retry_resource(task_id: int, resource_id: int):
    ok, err = task_manager.submit_resource(task_id, resource_id)
    if ok is None:
        raise HTTPException(status_code=404, detail=err)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    task = db.get_task(task_id)
    return _resource_out(db.get_resource(resource_id), task)


# ---- 目录选择 ----

@router.get("/fs/browse")
def browse_fs(path: str = None):
    """列出本机目录(仅子目录), 供前端目录选择器使用。

    默认从用户主目录开始。无权限或已断链的条目直接跳过, 不让整个列表失败。
    """
    target = Path(path) if path else Path.home()
    try:
        target = target.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail=f"无法解析路径: {path}")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target}")
    if not target.is_dir():
        target = target.parent

    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"没有访问权限: {target}")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"无法读取目录: {e}")

    entries = []
    for child in children:
        try:
            if child.name.startswith(".") or not child.is_dir():
                continue
        except OSError:
            continue
        entries.append({"name": child.name, "path": str(child)})

    parent = target.parent
    return {
        "cwd": str(target),
        "parent": str(parent) if parent != target else None,
        "entries": entries,
    }


class MkdirIn(BaseModel):
    parent: str
    name: str


@router.post("/fs/mkdir")
def mkdir_fs(payload: MkdirIn):
    """在选择器里新建文件夹。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名不能为空")
    if any(c in name for c in '\\/:*?"<>|'):
        raise HTTPException(status_code=400, detail=f"文件夹名含非法字符: {name}")
    base = Path(payload.parent)
    if not base.is_absolute():
        raise HTTPException(status_code=400, detail="父目录必须是绝对路径")
    if any(part == ".." for part in base.parts):
        raise HTTPException(status_code=400, detail="路径不允许包含 '..'")
    target = base / name
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"创建失败: {e}")
    return {"path": str(target)}


# ---- 订阅巡检 ----

class WatchIn(BaseModel):
    url: str
    collector: str = AUTO_COLLECTOR
    interval_minutes: int = 360
    download_dir: Optional[str] = None
    quality: Optional[str] = None
    media: Optional[str] = None
    album_title: Optional[str] = None
    album_tags_dir: Optional[bool] = None
    # 聚合页: 一次巡检展开的范围(与创建任务同一套选项)
    max_items: Optional[int] = None
    aggregate_depth: Optional[int] = None
    run_now: bool = True


@router.post("/watches")
def create_watch(payload: WatchIn):
    """订阅一个 URL, 之后按周期巡检并只补新出现的资源。

    巡检任务内部**强制增量**: 复用历史已下载的内容, 不再重复传输。
    """
    collector, resolved, warning = _pick_collector(payload.url, payload.collector)
    if payload.interval_minutes < 1:
        raise HTTPException(status_code=400, detail="interval_minutes 至少为 1")
    # 与创建任务/预览**共用**同一个选项构造器。原先这里手抄了一份校验,
    # 结果是 album_tags_dir 这类新选项在前端传了却被静默丢掉 —— 订阅会长期
    # 反复跑, 悄悄少一个选项比直接报错难查得多。
    options = _gallery_options(
        quality=payload.quality,
        media=payload.media,
        album_title=payload.album_title,
        album_tags_dir=payload.album_tags_dir,
        max_items=payload.max_items,
        aggregate_depth=payload.aggregate_depth,
    )
    download_dir = _validate_download_dir(payload.download_dir)
    if download_dir:
        options["download_dir"] = download_dir
    wid = db.create_watch(
        payload.url, collector, payload.interval_minutes, options,
        run_now=payload.run_now,
    )
    watch = dict(db.get_watch(wid))
    # 订阅会长期反复跑, 识别结论更要回显: 一旦猜错, 每次巡检都会错。
    # 手选时的形状软提示同理 —— 订阅错一个 ID, 错的是**每一次**巡检。
    if resolved is not None:
        watch["resolved"] = resolved
    if warning:
        watch["warning"] = warning
    return watch


@router.get("/watches")
def list_watches_api():
    return [dict(w) for w in db.list_watches()]


@router.post("/watches/{watch_id}/run")
def run_watch(watch_id: int):
    """手动跑一次(不等周期)。"""
    task_id = task_manager.run_watch(watch_id)
    if not task_id:
        raise HTTPException(
            status_code=404, detail="watch not found or already claimed"
        )
    return {"task_id": task_id}


@router.post("/watches/{watch_id}/toggle")
def toggle_watch(watch_id: int):
    w = db.get_watch(watch_id)
    if not w:
        raise HTTPException(status_code=404, detail="watch not found")
    enabled = not bool(w["enabled"])
    db.set_watch_enabled(watch_id, enabled)
    return dict(db.get_watch(watch_id))


@router.delete("/watches/{watch_id}")
def remove_watch(watch_id: int):
    if not db.delete_watch(watch_id):
        raise HTTPException(status_code=404, detail="watch not found")
    return {"deleted": watch_id}


# ---- 文件服务 ----

@router.get("/files/{task_id}/manifest")
def task_manifest(task_id: int):
    """读取任务产出清单, 供前端展示"原来在哪/是否完整/为什么没下载"。"""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    data = read_manifest(task_base_dir(task) / str(task_id))
    if data is None:
        raise HTTPException(status_code=404, detail="manifest not found")
    return data


@router.post("/tasks/{task_id}/archive")
def archive_task(task_id: int, only: str = "done"):
    """把任务产出打包成 zip 下载。

    only: done / all —— 默认只打包成功下载的, 失败与过滤项通常不值得打包。
    打包前重新核对每个文件是否仍在 disk 上: 用户可能手工删过几个。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    base = task_base_dir(task).resolve()
    rows = [dict(r) for r in db.get_resources(task_id)]
    items = []
    for r in rows:
        if only == "done" and r["status"] != "done":
            continue
        path = _resolve_safely(base, r.get("local_path"))
        if path:
            items.append((r, path))
    if not items:
        raise HTTPException(status_code=404, detail="no downloadable file in this task")
    return StreamingResponse(
        _zip_stream(base, items, task_id),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="task-{task_id}.zip"',
            "X-Accel-Buffering": "no",
        },
    )


def _resolve_safely(base, local_path):
    """把 DB 里的绝对路径还原成任务目录内的真实文件, 不存在或越界返回 None。"""
    if not local_path:
        return None
    try:
        p = Path(local_path).resolve()
    except OSError:
        return None
    if not p.is_file() or not p.is_relative_to(base):
        return None
    return p


def _dedup_name(name, counter):
    """同名文件去重: 不同子目录里的 a.jpg 进同一个包会互相覆盖 -> a (1).jpg。"""
    if name not in counter:
        counter[name] = 0
        return name
    counter[name] += 1
    root, dot, ext = name.rpartition(".")
    head = root if dot else name
    tail = f".{ext}" if dot else ""
    return f"{head} ({counter[name]}){tail}"


def _zip_stream(base, items):
    """边打包边吐数据的生成器, 不把整个压缩包攒在内存里。

    图片/视频本身已经是压缩过的媒体, deflate 几乎压不动却要吃掉不少 CPU,
    所以这里用 ZIP_STORED(仅归档, 不再压缩)。
    """

    class _Sink(io.RawIOBase):
        """收集 zipfile 写出的字节, 取走后清空(缓冲区不膨胀)。"""

        def __init__(self):
            self.pending = []

        def writable(self):
            return True

        def write(self, b):
            data = bytes(b)
            self.pending.append(data)
            return len(data)

        def take(self):
            if not self.pending:
                return b""
            out = b"".join(self.pending)
            self.pending.clear()
            return out

    sink = _Sink()
    counter = {}
    # zipfile 对不可 seek 的 fileobj 会自动走流式路径(self._seekable=False)
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
        for _, path in items:
            zf.write(path, arcname=_dedup_name(path.relative_to(base).as_posix(), counter))
            chunk = sink.take()
            if chunk:
                yield chunk
    # 退出 with 时写出的是 central directory, 必须一并吐出去
    tail = sink.take()
    if tail:
        yield tail


@router.get("/files/{task_id}/{file_path:path}")
def serve_file(task_id: int, file_path: str):
    """按任务访问已下载文件。

    相比直接 mount 整个 downloads 目录, 这里把访问范围限制在该任务自己的
    下载根目录内, 避免跨任务/跨目录读取。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    base = task_base_dir(task).resolve()
    try:
        full = (base / file_path).resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="invalid path")
    if not full.is_relative_to(base):
        raise HTTPException(status_code=403, detail="forbidden")
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full)


@router.get("/events")
def sse_events():
    q = events.subscribe()

    def gen():
        try:
            while True:
                try:
                    name, data = q.get(timeout=15)
                    yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
