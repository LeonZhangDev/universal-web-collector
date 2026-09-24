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
       (还包括: 文件名里除了序号, 是不是还跟着一段**内容哈希**?
        跟着就说明它**不是**序号枚举型 —— 见 `suffix_is_opaque`)

这五件事**猜错的代价是不对称的**, 而这正是本项目反复吃过的亏:

- ① 猜错是**静默**的 —— 站点对不存在的图集照样答 `200 text/html`, 于是枚举
  "正常地"采到 0 个资源, 任务还报 success(2026-09-18 真实事故)。
- ② 猜错的表现是"**浏览器能看, 代码全 403**", 极易被误判成防盗链或需要登录。
- ⑤ 猜错有两种, 症状一样是"**成功但 0 资源**": 一种是把**页码**当 ID(去枚举一个
  不存在的图集), 另一种是**序号后面跟着内容哈希**(每一页拼出来的 URL 都不存在)。

而 `scripts/probe.py` 是**浏览器探针**(跑 `BrowserCollector` 看四种解析器发现了
什么), 服务的是通用采集器, 与上面五项无关。这五项此前只能手搓 curl 挨个试。

本脚本把它们跑成报告 + 一份可直接填的 `GallerySite` 草稿。

它自己怎么被验证
================
**① 拿已知站点当回归靶子。** `collectors/xchina/gallery.py` 的模块文档里那五项结论
是钉死的(越界返回 `200 text/html` / `Accept` 必须显式带 `image/*` / HEAD 可靠 /
资源按相册分到 `photos`、`photos2` / 7 种 URL 形态), 而且每条都写明了日期。
探针跑不出同样的结论, 说明**探针写错了**, 而不是站点变了 ——
这是验证探针自身的唯一办法, 也是本脚本存在的第二个理由。

**② 再拿真实站点当靶子。** (2026-09-24) 本地假站点只能验"我**想到**的行为";
真实站点的 URL 形态是**想不出来**的。拿几个形态不同的真实站点跑一遍, 探针当场暴露
出五处"看着能用、其实 0 资源"的产出错误 —— 详见 `tests/test_probe_site.py` 末尾
那一节。它们**都不是崩溃**, 所以"跑得通"根本发现不了:

    · 序号之后的高熵内容哈希被当成"画质后缀"(MangaDex: `/data/<hash>/1-<sha256>.png`)
    · `id_samples` 给自检**没通过**的 URL 编了一个期望 gid
    · 末尾整段是数字的路径(`/id/1040`)被当成"相册桶号" -> 展开上千候选
    · 多段 base_path 去数字后留尾随斜杠 -> 生成 `/id//(...)` 这种正则
    · 命中 0 个序号时照样打印一份没有证据支持的草稿

**结论: 靶子要定期换真实站点, 优先挑形态怪的。** 顺手还能记下哪些站根本不可达
(本轮: 漫画柜连接超时; Lorem Picsum 的直链要 hmac 签名; Internet Archive 的页图
是 `.php?file=...` 形态, 连"这是资源直链"都判不出来)。

门禁: 从「五个手写的 if」到「一条规则」
======================================
上面那五处当时是逐个手写 `if` 堵的。堵完之后顺手把模式抽了出来 —— 因为**补丁只能
挡住已经见过的那几种形态**, 而真正要防的是"第六种":

    档 A  拼不出 URL   base / seq_format / suffix / gid_shape / id_patterns
                       缺实测证据 -> **拒绝出草稿**(exit 2), 一个字都不给
    档 B  采多采少     variants / quality_map / id_samples        -> 降级并标注
    档 C  只影响体验   input_forms / album_url_template           -> 只标注

分档的判据就一句: **猜错了, 是每条 URL 都错, 还是只是少采一点。**
有了它, 第六种形态哪怕从没见过也会被拦下 —— 新写出来的那一行必然带缺口。

另外两处也一并前移了:

- **第 0 段 可达性**: 直链不通就早退(省掉后面四项、二十来个请求), 且 `403/429/503`
  这类"被挡在门外"的状态会**自动**换一次浏览器 TLS 指纹重试 —— 不该让人先看见 403、
  再手动加参数重跑一遍(2026-09-23 在 xchina 上就是这么手动重跑的)。
