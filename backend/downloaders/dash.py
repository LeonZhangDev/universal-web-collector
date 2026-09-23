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
| `SegmentBase`(单文件 + 字节区间) | ✅ 支持(有 `indexRange` 就读 sidx 切分片, 没有则整文件算一段) |
| `SegmentURL@mediaRange` / `Initialization@range`(按字节范围) | ✅ 支持 |
| 多个 `Period`(时段) | ✅ 支持(按时段分段下载, 由上层逐轨拼起来) |
| `ContentProtection`(DRM: Widevine / PlayReady / CENC) | ❌ 明确报错 |
| `type="dynamic"`(直播) | ✅ 支持 —— 但产出是**一段录制**, 见下节 |
| `SegmentTimeline` 里 `r="-1"`(直到时段结束) | ✅ 点播报错 / 直播按窗口末端展开 |
| `sidx` 里的 `reference_type=1`(嵌套索引) | ✅ 支持(递归展开, 有层数与条数上限) |

**为什么"明确拒绝"比"尽力而为"重要**: 这几类如果硬当成普通分片去下, 得到的不是
报错, 而是一个"能播几秒 / 打不开"的垃圾文件 —— DRM 流下下来是密文。用户拿到的是
"下载成功但文件没用", 那比明确失败糟糕得多: 失败会让他换个办法, 假成功会让他以为
已经拿到了。判据只取**能被证明**的部分, 认不出就拒绝 —— 与 `core/mediacheck.py`
同一条原则。

直播(`type="dynamic"`)
=====================
这里做的是**录制, 不是"把整个流下完"** —— 直播没有"下完"这回事。所以本模块只负责
把"此刻的可用窗口"翻成一份明确的分片清单, "录多久"由下载层(时间上限)决定。

窗口按 `availabilityStartTime` + 当前时间 + `timeShiftBufferDepth` 推算, 三种情况:

  * `SegmentTimeline` 里**显式列出**的段(r>=0) —— 清单自己就声明了可用范围,
    **以清单为准**, 不用时钟去裁(时钟偏一点就会把有效分片裁掉);
  * `SegmentTimeline` 里 `r="-1"`(重复到末尾) —— 只有这种情况**必须**用时钟:
    点播里"末尾"= 时段末尾, 直播里"末尾"= 可用窗口末尾。用错会永远只录到第一次
    取回的那几片, 而且不报错;
  * `SegmentTemplate@duration` 均分 —— 清单没声明任何分片, 个数只能由窗口算:
    `[窗口起点, 窗口末尾]` 里能被 `duration` 整除的那些。⚠️ 这种形态**必须**有
    `timeShiftBufferDepth`, 否则"窗口从哪儿开始"就是未知的(按 0 算会算出几万片,
    全是早就滚走的 404)。

时钟取 `now` 参数, 没给就退回清单自己的 `publishTime` —— 那份时间戳的定义就是
"编码器生成这份清单的时刻", 正是我们要的"现在"。两个都没有时**不猜**: 能按清单
声明下的照下, 需要时钟才能算的**明确报错**。

字节范围寻址(`SegmentBase` / `mediaRange`)
=========================================
这里能读懂两种"地址":

  * **URL** —— 一片一个文件;
  * **`(URL, (起, 止))`** —— 同一个文件里的字节区间(闭区间, 直接当 `Range` 头用)。

第二种是 `SegmentBase`(整文件 + 索引)与 `SegmentList@mediaRange` 的形态。⚠️ 只有
`BaseURL` **显式写出来**时才敢这么下: 没写的话所谓"文件"就是 MPD 自己所在的位置,
拿它当媒体去请求会下回一份 XML —— 所以那种情况**报错**而不是猜。

`sidx`(Segment Index)按 ISO/IEC 14496-12 §8.16.3 解析: 第一段的起点是
**sidx box 结束处 + `first_offset`**, 之后每段紧接着上一段。这不是"差不多"能对付
的 —— 起点差一个 box 头就会把每一片都切错位, 拼出来是"长度对得上、能播、
但每隔几秒糊一下"的东西。

