"""图片尺寸的轻量解析: 只读文件头, 不解码、不依赖 Pillow。

为什么需要它
============
过滤广告图时, "文件名"和"体积"都靠不住: 一个 728x90 的横幅可以叫
`abc123.jpg`(看不出是广告), 也可以有 40KB 的体积(不算"过小")。真正的
判别特征是**宽高比和绝对尺寸** —— 横幅、按钮、1x1 信标全是极端长宽比
或极小尺寸, 而相册里的图极少是 300x250。

所以 `min_width` / `min_height` / `min_pixels` 是"什么是有效资源"里
最有力的一条规则。它只需读文件开头几十字节就能判定, 比下载整张图再解码
便宜得多, 也不引入任何第三方依赖。

判定时机
========
放在**下载完成后**(见 task_manager 的尺寸终检), 不是下载前。原因:
* 下载前用 Range 抓头部, 遇到 progressive JPEG 会抓不到 SOF 段(误判放行),
  而"误判放行"意味着广告照样落盘 —— 等于没做;
* 下载后手上就是完整文件, 零额外请求、零误判。代价是那点流量已经花了,
  但广告图本身很小, 这点代价换来的是判定绝对可靠。

⚠️ 解析不出来一律返回 None, 由调用方**放行**(与 filters 的
"探测失败一律放行"一致)。宁可漏判一张广告, 也不能误杀一张真图。
"""

#: 读多少字节用于解析。JPEG 的 SOF 段可能被 EXIF 缩略图(常见几 KB)挤到后面,
#: 64KB 足以覆盖绝大多数情况; 真读不到就当解析失败放行。
HEADER_BYTES = 65536

#: JPEG 里承载宽高的 SOF 段。**排除** 0xC4(DHT) / 0xC8(JPG) / 0xCC(DAC):
#: 它们同样落在 0xC0-0xCF 区间, 但结构不同, 混进来会读出乱码尺寸。
_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})
#: 没有长度字段的独立标记(SOI/EOI/RSTn/TEM)
_STANDALONE = frozenset({0xD8, 0xD9, 0x01}) | set(range(0xD0, 0xD8))


def _jpeg(b):
    """遍历 JPEG 段找 SOF。返回 (w, h) 或 None。"""
    i, n = 2, len(b)
    while i + 3 < n:
        if b[i] != 0xFF:
            # 段间填充字节(0xFF 可重复出现), 也用于容错
            i += 1
            continue
        marker = b[i + 1]
        if marker == 0xFF or marker == 0x00:
            i += 1
            continue
        if marker in _STANDALONE:
            i += 2
            continue
        if i + 4 > n:
            return None
        seg_len = int.from_bytes(b[i + 2:i + 4], "big")
        if seg_len < 2:          # 长度非法: 继续扫只会读出垃圾
            return None
        if marker in _SOF_MARKERS:
            if i + 9 > n:
                return None
            h = int.from_bytes(b[i + 5:i + 7], "big")
            w = int.from_bytes(b[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        if marker == 0xDA:       # 进入扫描数据, 后面不会再有 SOF
            return None
        i += 2 + seg_len
    return None


def _webp(b):
    """WebP 三种子格式(有损 VP8 / 无损 VP8L / 扩展 VP8X)各自的取法。

    长度检查按子格式分开做, 不用一刀切的 `len(b) < 30`: 三种格式需要读到的
    最大偏移不同, 一刀切会让本来就够读的短文件白白判成"认不出"。
    认不出等于放行 —— 于是广告照样落盘, 等于没做。
    """
    chunk = bytes(b[12:16])
    if chunk == b"VP8 ":
        # 有损: 帧头 3 字节 + 起始码 9D 01 2A, 之后各 2 字节(低 14 位有效)
        if len(b) < 30:
            return None
        w = int.from_bytes(b[26:28], "little") & 0x3FFF
        h = int.from_bytes(b[28:30], "little") & 0x3FFF
        return (w, h) if w and h else None
    if chunk == b"VP8L":
        # 无损: 1 字节签名(0x2F) + 4 字节位域, 各 14 位, 存的是"宽高 - 1"
        if len(b) < 25:
            return None
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        # 扩展: 4 字节标志 + 3 字节画布宽 + 3 字节高, 同样存"宽高 - 1"
        if len(b) < 30:
            return None
        w = int.from_bytes(b[24:27], "little") + 1
        h = int.from_bytes(b[27:30], "little") + 1
        return (w, h)
    return None


def dimensions_from_bytes(b):
    """从字节流开头解析 (宽, 高); 认不出返回 None。

    支持 PNG / JPEG / GIF / BMP / WebP —— 覆盖了实际会遇到的绝大多数情况。
    """
    if not b or len(b) < 16:
        return None
    b = bytes(b)

    if b[:8] == b"\x89PNG\r\n\x1a\n":
        # IHDR 必须是第一个块; 偏移 16/20 就是宽高(各 4 字节大端)
        if len(b) >= 24 and b[12:16] == b"IHDR":
            w = int.from_bytes(b[16:20], "big")
            h = int.from_bytes(b[20:24], "big")
            return (w, h) if w and h else None
        return None

    if b[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(b[6:8], "little")
        h = int.from_bytes(b[8:10], "little")
        return (w, h) if w and h else None

    if b[:2] == b"BM":
        if len(b) < 26:
            return None
        w = int.from_bytes(b[18:22], "little", signed=True)
        # 高度为负表示自上而下存储, 尺寸仍是绝对值
        h = abs(int.from_bytes(b[22:26], "little", signed=True))
        return (w, h) if w and h else None

    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return _webp(b)

    if b[:2] == b"\xff\xd8":
        return _jpeg(b)

    return None


def image_dimensions(path, limit=HEADER_BYTES):
    """读文件头解析 (宽, 高)。文件不存在/读失败/格式不认识都返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(limit)
    except OSError:
        return None
    return dimensions_from_bytes(head)


def fmt_dimensions(dims):
    """"(1920, 1080)" -> "1920x1080"; 拿不到尺寸时返回 "尺寸未知"。"""
    if not dims:
        return "尺寸未知"
    try:
        w, h = dims
        return f"{int(w)}x{int(h)}"
    except (TypeError, ValueError):
        return "尺寸未知"


__all__ = [
    "HEADER_BYTES",
    "dimensions_from_bytes",
    "image_dimensions",
    "fmt_dimensions",
]
