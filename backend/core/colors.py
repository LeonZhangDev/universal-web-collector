"""主色提取与色系归类 —— "按颜色找图"(digiKam 的颜色筛选那一类)。

为什么存**色系**而不是色值
==========================
库里存的是色系的**代号**(`red` / `blue` / `gray`…), 不是 RGB/HEX 具体值:

* 筛选要落在 SQL 上做**等值匹配**(`WHERE dominant_color = ?`), 这样才能走索引;
  存色值的话, "找偏红的图"就得在 SQL 里做 HSV 换算 —— 那是把图像算法塞进
  数据库, 既走不了索引也没法解释。
* 具体色值随时可以重算, 而"它属于哪个色系"才是用户检索时真正用的那个词。

代价是粒度粗(同一色系里的深红与浅红分不开), 这是**有意**的: 颜色检索本来就是
"我记得那张图偏蓝", 而不是"我要 #3a7bd5"。

为什么走 ffmpeg 而不引 Pillow
=============================
本项目**刻意不依赖 Pillow** —— 缩略图(`core/thumbs.py`)与完整性检查
(`core/mediacheck.py`)全部走 ffmpeg。为一个"取主色"的小功能引一个新的图像库,
等于给依赖表多开一个口子: 少装一个包就是整片测试 collection 红, 而红的原因
看起来跟这个功能毫无关系。ffmpeg 已经是硬依赖, 缩到 32x32 再读原始像素完全够用。

判据的松紧
==========
和 `core/filekind.py` 同款取舍: **宁可漏报, 不可误报**。
读不出图 / ffmpeg 不在 -> 返回 `None`(第 26 条: "没算出结果"不等于"没有主色"),
不猜一个色系填进去 —— 填进去就是伪造证据, 之后"按灰色筛"会混进一堆没测过的文件。
"""

import subprocess
from colorsys import rgb_to_hsv

from core.ffmpeg import ffmpeg_available, find_ffmpeg

#: 缩到多少再统计。32x32 = 1024 个采样点, 足够反映画面构成, 又小到一张图几毫秒。
SIDE = 32

#: 单张图的超时(秒)。与 `thumbs.TIMEOUT` 同款理由: 一张图不该让接口等半分钟。
TIMEOUT = 20

#: 每个通道量化成多少级(右移位数)。5 位 = 32 级/通道, 足够分出"偏蓝"与"偏青"。
_BUCKET_BITS = 5


#: 色系 -> (中文名, 代表色)。文案与色值都在后端, 前端拿键查表后直接画色块。
#: 代表色仅供**画色块**用, 不参与检索(检索用的是键)。
COLOR_FAMILIES = {
    "red": ("红", "#e02020"),
    "orange": ("橙", "#ff8000"),
    "yellow": ("黄", "#ffd400"),
    "green": ("绿", "#20c020"),
    "cyan": ("青", "#20c0c0"),
    "blue": ("蓝", "#2060e0"),
    "purple": ("紫", "#8020c0"),
    "pink": ("粉", "#ff60a0"),
    "brown": ("棕", "#8a5a2b"),
    "gray": ("灰", "#808080"),
    "black": ("黑", "#202020"),
    "white": ("白", "#f0f0f0"),
}


def families():
    """色系清单(带中文名与代表色), 供 `/library/facets` 下发。"""
    return [{"key": k, "label": v[0], "hex": v[1]}
            for k, v in COLOR_FAMILIES.items()]


def family_of_rgb(r, g, b):
    """RGB(0-255) -> 色系代号。

    先按**明度/饱和度**把黑/白/灰摘出来, 再按**色相**分彩色 —— 顺序不能反:
    一张很暗的蓝图的色相仍然是蓝, 但用户心里的"黑色"才是它的检索词。
    """
    h, s, v = rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)

    # ---- 无色系: 明度或饱和度太低, 色相没有意义 ----
    if v <= 0.12:
        return "black"
    if s <= 0.12 and v >= 0.82:
        return "white"
    if s <= 0.18:
        return "gray"

    # ---- 彩色: 棕色是"暗而偏橙", 必须排在橙色之前判 ----
    if v <= 0.55 and 0.02 <= h <= 0.11:
        return "brown"

    if h < 0.045 or h >= 0.94:
        return "red"
    if h < 0.11:
        return "orange"
    if h < 0.17:
        return "yellow"
    if h < 0.42:
        return "green"
    if h < 0.52:
        return "cyan"
    if h < 0.72:
        return "blue"
    if h < 0.83:
        return "purple"
    return "pink"


def _dominant_of_rgb24(data):
    """从 rgb24 原始像素里找**面积最大**的那一档颜色所属色系。

    用"出现最多的量化桶"而不是"全图平均色": 平均色会把蓝天绿地的图算成灰,
    那不是任何人的检索词。

    ⚠️ 取桶的**中心**值而不是下界: 全都取下界的话, 结果会整体偏暗, 于是
    浅蓝被判成蓝、蓝被判成深蓝 —— 偏差只往一个方向走, 最难发现。
    """
    counts = {}
    for i in range(0, len(data) - 2, 3):
        key = (data[i] >> _BUCKET_BITS,
               data[i + 1] >> _BUCKET_BITS,
               data[i + 2] >> _BUCKET_BITS)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    best = max(counts, key=counts.get)
    half = 1 << (_BUCKET_BITS - 1)
    return family_of_rgb(min(255, (best[0] << _BUCKET_BITS) + half),
                         min(255, (best[1] << _BUCKET_BITS) + half),
                         min(255, (best[2] << _BUCKET_BITS) + half))


def dominant(path):
    """读出图片的主色所属色系; 读不出来时返回 None(不下结论)。

    ⚠️ 只捕获**跑 ffmpeg**会抛的那些异常(`OSError` / `SubprocessError`),
    不写 `except Exception`: 后者会把本模块自己的 bug 也当成"这张图读不出来",
    于是所有失败都指向文件, 而真相可能在代码里(第 4 条同族)。
    """
    exe = find_ffmpeg()
    if not exe:
        return None
    cmd = [exe, "-v", "error", "-i", str(path),
           "-vf", f"scale={SIDE}:{SIDE}", "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    need = SIDE * SIDE * 3
    data = proc.stdout or b""
    if len(data) < need:
        return None
    return _dominant_of_rgb24(data[:need])


def available():
    """提色功能当前能不能用(取决于 ffmpeg 在不在)。

    ⚠️ 这是给**端点报错**用的: ffmpeg 不在时 `extract_colors` 会抛错回 501,
    而不是"成功处理 0 张" —— 后者会让按钮看起来点过了(第 27 条)。
    """
    return ffmpeg_available()
