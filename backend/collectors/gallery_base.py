"""序号枚举型图集采集器的公共基类。

适用场景
========
「一个相册/图集 ID 之下, 有 N 个按序号命名的资源」:

    https://img.example.com/photos/{gid}/00001.jpg
    https://img.example.com/photos/{gid}/00001_600x0.webp
    https://img.example.com/photos/{gid}/00001.mp4      <- 同一站点的视频相册

站点差异全部用声明式的 `GallerySite` 表达。接入新站只需要写一份配置,
不必复制本模块的枚举 / 存在性判定 / 限速 / 镜像构造逻辑。

接入前必须实测的两个未知量
==========================
1. **存在性怎么判定** —— 不是所有站点都返回 404。实测过的坑: img.xchina.io 对越界
   序号同样返回 200, 只是 Content-Type 变成 text/html。因此判定只走 `probe()`
   返回的三态, 且严格区分"不存在"与"探测失败"(后者重试, 绝不当作不存在,
   否则图集会从中间被截断)。
2. **是否需要特殊请求头** —— 部分 WAF 会校验 Accept, 见 `core.config.IMAGE_ACCEPT`。

媒体类型(图片 / 视频)
=====================
同一个 gid 下可能**同时**有图片和视频, 而且序号各自独立(实测 xchina 的
`6a3654854fd25` 是 12 张图 + 4 段 mp4, 图片 00001.jpg 与视频 00001.mp4 同名不同后缀)。
所以媒体类型是可枚举的多个声明, 由 `options.media` 决定实际采哪些:

    auto   相册里有什么采什么(默认) —— 有视频就图片+视频
    image  只采图片
    video  只采视频
    both   图片和视频都采

⚠️ 不要假设"有视频的相册就没有图片": 该站的视频相册里同样有整套图片。
反之也不要假设"视频序号与图片序号一一对应"。

画质档
======
`variants` 按画质**从高到低**排列。用户可按 `quality` 选择主 URL 用哪一档,
其余档位自动成为 mirrors(故障备用)。若某张图缺所选档位, 会自动回退到最高画质
档而不是直接判为不存在 —— 这是"不漏图"的关键。

输出命名
========
默认 `相册名/00001.jpg`, 相册名优先取相册页 `<title>`(见 `collectors.album_meta`),
拿不到时回退图集 ID。文件名是**建议**, 最终由 `core.naming` 统一清洗。
"""

import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

from core.config import IMAGE_ACCEPT, VIDEO_ACCEPT, settings
from core.filters import fmt_size
from core.naming import clean_segment, safe_relative
from collectors.scores import SCORE_ALBUM_PAGE, SCORE_BARE_ID, SCORE_RESOURCE_URL

PROBE_OK = "ok"            # 资源存在
PROBE_MISSING = "missing"  # 序号越界(服务器仍未返回 404, 但内容不是媒体)
PROBE_ERROR = "error"      # 网络/5xx, 需重试, 不可与"不存在"混同

QUALITY_KEYS = ("original", "1200", "800", "600")
DEFAULT_QUALITY = "original"

# 采哪些媒体; auto = 相册里有什么采什么
MEDIA_KEYS = ("auto", "image", "video", "both")
DEFAULT_MEDIA = "auto"

# 输出目录名的取值方式
#   clean  <title> 去掉站点尾巴(默认)
#   full   完整 <title>
#   h1    页面 <h1>(信息往往比 <title> 更全, 如带 "(FENDSON)")
#   id     直接图集 ID —— 并且**完全不开浏览器**
ALBUM_TITLE_MODES = ("clean", "full", "h1", "id")
DEFAULT_ALBUM_TITLE = "clean"
# 说明: "id" 不只是"不用标题", 而是**完全不开浏览器**(见 crawl)。
# 目录名前加一层标签(如 "丝袜-情趣内衣/相册名/...")由 options.album_tags_dir 控制。

DEFAULT_START = 1
DEFAULT_MAX = 1000         # 硬上限, 防止判定失效时无限枚举
DEFAULT_MISS_STOP = 3      # 连续多少次判定为不存在就停止

_ID_CHARS = re.compile(r"[0-9A-Za-z_-]{6,}")
_THUMB_NAME = re.compile(r"\d{3,}_[0-9x]+")


