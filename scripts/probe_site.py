r"""新站点探针 —— 把「接入新站点」的第一步变成一份可复现的实测报告。

为什么需要它
============
`gallery_base` 那套声明式契约已经很稳: 接入一个**序号枚举型图集站**只需写一份
`GallerySite`(见 `collectors/xchina/gallery.py`, 去掉文档注释后声明本体约 70 行),
基类把枚举 / 存在性判定 / 限速 / 镜像降级 / 命名 / 预览全部兜住。接入 pexels 这个
第二个实例时**没有改动 `gallery_base` 任何逻辑** —— 说明契约是可复用的, 不是刚好合身。

所以加一个站的**成本不在写声明**, 而在"写声明之前必须知道的那五件事":

    ① 存不存在怎么判定?        站点返回不返回 404?
    ② 请求头有没有被校验?      尤其 Accept(WAF 常见)
    ③ 要不要登录 / 浏览器?     裸 HTTP 能不能直接拿到资源
    ④ 有没有下载点变体?        尺寸/格式档, 天然的 mirrors
    ⑤ 用户会拿哪几种 URL 进来? 每种形态都能解析出**同一个** ID 吗

这五件事**猜错的代价是不对称的**, 而这正是本项目反复吃过的亏:

- ① 猜错是**静默**的 —— 站点对不存在的图集照样答 `200 text/html`, 于是枚举
  "正常地"采到 0 个资源, 任务还报 success(2026-09-18 真实事故)。
- ② 猜错的表现是"**浏览器能看, 代码全 403**", 极易被误判成防盗链或需要登录。
- ⑤ 猜错会把**页码**当 ID, 去枚举一个不存在的图集 —— 又是"成功但 0 资源"。

而 `scripts/probe.py` 是**浏览器探针**(跑 `BrowserCollector` 看四种解析器发现了
什么), 服务的是通用采集器, 与上面五项无关。这五项此前只能手搓 curl 挨个试。

本脚本把它们跑成报告 + 一份可直接填的 `GallerySite` 草稿。

它自己怎么被验证
================
**拿已知站点当回归靶子。** `collectors/xchina/gallery.py` 的模块文档里那五项结论
是钉死的(越界返回 `200 text/html` / `Accept` 必须显式带 `image/*` / HEAD 可靠 /
资源按相册分到 `photos`、`photos2` / 7 种 URL 形态), 而且每条都写明了日期。
探针跑不出同样的结论, 说明**探针写错了**, 而不是站点变了 ——
这是验证探针自身的唯一办法, 也是本脚本存在的第二个理由。

用法
====
至少给一条**资源直链**(用户右键复制到的那条)。它把基址与序号补零宽度直接写在了
URL 里, 是手上最硬的一份证据; 再给相册页 URL, 就能顺带验"页面可达性"和"页面里
真正出现的变体"(比靠后缀猜测可靠得多)::

    python scripts/probe_site.py \
        https://img.example.com/photos2/69ad45698f836/0001.jpg \
        https://example.com/photo/id-69ad45698f836.html

开关::

    --scan N        同一序号空间内探测前 N 个(默认 6), 用来看序号连不连续
    --over N        用哪个"基本肯定越界"的序号验存在性判定(默认 9000)
    --impersonate X 换成浏览器 TLS 指纹(curl-cffi), 如 chrome; 全 403 时再试
    --proxy URL     走代理
    --out draft.py  把声明草稿写成文件(同时仍打印报告)

⚠️ 本脚本**只探测、不落盘任何资源**。它发的是 HEAD(必要时一次流式 GET 只读响应头),
不会把图片正文读下来。
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from core.config import IMAGE_ACCEPT, VIDEO_ACCEPT, settings  # noqa: E402
from collectors.gallery_base import _head_status_headers  # noqa: E402

#: 视为"资源文件"的扩展名 —— 用来把输入的 URL 分成「资源直链」与「页面 URL」两类。
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "avif", "gif", "bmp", "heic"}
VIDEO_EXT = {"mp4", "m4v", "mov", "webm", "mkv", "ts"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT | {"m3u8", "mpd"}


def accept_for(ext):
    """按媒体类型选 Accept。

    ⚠️ 不能一律用 `image/*`: 有的站点对视频路径只认 `video/*`, 而用错前缀会
    **把每一段真实视频都判成"不存在"**(见 skill `gallery-site-probe` 的"同序号空间
    多媒体相册"一节)。反之亦然。
    """
    return VIDEO_ACCEPT if (ext or "").lstrip(".").lower() in VIDEO_EXT else IMAGE_ACCEPT


def ctype_for(ext):
    """存在性判定用的 Content-Type 前缀 —— 与 Accept 必须成对, 不能混用。"""
    return "video/" if (ext or "").lstrip(".").lower() in VIDEO_EXT else "image/"


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------


def make_session(proxy=None, impersonate=None):
    """建会话; 返回 (session, 传输说明)。

    `impersonate` 走 curl-cffi(浏览器 TLS 指纹)。它在 CI/最小环境里可能**没装** ——
    那是环境问题, 不该让整个探针挂掉: 降级成 requests 并**把降级说出来**,
    否则用户会以为"指纹这条路也试过了"(静默降级必须留痕)。
    """
    if impersonate:
        try:
            from curl_cffi import requests as curl_requests

            s = curl_requests.Session(impersonate=impersonate)
            transport = "curl-cffi (impersonate=%s)" % impersonate
        except Exception as e:
            import requests

            s = requests.Session()
            transport = ("requests —— !! curl-cffi 不可用(%s), 没能用上 %s 指纹"
                         % (type(e).__name__, impersonate))
    else:
        import requests

        s = requests.Session()
        transport = "requests (默认 TLS 指纹)"
    p = proxy if proxy is not None else settings.proxy
    if p:
        s.proxies.update({"http": p, "https": p})
    return s, transport


def head(session, url, headers, timeout=None):
    """取 (status, headers); 拿不到就返回 (None, None)。

    失败**不当成"不存在"** —— 那是本项目最忌讳的混淆(网络抖动会被当成序号越界,
    图集从中间被截断)。调用方必须把 None 单独处理。
    """
    try:
        return _head_status_headers(session, url, headers,
                                    timeout or settings.request_timeout)
    except Exception:
        return None, None


def short(url, n=96):
    return url if len(url) <= n else url[: n - 1] + "…"


def classify(urls):
    """把输入分成 (资源直链, 页面 URL)。按扩展名判, 不看域名。"""
    media, pages = [], []
    for u in urls:
        tail = u.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        ext = tail.rsplit(".", 1)[1].lower() if "." in tail else ""
        (media if ext in MEDIA_EXT else pages).append(u)
    return media, pages


# --------------------------------------------------------------------------
# 从直链里读答案
# --------------------------------------------------------------------------


def infer_from_link(url):
    """从一条资源直链里读出 基址 / gid / 序号宽度 / 后缀。

    直链是手上最可靠的一份证据: `.../photos2/69ad45698f836/0001.jpg` 同时说明了
    CDN 子路径是 `photos2`、gid 在倒数第二段、序号补 4 位零。

    返回的 `seq_format` 为 None 表示**文件名不以数字序号开头** —— 那说明这个站
    根本不是"序号枚举型", `SequenceGallerySpider` 处理不了它(如 pexels 一个 ID
    只有一张图)。这时必须明说, 不能硬套一个模板进去。
    """
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if len(segs) < 2 or "." not in segs[-1]:
        return None
    stem, ext = segs[-1].rsplit(".", 1)
    ext = "." + ext.lower()
    base_path = "/".join(segs[:-2])
    info = {
        "url": url,
        "scheme": p.scheme,
        "host": p.netloc,
        "gid": segs[-2],
        "base": (f"{p.scheme}://{p.netloc}" + (f"/{base_path}" if base_path else "")),
        "base_path": base_path,
        "ext": ext,
        "seq": None,
        "width": 0,
        "seq_format": None,
        "suffix": ext,
    }
    m = re.match(r"^(\d+)", stem)
    if m:
        digits = m.group(1)
        info.update(
            seq=int(digits),
            width=len(digits),
            seq_format="{seq:0%dd}" % len(digits),
            suffix=stem[len(digits):] + ext,
        )
    return info


def split_digit_base(base):
    """把以数字结尾的基址拆开: `.../photos2` -> (`.../photos`, 2)。

    站点按**数字后缀**给相册分桶(`photos`/`photos2`/`photos3`)时, 正确的声明是
    `base=".../photos"` + `base_candidate_digits=N`, 让 `_base_candidates` 自动
    展开候选。写成 `base=".../photos2"` 的话, 别的相册一律判空 —— 而用户只看到
    "任务失败", 完全看不出是路径不对。所以这里主动拆。
    """
    m = re.search(r"(\D)(\d+)$", base or "")
    if not m:
        return base, 0
    return base[: m.start(2)], int(m.group(2))


def gid_regex(gid):
    """从样本 ID 猜一个**形状**正则(用于自动识别时的把关)。

    刻意取"比样本更宽"的下界, 与既有站点一致(xchina 是 13 位 hex 而声明成
    `[0-9a-f]{8,}`, pexels 是 7 位十进制而声明成 `\\d{4,10}`)。理由: 写窄了会把
    真图集挡在自动识别之外, 症状是"落到通用采集器 -> 静默 0 资源"; 写宽了最多和
    别的站点抢认领, 那会走 `ambiguous` 让用户自己选。
    """
    if re.fullmatch(r"\d+", gid):
        return r"\d{4,}"
    if re.fullmatch(r"[0-9a-f]+", gid):
        return r"[0-9a-f]{8,}"
    if re.fullmatch(r"[0-9A-F]+", gid):
        return r"[0-9A-F]{8,}"
    return r"[0-9A-Za-z_-]{6,}"


def link_pattern(base_path, gid_re):
    """直链正则草稿。

    ⚠️ 必须**锚定到"gid 后面紧跟一个带媒体扩展名的文件名"**。xchina 早期只写
    `/photos/(...)`, 于是 `/photos/featured/0001.jpg` 把路径词 `featured` 当成 gid,
    去枚举一个不存在的图集 —— 又是一次"成功但 0 资源"。

    ⚠️ 基址末尾**无条件**补 `\\d*`(只要它不是以数字结尾)。理由是"我采信的是**一条**
    直链, 而站点可能把相册分到 `photos`/`photos2`/`photos3`"。用户恰好粘了 `photos`
    那张, 不代表别的相册也在 `photos` —— 只认默认桶的后果是 `photos2` 上的直链
    落给通用采集器(预览直接报错, 而采集本身明明好使, 是最难查的"一半好一半坏")。
    这一层**必须比用户给的那一条更宽**, 才不至于把站点的分桶能力锁死在一个样本上。
    """
    if not base_path:
        seg = r"[^/?#]*"
    else:
        root = re.sub(r"\d+$", "", base_path)
        seg = re.escape(root) + (r"\d*" if re.search(r"[A-Za-z]$", root) else "")
    exts = "jpe?g|png|webp|avif|gif|bmp|mp4|m4v|mov|webm|mkv|m3u8|mpd|ts"
    return r"/%s/(%s)/[^/?#]+\.(?:%s)(?:[?#]|$)" % (seg, gid_re, exts)


def page_pattern(page_url, gid, gid_re):
    """相册页正则草稿: 保留 gid 所在**路径段**及其**上一段**, gid 换成捕获组。

    ⚠️ 不能用"gid 前 N 个字符"这种固定窗口。踩过: 窗口取 8 而 `/photo/id-` 是
    10 个字符, 于是前缀被截成 `hoto/id-` —— 正则仍能匹配, 但它是从词中间切进去的,
    换一个路径深度就静默失配。必须**按路径段对齐**。

    两种形态都要认(同一站点常常并存)::

        /photo/id-{gid}.html        -> /photo/id-([0-9a-f]{8,})
        /photo/id-{gid}/10.html     -> /photo/([0-9a-f]{8,})
        /photoShow.html?id={gid}    -> [?&]id=([0-9a-f]{8,})
    """
    u = urlparse(page_url)
    segs = [s for s in u.path.split("/") if s]
    for k, seg in enumerate(segs):
        i = seg.find(gid)
        if i < 0:
            continue
        before = seg[:i]                              # 同段内 gid 之前的部分(常是 "id-")
        prev = segs[k - 1] if k > 0 else ""           # 上一段(常是 "photo")
        if not prev and not before:
            continue
        pre = ("/" + prev if prev else "") + "/" + before
        return re.escape(pre) + "(" + gid_re + ")"
    if gid in u.query:
        for pair in u.query.split("&"):
            if "=" not in pair:
                continue
            key, val = pair.split("=", 1)
            if gid in val:
                return r"[?&]" + re.escape(key) + "=" + r"(" + gid_re + ")"
    return None


def page_tail_evidence(page_urls, gid):
    """相册页 URL 的末段是不是**页码**?

    这是 ⑤ 里最阴险的一种: `/photo/id-XXX/10.html` 的 `10` 是第 10 页, 不是 ID。
    早期正则只认图片直链, 相册页匹配不上就走了"取路径末段"的退路 -> 拿到 `10`
    当 gid -> 枚举一个不存在的图集 -> **任务 success 但 0 个资源**。
    """
    bad = []
    for u in page_urls:
        seg = urlparse(u).path.rstrip("/").rsplit("/", 1)[-1]
        stem = seg.rsplit(".", 1)[0] if "." in seg else seg
        if re.fullmatch(r"\d+", stem) and stem != gid:
            bad.append(u)
    return bad


# --------------------------------------------------------------------------
# 五段探测
# --------------------------------------------------------------------------


def probe_existence(session, info, scan, over, timeout):
    """① 存不存在怎么判定 + 序号连不连续。

    对照点是**两端的极值**, 不是中间某一张:

    - 存在的序号(给定直链那个) —— 拿到"存在长什么样"
    - 一个基本肯定越界的序号(默认 9000) —— 拿到"不存在长什么样"

    两者一比, 判定规则就定了。若越界时站点**仍返回 200**, 结论只能是"按
    Content-Type 判定"—— 按状态码判定会让枚举永不停止(或第一张就误停)。
    """
    print("== ① 存在性判定 ==")
    headers = {"User-Agent": settings.user_agent, "Accept": accept_for(info["ext"])}
    fmt = info["seq_format"] or "{seq:05d}"
    prefix = ctype_for(info["ext"])

    def url_at(n):
        return f"{info['base']}/{info['gid']}/{fmt.format(seq=n)}{info['suffix']}"

    rows = []
    for n in range(1, scan + 1):
        status, hdrs = head(session, url_at(n), headers, timeout)
        ct = (hdrs.get("Content-Type") or "").lower() if hdrs else ""
        cl = hdrs.get("Content-Length") if hdrs else None
        ok = status == 200 and ct.startswith(prefix)
        rows.append((n, status, ct, cl, ok))
        print("  %s seq %8s  %-5s %-28s %s"
              % ("ok " if ok else "!! ", fmt.format(seq=n), status,
                 ct or "(无 Content-Type)", cl or "-"))

    o_status, o_hdrs = head(session, url_at(over), headers, timeout)
    o_ct = (o_hdrs.get("Content-Type") or "").lower() if o_hdrs else ""
    print("  --- 越界对照(seq %d) ---" % over)
    print("  !! seq %8d  %-5s %s" % (over, o_status, o_ct or "(无 Content-Type)"))

    hits = [r for r in rows if r[4]]
    blocked_all = bool(rows) and all(r[1] in (401, 403) for r in rows)
    # 「空洞」= 缺失之后**还有**命中。末尾那几个缺失不算 —— 那只是相册到这儿就完了
    # (`--scan 6` 探一个 5 张的相册必然触发), 报出来只会制造噪音, 让人开始忽略这一行。
    holes = [rows[i][0] for i, r in enumerate(rows)
             if not r[4] and any(x[4] for x in rows[i + 1:])]

    if not hits:
        # ⚠️ 没有"存在"的样本, 就不存在"对照" —— 此时任何关于判定规则的结论都是编的。
        # 本项目最忌讳的就是这类"看着有结论、其实没证据"的输出: 越界返回 403 会被
        # 误读成"状态码可用", 而真相是**整个站都进不去**。
        verdict = ("本次**一个存在的序号都没探到**, 判定规则**还没验出来** —— "
                   "不要照抄下面的结论。先让第一条直链能通, 再回来跑。")
    elif o_status is None:
        verdict = "越界探测**失败**(网络/超时) —— 判定规则没验出来, 重跑一次再下结论。"
    elif o_status == 200 and not o_ct.startswith(prefix):
        verdict = ("越界仍返回 200 但类型不是 %s* —— **只能用 Content-Type 判定存在性, "
                   "绝不能用状态码**(按状态码判定会让枚举永不停止)。" % prefix)
    elif o_status == 200 and o_ct.startswith(prefix):
        verdict = ("越界**也**返回 %s* —— 存在性无法按内容判定。上界只能来自页面"
                   "自报数量, 或给用户一个硬上限。这是最麻烦的一种形态。" % prefix)
    elif 400 <= o_status < 500:
        verdict = ("越界返回 %s —— 状态码可用, 但仍建议按 Content-Type 判定(更稳: "
                   "有的 CDN 对不同层走不同规则)。" % o_status)
    else:
        verdict = "越界返回 %s/%s —— 需要人工看一眼再定规则。" % (o_status,
                                                              o_ct or "?")
    print("  => %s" % verdict)
    if blocked_all:
        print("  => 全部 %s: 先解决**可达性**, 再谈判定规则。两条常见路 —— "
              "① 站点在 Cloudflare 后面(比对浏览器 TLS 指纹), 试 `--impersonate chrome`;"
              " ② 加 `--proxy`。" % rows[0][1])
    if holes:
        print("  => !! 中间有空洞(缺失之后**又**有命中): seq %s —— 序号**不连续**。"
              "此时**绝不能**启用指数探上界: 它假定序号连续, 会把上界定在缺口之前, "
              "结果是静默少采。用线性扫描。" % holes)
    print("  => 本次命中 %d/%d" % (len(hits), scan))
    return verdict, rows


def probe_accept(session, info, timeout):
    """② 请求头有没有被校验(尤其 Accept)。

    对照法是"固定 UA, 只改一个头, 逐个隔离" —— 一次只变一个变量, 才说得清是谁挡的。
    """
    print("\n== ② 请求头校验(Accept 对照) ==")
    fmt = info["seq_format"] or "{seq:05d}"
    url = f"{info['base']}/{info['gid']}/{fmt.format(seq=1)}{info['suffix']}"
    want = accept_for(info["ext"])
    variants = [
        ("不带 Accept", {"User-Agent": settings.user_agent}),
        ("Accept: */*", {"User-Agent": settings.user_agent, "Accept": "*/*"}),
        ("Accept: " + want[:30] + "…",
         {"User-Agent": settings.user_agent, "Accept": want}),
    ]
    got = []
    for label, hdrs in variants:
        status, _h = head(session, url, hdrs, timeout)
        got.append((label, status))
        print("  %-38s -> %s" % (label, status))

    ok_n = sum(1 for _l, s in got if s == 200)
    blocked_n = sum(1 for _l, s in got if s in (401, 403, 406))
    if blocked_n and ok_n:
        print("  => **Accept 被校验**: 有的一律 403、有的 200。必须显式声明 "
              "`image/*`(常量在 `core.config.IMAGE_ACCEPT`, 基类会统一带上)。")
    elif ok_n == len(got):
        print("  => Accept 未被校验(三种都给 200)。仍保留 IMAGE_ACCEPT —— 这是"
              "**站点级**差异, 别据此推断“Accept 无关紧要”。")
    elif not ok_n:
        print("  => 三种全不通 —— 大概率不是 Accept 的问题。先解决可达性: 站点在 "
              "Cloudflare 后面就试 `--impersonate chrome`(浏览器 TLS 指纹), 或加 `--proxy`。")
    else:
        print("  => 结论不明确, 人工看一眼上面的状态码。")
    return got


def probe_page(session, page_urls, timeout):
    """③ 页面可达性 / 要不要浏览器, 并顺手收割**页面里真正出现的**资源直链。

    页面能拿到就赚两份证据: 页面自己写着的 CDN 前缀(比用户那一条直链更能代表全体),
    以及页面里出现的尺寸/格式变体。
    """
    print("\n== ③ 页面可达性与资源线索 ==")
    if not page_urls:
        print("  (没给页面 URL, 跳过。给一条相册页 URL 能顺带验变体与可达性)")
        return [], []
    headers = {"User-Agent": settings.user_agent,
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    found_bases, suffix_hits = [], {}
    for u in page_urls:
        try:
            r = session.get(u, headers=headers, timeout=timeout, allow_redirects=True)
            status, body = r.status_code, r.text or ""
        except Exception as e:
            print("  %s -> 请求异常 %s" % (short(u, 58), type(e).__name__))
            continue
        print("  %s -> %s  %d 字节" % (short(u, 58), status, len(body)))
        low = body[:6000].lower()
        if status in (401, 403, 503) or "challenge" in low or "just a moment" in low:
            print("     => 页面走不动(403 / Cloudflare)。**这不影响采集** —— 资源发现走"
                  "纯 HTTP 序号枚举, 页面只用来取标题与自报数量。别为此把采集器"
                  "整个搬到浏览器上。")
        # 两种写法都要收: ① 直接写在页面里的**绝对** URL(xchina 就是这种,
        # 资源在 img.* 上而页面在 www.*); ② 属性值里的**相对/根相对**路径
        # (`src="/photos/..."`) —— 只收绝对 URL 的话, 用相对路径的站点这一节
        # 会静默变成"页面里没有任何资源线索", 而那和"页面确实没有"长得一样。
        cands = set()
        for m in re.finditer(r"""["'](https?://[^"'<>\\\s]+|/[^"'<>\\\s]*)["']""", body):
            cands.add(urljoin(u, m.group(1)))
        for m in re.finditer(r"https?://[^\s\"'<>\\]+", body):
            cands.add(m.group(0))
        for cand in cands:
            p = urlparse(cand)
            segs = [s for s in p.path.split("/") if s]
            if len(segs) < 3 or "." not in segs[-1]:
                continue
            fext = segs[-1].rsplit(".", 1)[1].lower()
            if fext not in MEDIA_EXT:
                continue
            base = f"{p.scheme}://{p.netloc}/" + "/".join(segs[:-2])
            if base not in found_bases:
                found_bases.append(base)
            stem = segs[-1].rsplit(".", 1)[0]
            m2 = re.match(r"^(\d+)", stem)
            if m2:
                suf = stem[len(m2.group(1)):] + "." + fext
                suffix_hits[suf] = suffix_hits.get(suf, 0) + 1
    if found_bases:
        print("  页面里出现的资源基址(站点自己写的, 最可信):")
        for b in found_bases[:8]:
            print("      " + b)
    if suffix_hits:
        print("  页面里出现的序号后缀(命中次数):")
        for s, n in sorted(suffix_hits.items(), key=lambda kv: -kv[1])[:12]:
            print("      %r  ×%d" % (s, n))
    return found_bases, [{"suffix": s, "hits": n} for s, n in
                         sorted(suffix_hits.items(), key=lambda kv: -kv[1])]


