"""断点续传的暂存区 —— 让中断过的下载在下一次接着下, 而不是从 0 重来。

问题
====
半成品(`.part`)原先只活在目标文件旁边。同一份字节于是在三种情况下白下:

1. **取消。** 下到一半用户点了停止, 代码刻意把 `.part` 删掉(理由是不删会在相册
   目录里留下一堆 `xxx.jpg.part`)。一个 400MB 的视频下了 380MB 被取消, 那
   380MB 就没了 —— 用户再点一次"开始", 从第 0 字节重来。
2. **换路径。** 站改了相册标题、用户改了命名模板 → 目标路径变了 → 旧 `.part`
   谁也找不到(它躺在**旧**路径旁边)。
3. **失败的资源。** 走完重试链仍失败的资源, `.part` 会一直躺在相册目录里 ——
   既不清掉也不复用, 下次同名同源续传纯属"碰运气能不能命中"。

做法
====
按 **URL** 寻址, 把"暂时不下了但还想要"的半成品挪进下载根下的暂存区::

    downloads/_meta/partial/{sha1(url)}.part        单文件半成品
    downloads/_meta/partial/{sha1(清单url)}.seg/    HLS/DASH 的分片缓存(一堆 000001.part)
    downloads/_meta/partial/index.json              索引(url / 字节数 / 时间)

URL 才是这份字节的身份: 同一份字节不管当初打算落在哪个相册、哪个文件名, 都是同一
份。寻址换了维度, 上面三种情况就都变成"取回接着下"。

两类内容**同一套纪律**
======================
`.part` 是"一个文件下了一半", `.seg/` 是"HLS/DASH 的分片下了一部分" —— 对用户
来说是同一件事(下过的字节没白下), 所以 TTL、总量预算、淘汰次序全部共用。分片缓存
按**清单 URL** 寻址(HLS 的 m3u8 / DASH 的 mpd), 因为分片清单就是那份字节的身份。

⚠️ 分片缓存必须**校验清单指纹**
--------------------------------
单文件有 `.partsrc` 挡住"两个来源拼成一份"(第 6 条静默坑); 分片缓存守的是同一类
风险, 但触发方式更隐蔽: **清单 URL 没变而分片内容变了**(CDN 换代、令牌刷新后指到
另一批分片、站点重排了分片)。这时按序号复用旧分片, 拼出来的是**两份不同批次混在
一起**的字节 —— 长度对、能播一部分、错得没有声响。

所以每条分片缓存都记一份 `fingerprint`(分片 URL 列表的 sha1), **指纹不符就整份
丢弃**, 不按序号勉强复用。这与"拿不准时宁可从头下"是同一条: 重下的代价是时间,
拼错的代价是一个看起来成功的坏文件。

⚠️ 那一层防线**一字未改**
==========================
`downloaders/base.py` 的 `.part` + `.partsrc` + `os.replace` 原样保留 —— 暂存区只
决定 `.part` **从哪来、到哪去**。取回时照旧写回 `.partsrc`, 续传前的来源比对
(第 6 条静默坑: 两个不同来源被拼成一份恰好等长的文件)照旧生效。**没有**因为引入
暂存区就给续传开一条捷径。

⚠️ 什么情况下**不** park
=========================
park 的语义是"这份字节还有用"。反过来说, 结论已经明确时绝不能 park:

* **判成坏文件**(`CorruptMediaError` / `DECODE_FAILED`) —— 结论是"这堆字节是坏的"。
  park 了下次会把它取回来接着写, 把一次已知的坏结果变成**持续的**坏结果。直接删。
* **磁盘满** —— 环境问题, 残片留着或挪走一样占空间; 删掉还给磁盘一点空间。
* **源站已无(gone)** —— URL 死了, 这份字节永远等不到续传。
* **用户删了任务(带文件)** —— 用户的意思就是"不要了", 连带清掉暂存。

选错的方向性后果不对等: 把该 park 的删掉, 代价是白下一次; 把该删的 park 了,
代价是**下次取回一份已知的坏字节继续拼**。所以拿不准时用 `discard`, 不用 park。

暂存不是无限的
==============
TTL + 总量预算, 按"最久没用"淘汰。清掉只是回到"从头下", 不会下出坏文件 ——
所以这里宁可清得保守, 也别占着磁盘不还。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path

from core.config import settings
from core.jsonstore import JsonStore

#: 与 `core/layout.py` 的 `META_DIR_NAME` 同名同义: 下载根下只有这一个非媒体目录
META_DIR_NAME = "_meta"
STAGE_DIR_NAME = "partial"

#: 单文件半成品的后缀
PART_SUFFIX = ".part"
#: 分片缓存(HLS/DASH)是一个**目录**: 里面是 `000001.part` 这样的分片
SEG_SUFFIX = ".seg"
#: 索引条目区分两类内容 —— 淘汰、统计、清理都要按它分派落点
KIND_FILE = "file"
KIND_SEGMENTS = "segments"
#: 分片缓存目录里那个"这批分片属于哪一份清单"的标记文件。
#: `_fetch_segments` 只认 `{序号:06d}.part`, 所以它混在同目录里不会干扰分片序号。
FP_MARKER = "manifest.fp"
#: 暂存区里的条目长这样: `{sha1}.part` / `{sha1}.seg`。
#: 用来在磁盘上认出"索引里没有的残留"(见 `_disk_keys`)。
_KEY_RE = re.compile(r"^([0-9a-f]{40})\.(?:part|seg)$")

#: `stats()` 最多回多少条明细 —— 界面只用来给人看一眼, 不是数据接口
_MAX_ITEMS = 200

#: 最近一次"想收下但没成功"的原因。只在内存里 —— 它是诊断信息, 不是状态。
#: ⚠️ 存在的理由: `park*` 搬不动时**故意不报错**(搬不动就当没收下, 原地那份保持
#: 不动, 最坏只是下次从头下)。但那样一来"暂存区怎么永远攒不起来"就**没有任何
#: 线索** —— 而它是个持续性的、用户能察觉却说不清的现象。记下来交给 `stats()`
#: 带出去, 界面上至少能看到一句话。
_LAST_PARK_ERROR = None


def _note_park_error(what, exc):
    global _LAST_PARK_ERROR
    _LAST_PARK_ERROR = f"{what}: {type(exc).__name__}: {exc}"


def _root() -> Path:
    # ⚠️ 每次调用重新取: 下载根可被环境变量/测试覆盖(见 tests/isolation.py),
    # 模块导入时算死会让暂存区落在用户真实的下载目录里。
    return Path(settings.download_dir)


def enabled() -> bool:
    return bool(getattr(settings, "partial_staging", True))


def stage_dir() -> Path:
    return _root() / META_DIR_NAME / STAGE_DIR_NAME


def key_for(url) -> str:
    """URL -> 暂存区里的文件名(不含扩展名)。

    用 sha1 而不是"清洗后的 URL": URL 里可能带 `?token=...&expires=...` 这类长查询
    串, 直接当文件名会超长、还会把签名写进用户的目录树里。
    """
    return hashlib.sha1(str(url or "").encode("utf-8", "replace")).hexdigest()


def stage_path(url) -> Path:
    return stage_dir() / f"{key_for(url)}{PART_SUFFIX}"


def seg_dir(addr) -> Path:
    """分片缓存的目录。`addr` 是**清单 URL**(HLS 的 m3u8 / DASH 的 mpd, 带轨后缀)。"""
    return stage_dir() / f"{key_for(addr)}{SEG_SUFFIX}"


def fingerprint(segments) -> str:
    """分片清单的指纹: 回答"这份缓存还是不是同一份清单"。

    元素可以是 URL 字符串, 也可以是 `(url, (start, end))` 这样的字节区间分片
    (DASH 的 SegmentBase)。**两者都要进指纹** —— 只哈希 URL 的话, 同一个文件换了
    区间就是另一份内容, 而复用旧缓存不会报错。

    拼接时分隔符用 `\\n` 而不是直接相连: 否则 `["ab","c"]` 与 `["a","bc"]` 同哈希,
    而那正是"清单错位却指纹相同"的入口。
    """
    h = hashlib.sha1()
    for s in segments or []:
        if isinstance(s, (tuple, list)) and len(s) == 2:
            u, rng = s
            try:
                a, b = rng
            except (TypeError, ValueError):
                pass                    # 不是区间: 退回按 URL 处理, 别在这里抛
            else:
                h.update(f"{u}|{a}-{b}\n".encode("utf-8", "replace"))
                continue
        h.update(f"{s}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def ensure_fingerprint(parts_dir, segments) -> bool:
    """确保 `parts_dir` 里那堆分片属于 `segments` 这一批。返回 True = 可以安全复用。

    分片缓存与单文件 `.part` 守的是同一类风险(第 6 条静默坑: 两个不同来源被拼成
    一份), 但触发方式**更隐蔽** —— 清单 URL 没变而分片内容变了(CDN 换代、令牌
    刷新后指到另一批、站点重排了分片)。按序号复用旧分片, 拼出来就是两批字节混在
    一起: 长度对得上, 也能播一部分, 错得没有声响。

    ⚠️ 标记缺失也当"不符": 没有标记 = 这批分片**来路不明**(崩在写标记之前、
    上一个版本留下的、被手改过)。这与 `base.py` 的 `.partsrc` 是同一条判断 ——
    宁可重下, 也不拿一批来路不明的分片去拼。

    不符时**在这里删干净**再返回 False: 一个"已判定不符但还留在磁盘上"的目录是
    纯粹的陷阱, 留着早晚有人复用。调用方只管重新下。
    """
    d = Path(parts_dir)
    want = fingerprint(segments)
    try:
        got = (d / FP_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        got = ""
    if got == want:
        return True
    if _dir_size(d)[1] > 0:
        shutil.rmtree(d, ignore_errors=True)
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / FP_MARKER).write_text(want, encoding="utf-8", newline="")
    except OSError:
        # 写不进标记不该成为"下不了"的理由 —— 最坏退回改动之前的"只看存在与否"。
        pass
    return True


def _disk_keys() -> set:
    """扫一遍暂存区**磁盘上**实际存在哪些 key。

    用来认出"索引里没有、磁盘上有"的残留: 崩在字段落盘之前、索引被手删、被别的
    工具清过。这类字节没有 URL 无从复用, 也不会被任何一次按索引走的清理命中 ——
    正是最容易永久占着磁盘的那种。
    """
    out = set()
    try:
        entries = list(stage_dir().iterdir())
    except OSError:
        return out
    for f in entries:
        m = _KEY_RE.match(f.name)
        if not m:
            continue
        try:
            if f.is_dir() or f.is_file():
                out.add(m.group(1))
        except OSError:
            continue
    return out


def _dir_size(p: Path):
    """目录的 `(总字节, 文件数)`。**不抛异常** —— 遍历期间文件可能正好被挪走。

    ⚠️ 不计标记文件: `bytes` 这个字段的意思是"还能省下的下载量", 把 40 字节的
    指纹算进去只会让界面上的数字说不清。
    """
    total = 0
    n = 0
    try:
        for f in p.rglob("*"):
            if f.is_file() and f.name != FP_MARKER:
                try:
                    total += f.stat().st_size
                    n += 1
                except OSError:
                    continue
    except OSError:
        return total, n
    return total, n


def _merge_dir(src: Path, dest: Path):
    """把 `src` 里 `dest` **还没有**的文件搬进 `dest`, 然后删掉 `src`。

    为什么是"并"而不是"整体替换": 两边都可能是同一份缓存"下了一部分"的样子
    (一个躺在旧目标路径旁边, 一个在暂存区里)。整体替换必然丢掉其中一半。
    按文件名对齐是安全的 —— 分片名就是序号, 同一批清单里同序号即同一片。

    返回 `(搬过去的字节数, 搬过去的文件数)`。
    """
    moved = n = 0
    try:
        files = [f for f in src.rglob("*") if f.is_file()]
    except OSError:
        files = []
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0, 0
    for f in files:
        try:
            tgt = dest / f.relative_to(src)
            if tgt.is_file() and tgt.stat().st_size > 0:
                continue                    # 目标已有: 取回是锦上添花, 不比谁更全
            size = f.stat().st_size
            tgt.parent.mkdir(parents=True, exist_ok=True)
            os.replace(f, tgt)
        except (OSError, ValueError):
            continue
        moved += size
        n += 1
    shutil.rmtree(src, ignore_errors=True)
    return moved, n


def _entry_path(k, ent) -> Path:
    """索引条目 -> 它在暂存区里的落点。`kind` 缺省按单文件处理(兼容旧索引)。"""
    if isinstance(ent, dict) and ent.get("kind") == KIND_SEGMENTS:
        return stage_dir() / f"{k}{SEG_SUFFIX}"
    return stage_dir() / f"{k}{PART_SUFFIX}"


def _entry_size(p: Path) -> int:
    try:
        if p.is_dir():
            return _dir_size(p)[0]
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _remove_path(p: Path) -> int:
    """删掉一个落点(文件或目录), 返回释放的字节数。"""
    freed = _entry_size(p)
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        return 0
    return freed


def _remove_entry(k):
    """删掉一个 key 的**全部**落点, 返回 `(删掉几个, 释放字节)`。

    两类都试, 而不是先读索引再按 `kind` 挑: 索引可能正好缺这一条(崩溃 / 被手删),
    而"索引里没有、磁盘上有"恰恰是最该清掉的那种残留。
    """
    n = freed = 0
    for p in (stage_dir() / f"{k}{PART_SUFFIX}", stage_dir() / f"{k}{SEG_SUFFIX}"):
        try:
            exists = p.is_dir() or p.is_file()
        except OSError:
            exists = False
        if not exists:
            continue
        n += 1
        freed += _remove_path(p)
    return n, freed


def _index_path():
    # 关掉暂存时返回 None: JsonStore 会因此既不读也不写, 不留任何痕迹
    return (stage_dir() / "index.json") if enabled() else None


#: 索引文件的唯一实现走 `JsonStore` —— 与 cdn_profile / proxy_health 共用同一套
#: 并发纪律(读写都进 RLock + 原子替换 + 退避重试 + 写不进去不假装成功)。
#: ⚠️ 别为它手写第二份: 手写一遍就是少一条纪律, 而少的那条恰好是"静默丢一半"。
_index = JsonStore(_index_path)


def _min_bytes() -> int:
    try:
        return max(0, int(getattr(settings, "partial_min_bytes", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _ttl_seconds() -> float:
    try:
        return max(0.0, float(getattr(settings, "partial_ttl_hours", 0) or 0) * 3600.0)
    except (TypeError, ValueError):
        return 0.0


def _max_bytes() -> int:
    try:
        return max(0, int(getattr(settings, "partial_max_bytes", 0) or 0))
    except (TypeError, ValueError):
        return 0


# ---- 写入 / 取回 / 清理 ----

def park(part, url, sidecar=None):
    """把一份半成品收进暂存区。返回收下的字节数(**0 表示没收, 调用方自行决定怎么处理**)。

    没被收下的三种情况, 都是刻意的:
      * 暂存被关掉, 或 `url` 为空(来源不明的字节不该进按 URL 寻址的暂存区);
      * 体积小于 `partial_min_bytes` —— 为几 KB 维护一条索引, 收益是负的;
      * 文件不存在 / 搬不动(磁盘错误) —— 静默放弃, 最坏结果是下次从头下。

    sidecar: `.partsrc` 之类的附属文件, 收下内容后一并删掉(它只对"躺在原地"的
    `.part` 有意义; 进了暂存区, URL 由索引记着, 留着就是相册目录里的垃圾)。
    """
    if not enabled() or not url:
        return 0
    p = Path(part)
    try:
        size = p.stat().st_size
    except OSError:
        return 0
    if size < _min_bytes():
        return 0
    dest = stage_path(url)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and dest.stat().st_size >= size:
            # 已经有一份**不比这份小**的: 留下它, 把这份丢掉。
            # 同 URL 的两次中断会走到这里(进程被杀、两个任务同时取消)。谁更全
            # 无法在这里判定(内容没校验过), 取字节数大的那个是合理的近似。
            p.unlink(missing_ok=True)
            size = dest.stat().st_size
        else:
            os.replace(p, dest)
    except OSError as e:
        # 搬不动就当没收下 —— ⚠️ 但**不要**顺手把原地那份删了:
        # 那一份还在原地, 下次同名同源照样能续上, 删了才是真的白下。
        _note_park_error(f"单文件 {p.name}", e)
        return 0
    if sidecar:
        try:
            Path(sidecar).unlink(missing_ok=True)
        except OSError:
            pass
    now = time.time()
    entry = {"url": str(url), "bytes": int(size), "kind": KIND_FILE,
             "saved_at": now, "touched_at": now}

    def _mutate(data):
        old = data.get(key_for(url))
        if isinstance(old, dict) and old.get("saved_at"):
            entry["saved_at"] = old["saved_at"]   # 保留"第一次收下"的时间
        data[key_for(url)] = entry
        return None

    _index.update(_mutate)
    return size


def take(url, dest):
    """把暂存区里属于 `url` 的半成品搬到 `dest`(调用方给的 `.part` 位置)。

    返回搬过去的字节数(0 = 没有可用的)。搬完就从索引里摘掉 —— 索引是"待续传"
    的清单, 不是备份目录。
    """
    if not enabled() or not url:
        return 0
    dest = Path(dest)
    k = key_for(url)
    entry = _index.read().get(k)
    # ⚠️ 比对索引里记的 url: 虽然文件名就是 url 的 sha1, 但"哈希撞了/索引被手改过"
    # 这条路不该通向"把别人的字节当自己的续上" —— 与 .partsrc 是同一个理由。
    if not isinstance(entry, dict) or str(entry.get("url")) != str(url):
        return 0
    src = stage_path(url)
    if not src.is_file():
        return 0
    if dest.exists():
        # 目标位置已经有东西了(另一条路径刚写下的)。**不覆盖**: 取回是锦上添花,
        # 纠结谁更全没有意义, 让调用方按自己的逻辑处理现场那份。
        return 0
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)
        size = dest.stat().st_size
    except OSError:
        return 0

    def _mutate(data):
        data.pop(k, None)
        return None

    _index.update(_mutate)
    return size


def park_segments(addr, parts_dir, segments, origin=None):
    """把一份分片缓存(HLS/DASH)收进暂存区。返回收下的字节数(0 = 没收)。

    与 `park` 同义, 差别只在落点是**目录**。`addr` 是**清单 URL**(HLS 的 m3u8 /
    DASH 的 mpd, 带轨后缀) —— 分片清单就是这份字节的身份。

    `origin`: 这份缓存**牵涉的资源 URL**。DASH 的 `addr` 带了 `#video` 这类后缀,
    清任务时按资源 URL 精确匹配就够不着它, 那些字节会永远躺在暂存区里(界面上也
    看不见)。记下 origin 是为了让"删任务带文件"能连带收回。
    """
    if not enabled() or not addr:
        return 0
    src = Path(parts_dir)
    size, n_files = _dir_size(src)
    if n_files <= 0 or size < _min_bytes():
        return 0
    dest = seg_dir(addr)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir():
            # 已经有一份了: **合并**而不是覆盖 —— 两份都是"下了一部分",
            # 合起来才是省得最多的那一份(见 `_merge_dir`)。
            _merge_dir(src, dest)
        else:
            # 整个目录 rename: 同盘原子, 也不会撞上"复制到一半崩了"。
            os.replace(src, dest)
    except OSError as e:
        # 搬不动就当没收下 —— ⚠️ 但**不要**顺手把原地那份删了:
        # 原地还在, 同路径重跑照样能续(与 `park` 同一条)。
        _note_park_error(f"分片缓存 {src.name}", e)
        return 0
    size, _n = _dir_size(dest)
    now = time.time()
    entry = {
        "url": str(addr),
        "bytes": int(size),
        "kind": KIND_SEGMENTS,
        "segments": len(segments or []),
        "fingerprint": fingerprint(segments),
        "saved_at": now,
        "touched_at": now,
    }
    if origin:
        entry["origin"] = str(origin)

    def _mutate(data):
        old = data.get(key_for(addr))
        if isinstance(old, dict) and old.get("saved_at"):
            entry["saved_at"] = old["saved_at"]   # 保留"第一次收下"的时间
        data[key_for(addr)] = entry
        return None

    _index.update(_mutate)
    return size


def take_segments(addr, parts_dir, segments):
    """把暂存区里属于 `addr` 的分片缓存搬进 `parts_dir`。返回搬过去的字节数。

    指纹不符就**整份丢弃**再返回 0(理由见 `fingerprint` / `ensure_fingerprint`)。
    这里刻意不做"按序号挑能对上的"那种聪明事 —— 那正是把两批字节混成一份的入口。
    """
    if not enabled() or not addr:
        return 0
    dest = Path(parts_dir)
    k = key_for(addr)
    entry = _index.read().get(k)
    if (not isinstance(entry, dict)
            or str(entry.get("url")) != str(addr)
            or entry.get("kind") != KIND_SEGMENTS):
        return 0
    src = seg_dir(addr)
    if not src.is_dir():
        return 0
    if str(entry.get("fingerprint") or "") != fingerprint(segments):
        clear(addr)
        return 0
    moved, _n = _merge_dir(src, dest)

    def _mutate(data):
        data.pop(k, None)
        return None

    _index.update(_mutate)
    return moved


def clear(url=None):
    """清掉暂存(不传 url 则清空)。返回 `(个数, 字节数)`。

    用在: 用户点"释放空间"、删任务(带文件)、以及 TTL/预算淘汰。

    ⚠️ 传 `url` 时**连带清掉由它派生出来的条目**: DASH 的分片缓存按
    `清单url#video` 寻址(两条轨各一份), 只做精确匹配的话, 用户"删任务带文件"
    之后这些字节会永远躺在暂存区里 —— 界面上看不见, 也不会被任何一次清理命中。
    所以索引条目里记了 `origin`, 这里一并回收。

    不传 `url` 时连**索引里没有的残留**一起清: 那正是"释放空间"这个动作最该
    覆盖的东西(见 `_disk_keys`)。
    """
    if not enabled():
        return 0, 0
    data = _index.read()
    if url:
        keys = {key_for(url)}
        keys |= {k for k, ent in data.items()
                 if isinstance(ent, dict) and str(ent.get("origin")) == str(url)}
    else:
        keys = set(data.keys()) | _disk_keys()
    n = freed = 0
    for k in keys:
        a, b = _remove_entry(k)
        n += a
        freed += b

    def _mutate(d):
        if url:
            for k in keys:
                d.pop(k, None)
        else:
            d.clear()
        return None

    _index.update(_mutate)
    return n, freed


def clear_urls(urls):
    """按 URL 批量清理(删任务带文件时用)。返回 `(个数, 字节数)`。"""
    n = freed = 0
    for u in urls or []:
        a, b = clear(u)
        n += a
        freed += b
    return n, freed


def sweep(now=None):
    """按 TTL 与总量预算清理。返回 `(删掉几个, 释放字节)`。

    三件事一起做:
      * **自愈**: 索引里指向已不存在的文件的条目直接摘掉(用户手动删过、盘被清过);
      * **回收残骸**: 磁盘上有、索引里没有的条目一律清掉 —— 没有 URL 就无从复用,
        也不会被任何按索引走的清理命中, 是最容易永久占着磁盘的那一类;
      * **淘汰**: 先按 TTL 去掉过期的, 再按"最久没用"砍到预算以内。

    ⚠️ 淘汰**不会**下出坏文件: 清掉的只是"还能省一次重下"的资本。
    """
    if not enabled():
        return 0, 0
    now = float(now if now is not None else time.time())
    data = _index.read()
    # 磁盘上有、索引里没有的残骸。放在这里算(而不是淘汰之后)是因为它与 TTL/预算
    # 无关: 没有 URL 就无从复用, 也就没有"留着还有用"这一说。
    # ⚠️ 判据要用 `data` 的 key 而不是 `alive` 的: 二者之差只有"索引里有、磁盘上
    # 没有"那一种, 而那种本来就不在磁盘上, 不会进 `_disk_keys()`。
    orphans = _disk_keys() - set(data.keys())
    if not data and not orphans:
        return 0, 0
    ttl = _ttl_seconds()
    budget = _max_bytes()

    alive = {}
    for k, ent in data.items():
        if not isinstance(ent, dict):
            continue
        p = _entry_path(k, ent)
        size = _entry_size(p)
        if size <= 0:
            continue                        # 内容没了 -> 索引条目是空头支票
        ent["bytes"] = int(size)
        alive[k] = ent

    doomed = set()
    if ttl > 0:
        for k, ent in alive.items():
            if now - float(ent.get("touched_at") or ent.get("saved_at") or 0) > ttl:
                doomed.add(k)
    keep = {k: v for k, v in alive.items() if k not in doomed}
    if budget > 0:
        total = sum(int(v.get("bytes") or 0) for v in keep.values())
        if total > budget:
            # 最久没用的先走 —— "最近还在续传的"才最可能有下一半
            order = sorted(keep.items(),
                           key=lambda kv: float(kv[1].get("touched_at") or 0))
            for k, v in order:
                if total <= budget:
                    break
                total -= int(v.get("bytes") or 0)
                doomed.add(k)
    # 索引里没有的残骸一并走(理由见 docstring)
    doomed |= orphans

    if not doomed:
        _index.write(alive)     # 顺手把自愈结果落盘(条目可能刚被摘掉)
        return 0, 0

    n = freed = 0
    for k in doomed:
        a, b = _remove_entry(k)
        n += a
        freed += b

    def _mutate(d):
        for k in doomed:
            d.pop(k, None)
        return None

    _index.update(_mutate)
    return n, freed


def stats():
    """暂存区现状, 供接口/界面展示。**不抛异常**: 它是只读的观测, 不该有能力影响流程。"""
    out = {
        "enabled": enabled(),
        "dir": str(stage_dir()),
        "count": 0,
        "bytes": 0,
        "files": 0,
        "bundles": 0,
        "ttl_hours": getattr(settings, "partial_ttl_hours", 0),
        "max_bytes": _max_bytes(),
        "min_bytes": _min_bytes(),
        # 最近一次"想收下但搬不动"的原因。⚠️ 它是**诊断**不是状态: 只在内存里,
        # 进程重启就没了 —— 但那正是它该有的寿命("这次的暂存为什么不生效")。
        "last_error": _LAST_PARK_ERROR,
        "items": [],
    }
    if not enabled():
        return out
    try:
        data = _index.read()
    except Exception:
        return out
    items = []
    total = 0
    bundles = 0
    for k, ent in data.items():
        if not isinstance(ent, dict):
            continue
        p = _entry_path(k, ent)
        size = _entry_size(p)
        if size <= 0:
            continue                    # 内容已不在: 不计入(下次 sweep 会摘掉条目)
        kind = ent.get("kind") or KIND_FILE
        if kind == KIND_SEGMENTS:
            bundles += 1
        total += size
        items.append({
            "key": k,
            "url": ent.get("url"),
            "kind": kind,
            "bytes": size,
            "segments": ent.get("segments"),
            "saved_at": ent.get("saved_at"),
            "touched_at": ent.get("touched_at"),
            "age_seconds": max(0.0, time.time() - float(
                ent.get("touched_at") or ent.get("saved_at") or 0)),
        })
    items.sort(key=lambda x: -float(x.get("touched_at") or 0))
    out["count"] = len(items)
    out["bytes"] = total
    out["bundles"] = bundles
    out["files"] = len(items) - bundles
    out["items"] = items[:_MAX_ITEMS]
    return out
