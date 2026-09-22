"""容器完整性终检 —— 零子进程、零新增依赖, 只做算术。

为什么需要这个
==============
下载层已经有一层长度校验(`Content-Length` vs 实际字节数, 见
`downloaders/base.py` 的 `_stream_one`)。它能挡住"少下了一半", 但**挡不住**
"字节数正确、内容是坏的":

* 中间的代理/网关截断了响应体, 却原样转发上游的 `Content-Length`;
* CDN 回了一个长度正确的错误页/占位图;
* 源站上的文件本身就是转码失败的半成品。

这三种都会以"下载成功"的身份落盘。之后 sha256 去重、manifest、缩略图、dHash
全都建立在一份坏字节上, 而且**没有任何报错** —— 只有用户双击打开时才看到
"文件已损坏"。所以判据必须来自文件内容本身, 不能只看字节数。

为什么是算术而不是解码
======================
最直白的做法是"用解码器解一遍, 解不开就是坏的"。但解码有两个问题:

1. **慢。** 一张 5000x5000 的 JPEG 完整解码要上百毫秒, 一个 300 张的相册就是
   一分多钟纯等待。而这一步在**每个**资源上都要跑。
2. **不准, 而且方向不明。** 解码器对损坏文件相当宽容: 大多数截断的 JPEG 仍能
   解出上半张图, 退出码是 0。反过来, ffmpeg 不认识的冷门封装会被误判成坏文件
   —— 那时删掉的是用户真正想要的东西。

而"这个文件是不是完整的"其实**是算术题**: 绝大多数容器格式自己就声明了总长度
(Riff/BMP 的长度字段、ISOBMFF 的 box 链), 或者有必须出现在结尾的标记
(JPEG 的 EOI、PNG 的 IEND、GIF 的 trailer)。读头尾各 32 字节就能判定,
开销与文件大小无关, 而且结论是**确定的** —— 没有"解码器觉得很怪"这种模糊地带。

所以本模块只做**能被证明**的判断: 报出问题时就一定是文件被截断了。宁可漏报
(交给上层按需再做深度检查), 不可误报 —— 报错的代价是删掉一个好文件。

判据一览
========
| 格式        | 判据                                            | 强度 |
| ----------- | ----------------------------------------------- | ---- |
| WebP (RIFF) | 头部长度字段 + 8 必须恰好等于文件大小            | 精确 |
| BMP         | 头部长度字段必须恰好等于文件大小                 | 精确 |
| MP4/MOV/AVIF| box 链必须能走通, 且没有 box 越出文件末尾        | 精确 |
| JPEG        | 尾部必须有 EOI 标记 `FF D9`                      | 强   |
| PNG         | 尾部必须有 IEND 块                               | 强   |
| GIF         | 末尾必须是 trailer `0x3B`                        | 强   |
| 其它        | 不认识 -> 不下结论(返回 None)                    | —    |

⚠️ 判据选的是"**必须有**"的东西, 不是"宽松的估算"。JPEG 的尾部是在最后 64 字节
**里找** EOI 而不是要求正好在最后两位 —— 有些编码器会在后面补几个字节, 那种
文件是好的, 不该被冤枉。
"""

import struct
from pathlib import Path

#: 读多少字节来判断。32 足够覆盖所有 magic 与长度字段。
_HEAD = 32
#: 尾部取样窗口。取 64 是为了容忍"EOI/IEND 之后还有几个填充字节"的编码器怪癖。
_TAIL = 64
#: ISOBMFF box 链的遍历上限。一个正常 mp4 的顶层 box 不到 10 个; 这个数字只是
#: 防"坏数据让 while 循环转不停"(比如 size 字段恒为 8 的构造性坏文件)。
_MAX_BOXES = 1000

#: 只认这些**真实存在于顶层链上**的 box 类型。
#:
#: ⚠️ 这张白名单是为了挡住一类具体的误报: 有工具会在完整的 mp4 后面追加数据
#: (注释、元数据、封面)。那时 box 链会**错位** —— 追加的首字节被当成新的长度字段,
#: 而错位后紧跟着的 4 个字节完全可能碰巧长得像类型名。实测一个真实 mp4 后面追加
#: `<!--6aaa517d3f106.mp4-->` 时, "长度"读成 `<!--`(约 10 亿), "类型"读成 `6aaa`,
#: 一个**好文件**就这样被判成"被截断"。所以判据从"像不像类型名"收紧成
#: "是不是我们认识的顶层 box 类型"。
#:
#: 漏报(不认识某个冷门 box 而不下结论)是可接受的; 误报(删掉用户要的文件)不是。
_TOP_BOX_TYPES = frozenset({
    b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide",
    b"moof", b"mfra", b"pdin", b"meta", b"uuid", b"sidx", b"ssix", b"emsg",
})


def _read_edges(path):
    """返回 (头 _HEAD 字节, 尾 _TAIL 字节, 文件大小); 读不了返回 None。

    只做两次 seek + 两次 read, 与文件大小无关 —— 这是本模块敢放在每个资源上跑
    的前提。
    """
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            return f.read(_HEAD), _tail_bytes(f, size), size
    except OSError:
        return None


