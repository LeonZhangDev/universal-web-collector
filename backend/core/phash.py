"""感知指纹(dHash)—— 零新增依赖, 复用已装好的 ffmpeg。

为什么需要它
============
项目已有的是**字节级** sha256 去重(见 `task_manager._download_one` 里的
`db.find_by_hash`)。它只能认出"一个字节都不差"的两份文件。但图集站上最常见的
重复恰恰不是这种:

- 同一张图存在多个尺寸/格式变体(`00046.jpg` 与 `00046_1200x0.webp` 常常
  是同一张照片的不同编码);
- 相册里重复收录、或者"连拍里挑了两张几乎一样的";
- 站点换 CDN 后同一资源被改了 query 参数、重新压了一遍。

上面这些用 sha256 全部认不出来, 用户看到的是"明明只该有 100 张, 却下了 130 张",
而且查不出哪几张是重的。

dHash 的做法: 把图缩成 9x8 的灰度, 逐行比较相邻像素的明暗, 得到 64 位指纹
(16 个十六进制字符)。两张"人眼看着是同一张"的图, 指纹海明距离通常在 4/64
以内; 完全不同的图通常在 25 以上。中间那一段是灰区, 阈值是个**人为约定**,
所以下面第三条约束才那么重要。

三条硬约束(改这个模块之前先读)
==============================

1. **只标记, 绝不自动删除。** dHash 会误判 —— 纯色图、大面积渐变、"同一场景连拍"
   都可能撞车。删文件是不可逆的, 而误判的后果由用户承担。所以本模块只**返回**
   指纹, 由调用方把 `duplicate_of` 写进资源记录与 manifest, 让人自己看着办。

2. **失败即放行。** ffmpeg 不在、解码失败、格式古怪 —— 一律返回 None(视为
   "没算出指纹"), 绝不因此让下载失败。指纹是**优化**, 不是流程的一部分;
   一个附件能力把主流程搞挂, 是本项目反复踩过的坑。

3. **不改变既有 sha256 去重的行为。** 两条路径并行: sha256 命中就照旧复用文件,
   感知指纹只在"sha256 没命中但人眼看着一样"时补一句提示。

4. **解码失败要上报, 不能吞掉。** 约束 2 说的是"失败即放行"(不阻断下载), 不是
   "失败即遗忘"。`DECODE_FAILED` —— ffmpeg 在、却解不开这个文件 —— 是整个系统里
   **最可靠的一条坏文件证据**(比长度校验强: 长度对得上也可能是坏字节)。所以
   `decode_gray_ex` 把它作为原因码返回给调用方, 由调用方写进
   `resources.error_kind='corrupt'`。仍然**只标记不删除**(见约束 1): 解码器也会
   认错冷门格式, 而删文件不可逆。

为什么用 ffmpeg 而不是自己解码: 项目已经依赖 ffmpeg(视频 remux/AES-128 解密),
而图片解码要覆盖 jpeg/png/webp/gif/bmp/avif 的话, 自己写等于重造一个解码器。
ffmpeg 一条命令行就够, 且与已有依赖**同一份探测逻辑**(`core/ffmpeg.py`)。

⚠️ 别用 `shutil.which("ffmpeg")`: PATH 是进程启动时的快照, 用户在服务运行期间
装的 ffmpeg 探测不到。统一走 `find_ffmpeg()`。
"""

import os
import subprocess
from typing import Optional

# dHash 的取样尺寸。9 列 8 行 -> 每行比较 8 次相邻像素 -> 正好 64 位。
# 用 9 而不是 8 是因为"相邻比较"天然比"与均值比较"(aHash)更能抵抗整体亮度变化。
_W, _H = 9, 8

#: 判定"同一张图"的海明距离阈值(单位: 位, 满值 64)。
#:
#: 4 是文献里常用的经验值, 但它**不是**普适真理 —— 阈值调大一点, 漏报少误报多;
#: 调小则相反。选 4 的理由: 实测同一张图换尺寸/重压缩后距离多为 0~2, 不同图
#: 之间很少低于 20。灰区(5~19)的样本本来就少, 把它们放在"不标记"一侧更安全 ——
#: 标错的代价是用户白看一眼, 漏标的代价是用户以为去重坏了。
DEFAULT_THRESHOLD = 4

