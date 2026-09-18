"""序号枚举型图集采集器的公共基类。

适用场景
========
「一个图集/相册 ID 之下, 有 N 个按序号命名的资源」:

    https://img.example.com/photos/{gid}/00001.jpg
    https://img.example.com/photos/{gid}/00001_600x0.webp

站点差异全部用声明式的 `GallerySite` 表达。接入新站只需要写一份配置,
不必复制本模块的枚举 / 存在性判定 / 限速 / 镜像构造逻辑。

接入前必须实测的两个未知量
==========================
1. **存在性怎么判定** —— 不是所有站点都返回 404。实测过的坑: img.xchina.io 对越界
   序号同样返回 200, 只是 Content-Type 变成 text/html。因此判定只走 `probe()`
   返回的三态, 且严格区分"不存在"与"探测失败"(后者重试, 绝不当作不存在,
   否则图集会从中间被截断)。
2. **是否需要特殊请求头** —— 部分 WAF 会校验 Accept, 见 `core.config.IMAGE_ACCEPT`。

画质档
======
`variants` 按画质**从高到低**排列。用户可按 `quality` 选择主 URL 用哪一档,
其余档位自动成为 mirrors(故障备用)。若某张图缺所选档位, 会自动回退到最高
画质档而不是直接判为不存在 —— 这是"不漏图"的关键。
"""

import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

from core.config import IMAGE_ACCEPT, settings

PROBE_OK = "ok"            # 资源存在
PROBE_MISSING = "missing"  # 序号越界(服务器仍未返回 404, 但内容不是媒体)
PROBE_ERROR = "error"      # 网络/5xx, 需重试, 不可与"不存在"混同

QUALITY_KEYS = ("original", "1200", "800", "600")
DEFAULT_QUALITY = "original"

DEFAULT_START = 1
DEFAULT_MAX = 1000         # 硬上限, 防止判定失效时无限枚举
DEFAULT_MISS_STOP = 3      # 连续多少次判定为不存在就停止

_ID_CHARS = re.compile(r"[0-9A-Za-z_-]{6,}")
_THUMB_NAME = re.compile(r"\d{3,}_[0-9x]+")
_EXT_TAIL = re.compile(r"\.(jpe?g|webp|png|gif|mp4)$", re.I)


@dataclass
class GallerySite:
    """一个「序号枚举型」图集站点的声明式描述。"""

    name: str
    base: str                       # URL 前缀, 如 https://img.xchina.io/photos
    variants: list                  # 变体后缀, 按画质从高到低
    quality_map: dict = field(default_factory=dict)  # 画质档 -> 后缀
    seq_format: str = "{seq:05d}"
    id_in_path: Optional[str] = None  # 从路径提取 ID 的正则(第 1 组即 ID)
    id_in_query: str = "id"

    def url_for(self, gid, seq, variant=None):
        suffix = variant if variant is not None else self.variants[0]
        return f"{self.base}/{gid}/{self.seq_format.format(seq=seq)}{suffix}"

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


def parse_gid(site, raw):
    """从「图集 ID / 资源 URL / 页面 URL」中提取图集 ID。

    支持三种输入::

        6aa113208a506
        https://img.xchina.io/photos/6aa113208a506/00001.jpg
        https://xchina.co/photoShow.html?id=6aa113208a506
    """
    s = (raw or "").strip()
    if not s:
        return None
    # 纯 ID: 既没有协议也没有路径分隔符, 且字符集合法
    if "://" not in s and "/" not in s:
        return s if _ID_CHARS.fullmatch(s) else None
    if site.id_in_path:
        m = re.search(site.id_in_path, s)
        if m:
            return m.group(1)
    m = re.search(rf"[?&]{re.escape(site.id_in_query)}=([0-9A-Za-z_-]{{6,}})", s)
    if m:
        return m.group(1)
    # 退路: 取路径最后一段并去掉扩展名
    seg = urlparse(s).path.rstrip("/").rsplit("/", 1)[-1]
    seg = _EXT_TAIL.sub("", seg)
    # 形如 00046_600x0 的缩略图名不是图集 ID, 放弃
    if _THUMB_NAME.fullmatch(seg):
        return None
    return seg or None


def _session(proxy=None):
    s = requests.Session()
    p = proxy if proxy is not None else settings.proxy
    if p:
        s.proxies.update({"http": p, "https": p})
    return s


