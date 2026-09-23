"""DASH(.mpd) 解析 —— 把一份 MPD 变成"可以直接下载的分片 URL 列表 + 一条时长基线"。

为什么单独成一个模块
====================
MPD 是 XML 规范, 解析逻辑与"怎么下载"完全无关, 而它又是这一块最容易出错的地方
(继承、时间轴、模板替换)。分开之后可以**纯字符串**测: 不用网络、不用 ffmpeg,
给一段 XML 就能断言解析结论 —— 这与 `collectors/hls.py::parse_playlist` 是同一个
思路(那个也单独出来就是为了能被钉住)。

⚠️ 边界: 刻意只做一块, 其余**明确拒绝**
========================================
MPD 比 m3u8 复杂得多(多时段、字节范围、DRM、直播)。这里只做图集站实际会出现的
那一块:

| 形态 | 态度 |
| --- | --- |
| `SegmentTemplate` + `$Number$`(配 `duration`/`timescale`) | ✅ 支持 |
| `SegmentTemplate` + `$Time$` + `SegmentTimeline` | ✅ 支持 |
| `SegmentList`(`SegmentURL@media`) | ✅ 支持 |
| `SegmentBase`(单文件 + 字节区间) | ❌ 明确报错 |
| `SegmentURL@mediaRange` / `Initialization@range`(按字节范围) | ❌ 明确报错 |
| `ContentProtection`(DRM: Widevine / PlayReady / CENC) | ❌ 明确报错 |
| `type="dynamic"`(直播) | ❌ 明确报错 |
| `SegmentTimeline` 里 `r="-1"`(直到时段结束) | ❌ 明确报错 |

**为什么"明确拒绝"比"尽力而为"重要**: 这几类如果硬当成普通分片去下, 得到的不是
报错, 而是一个"能播几秒 / 打不开"的垃圾文件 —— DRM 流下下来是密文, SegmentBase
只下到第一个字节区间。用户拿到的是"下载成功但文件没用", 那比明确失败糟糕得多:
失败会让他换个办法, 假成功会让他以为已经拿到了。判据只取**能被证明**的部分,
认不出就拒绝 —— 与 `core/mediacheck.py` 同一条原则。

轨道选择
========
视频取 `bandwidth` 最高的 Representation, 音频同样。多语言音轨不做智能选择
(取最高码率那条), 但把"选了什么"如实写进 `notes` 带回去 —— 选择的依据要能被
看见, 否则"为什么下的是这条轨"无从复盘。
"""

from __future__ import annotations

import math
import re
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

#: 支持的寻址方式, 按优先级(规范里三者互斥, 同时出现时按这个顺序取)
_ADDRESSING = ("SegmentTemplate", "SegmentList", "SegmentBase")

_ISO_DUR_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.I,
)

#: `$Number$` / `$Number%05d$` / `$Time%08d$` —— 带宽度时按 DASH 规范左补零
_NUM_RE = re.compile(r"\$(Number|Time)(?:%0(\d+)d)?\$")

#: 跳过这些轨道: 字幕/缩略图轨不是"视频文件"的一部分, 混进来只会让合并步骤报错
_SKIP_KINDS = {"text", "image", "subtitle", "closedcaption"}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _iter(root, name):
    """按**本地名**遍历所有后代 —— MPD 基本都带默认命名空间, 直接比 tag 会全落空。"""
    for el in root.iter():
        if _local(el.tag) == name:
            yield el


def _kids(el, name):
    return [c for c in list(el) if _local(c.tag) == name]


def _kid(el, name):
    for c in list(el):
        if _local(c.tag) == name:
            return c
    return None


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return float(default)


def parse_duration(text):
    """ISO-8601 时长(`PT1H2M3.5S`) -> 秒; 认不出返回 None。

    ⚠️ 认不出返回 None 而不是 0: 调用方拿它算分片个数, 0 会推出"0 个分片",
    而那看起来像"这个视频是空的"—— 真相只是我们没读懂这个字段。
    """
    if not text:
        return None
    m = _ISO_DUR_RE.match(str(text).strip())
    if not m:
        return None
    g = m.groupdict()
    return (float(g["days"] or 0) * 86400.0 + float(g["hours"] or 0) * 3600.0
            + float(g["minutes"] or 0) * 60.0 + float(g["seconds"] or 0))