#: 单次 ffmpeg 解码的超时(秒)。9x8 的缩放是毫秒级的, 给 20s 只是防"输入是
#: 一个坏文件导致解码器卡死"。超时后返回 None, 不重试 —— 指纹算不出来就算了。
_TIMEOUT = 20.0

#: 判定"几乎没有梯度"的灰度极差阈值。实测: 一张纯色图极差 0, 一张带文字的白底
#: 截图极差 200+。取 8 是为了放过"轻微噪点/渐变", 只挡真正的纯色块。
#: ⚠️ 这个数是用真图比出来的, 不是文献值 —— 见 `is_flat_gray`。
_FLAT_RANGE = 8


def _no_window_kwargs():
    """Windows 上别弹出控制台窗口。

    指纹是**每张图**算一次, 一个 300 张的相册就是 300 次子进程。若每次闪一个
    黑框, 用户会以为程序坏了。其它平台返回空字典。
    """
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def decode_gray(path, width=_W, height=_H) -> Optional[bytes]:
    """用 ffmpeg 把任意图片解码成 `width x height` 的 8 位灰度原始字节。

    返回长度恰好 `width*height` 的 bytes; 任何一步不顺利都返回 None
    (约束 2: 失败即放行, 不抛异常给上层)。

    需要知道**为什么**失败时用 `decode_gray_ex` —— 本函数把两种截然不同的原因
    压成了同一个 None, 详见那里的说明。
    """
    return decode_gray_ex(path, width, height)[0]


#: 解码失败的原因码 —— 调用方据此分辨"本机缺解码器"与"文件真坏了"。
#:
#: ⚠️ 这两个值**不能合并**。合并之后, "ffmpeg 说这个文件解不开"这条最可靠的坏文件
#: 证据就和"本机没装 ffmpeg"混在一起, 于是要么把整机所有文件都当成坏的, 要么把
#: 真正损坏的文件当成"环境问题"放过去。原实现就是后者: 一个解不开的文件, 唯一的
#: 后果是"这次没算指纹", 没有任何地方知道它坏。
NO_DECODER = "no_decoder"        # 本机没有 ffmpeg(或环境异常): 与文件无关
DECODE_FAILED = "decode_failed"  # ffmpeg 在, 但解不开 —— 文件可疑


def decode_gray_ex(path, width=_W, height=_H):
    """解码成灰度字节, 并说明失败原因; 返回 `(raw, reason)`。

    reason 为 None 表示成功; 否则取 `NO_DECODER` 或 `DECODE_FAILED`。
    `raw` 为 None 时 reason 一定有值。

    ⚠️ 必须带 `-frames:v 1`: 动图(gif/webp)和解码器的好奇心会让 ffmpeg 一直
    吐帧, 输出就会超过 `width*height` 字节。多出来的字节会让指纹算错而不是
    算失败 —— 那种错误最难发现, 所以宁可多一个参数。
    """
    try:
        from core.ffmpeg import find_ffmpeg

        ff = find_ffmpeg()
        if not ff:
            return None, NO_DECODER
        cmd = [
            ff, "-v", "error", "-nostdin",
            "-i", str(path),
            "-frames:v", "1",                     # 只取第一帧(见 docstring)
            "-vf", f"scale={int(width)}:{int(height)}:flags=bilinear",
            "-f", "rawvideo", "-pix_fmt", "gray",
            "-",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=_TIMEOUT, **_no_window_kwargs()
        )
        if proc.returncode != 0:
            # 解码器明确报错: 这不是"没算出来", 是"这个文件有问题"
            return None, DECODE_FAILED
        raw = proc.stdout or b""
        want = int(width) * int(height)
        if len(raw) < want:
            # 退出码 0 但吐不出一整帧 = 文件头合法、后半截没了。
            # 这正是纯长度校验(Content-Length 对得上)抓不到的那一类。
            return None, DECODE_FAILED
        # 截断而不是丢弃: 有些解码器会多吐一点, 前面的像素仍然是对的。
        return raw[:want], None
    except Exception:
        # 约束 2: ffmpeg 不在 / 超时 / 文件被别的进程占着 —— 都不该影响下载。
        # ⚠️ 这里**不能**返回 DECODE_FAILED: OSError("文件被占用") 是我们的环境
        # 问题, 拿它去指认"文件已损坏"就是冤枉。
        return None, NO_DECODER