def _tail_bytes(f, size):
    """读文件末尾的 _TAIL 字节。小文件就整份读(避免 seek 到负数)。"""
    take = min(_TAIL, size)
    if take <= 0:
        return b""
    f.seek(size - take)
    return f.read(take)


def _isobmff_reason(size, read_at):
    """ISOBMFF(mp4/mov/avif/heic)的 box 链检查。

    结构是"一串 box": 每个 box = 4 字节大端长度 + 4 字节类型 + 内容。长度字段
    累加起来必须恰好铺满整个文件。截断的文件几乎必然在**最后一个 box** 上露馅:
    它声明的长度会超出文件末尾。

    两个特殊值(有意的设计, 不是补丁):
      * `size == 0` —— "这个 box 一直到文件结尾", 合法, 到此结束;
      * `size == 1` —— 后面 8 字节才是真正的 64 位长度, 大文件会用。

    ⚠️ 只在**发现越界**时下结论。走到一半没字节了(剩余 < 8 字节的尾填充)算通过:
      有些工具会追加对齐字节, 那不该被判成坏文件。
    ⚠️ 而且只相信**白名单里的顶层 box 类型**(见 `_TOP_BOX_TYPES`)。文件后面被追加
      数据会让 box 链错位, 错位后的字节碰巧像类型名, 于是一个**好文件**被判成截断 ——
      误报的代价是删掉用户要的文件, 所以这里必须多问一句"这真的是个 box 吗"。
    """
    offset = 0
    for _ in range(_MAX_BOXES):
        if size - offset < 8:
            return None          # 剩下的不足一个 box 头 —— 视作尾部填充, 放行
        chunk = read_at(offset, offset + 16)
        if len(chunk) < 8:
            return None
        if chunk[4:8] not in _TOP_BOX_TYPES:
            # 不是我们认识的顶层 box 类型 = 这多半是追加在后面的数据, 而不是
            # 被截断的 box 头。链本身到此为止, 后面的东西不归我们判断。
            return None
        box_size = struct.unpack(">I", chunk[:4])[0]
        if box_size == 0:
            return None          # "直到文件末尾", 合法结束
        if box_size == 1:
            if len(chunk) < 16:
                return None
            box_size = struct.unpack(">Q", chunk[8:16])[0]
        if box_size < 8:
            # 长度比 box 头还小 = 结构自相矛盾, 已经不是"截断"而是"不是这个格式"
            return None
        if offset + box_size > size:
            return (
                f"容器声明长度 {offset + box_size} 字节, 实际只有 {size} 字节"
                f"(最后一个 box 被截断)"
            )
        offset += box_size
        if offset == size:
            return None          # 正好铺满 —— 完整
    return None


def truncation_reason(path):
    """文件是否**可证地**不完整; 完整或无法判断时返回 None。

    返回的字符串是给用户看的(会进 `resources.note` 与界面), 所以要说清
    "短了多少", 而不是"校验失败"。
    """
    edges = _read_edges(path)
    if not edges:
        return None
    head, tail, size = edges
    if size == 0:
        return "文件长度为 0"
    if len(head) < 12:
        return None              # 太小, 任何判据都不可靠

    # ---- 头部自述长度的格式: 长度字段与文件大小对不上就是被截断 ----
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        declared = struct.unpack("<I", head[4:8])[0] + 8
        if declared != size:
            return f"WebP 声明 {declared} 字节, 实际 {size} 字节"
        return None
    if head[:2] == b"BM":
        declared = struct.unpack("<I", head[2:6])[0]
        # 少数写出器把长度字段留 0, 那是"未填写"而非"截断", 不能据此判坏
        if declared and declared != size:
            return f"BMP 声明 {declared} 字节, 实际 {size} 字节"
        return None

    # ---- box 链格式 ----
    if head[4:8] in (b"ftyp", b"styp"):
        def read_at(start, end):
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    return f.read(end - start)
            except OSError:
                return b""

        return _isobmff_reason(size, read_at)

    # ---- 必须有尾部标记的格式 ----
    if head[:2] == b"\xff\xd8":
        # JPEG: EOI(FF D9) 是硬性要求。在尾部窗口**里找**而不是要求正好在最后
        # 两位 —— 有的编码器会补几个字节, 那种文件是好的。
        # ⚠️ FF D9 也可能碰巧出现在截断处的压缩数据里, 极少数情况下会漏报;
        #    漏报只是"没查出来", 比误报删好文件可接受。
        if b"\xff\xd9" in tail:
            return None
        return f"JPEG 缺少结束标记 EOI(疑似被截断, 末尾 {len(tail)} 字节无 FF D9)"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        if b"IEND" in tail:
            return None
        return "PNG 缺少结束块 IEND(疑似被截断)"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        if tail[-1:] == b"\x3b":
            return None
        return "GIF 缺少结束标记 0x3B(疑似被截断)"

    # 不认识的格式: 不下结论。误判会删掉用户要的文件, 漏判只是少一层保护。
    return None


def looks_truncated(path):
    """`truncation_reason` 的布尔形态。"""
    return truncation_reason(path) is not None
