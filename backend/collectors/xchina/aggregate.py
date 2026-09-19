"""XChina 聚合页采集器: 一个模特/系列/索引 -> 它下面的全部相册与视频。

为什么需要单独一个采集器
======================
图集采集器处理的是"**一个**相册"。但用户最常提的需求是"整个模特一次下完"。
模特落地页 / 系列页 / 模特索引本身不是相册, 它只提供**指向相册的链接** ——
所以这一层的职责就是"把聚合页展开成一堆相册 URL", 再交给既有采集器。

三个设计决定
============

1. **URL 驱动抽取, 不依赖 DOM 结构。**
   从 HTML 里取出全部 href, 再按 URL 模式分类。只要站点不改 URL 形态,
   改版也能用。反过来, 依赖 `class="item"` 这类选择器的写法, 站点一改版就
   **静默采到 0 个** —— 那正是本项目反复强调的最难查的失败模式。
   代价是拿不到锚文本, 所以标题得另想办法(见 `_group_name`)。

2. **委派, 不重写。**
   展开出子页面 URL 后逐个交给已有的 `xchina_gallery` / `xchina_video`,
   命名/去重/过滤/下载/重试全部复用。本模块**只做"发现子页面"这一件事**,
   一行下载逻辑都不碰 —— 这是 `README` 里那条铁律
   (`URL -> Extractor -> Resource -> Downloader -> Storage`) 的直接体现。

3. **数量与层数双闸门。**
   一个模特页可能挂 60 个相册, 一个索引页挂着 108 个模特 —— 不设限就是
   几万条资源、几百次浏览器。所以:
   - `max_items`: 一次最多展开多少个条目(相册+视频), 默认 50;
   - `aggregate_depth`: 还能再往下钻几层, 默认 1。
   到顶了会**明确写日志**, 而不是悄悄少采。

为什么没有 "list-only" 模式
===========================
"只列清单不下载"在现有资源模型里没有自然位置: 资源 = 一个可下载的 URL,
而"清单"不是 URL。硬塞(比如让每条清单项都当 text 资源去 GET 相册页)会污染
下载层, 而且相册页是 CF 保护的, 全都会存成 403 错误页。
"先看看有什么"这个需求由 **`preview()`** 承担 —— 它零下载地报出
"这个模特有 N 个相册 / M 个视频", 列表也在日志里。这比一个别扭的 list 模式诚实。

实测的 URL 结构(2026-09-19, 只读低频探测)
=====================================
    /models.html                模特索引      200 开放, 108 个模特
    /models/type-{n}.html       模特分类      200
    /model/id-{id}.html         模特落地页    200 开放, 自报"收录视频数: 60"
    /actor/id-{id}.html         演员页        200
    /photos/series-{id}.html    相机系列      403 CF
    /videos/series-{id}.html    视频系列      403 CF
    /photos/model-{id}.html     某模特全部图  403 CF
    /videos/model-{id}.html     某模特全部视频 403 CF(headless 可过)

**规律: 落地/索引页开放, 全量列表页 CF 保护。** 这正好配合
`album_meta.load_page_html` 的"先纯 HTTP 再 headless"策略: 索引页与落地页
常见情形下零浏览器开销。

⚠️ 我原本猜的 `/model`、`/tags`、`/series`、`/photos` 全是 **404**。
   先探再写, 不猜 —— 猜错就是又一次"任务成功但 0 资源"。
"""
import inspect
import re
from urllib.parse import urljoin, urlparse

from collectors.album_meta import DEFAULT_TITLE_SPLIT, load_page_html
from collectors.scores import SCORE_AGGREGATE_PAGE
from core.naming import clean_segment

from .. import register

#: 站点主域。子域(`www.` / `img.`)也算, 用后缀匹配。
SITE_HOST = "xchina.co"