def _base_at(el, base):
    """把这一层的 `BaseURL` 拼到 base 上。

    BaseURL 可以出现在 MPD/Period/AdaptationSet/Representation 任一层, 而且是
    **逐层拼接**的(子层相对父层)。漏掉任何一层, 分片 URL 就会指到错的地方 ——
    症状是 404 一片, 而 MPD 本身看起来完全正常。
    规范允许多个 BaseURL(表示多路冗余), 取第一个是通行做法。
    """
    for node in _kids(el, "BaseURL"):
        txt = (node.text or "").strip()
        if txt:
            return urljoin(base, txt)
    return base


def _fill(template, rep_id="", number=None, time_value=None, bandwidth=None):
    """替换 SegmentTemplate 里的占位符。

    支持 `$RepresentationID$` / `$Number$` / `$Time$` / `$Bandwidth$`, 以及
    `$Number%05d$` 这种带零填充宽度的写法(不处理宽度就会生成 `1.m4s` 而不是
    `00001.m4s` —— 一整条轨全 404)。
    `$$` 是规范里的转义(`$` 字面量), 最后再还原。
    """
    out = str(template).replace("$$", "\x00")

    def sub(m):
        what, width = m.group(1), m.group(2)
        val = number if what == "Number" else time_value
        if val is None:
            return m.group(0)        # 模板用了这个占位符但调用方没给值: 原样留着
        text = str(int(val))
        return text.zfill(int(width)) if width else text

    out = _NUM_RE.sub(sub, out)
    out = out.replace("$RepresentationID$", str(rep_id or ""))
    if bandwidth is not None:
        out = out.replace("$Bandwidth$", str(int(bandwidth)))
    return out.replace("\x00", "$")


def _merged(el_name, chain):
    """从泛到具体合并同一类元素的属性。

    规范允许(也常见于真实 MPD)`SegmentTemplate` 在 Period/AdaptationSet 上给
    公共的 `media`/`timescale`, 在 Representation 上只覆盖 `startNumber`。
    只看最具体的那一层会丢掉 `media`; 只看最泛的会丢掉覆盖值。
    """
    attrs = {}
    nodes = []
    for node in chain:
        if node is None:
            continue
        el = _kid(node, el_name)
        if el is None:
            continue
        attrs.update({k: v for k, v in el.attrib.items()})
        nodes.append(el)
    return attrs, nodes


def _segments_from_template(attrs, nodes, rep, base, period_dur):
    media = attrs.get("media")
    if not media:
        return None, None               # 没有 media 模板: 交由别的寻址方式处理
    rep_id = rep.get("id", "")
    bandwidth = _int(rep.get("bandwidth"), 0) or None
    timescale = _float(attrs.get("timescale"), 1.0) or 1.0
    start = _int(attrs.get("startNumber"), 1)

    init = None
    init_tpl = attrs.get("initialization")
    if init_tpl:
        init = urljoin(base, _fill(init_tpl, rep_id=rep_id, bandwidth=bandwidth))

    timeline = None
    for node in nodes:                  # 取**最具体**的那条时间轴
        tl = _kid(node, "SegmentTimeline")
        if tl is not None:
            timeline = tl
    pairs = []                          # (time, number) 对
    if timeline is not None:
        cur = None
        for s in _kids(timeline, "S"):
            if s.get("t") is not None:
                cur = _int(s.get("t"), 0)
            dur = _int(s.get("d"), 0)
            if dur <= 0:
                raise ValueError(
                    "MPD 的 SegmentTimeline 里有 d<=0 的分段 —— 无法确定分片时长。"
                    " 这份 MPD 可能不是给普通点播用的。"
                )
            repeat = _int(s.get("r"), 0)
            if repeat < 0:
                raise ValueError(
                    "MPD 的 SegmentTimeline 用了 r=-1(表示'重复到时段结束')。"
                    " 它要先解出时段时长才能换算成分片个数, 本下载器不猜这个 ——"
                    " 猜错会少下尾部一截, 而且不报错。"
                )
            if cur is None:
                cur = 0
            for _ in range(repeat + 1):
                pairs.append((cur, start + len(pairs)))
                cur += dur
    else:
        seg_dur = _float(attrs.get("duration"), 0.0)
        if seg_dur <= 0:
            raise ValueError(
                "MPD 的 SegmentTemplate 既没有 SegmentTimeline, 也没有 duration/"
                "timescale —— 推不出分片个数。"
            )
        if period_dur is None:
            raise ValueError(
                "MPD 没有声明时长(mediaPresentationDuration / Period@duration),"
                " 而分片是靠 duration 均分的 —— 无法确定要下几片。"
            )
        count = max(1, int(math.ceil(period_dur * timescale / seg_dur)))
        pairs = [(int(i * seg_dur), start + i) for i in range(count)]

    if not pairs:
        raise ValueError("MPD 的 SegmentTimeline 是空的 —— 没有任何分片可下。")
    urls = [urljoin(base, _fill(media, rep_id=rep_id, number=n, time_value=t,
                                bandwidth=bandwidth)) for t, n in pairs]
    return init, urls