- **草稿自检**: 拼完就地 exec 起来跑 `check_site()`。它是这份声明**唯一**的验收标准,
  以前靠人肉(存盘 -> import -> 跑 selfcheck.py), 现在当场就能看见。

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
from collectors.gallery_base import (  # noqa: E402
    GallerySite,
    _head_status_headers,
    check_site,
)

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


def short_tail(url, n=48):
    """截断时**保留尾部**。

    URL 的末段才是要人判断的那一段: `/photo/id-x/10.html` 里的 `10.html` 说明它是
    **页码**, 而 `/photo/id-x.html` 不是。用 `short()` 从头截恰好把这段砍掉, 于是
    提示里给出的两个 URL 长得一模一样 —— 人有理由怀疑探针到底在说哪一个。
    """
    return url if len(url) <= n else "…" + url[-(n - 1):]


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


#: 序号之后那段残留多长 / 含多长的 hex 串, 就不再可能是"画质后缀"。
_OPAQUE_MIN_LEN = 40
_OPAQUE_HEX = re.compile(r"[0-9a-fA-F]{16,}")

#: 相册桶号(`photos2`)的最大值 —— 超过这个数就不像桶号, 更像路径里的一个 ID。
_MAX_BUCKET_DIGITS = 99


def suffix_is_opaque(rest):
    """序号之后的残留是**画质后缀**还是**不透明串(内容哈希)**?

    这一条决定站点到底是不是"序号枚举型", 而它**不是**"文件名以数字开头"就能判的:

        MangaDex(真实站点, 2026-09-24 实测)的直链是
        `/data/<图集hash32>/1-<该页内容的sha256>.png`

    文件名以 `1` 开头、看着像序号, 但**改序号毫无用处** —— 第 2 页的哈希与第 1 页
    完全不同, 照那个模板拼出来的 URL 必然 404(实测 seq 2..6 全 404)。这类站点的
    文件名由 API 下发, 属于"资源来自页面/API 解析", 与 pexels 同类。

    判据刻意取**很宽**的下界(≥40 字符 或 含 ≥16 位连续 hex): 漏判成"不透明"最多
    让人多看一眼; 判反了则会生成一份"看着能用、实际 0 资源"的声明。
    """
    body = rest.rsplit(".", 1)[0] if "." in rest else rest
    if len(rest) >= _OPAQUE_MIN_LEN:
        return True
    return bool(_OPAQUE_HEX.search(body))


