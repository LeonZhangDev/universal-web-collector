"""ffmpeg 可执行文件定位。

为什么不能只用 shutil.which()
==============================

1. **PATH 是进程启动时的快照。** 用户在服务运行期间装了 ffmpeg(如 winget),
   当前进程读不到更新后的 PATH, `which()` 会一直返回 None —— 直到进程重启。
   实测 2026-09-18: winget 把 ffmpeg 放进
   `%LOCALAPPDATA%\\Microsoft\\WinGet\\Links`, 且注册表 PATH 已更新,
   但运行中的后端仍探测不到。

2. **不同安装方式落在不同目录, 且不保证进 PATH。** 官网 zip 解压到
   `C:\\ffmpeg` 是最常见的用法, 很多教程也不要求加 PATH。

因此按 `显式配置 -> PATH -> 已知安装位置` 顺序探测。
探测失败带 TTL 缓存, 使用户装好 ffmpeg 后无需重启进程即可自动发现。

显式配置见 `config.yaml` 的 `ffmpeg_path`, 或环境变量 `UWC_FFMPEG`。
"""

import os
import shutil
import time
from pathlib import Path
from typing import Optional

from core.config import settings

# 探测失败后的重试间隔(秒)。
# 缓存失败是为了避免每次下载都遍历一遍目录; 给它一个 TTL 则能让
# "服务运行期间才装好 ffmpeg" 这种情况自动生效, 无需重启。
_FAIL_TTL = 30.0

_EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

# key(配置值) -> (路径或 None, 记录时刻)
_cache: dict = {}


def _windows_candidates():
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    programdata = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
    return [
        # winget install Gyan.FFmpeg
        local / "Microsoft" / "WinGet" / "Links" / _EXE,
        # scoop
        home / "scoop" / "shims" / _EXE,
        # chocolatey
        programdata / "chocolatey" / "bin" / _EXE,
        # 官网 zip 手动解压的常见落点
        Path(r"C:\ffmpeg\bin") / _EXE,
        Path(r"C:\Program Files\ffmpeg\bin") / _EXE,
        Path(r"C:\Program Files (x86)\ffmpeg\bin") / _EXE,
        local / "Programs" / "ffmpeg" / "bin" / _EXE,
        # 微软商店 / winget 的另一种落点
        local / "Microsoft" / "WindowsApps" / _EXE,
    ]


def _unix_candidates():
    return [
        Path(p) / "ffmpeg"
        for p in ("/usr/local/bin", "/usr/bin", "/opt/homebrew/bin", "/snap/bin")
    ]


def _probe(configured: Optional[str]) -> Optional[str]:
    # 1) 显式配置优先。配置了但文件不存在时不直接报错, 而是继续往下探测 ——
    #    对"路径写错了但仍希望自动找到"这种场景更宽容。
    if configured:
        p = Path(configured)
        try:
            if p.is_file():
                return str(p)
        except OSError:
            pass

    # 2) PATH(注意: 可能是不含新装目录的旧快照)
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 3) 已知安装位置
    cands = _windows_candidates() if os.name == "nt" else _unix_candidates()
    for c in cands:
        try:
            if Path(c).is_file():
                return str(c)
        except OSError:
            continue

    # 4) 可选依赖: imageio-ffmpeg 自带静态二进制, 无需用户手动安装
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return str(exe)
    except Exception:
        pass

    return None


def find_ffmpeg(refresh: bool = False) -> Optional[str]:
    """返回 ffmpeg 可执行文件的绝对路径; 找不到返回 None。

    成功结果在进程内永久缓存; 失败结果只缓存 `_FAIL_TTL` 秒,
    以便新装好的 ffmpeg 能被自动发现。改 `settings.ffmpeg_path` 会另起缓存。
    """
    configured = getattr(settings, "ffmpeg_path", None) or None
    key = configured or ""

    hit = _cache.get(key)
    if hit and not refresh:
        path, ts = hit
        if path or (time.monotonic() - ts) < _FAIL_TTL:
            return path

    path = _probe(configured)
    _cache[key] = (path, time.monotonic())
    return path


def ffmpeg_available(refresh: bool = False) -> bool:
    return find_ffmpeg(refresh=refresh) is not None


def clear_cache():
    """清空探测缓存(测试或运行中手工重探时用)。"""
    _cache.clear()