def probe(session, url, timeout=None, retries=2):
    """探测单个资源是否存在, 返回 (state, size, content_type)。

    state 取 PROBE_OK / PROBE_MISSING / PROBE_ERROR。
    网络异常与 5xx 归为 ERROR(可重试), 与"序号不存在"严格区分 ——
    把 ERROR 当成 MISSING 会导致图集从中间被截断。
    """
    headers = {"User-Agent": settings.user_agent, "Accept": IMAGE_ACCEPT}
    last = (PROBE_ERROR, None, None)
    for _ in range(max(1, retries)):
        try:
            resp = session.head(
                url, headers=headers, allow_redirects=True,
                timeout=timeout or settings.request_timeout,
            )
        except Exception:
            last = (PROBE_ERROR, None, None)
            continue

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if resp.status_code >= 500:
            last = (PROBE_ERROR, None, ctype)
            continue
        if resp.status_code == 200 and ctype.startswith("image/"):
            raw_len = resp.headers.get("Content-Length")
            try:
                size = int(raw_len) if raw_len else None
            except ValueError:
                size = None
            return (PROBE_OK, size, ctype)
        # 200 但内容不是图片(越界时返回 html), 或 4xx: 一律视为不存在
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


def _ext_of_url(url):
    """取 URL 末段扩展名(含点, 小写); URL 没有扩展名时按图片兜底为 .jpg。"""
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return ("." + tail.rsplit(".", 1)[1].lower()) if "." in tail else ".jpg"


def discover(site, gid, quality=None, start=DEFAULT_START, max_count=DEFAULT_MAX,
             miss_stop=DEFAULT_MISS_STOP, session=None, proxy=None,
             min_interval=None, max_interval=None, log=None):
    """枚举图集资源, 逐个 yield 结果字典。

    yield::

        {"seq": 1, "url": "...00001.jpg", "mirrors": [...], "size": 656713}

    停止条件(任一): 连续 miss_stop 次判定为不存在 / 达到 max_count 硬上限。

    quality 决定主 URL 取哪一档变体; 若该档位在这张图上缺失, 退回最高画质档,
    避免把"缺一个尺寸"误判成"这张图不存在"。
    """
    gid = (gid or "").strip()
    default_variant = site.variant_of(quality)
    top_variant = site.variants[0]
    sess = session or _session(proxy)
    own = session is None

    try:
        miss_run = 0
        seq = start
        while seq < start + max_count:
            use = default_variant
            main = site.url_for(gid, seq, use)
            state, size, ctype = probe(sess, main)

            if state == PROBE_MISSING and use != top_variant:
                # 该档位不存在 != 这张图不存在
                fallback = site.url_for(gid, seq, top_variant)
                state, size, ctype = probe(sess, fallback)
                if state == PROBE_OK:
                    use, main = top_variant, fallback
                    if log:
                        log(f"seq {seq:05d} 缺 {quality} 档, 回退最高画质")

            if state == PROBE_OK:
                miss_run = 0
                others = [site.variants[i] for i in site.mirror_order(use)]
                # filename 是"建议"而非命令: task_manager 会过一遍安全清洗,
                # 用户显式指定 name_template 时也会被覆盖。之所以在这里按
                # 相册分子目录, 是因为**采集器知道 gid 而下载层不知道**。
                suffix = "" if use in ("original", None) else f"_{use}"
                yield {
                    "seq": seq,
                    "url": main,
                    "mirrors": [site.url_for(gid, seq, v) for v in others],
                    "size": size,
                    "filename": f"{gid}/{seq:05d}{suffix}{_ext_of_url(main)}",
                }
            elif state == PROBE_MISSING:
                miss_run += 1
                if log:
                    log(f"seq {seq:05d} 不存在({ctype or 'no content-type'}), "
                        f"连续缺失 {miss_run}/{miss_stop}")
                if miss_run >= miss_stop:
                    break
            else:  # ERROR: 重试后仍失败, 不计入连续缺失(避免误截断), 但防止死循环
                if log:
                    log(f"seq {seq:05d} 探测失败, 跳过")
                miss_run += 1
                if miss_run >= miss_stop * 2:
                    break

            seq += 1
            if seq < start + max_count:
                _pause(min_interval, max_interval)
    finally:
        if own:
            sess.close()


class SequenceGallerySpider:
    """序号枚举型采集器基类: 子类只需声明 `site`。"""

    site: GallerySite = None

    def crawl(self, url, options=None, log=None):
        site = self.site
        if site is None:
            raise RuntimeError(f"{type(self).__name__} 未声明 site")
        gid = parse_gid(site, url)
        if not gid:
            raise ValueError(f"无法从 {url!r} 解析出图集 ID")
        quality = (options or {}).get("quality")
        items = []
        for it in discover(site, gid, quality=quality, log=log):
            items.append({
                "type": "image",
                "url": it["url"],
                "headers": None,
                "mirrors": it["mirrors"],
                "size": it["size"],
                "filename": it.get("filename"),
            })
        return items