`sidx` 拿不到时(没写 `indexRange`)退回"整个文件算一段": 对 `SegmentBase` 而言那个
文件**就是**这条轨的完整内容, 下全了就是对的 —— 只是没法并行、也没法按片续传。

多时段(多 `Period`)
===================
一部片由几段拼成(带片头/广告插播的点播很常见)。**取第一段就是静默少下后面全部**,
所以必须支持, 且必须在**逐轨**这一层拼: 每个时段的视频轨与音频轨各自独立, 混着拼会
得到音画错位。这里只负责把结构解析出来(每段各自的分片清单), 拼接交给下载层 ——
解析模块不碰网络也不碰 ffmpeg。

轨道选择
========
视频取 `bandwidth` 最高的 Representation, 音频同样。多语言音轨不做智能选择
(取最高码率那条), 但把"选了什么"如实写进 `notes` 带回去 —— 选择的依据要能被
看见, 否则"为什么下的是这条轨"无从复盘。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

#: 支持的寻址方式, 按优先级(规范里三者互斥, 同时出现时按这个顺序取)
#: `SegmentTemplate` 常见于"每片一个文件", `SegmentList`/`SegmentBase` 则是
#: "整文件 + 字节区间"那一类 —— 后者返回的地址是 `(URL, (起, 止))`。
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

#: 直播窗口一次最多枚举多少片。⚠️ 这不是性能参数而是**防呆**: 窗口是由
#: 时间减出来的, 一旦 `timeShiftBufferDepth` 缺失或时钟错得离谱, 算出来就是几万片,
#: 请求全打出去换回一堆 404。超过上限**明确报错**, 不裁剪 —— 裁剪等于静默少录。
LIVE_WINDOW_MAX_SEGMENTS = 5000


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


def parse_iso_time(text):
    """ISO-8601 时间戳(`2026-09-23T10:00:00Z`) -> epoch 秒; 认不出返回 `None`。

    ⚠️ 认不出时返回 `None`, 而**不是**"当作 0": 它被用来减出直播窗口的起点, 当成 0
    会算出一个"从上世纪开始"的窗口, 于是请求一大片早就滚走的分片(全 404), 报错会
    指向"下载失败"而不是"这个字段没读懂"。

    规范里 MPD 的时间都是 UTC。没带时区的按 UTC 解释(而不是本地时区 —— 本机在
    UTC+8, 按本地解释会让窗口整体偏 8 小时)。
    """
    if not text:
        return None
    s = str(text).strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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


def _range_of(text):
    """`"1234-5678"` -> `(1234, 5678)`。闭区间, 与 `Range: bytes=a-b` 同义。

    ⚠️ 认不出返回 `None` 而**不是**猜一个。DASH 的 range 属性只允许 `a-b` 这种
    形式(没有 `a-` 这种开放式写法); 出现别的说明这份 MPD 不是标准生成的 ——
    按猜出来的区间去请求, 得到的是"另一段字节"而不是报错。
    """
    s = str(text or "").strip()
    if not s or "-" not in s:
        return None
    a, _, b = s.partition("-")
    try:
        start, end = int(a), int(b)
    except ValueError:
        return None
    if start < 0 or end < start:
        return None
    return start, end


def _has_base(el):
    """这一层有没有**显式**写出 `BaseURL`。"""
    for node in _kids(el, "BaseURL"):
        if (node.text or "").strip():
            return True
    return False


