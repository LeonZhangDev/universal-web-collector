import inspect
import io
import json
import queue
import zipfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Query
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
from core import layout
from core import mediacheck
from core import partials
from core import thumbs
from core.config import settings
from core.content_identity import canonical_content_key
from core.errors import KIND_CORRUPT, KIND_LABELS, KIND_MISSING
from core.filters import fmt_size, parse_size
from core.manifest import read_manifest
from core.task_dedup import create_or_dispose
from core.task_manager import (
    ACTIVE_STATES,
    DOWNLOADERS,
    TaskStatus,
    task_base_dir,
    task_manager,
)
from downloaders import ratelimit
from downloaders.base import discard_partial
from models.schemas import (
    BatchTaskIn,
    BatchTaskItemOut,
    BatchTaskOut,
    BulkActionIn,
    BulkActionOut,
    LibraryBulkDeleteIn,
    LibraryBulkOut,
    LibraryFavoriteIn,
    LibraryFavoriteOut,
    LibraryTagsIn,
    LibraryTagsOut,
    LibraryVerifyIn,
    LibraryVerifyItem,
    LibraryVerifyOut,
    MarkReadIn,
    NotificationListOut,
    NotificationOut,
    ResourceOut,
    RetryFailedOut,
    TaskCreateIn,
    TaskCreateOut,
    TaskDetail,
    TaskListOut,
    TaskOut,
    TaskStatsOut,
)

router = APIRouter()


# 列表筛选用的状态分组。前端下拉只发一个组代号(如 "active"), 后端必须展开成
# 真实 status 值再去查 —— 否则 `status IN ('active')` 永远 0 条。
# 单个真实状态(如 "success")原样透传。空串/None 表示不过滤。
STATUS_GROUPS = {
    "active": ["pending", "running", "extracting", "downloading"],
}


def _expand_statuses(vals):
    """把查询参数里的状态组代号展开成真实 status 列表。"""
    out = []
    for v in vals or []:
        out.extend(STATUS_GROUPS.get(v, [v]))
    return out or None


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
                     max_items=None, aggregate_depth=None):
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
        max_items=payload.max_items,
        aggregate_depth=payload.aggregate_depth,
    )
    # 两侧都是往 options 里塞独立字段，互不覆盖，合并保留：
    if payload.proxy:
        options["proxy"] = payload.proxy
    if payload.incremental is not None:
        options["incremental"] = payload.incremental

    content_key = canonical_content_key(payload.url, collector=collector)
    result = create_or_dispose(
        payload.url,
        collector,
        download_dir,
        options,
        content_key=content_key,
        deduplicate=payload.deduplicate,
        force_new=payload.force_new,
    )
    if result.created:
        try:
            task_manager.submit(result.task_id)
        except Exception as exc:
            db.update_task(
                result.task_id,
                status=TaskStatus.FAILED,
                error=f"task submission failed: {exc}"[:500],
            )
            raise
    # resolved 非 None 时把识别结论一并回显, 界面可以显示"已识别为 X";
    # warning 是**软**提示(任务已创建, 只是提醒多半粘错了链接), 界面不要当错误显示。
    return TaskCreateOut(
        task_id=result.task_id,
        status=result.status,
        resolved=resolved,
        warning=warning,
        disposition=result.disposition,
        content_key=content_key,
    )


# ---- 批量创建 ----
#: 单次批量创建的行数上限。粘贴几百条是合理的; 一次几万条多半是误操作
#: (比如把整个网页的文本粘进来了), 那会把任务队列整个堵死。
BATCH_MAX_URLS = 500

#: 单行长度上限。URL 与图集 ID 都不会这么长, 超过基本可以断定粘错了东西。
_MAX_URL_LEN = 2000


def _batch_key(s):
    """批量去重用的比较键。

    ⚠️ 只做最保守的归一化(去首尾空白 + 去结尾斜杠), 不改写协议/host 大小写。
    看起来"更聪明"的归一化会把 `/a` 和 `/A` 判成同一个, 而图片直链改大小写
    就 404 的站点是真实存在的。误判成重复的代价是**该下的东西没下**, 比
    "多下一次"严重得多 —— 后者用户一眼能看出来, 前者不会有人发现。
    """
    return s.strip().rstrip("/")


def _shape_reject(s):
    """最保守的形状校验, 只拦"明显不是输入"的行。返回原因或 None。

    ⚠️ 这不是采集器识别(那是 `_pick_collector` 的事), 也刻意不顺便当它用:
    手选采集器时自动识别是被跳过的, 若这里又用识别结果当门槛, 手选模式下的
    批量创建就退化成"必须自动认得出才能提交", 而用户手选恰恰是因为认不出。
    """
    if any(c.isspace() for c in s):
        return "含空格或换行, 一行只能放一个链接"
    if len(s) > _MAX_URL_LEN:
        return f"超过 {_MAX_URL_LEN} 字符, 不像链接"
    return None