def _segments_from_list(nodes, base):
    if not nodes:
        return None, None
    el = nodes[-1]                      # 最具体的那一层
    init = None
    init_el = _kid(el, "Initialization")
    if init_el is not None:
        if not init_el.get("sourceURL") and init_el.get("range"):
            raise ValueError(
                "MPD 用 SegmentList 的 Initialization@range 按字节区间取初始化段。"
                " 本下载器按整文件下载, 不做字节区间请求。"
            )
        src = init_el.get("sourceURL")
        if src:
            init = urljoin(base, src)
    urls = []
    for su in _kids(el, "SegmentURL"):
        if su.get("mediaRange"):
            raise ValueError(
                "MPD 用 SegmentURL@mediaRange 按字节区间分片(整文件 + 区间索引)。"
                " 本下载器按整文件下载, 不做字节区间请求。"
            )
        media = su.get("media")
        if not media:
            raise ValueError("MPD 的 SegmentURL 既没有 media, 也不支持 range 寻址。")
        urls.append(urljoin(base, media))
    if not urls:
        raise ValueError("MPD 的 SegmentList 没有任何 SegmentURL。")
    return init, urls


def _track_kind(adapt, rep):
    ct = (rep.get("contentType") or adapt.get("contentType") or "").strip().lower()
    if ct in ("video", "audio"):
        return ct
    mime = (rep.get("mimeType") or adapt.get("mimeType") or "").strip().lower()
    for kind, prefix in (("video", "video/"), ("audio", "audio/")):
        if mime.startswith(prefix):
            return kind
    if mime.startswith("text/") or mime.startswith("application/ttml"):
        return "text"
    if mime.startswith("image/"):
        return "image"
    # 退一步看 codecs: `stpp`/`wvtt` 是字幕, 别把它当视频轨下下来
    codecs = (rep.get("codecs") or adapt.get("codecs") or "").strip().lower()
    if codecs in ("stpp", "wvtt", "tx3g"):
        return "text"
    if ct.startswith("text") or ct.startswith("image"):
        return ct
    return ""


def _representation(adapt, rep, period, mpd_base, period_dur, notes):
    """把一条 Representation 解析成可下载的分片清单, 认不出抛 ValueError。"""
    base = _base_at(rep, _base_at(adapt, _base_at(period, mpd_base)))
    chain = [period, adapt, rep]
    found = {}
    for name in _ADDRESSING:
        found[name] = _merged(name, chain)

    if found["SegmentTemplate"][0]:
        init, urls = _segments_from_template(found["SegmentTemplate"][0],
                                            found["SegmentTemplate"][1],
                                            rep, base, period_dur)
        if urls:
            return init, urls
    if found["SegmentList"][0]:
        init, urls = _segments_from_list(found["SegmentList"][1], base)
        if urls:
            return init, urls
    if found["SegmentBase"][0]:
        raise ValueError(
            "MPD 用 SegmentBase(整文件 + 索引区间)寻址。本下载器按整文件下载,"
            " 不做字节区间请求 —— 硬当普通分片下只会得到一小段垃圾数据。"
        )
    raise ValueError(
        "MPD 里这条 Representation 没有 SegmentTemplate / SegmentList,"
        " 认不出分片怎么寻址。"
    )


