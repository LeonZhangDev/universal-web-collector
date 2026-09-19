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

长期对策(2026-09-19)
====================
这个项目**不投入 Cloudflare 指纹对抗**: 那是一场永远升级的军备竞赛, 而且一旦
靠headless 指纹"打赢", 下次失败往往更莫名其妙。真正的对策是让**采集不依赖
那个 HTML 页** —— 资源发现永远走纯 HTTP 的序号枚举(见 `gallery_base`),
相册页只提供三样锦上添花的东西: 目录名、自报数量、视频体积线索。拿不到就
降级即用图集 ID 命名, 与"取得了命名"相比只差一点美观。

在此之上补齐三件事, 让降级**省钱、可见、不自伤**:

1. **域级熔断 + 冷却**(`_DOMAIN_STATE`)
   每次读相册页都要开一个 headless Chromium(几百毫秒到几十秒), 而 CF 拦截
   期间每次都注定失败。连续失败 `_CF_TRIP` 次就跳闸, 冷却 `_CF_COOLDOWN`
   秒内**不再开浏览器**, 直接降级并说明原因。这既省时间, 也避免在对方眼里
   "像个打不强退的扫描器" —— 后者更可能招来更严的策略。

2. **陈旧登录态自动隔离**
   带着失效的 `cf_clearance` 访问会直接拿到 `Attention Required!`(永久拒绝,
   不会自愈)。一旦在使用登录态时命中, 就把该域的登录态标记为失效,
   本进程内不再使用, 并明确提示"请重新登录"。

3. **降级原因对用户可见**
   所有降级路径都往 `log` 里写人话; 界面/预览原样显示。用户至少要知道
   "是我被拦了还是站点改版了", 以及下一步该干什么。

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
from urllib.parse import urlparse

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

# 页面里出现的媒体地址。这是本站最硬的一条证据: 页面自己写着资源真实的 CDN
# 子路径(如 img.xchina.io/photos2)与补零宽度(0001), 比任何候选探测都可靠,
# 而且能发现候选清单里根本没有的新子路径(photos4/photos5...)。
# 覆盖常见的懒加载属性名 —— 相册页大量用 data-src, 只看 src 会一个都抓不到。
_MEDIA_URL_RE = re.compile(
    r"""(?:src|data-src|data-original|data-lazy|data-echo|poster)\s*=\s*["']([^"']+)["']""",
    re.I,
)
_SRCSET_RE = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.I)
# 只留"看起来就是媒体"的地址: 站点 logo/图标也满足 src=..., 但它们走的是
# /static/ 之类的路径, 不会与站点声明的资源根前缀匹配。调用方还会再用
# parse_resource_hint 筛一遍, 所以这里宁滥勿缺。
_MEDIA_EXT_RE = re.compile(
    r"\.(?:jpe?g|png|webp|gif|bmp|avif|mp4|m3u8|ts)(?:[?#]|$)", re.I
)

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

_CF_REJECT_HINT = "attention required"  # 只有带着失效登录态访问才会出现

# ---- 域级熔断 / 登录态隔离状态 ----
# host -> {"fails": 连续失败次数, "until": 冷却截止时间戳, "stale_state": 登录态已失效}
_DOMAIN_STATE = {}

#: 连续多少次"仍被拦"就跳闸。取值理由: 单次失败可能只是网络抖动或一次
#: 偶发的挑战加严, 连续 3 次基本可以断定"这一阵压根过不去"。
_CF_TRIP = 3
#: 跳闸后的冷却秒数。10 分钟足够跨过一轮 CF 策略窗口, 又不至于让用户
#: 在这台机器上等到失去耐心 —— 而且期间采集照旧, 只是目录名用图集 ID。
_CF_COOLDOWN = 600.0

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


def _resource_urls(html, base, limit=60):
    """页面里出现的媒体直链(绝对化、去重、限量)。

    为什么要抓它: 相册页的 HTML 里引用了本相册的真实图片地址, 于是**页面自己
    交代了** CDN 子路径与序号补零宽度。用它可以完全跳过候选探测, 而且能发现
    候选清单里没有的新子路径 —— 站点哪天把相册挪到 `photos7`, 这里第一时间就知道。

    只做"绝对值化 + 去重 + 限量", 不做站点相关判断(那是 gallery_base 的事):
    采集器基类知道资源根长什么样, 会拿这些 URL 逐个过 `parse_resource_hint`。
    """
    out, seen = [], set()
    for m in _MEDIA_URL_RE.finditer(html or ""):
        u = _absolutize(m.group(1), base)
        if not u or not _MEDIA_EXT_RE.search(u) or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            return out
    # srcset="a.jpg 1x, b.jpg 2x" —— 取每个候选的第一段(URL)
    for m in _SRCSET_RE.finditer(html or ""):
        for part in (m.group(1) or "").split(","):
            u = _absolutize(part.strip().split(" ")[0], base)
            if not u or not _MEDIA_EXT_RE.search(u) or u in seen:
                continue
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                return out
    return out


