"""相册页元信息: `<title>`(当作输出目录名) 与视频线索。

为什么要用浏览器
================
`xchina.co` 的相册页对普通 HTTP 请求返回 **403**(Cloudflare challenge),
`requests` 拿不到 HTML; headless Chromium 能自己把挑战解开。所以这里复用项目本来
就有的 Playwright 依赖。

⚠️ 不要退回"用 requests 抓相册页 HTML": 那条路在本站是死的(403)。
   纯 HTTP 的序号枚举才是资源发现的主路径, 本模块只负责命名与判断。

Cloudflare 的两个坑(2026-09-18 实测, 都踩过)
============================================
1. **首次拿到的是挑战页**(`<title>Just a moment...</title>`), 几秒后才自动跳到
   真实页面。所以不能固定 sleep(2.5s 时还没过、8s 时已过), 只能轮询;
   也不能用 `wait_for_function`(挑战成功靠一次导航完成, 导航会销毁执行上下文,
   实测等到超时也拿不到返回值)。这里用"每秒读一次标题"的轮询。

2. **不要默认带上已保存的登录态**。`browser_state/xchina.co.json` 里那份
   `cf_clearance` 一旦与当前 IP/指纹不匹配, Cloudflare 会直接回
   `Attention Required! | Cloudflare`(永久拒绝, 不会自愈); 而**匿名**访问反而
   能正常解开挑战。所以顺序是: 先匿名读, 读不到符合预期的内容再带登录态重试
   (付费相册才需要)。这与 `collectors/browser.py` 的做法不同 —— 那边是通用采集,
   这里的页面是公开的。

失败怎么办
==========
一律返回 None, 由调用方回退成"用图集 ID 当目录名"。
**标题只是命名优化, 拿不到绝不能让采集任务失败** —— 否则一次 Cloudflare 抖动
或没装浏览器就会让整个相册采不动。

实测(2026-09-18, 相册 6a3654854fd25)
====================================
页面里有几段可用线索::

    <title>约啪172cm车模 完美肉体 主动求操 - 国模套图 - 各国其他套图 - 小黄书 xChina</title>
    <h1 class="hero-title-item">约啪172cm车模 完美肉体 主动求操（FENDSON）</h1>
    <i class="fas fa-image"></i>…<div class="text">12P + 4V</div>      <- 站点自报数量
    <i class="fas fa-file"></i>…<div class="text">FENDSON</div>        <- 厂牌
    <div class="item tags-line">…<div class="tag">丝袜</div>…          <- 标签
    var domain = "https://img.xchina.io";
    var favOptions = {"enabled":true,"objMode":"photo","objId":"6a3654854fd25", ...};
    var videos = [{"url":"\\/photos\\/gid\\/00001.mp4","filename":"00001.mp4","filesize":"64M"}, ...];

`<title>` 带站点尾巴(分类 + 站名), 默认按 `" - "` 截取第一段作目录名。
`objId` 用来**确认拿到的确实是这个相册的页面**(不是登录页/拦截页) ——
命名错比不命名更糟, 所以宁可校验失败后退回图集 ID。

`videos[].filesize` 实测**精确可信**(2026-09-18 交叉核对):
`"64M"` ↔ `Content-Range: bytes 0-0/67341834`、`"105M"` ↔ 109900697 字节。
单位是 MiB 取整, 误差 <1MiB —— 用来做"视频体积过滤/预告"足够, 而且**零请求**
(比再发一次 HEAD 探测更快更稳)。注意这只是**视频**的体积; 图片的尺寸档位
没有等价信息, 只能靠 `probe()` 的 Content-Length。

`var videos` 与 `12P + 4V` 都**不作为资源清单** —— 页面结构会变、自报值可能
滞后, 序号枚举才是稳定路径。它们只用来做判定与预告。
"""

import json
import re
import threading
import time
from html import unescape

from core.config import settings
from core.filters import parse_size

# 元信息缓存时长(秒)。同一相册反复采时不必反复开浏览器;
# 只缓存**成功**结果 —— 失败缓存会把一次网络抖动固化成永久降级。
META_TTL = 3600.0