def infer_from_link(url):
    """从一条资源直链里读出 基址 / gid / 序号宽度 / 后缀。

    直链是手上最可靠的一份证据: `.../photos2/69ad45698f836/0001.jpg` 同时说明了
    CDN 子路径是 `photos2`、gid 在倒数第二段、序号补 4 位零。

    返回的 `seq_format` 为 None 表示**文件名不以数字序号开头** —— 那说明这个站
    根本不是"序号枚举型", `SequenceGallerySpider` 处理不了它(如 pexels 一个 ID
    只有一张图)。这时必须明说, 不能硬套一个模板进去。

    另一种同样不能硬套的是 `opaque_suffix`: 文件名以数字开头, 但序号之后是**内容
    哈希**(见 `suffix_is_opaque`)。两者判反的后果一样 —— 一份永远采不到的声明。
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
        "opaque_suffix": False,
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
        info["opaque_suffix"] = suffix_is_opaque(info["suffix"])
    return info


def split_digit_base(base):
    """把以数字结尾的基址拆开: `.../photos2` -> (`.../photos`, 2)。

    站点按**数字后缀**给相册分桶(`photos`/`photos2`/`photos3`)时, 正确的声明是
    `base=".../photos"` + `base_candidate_digits=N`, 让 `_base_candidates` 自动
    展开候选。写成 `base=".../photos2"` 的话, 别的相册一律判空 —— 而用户只看到
    "任务失败", 完全看不出是路径不对。所以这里主动拆。

    ⚠️ 但"基址以数字结尾"**不等于**"那个数字是桶号"。picsum 的
    `https://fastly.picsum.photos/id/1040/200/300.jpg` 推出 base 到 `/id/1040`,
    这里的 `1040` 是**图集 ID 本身**; 按桶拆会把基址砍成 `/id/` 并写出
    `base_candidate_digits=1040`, 让基类去展开一千多个候选(2026-09-24 实测)。
    两条判据挡掉它: 数字前面紧跟 `/`(说明是**整段**路径而不是 `photos2` 这种后缀),
    或桶号大得不像桶号。
    """
    m = re.search(r"(\D)(\d+)$", base or "")
    if not m:
        return base, 0
    if m.group(1) == "/":
        return base, 0
    n = int(m.group(2))
    if n > _MAX_BUCKET_DIGITS:
        return base, 0
    return base[: m.start(2)], n


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
        # `rstrip("/")` 不能省: base_path 是 `id/1040` 这种多段时, 去掉末尾数字会
        # 留下尾随斜杠, 拼进模板就变成 `/id//(...)` —— 一条几乎不可能匹配上的正则
        # (2026-09-24 在 picsum 上实测到)。
        root = re.sub(r"\d+$", "", base_path).rstrip("/")
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
# 第 0 段(可达性) + 五段探测
# --------------------------------------------------------------------------


#: 这些状态码说明"不是这条 URL 的问题, 而是我们被挡在门外了" —— 值得换指纹再试一次。
_REACH_RETRY_STATUS = (401, 403, 406, 429, 503)


def probe_reachability(session, info, timeout):
    """**第 0 段**: 先确认"你给的那条直链"到底通不通, 不通就早退。

    判据是 **200 + 媒体类型**, 不是"状态码是 200" —— 本站项目在这一点上吃过大亏:
    站点对拿不到的图集照样答 `200`, 只是 Content-Type 变成 `text/html`。只看状态码
    会把"完全拿不到"读成"一切正常"。

    早退的回报是**省掉四次白跑**: 直链不通时 ②③④⑤ 加起来会发二十来个请求, 然后
    给出一屏没有意义的数字 —— 而真正该做的第一件事(解决可达性)被埋在最下面。

    返回 (ok, status, ctype)。
    """
    print("== 第 0 段 直链可达性 ==")
    headers = {"User-Agent": settings.user_agent, "Accept": accept_for(info["ext"])}
    status, hdrs = head(session, info["url"], headers, timeout)
    ct = (hdrs.get("Content-Type") or "").lower() if hdrs else ""
    ok = status == 200 and ct.startswith(ctype_for(info["ext"]))
    print("  %s" % short(info["url"], 68))
    print("      -> %s  %s" % (status, ct or "(无 Content-Type)"))
    print("  => %s" % ("通过: 直链能拿到资源, 后面的探测才有意义"
                      if ok else "不通 —— 后面的四项**都没跑**(它们全都要先能拿到资源)"))
    return ok, status, ct


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
    if len(hits) == 1 and hits[0][0] == 1 and scan > 1:
        # 「只命中第一个」是**歧义**, 不是结论。光看这一条分不清是哪一个:
        #   ① 图集本来就只有 1 张;
        #   ② 每页的文件名各不相同(序号后面还跟着内容哈希), 改序号拼不出下一页
        #      —— 那这个站根本**不是**序号枚举型, 草稿一个字都不该抄;
        #   ③ 序号不是从 1 开始。
        # 当成"站点正常"的代价: 照 ② 那份草稿下单 = 任务 success 但 0 个资源。
        print("  => !! 只命中第 1 个序号 —— 这是**歧义, 不是结论**: "
              "①图集本来就只有 1 张; ②每页文件名各不相同(改序号拼不出下一页, 例如 "
              "`/data/<图集hash>/1-<该页内容哈希>.png`); ③序号不从 1 开始。"
              "再给一条**不同序号**的直链就能当场分辨, 别急着照抄草稿。")
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
    seen_pat = {patterns[0][1]}
    for u in pages:
        pp = page_pattern(u, gid, gid_re)
        # 去重: 同一个站点的多种页面形态(相册页 `/photo/id-X.html` 与分页
        # `/photo/id-X/10.html`)常常推出**同一条**正则。重复写进 `id_patterns` 无害,
        # 但会让人以为漏了哪条, 也让 `check_site()` 白跑一遍。
        if pp and pp not in seen_pat:
            seen_pat.add(pp)
            patterns.append(("相册页", pp))

    for kind, pat in patterns:
        hit = sum(1 for u in all_urls if re.search(pat, u))
        print("  %-6s %s" % (kind, pat))
        print("         命中 %d/%d 条输入" % (hit, len(all_urls)))

    bad_tail = page_tail_evidence(pages, gid)
    if bad_tail:
        print("  !! 末段是纯数字(页码, 不是 ID): %s"
              % [short_tail(u, 46) for u in bad_tail])
        print("     => 必须声明 page_tail=r\"^\\d+$\"。否则解析失败时会退回"
              "“取路径末段”, 把页码当 ID -> 枚举不存在的图集 -> 成功但 0 资源。")

    ok, fails, resolution = 0, [], {}
    for u in all_urls:
        got = None
        for _kind, pat in patterns:
            m = re.search(pat, u)
            if m and m.group(1):
                got = m.group(1)
                break
        resolution[u] = got
        if got == gid:
            ok += 1
        else:
            fails.append((u, got))
    print("  自检: %d/%d 条输入解析出一致的 gid (%s)" % (ok, len(all_urls), gid))
    for u, got in fails:
        print("      !! %s -> %r" % (short_tail(u, 58), got))
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
    return patterns, gid_re, bad_tail, resolution


# --------------------------------------------------------------------------
# 草稿
# --------------------------------------------------------------------------


#: 草稿字段按"猜错的后果"分档 —— **门禁只认档 A**。
#:
#:     A  拼不出 URL    猜错 = 每一条 URL 都是错的, 整份声明作废
#:     B  采多采少      猜错 = 少几个画质档 / 少两条样本, 不影响能不能采到
#:     C  只影响体验    猜错 = 提示文案不好看
#:
#: 分档不是为了好看, 是为了把"哪些字段必须实测"从**人脑**搬进**程序**。
#: 在那之前, 每遇到一个新形态就得手写一个 `if` 去堵 —— 2026-09-24 在 MangaDex
#: 与 picsum 上一次性堵了五个, 而它们只是同一类错(探测结果没进结论)的五个化身。
#: 搬进来之后, 第六种形态**哪怕从没见过**, 新写出来的那行也必然带缺口、必然被拦下。
TIER_A, TIER_B, TIER_C = "A", "B", "C"


class Gap:
    """一条「这个字段其实没有实测证据」的缺口。"""

    __slots__ = ("field", "tier", "why", "fix")

    def __init__(self, field, tier, why, fix=""):
        self.field = field
        self.tier = tier
        self.why = why
        self.fix = fix


class Draft(str):
    """草稿正文 + 它的证据缺口。

    继承 `str` 是为了让 `draft.split(...)` / `x in draft` 这类用法照旧可用 ——
    绝大多数调用方只关心正文, 只有 `main()` 需要看 `gaps`。
    """

    def __new__(cls, text, gaps=()):
        self = super().__new__(cls, text)
        self.gaps = list(gaps)
        return self

    @property
    def blocking(self):
        """档 A 的缺口 —— 有它就不该出草稿。"""
        return [g for g in self.gaps if g.tier == TIER_A]

    @property
    def advisory(self):
        """档 B/C 的缺口 —— 照出草稿, 但要标注。"""
        return [g for g in self.gaps if g.tier != TIER_A]


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


def build_draft(info, media, pages, hits, patterns, gid_re, bad_tail, page_bases,
                resolution=None, seq_hits=None):
    """拼一份 `GallerySite` 草稿, 并把**证据缺口**一并带出来。

    带 `# TODO` 的地方是**必须人工确认**的。

    `resolution` 是 ⑤ 逐条 URL 的解析结果。**只有自检通过的 URL 才写进
    `id_samples`** —— 期望值必须是"正则真的从这条 URL 里抽出来的那个", 不能一律
    填采信直链的 gid。填错了不会报错, 只会让 `check_site()` 拿着假证据空转
    (2026-09-24 用 MangaDex 实测到: 页面 URL 明明报 `-> None`, 草稿却给它配了 gid)。

    返回 `Draft`(str 的子类, 正文照旧可以直接用) 外加 `gaps`。**gaps 才是重点**:
    在这之前"哪些字段是实测的、哪些是猜的"只存在于人脑里, 于是每一处新形态都得手写
    一个 `if` 去堵; 现在交给调用方判断 —— 档 A 有缺口就**一个字都不给**(见 `main()`)。
    """
    name = site_name(info["host"])
    base, digits = split_digit_base(info["base"])
    resolution = resolution or {}
    gaps = []
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

    # ---- 收集证据缺口(门禁的输入) ----
    if seq_hits == 0:
        # 命中 0 意味着"序号枚举型"这个**前提**本身没验出来 —— 草稿里每一条都是按
        # URL 形态猜的。猜错的失败方式是"任务 success 但 0 个资源", 本项目最贵的一种错。
        gaps.append(Gap(
            "base / seq_format", TIER_A,
            "--scan 范围内**一个存在的序号都没探到**",
            "先让报告 ① 里那几条 HEAD 真的返回 200 + 媒体类型, 再回来重跑"))
    if not any(h["suffix"] == info["suffix"] for h in hits):
        # 采信的那条直链自己没探通 -> base / gid / suffix 至少有一个是错的。
        gaps.append(Gap(
            "base / gid / suffix", TIER_A,
            "按你给的直链拼出来的那条 URL **自己都没探通**",
            "直链可能已失效(签名过期 / 图集删了), 先换一条有效的"))
    if seq_hits == 1:
        # 单条命中是**歧义**而不是缺口: 有证据, 只是不足以定论。所以只降级、不阻断。
        gaps.append(Gap(
            "base / seq_format", TIER_B,
            "只探到 **1 个**存在的序号(见报告 ① 的歧义提示)",
            "再给一条**不同序号**的直链就能当场分辨"))

    L = ["# 由 scripts/probe_site.py 生成 —— 每条都对应报告里的一条实测证据。",
         "# 标 TODO 的地方必须人工确认, 不要直接提交。", ""]
    if seq_hits == 1:
        L.extend([
            "# !! 实测: --scan 范围内只探到 **1 个**存在的序号 —— 这份声明可能根本",
            "#    不成立。先按报告 ① 的歧义提示补一条**不同序号**的直链复核, 再决定用不用。",
            "",
        ])
    L.extend([
        "from .. import register",
        "from ..gallery_base import GallerySite, SequenceGallerySpider", ""])
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
    # ⚠️ 期望值必须是"正则真的从这条 URL 里抽出来的", 不能一律填采信直链的 gid。
    good = [u for u in (media + pages) if resolution.get(u) == info["gid"]]
    missed = [u for u in (media + pages) if resolution.get(u) != info["gid"]]
    if (media or pages) and not good:
        gaps.append(Gap(
            "id_patterns", TIER_A,
            "**没有一条**输入能解析出 gid —— 现有正则一条 ID 都抽不出来",
            "先修正则, 或换一条更有代表性的直链"))
    elif missed:
        gaps.append(Gap(
            "id_patterns", TIER_B,
            "%d 条输入没被任何正则覆盖(见下方注释)" % len(missed),
            "补一条正则; 该形态也要进 `id_samples`, 否则回归时没有护栏"))
    L.append("    id_samples=[")
    L.append('        ("%s", "%s"),' % (info["gid"], info["gid"]))
    for u in good[:4]:
        kind = "直链" if u in media else "相册页"
        L.append('        ("%s", "%s"),   # %s' % (u, info["gid"], kind))
    if not good:
        L.append("        # !! **没有一条**输入通过自检 —— 上面的 id_patterns 目前抽不出任何 gid,")
        L.append("        #    先修正则(或换一条更有代表性的直链), 再回来补这一节。")
    L.append("        # TODO 每种输入形态都要有一条: 直链 / 相册页 / 分页 / 老式 query / 裸 ID")
    L.append("    ],")
    if missed:
        L.append("    # !! 以下输入**没通过自检**, 因此**没有**写进 id_samples ——")
        L.append("    #    给它们编一个期望值, 只会让 check_site() 拿着假证据空转:")
        for u in missed[:3]:
            L.append("    #      %s  -> %r" % (short_tail(u, 56), resolution.get(u)))
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
    return Draft("\n".join(L), gaps)


def refuse(title, gaps=(), lines=()):
    """一份「不生成草稿」的拒绝报告 —— 所有挡路的出口共用它。

    统一出口的价值是口径一致: 用户永远看到「为什么拦」+「怎么补」两件事, 而不是
    三个出口三种写法(其中一种忘了写"怎么补")。
    """
    print("\n" + "=" * 74)
    print("!! 不生成草稿: %s" % title)
    print("=" * 74)
    for ln in lines:
        print(ln)
    if gaps:
        print("档 A 字段(拼不出 URL —— 猜错就是整份声明作废)缺实测证据:")
        for g in gaps:
            print("  · %s" % g.field)
            print("      为什么: %s" % g.why)
            if g.fix:
                print("      怎么补: %s" % g.fix)
    print()
    print("一份没有实测支撑的声明, 拼出来的是**每一页都不存在**的 URL, 而失败方式是")
    print("「任务 success 但 0 个资源」—— 本项目最贵的一种错。所以这里一个字都不给。")
    return 2


def validate_draft(text):
    """把草稿**跑起来**并调用 `check_site()` —— 那是这份声明唯一的验收标准。

    草稿里写的是相对 import(`from .. import register`), 直接 exec 会失败; 换成绝对
    import 即可(`backend` 已在 `sys.path` 上)。这不改变"草稿被放进 `collectors/`
    之后"的语义 —— 那时相对 import 指向的正是同一个模块。

    以前这步靠人肉: 存盘 -> import -> 跑 `selfcheck.py`。现在前移到探针里, 于是
    "草稿自己的 `id_samples` 过不了自己的 `id_patterns`"当场就能看见, 而不是等提交
    之后被 CI 拦下。

    返回 (problems, note)。note 非空表示草稿连跑都跑不起来。
    """
    src = (text
           .replace("from .. import register", "from collectors import register")
           .replace("from ..gallery_base import",
                    "from collectors.gallery_base import"))
    ns = {}
    try:
        exec(compile(src, "<draft>", "exec"), ns)
    except Exception as e:
        return [], "%s: %s" % (type(e).__name__, e)
    site = next((v for v in ns.values() if isinstance(v, GallerySite)), None)
    if site is None:
        return [], "草稿里没有 GallerySite 实例"
    try:
        return check_site(site), ""
    except Exception as e:
        return [], "check_site 抛出 %s: %s" % (type(e).__name__, e)


def report_draft_selfcheck(text):
    """跑一遍草稿自检并打印结论。"""
    print("\n" + "=" * 74)
    print("草稿自检 (check_site)")
    print("=" * 74)
    problems, note = validate_draft(text)
    if note:
        print("  !! 草稿跑不起来: %s" % note)
        return
    if not problems:
        print("  => 通过: 每条 `id_samples` 都被解析出期望的 gid, 各 pattern 无冲突。")
        return
    print("  !! %d 条问题 —— 草稿是给人改的起点, 但下面这些**必须先改掉**:" % len(problems))
    for p in problems:
        print("     · %s" % p)
    print("  => 改到这一节为空, 才算可以提交的声明。")


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

    ok, status, _ct = probe_reachability(session, info, args.timeout)
    if not ok and not args.impersonate and status in _REACH_RETRY_STATUS:
        # "站点在 Cloudflare 后面"是最常见的一种"谁都没做错、就是进不去"。既然换指纹
        # 这条现成的路就在手边, 就不该让用户先看见 403、再手动加参数重跑一遍 ——
        # 中间那一步纯属浪费(2026-09-23 在 xchina 上就是这么手动重跑的)。
        print("  => %s: 自动改用 chrome TLS 指纹重试一次(curl-cffi)" % status)
        alt_session, alt_transport = make_session(args.proxy, "chrome")
        ok2, status2, _ct2 = probe_reachability(alt_session, info, args.timeout)
        if ok2 or status2 != status:
            try:
                session.close()
            except Exception:
                pass
            session, transport = alt_session, alt_transport
            ok, status = ok2, status2
        else:
            try:
                alt_session.close()
            except Exception:
                pass
    if not ok:
        sys.exit(refuse(
            "你给的那条直链**本身没探通**",
            lines=[
                "所以后面的四项(请求头 / 页面 / 变体 / URL 形态)**都没跑** —— 它们全都",
                "要先能拿到资源, 跑了也只是给出一屏没有意义的数字。",
                "",
                "下一步按顺序试(上面标了「自动」的就不必再手工跑一遍):",
                "  1. --proxy <你的代理>      站点可能只对特定地区放行",
                "  2. --impersonate firefox   换一个指纹(curl-cffi 支持 chrome/firefox/safari/edge)",
                "  3. 浏览器里打开这条直链, 确认它**本身**还有效(签名过期/图集删了都会这样)",
            ]))

    _verdict, ex_rows = probe_existence(session, info, args.scan, args.over, args.timeout)
    seq_hits = sum(1 for r in ex_rows if r[4])
    probe_accept(session, info, args.timeout)
    page_bases, page_suffixes = probe_page(session, pages, args.timeout)
    hits = probe_variants(session, info, args.timeout, page_suffixes)
    patterns, gid_re, bad_tail, resolution = probe_url_forms(args.urls, media, pages, info)

    if info["opaque_suffix"]:
        # 文件名以数字开头、但序号之后是内容哈希 —— 与 pexels 同类, 只是更隐蔽。
        # 这里**断言地**拒绝出草稿: 那份草稿的每一条 URL 都会 404, 而失败方式是
        # "任务 success 但 0 个资源"(本项目最贵的一种错)。
        print("\n" + "=" * 74)
        print("!! 不生成草稿: 这不是「序号枚举型」站点")
        print("=" * 74)
        print("序号之后那一段是**不透明串(内容哈希)**, 不是画质后缀:")
        print("    后缀 = %r" % short(info["suffix"], 70))
        print()
        print("含义: 每一页的文件名都不相同(第 2 页的哈希与第 1 页无关), **改序号**")
        print("拼不出下一页 —— 上面 ① 的实测若也显示「命中 1/N」, 那就更确定了。")
        print("这类站点的资源清单由**页面或 API** 下发(与 pexels 同类), 得走")
        print("`SequenceGallerySpider` 之外的路子; 硬写一份 `seq_format + suffix` 的")
        print("声明, 结果只会是「任务 success 但 0 个资源」。")
        print()
        print("⚠️ 判据是启发式的(长度 ≥%d 或含 ≥16 位连续 hex)。若你确认这真的是"
              % _OPAQUE_MIN_LEN)
        print("   画质后缀, 把直链与站点说明发出来改判据 —— 但**不要**先照抄草稿。")
        try:
            session.close()
        except Exception:
            pass
        sys.exit(2)

    draft = build_draft(info, media, pages, hits, patterns, gid_re, bad_tail,
                        page_bases, resolution, seq_hits)

    if draft.blocking:
        # 档 A 缺证据 -> 拒绝出草稿。这一步是**把特例升成规则**: `opaque_suffix`
        # 早就在这么干了, 但全文件只有它一处 —— 于是每遇到一个新形态都得再手写一个
        # if。现在凡是"URL 拼不出来"的字段缺实测, 都走同一个出口。
        sys.exit(refuse("档 A 字段缺实测证据", draft.blocking))
    if draft.advisory:
        print("\n" + "-" * 74)
        print("注意(不阻断出草稿, 但也别当没看见):")
        for g in draft.advisory:
            print("  · %s —— %s" % (g.field, g.why))
            if g.fix:
                print("      怎么补: %s" % g.fix)
        print("-" * 74)

    print("\n" + "=" * 74)
    print("GallerySite 草稿")
    print("=" * 74)
    print(draft)

    report_draft_selfcheck(draft)

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
