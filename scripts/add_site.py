"""加站门禁: 把「探测 -> 声明 -> 认领 -> 过滤 -> 落盘」串成一条可执行的门禁。

为什么需要这个脚本
==================
加站链路上, **前三段是有护栏的, 后三段全裸**:

    ① 探测        scripts/probe_site.py      三档证据门禁 + 可达性分型 + 草稿自检
    ② 声明自洽    check_site()               样本 -> gid 必须唯一且正确
    ③ 草稿自检    validate_draft()           草稿必须过它自己的 check_site()
    ---------------------------------------------------------------- 以上有护栏
    ④ 认领        resolve_collector()        没人认领 -> 静默落到通用采集器, 行为完全不同
    ⑤ 过滤        Filters.match_resource()   命中"广告位/装饰图" -> 静默全过滤 -> 0 资源
    ⑥ 落盘/命名   layout.place()/claim()     相对路径写歪 -> 文件落到用户找不到的地方

后三段的共同点是**错了不报错**: 任务照样跑完、照样 success, 用户拿到 0 个资源或
一堆散落在下载根目录的文件。这与本项目头号教训完全一致 —— 静默的错会在真实使用中
活很久, 所以必须有一条**主动去问**的入口, 而不是等用户报障。

本脚本 ④⑤⑥ **纯离线**(不发任何网络请求), 所以可以放心接进 CI / pre-commit。
`--smoke` 才碰网络, 它补的是"HEAD 通、正文却不是图"这一类只靠声明看不出来的坑。

用法::

    python scripts/add_site.py --verify xchina            # 四闸(离线)
    python scripts/add_site.py --verify xchina --smoke     # 追加一次真取正文
    python scripts/add_site.py --all                       # 所有已注册站点

判据与 `check_site` 保持同一个口径: **返回问题列表, 空列表 = 通过**。任何一个闸
报出问题, 退出码就是 1；全绿才是 0。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from collectors import COLLECTORS, resolve_collector  # noqa: E402
from collectors.gallery_base import SequenceGallerySpider, check_site  # noqa: E402
from core.filters import Filters  # noqa: E402
from core.layout import claim, place  # noqa: E402

#: 预演用的相册名。闸 4 关心的是"相册名这一层有没有被保留", 用谁都一样, 但
#: 这里刻意用一个**含中文与空格**的名字 —— 那正是 `clean_segment` 要处理的形态,
#: 拿 `abc` 去试等于什么都没试。
_ALBUM = "葡萄 一番街"


# --------------------------------------------------------------------------
# 闸的结论: **结构化**, 不是"一串给人看的文案"
# --------------------------------------------------------------------------
#
# 两条规矩, 各治一种"绿/红是假的"的病:
#
# ① 每道闸必须报 `checked`(实际核了几项)。`checked == 0` **不许算绿**。
#    治的是**假绿**里最常见的一种: 闸跑通了、印了一行 ok, 但它一项都没核 ——
#    因为它要核的东西压根不存在(没有样本 / 拼不出 URL / 站点没注册)。
#    "没验到" 与 "验过了没问题" 是两件事, 混在一行 ok 里就是撒谎。
#
# ② 问题带 `kind`(代号), 测试断言 `kind`, 不断言中文文案。
#    治的是**假红**: 本项目真实踩过一条 —— 断言写 `"chrome" not in ip`, 而那句
#    文案里提到 Chrome 是在说"**换指纹没用**"(IP 封禁只能换出口), 于是一条**正确**
#    的文案把测试判红了。文案是给人看的: 会改措辞、会有反讽、会被翻译; 判据必须是代号。
#    (同族: `core/errors.py` 用 `error_kind` 归类, 而不是 match 中文标签。)


NOTHING_CHECKED = "nothing-checked"      # 闸跑了, 但一项都没核 -> 一律算红


class Problem:
    """一条问题: `kind` 给机器判, `message` 给人看。"""

    __slots__ = ("kind", "message")

    def __init__(self, kind, message):
        self.kind = kind
        self.message = message

    def __str__(self):
        return self.message

    def __repr__(self):
        return "Problem(%r, %r)" % (self.kind, self.message)


class Gate:
    """一道闸的结论。

    `checked` 是**必填**且**必须是实际数目** —— 它是这整套门禁里唯一能防"空转绿"
    的字段。写新闸时忘了填, 这闸就是永远绿的, 而没人看得出来。
    """

    __slots__ = ("title", "checked", "problems", "rows")

    def __init__(self, title, checked=0, problems=(), rows=()):
        self.title = title
        self.checked = int(checked)
        self.problems = list(problems)
        self.rows = list(rows)

    @property
    def empty(self):
        """空转: 一项都没核 —— 这不是"通过"。"""
        return self.checked == 0

    def effective_problems(self):
        """对外的问题清单: **空转也算一条问题**。

        放在这里(而不是散在各个 `section()` 分支里)的理由: 让"空转不许算绿"成为
        `Gate` 自己的性质, 那么 `ok` / `verify()` / 测试三处读的是同一个判据 ——
        不会出现"脚本记得拦、测试忘了拦"这种半拉子护栏。
        """
        if self.empty and not self.problems:
            return [_p(NOTHING_CHECKED,
                       "这道闸**一项都没核到**(要核的对象不存在)。"
                       "「没验到」与「验过了没问题」是两件事 —— 前者不许算绿。")]
        return list(self.problems)

    @property
    def ok(self):
        return not self.effective_problems()

    def kinds(self):
        return {p.kind for p in self.effective_problems()}


def _p(kind, message):
    return Problem(kind, message)


# --------------------------------------------------------------------------
# 收集站点
# --------------------------------------------------------------------------


def registered_sites():
    """已注册站点 -> `(走本门禁的, 不走的)` 两个字典。

    ⚠️ 判据是 **`SequenceGallerySpider` 子类**, 不是"有没有 `site` 属性"。
    `PexelsSpider` 有 `site = PEXELS`, 但它**刻意没有**继承 `SequenceGallerySpider`
    (见 `stockphotos/pexels.py:126`: 本站一个 id 下只有一张图, 序号枚举模型不成立)。
    按"有 site 就算"去筛, 它会被套上序号模型去跑四道闸 —— 闸 3/闸 4 照样"全绿",
    而那份绿是**假的**: 它连 `url_for` 拼出来的 URL 都不存在。

    这正是本项目反复踩的那类错(家族 F: 通过但理由已经不对)。宁可不验, 不给假绿。
    """
    seq_sites, others = {}, {}
    for name, cls in COLLECTORS.items():
        site = getattr(cls, "site", None)
        if site is None:
            continue
        try:
            is_seq = issubclass(cls, SequenceGallerySpider)
        except TypeError:
            is_seq = False
        (seq_sites if is_seq else others)[name] = site
    return seq_sites, others


def _url_samples(site):
    """`id_samples` 里**长得像 URL** 的那些 (raw, gid)。"""
    out = []
    for item in site.id_samples or []:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            continue
        raw, want = item
        if isinstance(raw, str) and "://" in raw:
            out.append((raw, want))
    return out


def _first_gid(site):
    for item in site.id_samples or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
            return item[1]
    return ""


def _media_names(site):
    """`site.media_names()`, 但**声明本身写坏时**返回空 + 原因, 而不是抛出去。

    `variants=[]` 这种声明会让 `GallerySite.media()` 在 `self.variants[0]` 上抛
    `IndexError`。门禁的职责是**报出**这类问题 —— 崩在同一个地方等于没报, 而且更糟:
    脚本跑不完, 前面几道闸已经核出来的结论也一起丢了。
    (与 `core/errors.py` 的口径一致: 把异常变成一条可处置的事实。)
    """
    try:
        return list(site.media_names()), ""
    except Exception as e:
        return [], "media_names() 抛异常 %s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------
# 闸 1: 声明自洽(复用 check_site, 不另写一份判据)
# --------------------------------------------------------------------------


def gate_declaration(site):
    """样本 -> gid 必须唯一且正确 (判据复用 `check_site`, 不另写一份)。

    ⚠️ **零样本时必须算红, 不能算绿。** `check_site()` 在 `id_samples` 为空时
    返回空列表(它的每条检查都被 `if ... and samples:` 挡掉了), 于是这闸会印一行
    ok —— 而它一项都没核。这是本文件里**真实存在过的**假绿: 一个还没写样本的
    新站点, 闸 1 直接放行。`checked` 字段就是为它加的。
    """
    problems = [_p("declaration", m) for m in check_site(site)]
    return Gate("闸 1 声明自洽(样本 -> gid)",
                checked=len(site.id_samples or []), problems=problems)


# --------------------------------------------------------------------------
# 闸 2: 认领 —— 这些 URL 真的会被**这个**站点接走吗
# --------------------------------------------------------------------------


def gate_claim(site, name):
    """样本里的每条 URL 都必须被本采集器认领, 且不是并列。

    这一闸防的是**站点改版**的第一现场: 站点换了 URL 形态(加了路径段 / 换了文件名
    前缀), `id_patterns` 就配不上了。后果是静默的 —— URL 落到通用采集器去"试着抓",
    用户看到的是"采到的东西不对", 而不是"采集器坏了"。
    """
    problems = []
    samples = _url_samples(site)
    if not samples:
        # 这一闸要核的对象是 URL 形态的样本; 一条都没有 = 没验到, 不是通过。
        return Gate("闸 2 认领(谁接走这条 URL)", checked=0, problems=[_p(
            NOTHING_CHECKED,
            "id_samples 里没有任何 URL 形态的样本, 认领这一闸**没有验到**"
            "(纯 ID 样本走的是 check_site 的 gid_shape 检查)")])
    for raw, _want in samples:
        try:
            r = resolve_collector(raw)
        except Exception as e:
            problems.append(_p("resolve-raised",
                               "%s -> 认领解析抛异常 %s: %s" % (raw, type(e).__name__, e)))
            continue
        who = r.get("collector")
        if not r.get("auto"):
            problems.append(_p(
                "unclaimed",
                "%s -> **没有专用采集器认领**(会落到 %r); 多半是 id_patterns 不再"
                "匹配这个 URL 形态 —— 站点改版了, 或者样本写错了" % (raw, who)))
        elif who != name:
            problems.append(_p(
                "stolen",
                "%s -> 被 %r 抢先认领(期望 %r)。同一个 URL 归谁必须是确定的, "
                "否则今天走 A 明天走 B" % (raw, who, name)))
        elif r.get("ambiguous"):
            others = ", ".join(c["name"] for c in r.get("candidates", [])
                               if c["score"] == r.get("score") and c["name"] != who)
            problems.append(_p(
                "ambiguous",
                "%s -> %r 与 [%s] **并列同分**, 已按字典序取前者。正则写得太宽会"
                "把别的站点也吃进来" % (raw, who, others)))
    return Gate("闸 2 认领(谁接走这条 URL)", checked=len(samples), problems=problems)


# --------------------------------------------------------------------------
# 闸 3: 过滤 —— 拿去下载的 URL 会不会被 `match_resource` 静默拦掉
# --------------------------------------------------------------------------


def _sample_media_urls(site, seq=1):
    """按声明拼出**每类媒体各一条**真实 URL。返回 [(rtype, url), ...]。

    走 `site.url_for`(而不是自己拼字符串): 那是这份声明**自己的** URL 构造口径,
    闸 3 要验的正是"它拼出来的东西能不能过过滤", 自己拼就等于换了口径。
    """
    gid = _first_gid(site)
    if not gid:
        return []
    names, _err = _media_names(site)
    out = []
    for mname in names:
        try:
            url = site.url_for(gid, seq, None, mname)
        except Exception:
            continue
        if url:
            out.append((mname, url))
    return out


def gate_filter(site):
    """声明的资源 URL 必须能**通过"什么是有效资源"的唯一定义**。

    `Filters` 默认 `exclude_ad=True` —— 它会按**路径分段精确匹配**排除 `banner` /
    `sprite` / `logo` 这类名字。这对通用采集器是对的(整页抓取会混进站点装饰),
    但万一某个图集站的正常路径里就有一个 `ad` / `promo` 段, 那它的资源会被**全部**
    过滤掉, 而任务的终态是 `success` + 0 个资源。

    这就是"静默少采"里最难查的一种, 所以必须在加站时问一次。判据用默认 `Filters()`,
    因为那正是用户不配任何过滤条件时的行为。
    """
    flt = Filters()
    _names, broken = _media_names(site)
    if broken:
        # 声明本身就取不出媒体类型 -> 这是**能直接照做**的问题, 比"没验到"具体得多。
        return Gate("闸 3 过滤(默认 RuleSet 会不会静默拦掉)", checked=0,
                    problems=[_p("media-declaration-broken",
                                 "取不出本站的媒体类型(声明写坏): %s" % broken)])
    urls = _sample_media_urls(site)
    if not urls:
        return Gate("闸 3 过滤(默认 RuleSet 会不会静默拦掉)", checked=0, problems=[_p(
            NOTHING_CHECKED,
            "按声明拼不出任何资源 URL(url_for 返回空) —— 闸 3 **没有验到**")])
    problems = []
    rows = []
    for rtype, url in urls:
        reason = flt.match_resource(rtype, url)
        rows.append((rtype, url))
        if reason:
            problems.append(_p(
                "filtered-out",
                "%s 资源被默认过滤规则拦下: %s\n        URL: %s\n"
                "        修法: 该站点确实会把这类路径当资源 -> 把该段从 "
                "filters._AD_SEGMENTS/_AD_STEMS 里去掉; 若只是文件名巧合, "
                "改采集器的命名模板避开它" % (rtype, reason, url)))
    return Gate("闸 3 过滤(默认 RuleSet 会不会静默拦掉)",
                checked=len(urls), problems=problems, rows=rows)


# --------------------------------------------------------------------------
# 闸 4: 落盘 / 命名 —— 用户按预期去找, 能不能找到
# --------------------------------------------------------------------------


def gate_layout(site):
    """资源最终落在**哪一层** —— 离线预演一遍, 并断言铁律。

    铁律只有两条: 非视频 = `相册名/文件名`; 视频 = 平铺在下载根目录。
    第三件事是同名消解: 两个不同来源的同名文件必须在**下载前**被分开, 否则下载层
    会把后一个当"半成品续传", 拼出一份"大小对、内容是坏的"产物。
    """
    problems = []
    rows = []
    checked = 0
    names, broken = _media_names(site)
    if broken:
        return Gate("闸 4 落盘/命名(用户按预期找得到吗)", checked=0,
                    problems=[_p("media-declaration-broken",
                                 "取不出本站的媒体类型(声明写坏): %s" % broken)])
    for mname in names:
        try:
            mtype = site.media(mname)
        except Exception:
            continue
        if mtype is None:
            continue
        checked += 1
        ext = mtype.default_ext or (".mp4" if mname == "video" else ".jpg")
        try:
            stem = (mtype.seq_format or "{seq:05d}").format(seq=1)
        except Exception as e:
            problems.append(_p("seq-format-unrenderable",
                               "%s.seq_format 无法渲染: %s" % (mname, e)))
            continue
        # 采集器给出的 filename 形态: 相册名/序号+扩展名(见 gallery_base.crawl)
        rel_in = "%s/%s%s" % (_ALBUM, stem, ext)
        path, album = place(mname, rel_in, _ALBUM)
        if not path:
            problems.append(_p("place-empty",
                               "%s: place() 返回空路径(资源无处可落)" % mname))
            continue
        if ".." in path.split("/"):
            problems.append(_p("escapes-root",
                               "%s: 落盘路径含 `..`, 能跳出下载目录: %r" % (mname, path)))
        if mname == "video":
            if "/" in path:
                problems.append(_p("video-not-flat",
                                   "视频必须**平铺**在下载根目录, 却落到了 %r" % path))
        else:
            if "/" not in path or path.split("/")[0] != album:
                problems.append(_p(
                    "image-not-in-album",
                    "非视频资源必须落在 `相册名/文件名`, 却落到了 %r(相册名=%r)"
                    % (path, album)))
        rows.append((mname, path))

    # 同名消解: 同一格被**另一个来源**占了, 必须换名而不是原样返回
    occupied = {"%s/00001.jpg" % _ALBUM: "https://other.example.com/00001.jpg"}
    got = claim("%s/00001.jpg" % _ALBUM, _ALBUM, occupied.get,
                "https://this.example.com/00001.jpg")
    if got == "%s/00001.jpg" % _ALBUM:
        problems.append(_p(
            "claim-not-deduped",
            "同名消解没生效: 目标已被**别处**的同名文件占用, claim() 却原样返回 —— "
            "下载层会把它当半成品续传, 拼出一份内容坏掉的文件"))
    rows.append(("claim", got))
    return Gate("闸 4 落盘/命名(用户按预期找得到吗)",
                checked=checked, problems=problems, rows=rows)


# --------------------------------------------------------------------------
# 闸 5(可选, 走网络): HEAD 通 -> 正文真的是图/视频吗
# --------------------------------------------------------------------------


def gate_smoke(site, timeout=None, proxy=None):
    """真取一次正文的**头 64KB**, 按魔术字节判定它是不是媒体。

    `probe_site.py --smoke` 完整下一张(含 match_resource 复核), 贵但全；这一闸只取
    64KB, 便宜, 专门回答一个问题: **Content-Type 有没有撒谎**。有的站点对越界 URL
    答 `200 image/jpeg`, 正文却是 HTML 错误页 —— 那种坑只在下载完之后才暴露。
    """
    try:
        from core.config import settings
        from core.imageinfo import dimensions_from_bytes
        from core import transport
    except Exception as e:                     # 依赖缺失不该让门禁崩掉
        return Gate("闸 5 冒烟(真取正文头 64KB, 判魔术字节)", checked=0,
                    problems=[_p("dependency-missing", "冒烟无法执行(导入失败): %s" % e)])
    _names, broken = _media_names(site)
    if broken:
        return Gate("闸 5 冒烟(真取正文头 64KB, 判魔术字节)", checked=0,
                    problems=[_p("media-declaration-broken",
                                 "取不出本站的媒体类型(声明写坏): %s" % broken)])
    urls = _sample_media_urls(site)
    if not urls:
        return Gate("闸 5 冒烟(真取正文头 64KB, 判魔术字节)", checked=0, problems=[_p(
            NOTHING_CHECKED, "按声明拼不出任何资源 URL —— 冒烟**没有验到**")])
    problems = []
    hdrs = {"User-Agent": settings.user_agent, "Accept": "*/*"}
    # ⚠️ 走 `core.transport` 而不是裸 `requests`: 后者不能换浏览器指纹, 而这一闸最常
    # 需要它的场合(站点在 CF 后面)恰恰是"裸 HTTP 拿不到正文"的那种。顺便: 手写
    # `with requests.get(...)` 会被 `tests/test_transport.py` 的源码门禁拦下 ——
    # `curl_cffi` 的响应不支持上下文管理器, 所以这个写法只能有一个出口。
    session, _label = transport.build(proxy)
    try:
        for rtype, url in urls:
            try:
                with transport.streamed(session, "get", url, headers=hdrs,
                                        timeout=timeout or 15, stream=True) as resp:
                    if resp.status_code != 200:
                        problems.append(_p("http-error",
                                           "%s: HTTP %s —— 声明拼出来的 URL 取不到正文"
                                           % (url, resp.status_code)))
                        continue
                    buf = b""
                    for chunk in resp.iter_content(8192):
                        buf += chunk
                        if len(buf) >= 65536:
                            break
                    ctype = (resp.headers.get("Content-Type") or "").lower()
            except Exception as e:
                problems.append(_p("fetch-failed",
                                   "%s: 取正文失败 %s: %s" % (url, type(e).__name__, e)))
                continue
            dims = dimensions_from_bytes(buf)
            print("    %s  %s" % ("ok " if dims else "!! ", url))
            print("        Content-Type=%s  头部 %d 字节  尺寸=%s"
                  % (ctype or "(无)", len(buf), dims or "解析不出(可能不是媒体)"))
            if dims is None:
                problems.append(_p(
                    "not-media",
                    "%s: 正文**认不出是媒体**(Content-Type=%s, 前 64KB 没有可识别的魔术字节)\n"
                    "        站点可能对越界请求答 200 + 错误页; 直接下单会拿到一堆坏文件"
                    % (url, ctype or "无")))
    finally:
        try:
            session.close()
        except Exception:
            pass
    return Gate("闸 5 冒烟(真取正文头 64KB, 判魔术字节)",
                checked=len(urls), problems=problems)


# --------------------------------------------------------------------------
# 串起来
# --------------------------------------------------------------------------


def verify(name, site, smoke=False, timeout=None, proxy=None):
    """跑一条站点的全部闸。返回 (是否全绿, 问题总数)。"""
    print("=" * 74)
    print("加站门禁: %s" % name)
    print("=" * 74)
    names, broken = _media_names(site)
    print("声明      : base=%s  媒体=%s  样本=%d 条"
          % (site.base, ",".join(names) if names else ("(取不出: %s)" % broken if broken else "(无)"),
             len(site.id_samples or [])))
    print()

    total = 0

    def section(gate):
        """印一道闸。⚠️ 空转**不许印 ok** —— 判据在 `Gate.effective_problems()`。"""
        nonlocal total
        print("-- %s" % gate.title)
        for row in gate.rows:
            print("     " + "  ".join(str(x) for x in row))
        problems = gate.effective_problems()
        if not problems:
            print("   核了 %d 项: ok" % gate.checked)
            return
        total += len(problems)
        for p in problems:
            print("   !! %s" % p)
        print()

    section(gate_declaration(site))
    section(gate_claim(site, name))
    section(gate_filter(site))
    section(gate_layout(site))
    if smoke:
        section(gate_smoke(site, timeout, proxy))

    print("-" * 74)
    if total == 0:
        print("=> 全绿: %s 的四道离线闸%s都过了" % (name, "+冒烟" if smoke else ""))
    else:
        print("=> %d 个问题。上面每条都写清了「发生了什么 + 怎么修」—— 加站前必须清零。"
              % total)
    print("-" * 74)
    return total == 0, total


def main():
    ap = argparse.ArgumentParser(description="加站门禁: 认领 / 过滤 / 落盘(离线)")
    ap.add_argument("name", nargs="?", default="", help="站点名(已注册的采集器名)")
    ap.add_argument("--verify", default="", help="同位置参数, 显式写法")
    ap.add_argument("--all", action="store_true", help="校验所有已注册的声明式站点")
    ap.add_argument("--smoke", action="store_true", help="追加一次网络冒烟(会发请求)")
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--list", action="store_true", help="只列出可校验的站点")
    args = ap.parse_args()

    sites, others = registered_sites()
    if args.list or not (args.name or args.verify or args.all):
        print("可校验的声明式站点(%d 个):" % len(sites))
        for k in sorted(sites):
            print("  %-24s base=%s" % (k, sites[k].base))
        if others:
            print("\n**不走本门禁**(不是 SequenceGallerySpider, 序号枚举模型不成立):")
            for k in sorted(others):
                print("  %-24s base=%s  -> 用 scripts/probe_feed.py 探"
                      % (k, others[k].base))
        if not (args.name or args.verify or args.all):
            print("\n用法: python scripts/add_site.py --verify <站点名>  (或 --all)")
        return 0

    ok_all = True
    if args.all:
        for k in sorted(sites):
            ok, _n = verify(k, sites[k], args.smoke, args.timeout, args.proxy)
            ok_all = ok_all and ok
            print()
        return 0 if ok_all else 1

    target = args.name or args.verify
    if target in others:
        print("!! %r 是**第二类站点**(非序号枚举型), 四道闸对它有假的通过 —— 所以不跑。" % target)
        print("   id 下只有一张图 / 资源清单由页面或 API 下发的那类, 走:")
        print("     python scripts/probe_feed.py <页面URL>   # 探分页/端点/密钥")
        return 1
    if target not in sites:
        # 名字打错时把可选项列出来, 而不是只说一句 "unknown"
        near = [k for k in sites if target.lower() in k.lower()]
        print("!! 没有名为 %r 的声明式站点(它没注册, 或没有 `site` 声明)" % target)
        if near:
            print("   最接近的: %s" % ", ".join(sorted(near)))
        print("   全部候选: %s" % ", ".join(sorted(sites)))
        return 1
    ok, _n = verify(target, sites[target], args.smoke, args.timeout, args.proxy)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