@router.post("/tasks/batch-create", response_model=BatchTaskOut)
def batch_create(payload: BatchTaskIn):
    """一次创建多个任务, 并在创建**之前**把每一行的问题标出来。

    与"前端循环调 /tasks/create"的区别不在于快, 而在于**结论出现得更早**:
    重复与无效输入在创建之前就摊在界面上, 而不是变成 40 个任务里 12 个失败、
    用户还得自己猜是哪 12 行粘错了。

    ⚠️ 公共参数(下载目录 / 画质 / 过滤条件)的校验放在逐行处理**之前**:
    那是"这一次提交"的问题, 不是某一行的问题, 报错就该整体 400。
    """
    download_dir = _validate_download_dir(payload.download_dir)
    options = _gallery_options(
        filters=payload.filters,
        quality=payload.quality,
        media=payload.media,
        album_title=payload.album_title,
        max_items=payload.max_items,
        aggregate_depth=payload.aggregate_depth,
    )
    if payload.proxy:
        options["proxy"] = payload.proxy

    lines = list(payload.urls or [])
    truncated = 0
    if len(lines) > BATCH_MAX_URLS:
        truncated = len(lines) - BATCH_MAX_URLS
        lines = lines[:BATCH_MAX_URLS]

    # 一次性查出哪些 URL 已经建过任务(逐条查就是 N 次扫描)
    cleaned = [(raw or "").strip() for raw in lines]
    keys = {_batch_key(s): s for s in cleaned if s}
    existing = db.find_tasks_by_urls(list(keys.keys())) if keys else {}

    items = []
    seen = {}                       # 比较键 -> 首次出现的行号
    for idx, raw in enumerate(lines, 1):
        s = cleaned[idx - 1]
        if not s:
            items.append(BatchTaskItemOut(line=idx, raw=raw or "", ok=False,
                                          reason="empty", message="空行"))
            continue
        why = _shape_reject(s)
        if why:
            items.append(BatchTaskItemOut(line=idx, raw=raw, url=s, ok=False,
                                          reason="invalid", message=why))
            continue
        key = _batch_key(s)
        if key in seen:
            items.append(BatchTaskItemOut(
                line=idx, raw=raw, url=s, ok=False, reason="duplicate_in_batch",
                message=f"与第 {seen[key]} 行重复"))
            continue
        seen[key] = idx
        if not payload.allow_duplicates and key in existing:
            items.append(BatchTaskItemOut(
                line=idx, raw=raw, url=s, ok=False, reason="duplicate_existing",
                existing_task_id=existing[key],
                message=f"已经建过任务 #{existing[key]}"))
            continue
        try:
            name, _resolved, warning = _pick_collector(s, payload.collector)
        except HTTPException as e:
            # 自动识别认不出是最常见的"粘错了"信号, 这里转成行级结论而不是
            # 整个请求 400 —— 一行粘错不该让另外 39 行也建不了
            items.append(BatchTaskItemOut(line=idx, raw=raw, url=s, ok=False,
                                          reason="invalid", message=e.detail))
            continue
        task_id = db.create_task(s, name, download_dir, options)
        task_manager.submit(task_id)
        items.append(BatchTaskItemOut(line=idx, raw=raw, url=s, ok=True,
                                      task_id=task_id, collector=name,
                                      warning=warning))

    return BatchTaskOut(
        items=items,
        created_count=sum(1 for i in items if i.ok),
        rejected_count=sum(1 for i in items if not i.ok),
        truncated_count=truncated,
    )


class PreviewIn(BaseModel):
    url: str
    collector: str = AUTO_COLLECTOR
    quality: Optional[str] = None
    media: Optional[str] = None
    album_title: Optional[str] = None
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
        # 全局带宽上限(bytes/s, 0 = 不限)。下发是必要的: 这是一个"看不见的阈值",
        # 而看不见的阈值会被反复误调 —— 用户会以为"下载慢"是站点的问题。
        "max_download_bytes_per_sec": int(
            getattr(settings, "max_download_bytes_per_sec", 0) or 0
        ),
    }


#: 前端可以随时查询/调整全局带宽上限(见 downloaders/ratelimit.ByteRateLimiter)。
class ByteRateIn(BaseModel):
    bytes_per_sec: int = 0


@router.get("/config/bandwidth")
def get_bandwidth():
    """当前全局带宽上限 + 实时速率(供诊断面板展示"限速到底生效没有")。"""
    lim = ratelimit.bytes_limiter
    return {"bytes_per_sec": int(lim.rate or 0)}


@router.post("/config/bandwidth")
def set_bandwidth(payload: ByteRateIn):
    """设置全局带宽上限; 0 = 不限速。

    ⚠️ 走 `ratelimit.set_byte_rate` 而不是直接改 settings: 桶在进程启动时就建好了,
    只改配置会表现为"接口返回成功但下载速度没变" —— 那正是最容易被归因成
    "限速功能是假的"的一类问题。
    """
    value = max(0, int(payload.bytes_per_sec or 0))
    return {"bytes_per_sec": ratelimit.set_byte_rate(value)}


#: 单页上限。不设上限的话一个 `page_size=100000` 就能把整表打出来, 分页白做。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


