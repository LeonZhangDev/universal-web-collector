"""磁盘空间守卫。

⚠️ 为什么需要单独一层: 磁盘写满之后, 每个资源都会各自走完一整条重试链才失败
(退避 + 抖动 + 多轮), 几百个资源就是长时间空转, 最后得到一堆
"No space left on device" —— 用户既不知道要清哪里, 也不知道清多少。

所以两件事必须做:
  ① **提前检查, 快失败**: 满盘之后再下就是纯浪费时间和带宽;
  ② **失败消息要能行动**: 写清还剩多少、水位多少、接下来怎么办。
"""
import shutil
from pathlib import Path

from core.config import settings
from core.errors import DiskFullError


def _fmt(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def free_bytes(target):
    """目标路径所在分区的剩余字节数。查不出来返回 None —— 查不出就不拦。

    ⚠️ 不要在查不出来时"保守地"抛错: 某些文件系统/挂载点拿不到 usage,
    把正常运行判成满盘, 用户会一头雾水。
    """
    try:
        return shutil.disk_usage(Path(target)).free
    except Exception:
        return None


def ensure_free(target, need=None):
    """剩余空间不足时抛 DiskFullError。

    need: 本次预计要写多少字节。给不出(None)就只按最低水位判断。
    """
    free = free_bytes(target)
    if free is None:
        return
    floor = max(int(getattr(settings, "min_free_bytes", 0)), 0)
    need = int(need or 0)
    short = need and free < need
    if free < floor or short:
        if short:
            why = f"本次需要约 {_fmt(need)}, 但目标盘只剩 {_fmt(free)}"
        else:
            why = f"目标盘只剩 {_fmt(free)}, 低于最低水位 {_fmt(floor)}"
        raise DiskFullError(
            f"磁盘空间不足: {why}。已停止下载 —— 已下好的文件会保留, "
            f"清理磁盘后点「继续」可从断点接着下。"
        )