def parse_sidx_refs(blob, box_start=0):
    """解析 Segment Index(`sidx`)box, 按**引用顺序**返回 `[(kind, (起, 止)), ...]`。

    `kind` 两种:

      * `"media"` —— 一段媒体字节, 可以直接下;
      * `"index"` —— **嵌套的下一层索引**(`reference_type=1`)。它本身不是媒体:
        取回那一小段字节再解一次才是。硬当媒体拼进去得到的是"索引的原始字节",
        长度对得上、能播、内容是一堆二进制垃圾 —— 所以必须区分。

    `blob` 是**从 box 头开始**取回的那段字节(`indexRange` 请求的结果),
    `box_start` 是它在整个文件里的绝对偏移。

    按 ISO/IEC 14496-12 §8.16.3:

        size(4) type(4)='sidx' version(1) flags(3) reference_ID(4) timescale(4)
        [version==1: earliest_presentation_time(8) first_offset(8)
         version==0: earliest_presentation_time(4) first_offset(4)]
        reserved(2) reference_count(2)
        reference_count × { type(1bit)+size(31bit) | duration(4) | SAP(4) }

    第一段起点 = **sidx box 结束处 + first_offset**, 之后每段紧接上一段。

    ⚠️ 两种引用**在文件里是同一条字节流的前后顺序**: 嵌套索引的区间与它展开出来的
    媒体区间是紧挨着的(嵌套索引 box 之后跟着它索引的那些媒体)。所以调用方要按返回
    顺序**原地**处理 —— 遇到 `index` 就先把那一层走完, 再回到父层的下一条引用。
    先收集父层全部媒体、事后再补子层的话, 子层分片会被排到父层后半段之后, 拼出来
    是错位的文件(长度对得上、能播)。
    """
    b = bytes(blob or b"")
    if len(b) < 12:
        raise ValueError(
            "Segment Index(sidx)太短, 读不出 box 头 —— MPD 的 indexRange 可能取错了位置。"
        )
    box_size = int.from_bytes(b[0:4], "big")
    if b[4:8] != b"sidx":
        typ = b[4:8].decode("ascii", "replace")
        raise ValueError(
            f"索引区间的第一个 box 是 {typ!r}, 不是 'sidx' ——"
            f" 这份 MPD 声明的 indexRange 与实际文件对不上。"
        )
    if box_size == 1:
        if len(b) < 20:
            raise ValueError("sidx 声明了 64 位长度, 但取回的字节不够读它。")
        box_size = int.from_bytes(b[8:16], "big")
        p = 16
    else:
        p = 8
    if box_size <= p or box_size > len(b):
        raise ValueError(
            f"sidx 自报长度 {box_size} 字节, 而实际取回 {len(b)} 字节 ——"
            f" 索引区间被截断了, 按它算出来的分片偏移会全错。"
        )
    version = b[p]
    p += 4                                  # version(1) + flags(3)
    p += 4                                  # reference_ID
    p += 4                                  # timescale
    if p + (16 if version == 1 else 8) + 4 > len(b):
        raise ValueError("sidx 在读到 first_offset 之前就结束了。")
    if version == 1:
        p += 8
        first_offset = int.from_bytes(b[p:p + 8], "big")
        p += 8
    else:
        p += 4
        first_offset = int.from_bytes(b[p:p + 4], "big")
        p += 4
    p += 2                                  # reserved
    count = int.from_bytes(b[p:p + 2], "big")
    p += 2
    if count <= 0:
        raise ValueError("sidx 里没有任何分片引用 —— 这份索引是空的。")
    if p + count * 12 > len(b):
        raise ValueError(
            f"sidx 声明 {count} 个分片引用, 但取回的字节只够读 "
            f"{(len(b) - p) // 12} 个 —— 索引区间取少了。"
        )
    cur = box_start + box_size + first_offset
    out = []
    for _ in range(count):
        w0 = int.from_bytes(b[p:p + 4], "big")
        p += 12
        size = w0 & 0x7FFFFFFF
        if size <= 0:
            raise ValueError("sidx 里有 0 字节的分片引用 —— 这份索引不可信。")
        out.append(("index" if w0 >> 31 else "media", (cur, cur + size - 1)))
        cur += size
    return out