_CACHE = {}
_LOCK = threading.Lock()

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RE = re.compile(
    r'<h1[^>]*class="[^"]*hero-title[^"]*"[^>]*>(.*?)</h1>', re.S | re.I
)
_VIDEOS_RE = re.compile(r"var\s+videos\s*=\s*(\[.*?\])", re.S)
_DOMAIN_RE = re.compile(r'var\s+domain\s*=\s*"([^"]+)"')
_OBJ_ID_RE = re.compile(r'"objId"\s*:\s*"([0-9A-Za-z_-]+)"')
_TAG_RE = re.compile(r"<[^>]+>")

# 站点在相册页里自报的资源数量, 形如 `12P + 4V`(12 张图 + 4 段视频)。
# 定位靠图标锚定(fa-image / fa-video-camera), 不靠 div 顺序 —— 顺序会变。
# ⚠️ 它是**站点自报值**, 可能与实际枚举结果有出入, 只用于"创建前预告"与对照,
# 绝不用它当资源清单(序号枚举才是权威)。
_COUNT_RE = re.compile(
    r'fa-image[^>]*>\s*</i>\s*</div>\s*<div class="text">\s*(\d+)\s*P'
    r"(?:\s*\+\s*(\d+)\s*V)?\s*</div>",
    re.S | re.I,
)
# 厂牌 / 制作方(如 "FENDSON"), 图标 fa-file
_MAKER_RE = re.compile(
    r'fa-file[^>]*>\s*</i>\s*</div>\s*<div class="text">\s*(.*?)\s*</div>',
    re.S | re.I,
)
# 标签块: 整段取出来再逐个抽 <div class="tag">, 避免误抓页面别处的同名 class
# `\s*` 而非紧贴: 真实页面是 `</div></div>`, 但换行/缩进一变就会整块抓不到
_TAGLIST_RE = re.compile(r'class="[^"]*tags-line[^"]*"(.*?)</div>\s*</div>', re.S | re.I)
_TAG_ITEM_RE = re.compile(r'<div class="tag">\s*([^<]{1,40}?)\s*</div>', re.I)

# Cloudflare 拦截页的特征: 命中说明拿到的不是真实页面
_CHALLENGE_HINTS = (
    "just a moment", "attention required", "checking your browser",
    "cf-chl", "enable javascript and cookies",
)
# 标题层面同样要挡: 拦截页 / 登录页的标题都不能当相册名用。
# "Loading <url>" 是 Chrome 导航中的占位标题, 同样不能当页面就绪的信号。
_BAD_TITLE_RE = re.compile(
    r"just a moment|attention required|checking your browser|cf-chl"
    r"|^\s*(loading|登录|登陆|注册)|login|sign\s*in",
    re.I,
)

# 相册页正文里的"我确实是个相册页"标记(任取其一)。
# 用于挡掉挑战页 / 登录页 / 导航中的半成品页面。
_READY_MARKERS = ("photo-items", "hero-title-item", "var videos", '"objId"')

DEFAULT_TITLE_SPLIT = r"\s+-\s+"


def _text(raw):
    """去掉标签、还原实体、压缩空白。"""
    if not raw:
        return ""
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", raw))).strip()


def _absolutize(url, base):
    if not url:
        return ""
    url = url.replace("\\/", "/")
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/") and base:
        return base.rstrip("/") + url
    return url


def _is_challenge(html):
    low = (html or "").lower()
    return any(h in low for h in _CHALLENGE_HINTS)


