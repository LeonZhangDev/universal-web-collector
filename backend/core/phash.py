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

    ⚠️ 必须带 `-frames:v 1`: 动图(gif/webp)和解码器的好奇心会让 ffmpeg 一直
    吐帧, 输出就会超过 `width*height` 字节。多出来的字节会让指纹算错而不是
    算失败 —— 那种错误最难发现, 所以宁可多一个参数。
    """
    try:
        from core.ffmpeg import find_ffmpeg

        ff = find_ffmpeg()
        if not ff:
            return None
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
            return None
        raw = proc.stdout or b""
        want = int(width) * int(height)
        if len(raw) < want:
            return None
        # 截断而不是丢弃: 有些解码器会多吐一点, 前面的像素仍然是对的。
        return raw[:want]
    except Exception:
        # 约束 2: ffmpeg 不在 / 超时 / 文件被别的进程占着 —— 都不该影响下载
        return None


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


def dhash(path, width=_W, height=_H) -> Optional[str]:
    """算一张图(或一段视频的首帧)的 dHash; 算不出返回 None。"""
    return dhash_from_gray(decode_gray(path, width, height), width, height)


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
