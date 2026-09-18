"""资源过滤规则。

过滤发生在两个阶段:
1. URL 层面(无需网络请求): 类型白名单 / 扩展名黑白名单 / URL 关键词
2. 大小层面(需 HEAD 探测 Content-Length): min_size / max_size

被过滤掉的资源不下载, 标记 status=filtered 并把原因写入 note,
保证前端能看到"为什么这个资源没下载", 而不是静默消失。

options 结构(存 tasks.options JSON):
{
  "types": ["image", "video"],        # 资源类型白名单, 空=不限
  "exts": ["jpg", "png"],             # 扩展名白名单, 空=不限
  "exclude_exts": ["gif", "svg"],     # 扩展名黑名单
  "keywords": [],                     # URL 须包含其一, 空=不限
  "exclude_keywords": ["thumb"],      # URL 包含任一则排除
  "min_size": "10KB",                 # 最小文件大小(支持 500KB/2MB/1024)
  "max_size": "50MB"                  # 最大文件大小
}
"""

import re
from urllib.parse import unquote, urlparse

import requests

from core.config import DEFAULT_ACCEPT, settings

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
}

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]*)$")


def parse_size(value):
    """'1MB' / '500KB' / '1024' -> 字节数。空值或非法值返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    s = str(value).strip().lower()
    if not s:
        return None
    m = _SIZE_RE.match(s)
    if not m:
        return None
    unit = m.group(2).lower()
    if unit not in _SIZE_UNITS:
        return None
    return int(float(m.group(1)) * _SIZE_UNITS[unit])


def fmt_size(n):
    """字节数 -> 人类可读字符串。"""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if unit == "B" and n < 1024:
            return f"{n:.0f} B"
        if unit == "GB":
            return f"{n:.2f} GB"
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def url_ext(url):
    """从 URL 提取小写扩展名(不含点)。无扩展名返回 ''。"""
    try:
        path = unquote(urlparse(url).path or "")
    except Exception:
        return ""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].strip().lower()
    # 过长的"扩展名"几乎肯定是路径噪音, 直接视为无扩展名
    return ext if 0 < len(ext) <= 8 and ext.isalnum() else ""


def _norm_exts(exts):
    if not exts:
        return []
    if isinstance(exts, str):
        exts = re.split(r"[,\s]+", exts)
    out = []
    for e in exts:
        if not isinstance(e, str):
            continue
        e = e.strip().lower().lstrip(".")
        if e:
            out.append(e)
    return out


def _norm_words(words):
    if not words:
        return []
    if isinstance(words, str):
        words = re.split(r"[,\s]+", words)
    return [w.strip() for w in words if isinstance(w, str) and w.strip()]


class Filters:
    """一组过滤规则。默认(空配置)放行一切。"""

    def __init__(self, options=None):
        o = options or {}
        if isinstance(o, str):
            import json

            try:
                o = json.loads(o)
            except Exception:
                o = {}
        self.types = {t.strip().lower() for t in _norm_words(o.get("types"))}
        self.allow_exts = set(_norm_exts(o.get("exts")))
        self.deny_exts = set(_norm_exts(o.get("exclude_exts")))
        self.keywords = _norm_words(o.get("keywords"))
        self.exclude_keywords = _norm_words(o.get("exclude_keywords"))
        self.min_size = parse_size(o.get("min_size"))
        self.max_size = parse_size(o.get("max_size"))
        # 只有配置了大小区间才需要 HEAD 探测, 避免额外的网络开销
        self.need_size = self.min_size is not None or self.max_size is not None

    @property
    def active(self):
        return bool(
            self.types
            or self.allow_exts
            or self.deny_exts
            or self.keywords
            or self.exclude_keywords
            or self.need_size
        )

    def match_size(self, size):
        """大小校验。通过返回 None, 否则返回原因。size 为 None 时放行。"""
        if size is None:
            return None
        if self.min_size is not None and size < self.min_size:
            return f"太小 {fmt_size(size)} < 下限 {fmt_size(self.min_size)}"
        if self.max_size is not None and size > self.max_size:
            return f"太大 {fmt_size(size)} > 上限 {fmt_size(self.max_size)}"
        return None

    def match_url(self, rtype, url):
        """URL/类型层面校验。通过返回 None, 否则返回原因。"""
        low = (url or "").lower()
        if self.types and (rtype or "").lower() not in self.types:
            return f"类型 {rtype} 不在白名单"

        for kw in self.exclude_keywords:
            if kw.lower() in low:
                return f"命中排除关键词 '{kw}'"

        if self.keywords and not any(kw.lower() in low for kw in self.keywords):
            return "未命中包含关键词"

        ext = url_ext(url)
        if ext and ext in self.deny_exts:
            return f"扩展名 .{ext} 在黑名单"
        # 无扩展名的 URL(常见于 CDN)无法在下载前判定, 一律放行,
        # 交由下载后的实际内容决定, 避免误杀
        if self.allow_exts and ext and ext not in self.allow_exts:
            return f"扩展名 .{ext} 不在白名单"
        return None

    def summarize(self):
        """给日志用的一行摘要。"""
        if not self.active:
            return "无过滤条件"
        parts = []
        if self.types:
            parts.append("类型=" + ",".join(sorted(self.types)))
        if self.allow_exts:
            parts.append("仅=" + ",".join(sorted(self.allow_exts)))
        if self.deny_exts:
            parts.append("排除=" + ",".join(sorted(self.deny_exts)))
        if self.keywords:
            parts.append("含=" + ",".join(self.keywords))
        if self.exclude_keywords:
            parts.append("不含=" + ",".join(self.exclude_keywords))
        if self.need_size:
            parts.append(
                f"大小={fmt_size(self.min_size) if self.min_size is not None else '不限'}"
                f"~{fmt_size(self.max_size) if self.max_size is not None else '不限'}"
            )
        return "; ".join(parts)


def probe_size(url, headers=None, timeout=None):
    """HEAD 探测 Content-Length, 返回字节数。

    探测失败(405/超时/无 Content-Length)返回 None —— 此时一律放行,
    不因为探针本身的问题误杀资源。
    """
    try:
        # Accept 不可省: 部分 WAF 只给 */* 或不给会直接 403, 导致探测全部失败
        h = {"User-Agent": settings.user_agent, "Accept": DEFAULT_ACCEPT}
        for k in ("cookie", "referer", "user-agent", "accept"):
            v = (headers or {}).get(k)
            if v:
                h[k.title()] = v
        if settings.proxy:
            proxies = {"http": settings.proxy, "https": settings.proxy}
        else:
            proxies = None
        resp = requests.head(
            url,
            headers=h,
            timeout=timeout or settings.request_timeout,
            allow_redirects=True,
            proxies=proxies,
        )
        if resp.status_code >= 400:
            return None
        cl = resp.headers.get("Content-Length")
        if cl and str(cl).strip().isdigit():
            return int(cl)
    except Exception:
        return None
    return None