def parse_sidx(blob, box_start=0):
    """只认媒体段的单层视图: 返回 `[(起, 止), ...]`(闭区间, 绝对偏移)。

    ⚠️ 出现嵌套索引(`reference_type=1`)时**报错**而不是忽略它 —— 忽略会静默少下
    一整段内容(嵌套索引覆盖的那些分片全部没下), 长度对得上但内容是断的。要支持
    嵌套的调用方用 `parse_sidx_refs` 自己递归展开(见 `video.py::_expand_sidx`)。
    """
    refs = parse_sidx_refs(blob, box_start)
    if any(k == "index" for k, _ in refs):
        raise ValueError(
            "sidx 用了嵌套索引引用(reference_type=1)。展开它要先请求下一层索引,"
            " 本调用方不做 —— 硬当成媒体段拼进去得到的是索引的原始字节。"
        )
    return [r for _, r in refs]


def _segments_from_template(attrs, nodes, rep, base, period_dur, win=None):
    """`SegmentTemplate` -> `(init, [分片 URL...])`。

    `win` 只在**直播**时有值, 形如 `{"start": 秒|None, "end": 秒|None}`, 是相对于
    本周起点的可用窗口。点播(`win is None`)的行为与既往完全一致。
    """
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
        # ⚠️ 直播里**不**拿时钟去裁显式列出的段: 清单既然一条条写出来了, 那就是
        # 它声明可用的范围。用时钟裁的话, 时钟偏一点(或 tsbd 填得比实际小)就会把
        # 本该录的有效分片裁掉 —— 那是静默少录。
        entries = _kids(timeline, "S")
        cur = None
        for i, s in enumerate(entries):
            if s.get("t") is not None:
                cur = _int(s.get("t"), 0)
            dur = _int(s.get("d"), 0)
            if dur <= 0:
                raise ValueError(
                    "MPD 的 SegmentTimeline 里有 d<=0 的分段 —— 无法确定分片时长。"
                    " 这份 MPD 可能不是给普通点播用的。"
                )
            if cur is None:
                cur = 0
            repeat = _int(s.get("r"), 0)
            if repeat >= 0:
                count = repeat + 1
            else:
                # `r="-1"`: 重复到"下一段的起点"为止; 最后一段则重复到**末尾**。
                # 点播里那个"末尾"是时段末尾(由 Period@mediaPresentationDuration 定),
                # 直播里则是**可用窗口末尾** —— 这两个值不一样, 用错会永远只录到
                # 第一次取回的那几片, 而且不报错。
                nxt = entries[i + 1].get("t") if i + 1 < len(entries) else None
                if nxt is not None:
                    limit = _int(nxt, 0)
                elif win is not None and win.get("end") is not None:
                    # 窗口末尾按 timescale 换算, 再加一格: 起点恰好落在末尾上的
                    # 那一片仍然是有内容的(它覆盖 [t, t+d), 只要 t 早于末尾就算)。
                    limit = int(math.floor(win["end"] * timescale)) + 1
                else:
                    if win is None:
                        raise ValueError(
                            "MPD 的 SegmentTimeline 用了 r=-1(表示'重复到时段末尾')。"
                            " 它要先解出时段时长才能换算成分片个数, 本下载器不猜这个 ——"
                            " 猜错会少下尾部一截, 而且不报错。"
                        )
                    raise ValueError(
                        "直播清单的 SegmentTimeline 用了 r=-1(重复到可用窗口末尾),"
                        " 但推不出窗口 —— 需要 availabilityStartTime 与"
                        " publishTime/当前时间。"
                        " 不猜: 猜错会少录尾部一截, 而且不报错。"
                    )
                # 段起点 t, t+d, t+2d ... 取所有 t < limit 的
                count = max(0, -(-(limit - cur) // dur)) if limit > cur else 0
            for _ in range(count):
                pairs.append((cur, start + len(pairs)))
                cur += dur
    else:
        seg_dur = _float(attrs.get("duration"), 0.0)
        if seg_dur <= 0:
            raise ValueError(
                "MPD 的 SegmentTemplate 既没有 SegmentTimeline, 也没有 duration/"
                "timescale —— 推不出分片个数。"
            )
        if win is not None and win.get("end") is not None:
            # 直播 + 均分时长: 清单里一个分片都没写, 个数**只能**由窗口算。
            # 起点取 timeShiftBufferDepth 划出的那一段 —— 缺它时窗口起点 = 直播点,
            # 也就是"从此刻开始录"(而不是从头补全整段历史, 那既下不到也存不下)。
            seg_dur_s = seg_dur / timescale
            end_s = win["end"]
            start_s = win.get("start")
            if start_s is None:
                start_s = end_s
            first_off = max(0, int(math.floor(start_s / seg_dur_s)))
            last_off = int(math.floor(end_s / seg_dur_s))
            count = last_off - first_off + 1
            if count > LIVE_WINDOW_MAX_SEGMENTS:
                raise ValueError(
                    f"直播窗口按 timeShiftBufferDepth 算出来有 {count} 个分片,"
                    f" 超过上限 {LIVE_WINDOW_MAX_SEGMENTS} ——"
                    f" 多半是 timeShiftBufferDepth 缺失或时钟对不上。"
                    f" 这里明确报错而不是裁掉一部分: 裁掉就是静默少录。"
                )
            if count <= 0:
                raise ValueError(
                    "直播的可用窗口里没有任何分片 —— 流可能还没开始,"
                    " 或 availabilityStartTime 比当前时间还晚。"
                )
            pairs = [(int((first_off + i) * seg_dur), start + first_off + i)
                     for i in range(count)]
        else:
            if period_dur is None:
                raise ValueError(
                    "MPD 没有声明时长(mediaPresentationDuration / Period@duration),"
                    " 而分片是靠 duration 均分的 —— 无法确定要下几片。"
                    " (直播需要 availabilityStartTime + 当前时间才能算出窗口。)"
                )
            count = max(1, int(math.ceil(period_dur * timescale / seg_dur)))
            pairs = [(int(i * seg_dur), start + i) for i in range(count)]

    if not pairs:
        raise ValueError("MPD 的 SegmentTimeline 是空的 —— 没有任何分片可下。")
    urls = [urljoin(base, _fill(media, rep_id=rep_id, number=n, time_value=t,
                                bandwidth=bandwidth)) for t, n in pairs]
    return init, urls


def _segments_from_list(nodes, base):
    """`SegmentList` -> `(init, [地址...])`。地址可以是 URL, 也可以是
    `(URL, (起, 止))` —— 后者是 `mediaRange` / `Initialization@range` 的形态。"""
    if not nodes:
        return None, None
    el = nodes[-1]                      # 最具体的那一层
    init = None
    init_el = _kid(el, "Initialization")
    if init_el is not None:
        src = init_el.get("sourceURL")
        rng = _range_of(init_el.get("range"))
        if rng is not None:
            # 按字节区间取初始化段。⚠️ 缺 sourceURL 时区间作用在 base 上 ——
            # 那正是"Representation 的那个文件", 与 SegmentBase 同一条语义。
            target = urljoin(base, src) if src else base
            init = (target, rng)
        elif src:
            init = urljoin(base, src)
    urls = []
    for su in _kids(el, "SegmentURL"):
        media = su.get("media")
        rng = _range_of(su.get("mediaRange"))
        if rng is not None:
            target = urljoin(base, media) if media else base
            urls.append((target, rng))
            continue
        if not media:
            raise ValueError(
                "MPD 的 SegmentURL 既没有 media, 也没有能认出来的 mediaRange。"
            )
        urls.append(urljoin(base, media))
    if not urls:
        raise ValueError("MPD 的 SegmentList 没有任何 SegmentURL。")
    return init, urls


def _segments_from_base(el, base, has_base):
    """`SegmentBase`(整文件 + 索引区间) -> `(init, 分片地址, 索引描述)`。

    三种情况:
      * 有 `indexRange` —— 返回 `index` 描述, 由**下载层**取回 sidx 再算出分片
        (解析模块不碰网络, 所以这里只能给"去哪儿取索引");
      * 没有 `indexRange` —— 整个文件算一段。对 `SegmentBase` 而言那个文件
        **就是**这条轨的完整内容, 下全了就是对的;
      * `BaseURL` 没显式写 —— **报错**。此时"文件"就是 MPD 自己所在的位置,
        拿它当媒体请求会下回一份 XML, 而字节数是"有内容"的(所以不会被发现)。
    """
    if not has_base:
        raise ValueError(
            "MPD 用 SegmentBase 寻址, 但这条 Representation 没有写 BaseURL ——"
            " 无法确定媒体文件的地址(此时它指向的是 MPD 自己的位置)。"
            " 这份 MPD 不是标准生成的, 本下载器不猜。"
        )
    init = None
    init_el = _kid(el, "Initialization")
    if init_el is not None:
        rng = _range_of(init_el.get("range"))
        src = init_el.get("sourceURL")
        target = urljoin(base, src) if src else base
        if rng is not None:
            init = (target, rng)
        elif src:
            init = target
    index_range = _range_of(el.get("indexRange"))
    if index_range is not None:
        return init, [], {"kind": "sidx", "media": base, "range": index_range}
    # 没有索引: 整个文件就是"一段", 而它**自带**初始化段(ftyp+moov 就在开头)。
    # ⚠️ 此时必须把**指向同一个文件**的 Initialization 丢掉 —— 它和文件开头是
    # 同一批字节, 拼到前面会把 moov 放两遍。播放器遇到第二个 moov 的行为不确定,
    # 而字节数是"看起来没问题"的。判据按**目标文件**比, 不按"有没有区间":
    # `sourceURL` 指向另一个文件的初始化段是必须留下的。
    if init is not None:
        target = init[0] if isinstance(init, tuple) else init
        if target == base:
            init = None
    return init, [base], None


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


def _representation(adapt, rep, period, mpd_base, period_dur, notes, win=None):
    """把一条 Representation 解析成可下载的分片清单。

    返回 `(init, 地址列表, 索引描述)`。认不出抛 `ValueError`。
    `地址列表` 的每个元素是 URL 或 `(URL, (起, 止))`; `索引描述` 非空时表示
    "分片要先取回 sidx 才算得出来"(见 `parse_sidx`), 由下载层完成。
    `win` 只在直播时非空, 见 `_segments_from_template`。
    """
    base = _base_at(rep, _base_at(adapt, _base_at(period, mpd_base)))
    has_base = any(_has_base(x) for x in (period, adapt, rep))
    chain = [period, adapt, rep]
    found = {}
    for name in _ADDRESSING:
        found[name] = _merged(name, chain)

    if found["SegmentTemplate"][0]:
        init, urls = _segments_from_template(found["SegmentTemplate"][0],
                                            found["SegmentTemplate"][1],
                                            rep, base, period_dur, win)
        if urls:
            return init, urls, None
    if found["SegmentList"][0]:
        init, urls = _segments_from_list(found["SegmentList"][1], base)
        if urls:
            return init, urls, None
    if found["SegmentBase"][1]:
        # ⚠️ 判"有没有这个元素"看 `[1]`(元素列表)而不是 `[0]`(合并后的属性):
        # `<SegmentBase/>` 这种没有属性的写法是合法的, 而 `{}` 是假值 ——
        # 用它当判据会直接跳到最后的"认不出怎么寻址", 报错离真相很远。
        init, urls, index = _segments_from_base(found["SegmentBase"][1][-1],
                                               base, has_base)
        if urls or index:
            return init, urls, index
    raise ValueError(
        "MPD 里这条 Representation 没有 SegmentTemplate / SegmentList /"
        " SegmentBase, 认不出分片怎么寻址。"
    )


def parse_mpd(text, mpd_url, now=None, allow_dynamic=False):
    """解析 MPD。失败抛 `ValueError`, 消息是**写给用户看的**(不含栈/内部术语)。

    成功返回::

        {"duration": float|None,
         "periods": [period, ...],          # 至少一个
         "video": track|None, "audio": track|None,   # 只有**单时段**时才有值
         "live": bool,                      # 这份清单是直播(type="dynamic")
         "minimum_update_period": float|None,   # 直播: 清单多久刷新一次
         "time_shift_buffer_depth": float|None, # 直播: 可回溯窗口有多长
         "notes": [str]}

    period = {"video": track|None, "audio": track|None, "duration": float|None,
              "id": str}

    track = {"segments": [地址...], "init": 地址|None, "index": 描述|None,
             "mime": str, "bandwidth": int, "width": int|None, "height": int|None,
             "lang": str, "codecs": str, "rep_id": str}

    地址 = URL 字符串, 或 `(URL, (起, 止))`(同一文件里的字节区间, 闭区间)。

    ⚠️ `init` 是 fMP4 的**初始化段**, 必须排在分片前面一起合并 —— 少了它, 拼出来
    的文件没有任何轨道元数据, 播放器表现为"文件坏了"。

    ⚠️ 多时段时顶层 `video`/`audio` 是 `None`, 只读第一条轨的调用方会拿到空值而
    **自己炸**, 而不是静默少下后面全部 —— 这是故意的(见模块 docstring 末节)。

    `now`(epoch 秒)只在直播时用到: 没给就退回清单自己的 `publishTime`。两个都没有
    时, 需要时钟才能算出来的形态会**明确报错**(见模块 docstring 的直播一节)。

    `allow_dynamic` 默认 False —— 解析器不替调用方决定"录不录直播"。关着的时候
    遇到 `type="dynamic"` 仍然报错, 这是给"只想拿点播清单"的调用方(和旧行为)留的。
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
    live = mtype == "dynamic"
    if live and not allow_dynamic:
        raise ValueError(
            "MPD 声明 type=\"dynamic\"(直播/长时段流), 而这次调用没有开启直播录制。"
            " 直播没有确定的结束点: 按分片清单一路下会得到一个一直长不大的文件。"
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

    # ---- 直播: 先定出"现在" ----
    # ⚠️ 时钟只有一个来源, 优先级明确写在这里: 调用方给的 now 最准(它就在下载的
    # 那一刻), 其次是清单的 publishTime(编码器生成这份清单的时刻, 定义上就是
    # "这份清单的现在")。都没有就是 None —— 需要它的形态会报错, 不需要的照下。
    clock = None
    live_meta = {"minimum_update_period": None, "time_shift_buffer_depth": None}
    if live:
        published = parse_iso_time(root.get("publishTime"))
        clock = now if now is not None else published
        live_meta["minimum_update_period"] = parse_duration(
            root.get("minimumUpdatePeriod"))
        live_meta["time_shift_buffer_depth"] = parse_duration(
            root.get("timeShiftBufferDepth"))
        ast = parse_iso_time(root.get("availabilityStartTime"))
        if clock is None:
            notes.append(
                "直播: 清单没有 publishTime, 调用方也没给当前时间 ——"
                " 只有把分片一条条写出来的清单能下"
            )
        elif ast is None:
            clock = None
            notes.append(
                "直播: 清单没有 availabilityStartTime, 推不出可用窗口 ——"
                " 只有把分片一条条写出来的清单能下"
            )

    parsed = []
    skipped = []
    for idx, period in enumerate(periods):
        p_base = _base_at(period, mpd_url)
        p_dur = parse_duration(period.get("duration"))
        if p_dur is None:
            p_dur = duration
        win = None
        if live:
            p_start = parse_duration(period.get("start")) or 0.0
            w_end = None
            if clock is not None:
                w_end = clock - parse_iso_time(root.get("availabilityStartTime")) - p_start
            tsbd = live_meta["time_shift_buffer_depth"]
            # ⚠️ 窗口起点: 有 tsbd 就回溯那么长(**这是特性** —— 中途接入直播能补上
            # 刚才那几分钟), 没有就把起点放在直播点上, 也就是"从现在开始录"。
            # 绝不用 0 当起点: 那等于"从流的开头补全", 下不到还全是 404。
            w_start = (max(0.0, w_end - tsbd) if tsbd is not None
                       else w_end) if w_end is not None else None
            win = {"start": w_start, "end": w_end}
        best = {"video": None, "audio": None}
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
                init, segs, index = _representation(
                    adapt, rep, period, p_base, p_dur, notes, win
                )
                mime = (rep.get("mimeType") or adapt.get("mimeType")
                        or ("video/mp4" if kind == "video" else "audio/mp4"))
                track = {
                    "segments": segs,
                    "init": init,
                    # 非空表示"分片要先取索引才算得出来" —— 由下载层完成
                    "index": index,
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
        parsed.append({
            "video": best["video"],
            "audio": best["audio"],
            "duration": p_dur,
            "id": period.get("id") or ("" if len(periods) == 1 else f"p{idx + 1}"),
        })

    # ⚠️ 每个时段都必须有可下的轨。某个时段是空的(常见于"纯广告时段只有图片轨")
    # 时**报错**而不是跳过它 —— 跳过会让成品少一段, 而长度对不上要到播放时才发现。
    for i, p in enumerate(parsed):
        if p["video"] is None and p["audio"] is None:
            if skipped:
                kinds = ", ".join(sorted(set(skipped)))
                raise ValueError(
                    f"MPD 第 {i + 1} 个时段里只有 {kinds} 轨(字幕/缩略图),"
                    f" 没有视频或音频轨可下。"
                )
            raise ValueError(
                f"MPD 第 {i + 1} 个时段里没有任何可下载的 Representation。"
            )

    if len(parsed) > 1:
        notes.append(
            f"这次是 {len(parsed)} 个时段(Period), 会按时段分别下载再逐轨拼接"
        )
        p_durs = [p["duration"] for p in parsed]
        if any(d is None for d in p_durs):
            # 少一段时长不影响下载(分片清单是明确的), 只影响落盘后的时长终检
            notes.append("部分时段没有声明时长, 落盘后的时长终检会跳过")
        elif duration is not None:
            total = sum(p_durs)
            if abs(total - duration) > 1.0:
                notes.append(
                    f"各时段时长之和 {total:.1f}s 与 MPD 声明的总时长 "
                    f"{duration:.1f}s 不一致, 以分片清单为准"
                )

    # 单时段时把 `video`/`audio` 直接暴露出来(老调用方与人工看 JSON 都方便);
    # ⚠️ 多时段时它们是 `None` —— 这是**故意**的: 想让"只读第一条轨"的调用方
    # 拿到空值后自己炸, 而不是静默少下后面全部。
    single = parsed[0] if len(parsed) == 1 else None
    best = single or {"video": None, "audio": None}

    for kind in ("video", "audio"):
        t = best[kind]
        if t is None:
            continue
        if t.get("index") and not t["segments"]:
            desc = [f"{kind}: 整文件 + sidx 索引"]
        else:
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

    if live:
        tsbd = live_meta["time_shift_buffer_depth"]
        mup = live_meta["minimum_update_period"]
        # 把窗口"用秒说出来" —— 用户唯一能据此判断"这次会录到多少"的东西
        if clock is None:
            pass                    # 上面已经写过"推不出窗口"的说明
        elif tsbd is not None:
            notes.append(
                f"直播: 可用窗口 {tsbd:.0f}s(清单声明的回溯范围),"
                f" 从这里开始录"
            )
        else:
            notes.append("直播: 清单未声明 timeShiftBufferDepth, 从当前直播点开始录")
        # ⚠️ 没有 minimumUpdatePeriod 就没有"下一份清单"的节奏: 只能录到第一次
        # 取回的窗口就结束。这不是失败, 但必须说出来 —— 否则用户以为"录了 5 分钟"
        # 而实际只录到 2 秒。
        if mup is None:
            notes.append(
                "直播: 清单没有声明 minimumUpdatePeriod, 无法得知新分片何时出现 ——"
                " 本轮只会下取回时已经存在的那几片"
            )

    return {"duration": duration, "video": best["video"], "audio": best["audio"],
            "periods": parsed, "notes": notes, "live": live,
            **live_meta}