def _truthy(value):
    """把 options 里五花八门的"真"统一成 bool。

    任务 options 来自 JSON / 前端表单, 可能是 True、"true"、"1"、1。
    只认这些, 其余(含 "false"/"0"/None)一律 False。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _pick(value, allowed, default, name, log=None):
    """校验枚举型 option: 非法值回退默认并留日志(不抛错, 采集不该因此中断)。"""
    v = str(value if value is not None else default).strip().lower()
    if v in allowed:
        return v
    if log:
        log(f"{name}={value!r} 非法, 回退 {default}; 可选 {', '.join(allowed)}")
    return default


def _human_bytes(n):
    """字节数 -> 人类可读; 拿不到(0/None)时返回空串。"""
    return fmt_size(n) if n else ""


@dataclass
class MediaType:
    """一种媒体类型(图片 / 视频)的声明。

    只描述"这种媒体长什么样、怎么判定存在、要什么请求头", 与具体站点无关。
    """

    name: str                        # image / video, 同时是任务的资源 type
    variants: list                   # 变体后缀, 按画质从高到低
    quality_map: dict = field(default_factory=dict)
    ctype_prefix: str = "image/"     # 存在性判定: Content-Type 必须以它开头
    accept: str = IMAGE_ACCEPT
    seq_format: str = "{seq:05d}"
    base: str = ""                   # 资源根; 留空则用 GallerySite.base
    default_ext: str = ".jpg"        # URL 没扩展名时的兜底(仅用于命名)

    def root(self, site):
        return self.base or site.base

    def url_for(self, site, gid, seq, variant=None):
        suffix = variant if variant is not None else self.variants[0]
        return f"{self.root(site)}/{gid}/{self.seq_format.format(seq=seq)}{suffix}"

    def variant_of(self, quality):
        """画质档 -> 变体后缀; 未知档位回退最高画质。"""
        return self.quality_map.get(quality or DEFAULT_QUALITY, self.variants[0])

    def mirror_order(self, chosen):
        """其余变体的尝试顺序: 按与所选档位的画质接近度排列。"""
        try:
            k = self.variants.index(chosen)
        except ValueError:
            k = 0
        return sorted(
            (i for i in range(len(self.variants)) if i != k),
            key=lambda i: (abs(i - k), i),
        )


@dataclass
class GallerySite:
    """一个「序号枚举型」图集站点的声明式描述。"""

    name: str
    base: str                       # URL 前缀, 如 https://img.xchina.io/photos
    variants: list                  # 主媒体(图片)的变体后缀, 按画质从高到低
    quality_map: dict = field(default_factory=dict)  # 画质档 -> 后缀
    seq_format: str = "{seq:05d}"
    # 视频媒体: 非空即声明"该站同一 gid 下还有视频"。留空则该站只采图片。
    video_variants: list = field(default_factory=list)
    video_quality_map: dict = field(default_factory=dict)
    video_seq_format: str = "{seq:05d}"
    video_base: str = ""            # 视频与图片不同 CDN 时填这里
    # 依次尝试的 ID 提取正则(第 1 组即 ID), **顺序即优先级**。
    # 一条 URL 可能有多种形态(资源直链 / 相册页 / 老式 query), 正则要写全。
    id_patterns: list = field(default_factory=list)
    id_in_path: Optional[str] = None  # 兼容旧写法: 等价于 id_patterns 里的一条
    id_in_query: str = "id"
    # 去掉扩展名后的末段若匹配此规则, 视为**页码**而非 ID。
    # 例如 /photo/id-6aa5136f606fe/10.html 的末段是 "10"。
    # 拿不到靠谱的 ID 时必须返回 None(让上层报错), 绝不能猜 ——
    # 猜错会去枚举一个不存在的图集, 结果是"任务成功但 0 个资源"。
    page_tail: Optional[str] = r"^\d+$"
    # 该站接受的输入形态(仅用于报错提示, 不影响解析逻辑)
    input_forms: list = field(default_factory=list)
    # 相册页 URL 模板(含 {gid})。给了才能取到 <title> 作目录名、并判断有无视频。
    # 取不到页面只是退化成"用图集 ID 命名", 不影响采集本身。
    album_url_template: str = ""
    # <title> 里站点尾巴的分隔规则: 取第一段, 如
    # "某套图 - 国模套图 - 小黄书 xChina" -> "某套图"
    title_split: str = r"\s+-\s+"

    def media(self, name="image"):
        """按名字取媒体声明; 站点未声明该媒体时返回 None。"""
        if name == "video":
            if not self.video_variants:
                return None
            qmap = dict(self.video_quality_map) or {"original": self.video_variants[0]}
            return MediaType(
                "video", list(self.video_variants), qmap, "video/",
                VIDEO_ACCEPT, self.video_seq_format, self.video_base,
                _ext_tail(self.video_variants[0], ".mp4"),
            )
        return MediaType(
            "image", list(self.variants), dict(self.quality_map), "image/",
            IMAGE_ACCEPT, self.seq_format, "", _ext_tail(self.variants[0], ".jpg"),
        )

    def media_names(self):
        """本站可采的媒体类型, 顺序即枚举顺序。"""
        return [n for n in ("image", "video") if self.media(n)]

    def album_url(self, gid):
        return self.album_url_template.format(gid=gid) if self.album_url_template else ""

    # ---- 兼容旧调用: 默认都作用在图片媒体上 ----

    def url_for(self, gid, seq, variant=None, media="image"):
        m = self.media(media) or self.media("image")
        return m.url_for(self, gid, seq, variant)

    def variant_of(self, quality, media="image"):
        return (self.media(media) or self.media("image")).variant_of(quality)

    def mirror_order(self, chosen, media="image"):
        return (self.media(media) or self.media("image")).mirror_order(chosen)


def _ext_tail(variant, default):
    """从变体后缀里取出扩展名(含点), 取不到就用默认值。"""
    tail = variant.lstrip(".").rsplit(".", 1)
    return "." + tail[-1].lower() if len(tail) > 1 else default


def parse_gid(site, raw, strict=False):
    """从「图集 ID / 资源直链 / 相册页 URL」中提取图集 ID。

    支持三种输入::

        6aa113208a506
        https://img.xchina.io/photos/6aa113208a506/00001.jpg
        https://xchina.co/photo/id-6aa113208a506.html         <- 相册页
        https://xchina.co/photo/id-6aa113208a506/10.html      <- 相册页(可带页码)
        https://xchina.co/photoShow.html?id=6aa113208a506

    ⚠️ 解析不出来时**必须返回 None**, 绝不能"退而求其次"猜一个。
    真实事故: `/photo/id-XXXX/10.html` 只被旧正则认了 `/photos/`, 于是退路取到
    路径末段 `10` 当作图集 ID, 后续去枚举 `photos/10/00001.jpg`。该站对不存在的
    图集仍返回 200 + text/html, 于是被判定为"序号不存在", 连续 3 次后停止,
    最终**任务报 success 但 0 个资源** —— 用户完全看不出哪里错了。

    `strict=True` 时**跳过下面的末段退路**, 只认正则配出来的 ID。它为自动
    识别服务: `/tag/some-tag` 这种列表页的末段同样过得了 ID 字符集校验,
    于是会被当成图集 ID 认领, 后果是去枚举一个不存在的图集 —— 又是一次
    "成功但 0 资源"。真实采集保留退路(用户已手选采集器, 输入形态各异);
    **替用户做决定的场合必须用 strict**, 那里猜错的代价是静默的。
    """
    s = (raw or "").strip()
    if not s:
        return None
    # 纯 ID: 既没有协议也没有路径分隔符, 且字符集合法
    if "://" not in s and "/" not in s:
        return s if _ID_CHARS.fullmatch(s) else None

    patterns = list(site.id_patterns or [])
    if site.id_in_path:
        patterns.append(site.id_in_path)
    for pat in patterns:
        m = re.search(pat, s)
        if m and m.group(1):
            return m.group(1)

    m = re.search(rf"[?&]{re.escape(site.id_in_query)}=([0-9A-Za-z_-]{{6,}})", s)
    if m:
        return m.group(1)
    if strict:
        return None

    # 退路: 取路径最后一段, 去掉扩展名后再判断它到底像不像一个 ID
    seg = urlparse(s).path.rstrip("/").rsplit("/", 1)[-1]
    stem = seg.rsplit(".", 1)[0] if "." in seg else seg
    # 形如 00046_600x0 的缩略图名不是图集 ID, 放弃
    if _THUMB_NAME.fullmatch(stem):
        return None
    # 形如 10.html -> "10" 的**页码**不是图集 ID。宁可让上层报错,
    # 也不能拿它去枚举一个猜出来的图集。
    if site.page_tail and re.fullmatch(site.page_tail, stem):
        return None
    # 退路只在"看起来确实像个 ID"时才算数: /photo/ 这种普通路径词长度/字符集
    # 都不达标, 就该返回 None, 由采集器抛出可读的错误。
    return stem if _ID_CHARS.fullmatch(stem) else None


def _session(proxy=None):
    s = requests.Session()
    p = proxy if proxy is not None else settings.proxy
    if p:
        s.proxies.update({"http": p, "https": p})
    return s


def _head_status_headers(session, url, headers, timeout):
    """取响应状态码与响应头, 优先 HEAD(不传正文)。

    ⚠️ HEAD 不是所有站点都老实: 少数配置(见过 Cloudflare 后面的图床)对 HEAD
    返回 200 却**不带 Content-Type**, 甚至只回一行状态行。若直接按"没有 image/
    前缀"判为不存在, 整个图集会被误判成空。此时退回**流式 GET** —— 只读响应头
    就断开, 正文一个字节都不读。

    ⚠️ 2026-09-18 复测修正: 目标站点(img.xchina.io)的 HEAD **是可靠的**, 存在
    与越界分别返回 `200 image/jpeg|video/mp4`(带 Content-Length) 与
    `200 text/html`, 无需回退。此前记的"head 无任何响应头"是 **curl 经系统代理
    时 `-I` 只回一行 `200 Connection Established`** 的假象, 别据此去优化。
    这段回退只为"真的不返回 Content-Type 的站点"保留。
    """
    resp = session.head(url, headers=headers, allow_redirects=True, timeout=timeout)
    if (resp.headers.get("Content-Type") or "").strip() or resp.status_code != 200:
        return resp.status_code, resp.headers
    with session.get(
        url, headers=headers, allow_redirects=True, timeout=timeout, stream=True
    ) as raw:
        return raw.status_code, raw.headers


def probe(session, url, timeout=None, retries=2, ctype_prefix="image/", accept=None):
    """探测单个资源是否存在, 返回 (state, size, content_type)。

    state 取 PROBE_OK / PROBE_MISSING / PROBE_ERROR。
    网络异常与 5xx 归为 ERROR(可重试), 与"序号不存在"严格区分 ——
    把 ERROR 当成 MISSING 会导致图集从中间被截断。

    `ctype_prefix` 决定"什么才算这种媒体存在": 图片看 `image/`, 视频看 `video/`。
    越界时该站返回 text/html, 两种前缀都会把它判成 missing。
    """
    headers = {"User-Agent": settings.user_agent, "Accept": accept or IMAGE_ACCEPT}
    timeout = timeout or settings.request_timeout
    last = (PROBE_ERROR, None, None)
    for _ in range(max(1, retries)):
        try:
            status, hdrs = _head_status_headers(session, url, headers, timeout)
        except Exception:
            last = (PROBE_ERROR, None, None)
            continue

        ctype = (hdrs.get("Content-Type") or "").lower()
        if status >= 500:
            last = (PROBE_ERROR, None, ctype)
            continue
        if status == 200 and ctype.startswith(ctype_prefix):
            raw_len = hdrs.get("Content-Length")
            try:
                size = int(raw_len) if raw_len else None
            except ValueError:
                size = None
            return (PROBE_OK, size, ctype)
        # 200 但不是目标媒体(越界时返回 html), 或 4xx: 一律视为不存在
        return (PROBE_MISSING, None, ctype)
    return last


def _pause(min_s, max_s):
    """按区间随机停顿; 未指定时用探测专用间隔(比下载节奏快得多)。"""
    lo = min_s if min_s is not None else settings.probe_interval
    hi = max_s if max_s is not None else None
    if hi and hi > lo:
        time.sleep(random.uniform(lo, hi))
    elif lo:
        time.sleep(lo)


def _ext_of_url(url, default=".jpg"):
    """取 URL 末段扩展名(含点, 小写); URL 没有扩展名时用默认值。"""
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return ("." + tail.rsplit(".", 1)[1].lower()) if "." in tail else default


def _quality_tag(mtype, variant):
    """文件名里的画质标记: 最高画质档留空, 其余返回形如 "_1200" 的标记。

    ⚠️ 这里的入参是**变体后缀**(如 ".jpg" / "_1200x0.webp"), 不是画质档名。
    曾经误写成 `"" if use in ("original", None)` —— 拿变体后缀去和画质档名比
    永不相等, 于是原图也被贴上标记, 落盘成 `00001_.jpg.jpg` 这种双后缀。
    """
    if variant == mtype.variants[0]:
        return ""
    for key, suffix in (mtype.quality_map or {}).items():
        if suffix == variant and key != "original":
            return f"_{key}"
    # 站点没在 quality_map 里登记这一档: 退化用后缀本身清洗出一个标记
    return "_" + variant.strip("._").replace(".", "_")


def discover(site, gid, quality=None, start=DEFAULT_START, max_count=DEFAULT_MAX,
             miss_stop=DEFAULT_MISS_STOP, session=None, proxy=None,
             min_interval=None, max_interval=None, log=None,
             media="image", album=None):
    """枚举图集资源, 逐个 yield 结果字典。

    yield::

        {"seq": 1, "type": "image", "url": "...00001.jpg",
         "mirrors": [...], "size": 656713, "filename": "相册名/00001.jpg"}

    停止条件(任一): 连续 miss_stop 次判定为不存在 / 达到 max_count 硬上限。

    quality 决定主 URL 取哪一档变体; 若该档位在这张图上缺失, 退回最高画质档,
    避免把"缺一个尺寸"误判成"这张图不存在"。

    media  决定枚举哪种媒体(见 MEDIA_KEYS); 视频没有尺寸档位, 只有一条主 URL。
    album  输出目录名(相册标题); **可含 `/` 分层**(如 "标签/相册名")。
           留空回退 gid, 保证文件名始终带一层分组目录。
    """
    mtype = site.media(media)
    if mtype is None:
        raise ValueError(f"站点 {site.name} 未声明 {media} 资源, 无法枚举")
    # max_count=None 表示"用默认硬上限"; 调用方(预览)会用小的值限时
    limit = int(max_count or DEFAULT_MAX)
    gid = (gid or "").strip()
    # album 允许带 `/` 分层(如 "标签/相册名"): 逐段清洗但**保留层级**。
    # 含 `..` 或绝对路径时 safe_relative 返回 None, 退回"整串当一段清洗",
    # 因此无论相册标题里出现什么字符都逃不出任务目录。
    group = safe_relative(album) or clean_segment(album) or gid
    default_variant = mtype.variant_of(quality)
    top_variant = mtype.variants[0]
    sess = session or _session(proxy)
    own = session is None

    try:
        miss_run = 0
        seq = start
        while seq < start + limit:
            use = default_variant
            main = mtype.url_for(site, gid, seq, use)
            state, size, ctype = probe(sess, main, ctype_prefix=mtype.ctype_prefix,
                                       accept=mtype.accept)

            if state == PROBE_MISSING and use != top_variant:
                # 该档位不存在 != 这张图不存在
                fallback = mtype.url_for(site, gid, seq, top_variant)
                state, size, ctype = probe(sess, fallback,
                                           ctype_prefix=mtype.ctype_prefix,
                                           accept=mtype.accept)
                if state == PROBE_OK:
                    use, main = top_variant, fallback
                    if log:
                        log(f"[{mtype.name}] seq {seq:05d} 缺 {quality} 档, 回退最高画质")

            if state == PROBE_OK:
                miss_run = 0
                others = [mtype.variants[i] for i in mtype.mirror_order(use)]
                # filename 是"建议"而非命令: task_manager 会过一遍安全清洗,
                # 用户显式指定 name_template 时也会被覆盖。之所以在这里按
                # 相册分子目录, 是因为**采集器知道 gid/相册名而下载层不知道**。
                tag = _quality_tag(mtype, use)
                yield {
                    "seq": seq,
                    "type": mtype.name,
                    "url": main,
                    "mirrors": [mtype.url_for(site, gid, seq, v) for v in others],
                    "size": size,
                    "filename": f"{group}/{seq:05d}{tag}"
                                f"{_ext_of_url(main, mtype.default_ext)}",
                }
            elif state == PROBE_MISSING:
                miss_run += 1
                if log:
                    log(f"[{mtype.name}] seq {seq:05d} 不存在({ctype or 'no content-type'}), "
                        f"连续缺失 {miss_run}/{miss_stop}")
                if miss_run >= miss_stop:
                    break
            else:  # ERROR: 重试后仍失败, 不计入连续缺失(避免误截断), 但防止死循环
                if log:
                    log(f"[{mtype.name}] seq {seq:05d} 探测失败, 跳过")
                miss_run += 1
                if miss_run >= miss_stop * 2:
                    break

            seq += 1
            if seq < start + limit:
                _pause(min_interval, max_interval)
    finally:
        if own:
            sess.close()


def _video_at_first_seq(site, gid, session=None):
    """该图集是否存在 `00001.mp4`。

    用于 media=auto 的判定: 该站视频序号从 1 开始, 所以"第一段在不在"就等价于
    "这个相册有没有视频"。只花一次 HEAD+GET, 比让用户手选媒体类型可靠得多。
    探测失败(网络异常)按"没有"处理 —— 宁可少采也不要把任务搞挂。
    """
    mtype = site.media("video")
    if mtype is None:
        return False
    sess = session or _session()
    own = session is None
    try:
        state, _, _ = probe(sess, mtype.url_for(site, gid, DEFAULT_START),
                            ctype_prefix=mtype.ctype_prefix, accept=mtype.accept,
                            retries=1)
        return state == PROBE_OK
    except Exception:
        return False
    finally:
        if own:
            sess.close()


def _netloc_path(url):
    p = urlparse(url)
    return p.netloc.lower(), p.path.rstrip("/")


def _match_score(site, raw):
    """站点声明 -> 对某个输入的认领分数(None = 不认领), 详见 `match_score`。"""
    if site is None:
        return None
    s = (raw or "").strip()
    if not s:
        return None

    # 纯 ID: 既没协议也没路径。这份功劳谁都能领, 所以分数最低。
    if "://" not in s:
        if "/" in s:
            return None
        return SCORE_BARE_ID if _ID_CHARS.fullmatch(s) else None

    u = urlparse(s)
    if u.scheme not in ("http", "https"):
        return None
    # 先坐实这是本图集站的东西: 只认正则配出来的 ID, 不能用末段退路 ——
    # 退路会把 `/tag/some-tag` 这种列表页也认领下来(详见 parse_gid)。
    if not parse_gid(site, s, strict=True):
        return None

    host, path = u.netloc.lower(), u.path
    b_host, b_path = _netloc_path(site.base)
    if host == b_host and (path + "/").startswith(b_path + "/"):
        return SCORE_RESOURCE_URL
    if site.album_url_template:
        p_host, _ = _netloc_path(site.album_url_template.format(gid="x"))
        if host == p_host:
            return SCORE_ALBUM_PAGE
    return None


class SequenceGallerySpider:
    """序号枚举型采集器基类: 子类只需声明 `site`。"""

    site: GallerySite = None

    @classmethod
    def match_score(cls, url):
        """该站点采集器对这个输入的自信程度; None = 处理不了。

        ⚠️ **必须先 parse_gid 成功才认领**。只凭域名认领会把同域的其它路径
        (如 `img.xchina.io/gallery/abc`)也揽下来, 然后 `parse_gid` 的退路
        取到 `abc` 去枚举一个不存在的图集 —— 那条退路本来只服务于"已知是
        资源/相册页"的情形。宁可让 URL 落给通用采集器, 也不接站不住的输入。
        """
        return _match_score(cls.site, url)

    def resolve_media(self, site, gid, media_opt, meta=None, log=None):
        """把 options.media 解析成实际要枚举的媒体列表。

        auto 的判定顺序(先看相册页, 再看一次探测): 相册页里的视频清单是站点
        自己给的线索, 但**不作为权威资源列表** —— 页面结构会变, 序号枚举才是
        稳定路径。两者都说没有才认为没有视频, 避免静默漏采。
        """
        names = site.media_names()
        if media_opt == "image":
            return ["image"]
        if media_opt == "video":
            if "video" not in names:
                raise ValueError(f"站点 {site.name} 未声明视频资源, 无法用 media=video 采集")
            return ["video"]
        if media_opt == "both":
            return names
        if "video" not in names:
            return ["image"]

        if meta and meta.get("videos"):
            if log:
                log(f"相册页显示含 {len(meta['videos'])} 段视频, 图片+视频一起采")
            return ["image", "video"]
        if _video_at_first_seq(site, gid):
            if log:
                log("探测到 00001.mp4, 判定该相册含视频, 图片+视频一起采")
            return ["image", "video"]
        return ["image"]

    # ---- 相册页与命名 ----

    def album_page_url(self, site, gid, title_mode):
        """本次需要读的相册页 URL; 空串表示不需要开浏览器。"""
        return site.album_url(gid) if title_mode != "id" else ""

    def read_album_meta(self, site, gid, title_mode, log=None, use_cache=True):
        """读相册页元信息(带 TTL 缓存)。

        `album_title=id` 是用户"不要浏览器"的明确表态, 此时**整个跳过**相册页:
        选它的人往往正是因为 Cloudflare/浏览器慢或不可用。
        """
        page_url = self.album_page_url(site, gid, title_mode)
        if not page_url:
            return None
        return fetch_album_meta(page_url, site=site, log=log, gid=gid,
                                use_cache=use_cache)

    def album_name(self, meta, title_mode, gid):
        """按 album_title 取目录名; 任何一环取不到都回退图集 ID。"""
        if not meta or title_mode == "id":
            return gid
        if title_mode == "full":
            return meta.get("title") or gid
        if title_mode == "h1":
            # h1 往往比 <title> 更全(带厂牌括注), 但页面可能没有 -> 依次退让
            return meta.get("h1") or meta.get("album") or gid
        return meta.get("album") or gid

    def group_name(self, meta, album, opts, gid=""):
        """输出分组目录: 可选在相册名外再套一层标签目录。

        如 `丝袜-情趣内衣/相册名/00001.jpg`。只取前 3 个标签 —— 再多会把路径
        撑得过长, 而信息量递减。
        """
        if not _truthy(opts.get("album_tags_dir")):
            return self.normalize_group(album, gid)
        tags = [clean_segment(t) for t in ((meta or {}).get("tags") or [])]
        tags = [t for t in tags if t][:3]
        group = f"{'-'.join(tags)}/{album}" if tags else album
        return self.normalize_group(group, gid)

    @staticmethod
    def normalize_group(group, gid=""):
        """把分组目录名规范化成 `discover()` 真正会用的形态。

        预览显示的文件名示例必须与真实落盘的路径一致 —— 否则用户照预览去别处
        找文件会找不到。所以规范化只做一次, 两边共用。
        """
        return safe_relative(group) or clean_segment(group) or gid

    def plan_log(self, log, gid, group, medias, meta):
        """把"这次采什么"一次说清: 目录、媒体、相册页自报的数量与视频体积。"""
        if not log:
            return
        if not meta:
            log(f"图集 {gid} -> 目录 {group!r}; 采集媒体: {', '.join(medias)}"
                "; 未取到相册标题, 用图集 ID 命名")
            return
        bits = []
        if meta.get("photos") is not None:
            bits.append(f"{meta['photos']} 张图")
        if meta.get("videos_declared") is not None:
            bits.append(f"{meta['videos_declared']} 段视频")
        extra = f"; 相册页自报 {' + '.join(bits)}" if bits else ""
        log(f"图集 {gid} -> 目录 {group!r}; 采集媒体: {', '.join(medias)}{extra}")
        total = sum(v.get("size") or 0 for v in (meta.get("videos") or []))
        if total:
            hint = ""
            if "video" in medias:
                hint = "(零请求; 想只要图片可设 media=image, 想卡体积可设 max_size)"
            else:
                hint = "(零请求; 本次已不含视频, 这部分不会被下载)"
            log(f"相册页给出视频体积合计 {_human_bytes(total)}{hint}")

    # ---- 采集 ----

    def crawl(self, url, options=None, log=None, max_count=None):
        """枚举全部资源(**不下载**)。

        max_count: 本次最多枚举几个序号; 供"预览"这类需要限时的调用方使用。
        """
        site = self.site
        if site is None:
            raise RuntimeError(f"{type(self).__name__} 未声明 site")
        opts = options or {}
        gid = parse_gid(site, url)
        if not gid:
            forms = getattr(site, "input_forms", None) or ["图集 ID", "资源直链 URL", "相册页 URL"]
            raise ValueError(
                f"无法从 {url!r} 解析出图集 ID。可接受的输入: " + " / ".join(forms)
            )

        quality = opts.get("quality")
        media_opt = _pick(opts.get("media"), MEDIA_KEYS, DEFAULT_MEDIA, "media", log)
        title_mode = _pick(opts.get("album_title"), ALBUM_TITLE_MODES,
                           DEFAULT_ALBUM_TITLE, "album_title", log)

        # 相册页只用于三件事: 目录名、判断有无视频、给出数量与视频体积。
        # 取不到页面不会让任务失败 —— 它只是命名与判定上的优化。
        meta = self.read_album_meta(site, gid, title_mode, log=log)
        album = self.album_name(meta, title_mode, gid)
        group = self.group_name(meta, album, opts, gid)

        medias = self.resolve_media(site, gid, media_opt, meta=meta, log=log)
        self.plan_log(log, gid, group, medias, meta)

        items = []
        for name in medias:
            for it in discover(site, gid, quality=quality, media=name,
                               album=group, log=log, max_count=max_count):
                items.append({
                    "type": it["type"],
                    "url": it["url"],
                    "headers": None,
                    "mirrors": it["mirrors"],
                    # 采集阶段的 probe 已经拿到 Content-Length, 带出去后下载层
                    # 就不必为"大小过滤"再发一次 HEAD(见 task_manager 的体积预检)
                    "size": it["size"],
                    "filename": it.get("filename"),
                    "album": album,
                })
        return items

    # ---- 创建前预览 ----

    def preview(self, url, options=None, log=None, max_items=12):
        """只"看"不采: 这次会采到什么、大概多大。

        优先用相册页的**站点自报数据**(张数 + 每段视频体积) —— 一次页面读取就
        够, 不必枚举任何序号; 相册页拿不到(没装浏览器/Cloudflare 失败)才退回
        "受限枚举", 此时 `sampled=True`, 数量只是下限。

        ⚠️ 自报数量与体积**只用于预告**, 不是资源清单; 真正采集仍走序号枚举。
        """
        site = self.site
        if site is None:
            raise RuntimeError(f"{type(self).__name__} 未声明 site")
        opts = dict(options or {})
        gid = parse_gid(site, url)
        if not gid:
            forms = getattr(site, "input_forms", None) or ["图集 ID", "资源直链 URL", "相册页 URL"]
            raise ValueError(
                f"无法从 {url!r} 解析出图集 ID。可接受的输入: " + " / ".join(forms)
            )

        title_mode = _pick(opts.get("album_title"), ALBUM_TITLE_MODES,
                           DEFAULT_ALBUM_TITLE, "album_title", log)
        media_opt = _pick(opts.get("media"), MEDIA_KEYS, DEFAULT_MEDIA, "media", log)

        meta = self.read_album_meta(site, gid, title_mode, log=log,
                                    use_cache=not _truthy(opts.get("refresh")))
        album = self.album_name(meta, title_mode, gid)
        group = self.group_name(meta, album, opts, gid)
        medias = self.resolve_media(site, gid, media_opt, meta=meta, log=log)

        video_items = list((meta or {}).get("videos") or [])
        photos = (meta or {}).get("photos")
        videos_n = (meta or {}).get("videos_declared")
        if videos_n is None and video_items:
            videos_n = len(video_items)

        # 只报"这次真要采的媒体"。用户选了 media=image 却看到"4 段视频 / 251MiB",
        # 会以为视频也会被下下来 —— 预告必须与创建后的实际行为一致。
        # 相册页自报的总量仍然原样带出(videos_declared), 供用户对照"少采了什么"。
        declared_photos, declared_videos = photos, videos_n
        if "video" not in medias:
            videos_n, video_items = None, []
        if "image" not in medias:
            photos = None

        sampled = False
        sample_files = []
        counts = {}
        # 页面没给出可用数量 -> 退化成受限枚举; 拿不到页面时这是唯一的办法
        if photos is None and not video_items:
            sampled = True
            for name in medias:
                got = list(discover(site, gid, quality=opts.get("quality"),
                                    media=name, album=group, log=log,
                                    max_count=max_items))
                counts[name] = len(got)
                for it in got[:3]:
                    if it.get("filename"):
                        sample_files.append(it["filename"])
            photos = counts.get("image")
            if videos_n is None and "video" in counts:
                videos_n = counts["video"]

        if not sample_files:
            # 没枚举也要让用户先看到"文件会长什么样"(命名是最容易出错的一环)
            for name in medias[:1]:
                mtype = site.media(name)
                if not mtype:
                    continue
                ext = mtype.default_ext
                sample_files = [f"{group}/{i:05d}{ext}" for i in (1, 2)]

        return {
            "collector": site.name,
            "gid": gid,
            "album": album,
            "group": group,
            "album_source": title_mode if meta else "id",
            "media": medias,
            "photos": photos,
            "videos": videos_n,
            # 相册页自报的总量(未按 media 过滤): 用户拿它对照"我少采了什么"
            "photos_declared": declared_photos,
            "videos_declared": declared_videos,
            "video_items": [
                {"name": v["url"].rsplit("/", 1)[-1],
                 "url": v["url"], "size": v.get("size")} for v in video_items
            ],
            "video_bytes": sum(v.get("size") or 0 for v in video_items),
            "tags": (meta or {}).get("tags") or [],
            "maker": (meta or {}).get("maker") or "",
            "h1": (meta or {}).get("h1") or "",
            "page": meta is not None,
            "sampled": sampled,
            "sample_files": sample_files,
        }


def fetch_album_meta(page_url, site=None, log=None, gid=None, use_cache=True):
    """取相册页元信息: 目录名、有无视频、数量、视频体积、标签、厂牌。

    实现见 collectors.album_meta。这里只做一次延迟导入并兜底: 该模块依赖
    Playwright, 采集器本身不依赖(没有浏览器时照样能按序号枚举, 只是拿不到
    这些优化信息)。
    """
    try:
        from .album_meta import fetch_album_meta as _fetch
    except Exception as e:  # pragma: no cover - 正常情况下必然可导入
        if log:
            log(f"相册元信息模块不可用({type(e).__name__}), 用图集 ID 命名")
        return None
    try:
        return _fetch(page_url, split=(site.title_split if site else None),
                      log=log, gid=gid, use_cache=use_cache)
    except Exception as e:
        if log:
            log(f"取相册页失败({type(e).__name__}: {e}), 用图集 ID 命名")
        return None
