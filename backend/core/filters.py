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
  "max_size": "50MB",                 # 最大文件大小
  "min_image_bytes": "1KB",           # 图片体积下限(专治 1x1 跟踪像素/广告占位图)
  "exclude_ad": true,                 # 排除广告位/站点装饰/跟踪像素(见 _match_ad)
  "min_width": 400,                   # 图片最小宽度(px) —— 横幅/按钮的杀手
  "min_height": 400,                  # 图片最小高度(px)
  "min_pixels": 300000                # 最小总像素(宽 x 高), 挡小方块与细长条
}

"什么是有效资源"四道关:
1. **长得像不像**(URL 层): 类型/扩展名/关键词, 以及广告位与站点装饰
2. **够不够格**(体积层): min/max_size, 图片专属下限
3. **尺寸对不对**(尺寸层): min_width/min_height/min_pixels。横幅(728x90)、
   按钮(88x31)、信标(1x1)都是极端长宽比或极小尺寸, 而相册图极少是 300x250
   —— 文件名和体积都看不出这一点, 只有量宽高才认得出。⚠️ 判定放在**下载后**
   (见 task_manager 尺寸终检), 因为那时手上是完整文件, 零误判。
4. **是不是真的**(内容层): 下载后按 Content-Type 复核 —— 见 downloaders 的
   require_image(越界 URL 会返回 200+html, 光看状态码会被骗)

⚠️ 第 1 关的**广告识别一律按路径分段精确匹配, 绝不做子串包含**:
子串匹配会把 `/photos2/my-logo-album/0001.jpg` 这种正常资源误杀,
而"静默少采几张"比"多采一张广告"难查得多。
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


def _truthy(value, default=False):
    """options 里的布尔值兼容(True/"true"/"1"/1 为真, 其余为假)。

    未显式配置(null/"")返回 default —— 与"写了 false"区分开, 否则
    options 里缺这个键会被当成"用户明确关掉了"。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _coerce_size(value):
    """把 size 规整为 int 或 None。

    资源 size 经 JSON/DB 往返可能变成字符串("1047527424"), 也可能就是 int/None。
    统一成 int, 让 match_size 的数值比较不会因 str < int 抛 TypeError。
    非法值(非数字、负数)返回 None(上网把"判不出大小"当成放行, 交给下载后复核)。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    return None


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


#: 广告位: URL **路径分段**命中即排除(整段匹配, 不含扩展名)。
#: 这类词出现在路径里几乎不可能是相册内容。
_AD_SEGMENTS = frozenset({
    "ad", "ads", "advert", "advertising", "advertisement", "adserver",
    "banner", "banners", "sponsor", "sponsored", "promo", "promotion",
    "popup", "popunder", "adbox", "adimg", "adimage",
})

#: 站点装饰 / 占位图: 只看**最后一段文件名**的 stem, 且必须完全相等。
#: (放在 stem 而不是路径段, 是因为 /static/logo.png 的 logo 在文件名里)
_AD_STEMS = frozenset({
    "pixel", "tracking", "track", "spacer", "blank", "placeholder",
    "default", "sprite", "favicon", "logo", "icon", "noimage", "no-image",
    "noimg", "loading", "placeholder-image",
})

#: 1x1 / 2x2 这类尺寸的跟踪像素文件名
_AD_DIM_RE = re.compile(r"^(\d+)x(\d+)$")


def _norm_words(words):
    if not words:
        return []
    if isinstance(words, str):
        words = re.split(r"[,\s]+", words)
    return [w.strip() for w in words if isinstance(w, str) and w.strip()]


