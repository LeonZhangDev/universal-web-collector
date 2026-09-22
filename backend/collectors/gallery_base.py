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
from urllib.parse import unquote, urlparse

import requests

from core.config import IMAGE_ACCEPT, VIDEO_ACCEPT, settings
from core.filters import fmt_size
from core import layout
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
# 目录树固定是"下载目录 / 相册文件夹 / 文件"(视频平铺在下载根目录),
# 规则唯一定义在 core/layout.py —— 采集器只提供相册名, 不决定层数。

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

    def url_for(self, site, gid, seq, variant=None, base=None, seq_format=None):
        suffix = variant if variant is not None else self.variants[0]
        root = base or self.root(site)
        fmt = seq_format or self.seq_format
        return f"{root}/{gid}/{fmt.format(seq=seq)}{suffix}"

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
    # CDN 基址候选(可选)。部分站点会按相册把资源分到不同子路径, 如
    # img.xchina.io/photos 与 img.xchina.io/photos2/photos3 并存。留空只试 base;
    # 填上后枚举前会先对 00001 试探, 命中 image/* 的那个就作为本次基址 ——
    # 让采集器自己发现"资源到底在哪", 而不是写死一个 base(否则像
    # 69ad45698f836 这种在 photos2 的相册会整体判空 -> 任务 failed)。
    base_candidates: Optional[list] = None
    # `base_candidates` 的自动展开版: base 后面接 1..N 的数字后缀
    # (photos -> photos2 / photos3 ... / photosN)。数字后缀是这类站最常见的
    # 分桶方式, 手写三个的话下次多出一个 photos4 就整批判空 —— 而"整批判空"
    # 对用户只表现为"任务失败", 看不出是路径变了。显式 candidates 仍可与它并用
    # (用于非数字后缀的情形, 如 img2.example.com)。
    base_candidate_digits: int = 0
    # 数字后缀**覆盖不到**的另一类迁移: 换 host / 换前缀(如 CDN 从
    # img.xchina.io 迁到 cdn2.xchina.io, 或新增 /media 前缀)。这里声明整条
    # 备选基址(可选, 含 {gid} 前的完整前缀), 与数字后缀并列进候选。
    # 留空则只按 base + 数字后缀生成 —— 那种情形下"站点换域名"只能靠用户报障
    # 才会被发现, 所以有已知迁移迹象就填上。
    base_host_templates: list = None
    # 自检样本 URL: 每条都必须能被本站的 id_patterns 解析出**同一个** gid。
    # `check_site()` 用它做启动期断言, 防的是一类很难查的回归 —— 新加的
    # pattern 与旧 pattern 对同一条 URL 各配出一个不同的 ID, 而 `parse_gid`
    # 取的是**首个命中**, 于是后加的那条**静默改变**了已有行为(某个相册突然
    # 采空)。样本形如 [(url, 期望 gid), ...]。
    id_samples: list = None
    # 图集 ID 的**形状**正则(fullmatch)。只在**自动识别**时生效 —— 那时是我们
    # 替用户做决定, 认错会去枚举一个不存在的图集(表现为"成功但 0 个资源"),
    # 代价是静默的, 所以要用最严的判据。用户手选采集器时不校验:
    # 他已经表过态, 而且输入形态可能是我们没见过的。
    # 例: XChina 实测所有 gid 都是 13 位小写十六进制(6aa5136f606fe / 69ad45698f836),
    # 于是 `[0-9a-f]{8,}` 能挡掉 /photos/featured/0001.jpg 这类"路径词被当成 ID"。
    gid_shape: str = ""
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
    # 接受单条正则或正则列表: 站点分页形态不止一种时(纯数字 `10.html`、
    # 带前缀 `page-10.html`、query `?p=10`), 列表化可以只加不改 —— 写成
    # 单条的话换形态那天它悄悄失效, 又退化成"拿页码当 gid"。
    page_tail: object = r"^\d+$"
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

    `strict=True` 时**跳过下面的末段退路**, 只认正则配出来的 ID, 并要求它满足
    `site.gid_shape`(站点声明的 ID 形状)。它为自动识别服务: `/tag/some-tag`
    这种列表页的末段同样过得了 ID 字符集校验, 于是会被当成图集 ID 认领, 后果是
    去枚举一个不存在的图集 —— 又是一次"成功但 0 资源"。真实采集保留退路(用户已手选
    采集器, 输入形态各异); **替用户做决定的场合必须用 strict**, 那里猜错的代价是静默的。

    ⚠️ 形状不符时**不再往下走退路**: 退路比形状判据更不可靠, 真要放行反而更危险。
    """
    s = (raw or "").strip()
    if not s:
        return None
    # percent-encode 过的 ID 直接匹配不上 `_ID_CHARS`, 会白白落进退路(甚至被
    # 判成 None -> "无法识别")。解码一次成本几乎为零, 却能把 `%36aa...` 这类
    # 从别处复制来的链接救回来。真实 ID 是十六进制/短横线, 不会因为多解一次变形。
    if "%" in s:
        s = unquote(s)

    def accept(gid):
        """strict 下再验一次 ID 形状 —— 形状不符说明这大概率不是图集 ID。"""
        if gid and strict and site.gid_shape and not re.fullmatch(site.gid_shape, gid):
            return None
        return gid

    # 纯 ID: 既没有协议也没有路径分隔符, 且字符集合法
    if "://" not in s and "/" not in s:
        return accept(s) if _ID_CHARS.fullmatch(s) else None

    patterns = list(site.id_patterns or [])
    if site.id_in_path:
        patterns.append(site.id_in_path)
    for pat in patterns:
        m = re.search(pat, s)
        if m and m.group(1):
            return accept(m.group(1))

    m = re.search(rf"[?&]{re.escape(site.id_in_query)}=([0-9A-Za-z_-]{{6,}})", s)
    if m:
        return accept(m.group(1))
    if strict:
        return None

    # 退路: 取路径最后一段, 去掉扩展名后再判断它到底像不像一个 ID
    seg = urlparse(s).path.rstrip("/").rsplit("/", 1)[-1]
    stem = seg.rsplit(".", 1)[0] if "." in seg else seg
    # 形如 00046_600x0 的缩略图名不是图集 ID, 放弃
    if _THUMB_NAME.fullmatch(stem):
        return None
    # 形如 10.html -> "10" 的**页码**不是图集 ID。宁可让上层报错,
    # 也不能拿它去枚举一个猜出来的图集。page_tail 支持单条或列表 ——
    # 站点分页形态不止一种时(纯数字/带前缀/query), 加一条即可, 不必改动既有。
    tails = site.page_tail
    if isinstance(tails, str):
        tails = [tails]
    if any(p and re.fullmatch(p, stem) for p in (tails or [])):
        return None
    # 退路只在"看起来确实像个 ID"时才算数: /photo/ 这种普通路径词长度/字符集
    # 都不达标, 就该返回 None, 由采集器抛出可读的错误。
    return stem if _ID_CHARS.fullmatch(stem) else None


def check_site(site):
    """本站点声明的自检: 样本 URL 必须被解析出**唯一且正确**的 gid。

    防的是一类很难查的回归 —— `parse_gid` 取的是**首个命中**的 pattern, 新加
    一条正则时若不慎也能匹配旧 URL, 就会**静默改变**已有行为: 某个相册突然
    采空, 而日志里一切正常。样本把这个"改一行正则"的副作用变成一条可断言的
    事实, 而不是等用户报障。

    返回**问题描述列表**; 空列表 = 通过。调用方决定是抛错还是只记日志
    (默认由 `tests` 断言为空, 运行期只记 warn —— 采集器不该因为一次自检
    失败就拒绝干活, 那时用户更需要的仍然是"先把资源下下来")。
    """
    problems = []
    samples = list(site.id_samples or [])
    for item in samples:
        try:
            raw, want = item
        except (TypeError, ValueError):
            problems.append(f"样本格式应为 (url, 期望gid): {item!r}")
            continue
        got = parse_gid(site, raw, strict=True)
        if got != want:
            problems.append(f"{raw} -> 解析出 {got!r}, 期望 {want!r}")

    # 每条 pattern 单独跑: 若某条单独就能配出与整套不同的结果, 说明存在
    # "谁先谁赢" 的隐式依赖 —— 顺序一变行为就变, 应当修正则而不是靠顺序。
    for raw, want in [s for s in samples if isinstance(s, (list, tuple)) and len(s) == 2]:
        for pat in (site.id_patterns or []):
            m = re.search(pat, raw)
            if m and m.group(1) and m.group(1) != want:
                problems.append(
                    f"pattern {pat!r} 单独匹配 {raw} 得到 {m.group(1)!r}, 与期望 {want!r} 冲突"
                )

    # 形状声明与样本要自洽: 声明了 gid_shape 却没有任何样本能通过, 说明
    # 形状写窄了(自动识别会把真图集也挡在外面)。
    shape = site.gid_shape
    if shape and samples:
        ok = any(
            isinstance(s, (list, tuple)) and len(s) == 2 and re.fullmatch(shape, s[1] or "")
            for s in samples
        )
        if not ok:
            problems.append(f"gid_shape={shape!r} 无法匹配任何样本的期望 gid")

    # 序号格式与候选位数也要检: 这两项写错的表现是"每张都判 MISSING -> 0 资源
    # -> failed", 而用户只看到失败。`seq_format` 少写一位零就是一个字符的事。
    for name in ("image", "video"):
        mtype = None
        try:
            mtype = site.media(name)
        except Exception:
            mtype = None
        if mtype is None:
            continue
        try:
            rendered = mtype.seq_format.format(seq=7)
        except Exception as e:
            problems.append(f"{name}.seq_format={mtype.seq_format!r} 无法渲染: {e}")
            continue
        if "7" not in rendered:
            problems.append(
                f"{name}.seq_format={mtype.seq_format!r} 渲染成 {rendered!r}, 不含序号本身"
            )

    # ⚠️ `base_candidate_digits` 的合法下界是 **0**(= 不展开数字后缀), 不是 1。
    # 字段默认值就是 0, 校验写 <1 会把"我就是不要数字后缀"的站点一律判错 ——
    # 而那个站点用 `base_candidates` / `base_host_templates` 表达候选也完全合理。
    # 负数才是真错误(生成 range(1, -1) 这种无意义区间)。
    digits = getattr(site, "base_candidate_digits", None)
    if digits is not None:
        try:
            if int(digits) < 0:
                problems.append(f"base_candidate_digits={digits!r} 不能为负")
        except (TypeError, ValueError):
            problems.append(f"base_candidate_digits={digits!r} 不是整数")

    # ⚠️ `gid_shape` 必须能被放到**纯 ID 输入**上做 fullmatch —— 那是它最主要的
    # 用武之地。曾经这里出过一个真实缺陷: 正则按"整条 URL"写(带 ^$ 或含 https),
    # 于是纯 ID 输入永远匹配不上, 自动识别时站点被误判为"处理不了", 用户粘一串
    # ID 就落到通用采集器去猜图集(静默 0 资源)。这里拿样本里的纯 ID 形态反查。
    shape = site.gid_shape
    if shape:
        bare = [s[1] for s in samples
                if isinstance(s, (list, tuple)) and len(s) == 2
                and isinstance(s[0], str) and "://" not in s[0] and "/" not in s[0]]
        if bare and not any(re.fullmatch(shape, b or "") for b in bare):
            problems.append(
                f"gid_shape={shape!r} 无法匹配纯 ID 样本 {bare!r}; "
                f"该正则会用 fullmatch 直接作用于纯 ID 输入"
            )
    return problems


def assert_site(site):
    """`check_site` 的抛错版: 配置错误应当**当场**炸掉, 而不是留到采集时。"""
    problems = check_site(site)
    if problems:
        raise ValueError(
            f"站点 {site.name!r} 的声明自检未通过:\n  - " + "\n  - ".join(problems)
        )


def selfcheck_all():
    """对所有已注册的图集站点跑一遍声明自检, 返回 {站点名: [问题, ...]}。

    只返回**有问题**的站点(全绿时返回 {})。放在这里而不是 import 期自动执行:
    自检失败**不能**拦着采集器干活 —— 用户当下更需要"先把资源下下来"。所以它由
    测试(`tests/test_gallery.py`)与 `scripts/selfcheck.py` 显式调用; 运行期若在
    日志里看见 `site self-check failed`, 那是真的配置写错了, 别当噪音忽略。
    """
    try:
        from . import COLLECTORS
    except ImportError:      # 以脚本方式单文件运行时
        return {}
    out = {}
    for name, cls in COLLECTORS.items():
        site = getattr(cls, "site", None)
        if site is None:
            continue
        try:
            problems = check_site(site)
        except Exception as e:                       # 自检本身出错也算问题
            problems = [f"自检抛异常: {type(e).__name__}: {e}"]
        if problems:
            out[getattr(site, "name", name)] = problems
    return out


def shape_warning(site, url):
    """手选采集器时, 对"ID 形状明显不像本站"的输入给一条**警告**(不阻止)。

    与 `strict=True` 的区别在于**谁来承担后果**:

    - 自动识别在替用户做决定, 认错是静默的(去枚举一个不存在的图集, 站点照样
      返回 200 text/html, 于是"成功但 0 资源")。那里必须严格, 不符直接不认领。
    - 手动选择是用户已经表过态。此时我们**没有资格**拒绝 —— 站点可能刚换了 ID
      格式, 而我们知道得比用户晚。但让他知道"这个 ID 形状本站从没见过"是有价值
      的: 一旦真是手滑粘错, 他当场就能改, 不必等任务失败再回头查。

    返回提示字符串; 形状相符、或站点未声明 `gid_shape`、或 URL 里根本没有 ID
    时返回 None(后两种情形"没意见"就是正确的表态)。
    """
    shape = getattr(site, "gid_shape", "")
    if not shape:
        return None
    try:
        gid = parse_gid(site, url, strict=False)
    except Exception:
        return None
    if not gid or re.fullmatch(shape, gid):
        return None
    return (
        f"输入的图集 ID {gid!r} 不符合本站已知的 ID 形状({shape}); "
        f"本站 ID 通常形如 {_shape_sample(site)}。若确认无误可继续, "
        f"但更可能是粘错了链接 —— 形状不符时任务多半会以 0 个资源告终。"
    )


def _shape_sample(site):
    """从 id_samples 里挑一个样本 ID 给用户看, 没有就退回描述形状本身。"""
    for item in (site.id_samples or []):
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
            return repr(item[1])
    return "站点文档中给出的形态"


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


def _sample_points(start, end, n):
    """枚举快路径的抽样点: 首、尾 + 均匀内点。

    ⚠️ **首尾必测**。"数量错一个"和"起点错一位"是最常见的两种不一致, 只抽中间
    点会同时放过这两类错误 —— 而这两类错误都是"整批资源全错", 不是"少几张"。
    """
    if end <= start:
        return [start]
    n = max(2, min(int(n or 2), end - start + 1))
    if n == 2:
        return [start, end]
    step = (end - start) / (n - 1)
    pts = {start, end}
    for i in range(1, n - 1):
        pts.add(int(round(start + step * i)))
    return sorted(pts)


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


def _seq_format_variants(fmt, spread=2):
    """候选序号格式: 先站点默认, 再试**相邻**补零宽度(默认 ±2)。

    同一站点不同相册的补零位数可能不一样(实测 xchina 既有 `00001.jpg` 也有
    `0001.jpg`)。宽度只影响"不足位补几个零" —— 序号变大后 `{seq:04d}` 照样
    渲染出 5 位, 所以多试几种宽度只会多花几次探测, 不会改变既有行为。

    ⚠️ 为什么是 ±2 而不是 ±1: 站点若把默认写成 5 位而实际相册是 3 位, ±1
    恰好漏掉。探测是 HEAD 且无限速器, 多两轮代价可忽略, 覆盖却翻倍。
    """
    m = re.search(r"seq:0(\d+)d", fmt or "")
    if not m:
        return [fmt]
    w = int(m.group(1))
    out = [fmt]
    for delta in range(1, max(0, int(spread)) + 1):
        for alt in (w - delta, w + delta):
            if 1 <= alt <= 9:
                f2 = f"{fmt[:m.start(1)]}{alt}{fmt[m.end(1):]}"
                if f2 not in out:
                    out.append(f2)
    return out


def parse_resource_hint(site, raw, gid=None):
    """从一条**资源直链**里直接读出 CDN 基址与序号宽度; 不是直链返回 None。

    用户手上的直链是本站最可靠的一份证据: 它把"这批资源放在哪个 CDN 子路径"和
    "序号补几位零"都写在 URL 里了 —— 例如 `.../photos2/69ad45698f836/0001.jpg`
    同时说明了基址是 `photos2`、宽度是 4。先采信它可以省掉一整轮探测。

    gid 传入时**只接受指向该图集的直链**。这一点很关键: 相册页里常混着推荐位
    别的相册的图, 而那些相册未必在同一个 CDN 子路径上 —— 采信了它, 枚举就会被
    引到一条错误的路径上, 结果同样是"0 资源 -> failed"。

    ⚠️ 这只是**线索**不是结论: `_resolve_base` 仍会拿它去探一次, 探不通就
    退回候选探测 —— 用户也可能粘了一条失效的旧直链。
    """
    s = (raw or "").strip()
    if "://" not in s:
        return None
    u = urlparse(s)
    if u.scheme not in ("http", "https"):
        return None
    # 去掉 query/fragment 再匹配: 直链可能带 ?v=... 之类的无关参数
    plain = s.split("#", 1)[0].split("?", 1)[0]
    for name in ("image", "video"):
        mtype = site.media(name)
        if mtype is None:
            continue
        # 遍历**全部候选根**而不是只看默认的那个: 候选里可能有完全不同的 host
        # (如 cdn-a.io / cdn-b.io), 只看默认根会让那条直链线索被白白丢掉。
        for root in _base_candidates(site):
            # 根后面允许一段数字(photos -> photos2 / photos3); 序号后面用
            # `[^/]*` 收尾而不是 `\.[0-9A-Za-z]+$`: 直链可能带变体后缀
            # (`00046_600x0.webp`), 只认纯扩展名的话这条最有价值的线索会被丢掉。
            m = re.match(
                rf"^{re.escape(root)}(?P<tail>\d*)/(?P<gid>[0-9A-Za-z_-]{{6,}})"
                rf"/(?P<seq>\d+)[^/]*$",
                plain,
            )
            if not m:
                continue
            if gid and m.group("gid") != gid:
                continue
            return {
                "base": f"{root}{m.group('tail')}",
                "seq_format": "{seq:0%dd}" % len(m.group("seq")),
            }
    return None


def _first_hint(site, gid, candidates):
    """从若干条候选 URL 里取回第一条能解析出线索的(基址 + 序号格式)。

    给"零枚举"的预览用: 那时没有枚举结果可看, 但相册实际用 4 位还是 5 位
    直接决定了预告里那个示例文件名对不对。用户在核对命名时看到位数不符,
    会以为采集器给文件名加错了 —— 明明是他照着错误的预告去核对的。
    """
    for cand in candidates:
        hint = parse_resource_hint(site, cand, gid=gid)
        if hint:
            return hint
    return None


def _base_candidates(site, default=None):
    """该站点可能要试的资源根列表(去重保序, 第一个是默认值)。

    ⚠️ **单一来源**: `_resolve_base`(真的去探)与 `_match_score`(静态判前缀)必须
    用同一份列表。各写一份的话, 新增一个 CDN 子路径时会出现"采集能探到、自动识别
    却不认领"的半通状态 —— 直链被派给 generic, 预览直接报 400, 而采集本身明明好使。
    这种"一半好一半坏"最难查, 所以候选只允许有一个出处。
    """
    d = default if default is not None else site.base
    out = [d]
    for c in (site.base_candidates or []):
        if c and c not in out:
            out.append(c)
    # 换 host 的迁移形态: 站点若把 CDN 从 img.xchina.io 迁到 img2.xchina.io,
    # 「数字后缀」那套生成逻辑无能为力, 只能靠显式声明。
    for t in (site.base_host_templates or []):
        if t and t not in out:
            out.append(t)
    try:
        n = int(site.base_candidate_digits or 0)
    except (TypeError, ValueError):
        n = 0
    # 从 2 起: 无后缀的那个就是 base 本身(已在列表首位), `photos1` 不是真实存在
    # 的形态 —— 生成它只会白白多探一次。
    for i in range(2, n + 1):
        c = f"{d}{i}"
        if c not in out:
            out.append(c)
    # 站点画像排序: 把"命中过"的基址提到前面, 省掉每个新相册那次白试。
    # 只重排**候选集合内部**的顺序, 不增不减 —— 探测仍逐条真验, 画像错了也不会
    # 让本来能探到的相册探不到。
    try:
        from core.cdn_profile import preferred_bases

        head = [b for b in preferred_bases(site.name) if b in out]
    except Exception:
        head = []
    if head:
        out = head + [b for b in out if b not in head]
    return out


def _resolve_base(site, gid, session, media, log=None, hint=None, hints=None,
                  budget=None):
    """探测该相册真实的(CDN 基址, 序号补零宽度)。

    该站会按相册把资源分到不同 CDN 子路径(`photos` / `photos2` / `photos3`),
    甚至补零位数也不同。写死一个 base 的后果是: 相册明明存在, 枚举的却是另一条
    不存在的路径 -> 全部判 missing -> **0 个资源 -> 任务 failed**, 而用户看到
    的只是"失败", 完全不知道是 CDN 路径不对。

    线索来源按可信度排序(hints 顺序即优先级):

        ① 用户输入的直链 —— 他手上最硬的一份证据
        ② 相册页 HTML 里出现的资源直链 —— 页面自己写着真实前缀, 最强
        ③ 候选基址 × 序号宽度 逐个探测 —— 拿不到①②时的兜底

    ①②命中即停(各花 1 次探测验一下), 常见情形根本不会走到 ③。
    `budget=None` 表示试满全部组合(见下, 别轻易调小)。

    全部探不到时退回默认值, 由常规的「连续缺失 -> 停止 -> 空结果判 failed」
    处理 —— 那时确实是这个图集不存在。
    """
    mtype = site.media(media)
    if mtype is None:
        return site.base, None
    default = mtype.root(site)
    default_fmt = mtype.seq_format

    def _try(cand_base, cand_fmt):
        url = mtype.url_for(site, gid, DEFAULT_START, mtype.variants[0],
                            base=cand_base, seq_format=cand_fmt)
        try:
            state, _, _ = probe(session, url, ctype_prefix=mtype.ctype_prefix,
                                accept=mtype.accept, retries=1)
        except Exception:
            state = PROBE_ERROR
        return state

    def _hit(cand_base, cand_fmt, how):
        """命中收尾: 记进站点画像(下次优先试它), 然后原样返回。

        ⚠️ 只在**真的探通**时记 —— 退回默认值不算命中, 把它记进去会让画像
        被"没探到"的结果污染, 反而拖慢下次的真实命中。
        """
        try:
            from core.cdn_profile import record_hit

            record_hit(site.name, cand_base, cand_fmt)
        except Exception:
            pass    # 画像只是优化, 写不进去不该影响采集
        if log:
            log(f"CDN {how}: {cand_base} (序号格式 {cand_fmt})")
        return cand_base, cand_fmt

    # ① ② 线索: 用户直链 + 相册页里出现的直链。去重保序, 先来的更可信。
    all_hints = ([hint] if hint else []) + list(hints or [])
    seen = set()
    for h in all_hints:
        h_base = (h or {}).get("base")
        h_fmt = (h or {}).get("seq_format")
        if not h_base or (h_base, h_fmt) in seen:
            continue
        seen.add((h_base, h_fmt))
        if _try(h_base, h_fmt) == PROBE_OK:
            return _hit(h_base, h_fmt, "基址采信线索")
        if log:
            log(f"线索指向的 {h_base} 探测未命中, 继续尝试下一条")

    # ③ 候选组合
    cands = _base_candidates(site, default)
    fmts = _seq_format_variants(default_fmt)
    # 画像里的**序号格式**也提到最前。
    #
    # 实测这有多值: 循环是"格式外层、基址内层", 所以宽度不对时会把**每个候选
    # 基址都白试一遍**才轮到正确宽度 —— 用户那个相册因此花掉 6 次探测
    # (photos/photos2..5 × 05d 全灭, 才试到 photos2+04d)。把上次命中的宽度
    # 提到首位后, 一轮就中。
    #
    # ⚠️ 只是**排序**提示: 全部候选组合仍然真探一遍, 画像错了(比如另一个相册
    # 宽度不同)最多多花一次探测, 不会让本来能采的相册采不到。
    try:
        from core.cdn_profile import preferred_seq_format

        fav_fmt = preferred_seq_format(site.name)
    except Exception:
        fav_fmt = None
    if fav_fmt and fav_fmt in fmts and fmts[0] != fav_fmt:
        fmts = [fav_fmt] + [f for f in fmts if f != fav_fmt]
    # 预算默认 = 全部组合数。候选基址是一份"可能命中"的清单, 漏试任何一个都等于
    # 把那个相册判成不存在。旧版把预算写死 6(按"3 基址 × 2 宽度"定的), 候选一扩到
    # photos4/photos5 就会在还没试到 photos2+4 位 之前耗光, 静默退回默认基址 ->
    # 0 资源 -> failed —— 恰恰是这整套探测要消灭的那个坑。
    # 探测是 HEAD、不受下载级限速约束, 代价以毫秒计, 值得试满。
    limit_probes = int(budget) if budget else len(cands) * len(fmts)
    spent = 0
    for fmt in fmts:
        for c in cands:
            if spent >= limit_probes:
                if log:
                    log(f"CDN 候选探测已达上限({limit_probes}), 用默认基址")
                return default, default_fmt
            spent += 1
            if _try(c, fmt) == PROBE_OK:
                # 探测命中也要记进画像 —— 这条正是"首次遇到新子路径"时唯一能学到
                # 东西的时机。只记线索命中(上面那支)的话, 画像永远只覆盖用户手动
                # 粘过直链的相册, 对"自动探索"一点帮助都没有。
                return _hit(c, fmt, "探测命中")
            if log:
                log(f"CDN 探测未命中: {c} (序号格式 {fmt})")
    return default, default_fmt


def _empty_hint(site, gid, medias, meta):
    """一个资源都没枚举到时, 给出一条**能据以行动**的错误。

    旧行为是返回空列表, 上层只能记一句"采集到 0 个资源"。用户只知道失败了,
    不知道是站点换了资源路径、图集被删了, 还是自己的 media/过滤条件把资源
    全排除了 —— 而这三者的解法完全不同。这里把"试过什么"和"下一步做什么"
    一并说清楚, 顺带用相册页的自报数量做个对照(有自报却采不到, 就是路径问题)。
    """
    bases = _base_candidates(site)
    shown = ", ".join(bases[:3]) + (f" 等 {len(bases)} 个" if len(bases) > 3 else "")
    m = meta or {}
    declared = ""
    if m.get("photos") or m.get("videos_declared"):
        bits = []
        if m.get("photos"):
            bits.append(f"{m['photos']} 张图")
        if m.get("videos_declared"):
            bits.append(f"{m['videos_declared']} 段视频")
        declared = (" 相册页自报 " + " + ".join(bits)
                    + ", 却一个都没枚举到 —— 基本可以断定是资源路径或序号位数变了。")
    return (
        f"图集 {gid} 未发现任何资源(媒体={','.join(medias)}; 试过资源基址: {shown})。"
        + declared
        + " 先自查 media 有没有把该媒体排除掉; 否则请把该图集任意一张真实图片的"
          "直链粘进输入框 —— 采集器会直接从 URL 读出正确的基址与序号位数。"
    )


def discover(site, gid, quality=None, start=DEFAULT_START, max_count=DEFAULT_MAX,
             miss_stop=DEFAULT_MISS_STOP, session=None, proxy=None,
             min_interval=None, max_interval=None, log=None,
             media="image", album=None, hint_url=None, page_hints=None, diag=None,
             declared_count=None):
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
    diag   可选 dict; 会填入本次**实际生效**的资源根地址与序号格式。这是排查
           "为什么这个相册采不到东西"的唯一线索 —— 写死基址的旧版就是在这里
           静默采到 0 个, 而调用方看不到用的是哪条路径。
    hint_url    用户**原始输入**, 可能是资源直链 —— 直链把基址与序号宽度直接
                写在了 URL 里, 是最硬的一份证据。
    page_hints  相册页 HTML 里出现的资源直链。页面自己写着真实 CDN 前缀, 所以
                这份线索比候选探测可靠得多(而且能发现候选清单里没有的新子路径)。
    declared_count
                相册页**自报**的数量。给了它就可以走快路径(见下)。

    为什么需要快路径
    ----------------
    逐张探测是 O(N) 次 HEAD, 而每次之间还要停顿 —— 300 张图约 90 秒**纯等待**,
    传输时间反而只占 5%。页面既然把数量写在了 HTML 里, 就没有必要再一个个去问。

    但它本质上是"用抽样推断全体", 所以必须守住两条:
      ① **抽样校验** —— 首尾 + 均匀内点全过才敢跳过逐张探测; 任一不中立刻退回
         逐张扫描(最坏情况只是多花几次抽样探测, 不会采错)。
      ② **上界来自站点**而非猜 —— 页面没给数量时默认**不去**指数探上界, 因为
         二分假定序号连续, 中间缺一张就会把上界定在缺口之前(**静默截断**)。
         要启用见 config.enumeration_search 的说明。
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
    # CDN 基址与序号宽度(photos/photos2/photos3..., 00001/0001)**惰性探测**:
    # 写死 base 会让"相册在另一个 CDN 子路径"整体判空 -> 0 资源 -> failed,
    # 而用户只看到失败、看不出是路径不对。但反过来, 若每次枚举都先探一轮候选,
    # 那么绝大多数(就在默认基址上的)相册都要平白多花一次请求。
    # 所以: 先用默认值探第一张 —— 中了就完全不额外开销; 不中才启动候选探测。
    # hint 来自**用户原始输入**(可能是资源直链), 它把答案直接写在了 URL 里,
    # 这里乐观采信、探不中再退回候选探测(用户也可能粘了条失效的旧直链)。
    # 线索按可信度: ① 用户直链 ② 相册页里出现的直链。两者都过 gid 校验 ——
    # 页面里混着推荐位别的相册的图, 而它们未必在同一个 CDN 子路径上; 采信错了
    # 就等于把枚举引到一条不存在的路径, 结果还是"0 资源 -> failed"。
    hint = parse_resource_hint(site, hint_url, gid=gid)
    if not hint:
        for cand in (page_hints or []):
            hint = parse_resource_hint(site, cand, gid=gid)
            if hint:
                if log:
                    log(f"从相册页 HTML 读出资源路径: {hint['base']} "
                        f"(序号格式 {hint['seq_format']})")
                break
    resolved_base = (hint or {}).get("base") or mtype.root(site)
    fmt = (hint or {}).get("seq_format") or mtype.seq_format
    base_fixed = False  # 基址是否已经确认过(只对第一张做一次候选探测)

    def seq_name(n):
        """按探测到的宽度渲染序号(同一站点不同相册可能是 00001 也可能是 0001)。"""
        try:
            return fmt.format(seq=n)
        except Exception:
            return mtype.seq_format.format(seq=n)

    def _beat():
        _pause(min_interval, max_interval)

    def _probe_seq(n, variant=None):
        """探一个序号, 判定与主循环一致(缺画质档时回退最高画质档)。"""
        use = variant or default_variant
        u = mtype.url_for(site, gid, n, use, base=resolved_base, seq_format=fmt)
        st, sz, ct = probe(sess, u, ctype_prefix=mtype.ctype_prefix,
                           accept=mtype.accept)
        if st == PROBE_MISSING and use != top_variant:
            fb = mtype.url_for(site, gid, n, top_variant, base=resolved_base,
                               seq_format=fmt)
            st2, sz2, ct2 = probe(sess, fb, ctype_prefix=mtype.ctype_prefix,
                                  accept=mtype.accept)
            if st2 == PROBE_OK:
                return st2, sz2, ct2, top_variant, fb
        return st, sz, ct, use, u

    def _search_upper():
        """指数探上界 + 二分定位末尾。⚠️ 假定序号连续(见 config.enumeration_search)。"""
        last_ok = start
        step = 1
        while True:
            n = last_ok + step
            if n >= start + limit:
                n = start + limit - 1
                if n <= last_ok:
                    return last_ok
                st, _s, _c, _v, _u = _probe_seq(n)
                _beat()
                return n if st == PROBE_OK else last_ok
            st, _s, _c, _v, _u = _probe_seq(n)
            _beat()
            if st == PROBE_OK:
                last_ok, step = n, step * 2
                continue
            lo, hi = last_ok, n          # 已知 lo 存在、hi 不存在, 二分收窄
            while hi - lo > 1:
                mid = (lo + hi) // 2
                st2, _s2, _c2, _v2, _u2 = _probe_seq(mid)
                _beat()
                if st2 == PROBE_OK:
                    lo = mid
                else:
                    hi = mid
            return lo

    def _yield_range(end, mode, sampled):
        """按校验过的上界一次性给出整段(跳过逐张探测)。"""
        if diag is not None:
            diag["enumeration"] = {
                "media": media, "mode": mode, "end": end, "sampled": sampled,
            }
        for n in range(start, end + 1):
            u = mtype.url_for(site, gid, n, default_variant, base=resolved_base,
                              seq_format=fmt)
            others = [mtype.variants[i] for i in mtype.mirror_order(default_variant)]
            tag = _quality_tag(mtype, default_variant)
            yield {
                "seq": n,
                "type": mtype.name,
                "url": u,
                "mirrors": [mtype.url_for(site, gid, n, v, base=resolved_base,
                                          seq_format=fmt) for v in others],
                # 只有第一张探过, 其余体积交给下载层。大小过滤会自己补一次 HEAD
                # (见 task_manager 的体积预检), 所以这里不额外发请求
                "size": size if n == start else None,
                "filename": f"{group}/{seq_name(n)}{tag}"
                            f"{_ext_of_url(u, mtype.default_ext)}",
            }

    try:
        miss_run = 0
        seq = start
        while seq < start + limit:
            use = default_variant
            main = mtype.url_for(site, gid, seq, use, base=resolved_base,
                                 seq_format=fmt)
            state, size, ctype = probe(sess, main, ctype_prefix=mtype.ctype_prefix,
                                       accept=mtype.accept)

            # 第一张就不中: 极可能是这个相册不在默认(或直链所指的)CDN 路径/宽度上
            # —— 换成候选基址重探一次, 避免"相册存在却采到 0 个"的静默失败。
            if state != PROBE_OK and seq == start and not base_fixed:
                base_fixed = True
                b2, f2 = _resolve_base(site, gid, sess, media, log, hint=hint)
                if (b2, f2) != (resolved_base, fmt):
                    resolved_base, fmt = b2, f2
                    main = mtype.url_for(site, gid, seq, use, base=resolved_base,
                                         seq_format=fmt)
                    state, size, ctype = probe(sess, main,
                                               ctype_prefix=mtype.ctype_prefix,
                                               accept=mtype.accept)

            # ---- 快路径: 有可信上界时, 用抽样校验替代逐张探测 ----
            if state == PROBE_OK and seq == start:
                end, mode = None, None
                if settings.enumeration_fast and declared_count:
                    try:
                        n_decl = int(declared_count)
                    except (TypeError, ValueError):
                        n_decl = 0
                    if n_decl > 0:
                        end = min(start + n_decl - 1, start + limit - 1)
                        mode = "declared"
                if end is None and settings.enumeration_search:
                    end, mode = _search_upper(), "search"
                if end and end > start:
                    # 第一张已在本次循环里探过, 不重复探
                    pts = [p for p in _sample_points(
                        start, end, settings.enumeration_samples) if p != start]
                    bad = None
                    for p in pts:
                        st_p, _sz, _ct, _v, _u = _probe_seq(p)
                        _beat()
                        if st_p != PROBE_OK:
                            bad = p
                            break
                    if bad is None:
                        if log:
                            log(f"[{mtype.name}] 快路径({mode}): 抽样 "
                                f"{len(pts) + 1}/{end - start + 1} 张全部命中, "
                                f"跳过逐张探测")
                        yield from _yield_range(end, mode, len(pts) + 1)
                        return
                    if log:
                        log(f"[{mtype.name}] 快路径({mode}) 在 seq {bad} 未命中, "
                            f"退回逐张探测")

            if state == PROBE_MISSING and use != top_variant:
                # 该档位不存在 != 这张图不存在
                fallback = mtype.url_for(site, gid, seq, top_variant,
                                         base=resolved_base, seq_format=fmt)
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
                    "mirrors": [mtype.url_for(site, gid, seq, v, base=resolved_base,
                                              seq_format=fmt)
                                for v in others],
                    "size": size,
                    "filename": f"{group}/{seq_name(seq)}{tag}"
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
        if diag is not None:
            default_root = mtype.root(site)
            if resolved_base == default_root:
                src = "default"
            elif (hint or {}).get("base") == resolved_base:
                src = "hint"          # 采信了用户贴的直链
            else:
                src = "probe"         # 靠候选探测找到的
            diag.setdefault("roots", {})[media] = {
                "base": resolved_base, "seq_format": fmt,
                "source": src, "site_default": default_root,
            }
        if own:
            sess.close()


def _video_at_first_seq(site, gid, session=None, proxy=None):
    """该图集是否存在 `00001.mp4`。

    用于 media=auto 的判定: 该站视频序号从 1 开始, 所以"第一段在不在"就等价于
    "这个相册有没有视频"。只花一次 HEAD+GET, 比让用户手选媒体类型可靠得多。
    探测失败(网络异常)按"没有"处理 —— 宁可少采也不要把任务搞挂。
    """
    mtype = site.media("video")
    if mtype is None:
        return False
    sess = session or _session(proxy)
    own = session is None
    try:
        resolved_base, fmt = _resolve_base(site, gid, sess, "video")
        state, _, _ = probe(sess, mtype.url_for(site, gid, DEFAULT_START,
                                                mtype.variants[0],
                                                base=resolved_base, seq_format=fmt),
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
    # ⚠️ 不能只比 site.base: 该站会把相册分到 photos/photos2/photos3 等不同
    # CDN 子路径, 只认默认那个会让 photos2 上的直链落不进来 -> 派给 generic
    # -> "不支持预览" 400。候选基址要一起比对(与 _resolve_base 同一份列表)。
    for root in _base_candidates(site):
        r_host, r_path = _netloc_path(root)
        if host == r_host and (path + "/").startswith(r_path + "/"):
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

    def resolve_media(self, site, gid, media_opt, meta=None, log=None, proxy=None):
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
        if _video_at_first_seq(site, gid, proxy=proxy):
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

    def group_name(self, album, gid=""):
        """相册文件夹名 —— 整个输出目录树里**唯一**的一层分类目录。

        ⚠️ 这里不再往相册名外面套标签层(`丝袜-情趣内衣/相册名/`)。布局规则是
        "下载目录的下一级只有相册文件夹"(见 core/layout.py), 套上去的分类层
        只会被布局规则削掉 —— 那就成了"日志和预览显示的路径与实际落盘的不是
        一个", 用户照预览去找文件会找不到。标签本身没丢, 完整写在 album.json
        与 manifest 里。
        """
        return self.normalize_group(album, gid)

    @staticmethod
    def normalize_group(group, gid=""):
        """把分组目录名规范化成 `discover()` 真正会用的形态。

        预览显示的文件名示例必须与真实落盘的路径一致 —— 否则用户照预览去别处
        找文件会找不到。所以规范化只做一次, 两边共用。

        ⚠️ `clean_segment` 会把结尾的点换成下划线, 于是标题是 `..` 时得到 `._` ——
        那是个谁也看不懂的目录名。纯点/下划线拼成的结果一律当作"没取到", 回退
        图集 ID: 与其造一个莫名其妙的目录, 不如用一个能反查的标识。
        """
        name = safe_relative(group) or clean_segment(group)
        if name and set(name.strip()) <= {".", "_"}:
            name = ""
        return name or gid

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
        group = self.group_name(album, gid)

        medias = self.resolve_media(site, gid, media_opt, meta=meta, log=log,
                                    proxy=opts.get("proxy"))
        self.plan_log(log, gid, group, medias, meta)

        items = []
        diag = {}
        # 相册页里引用的图片地址 = 页面自报的真实 CDN 前缀, 优先级仅次于用户直链
        page_hints = (meta or {}).get("resource_urls")
        for name in medias:
            # 页面自报数量 -> 让 discover 走快路径(抽样校验替代逐张 HEAD)
            declared = ((meta or {}).get("photos") if name == "image"
                        else (meta or {}).get("videos_declared"))
            for it in discover(site, gid, quality=quality, media=name,
                               album=group, log=log, max_count=max_count,
                               hint_url=url, page_hints=page_hints,
                               proxy=opts.get("proxy"), diag=diag,
                               declared_count=declared):
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
        if not items:
            # 这里**必须大声失败**: 上层只会把空列表记成"采集到 0 个资源",
            # 用户看不出到底是路径变了、图集没了, 还是 media 设窄了。
            raise ValueError(_empty_hint(site, gid, medias, meta))
        # 相册元信息挂到每条资源上(同一个 dict 引用, 不额外占内存), 由
        # task_manager 落成 sidecar(album.json)。为什么不在这里直接写文件:
        # 采集器的职责是**发现**, 落盘统一归任务层管(见 README 的铁律)。
        task_meta = self.task_meta(site, gid, album, title_mode, medias, meta,
                                   diag, url)
        for it in items:
            it["task_meta"] = task_meta
        return items

    @staticmethod
    def task_meta(site, gid, album, title_mode, medias, meta, diag, source_url):
        """这次采集"采的是什么"的机器可读描述(落成 album.json)。

        ⚠️ `roots` 是这里最有用的一项: 它记录了**实际生效**的资源根地址与序号
        位数, 以及那个地址是怎么定下来的(hint / probe / default)。
        "任务失败但日志看不出为什么"时, 先看这一项 —— 站点悄悄换了 CDN 子路径
        就是靠它认出来的。
        """
        m = meta or {}
        return {
            "collector": site.name,
            "source_url": source_url,
            "gid": gid,
            "album": album,
            "album_source": title_mode,
            "title": m.get("title") or m.get("h1") or "",
            "maker": m.get("maker") or "",
            "tags": list(m.get("tags") or []),
            "media": list(medias),
            "photos_declared": m.get("photos"),
            "videos_declared": m.get("videos_declared"),
            "resource_roots": dict((diag or {}).get("roots") or {}),
        }

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
        group = self.group_name(album, gid)
        medias = self.resolve_media(site, gid, media_opt, meta=meta, log=log,
                                    proxy=opts.get("proxy"))

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
        diag = {}
        # 相册页里引用的图片地址 = 页面自报的真实 CDN 前缀, 与创建时同源
        page_hints = (meta or {}).get("resource_urls")
        # 零枚举路径(页面自报数量)下没有 diag, 靠线索推断出同一个结论
        hint = _first_hint(site, gid, [url, *(page_hints or [])])
        # 页面没给出可用数量 -> 退化成受限枚举; 拿不到页面时这是唯一的办法
        if photos is None and not video_items:
            sampled = True
            for name in medias:
                got = list(discover(site, gid, quality=opts.get("quality"),
                                    media=name, album=group, log=log,
                                    max_count=max_items, hint_url=url,
                                    page_hints=page_hints, diag=diag))
                counts[name] = len(got)
                for it in got[:3]:
                    if it.get("filename"):
                        # 预告里的文件名必须与真实落盘一致(见 normalize_group),
                        # 而归位规则(视频平铺/相册单层)唯一定义在 core/layout.py
                        sample_files.append(
                            layout.place(it["type"], it["filename"], album)[0]
                        )
            photos = counts.get("image")
            if videos_n is None and "video" in counts:
                videos_n = counts["video"]

        # 实际生效的资源根: 枚举过的媒体由 diag 给出(它知道是采信直链、探测命中
        # 还是用了默认值); 零枚举路径(页面自报数量)则由线索补上 —— 预览不枚举
        # 也不能对"到底用的是哪条 CDN 路径"一无所知。
        roots = dict(diag.get("roots") or {})
        if hint:
            for name in medias:
                mtype = site.media(name)
                if mtype is None or name in roots:
                    continue
                roots[name] = {
                    "base": hint["base"], "seq_format": hint["seq_format"],
                    "source": "hint", "site_default": mtype.root(site),
                }

        if not sample_files:
            # 没枚举也要让用户先看到"文件会长什么样"(命名是最容易出错的一环)。
            # 序号位数拿不到枚举结果时, 采信线索给的宽度 —— 曾经无论相册实际用
            # 4 位还是 5 位都写死 00001, 用户照着预告去核对会以为命名错了。
            fmt = ((roots.get(medias[0]) or {}).get("seq_format")
                   or (hint or {}).get("seq_format") or "{seq:05d}")
            for name in medias[:1]:
                mtype = site.media(name)
                if not mtype:
                    continue
                sample_files = [
                    layout.place(name, f"{group}/{fmt.format(seq=i)}{mtype.default_ext}",
                                 album)[0]
                    for i in (1, 2)
                ]

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
            # 实际生效的资源根, 结构与 album.json 的 resource_roots 一致(同一套
            # 词汇, 查问题时不用在两处翻译)。写死基址的旧版在预览这里静默显示
            # 0 张, 用户看不到"用的是哪条路径", 于是无从判断是路径变了还是图集没了。
            "resource_roots": roots,
            "warning": _empty_hint(site, gid, medias, meta) if (
                sampled and not any(counts.values())
            ) else "",
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
