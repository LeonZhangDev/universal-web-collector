"""资源落在下载目录的哪一层 —— 唯一定义。

铁律
====
下载目录的**下一级只允许是相册文件夹**(视频除外)::

    downloads/
      葡萄一番街/00001.jpg          <- 图片等非视频资源: 相册文件夹 + 文件名
      0001_葡萄一番街.mp4            <- 视频: 平铺在下载根目录
      _meta/101/manifest.json      <- 每个任务的溯源清单(不是媒体, 见 meta_dir)

为什么集中在这里
================
"文件落到哪"原先散在三处: 采集器给 `filename`、task_manager 拼 `{task_id}/`、
下载器 `resolve_target` 兜底。三处各改一点就会出现"能采到、但用户按预期去找
找不到"的半通状态 —— 与 `filters.match_resource` 是同一种病, 解法也一样:
只留一个定义, 其他组件都来问它。

为什么视频要平铺
================
视频页的资源是 `.../6aaa517d3f106.m3u8`, 站点自己拿 gid 当文件名, 平铺后天然
唯一; 而相册里的视频是 `0001.mp4`、`0002.mp4` —— 每个相册都从 0001 重数。

⚠️ 重名必须在**下载前**消解(`claim`), 不能指望下载器兜底
========================================================
下载器碰到"目标路径已有文件"时会把它当**半成品**搬进 `.part` 续传
(见 `downloaders/base.py::_prepare_resume`): 它是按"这是同一个文件的下一次
续传"设计的, 于是不同来源的同名文件会被**拼接**成一份产物, 而且因为字节数
可能恰好对得上, 最终以"成功"落盘 —— 又是"文件在、大小对、内容是坏的"那一类。
所以同名不同源必须在命名阶段就分开, 靠 `claim()` + 一个"这一格现在归谁"的
查询(库里的 `filename` / `local_path`)完成。

相册名怎么取
============
取**文件所在目录的最深一段**。采集器可能套多层(标签层 `丝袜-情趣内衣/相册名`、
聚合层 `模特名/相册名`), 布局规则只保留最后一层 —— 那是相册自己的名字, 上面
几层是分类维度, 而"下载目录下一级只有一个相册文件夹"是用户的明确要求。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from core.naming import clean_segment, safe_relative

#: 平铺到下载根目录的资源类型(见模块注释)
FLAT_TYPES = {"video"}

#: 每个任务的溯源清单放在这里, 与媒体文件分开
META_DIR_NAME = "_meta"

#: 同名消解时最多尝试的序号, 超过就退回原名交给下载层处理(不该发生)
_MAX_SUFFIX = 999


def is_flat(rtype) -> bool:
    return (rtype or "").lower() in FLAT_TYPES


def meta_dir(base, task_id) -> Path:
    """任务溯源清单(manifest.json / album.json)的目录。

    ⚠️ 不能跟媒体文件放一起: 视频平铺在下载根目录, 清单要是也放根目录, 每跑完
    一个视频任务就会盖掉上一个的 manifest.json, 用户拿到的溯源信息是串的。
    """
    return Path(base) / META_DIR_NAME / str(task_id)


def _segments(rel):
    # ⚠️ 必须先挡 falsy: str(None) 是 "None", 会被当成一个真实的目录名,
    # 落出 `None/00001.jpg` 这种目录(曾经在预览里出现过)。
    if not rel:
        return []
    return [s for s in str(rel).replace("\\", "/").split("/") if s]


def _clean_segments(rel):
    """逐段清洗; `.` / `..` 一律丢掉 —— 它们能让路径往上跳一层。

    ⚠️ 不能指望 `clean_segment` 挡这个: 它只处理非法字符, `..` 是"合法字符
    组成的不安全段", 清洗后仍是 `..`。
    """
    out = []
    for seg in _segments(rel):
        # 纯点段(`.` / `..` / `...`)直接丢: `clean_segment` 会把结尾的点换成
        # 下划线, 于是 `..` 变成 `._` —— 不越界, 但会在下载目录里造出一个
        # 谁也看不懂的目录。宁可不认这个名字。
        if set(seg.strip()) <= {"."}:
            continue
        name = clean_segment(seg)
        if name and name not in (".", ".."):
            out.append(name)
    return out


def album_of(relative, album=None) -> str:
    """这个文件属于哪个相册文件夹。

    ① 文件名前面已有目录 -> 取最深一段(丢掉标签/模特等分类层);
    ② 没有目录(模板只给了文件名) -> 退回资源自带的 album, 同样取最深一段;
    ③ 都没有 -> 空串, 由调用方决定(通常落到下载根目录)。
    """
    segs = _clean_segments(relative)
    if len(segs) > 1:
        return segs[-2]
    album_segs = _clean_segments(album)
    return album_segs[-1] if album_segs else ""


def place(rtype, relative, album=None):
    """按布局规则归位, 返回 `(相对路径, 相册名)`。

    相册名一并返回是因为消解重名时要用它做区分词, 而那时相对路径已经被拍平成
    文件名了 —— 让调用方自己再推一次容易两边不一致。
    """
    rel = safe_relative(relative)
    if not rel:
        # 采集器给了绝对路径或含 `..` 的东西: 只留最后一段。
        # 宁可名字难看, 也不能让文件落到任务目录外面去。
        segs = _clean_segments(relative)
        rel = segs[-1] if segs else ""
    if not rel:
        return "", ""
    segs = _clean_segments(rel)
    if not segs:
        return "", ""
    name = segs[-1]
    album_name = album_of(rel, album)
    if is_flat(rtype):
        # 视频: 只留文件名, 不建文件夹
        return name, album_name
    # 非视频: 相册文件夹 + 文件名。`album_of` 已经把标签/模特等多层折叠成最深
    # 一段; 没有目录层时(如模板只给了 `{name}`)也要补一层相册目录, 否则所有
    # 资源的文件会全堆在下载根目录 —— 那正是"相册文件夹"这条铁律要挡的
    return (f"{album_name}/{name}" if album_name else name), album_name


def _candidate_names(stem, ext, tag):
    """同名时的候选名, 按优先级依次产出。

    先试"原名 + 相册名"(一眼看出它属于哪个相册, 也比 `0001(2)` 好认),
    再退回纯序号 —— 相册名取不到时只剩这条路。

    ⚠️ 两种候选**按序号交替**产出(`0001_相册` -> `0001(2)` -> `0001_相册(2)`
    ...), 不是"先把这个前缀的 999 个试完再换下一个": 占用者通常只有一两个,
    交替产出既让首选名字最快出现, 也不会在极端情况下退化成一次几百个查询。
    """
    variants = [stem]
    if tag and tag != stem:
        variants.insert(0, f"{stem}_{tag}")
    for n in range(1, _MAX_SUFFIX + 1):
        for base in variants:
            yield f"{base}{ext}" if n == 1 else f"{base}({n}){ext}"


def claim(relative, album, owner, url):
    """`relative` 已被别的来源占用时, 换一个不冲突的名字。

    owner(相对路径) -> 占用它的**来源 URL**(空串/None 表示这一格是空的)。
    与自己的 `url` 相同意味着"就是同一个文件", 沿用原名让下载层去复用/校验 ——
    那是它的本职工作, 不必在这里另造一份。

    相册名只在**平铺**文件(视频)上做区分词: 那时的文件名里看不到任何相册信息,
    `0001_葡萄一番街.mp4` 比 `0001(2).mp4` 有用得多; 而相册文件夹里的同名文件
    本来就已经在那个相册下面了, 再缀一遍相册名只是噪音。

    找不到空位时原样返回: 那种情况下交给下载层报错, 也比静默拼坏文件好。
    """
    current = owner(relative)
    if not current or current == url:
        return relative
    p = PurePosixPath(relative)
    folder = str(p.parent)
    folder = "" if folder in (".", "/") else folder
    tag = clean_segment(album or "") if not folder else ""
    for cand in _candidate_names(p.stem, p.suffix, tag):
        rel = f"{folder}/{cand}" if folder else cand
        cur = owner(rel)
        if not cur or cur == url:
            return rel
    return relative


def same_file(a, b) -> bool:
    """两个本地路径是否指向同一个文件(用于"盘上那份是不是我的")。"""
    try:
        na = os.path.normcase(os.path.abspath(str(a)))
        nb = os.path.normcase(os.path.abspath(str(b)))
    except (TypeError, ValueError):
        return False
    return na == nb