def is_flat_gray(raw, width=_W, height=_H) -> bool:
    """这张图是不是**几乎没有梯度**的(纯色 / 大块同色)。

    ⚠️ dHash 只看**相邻像素谁更亮**, 不看绝对亮度。所以一张纯红和一张纯蓝的图,
    指纹**完全相同**(距离 0)—— 因为两者每一对相邻像素都是"不大于"。纯色截图、
    占位图、Logo 同理: 它们会被判成"彼此都是重复", 而且是一整片互指。

    判据用"灰度的极差": 极差小于 `_FLAT_RANGE` 就认为这张图的指纹**不携带信息**。
    误报的代价(第 ③ 条心法)决定这里要偏保守 —— 这类图一律**不参与比对**,
    而不是参与之后给出一堆假配对。
    """
    if not raw or len(raw) < width * height:
        return True
    low = 255
    high = 0
    for value in raw[: width * height]:
        if value < low:
            low = value
        if value > high:
            high = value
    return (high - low) < _FLAT_RANGE


def dhash_from_gray(raw, width=_W, height=_H) -> Optional[str]:
    """把灰度原始字节转成 dHash 的十六进制字符串(64 位 -> 16 字符)。"""
    if not raw or len(raw) < width * height:
        return None
    bits = 0
    for y in range(height):
        row = y * width
        for x in range(width - 1):
            bits = (bits << 1) | (1 if raw[row + x] < raw[row + x + 1] else 0)
    return "%016x" % bits


def dhash_ex(path, width=_W, height=_H):
    """算指纹并说明失败原因; 返回 `(指纹, reason)`。

    reason 语义同 `decode_gray_ex`: None=成功, `NO_DECODER`=无法判断,
    `DECODE_FAILED`=**文件可疑**。
    """
    raw, reason = decode_gray_ex(path, width, height)
    if raw is None:
        return None, reason
    got = dhash_from_gray(raw, width, height)
    # 灰度字节齐全却算不出指纹 = 本模块自己的问题, 不是文件的问题
    return got, (None if got else NO_DECODER)


def dhash(path, width=_W, height=_H) -> Optional[str]:
    """算一张图(或一段视频的首帧)的 dHash; 算不出返回 None。"""
    return dhash_ex(path, width, height)[0]


def _unhex(value) -> Optional[int]:
    """把 16 进制指纹解析成整数; 形态不对返回 None(而不是抛异常)。"""
    if not value:
        return None
    try:
        s = str(value).strip().lower()
        if len(s) != 16:
            return None
        return int(s, 16)
    except (TypeError, ValueError):
        return None


def distance(a, b) -> Optional[int]:
    """两个指纹的海明距离(位数)。任一侧形态不对时返回 None。

    ⚠️ 返回 None 与返回 0 含义完全不同: None = "没法比"(调用方应当**放行**),
    0 = "一模一样"。把"没法比"当成"相同"去标重, 那才是真的把用户的文件冤枉了。
    """
    x, y = _unhex(a), _unhex(b)
    if x is None or y is None:
        return None
    return bin(x ^ y).count("1")


def is_duplicate(a, b, threshold=DEFAULT_THRESHOLD) -> bool:
    """两张图是否**疑似**同一张。算不出指纹时返回 False(宁可漏标, 不可误标)。"""
    d = distance(a, b)
    if d is None:
        return False
    return d <= int(threshold)


def find_duplicate(phash_value, known, threshold=DEFAULT_THRESHOLD):
    """在已有指纹集合里找最接近的一个。

    `known`: 可迭代的 `(标识, 指纹)` 二元组。返回 `(标识, 距离)` 或 None。

    ⚠️ 返回**最接近**的那个而不是"第一个命中的": 提示语里会写"与第 N 张重复,
    差异 d 位", 报一个距离更远、更不像的, 会让用户以为指纹根本不准 ——
    提示的可信度一旦被消耗掉, 这个功能就没人看了。
    """
    if not phash_value:
        return None
    best = None
    for key, other in known or ():
        d = distance(phash_value, other)
        if d is None:
            continue
        if best is None or d < best[1]:
            best = (key, d)
    if best is None or best[1] > int(threshold):
        return None
    return best