@router.get("/env/diagnose")
def env_diagnose():
    """环境自检: Python / 浏览器 / ffmpeg / 磁盘 / 下载目录。

    每一项都给三件事:**能不能用**、**现在是什么**、**不能用时怎么办**。
    前两件机器都会算, 诊断面板的全部价值在第三件 —— 只显示"ffmpeg 未找到",
    用户照样不知道下一步该干嘛。

    ⚠️ 等级区分 ok / warn / fail, 且**只有真正挡路的项目才是 fail**:
    ffmpeg 缺失不影响图片采集, 标成红色会让用户以为整个工具坏了, 于是先去
    解决一个其实不影响他当前任务的问题。
    """
    import os as _os
    import platform as _platform
    import shutil as _shutil
    import sys as _sys
    import time as _time

    from core.ffmpeg import find_ffmpeg

    items = []

    # ---- Python ----
    major, minor = _sys.version_info[:2]
    py_ok = (major, minor) >= (3, 11)
    items.append({
        "key": "python", "label": "Python",
        "level": "ok" if py_ok else "fail",
        "value": _platform.python_version(),
        "detail": _sys.executable,
        "hint": None if py_ok else "需要 Python 3.11 或更高版本(见 pyproject.toml)",
    })

    # ---- 浏览器(Playwright) ----
    # 两层都要查: 装了包但没下载浏览器是**最常见**的状态(install 与 install
    # chromium 是两个命令), 只查 import 会给出"浏览器正常"的假象。
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        try:
            with sync_playwright() as p:
                exe = p.chromium.executable_path
            if exe and Path(exe).is_file():
                items.append({
                    "key": "browser", "label": "浏览器", "level": "ok",
                    "value": "Chromium 可用", "detail": str(exe), "hint": None,
                })
            else:
                items.append({
                    "key": "browser", "label": "浏览器", "level": "warn",
                    "value": "浏览器未下载", "detail": str(exe or ""),
                    "hint": "执行 playwright install chromium 下载浏览器; "
                            "在此之前相册名会退回图集 ID, 视频页无法采集",
                })
        except Exception as e:
            items.append({
                "key": "browser", "label": "浏览器", "level": "warn",
                "value": "已安装但启动失败", "detail": str(e),
                "hint": "重新执行 playwright install chromium 试试",
            })
    except Exception:
        items.append({
            "key": "browser", "label": "浏览器", "level": "warn",
            "value": "未安装",
            "detail": "playwright 不可用",
            "hint": "pip install playwright && playwright install chromium; "
                    "不装也能采图片直链, 但取不到相册名与 m3u8",
        })

    # ---- ffmpeg ----
    # warn 而不是 fail: 只有 HLS 视频转封装需要它, 图片采集完全不受影响。
    exe = find_ffmpeg(refresh=True)
    items.append({
        "key": "ffmpeg", "label": "ffmpeg",
        "level": "ok" if exe else "warn",
        "value": Path(exe).name if exe else "未找到",
        "detail": exe or "",
        "hint": None if exe else
                "只影响 m3u8 视频(HLS 分片合流)。装法见 README; "
                "装好后无需重启服务, 这里点“重新检测”即可",
    })

    # ---- 磁盘 ----
    try:
        usage = _shutil.disk_usage(str(settings.download_dir))
        free, total = usage.free, usage.total
        floor = max(int(getattr(settings, "min_free_bytes", 0)), 0)
        if free < floor:
            level, hint = "fail", (f"剩余空间低于最低水位, 下载会被直接中止。"
                                   f"清理到 {floor // 1024 // 1024}MB 以上再开始")
        elif free < floor * 3:
            level, hint = "warn", "空间偏低, 大相册可能下到一半被中止"
        else:
            level, hint = "ok", None
        items.append({
            "key": "disk", "label": "磁盘空间", "level": level,
            "value": f"{free / 1024 ** 3:.1f}GB 可用 / 共 {total / 1024 ** 3:.1f}GB",
            "detail": str(settings.download_dir), "hint": hint,
        })
    except Exception:
        # 磁盘信息拿不到不是故障(见 core.disk.free_bytes 的说明), 别谎报红色
        items.append({
            "key": "disk", "label": "磁盘空间", "level": "warn",
            "value": "读取失败", "detail": str(settings.download_dir),
            "hint": "拿不到该分区信息, 下载不会因为这一项被拦住",
        })

    # ---- 下载目录可写 ----
    d = Path(settings.download_dir)
    try:
        writable = d.is_dir() and _os.access(str(d), _os.W_OK)
    except Exception:
        writable = False
    items.append({
        "key": "download_dir", "label": "下载目录",
        "level": "ok" if writable else "fail",
        "value": "可写" if writable else ("不存在" if not d.exists() else "不可写"),
        "detail": str(d),
        "hint": None if writable else "检查目录是否存在、当前用户是否有写权限",
    })

    # ---- 代理 ----
    # 只报"配了什么、有几条", 不做主动连通性探测 —— 诊断端点会被前端频繁调用,
    # 每次真去连一遍代理既慢又会在代理慢时把面板拖住。真正的健康信息由下载
    # 过程中的熔断状态提供(见 runtime/proxy 端点)。
    proxy_env = getattr(settings, "proxy", None) or ""
    items.append({
        "key": "proxy", "label": "代理",
        "level": "ok",
        "value": f"环境变量已设置" if proxy_env else "未设置(直连)",
        "detail": _mask_proxy(proxy_env),
        "hint": None if proxy_env else
                "需要走代理时设环境变量 UWC_PROXY, 或在创建任务时填写任务级代理",
    })

    return {
        "items": items,
        "all_ok": all(i["level"] == "ok" for i in items),
        # 只要没有 fail 就还能干活; warn 是"部分能力打折", 不该拦住用户
        "usable": not any(i["level"] == "fail" for i in items),
        "checked_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _mask_proxy(spec):
    """代理串脱敏: 只留协议与主机端口, 抹掉 user:pass。

    诊断面板会展示这段文本, 代理凭据(常是付费账号)不该出现在屏幕上,
    更不该被截图外传。
    """
    if not spec:
        return ""
    out = []
    for p in str(spec).split(","):
        p = p.strip()
        if not p:
            continue
        try:
            u = urlparse(p)
            if u.hostname:
                out.append(f"{u.scheme}://{u.hostname}:{u.port or ''}")
            else:
                out.append("***")
        except Exception:
            out.append("***")
    return ", ".join(out)


@router.get("/tasks", response_model=TaskListOut)
def list_tasks(
    q: Optional[str] = Query(None, description="按 URL 或任务名模糊搜索"),
    status: Optional[List[str]] = Query(None, description="状态筛选, 可重复传入"),
    collector: Optional[str] = Query(None, description="按采集器精确筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """任务列表: 搜索 / 状态筛选 / 采集器筛选 / 分页。

    ⚠️ 返回结构从"裸数组"改成了 `{items, total, ...}`。分页只有拿到总数才知道
    有几页, 而总数不该靠前端拉全量自己数 —— 那正是分页要避免的事。

    ⚠️ 搜索走 SQL LIKE, 关键词里的 `%` `_` 已被转义(见 database._like_pattern):
    不转义的话搜 `100%` 会变成"匹配任意串", 搜索看起来能用但结果不对,
    而用户只会以为是自己记错了。
    """
    total = db.count_tasks(q=q, status=_expand_statuses(status), collector=collector)
    offset = (page - 1) * page_size
    rows = db.list_tasks(
        q=q,
        status=_expand_statuses(status),
        collector=collector,
        limit=page_size,
        offset=offset,
    )
    return TaskListOut(
        items=[TaskOut(**dict(t)) for t in rows],
        total=total,
        page=page,
        page_size=page_size,
        # 空结果时 pages=0 而不是 1: 显示"第 1 / 0 页"比"共 0 条"更让人困惑
        pages=(total + page_size - 1) // page_size,
    )


# ⚠️ 必须声明在 /tasks/{task_id} **之前**: FastAPI 按注册顺序匹配, 否则
# "storage" 会被当成 task_id 去解析成 int 而返回 422。
@router.get("/library")
def resource_library(
    q: Optional[str] = Query(None, description="按本地路径或来源 URL 模糊搜索"),
    kind: Optional[str] = Query(None, description="按类型筛选: image / video / text"),
    album: Optional[str] = Query(None, description="按相册名精确筛选"),
    task_id: Optional[int] = Query(None, description="只看某个任务的产出"),
    tag: Optional[str] = Query(None, description="只看带这个标签的资源(大小写不敏感)"),
    favorite: bool = Query(False, description="只看收藏"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """跨任务资源库: 按相册 / 类型 / 标签 / 关键词浏览**已落盘**的产物。

    与 `/tasks/{id}/resources` 的区别: 那个是"这个任务采到了什么", 这个是
    "我手上有什么"。同一张图片被 sha256 去重复用过时会同时出现在两个任务下,
    资源库会**各列一条**并给出 `refs`(被几个任务引用) —— 这是有意的: 用户
    想删的是"某个任务的那条记录", 而真删文件与否由 refs 决定(见 DELETE 端点)。
    """
    total = db.library_count(q=q, kind=kind, task_id=task_id, album=album,
                             tag=tag, favorite=favorite)
    offset = (page - 1) * page_size
    rows = db.library_list(
        q=q, kind=kind, task_id=task_id, album=album,
        limit=page_size, offset=offset, tag=tag, favorite=favorite,
    )
    # ⚠️ refs 与 tags 都**批量取**。逐行查的话一页 40 条就是 80 次往返, 而这是
    # 最高频的接口 —— 列表页的"N+1"是最容易被写出来、也最难被察觉的一类慢。
    refs = db.resource_refs_many([r["local_path"] for r in rows])
    tagmap = db.tags_of([r["id"] for r in rows])
    items = []
    for r in rows:
        d = dict(r)
        d["refs"] = refs.get(d.get("local_path"), 0)
        d["tags"] = tagmap.get(d["id"], [])
        d["favorite"] = bool(d.get("favorite"))
        items.append(d)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "stats": db.library_stats(),
    }


@router.get("/library/tags")
def library_tag_list(limit: int = Query(200, ge=1, le=1000)):
    """标签清单(带资源数), 供筛选下拉与自动补全。"""
    items = [dict(r) for r in db.all_tags(limit)]
    return {"items": items, "total": len(items)}


@router.post("/library/tags", response_model=LibraryTagsOut)
def library_tag_edit(payload: LibraryTagsIn):
    """批量打标签 / 去标签。

    ⚠️ 校验失败(标签超长、超过每资源上限)时**整个请求失败**并说清是哪一条,
    而不是"能加的加上、剩下的悄悄丢掉" —— 后者会让用户以为标签打上了。
    """
    ids = _unique_ids(payload.ids)
    if not ids:
        return LibraryTagsOut(added=0, removed=0, touched=0)
    try:
        # clear 先执行: "清空再加"与"加了再清空"是两种完全不同的意图,
        # 而用户勾了"清空"又填了新标签时, 想要的显然是后者(清空是起点不是终点)。
        removed = db.remove_tags(ids, []) if payload.clear else \
            db.remove_tags(ids, payload.remove)
        added = db.add_tags(ids, payload.add)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return LibraryTagsOut(added=added, removed=removed, touched=len(ids))


@router.post("/library/favorite", response_model=LibraryFavoriteOut)
def library_set_favorite(payload: LibraryFavoriteIn):
    """批量收藏 / 取消收藏。"""
    ids = _unique_ids(payload.ids)
    n = db.set_favorite(ids, payload.value)
    return LibraryFavoriteOut(updated=n, value=bool(payload.value))


def _unique_ids(raw):
    """去重并保序地把入参转成 int 列表(非法值直接 400, 不静默跳过)。"""
    out, seen = [], set()
    for x in raw or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"非法资源 id: {x!r}")
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


@router.get("/library/albums")
def library_albums(limit: int = Query(200, ge=1, le=1000)):
    """有产物的相册名单(供资源库筛选下拉)。"""
    return {"items": [dict(r) for r in db.library_albums(limit)]}


# ⚠️ 批量与巡检这两个路由必须声明在 `/tasks/{task_id}` 之前(FastAPI 按注册顺序
# 匹配)。它们挂在 /library 前缀下, 本来就与 /tasks 不冲突, 但仍按既有惯例
# 集中放在这里, 免得日后有人把 /library/{id} 加进来时踩到顺序问题。

@router.post("/library/bulk-delete", response_model=LibraryBulkOut)
def library_bulk_delete(payload: LibraryBulkDeleteIn):
    """资源库批量删除。

    ⚠️ 文件是**最后一步、且带三道闸**才删的:

      1. `with_files` 必须显式为真(默认只删记录);
      2. 引用的行必须只剩这一条(`resource_refs <= 1`)—— sha256 去重时后到的
         任务只是复用路径、不复制文件, 删掉等于把别人的结果一并毁掉;
      3. 路径必须真的落在某个任务下载根之内 —— 库里的 local_path 是数据, 不能
         当可信路径直接 unlink。

    删记录而不是删任务: 用户在资源库里看到的粒度是"一张图", 不是"一个任务"。
    """
    out = LibraryBulkOut(requested=len(payload.ids))
    for rid in payload.ids:
        row = db.get_resource(rid)
        if not row:
            out.skipped.append(rid)
            continue
        local = db_row_field(row, "local_path")
        if payload.with_files and local:
            refs = db.resource_refs(local)
            if refs > 1:
                # 还有别的任务指着这个文件 —— 记录照删, 文件留着并如实回报
                out.kept_files.append(rid)
                db.delete_resource(rid)
                out.deleted += 1
                continue
            path = _safe_local_for_delete(row)
            if path is None:
                out.errors[str(rid)] = "路径不在任何任务下载根内, 未删文件"
            else:
                try:
                    if path.is_file():
                        out.bytes += path.stat().st_size
                        out.files += 1
                    # 半成品一并清: 只删最终文件会在用户目录里留下 xxx.jpg.part
                    discard_partial(path)
                    path.unlink(missing_ok=True)
                except OSError as e:
                    out.errors[str(rid)] = f"删除文件失败: {e}"
        db.delete_resource(rid)
        out.deleted += 1
    return out


def db_row_field(row, key):
    """读 sqlite3.Row 的可选列: 旧库缺列时返回 None 而不是抛 IndexError。

    与 `TaskManager._row_field` 同一件事 —— 那边是实例内部工具, 这边在 API 层,
    但"旧库缺列"这个现实是共享的, 所以判据也必须一致(缺列 == 视作没有值),
    否则同一份数据在两个层次上会得到两种结论。
    """
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _safe_local_for_delete(row):
    """把库里的 local_path 还原成"可以安全 unlink"的路径, 不安全返回 None。

    判据: 必须落在**该记录所属任务**的下载根之内。库里存的是我们自己写的路径,
    但"数据"与"可信输入"是两回事 —— 用户手改过库、或早期版本的记录混进来时,
    一行记录就能让批量删除去 unlink 任意文件。
    """
    local = db_row_field(row, "local_path")
    if not local:
        return None
    task = db.get_task(db_row_field(row, "task_id"))
    if not task:
        return None
    try:
        p = Path(str(local)).resolve()
        base = task_base_dir(task).resolve()
    except (OSError, ValueError):
        return None
    if p == base or not p.is_relative_to(base):
        return None
    return p


@router.get("/library/archive")
def library_archive(ids: str = Query(..., description="逗号分隔的资源 id")):
    """把选中的资源打成一个 zip 流式下载。

    ⚠️ 命名规则: `相册名/文件名`。资源库是跨任务的, 直接把一堆 `00001.jpg` 装进
    同一个包必然互相覆盖 —— 加一层相册目录是最省心的区分方式, 且与落盘布局
    (下载根/相册名/文件)一致, 解压后与用户目录里的结构对得上。
    """
    try:
        wanted = [int(x) for x in str(ids).replace(" ", "").split(",") if x]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids 必须是逗号分隔的资源 id")
    if not wanted:
        raise HTTPException(status_code=400, detail="未选中任何资源")
    if len(wanted) > 2000:
        raise HTTPException(status_code=400, detail="单次最多 2000 项, 请分批")

    named = []
    for rid in wanted:
        row = db.get_resource(rid)
        if not row:
            continue
        p = _resolve_safely_for_row(row)
        if p is None:
            continue
        task = db.get_task(db_row_field(row, "task_id"))
        album = (db_row_field(task, "name") if task else None) or "未命名"
        named.append((f"{album}/{p.name}", p))
    if not named:
        raise HTTPException(status_code=404, detail="选中的资源都没有可下载的文件")

    headers = {
        # 文件名可能是中文: 用 RFC 5987 的 filename* 而不是把原始字节塞进 header
        "Content-Disposition": (
            "attachment; filename=library.zip; "
            "filename*=UTF-8''" + quote("资源库.zip")
        )
    }
    return StreamingResponse(
        _zip_stream_named(named), media_type="application/zip", headers=headers
    )


def _resolve_safely_for_row(row):
    """按记录的所属任务校验并返回真实文件路径(不存在/越界返回 None)。"""
    task = db.get_task(db_row_field(row, "task_id"))
    if not task:
        return None
    local = db_row_field(row, "local_path")
    if not local:
        return None
    try:
        base = task_base_dir(task).resolve()
    except OSError:
        return None
    return _resolve_safely(base, local)


@router.post("/library/verify", response_model=LibraryVerifyOut)
def library_verify(payload: LibraryVerifyIn):
    """落盘后完整性巡检: 库里的 `local_path` 可能已被外部删除或截断。

    ⚠️ 只**标记**不删除。判据用的是 `core/mediacheck.py` 那套**算术级**证据
    (容器自述长度 / 尾部结束标记)与"文件缩小了"这一条, 都能被证明;
    认不出的容器一律放行(误报的代价是删掉一个好文件)。

    标记写进 `resources.error_kind` + `note`, 不改 `status` —— 文件可能仍然能看
    (比如截断的 JPEG 前半段还是好的), 把状态改成 failed 会让用户以为整份没了。
    """
    limit = max(1, min(int(payload.limit or 500), 5000))
    rows = db.done_resources_for_scan(task_id=payload.task_id, limit=limit)
    out = LibraryVerifyOut()
    for r in rows:
        out.checked += 1
        path = Path(str(r["local_path"]))
        kind = reason = None
        if not path.is_file():
            kind = KIND_MISSING
            reason = "库里有记录, 磁盘上找不到该文件(被外部删除或移动过)"
        else:
            reason = mediacheck.truncation_reason(path)
            if reason:
                kind = KIND_CORRUPT
            else:
                real = path.stat().st_size
                recorded = r["size"]
                if isinstance(recorded, int) and 0 < real < recorded:
                    # 文件比记录里小 = 被截断过(变大则可能是被替换成另一份内容,
                    # 那属于"源站换了内容", 不是损坏, 不在这里报)
                    kind = KIND_CORRUPT
                    reason = (
                        f"文件比记录里小: 实际 {real} 字节, 记录 {recorded} 字节"
                        f"(疑似被截断)"
                    )
        if not kind:
            continue
        item = LibraryVerifyItem(
            id=r["id"], task_id=r["task_id"],
            name=(r["filename"] or path.name), path=str(path),
            kind=kind, reason=reason,
        )
        out.items.append(item)
        if kind == KIND_MISSING:
            out.missing += 1
        else:
            out.truncated += 1
        try:
            db.update_resource(r["id"], error_kind=kind, note=reason)
            out.marked += 1
        except Exception:
            # 标记失败不影响这次巡检的结论 —— 报告本身仍然有效
            pass
    return out


@router.get("/library/partials")
def library_partials():
    """断点续传暂存区现状(见 core/partials.py)。

    没有它, 这块磁盘占用就是**隐形的**: 用户看到"下载目录 8GB", 但里面只有 6GB
    是看得见的产物, 那 2GB 是几个没下完的大视频躺在 `_meta/partial/` 里等着续传。
    隐形的占用会被当成"程序在偷偷吃盘", 所以这里连 TTL/预算一起下发 ——
    让人知道它**会自己过期**, 而不是无限涨。

    `bytes_h` / `max_bytes_h` 由后端算好: 单位换算只有一个实现(`core/filters.fmt_size`),
    前端各写一套迟早对不上(界面显示 "1.0 GB" 而实际是 GiB 这类问题没人会去查)。
    """
    st = partials.stats()
    st["bytes_h"] = fmt_size(st.get("bytes") or 0)
    st["max_bytes_h"] = fmt_size(st.get("max_bytes") or 0)
    return st


@router.delete("/library/partials")
def library_partials_clear():
    """清空暂存区, 立刻还磁盘。

    ⚠️ 代价只是"下次从头下", 不会下出坏文件 —— 所以这个按钮可以放心给用户。
    手动清空后 `park` 照旧工作, 暂存区会重新攒起来。
    """
    n, freed = partials.clear()
    return {"cleared": n, "bytes": freed, "bytes_h": fmt_size(freed)}


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


@router.get("/tasks/stats", response_model=TaskStatsOut)
def task_stats():
    """首页统计面板: 总量 + 按状态 + 按采集器 + 资源总量与体积。

    by_status 直接复用 count_tasks_by_status(); active 字段用 STATUS_GROUPS 的
    "active" 组展开后求和, 让前端不必自己算"正在跑的有几个"。
    """
    by_status = db.count_tasks_by_status()
    active_statuses = _expand_statuses(["active"])
    active = sum(by_status.get(s, 0) for s in active_statuses)
    return TaskStatsOut(
        total_tasks=sum(by_status.values()),
        total_resources=db.count_resources_total(),
        total_bytes=db.sum_downloaded_bytes(),
        by_status=by_status,
        by_collector=db.count_tasks_by_collector(),
        active=active,
        by_date=db.resources_by_date(30),
        download_series=db.stats_download_series(30),
        failure_reasons=db.failure_reasons(8),
        error_kinds=db.error_kinds(),
        error_kind_labels=KIND_LABELS,
        duplicates=db.duplicate_stats(),
    )


@router.get("/notifications", response_model=NotificationListOut)
def list_notifications():
    """通知中心数据源: 最近通知 + 未读数。前端轮询或经 SSE 拉取。"""
    rows = db.list_notifications(limit=50)
    items = [
        NotificationOut(
            id=r["id"],
            task_id=r["task_id"],
            level=r["level"],
            title=r["title"],
            body=r["body"],
            read=bool(r["read"]),
            created_time=r["created_time"],
        )
        for r in rows
    ]
    return NotificationListOut(items=items, unread=db.unread_notification_count())


@router.post("/notifications/read")
def mark_notifications_read(payload: MarkReadIn):
    """标记通知已读。不传 ids 则全部标记已读。"""
    db.mark_notifications_read(payload.ids)
    return {"ok": True}


@router.post("/tasks/bulk-action", response_model=BulkActionOut)
def bulk_action(payload: BulkActionIn):
    """对一组 task_id 执行同一动作(pause/resume/cancel/retry/delete)。

    每个任务独立调用对应的 task_manager 方法, 把结果归类成 ok / skipped /
    not_found。正在运行的任务不会被 delete(交给已有的 bulk-delete 语义, 这里
    直接按方法返回值归类), 单个任务失败不影响其它任务。
    """
    valid = {"pause", "resume", "cancel", "retry", "delete"}
    if payload.action not in valid:
        raise HTTPException(
            status_code=400, detail=f"不支持的动作: {payload.action} (可选: {', '.join(sorted(valid))})"
        )
    out = BulkActionOut(action=payload.action, requested=len(payload.task_ids))
    for tid in payload.task_ids:
        try:
            if payload.action == "pause":
                ok, err = task_manager.pause(tid)
            elif payload.action == "resume":
                ok, err = task_manager.resume(tid)
            elif payload.action == "cancel":
                ok, err = task_manager.cancel(tid)
            elif payload.action == "retry":
                ok, err = task_manager.retry(tid)
            else:  # delete
                if not db.get_task(tid):
                    out.not_found.append(tid)
                    continue
                task_manager.delete(tid, with_files=payload.with_files)
                out.ok.append(tid)
                continue
            if ok is None:
                out.not_found.append(tid)
            elif ok:
                out.ok.append(tid)
            else:
                out.skipped.append(tid)
                if err:
                    out.errors[str(tid)] = err
        except Exception as e:  # 单个任务异常不阻断其它任务
            out.errors[str(tid)] = str(e)
            out.skipped.append(tid)
    return out


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def get_task(task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    resources = [_resource_out(r, task) for r in db.get_resources(task_id)]
    return TaskDetail(
        **dict(task),
        resources=resources,
        resource_counts=db.summarize_resources(task_id),
        # 逐资源耗时: "这个任务为什么慢"的答案。集成在详情里而不是单开接口 ——
        # 多一次往返只会让打开详情更慢, 而它只在详情页用得上。
        resource_timing=db.resource_timing(task_id),
        error_kind_labels=KIND_LABELS,
    )


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


@router.post("/tasks/{task_id}/retry-failed", response_model=RetryFailedOut)
def retry_failed(task_id: int):
    """失败资源选择性重试: 只把 failed / skipped / gone 的资源重新派发下载。

    与整任务 retry(删除全部资源重来)不同, 这里保留已成功的资源, 只重下失败的
    那部分 —— 网络抖动/单张 403 这类局部失败时最省时。复用已有的单资源
    submit_resource(它直接把资源丢进下载执行器), 逐个派发。

    `gone` 也纳入: 自动流程**不**重试它(源站没了, 重试是空转), 但这个接口是用户
    主动点的 —— 403 可能因为换了代理/Referer 就好了, 404 也可能是站点临时抽风。
    API 的克制与用户的选择权是两件事。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    retried = []
    for r in db.get_resources_with_status(task_id, ["failed", "skipped", "gone"]):
        ok, err = task_manager.submit_resource(task_id, r["id"])
        if ok:
            retried.append(r["id"])
    return RetryFailedOut(task_id=task_id, retried=retried, count=len(retried))


@router.get("/tasks/{task_id}/proxy")
def task_proxy(task_id: int):
    """任务级代理池的实时健康(轮换顺序 + 熔断状态)。

    只对**运行中**的任务有意义: 任务结束后条目回收, 返回空 lines 且
    running=False —— 前端据此不显示这一块, 而不是显示一堆"正常"骗人。
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    lines = task_manager.proxy_snapshot(task_id)
    # 跨会话的熔断历史(见 core/proxy_health.py): 任务结束后内存条目会被回收,
    # 但"这条线上一轮就不行"这条信息仍然有价值 —— 它正是重启后不必重新踩坑的依据。
    try:
        from core import proxy_health

        history = proxy_health.snapshot()
    except Exception:
        history = []
    # 从 options 里把配置读回来, 好让前端在没有活动 worker 时也能告诉用户
    # "这个任务配了几条线"(否则一块空白让人以为没配)。
    spec = ""
    try:
        task = db.get_task(task_id)
        raw = task["options"] if task else None
        if raw:
            opts = json.loads(raw) if isinstance(raw, str) else dict(raw)
            spec = (opts or {}).get("proxy") or ""
    except Exception:
        spec = ""
    return {
        "task_id": task_id,
        "running": bool(lines),
        "configured": len([p for p in str(spec).split(",") if p.strip()]),
        "masked_spec": _mask_proxy(spec),
        "lines": lines,
        "history": history,
    }


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
    # 结果是新选项在前端传了却被静默丢掉 —— 订阅会长期反复跑, 悄悄少一个选项
    # 比直接报错难查得多。
    options = _gallery_options(
        quality=payload.quality,
        media=payload.media,
        album_title=payload.album_title,
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
    data = read_manifest(layout.meta_dir(task_base_dir(task), task_id))
    if data is None:
        raise HTTPException(status_code=404, detail="manifest not found")
    return data


def _verify_local_file(raw):
    """把 `?path=` 还原成一个**可信**的本地文件, 返回 (task, row, path, base)。

    ⚠️ 两道校验缺一不可:

      1. **库里必须有这条记录。** `downloads/` 是用户自己的目录, 里面还可能有
         他放进去的别的东西; 只判"在下载根内"等于给了任意文件读取权
         (`.partsrc`、别的任务自定义目录、以及一切恰好落在 downloads 下的文件)。
      2. **必须在该任务的下载根内。** 记录本身也可能指向去重复用时别的目录
         的文件(见 task_manager._download_one 的 dedup 分支), 那时按本任务
         的根去判会拒掉 —— 所以这里用**记录所属任务**的根, 而不是调用方给的。
    """
    if not raw or not str(raw).strip():
        raise HTTPException(status_code=400, detail="缺少 path 参数")
    try:
        p = Path(str(raw)).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="invalid path")
    row = db.find_resource_by_path(str(p))
    if not row:
        raise HTTPException(status_code=404, detail="resource not found")
    task = db.get_task(row["task_id"])
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    base = task_base_dir(task).resolve()
    if not p.is_relative_to(base):
        raise HTTPException(status_code=403, detail="forbidden")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return task, row, p, base


# ⚠️ 这两个路由必须在 `/files/{task_id}/{file_path:path}` **之前**注册。
# 那个通配路由虽然需要两段路径(所以单段的 /files/raw 理论上不冲突), 但把字面量
# 路由放在通配之前是 FastAPI 的硬纪律: 一旦哪天有人把通配改成可选段,
# `/files/raw` 就会被解析成 task_id="raw" 并返回 422 —— 而前端看到的只是
# "缩略图不显示", 完全联想不到是路由顺序。
@router.get("/files/raw")
def serve_raw(path: str = Query(..., description="资源记录的本地绝对路径")):
    """按库里的路径取原文件(资源库网格与预览用)。

    与 `/files/{task_id}/{file}` 的分工: 那个按**任务**定位, 这个按**库里的路径**
    定位 —— 资源库是跨任务视图, 条目上只有 local_path, 没有唯一 task_id 可用
    (同一张图被多个任务 sha256 去重复用时会有多条记录指向同一个文件)。
    """
    _task, _row, p, _base = _verify_local_file(path)
    return FileResponse(p)


@router.get("/files/thumb")
def serve_thumb(
    path: str = Query(..., description="资源记录的本地绝对路径"),
    size: int = Query(thumbs.DEFAULT_MAX_SIDE, ge=64, le=1024,
                      description="缩略图长边上限(像素)"),
):
    """按库里的路径取缩略图; 生成不了就**回退原图**。

    ⚠️ 回退而不是返回错误图: 缩略图是加速手段。没有 ffmpeg、或这张图 ffmpeg 解不开
    (冷门格式/文件损坏)时, 用户仍然应该**看得到内容**, 只是慢一点 ——
    给一个破图标等于把人要的信息拿走了。
    """
    _task, _row, p, base = _verify_local_file(path)
    thumb = thumbs.ensure_thumb(base, p, max_side=size)
    if thumb is None:
        return FileResponse(p)
    resp = FileResponse(thumb)
    # 缩略图内容由源文件 mtime 决定, 而 URL 里带着源路径 —— 换源即换 URL,
    # 所以可以放心让浏览器长缓存, 省掉"每次滚动都重新请求"的开销。
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


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
        # ⚠️ 这里原来多传了一个 task_id: `_zip_stream` 只收两个参数, 而生成器函数
        # 直到**第一次 next() 才执行函数体** —— 于是 TypeError 不发生在调用处,
        # 而是发生在 Starlette 迭代响应的时候, 表现成"打包接口 500 且没有堆栈
        # 指向这里"。有了回归测试(见 test_features_v34)之后这条路径才被走到过。
        _zip_stream(base, items),
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


class _ZipSink(io.RawIOBase):
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


def _zip_stream_named(named):
    """`named` = [(归档内名字, 真实路径)] 的流式 zip。

    与 `_zip_stream` 的区别: 那个按"同一个下载根的相对路径"命名; 资源库的条目
    可能跨多个根(任务可以各自指定下载目录), 相对路径无从算起, 所以命名由调用方
    给定。两者的压缩策略与流式机制共用, 不各写一遍。
    """
    sink = _ZipSink()
    counter = {}
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
        for arcname, path in named:
            zf.write(path, arcname=_dedup_name(arcname, counter))
            chunk = sink.take()
            if chunk:
                yield chunk
    tail = sink.take()
    if tail:
        yield tail


def _zip_stream(base, items):
    """边打包边吐数据的生成器, 不把整个压缩包攒在内存里。

    图片/视频本身已经是压缩过的媒体, deflate 几乎压不动却要吃掉不少 CPU,
    所以这里用 ZIP_STORED(仅归档, 不再压缩)。
    """
    return _zip_stream_named(
        [(p.relative_to(base).as_posix(), p) for _, p in items]
    )


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