def extract_album_meta(html, split=DEFAULT_TITLE_SPLIT):
    """从相册页 HTML 解析元信息(纯函数, 便于离线测试)。

    返回::

        {
          "title":  完整 <title> 文本,
          "album":  去掉站点尾巴后的相册名(默认命名用它),
          "h1":     页面主标题(往往比 <title> 更全, 如带 "(FENDSON)"),
          "obj_id": 页面自报的对象 ID(应与图集 ID 一致, 用于校验),
          "videos": [{"url": 绝对 URL, "size": 字节数或 None}, ...],
          "photos": 站点自报图片数(页面上的 "12P"), 没有则 None,
          "videos_declared": 站点自报视频数("12P + 4V" 里的 4),
          "maker":  厂牌/制作方(页面上的 "FENDSON"),
          "tags":   标签列表(["丝袜", "情趣内衣", ...]),
          "challenge": 是否拿到 Cloudflare 拦截页,
        }

    ⚠️ `photos` / `videos_declared` 是**站点自报值**, 只用于"创建前预告"与交叉
    对照, 绝不当资源清单 —— 序号枚举才是权威(页面改版/滞后都可能对不上)。
    `videos[].size` 则实测精确(见下), 可直接用于体积过滤。

    页面结构不符合预期时**不猜**: 相应字段留空, 由调用方回退图集 ID。
    """
    html = html or ""
    tm = _TITLE_RE.search(html)
    title = _text(tm.group(1)) if tm else ""
    h1m = _H1_RE.search(html)
    h1 = _text(h1m.group(1)) if h1m else ""

    dm = _DOMAIN_RE.search(html)
    base = dm.group(1).strip() if dm else ""
    om = _OBJ_ID_RE.search(html)
    obj_id = om.group(1) if om else ""

    # 站点自报数量: "12P + 4V" -> photos=12, videos=4(可能没有 "+nV" 段)
    photos = videos_declared = None
    cm = _COUNT_RE.search(html)
    if cm:
        photos = int(cm.group(1))
        if cm.group(2):
            videos_declared = int(cm.group(2))

    mm = _MAKER_RE.search(html)
    maker = _text(mm.group(1)) if mm else ""

    tags = []
    tm2 = _TAGLIST_RE.search(html)
    if tm2:
        for name in _TAG_ITEM_RE.findall(tm2.group(1)):
            name = _text(name)
            if name and name not in tags:
                tags.append(name)

    videos = []
    vm = _VIDEOS_RE.search(html)
    if vm:
        try:
            raw = json.loads(vm.group(1))
        except ValueError:
            raw = []
        if isinstance(raw, list):
            for it in raw:
                if not isinstance(it, dict):
                    continue
                u = _absolutize(it.get("url") or "", base)
                if not u:
                    continue
                videos.append({"url": u, "size": parse_size(it.get("filesize"))})

    # 相册名: 截掉 " - 分类 - 站名" 这类尾巴。截完为空就整体退回原标题。
    album = title
    if title and split:
        parts = re.split(split, title, maxsplit=1)
        if parts and parts[0].strip():
            album = parts[0].strip()

    return {
        "title": title,
        "album": album,
        "h1": h1,
        "obj_id": obj_id,
        "videos": videos,
        "photos": photos,               # 站点自报图片数(可能为 None)
        "videos_declared": videos_declared,  # 站点自报视频数
        "maker": maker,                 # 厂牌/制作方
        "tags": tags,                   # 标签列表
        "challenge": _is_challenge(html),
    }


