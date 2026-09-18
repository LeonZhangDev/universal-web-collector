"""输出文件名与目录组织。

职责边界
========
下载器只负责"把字节写到给定的路径", **不参与命名决策**; 命名集中在这里。
这样 `downloaders/*` 不会长出一堆 `if gallery: 用相册名 else: 用 URL 末段`,
新增站点也不需要改下载层。

模板变量
========
    {task_id}  任务 ID
    {site}     站点标识(注册域去点): img.xchina.io -> xchina
    {host}     完整主机名: img.xchina.io
    {type}     资源类型: image / video / audio / doc / text
    {seq}      资源在本任务中的序号, 从 1 开始
    {seq4}     序号补零至 4 位: 1 -> 0001
    {name}     URL 末段文件名(含扩展名): 00046_600x0.webp
    {stem}     URL 末段去扩展名: 00046_600x0
    {ext}      URL 末段扩展名(不含点): webp; 无扩展名时为空串
    {album}    分组名(相册/图集 ID), 由采集器给出; 缺失时回退 {site}
    {date}     采集日期 YYYYMMDD

默认模板见 config.yaml 的 `name_template`。留空等价于 `{name}` —— 与历史
行为一致(平铺放在任务目录下, 文件名取 URL 末段)。

安全
====
模板由用户填写, 必须防止路径穿越: 渲染结果会
  * 拆出 `..` 段并拒绝绝对路径;
  * 逐段清洗掉 Windows 非法字符与控制字符;
  * 空结果回退 `{name}`。
最终路径仍由调用方拼在任务目录下, 这里只保证相对部分是安全的。
"""

import re
import time
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlparse

_DEFAULT = "{name}"

_UNSAFE = r'[\\/:*?"<>|\x00-\x1f\x7f]'

_PUBLIC_SUFFIX_HINTS = (
    "co.uk", "com.cn", "org.cn", "net.cn", "com.hk", "co.jp", "com.tw",
)


def _url_tail(url):
    """取 URL 末段文件名(已去除查询串与百分号转义)。"""
    raw = unquote(url.split("?")[0].rstrip("/"))
    tail = raw.rsplit("/", 1)[-1]
    return tail or ""


def _split_ext(name):
    """拆分文件名与扩展名, 返回 (stem, ext); ext 不含点, 无则空串。"""
    m = re.match(r"^(.+?)(\.[A-Za-z0-9]{1,8})$", name)
    if not m:
        return name, ""
    return m.group(1), m.group(2).lstrip(".").lower()


def registered_domain(host):
    """由主机名推断注册域: img.xchina.io -> xchina.io。

    只覆盖常见情况: 默认取末尾两段, 再修正常见的二级后缀
    (如 example.co.jp -> example.co.jp)。拿不到结果时原样返回 host。
    """
    parts = [p for p in (host or "").lower().split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    tail2 = ".".join(parts[-2:])
    if tail2 in _PUBLIC_SUFFIX_HINTS:
        tail2 = ".".join(parts[-3:]) if len(parts) >= 3 else tail2
    return tail2 or host.lower()


def site_of(url):
    """站点标识: 注册域去掉点(img.xchina.io -> xchina)。"""
    host = urlparse(url).hostname or ""
    return registered_domain(host).split(".")[0] or "site"


def _clean_segment(seg):
    """清洗单个路径段: 去非法字符、去首尾空白与控制产生的点缀。"""
    seg = re.sub(_UNSAFE, "_", seg).strip()
    # 结尾的点在 Windows 上会被系统悄悄去掉("a." 落地变成 "a"),
    # 一律换成下划线, 让模板结果与实际文件名保持一致
    while seg.endswith("."):
        seg = seg[:-1] + "_"
    if not seg:
        return ""
    return seg[:120]


def clean_segment(seg):
    """公开版 `_clean_segment`: 把任意文本清洗成单个安全的路径段。

    采集器想用「相册标题」这类站点文本做目录名时调用它 —— 标题里可能带
    `/` `:` `?` 等字符, 不先清洗就会多出一层目录或直接拼出非法路径。
    返回空串表示清洗后没有可用内容, 调用方应回退到别的命名方案。
    """
    if not seg or not isinstance(seg, str):
        return ""
    return _clean_segment(seg)


def safe_relative(path_str):
    """把一个相对路径字符串清洗成安全的相对路径(统一用 / 分隔)。

    返回 None 表示清洗后无有效内容(调用方应回退默认命名)。
    """
    if not path_str:
        return None
    raw = path_str.replace("\\", "/")
    if any(sep in raw for sep in ("://",)) or raw.startswith("/"):
        return None
    for probe in (PureWindowsPath(raw), PurePosixPath(raw)):
        if probe.is_absolute() or probe.drive or probe.root:
            return None
    segs = raw.split("/")
    # 出现 "." / ".." 要么是误写, 要么是想往外逃 —— 两种情况都不该替它"擦干净"
    # (把 ".." 洗成 "_" 会让模板作者以为是路径写对了, 只是文件名怪),
    # 直接整体拒绝最清楚: 调用方回退默认命名, 用户立刻发现模板有问题。
    if any(s in (".", "..") for s in segs):
        return None
    segs = [_clean_segment(s) for s in segs]
    segs = [s for s in segs if s]
    if not segs:
        return None
    return "/".join(segs)


def _fallback_name(url, rtype):
    """模板渲染失败时的兜底文件名: URL 末段, 没有末段则用 sha1 片段。"""
    import hashlib

    tail = _url_tail(url)
    if not tail or "." not in tail:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        ext = {"image": ".jpg", "video": ".mp4", "text": ".txt"}.get(rtype, ".bin")
        return digest + ext
    return re.sub(_UNSAFE, "_", tail)


def build_context(task_id, url, rtype, seq, album=None, now=None):
    """构造模板可用的变量字典。"""
    host = urlparse(url).hostname or ""
    name = _url_tail(url)
    if name and "." not in name:
        name = ""
    stem, ext = _split_ext(_fallback_name(url, rtype))
    moment = now or time.localtime()
    return {
        "task_id": str(task_id),
        "site": site_of(url),
        "host": host,
        "type": rtype or "file",
        "seq": str(seq),
        "seq4": f"{seq:04d}",
        "name": _fallback_name(url, rtype),
        "stem": stem,
        "ext": ext,
        "album": _clean_segment(album) if album else site_of(url),
        "date": time.strftime("%Y%m%d", moment),
    }


def render(template, ctx, url, rtype):
    """渲染模板, 返回安全的相对路径字符串(失败/越界时回退 {name})。

    未知变量保留原样(如 `{foo}`), 让问题暴露在文件名上而不是静默吞掉 ——
    用户看到文件叫 `{foo}.jpg` 立刻知道模板写错了。
    """
    template = (template or _DEFAULT).strip()
    try:
        raw = template.format_map(_SafeDict(ctx))
    except (KeyError, ValueError, IndexError, AttributeError):
        raw = ctx.get("name", "")
    out = safe_relative(raw)
    return out or ctx.get("name") or _fallback_name(url, rtype)


class _SafeDict(dict):
    """format_map 用的字典: 缺失键保留为 `{key}` 而不是抛 KeyError。"""

    def __missing__(self, key):
        return "{" + key + "}"