def probe_variants(session, info, timeout, page_suffixes):
    """④ 下载点 / 尺寸变体。

    ⚠️ 这一项是**试出来**的, 不是猜出来的 —— 只有真的返回 `200 + 媒体类型` 才算档位。
    尺寸档不只是备用降级, **更是省钱手段**: 1200 宽的 webp 肉眼与原图几无差别,
    却省约 85% 的存储与带宽。
    """
    print("\n== ④ 下载点 / 尺寸变体 ==")
    fmt = info["seq_format"] or "{seq:05d}"
    seq = info["seq"] or 1
    want = accept_for(info["ext"])
    prefix = ctype_for(info["ext"])

    candidates, seen = [], set()

    def add(suffix, src):
        if suffix not in seen:
            seen.add(suffix)
            candidates.append((suffix, src))

    add(info["suffix"], "来自你给的直链")
    for item in page_suffixes or []:
        add(item["suffix"], "来自页面")
    for suf in ("_1200x0.webp", "_1000x0.webp", "_800x0.webp", "_600x0.webp",
                "_thumb.webp", ".webp", ".jpeg", "?w=1200",
                "?auto=compress&w=1200"):
        add(suf, "候选(试出来的)")

    headers = {"User-Agent": settings.user_agent, "Accept": want}
    hits = []
    for suffix, src in candidates:
        url = f"{info['base']}/{info['gid']}/{fmt.format(seq=seq)}{suffix}"
        status, hdrs = head(session, url, headers, timeout)
        ct = (hdrs.get("Content-Type") or "").lower() if hdrs else ""
        cl = hdrs.get("Content-Length") if hdrs else None
        ok = status == 200 and ct.startswith(prefix)
        if ok:
            hits.append({"suffix": suffix, "size": cl, "src": src})
        print("  %s %-26r %-5s %-18s %s   [%s]"
              % ("ok " if ok else "   ", suffix, status, ct or "-", cl or "-", src))

    if not any(h["suffix"] == info["suffix"] for h in hits):
        print("  !! 你给的那条直链**本身没探通** —— 它可能已失效(签名过期/图集删了),")
        print("     也可能需要代理/浏览器。先解决这条, 否则后面所有结论都不可靠。")
    if len(hits) <= 1:
        print("  => 只探到一条线路, `mirrors` 会是空的(该资源没有备用下载点)。")
    else:
        print("  => %d 条线路。按画质从高到低写进 `variants`, 主档之外自动成为 "
              "`mirrors`; `quality_map` 把档位名映射到后缀。" % len(hits))
    print("  => 注意: 不同序号可能**缺档**。基类会在缺档时回退最高画质档而不是判"
          "“这张不存在” —— 别自己再实现一遍这层回退。")
    return hits