def _host_of(url):
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def cooldown_left(url_or_host):
    """这个域还剩多少秒处于熔断冷却中; 0 表示可以正常尝试。"""
    host = url_or_host if "//" not in (url_or_host or "") else _host_of(url_or_host)
    st = _DOMAIN_STATE.get(host)
    if not st:
        return 0
    left = st.get("until", 0) - time.time()
    return max(0, int(left))


def is_stale_state(url_or_host):
    """已保存的登录态是否被判为失效(用例: 提示用户重新登录)。"""
    host = url_or_host if "//" not in (url_or_host or "") else _host_of(url_or_host)
    return bool(_DOMAIN_STATE.get(host, {}).get("stale_state"))


def stale_state_domains():
    """所有已失效的登录态域名, 用于界面提示"请重新登录"。"""
    with _LOCK:
        return [h for h, st in _DOMAIN_STATE.items() if st.get("stale_state")]


def note_success(url):
    """这一次读成功了 —— 连续失败计数清零(失败是"连续"才有意义)。"""
    host = _host_of(url)
    if not host:
        return
    with _LOCK:
        st = _DOMAIN_STATE.setdefault(host, {})
        st["fails"] = 0
        st["until"] = 0


def note_failure(url, html=None, used_state=False):
    """记录一次 Cloudflare 拦截失败, 必要时跳闸。

    返回 dict::{ "cooldown": 冷却秒数(>0 表示已跳闸), "stale_state": 本次判定为
    登录态失效, "fails": 连续失败次数 }。一切都只影响内存状态 —— 磁盘上的
    `browser_state/*.json` 不动, 重新生成之后重启进程即可恢复。
    """
    host = _host_of(url)
    if not host:
        return {"cooldown": 0, "stale_state": False, "fails": 0}
    rejected = _CF_REJECT_HINT in (html or "").lower()
    with _LOCK:
        st = _DOMAIN_STATE.setdefault(host, {})
        st["fails"] = st.get("fails", 0) + 1
        # 只有**带着登录态**访问却被拒绝, 才能断定登录态失效;
        # 匿名访问也一样会拿到拦截页, 不能甩锅给登录态。
        if used_state and rejected:
            st["stale_state"] = True
        if st["fails"] >= _CF_TRIP:
            st["until"] = time.time() + _CF_COOLDOWN
        return {
            "cooldown": max(0, int(st["until"] - time.time())) if st.get("until") else 0,
            "stale_state": bool(st.get("stale_state")),
            "fails": st["fails"],
        }


def clear_domain_state(url_or_host):
    """抹掉某个域的熔断/隔离记录。

    用例: 用户刚重新登录、或删掉了旧登录态 —— 这时候"记得它失效过"反而有害,
    会让人以为重登也没用。删除登录态与完成登录的接口都会调用它。
    """
    host = url_or_host if "//" not in (url_or_host or "") else _host_of(url_or_host)
    if host:
        with _LOCK:
            _DOMAIN_STATE.pop(host, None)


def reset_state():
    """清空熔断/隔离状态(测试与"我重新登录好了"的场景用)。"""
    with _LOCK:
        _DOMAIN_STATE.clear()


def _is_challenge(html):
    low = (html or "").lower()
    return any(h in low for h in _CHALLENGE_HINTS)


def extract_album_meta(html, split=DEFAULT_TITLE_SPLIT, page_url=""):
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
          "resource_urls": 页面里引用的媒体直链(绝对化, 最多 60 条),
          "challenge": 是否拿到 Cloudflare 拦截页,
        }

    ⚠️ `photos` / `videos_declared` 是**站点自报值**, 只用于"创建前预告"与交叉
    对照, 绝不当资源清单 —— 序号枚举才是权威(页面改版/滞后都可能对不上)。
    `videos[].size` 则实测精确(见下), 可直接用于体积过滤。

    `resource_urls` 的用途见 `_resource_urls`: 它是"这站的资源到底放在哪条 CDN
    子路径"最硬的证据, 比候选探测可靠。page_url 用于把相对地址补全(页面里的
    `<img src="/photos2/...">` 要按页面 origin 还原)。

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

    # 页面引用的媒体直链。相对地址按**页面 origin** 补全 —— 站点的 var domain
    # 可能指向 CDN 主机, 拿它当相对路径的 base 会拼出错误地址。
    url_base = ""
    if page_url:
        pu = urlparse(page_url)
        if pu.scheme and pu.netloc:
            url_base = f"{pu.scheme}://{pu.netloc}"
    resource_urls = _resource_urls(html, url_base or base)

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
        "resource_urls": resource_urls,  # 页面里的媒体直链(资源根线索)
        "challenge": _is_challenge(html),
    }


