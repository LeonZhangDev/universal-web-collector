"""通用资源解析器: API > Network > JS > DOM 四级链路 + 原图质量选择。

与具体站点无关, 任何 collector 均可复用。
解析器产出 Resource dict: {type, url, headers, source}
"""
import json
import re
from urllib.parse import urljoin

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".svg")
VIDEO_EXTS = (".mp4", ".m3u8", ".ts", ".webm", ".mov", ".mkv")
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus")
DOC_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".epub")

THUMB_PATTERNS = [
    r"/thumbs?/",
    r"thumb[_.-]",
    r"_thumbnail",
    r"(?:^|/)(\d{2,4})x(\d{2,4})(?=[-/.])",
    r"(?:^|/)(\d{2,4})px-",
    r"-\d{2,4}x\d{2,4}(?=\.)",
    r"/small/",
    r"/preview/",
]
_THUMB_RE = [re.compile(p, re.IGNORECASE) for p in THUMB_PATTERNS]
_SIZE_RE = re.compile(r"(\d{2,4})x(\d{2,4})", re.IGNORECASE)
_PX_RE = re.compile(r"(\d{2,4})px", re.IGNORECASE)


def classify(url, content_type=None):
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct.startswith("video/"):
            return "video"
        if ct.startswith("image/"):
            return "image"
        if ct.startswith("audio/"):
            return "audio"
        if ct in ("application/pdf",):
            return "doc"
    u = url.lower().split("?")[0]
    if any(u.endswith(e) for e in VIDEO_EXTS):
        return "video"
    if any(u.endswith(e) for e in AUDIO_EXTS):
        return "audio"
    if any(u.endswith(e) for e in DOC_EXTS):
        return "doc"
    return "image"


def looks_like_resource(url):
    u = url.lower().split("?")[0]
    return any(u.endswith(e) for e in IMAGE_EXTS + VIDEO_EXTS + AUDIO_EXTS + DOC_EXTS)


def extract_urls(obj, base=None, out=None, depth=0):
    """递归从 JSON 结构/字符串中提取资源 URL。"""
    if out is None:
        out = []
    if depth > 8:
        return out
    if isinstance(obj, str):
        for m in re.findall(r'https?://[^\s"\'<>()\\]+', obj):
            if looks_like_resource(m):
                out.append(m)
    elif isinstance(obj, dict):
        for v in obj.values():
            extract_urls(v, base, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            extract_urls(v, base, out, depth + 1)
    return out


def _request_headers(playwright_request):
    h = playwright_request.headers
    keep = {}
    for k in ("cookie", "referer", "user-agent"):
        if h.get(k):
            keep[k] = h[k]
    return keep


class NetworkParser:
    """监听浏览器 Response, 捕获媒体资源及其请求头。"""

    def __init__(self):
        self.resources = []

    def on_response(self, resp):
        try:
            url = resp.url
            ctype = (resp.headers or {}).get("content-type", "")
            is_media = (
                ctype.startswith(("image/", "video/", "audio/"))
                or ctype == "application/pdf"
            )
            if not is_media and not looks_like_resource(url):
                return
            self.resources.append(
                {
                    "type": classify(url, ctype),
                    "url": url,
                    "headers": _request_headers(resp.request),
                    "source": "network",
                }
            )
        except Exception:
            pass


class APIDetector:
    """捕获 JSON 接口响应, 从数据结构中提取资源 URL。"""

    def __init__(self):
        self.resources = []

    def on_response(self, resp):
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype:
                return
            body = resp.text()
            data = json.loads(body)
            urls = extract_urls(data)
            headers = _request_headers(resp.request)
            for u in urls:
                self.resources.append(
                    {"type": classify(u), "url": u, "headers": headers, "source": "api"}
                )
        except Exception:
            pass


class JSStateParser:
    """从页面 HTML(含内嵌 script 的 JS 状态变量)中提取资源 URL。"""

    def __init__(self, page_url):
        self.page_url = page_url
        self.resources = []

    def parse(self, html):
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S | re.I)
        for s in scripts:
            if not s.strip():
                continue
            for u in extract_urls(s):
                self.resources.append(
                    {
                        "type": classify(u),
                        "url": u,
                        "headers": {"referer": self.page_url},
                        "source": "js",
                    }
                )


class DOMParser:
    """从 DOM 节点属性中提取资源 URL。"""

    def __init__(self, page_url):
        self.page_url = page_url
        self.resources = []

    def parse(self, page):
        selectors = [
            "img[src]", "img[data-src]", "img[data-original]",
            "video[src]", "video source[src]",
            "source[src]", "a[href]",
            "audio[src]", "audio source[src]",
        ]
        seen = set()
        for sel in selectors:
            for el in page.query_selector_all(sel):
                for attr in ("src", "data-src", "data-original", "href"):
                    v = el.get_attribute(attr)
                    if not v:
                        continue
                    u = urljoin(self.page_url, v)
                    if u in seen or not looks_like_resource(u):
                        continue
                    seen.add(u)
                    self.resources.append(
                        {
                            "type": classify(u),
                            "url": u,
                            "headers": {"referer": self.page_url},
                            "source": "dom",
                        }
                    )


# ---- 质量选择: 原图优先, 缩略图降级 ----

def is_thumbnail(url):
    u = url.split("?")[0]
    return any(r.search(u) for r in _THUMB_RE)


def image_score(url):
    """尺寸得分, 越大越好; 非缩略图得高分。"""
    u = url.split("?")[0]
    score = 0
    m = _SIZE_RE.search(u) or _PX_RE.search(u)
    if m:
        score = max(int(x) for x in m.groups() if x)
    if not is_thumbnail(url):
        score += 100000
    return score


def base_identity(url):
    """去掉缩略图/尺寸修饰后的身份标识, 用于同图去重。"""
    u = url.split("?")[0].lower()
    u = re.sub(r"/thumbs?/", "/", u)
    u = re.sub(r"(?:^|/)\d{2,4}x\d{2,4}(?=[-/.])", "/", u)
    u = re.sub(r"(?:^|/)\d{2,4}px-", "/", u)
    u = re.sub(r"-\d{2,4}x\d{2,4}(?=\.)", "", u)
    u = re.sub(r"thumb[_.-]|_thumbnail", "", u)
    u = re.sub(r"/small/|/preview/", "/", u)
    u = re.sub(r"/+", "/", u)
    return u


def merge_by_priority(parser_results):
    """按 API > network > js > dom 合并去重, 同图取原图/大图。

    parser_results: {source: [resource, ...]}
    """
    priority = ["api", "network", "js", "dom"]
    merged = {}
    for source in priority:
        for r in parser_results.get(source, []):
            key = r["url"].split("?")[0]
            if key not in merged:
                merged[key] = r
    return list(merged.values())


def select_quality(resources):
    """图片按 base_identity 分组, 组内保留得分最高者; 其余类型全保留。"""
    images, others = [], []
    for r in resources:
        (others if r["type"] != "image" else images).append(r)

    best = {}
    for r in images:
        ident = base_identity(r["url"])
        cur = best.get(ident)
        if cur is None or image_score(r["url"]) > image_score(cur["url"]):
            best[ident] = r
    return list(best.values()) + others