#: 索引页: 页面上是一堆**条目**(模特/分类/集合)的列表。
_INDEX_PATHS = frozenset({
    "/models.html", "/actors.html", "/photos.html", "/videos.html",
    "/series.html", "/categories.html", "/collections.html",
    "/trend.html", "/amateurs.html",
})
#: 索引页的分类形态, 如 /models/type-7.html
_INDEX_TYPE_RE = re.compile(
    r"^/(?:models|actors|photos|videos|series|categories|collections|amateurs)"
    r"/type-\d+\.html$",
    re.I,
)
#: 落地页: 单个模特/演员的介绍页, 也挂着它的相册链接。
_LANDING_RE = re.compile(
    r"^/(?:model|actor)/id-([0-9A-Za-z_-]{6,})(?:/\d+)?\.html$", re.I
)
#: 全量列表页: 某模特/系列的全部资源(实测受 CF 保护)。
_FULL_LIST_RE = re.compile(
    r"^/(?:photos|videos)/(?:model|series)-([0-9A-Za-z_-]{6,})(?:/\d+)?\.html$",
    re.I,
)

#: 子条目(真正的内容页) —— 这些是要交给图集/视频采集器的。
_CHILD_ALBUM_RE = re.compile(r"^/photo/id-([0-9A-Za-z_-]{6,})(?:/\d+)?\.html$", re.I)
_CHILD_VIDEO_RE = re.compile(r"^/video/id-([0-9A-Za-z_-]{6,})\.html$", re.I)