def _load_html(url, log=None, storage_state=None, timeout_ms=30000,
               wait_ms=25000, step_ms=800, markers=None, ready_label="相册页"):
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
                if html and _looks_ready(html, markers):
                    return html
                page.wait_for_timeout(step_ms)
                waited += step_ms
            if log:
                log(f"{ready_label} {wait_ms}ms 内未就绪"
                    f"({'仍是 Cloudflare 拦截页' if _is_challenge(html) else '未出现预期标记'})")
            return None
        finally:
            try:
                context.close()
            finally:
                browser.close()


def _looks_ready(html, markers=None):
    """正文是否已经是一个可用的页面(挡掉挑战页/登录页/"导航中")。

    `markers`: 页面"就绪"的特征串, 命中**任一**即算就绪。默认是相册页的标记;
    聚合/列表页要传自己的(它们不含 `photo-items`), 否则会被判成"永远没就绪",
    白白轮询满 `wait_ms` 才失败。
    """
    if not html or _is_challenge(html):
        return False
    if not _TITLE_RE.search(html):
        return False
    ms = _READY_MARKERS if markers is None else tuple(markers)
    if not ms:
        return True
    return any(m in html for m in ms)


_PW_OK = None


def _playwright_missing():
    """Playwright 是否不可用。结果**永久缓存**: 缺一个依赖这件事在运行期
    不会变(与 ffmpeg 不同, ffmpeg 是可能被用户半路装上的)。"""
    global _PW_OK
    if _PW_OK is None:
        try:
            import playwright.sync_api  # noqa: F401

            _PW_OK = True
        except Exception:
            _PW_OK = False
    return not _PW_OK


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