def _match_ad(url):
    """广告位 / 站点装饰 / 跟踪像素识别。命中返回原因, 否则 None。

    ⚠️ 一律**路径分段精确匹配**: 不做子串包含。
    子串匹配会把 `/photos2/my-logo-album/0001.jpg` 这种正常相册误杀,
    而"静默少采几张"比"多采一张广告"难查得多 —— 宁可漏判也不误杀。
    """
    try:
        p = urlparse(url or "")
        path = unquote(p.path or "").lower()
    except Exception:
        return None
    segs = [s for s in path.split("/") if s]
    if not segs:
        return None
    for s in segs:
        stem = s.rsplit(".", 1)[0] if "." in s else s
        if stem in _AD_SEGMENTS:
            return f"疑似广告位(路径段 '{stem}')"
    stem = segs[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    if stem in _AD_STEMS:
        return f"疑似站点装饰/占位图(文件名 '{stem}')"
    m = _AD_DIM_RE.match(stem)
    if m and int(m.group(1)) <= 2 and int(m.group(2)) <= 2:
        return f"疑似跟踪像素({stem})"
    return None


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
        # 图片专属体积下限: 只作用于 image 类型, 用于过滤 1x1 跟踪像素 / 广告
        # 占位图。默认不配置, 用户显式开启才有(避免误伤合法的极小图标)。
        self.min_image_bytes = parse_size(o.get("min_image_bytes"))
        # 广告位/站点装饰排除。默认开: 通用采集器抓整页时, 站点 logo / 横幅 /
        # sprite 会混进资源列表, 它们"看起来是图片"但用户根本不想要。
        self.exclude_ad = _truthy(o.get("exclude_ad"), default=True)
        # 尺寸下限(仅图片): 横幅 / 按钮 / 信标的最强判别特征。默认关 ——
        # 相册站里总有竖构图小图, 阈值该由用户按目标站点定, 不能替他们拍板。
        self.min_width = parse_size(o.get("min_width")) if o.get("min_width") else None
        self.min_height = parse_size(o.get("min_height")) if o.get("min_height") else None
        self.min_pixels = parse_size(o.get("min_pixels")) if o.get("min_pixels") else None
        # 感知去重(dHash): 把"人眼看着是同一张、但字节不同"的图标出来
        # (换尺寸 / 重新压缩 / 重复收录)。默认**开**, 因为:
        #   ① 它只标记、绝不删文件(见 core/phash.py), 误判的代价只是多看一眼;
        #   ② 图集站上这类重复本来就是最常见的形态, 而 sha256 一个都认不出;
        #   ③ 开销是每张图一次 ffmpeg 缩放解码(毫秒级), 相对一次网络下载可忽略。
        # 觉得慢或不需要就传 dedup_perceptual=false。
        self.dedup_perceptual = _truthy(o.get("dedup_perceptual"), default=True)
        # 阈值单位是"位", 满值 64。**别把它调大来"多抓几个"** —— 调大后
        # 同一场景的连拍会互相标记, 用户会以为功能不准, 于是整个标记都不看了。
        try:
            self.dedup_threshold = int(o.get("dedup_threshold"))
        except (TypeError, ValueError):
            from core.phash import DEFAULT_THRESHOLD

            self.dedup_threshold = DEFAULT_THRESHOLD
        # 只有配置了大小区间才需要 HEAD 探测, 避免额外的网络开销
        self.need_size = (
            self.min_size is not None
            or self.max_size is not None
            or self.min_image_bytes is not None
        )

    @property
    def need_dimensions(self):
        """是否需要图片尺寸终检(在**下载后**量宽高)。"""
        return (
            self.min_width is not None
            or self.min_height is not None
            or self.min_pixels is not None
        )

    @property
    def active(self):
        return bool(
            self.types
            or self.allow_exts
            or self.deny_exts
            or self.keywords
            or self.exclude_keywords
            or self.need_size
            or self.exclude_ad
            or self.need_dimensions
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

    def match_dimensions(self, width, height):
        """尺寸校验(仅图片)。通过返回 None, 否则返回原因。

        拿不到尺寸(width/height 为 None)一律放行 —— 与 size 层同一个原则:
        判不出来的时候，宁可多留一张也不误杀一张。
        """
        if not self.need_dimensions:
            return None
        try:
            w = int(width) if width is not None else None
            h = int(height) if height is not None else None
        except (TypeError, ValueError):
            return None
        if w is None or h is None or w <= 0 or h <= 0:
            return None

        if self.min_width is not None and w < self.min_width:
            return f"疑似广告/图标(宽 {w}px < 下限 {self.min_width}px)"
        if self.min_height is not None and h < self.min_height:
            return f"疑似广告/图标(高 {h}px < 下限 {self.min_height}px)"
        if self.min_pixels is not None and w * h < self.min_pixels:
            return (
                f"疑似小图/横幅({w}x{h} = {w * h} 像素 < 下限 {self.min_pixels})"
            )
        return None

    def match_url(self, rtype, url):
        """URL/类型层面校验。通过返回 None, 否则返回原因。"""
        low = (url or "").lower()
        if self.types and (rtype or "").lower() not in self.types:
            return f"类型 {rtype} 不在白名单"

        for kw in self.exclude_keywords:
            if kw.lower() in low:
                return f"命中排除关键词 '{kw}'"

        if self.exclude_ad:
            reason = _match_ad(url)
            if reason:
                return reason

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

    def match_resource(self, rtype, url, size=None):
        """综合有效性校验: URL/类型 + 大小 + 图片体积下限。

        这就是"什么是有效资源"的统一定义 —— 下游两个接入点(提取阶段按 URL 预筛、
        下载前按真实体积复核)都走它, 避免定义散落两处导致行为不一致。
        size 为 None 时只做 URL/类型校验(提取阶段还不知道体积)。
        """
        # size 经 JSON/DB 往返可能是字符串(如 "1047527424"); 统一转成 int 再比,
        # 否则 str 与 int 比较会抛 TypeError, 把整个任务在提取阶段拖垮(资源卡 pending)。
        size = _coerce_size(size)
        reason = self.match_url(rtype, url)
        if reason:
            return reason
        if size is not None:
            reason = self.match_size(size)
            if reason:
                return reason
        # 图片专属下限: 1x1 跟踪像素 / 广告占位图体积极小(43~200B),
        # 真实缩略图通常 > 1KB。开启 min_image_bytes 后才生效。
        if (
            self.min_image_bytes is not None
            and (rtype or "").lower() == "image"
            and size is not None
            and size < self.min_image_bytes
        ):
            return f"疑似广告/占位图(体积过小 {fmt_size(size)} < {fmt_size(self.min_image_bytes)})"
        return None

    def summarize(self):
        """给日志用的一行摘要。"""
        if not self.active:
            return "无过滤条件"
        parts = []
        if self.exclude_ad:
            parts.append("排除广告位/装饰图")
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
        if self.need_dimensions:
            # 尺寸是"下载后终检", 说明清楚: 用户看到 filtered 时有的大小已经花掉了
            bits = []
            if self.min_width is not None:
                bits.append(f"宽≥{self.min_width}px")
            if self.min_height is not None:
                bits.append(f"高≥{self.min_height}px")
            if self.min_pixels is not None:
                bits.append(f"≥{self.min_pixels}像素")
            parts.append("尺寸(" + ",".join(bits) + ", 下载后终检)")
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