def _load_html(url, log=None, storage_state=None, timeout_ms=30000,
               wait_ms=25000, step_ms=800):
    """用 headless Chromium 打开页面, 等 Cloudflare 挑战解开后取 HTML。

    ⚠️ 判定"页面好了没"不能用标题, 只能看**正文里有没有相册页的标记**。
    实测时序(2026-09-18, 匿名访问)::

        0.9s  goto 返回 403, title='Just a moment...'   <- 挑战页
        1.0s  跳回原 URL 重新加载
        8.0s  title='Loading https://xchina.co/...'     <- 导航中的占位标题!
        8.x   page.content() 抛 "page is navigating and changing the content"
        9.x   title 才是真实相册标题

    也就是说: 按标题判定会在 8.0s 就误判成"已就绪", 紧接着 content() 直接抛异常。
    所以这里轮询 `page.content()` 本身(异常 = 还在导航, 下一轮再来),
    直到正文包含 `<title>` 且出现相册页标记(photo-items / hero-title-item /
    var videos / objId)。这样同时挡掉挑战页、登录页与"导航中"三种半成品。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # 没装 playwright: 退化成"用图集 ID 命名"
        if log:
            log(f"未安装 Playwright({type(e).__name__}), 跳过相册页读取")
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = {"user_agent": settings.user_agent}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        if settings.proxy:
            ctx_kwargs["proxy"] = {"server": settings.proxy}
        context = browser.new_context(**ctx_kwargs)
        try:
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:
                # 挑战页本身就是以 403 返回的, goto 抛错不代表失败, 继续轮询正文
                if log:
                    log(f"打开相册页时收到错误({type(e).__name__}: {e}), 继续等挑战解开")

            waited = 0
            html = ""
            while waited < wait_ms:
                try:
                    html = page.content()
                except Exception:
                    html = ""      # 正在导航: 执行上下文已销毁, 下一轮再读
                if html and _looks_ready(html):
                    return html
                page.wait_for_timeout(step_ms)
                waited += step_ms
            if log:
                log(f"相册页 {wait_ms}ms 内未就绪"
                    f"({'仍是 Cloudflare 拦截页' if _is_challenge(html) else '未出现相册页标记'})")
            return None
        finally:
            try:
                context.close()
            finally:
                browser.close()


def _looks_ready(html):
    """正文是否已经是一个可用的相册页(挡掉挑战页/登录页/"导航中")。"""
    if not html or _is_challenge(html):
        return False
    if not _TITLE_RE.search(html):
        return False
    return any(m in html for m in _READY_MARKERS)


def _storage_state(url):
    """已保存的登录态文件路径(付费站点需要), 不存在返回 None。"""
    try:
        from urllib.parse import urlparse

        domain = urlparse(url).netloc
        p = settings.browser_state_dir / f"{domain}.json"
        return str(p) if p.exists() else None
    except Exception:
        return None


def _validate(meta, gid):
    """校验抓到的页面确实是这个相册页; 返回 None 表示通过, 否则返回原因。"""
    if not meta:
        return "未取到页面"
    if meta.get("challenge"):
        return "拿到的是 Cloudflare 拦截页"
    if not meta.get("title"):
        return "页面没有 <title>"
    if _BAD_TITLE_RE.search(meta["title"]):
        return f"标题像登录/拦截页: {meta['title'][:40]!r}"
    # 页面自报的 objId 与图集 ID 对不上 -> 这不是我们要的相册页, 宁可不命名
    if gid and meta.get("obj_id") and meta["obj_id"] != gid:
        return f"页面 objId={meta['obj_id']} 与图集 {gid} 不一致"
    return None


def fetch_album_meta(url, split=DEFAULT_TITLE_SPLIT, log=None, use_cache=True,
                     gid=None):
    """取相册页元信息; 任何一步失败都返回 None(调用方回退图集 ID 命名)。

    先匿名读, 不合格再带已保存的登录态重试 —— 顺序不能反:
    陈旧的 `cf_clearance` 会让 Cloudflare 直接回 `Attention Required!`(见模块文档)。
    """
    if not url:
        return None
    split = DEFAULT_TITLE_SPLIT if split is None else split

    if use_cache:
        with _LOCK:
            hit = _CACHE.get(url)
        if hit and time.time() - hit[0] < META_TTL:
            return hit[1]

    attempts = [None]
    state = _storage_state(url)
    if state:
        attempts.append(state)

    for i, state in enumerate(attempts):
        html = _load_html(url, log=log, storage_state=state)
        meta = extract_album_meta(html, split=split) if html else None
        reason = _validate(meta, gid)
        if reason is None:
            if i and log:
                log("相册页: 匿名读取不合格, 带已保存登录态重试成功")
            if use_cache:
                with _LOCK:
                    _CACHE[url] = (time.time(), meta)
            return meta
        more = i + 1 < len(attempts)
        if log:
            log(f"相册页读取无效({reason})" + (", 改用已保存的登录态重试" if more
                                              else ", 用图集 ID 命名"))
    return None


def clear_cache():
    """清空缓存(测试用)。"""
    with _LOCK:
        _CACHE.clear()