def _http_get(url, log=None, timeout=25):
    """一次普通 HTTP GET; 非 4xx/5xx 才返回正文, 否则空串。

    为什么值得先试: 实测该站**索引页/落地页是开放的**(`/models.html` 返回 200),
    只有全量列表页才被 Cloudflare 挡。先走这一步, 常见的索引页就**完全不用开
    浏览器** —— 而开一次 headless 要几秒到几十秒, 还会把域推向熔断。
    """
    try:
        import requests
    except Exception:
        return ""
    headers = {
        "User-Agent": settings.user_agent,
        # ⚠️ 该站的 WAF 会校验 Accept: 缺它或写成 `*/*` 直接 403(见项目记忆)。
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    proxies = None
    if settings.proxy:
        proxies = {"http": settings.proxy, "https": settings.proxy}
    try:
        r = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
    except Exception as e:
        if log:
            log(f"纯 HTTP 打开 {url} 失败: {type(e).__name__}: {e}")
        return ""
    if r.status_code >= 400:
        if log:
            log(f"纯 HTTP 返回 {r.status_code}, 该页需要浏览器")
        return ""
    return r.text or ""


def load_page_html(url, markers=None, log=None, http_first=True, ready_label="页面"):
    """通用页面加载: 先纯 HTTP, 拿不到再用 headless 过 Cloudflare。

    返回 `(html, source)`, `source` ∈ {"http", "browser"}; 读不到返回 `(None, None)`。

    与 `fetch_album_meta` **共用同一套域级熔断与登录态隔离**: 相册页刚把某域
    跳闸, 这里也立刻省下那次浏览器 —— 两条链路不会各敲各的(见模块文档"长期对策")。

    `markers`: 页面就绪的特征串(命中任一即可), 必须传**该页真正有的**东西,
    否则会一路轮询到超时才失败。空元组表示只要求"有 title 且不是挑战页"。
    """
    if not url:
        return None, None
    markers = tuple(markers or ())

    if http_first:
        html = _http_get(url, log=log)
        if _looks_ready(html, markers):
            return html, "http"

    left = cooldown_left(url)
    if left > 0:
        if log:
            log(f"该域熔断冷却中(还剩 {left}s), 不再开浏览器, 降级处理")
        return None, None
    # 没装浏览器是**环境问题**, 不能计进 CF 熔断: 否则一次依赖缺失会把
    # 该域拉黑, 之后装好了也读不到(熔断是给"对方在拦我"用的)。
    if _playwright_missing():
        if log:
            log("未安装 Playwright, 无法用浏览器读取该页; 降级处理")
        return None, None

    attempts = [None]
    state = _storage_state(url)
    # 已判失效的登录态别再拿出来招 `Attention Required!`(永久拒绝、不自愈)
    if state and not is_stale_state(url):
        attempts.append(state)

    for i, storage in enumerate(attempts):
        html = _load_html(url, log=log, storage_state=storage,
                          markers=markers, ready_label=ready_label)
        if _looks_ready(html, markers):
            note_success(url)
            if i and log:
                log("匿名读取未就绪, 带已保存登录态重试成功")
            return html, "browser"
        info = note_failure(url, html=html, used_state=bool(storage))
        if storage and info["stale_state"] and log:
            log("带登录态仍被拒(Attention Required!), 已标记该登录态失效; "
                "请到「登录态」区重新登录")
        if info["cooldown"]:
            if log:
                log(f"连续 {info['fails']} 次未就绪, 该域进入 {info['cooldown']}s 冷却")
            break
    return None, None


def fetch_album_meta(url, split=DEFAULT_TITLE_SPLIT, log=None, use_cache=True,
                     gid=None):
    """取相册页元信息; 任何一步失败都返回 None(调用方回退图集 ID 命名)。

    顺序: 先匿名读, 不合格再带已保存的登录态重试 —— 不能反:
    陈旧的 `cf_clearance` 会让 Cloudflare 直接回 `Attention Required!`(见模块文档)。
    已判定失效的登录态会被直接跳过, 不再"明知会拒还要试一遍"。

    失败时按原因分流处理:**Cloudflare 拦截**会计入域级熔断(见本模块"长期对策"),
    并在必要时把登录态标记为失效; **页面结构不符**(比如站点改版)则不计入 ——
    那是另一个问题, 继续开浏览器尝试也救不回来, 但至少不该把采集器拖进冷却。
    """
    if not url:
        return None
    split = DEFAULT_TITLE_SPLIT if split is None else split

    if use_cache:
        with _LOCK:
            hit = _CACHE.get(url)
        if hit and time.time() - hit[0] < META_TTL:
            return hit[1]

    if _playwright_missing():
        if log:
            log("未安装 Playwright, 无法读取相册页; 本次用图集 ID 命名"
                "(资源发现不依赖相册页, 只是少了相册名)")
        return None

    left = cooldown_left(url)
    if left:
        if log:
            log(f"相册页连续取不到(Cloudflare 拦截), {left}s 内不再开浏览器尝试; "
                "本次用图集 ID 命名 —— 图片/视频照常采集")
        return None

    attempts = [None]
    saved = _storage_state(url)
    if saved and not is_stale_state(url):
        attempts.append(saved)
    elif saved and log:
        log("已跳过保存的登录态: 它曾导致 Cloudflare 直接拒绝, 请重新登录后再用")

    for i, state in enumerate(attempts):
        html = _load_html(url, log=log, storage_state=state)
        meta = extract_album_meta(html, split=split, page_url=url) if html else None
        reason = _validate(meta, gid)
        if reason is None:
            note_success(url)
            if i and log:
                log("相册页: 匿名读取不合格, 带已保存登录态重试成功")
            if use_cache:
                with _LOCK:
                    _CACHE[url] = (time.time(), meta)
            return meta

        more = i + 1 < len(attempts)
        tail = ", 改用已保存的登录态重试" if more else ", 用图集 ID 命名"
        # _load_html 只在"没能在限时内拿到可用正文"时返回空 —— 无论具体原因是
        # 挑战没解开还是页面是空的, 结果都一样: 这次白开了浏览器, 计入熔断。
        blocked = (not html) or _is_challenge(html)
        if blocked:
            info = note_failure(url, html, used_state=bool(state))
            bits = [reason]
            if info["stale_state"]:
                bits.append("保存的登录态已失效(带着它访问会被 Cloudflare 永久拒绝),"
                            " 请在登录态管理里重新生成")
            if info["cooldown"]:
                bits.append(f"连续 {info['fails']} 次被拦, "
                            f"未来 {info['cooldown']}s 内不再开浏览器重试")
            if log:
                log("相册页读取无效(%s)%s" % ("; ".join(bits), tail))
        elif log:
            log(f"相册页读取无效({reason}){tail}")
    return None


def clear_cache():
    """清空缓存(测试用)。熔断状态另有 `reset_state()`, 两者互不影响。"""
    with _LOCK:
        _CACHE.clear()