_HREF_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["']""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

#: 页面"就绪"的标记: **目标驱动** —— 出现内容链接才算好。
#: 不能用 `<title>` 就绪: 挑战解开的导航中途标题是 `Loading <url>`,
#: 有 title 却不是真页面(见 album_meta 模块文档里那个 8 秒陷阱)。
_READY_MARKERS = ("/photo/id-", "/video/id-", "/model/id-", "/actor/id-")

DEFAULT_MAX_ITEMS = 50
DEFAULT_DEPTH = 1


def _classify(path):
    """把站内路径分类; 认不出返回 None。"""
    if not path or not path.startswith("/"):
        return None
    p = path.split("?", 1)[0].split("#", 1)[0]
    if p.lower() in _INDEX_PATHS or _INDEX_TYPE_RE.match(p):
        return "index"
    if _LANDING_RE.match(p):
        return "landing"
    if _FULL_LIST_RE.match(p):
        return "list"
    if _CHILD_ALBUM_RE.match(p):
        return "album"
    if _CHILD_VIDEO_RE.match(p):
        return "video"
    return None


def _same_site(netloc):
    host = (netloc or "").lower().split(":")[0]
    return host == SITE_HOST or host.endswith("." + SITE_HOST)


def _path(url):
    try:
        return urlparse(url or "").path
    except Exception:
        return ""


def _canonical(path, kind):
    """内容页归一: 同一份内容的不同 URL 形态只留一条。

    ⚠️ 只归一 **内容页**(album / video)。`/photo/id-X/10.html` 是那个相册的
    第 10 页, 与 `/photo/id-X.html` 是同一份内容 —— 不归一的话一个相册会被
    当成 N 个条目: 既对同一批图重复发起采集, 又把 `max_items` 的名额白白吃掉。

    **列表页绝不能归一**: `/photos/model-X/2.html` 是列表的第 2 页, 里面是
    **另一批**相册, 归一成 `/photos/model-X.html` 会直接漏采 —— 而且因为
    去重是静默的, 用户只会觉得"少了几张", 完全查不出来。
    """
    if kind == "album":
        m = _CHILD_ALBUM_RE.match(path)
        if m:
            return f"/photo/id-{m.group(1)}.html"
    elif kind == "video":
        m = _CHILD_VIDEO_RE.match(path)
        if m:
            return f"/video/id-{m.group(1)}.html"
    return path


def extract_children(html, base_url, want=("album", "video")):
    """从页面 HTML 里抽出内容链接, 按 URL 模式分类。

    返回 `{"album": [绝对 URL, ...], "video": [...], ...}`, 去重且保持出现顺序
    (顺序有意义: 站点通常把新的排前面, 而 `max_items` 截断的就是尾巴)。

    去重按 `_canonical()` 归一后的路径 —— 内容页的分页形态(track 到的第 N 页)
    与它的首页是同一条, 不能重复计数。

    ⚠️ URL 驱动, 不是 DOM 驱动 —— 见模块文档第 1 条。
    """
    allow = set(want)
    seen = set()
    out = {}
    for m in _HREF_RE.finditer(html or ""):
        raw = (m.group(1) or "").strip()
        if not raw or raw[0] in "#?" or raw.lower().startswith(
            ("javascript:", "mailto:", "tel:", "data:")
        ):
            continue
        full = urljoin(base_url, raw)
        u = urlparse(full)
        if not _same_site(u.netloc):
            continue
        kind = _classify(u.path)
        if kind is None or kind not in allow:
            continue
        canon = _canonical(u.path, kind)
        if canon in seen:
            continue
        seen.add(canon)
        out.setdefault(kind, []).append(f"{u.scheme}://{u.netloc}{canon}")
    return out


def _int_opt(value, default):
    """读正整数选项; 非法就用默认值(选项是用户输入的, 不该让任务挂在解析上)。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _page_title(html):
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    # 站点标题形如 "模特名 - 分类 - 站点名": 只取第一段当目录名
    if DEFAULT_TITLE_SPLIT:
        parts = re.split(DEFAULT_TITLE_SPLIT, raw, maxsplit=1)
        if parts and parts[0].strip():
            raw = parts[0].strip()
    return raw


@register("xchina_aggregate")
class XChinaAggregateSpider:
    """聚合页采集器: 展开聚合页 -> 委派给图集/视频采集器。"""

    # --- 自动识别 ---
    @staticmethod
    def match_score(url):
        """只认**聚合形态**(索引/落地/全量列表)。

        刻意不认 `/photo/id-*` 与 `/video/id-*`: 那是图集与视频采集器的地盘,
        这边抢过来只会把"一个相册"错当成"一个模特"。
        """
        s = (url or "").strip()
        if "://" not in s:
            return None
        u = urlparse(s)
        if not _same_site(u.netloc):
            return None
        return SCORE_AGGREGATE_PAGE if _classify(u.path) in (
            "index", "landing", "list"
        ) else None

    # --- 页面加载(可注入, 测试不必真的开浏览器/联网) ---
    def _load(self, url, kind, log, loader=None, session=None):
        loader = loader or load_page_html
        html, src = loader(url, markers=_READY_MARKERS, log=log,
                           ready_label=f"聚合页({kind})")
        if html and log:
            log(f"聚合页读取成功(来源 {src}): {url}")
        return html

    # --- 展开 ---
    def _expand(self, url, kind, depth, *, depth_limit, max_items, media, log,
                state, loader=None, session=None):
        """递归展开: 取一页 -> 抽子链接 -> 累积内容页 / 继续下钻。

        `state` 汇总累积: albums / videos / failed / seen。
        """
        if url in state["seen"]:
            return
        if len(state["albums"]) + len(state["videos"]) >= max_items:
            return
        state["seen"].add(url)

        html = self._load(url, kind, log, loader=loader, session=session)
        if not html:
            state["failed"].append({"url": url, "kind": kind})
            return
        # 根页面的标题就是聚合层的父目录名(模特名/系列名)。只在第一层取 ——
        # 更深层的页面标题是"某个子分类", 拿来当父目录会让所有文件挤进一个怪名字。
        if not state.get("group"):
            state["group"] = clean_segment(_page_title(html))

        # 抽**全部**类别再决定怎么用。先按 depth 决定要不要抽, 就永远不知道
        # 被截掉了什么 —— 日志也就写不出"还有 N 个入口未展开", 而那正是用户
        # 判断"我是不是少采了"的唯一依据。
        kids = extract_children(
            html, url, want=("album", "video", "list", "landing", "index")
        )
        # `media=image` 时连视频条目都不收 —— 收了也会被委派阶段跳过
        if media not in ("auto", "video", "both"):
            kids.pop("video", None)

        # 闸门在**追加时**生效, 不是只在进入页面时看一眼: 一个索引页能挂上百个
        # 条目, 只在入口检查等于没检查(进来时是 0, 收完是 100)。
        truncated = False
        for kind, bucket in (("album", "albums"), ("video", "videos")):
            for u in kids.get(kind, []):
                if len(state["albums"]) + len(state["videos"]) >= max_items:
                    truncated = True
                    break
                if u not in state[bucket]:
                    state[bucket].append(u)
        if truncated:
            # 冒泡给 preview: 触顶时报出的数量只是**下限**, 不能当真值展示,
            # 否则用户会照着一个偏小的数字判断"这个模特就这点东西"。
            state["truncated"] = True

        deeper = kids.get("list", []) + kids.get("landing", []) + kids.get("index", [])
        if depth >= depth_limit:
            if deeper:
                state["depth_capped"] = True
                if log:
                    # 少采必须**说出来**。悄悄截断最容易被误当成"站点只有这些"。
                    log(f"已到展开层数上限({depth_limit} 层), 本页还有 {len(deeper)} 个"
                        "更深入口未展开; 要更全请调大 aggregate_depth")
            return
        if truncated and log:
            log(f"已达条目上限(max_items={max_items}), 本页后面的条目未展开; "
                "要更全请调大 max_items")
        for u in deeper:
            self._expand(u, _classify(_path(u)), depth + 1,
                         depth_limit=depth_limit, max_items=max_items,
                         media=media, log=log, state=state,
                         loader=loader, session=session)

    # --- 委派 ---
    @staticmethod
    def _call_crawl(spider, url, opts, log, session=None, browser_runner=None):
        """按子采集器**实际支持的参数**注入, 老采集器不受影响。

        与 `task_manager._crawl` 同一套思路: 只看签名, 不硬编码。
        """
        try:
            params = inspect.signature(spider.crawl).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs = {}
        if "log" in params:
            kwargs["log"] = log
        if "options" in params:
            kwargs["options"] = opts
        if session is not None and "session" in params:
            kwargs["session"] = session
        if browser_runner is not None and "browser_runner" in params:
            kwargs["browser_runner"] = browser_runner
        return spider.crawl(url, **kwargs) or []

    def _delegate(self, state, opts, log, group, session=None, browser_runner=None):
        """把内容页逐个交给对应采集器; 单个失败不影响其余。

        60 个相册里坏掉 1 个是常态 —— 因为一个子页面失败就整体报错, 用户会
        以为"这个模特采不了", 而实际上 59 个相册都是好的。
        """
        from .. import get_collector  # 延迟导入: 避免 __init__ 尚未完成的时序问题

        items, failures = [], []
        for child in state["albums"] + state["videos"]:
            is_video = bool(_CHILD_VIDEO_RE.match(_path(child)))
            name = "xchina_video" if is_video else "xchina_gallery"
            try:
                spider = get_collector(name)
                got = self._call_crawl(spider, child, opts, log,
                                       session=session,
                                       browser_runner=browser_runner)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                failures.append({"url": child, "error": msg})
                if log:
                    # 说清"跳过了"而不是让用户自己数少了几个
                    log(f"子页面展开失败, 已跳过: {child} ({msg[:120]})")
                continue
            for r in got:
                fn = r.get("filename")
                if fn and group:
                    # 子采集器给的已是 `{相册名}/{seq}`, 再套一层聚合层父目录,
                    # 变成 `{模特名}/{相册名}/{seq}` —— 否则不同相册的文件会平铺
                    r["filename"] = f"{group}/{fn}"
                items.append(r)
        state["failed"].extend(failures)
        return items

    # --- 资源发现 ---
    def crawl(self, url, options=None, log=None, max_count=None, session=None,
              browser_runner=None, page_loader=None):
        opts = dict(options or {})
        max_items = _int_opt(opts.get("max_items"), DEFAULT_MAX_ITEMS)
        depth_limit = _int_opt(opts.get("aggregate_depth"), DEFAULT_DEPTH)
        media = str(opts.get("media") or "auto").lower()

        root_kind = _classify(_path(url))
        if root_kind not in ("index", "landing", "list"):
            raise ValueError(
                f"{url!r} 不是聚合页。本采集器只处理模特/系列/索引页, "
                "如 https://xchina.co/model/id-XXXX.html 或 https://xchina.co/models.html"
            )

        state = {"albums": [], "videos": [], "failed": [], "seen": set()}
        self._expand(url, root_kind, 0, depth_limit=depth_limit,
                     max_items=max_items, media=media, log=log, state=state,
                     loader=page_loader, session=session)

        # 闸门: 先截断再委派, 避免为了一堆用不到的子页面去开浏览器。
        # `max_items` 是**条目总数**上限(相册+视频合计), 与 _expand 的口径一致 ——
        # 两处各用各的口径会让"预告说 50、实际下 100"这种事重新长出来。
        state["albums"] = state["albums"][:max_items]
        state["videos"] = state["videos"][:max(0, max_items - len(state["albums"]))]
        if not state["albums"] and not state["videos"]:
            raise ValueError(self._empty_hint(url, root_kind, state))

        # 父目录取页面标题; 但 crawl 阶段我们已经不再持有 html, 用状态里的标题
        group = state.get("group") or self._fallback_group(url)
        items = self._delegate(state, opts, log, group,
                               session=session, browser_runner=browser_runner)
        if not items:
            raise ValueError(self._empty_hint(url, root_kind, state))

        if log:
            log(f"聚合页展开完成: {len(state['albums'])} 个相册 + "
                f"{len(state['videos'])} 个视频页 -> {len(items)} 个资源")
            if state["failed"]:
                log(f"其中 {len(state['failed'])} 个子页面未能展开(见上), "
                    "不影响的资源照常下载", "warn")
        task_meta = self._task_meta(url, root_kind, state, group, media)
        for r in items:
            r["task_meta"] = task_meta
        return items

    # --- 命名与诊断 ---
    @staticmethod
    def _fallback_group(url):
        """取不到页面标题时的父目录名: 用 URL 里的 ID, 绝不空手。"""
        p = _path(url)
        for rx in (_LANDING_RE, _FULL_LIST_RE):
            m = rx.match(p)
            if m:
                return clean_segment(m.group(1)) or "aggregate"
        return "aggregate"

    @staticmethod
    def _empty_hint(url, kind, state):
        """一个内容页都没展开出来时, 给一条**能据以行动**的错误。"""
        tried = len(state["seen"])
        failed = len(state["failed"])
        kind_label = {"index": "索引页", "landing": "模特/演员页", "list": "全量列表页"}
        hint = (
            f"{kind_label.get(kind, '聚合页')} {url} 上没找到任何相册或视频链接"
            f"(读了 {tried} 个页面"
        )
        if failed:
            hint += f", {failed} 个被 Cloudflare 拦截或读取失败"
        hint += (
            ")。下一版可能的情况与做法: ① 站点改了 URL 形态 —— 请直接从浏览器"
            "复制某个相册的地址粘进来, 采集器会认它; ② 该页被 CF 拦住 —— "
            "稍后重试, 或先用「登录态」登录一次; ③ 这一页确实没有内容。"
        )
        return hint

    @staticmethod
    def _task_meta(url, kind, state, group, media):
        """聚合级元信息(落成 album.json 的那一份)。

        ⚠️ 这份**覆盖**子采集器各自的 task_meta: cascade 下一个任务会跨几十个
        相册, 只记第一个相册的元信息会误导用户。这里记录"这一趟展开了什么、
        哪些没展开成", 那才是排查"我少采了哪个相册"时真正要看的东西。
        """
        return {
            "collector": "xchina_aggregate",
            "source_url": url,
            "kind": kind,
            "group": group,
            "mode": "cascade",
            "media": [media],
            "albums": list(state["albums"][:200]),
            "album_count": len(state["albums"]),
            "videos": list(state["videos"][:200]),
            "video_count": len(state["videos"]),
            "pages_read": sorted(state["seen"])[:200],
            "failed": [dict(f) for f in state["failed"][:100]],
            "failed_count": len(state["failed"]),
            # "我少采了没有"是聚合任务最该被记录的一件事
            "truncated": bool(state.get("truncated") or state.get("depth_capped")),
            "resource_roots": {},
        }

    # --- 创建前预览 ---
    def preview(self, url, options=None, log=None, max_items=12, session=None,
                page_loader=None):
        """零下载地报"这个聚合页下面有什么"。

        这是 `list-only` 模式的替代品: 用户想要"先看看有哪些相册"时, 看这里
        就够 —— 十几秒拿到"N 个相册 / M 个视频", 而不用先建一个任务。
        """
        opts = dict(options or {})
        depth_limit = _int_opt(opts.get("aggregate_depth"), DEFAULT_DEPTH)
        media = str(opts.get("media") or "auto").lower()
        # 闸门口径必须与 crawl 一致: 创建时用的是 options 里的 max_items,
        # 而预览自己的 max_items 参数只是**防拖慢的上界**。取两者较小值 ——
        # 否则用户设了 5、预览却按 12 展开, 预告就不代表创建行为。
        max_items = min(
            _int_opt(opts.get("max_items"), DEFAULT_MAX_ITEMS),
            _int_opt(max_items, 12),
        )
        root_kind = _classify(_path(url))
        if root_kind not in ("index", "landing", "list"):
            raise ValueError(
                f"{url!r} 不是聚合页。可接受的输入: 模特页 /model/id-*.html、"
                "索引页 /models.html、全量列表页 /photos/model-*.html"
            )

        state = {"albums": [], "videos": [], "failed": [], "seen": set()}
        self._expand(url, root_kind, 0, depth_limit=depth_limit,
                     max_items=max_items, media=media, log=log, state=state,
                     loader=page_loader, session=session)
        # 父目录与 crawl 保持**同一个来源**: 根页面标题 -> 回退 URL 里的 ID。
        # 曾经这里直接 `_fallback_group(url)`, 于是预告显示 gid、实际落盘却是
        # 模特名 —— 用户照预告去核对目录, 会发现"名字不对", 而两者其实都"对",
        # 只是走了两条路。命名这类事只允许有一个来源。
        group = state.get("group") or self._fallback_group(url)
        # 触顶(条目或层数)时, 下面的数量只是**下限**。如实标出来, 别让用户
        # 拿一个偏小的数字当结论 —— 这与图集预览的 `sampled=true` 同一套语义。
        capped = bool(state.get("truncated") or state.get("depth_capped"))
        if log:
            log(f"预告: 展开 {len(state['albums'])} 个相册 + "
                f"{len(state['videos'])} 个视频页"
                + (f", {len(state['failed'])} 个页面未能读取" if state["failed"] else "")
                + ("(已达上限, 实际可能更多)" if capped else ""))

        files = [u.rsplit("/", 1)[-1] for u in state["albums"][:max_items]]
        warning = ""
        if not (state["albums"] or state["videos"]):
            warning = self._empty_hint(url, root_kind, state)
        elif capped:
            warning = (
                "已达展开上限, 上面的数量只是**下限**。要拿全量请调大 "
                "aggregate_depth / max_items 后再创建任务。"
            )
        return {
            "collector": "xchina_aggregate",
            "kind": "aggregate",
            "gid": group,
            "group": group,
            "album": group,
            "album_source": "url",
            # 与 `_expand` 的取舍规则一致: media=image 时不收视频条目,
            # media=video 时相册会以 video 形式采(只取其视频)。报告"这次要采
            # 哪些媒体"就该照用户设的来, 而不是照"展开了哪些条目"。
            "media": [m for m, ok in (("image", media in ("auto", "image", "both")),
                                      ("video", media in ("auto", "video", "both")))
                      if ok],
            # 这里的 photos/videos 是**子页面数量**, 不是文件数 —— 前端靠
            # `kind == "aggregate"` 换成"将展开 N 个相册"的文案。
            "photos": len(state["albums"]),
            "videos": len(state["videos"]),
            "photos_declared": None,
            "videos_declared": len(state["videos"]),
            "video_items": [],
            "video_bytes": 0,
            "tags": [],
            "maker": None,
            "h1": "",
            "page": True,
            "sampled": capped,
            "sample_files": files,
            "resource_roots": {},
            "warning": warning,
            "logs": [],
        }