def probe_url_forms(all_urls, media, pages, info):
    """⑤ 每一种输入形态都能解析出同一个 ID 吗 + 正则与样本草稿。

    报告里同时给**草稿**和**自检结果**: 把草稿正则挨个套回用户给的 URL, 看能否
    抽出一致的 gid。这比"看着像对"可靠 —— 而这正是 `check_site()` 要守的那件事。
    """
    print("\n== ⑤ URL 形态与 ID 解析 ==")
    gid = info["gid"]
    gid_re = gid_regex(gid)
    patterns = [("直链", link_pattern(info["base_path"], gid_re))]
    for u in pages:
        pp = page_pattern(u, gid, gid_re)
        if pp:
            patterns.append(("相册页", pp))

    for kind, pat in patterns:
        hit = sum(1 for u in all_urls if re.search(pat, u))
        print("  %-6s %s" % (kind, pat))
        print("         命中 %d/%d 条输入" % (hit, len(all_urls)))

    bad_tail = page_tail_evidence(pages, gid)
    if bad_tail:
        print("  !! 末段是纯数字(页码, 不是 ID): %s"
              % [short(u, 46) for u in bad_tail])
        print("     => 必须声明 page_tail=r\"^\\d+$\"。否则解析失败时会退回"
              "“取路径末段”, 把页码当 ID -> 枚举不存在的图集 -> 成功但 0 资源。")

    ok, fails = 0, []
    for u in all_urls:
        got = None
        for _kind, pat in patterns:
            m = re.search(pat, u)
            if m and m.group(1):
                got = m.group(1)
                break
        if got == gid:
            ok += 1
        else:
            fails.append((u, got))
    print("  自检: %d/%d 条输入解析出一致的 gid (%s)" % (ok, len(all_urls), gid))
    for u, got in fails:
        print("      !! %s -> %r" % (short(u, 58), got))
    if fails:
        print("     => 有形态没覆盖。补一条 pattern, 或把该形态加进 `id_samples` 后重跑。")
    if len(pages) < 2:
        print("  (只给了 %d 条页面 URL。样本要覆盖**每一种**形态: 直链/相册页/分页/"
              "老式 query/裸 ID —— 少一种, 那种形态回归时就没有护栏。)" % len(pages))

    for kind, pat in patterns:
        for u in all_urls:
            m = re.search(pat, u)
            if m and m.group(1) and m.group(1) != gid:
                print("  !! `%s` 正则单独匹配 %s 得到 %r, 与期望冲突 —— 应修正则, "
                      "而不是靠 pattern 顺序。" % (kind, short(u, 46), m.group(1)))
    return patterns, gid_re, bad_tail