def parse_mpd(text, mpd_url):
    """解析 MPD。失败抛 `ValueError`, 消息是**写给用户看的**(不含栈/内部术语)。

    成功返回::

        {"duration": float|None,
         "video": {track}|None, "audio": {track}|None,
         "notes": [str]}

    track = {"segments": [url...], "init": url|None, "mime": str,
             "bandwidth": int, "width": int|None, "height": int|None,
             "lang": str, "codecs": str}

    ⚠️ `init` 是 fMP4 的**初始化段**, 必须排在分片前面一起合并 —— 少了它, 拼出来
    的文件没有任何轨道元数据, 播放器表现为"文件坏了"。
    """
    if not text or not str(text).strip():
        raise ValueError("MPD 是空的(源站返回了 0 字节)")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(
            f"MPD 不是合法的 XML: {e} —— 可能拿到的是错误页而不是清单。"
        ) from e
    if _local(root.tag) != "MPD":
        raise ValueError(
            f"根元素是 <{_local(root.tag)}>, 不是 <MPD> —— 这个 URL 返回的不是 MPD。"
        )

    mtype = (root.get("type") or "static").strip().lower()
    if mtype == "dynamic":
        raise ValueError(
            "MPD 声明 type=\"dynamic\"(直播/长时段流)。本下载器只处理点播"
            "(static): 直播没有确定的结束点, 按分片清单下会得到一个一直长不大的文件。"
        )

    # DRM: 只要出现 ContentProtection 就拒绝。它的子元素里可能有 PSSH 数据,
    # 看上去"结构完整", 但内容是密文 —— 下完打不开, 而且不报错。
    prot = list(_iter(root, "ContentProtection"))
    if prot:
        schemes = []
        for el in prot:
            uri = (el.get("schemeIdUri") or "").strip()
            if uri and uri not in schemes:
                schemes.append(uri)
        detail = ", ".join(schemes[:3]) or "未标明 schemeIdUri"
        raise ValueError(
            f"MPD 声明了内容保护(DRM): {detail}。下载得到的是密文, 本工具无法解密 ——"
            f" 这不是下载故障, 是版权保护, 请用官方客户端观看。"
        )

    duration = parse_duration(root.get("mediaPresentationDuration"))
    notes = []
    periods = _kids(root, "Period") or [root]
    if len(periods) > 1:
        # 多时段意味着同一部片由若干段拼成(常见于带广告插播的点播)。取第一段
        # 会少下后面全部 —— 与其静默少下, 不如说清边界。
        raise ValueError(
            f"MPD 有 {len(periods)} 个 Period(时段)。本下载器只处理单时段点播;"
            f" 多时段需要按时段分别拼接, 这里不猜。"
        )

    best = {"video": None, "audio": None}
    skipped = []
    for period in periods:
        p_base = _base_at(period, mpd_url)
        p_dur = parse_duration(period.get("duration"))
        if p_dur is None:
            p_dur = duration
        for adapt in _kids(period, "AdaptationSet") or [period]:
            reps = _kids(adapt, "Representation")
            if not reps:
                continue
            lang = (adapt.get("lang") or "").strip()
            for rep in reps:
                kind = _track_kind(adapt, rep)
                if kind in _SKIP_KINDS or not kind:
                    skipped.append(kind or "unknown")
                    continue
                init, segs = _representation(adapt, rep, period, p_base, p_dur, notes)
                mime = (rep.get("mimeType") or adapt.get("mimeType")
                        or ("video/mp4" if kind == "video" else "audio/mp4"))
                track = {
                    "segments": segs,
                    "init": init,
                    "mime": str(mime).strip().lower(),
                    "bandwidth": _int(rep.get("bandwidth"), 0),
                    "width": _int(rep.get("width"), 0) or None,
                    "height": _int(rep.get("height"), 0) or None,
                    "lang": lang,
                    "codecs": (rep.get("codecs") or adapt.get("codecs") or "").strip(),
                    "rep_id": rep.get("id") or "",
                }
                # 一条轨可能有多个 Representation(不同码率), 这里只留最高码率的
                if (best[kind] is None
                        or track["bandwidth"] > (best[kind]["bandwidth"] or 0)):
                    best[kind] = track

    if best["video"] is None and best["audio"] is None:
        if skipped:
            kinds = ", ".join(sorted(set(skipped)))
            raise ValueError(
                f"MPD 里只有 {kinds} 轨(字幕/缩略图), 没有视频或音频轨可下。"
            )
        raise ValueError(
            "MPD 里没有任何可下载的 Representation(视频/音频轨都是空的)。"
        )

    for kind in ("video", "audio"):
        t = best[kind]
        if t is None:
            continue
        desc = [f"{kind}: {len(t['segments'])} 片"]
        if t["height"]:
            desc.append(f"{t['width']}x{t['height']}")
        if t["bandwidth"]:
            desc.append(f"{t['bandwidth'] // 1000} kbps")
        if t["lang"]:
            desc.append(f"lang={t['lang']}")
        desc.append("带初始化段" if t["init"] else "无初始化段")
        notes.append(" / ".join(desc))
    if best["audio"] is None and best["video"] is not None:
        notes.append("没有音频轨(该 MPD 只有视频)")

    return {"duration": duration, "video": best["video"], "audio": best["audio"],
            "notes": notes}
