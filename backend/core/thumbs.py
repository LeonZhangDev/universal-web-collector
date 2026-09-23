"""缩略图: 让网格不再为了一张原图等十几秒。

为什么必须做
============
资源库与任务详情都用 `<img src>` 直接铺**原图**。图集站的图常有几千像素宽、
几 MB 到几十 MB —— 一个 40 项的网格就是几百 MB 的下载与解码。用户只是想扫一眼
"哪张是哪张", 却在等浏览器一张一张地解码大图, 表现是"界面卡"而不是"报错",
所以很难被归因到"用了原图"上。

落在哪
======
`<下载根>/_meta/thumb/<key>.jpg`

* 与 manifest 同属 `_meta/`(见 core/layout.py: 媒体文件与附属产物分开),
  所以**永远不会**被当成用户的内容出现在网格里。
* 但**不按任务分目录**: 同一张图被两个任务 sha256 去重复用时会指向同一个文件,
  按任务分目录就是白生成两份、白占双份空间。
* 也正因为在 `_meta/thumb` 而不是 `_meta/<任务ID>/`, 删任务时的
  `_purge_files` 不会顺手删掉它 —— 但也没人清理它, 所以另给 `prune()`
  按"原图还在不在"做一次兜底清理。

零依赖降级
==========
优先用 ffmpeg(项目已在用, 见 core/ffmpeg.py)。**没有 ffmpeg 时返回 None**,
调用方回退到原图路径 —— 缩略图是加速手段, 拿不到它只该"慢", 不该"看不到"。
同理, 生成失败(源文件损坏/格式冷门)也返回 None, 绝不抛异常: 它是渲染路径上的
附件能力, 让一个附件把接口打成 500 是本项目反复踩过的坑。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path

from core.ffmpeg import find_ffmpeg

#: `_meta/` 下放缩略图的子目录名
THUMB_DIR_NAME = "thumb"

#: 长边上限(像素)。320 够 168px 的网格卡两倍屏用, 再大只是白占空间。
DEFAULT_MAX_SIDE = 320

#: 生成超时(秒)。ffmpeg 卡住不能把接口一起拖住 —— 单张图不该等半分钟。
TIMEOUT = 20

#: 同一张缩略图可能被多个请求同时要(网格滚动会撞上), 用按 key 的锁串行化
#: 生成过程。否则两个 ffmpeg 会写同一个目标文件, 而 Windows 上 `os.replace`
#: 撞到别人打开的句柄会直接 PermissionError —— 就是 cdn_profile 那一类问题。
_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def thumb_dir(base) -> Path:
    """缩略图缓存目录 `<下载根>/_meta/thumb`。"""
    from core.layout import META_DIR_NAME

    return Path(base) / META_DIR_NAME / THUMB_DIR_NAME


def thumb_key(src) -> str:
    """缓存键: 源文件绝对路径的 sha1(带长度, 避免同名前缀相撞)。

    ⚠️ 用**绝对路径**而不是相对路径: 同一张图可能落在两个不同的下载根里
    (用户给任务指定过自定义目录), 用相对路径两者会撞成同一个 key, 于是
    其中一个格子显示的其实是另一个根里的图 —— 内容对了还好, 一旦不对就是
    "资源库里的缩略图和点开的大图不是同一张", 极难怀疑到缓存键上。
    """
    raw = os.path.normcase(os.path.abspath(str(src))).encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()


def thumb_path(base, src) -> Path:
    return thumb_dir(base) / f"{thumb_key(src)}.jpg"


def is_fresh(src, dest, max_side=DEFAULT_MAX_SIDE) -> bool:
    """缓存的缩略图还有效吗。

    ⚠️ 要比 mtime: 源文件被重下/替换后(内容一样的复用不会换文件, 但
    "同名不同源"消解失败时可能真的换了)缩略图必须跟着变, 否则网格里显示的是
    上一版内容。比 mtime 是这里唯一便宜的判据。
    """
    try:
        if not dest.is_file() or dest.stat().st_size <= 0:
            return False
        return dest.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def ensure_thumb(base, src, max_side=DEFAULT_MAX_SIDE):
    """确保缩略图存在, 返回它的路径; 做不到就返回 None(调用方回退原图)。"""
    try:
        sp = Path(src)
        if not sp.is_file():
            return None
        dest = thumb_path(base, sp)
        if is_fresh(sp, dest, max_side):
            return dest
        exe = find_ffmpeg()
        if not exe:
            return None
        with _lock_for(dest.name):
            # 拿到锁之后再判一次: 排队的那些请求里, 第一个已经把图生成好了
            if is_fresh(sp, dest, max_side):
                return dest
            return _render(exe, sp, dest, max_side)
    except Exception:
        # 缩略图是渲染附件: 它失败只该"慢", 不该"出错"
        return None


def _render(exe, src, dest, max_side):
    tmp = dest.with_name(dest.name + ".tmp")
    # ⚠️ `min(max_side,iw)`: 小图**不放大**。放大只会让它更糊, 还白占空间。
    # 宽写成表达式、高给 -2 让 ffmpeg 自己算并保证偶数(奇数尺寸在 JPEG 上要报错)。
    vf = f"scale='min({int(max_side)},iw)':-2"
    cmd = [
        exe, "-y", "-v", "error",
        "-i", str(src),
        "-vf", vf,
        # 视频取首帧当海报图 —— 同一套参数顺带把视频的缩略图也解决了。
        # 放在图片上时这两个开关是空操作。
        "-frames:v", "1",
        "-an", "-sn",
        # 质量 5 ≈ 视觉无损; 缩略图不需要更好, 要的是快和小
        "-q:v", "5",
        "-f", "image2",
        str(tmp),
    ]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 0:
            return None
        # 原子换入: 半截的 jpg 会被浏览器当成完整图渲染(花屏), 比没有更难看
        os.replace(tmp, dest)
        return dest
    except Exception:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def prune(base, exists=None):
    """清掉"原图已经不在了"的缩略图, 返回删除数。

    判据只有一条: 缩略图的 mtime 早于它自己的源文件 —— 但缩略图里**不存源路径**,
    所以这里改用调用方给的 `exists(src_path)->bool` 来判。不传就不删(宁可留着,
    也不能凭猜测删用户目录里的东西)。
    """
    if exists is None:
        return 0
    removed = 0
    d = thumb_dir(base)
    if not d.is_dir():
        return 0
    # 缩略图文件名是源路径的 hash, 无法反推 —— 由调用方拿库里的 local_path 逐个核对。
    # 这里只做"库里没有任何一条资源指向这个 key"的清理。
    try:
        keep = {thumb_key(p) for p in exists()}
    except Exception:
        return 0
    for f in d.glob("*.jpg"):
        if f.stem in keep:
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            continue
    return removed