# --------------------------------------------------------------------------
# 草稿
# --------------------------------------------------------------------------


def site_name(host):
    """从域名猜一个站点名(`img.xchina.io` -> `xchina_gallery`)。

    IP / 纯数字主机名(如 `127.0.0.1`)取不出有意义的标签 —— 与其生成
    `0_gallery` 这种谁也看不懂的名字, 不如退回 `site_gallery`。
    """
    labels = [x for x in (host or "").split(".") if x]
    pick = labels[-2] if len(labels) >= 2 else ""
    if not re.search(r"[a-z]", pick.lower()):
        pick = next((c for c in labels if re.search(r"[a-z]", c.lower())), "")
    pick = re.sub(r"[^0-9a-z]+", "_", pick.lower()).strip("_")
    return (pick or "site") + "_gallery"


def quality_key(suffix, i):
    """从后缀里给一个画质档名:`_1200x0.webp` -> `1200`, `?w=800` -> `800`。"""
    m = re.match(r"_(\d+)x\d+", suffix or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]w=(\d+)", suffix or "")
    if m:
        return m.group(1)
    return "original" if i == 0 else ("alt%d" % i)


def build_draft(info, media, pages, hits, patterns, gid_re, bad_tail, page_bases):
    """拼一份 `GallerySite` 草稿。带 `# TODO` 的地方是**必须人工确认**的。"""
    name = site_name(info["host"])
    base, digits = split_digit_base(info["base"])
    others = [h["suffix"] for h in hits if h["suffix"] != info["suffix"]]
    variants = ([info["suffix"]] if any(h["suffix"] == info["suffix"] for h in hits)
                else []) + others
    quality_map = {quality_key(s, i): s for i, s in enumerate(variants)}
    if variants:
        quality_map = {"original" if k == quality_key(variants[0], 0) else k: v
                       for k, v in quality_map.items()}

    extra = [b for b in (page_bases or []) if b != info["base"]]
    if extra and not digits:
        for b in extra:
            _bb, d = split_digit_base(b)
            digits = max(digits, d)

    L = ["# 由 scripts/probe_site.py 生成 —— 每条都对应报告里的一条实测证据。",
         "# 标 TODO 的地方必须人工确认, 不要直接提交。", "",
         "from .. import register",
         "from ..gallery_base import GallerySite, SequenceGallerySpider", ""]
    L.append("%s = GallerySite(" % name.upper())
    L.append('    name="%s",' % name)
    L.append('    base="%s",' % base)
    if digits:
        L.append("    base_candidate_digits=%d,   # 实测: 直链出现在 %r 上 -> 站点按数字后缀分桶"
                 % (digits, info["base_path"]))
    else:
        L.append("    # base_candidate_digits=0,   # TODO 若站点也按 photos/photos2 分桶就调大")
    if variants:
        L.append("    # 画质从高到低")
        L.append("    variants=%r," % (variants,))
        L.append("    quality_map=%r," % (quality_map,))
    else:
        L.append("    variants=[],   # TODO 没探到任何档位, 确认直链是否有效")
    L.append('    seq_format="%s",' % (info["seq_format"] or "{seq:05d}"))
    L.append('    gid_shape=r"%s",   # TODO 从 1 个样本推断, 确认 ID 的长度范围' % gid_re)
    if bad_tail:
        L.append('    page_tail=r"^\\d+$",   # 实测: 相册页末段是页码')
    L.append("    id_patterns=[")
    for kind, pat in patterns:
        L.append('        r"%s",   # %s' % (pat, kind))
    L.append("    ],")
    L.append("    id_samples=[")
    L.append('        ("%s", "%s"),' % (info["gid"], info["gid"]))
    for u in media[:3]:
        L.append('        ("%s", "%s"),   # 直链' % (u, info["gid"]))
    for u in pages[:3]:
        L.append('        ("%s", "%s"),   # 相册页' % (u, info["gid"]))
    L.append("        # TODO 每种输入形态都要有一条: 直链 / 相册页 / 分页 / 老式 query / 裸 ID")
    L.append("    ],")
    L.append("    input_forms=[")
    for kind, _pat in patterns:
        L.append('        "%s",   # TODO 补成用户看得懂的说明' % kind)
    L.append("    ],")
    if pages:
        L.append('    album_url_template="%s",' % pages[0].replace(info["gid"], "{gid}"))
    L.append(")")
    L.append("")
    L.append("")
    L.append('@register("%s")' % name)
    L.append("class %sSpider(SequenceGallerySpider):" % "".join(
        w.capitalize() for w in name.split("_")))
    L.append('    """%s -> 该图集下的全部资源。"""' % info["host"])
    L.append("")
    L.append("    site = %s" % name.upper())
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="新站点探针: 五项实测 + GallerySite 草稿")
    ap.add_argument("urls", nargs="*", help="资源直链(必给 ≥1) + 相册页 URL")
    ap.add_argument("--scan", type=int, default=6, help="探测前 N 个序号(默认 6)")
    ap.add_argument("--over", type=int, default=9000, help="越界对照序号(默认 9000)")
    ap.add_argument("--impersonate", default="", help="curl-cffi 指纹, 如 chrome")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--out", default="", help="把草稿写成文件")
    args = ap.parse_args()

    if not args.urls:
        print(__doc__)
        sys.exit(1)

    media, pages = classify(args.urls)
    if not media:
        print("!! 至少需要一条**资源直链**(右键复制到的那条 .jpg/.mp4 链接)。")
        print("   它把基址与序号宽度直接写在 URL 里, 是唯一可靠的证据源;")
        print("   只给页面 URL 的话, 下面每一项都会退化成猜测 —— 而猜错是静默的。")
        sys.exit(1)

    info = None
    for u in media:
        cand = infer_from_link(u)
        if cand and cand["seq_format"]:
            info = cand
            break
    if info is None:
        print("!! 给的直链里文件名**不以数字序号开头** —— 这个站不是「序号枚举型」,")
        print("   本脚本(以及 SequenceGallerySpider)不适用。例如 pexels 一个 ID 只有")
        print("   一张图(pexels-photo-12345.jpeg), 它的资源来自页面/API 解析。")
        sys.exit(2)

    session, transport = make_session(args.proxy, args.impersonate or None)

    print("=" * 74)
    print("新站点探针 (只探测, 不落盘)")
    print("=" * 74)
    print("传输      : %s" % transport)
    print("资源直链  : %d 条, 采信 %s" % (len(media), short(info["url"], 66)))
    print("页面 URL  : %d 条" % len(pages))
    print("推断结果  : base=%s  gid=%s  序号=%s  后缀=%r"
          % (info["base"], info["gid"], info["seq_format"], info["suffix"]))
    print()

    probe_existence(session, info, args.scan, args.over, args.timeout)
    probe_accept(session, info, args.timeout)
    page_bases, page_suffixes = probe_page(session, pages, args.timeout)
    hits = probe_variants(session, info, args.timeout, page_suffixes)
    patterns, gid_re, bad_tail = probe_url_forms(args.urls, media, pages, info)

    draft = build_draft(info, media, pages, hits, patterns, gid_re, bad_tail, page_bases)

    print("\n" + "=" * 74)
    print("GallerySite 草稿")
    print("=" * 74)
    print(draft)

    print("=" * 74)
    print("接下来")
    print("=" * 74)
    print("1. 存成 backend/collectors/%s/gallery.py" % name_for_path(info["host"]))
    print("2. 在 backend/collectors/__init__.py 末尾 import 它(否则 @register 不执行)")
    print("3. python scripts/selfcheck.py    # 声明自检: 样本 -> gid 无冲突")
    print("4. 加一条测试: check_site(site) 必须返回空列表")
    print("5. 用真实相册跑一次, 核对落盘文件的魔术字节(图片 \\xff\\xd8\\xff)")
    print()
    print("!! 的行都是「证据与预期不符」的地方, 别跳过。")
    print("!! 探针跑不出 xchina 那五项已知结论 = 探针自身有 bug(见本文件顶部)。")

    if args.out:
        Path(args.out).write_text(draft + "\n", encoding="utf-8", newline="")
        print("\n草稿已写入 %s" % args.out)

    try:
        session.close()
    except Exception:
        pass


def name_for_path(host):
    return site_name(host)[: -len("_gallery")] or "site"


if __name__ == "__main__":
    main()
