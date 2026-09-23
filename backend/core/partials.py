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

    downloads/_meta/partial/{sha1(url)}.part      内容
    downloads/_meta/partial/index.json            索引(url / 字节数 / 时间)

URL 才是这份字节的身份: 同一份字节不管当初打算落在哪个相册、哪个文件名, 都是同一
份。寻址换了维度, 上面三种情况就都变成"取回接着下"。

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
import time
from pathlib import Path

from core.config import settings
from core.jsonstore import JsonStore

#: 与 `core/layout.py` 的 `META_DIR_NAME` 同名同义: 下载根下只有这一个非媒体目录
META_DIR_NAME = "_meta"
STAGE_DIR_NAME = "partial"

#: `stats()` 最多回多少条明细 —— 界面只用来给人看一眼, 不是数据接口
_MAX_ITEMS = 200


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
    return stage_dir() / f"{key_for(url)}.part"


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
    except OSError:
        # 搬不动就当没收下 —— ⚠️ 但**不要**顺手把原地那份删了:
        # 那一份还在原地, 下次同名同源照样能续上, 删了才是真的白下。
        return 0
    if sidecar:
        try:
            Path(sidecar).unlink(missing_ok=True)
        except OSError:
            pass
    now = time.time()
    entry = {"url": str(url), "bytes": int(size),
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


def clear(url=None):
    """清掉暂存(不传 url 则清空)。返回 `(个数, 字节数)`。

    用在: 用户点"释放空间"、删任务(带文件)、以及 TTL/预算淘汰。
    """
    if not enabled():
        return 0, 0
    if url:
        keys = [key_for(url)]
    else:
        keys = list(_index.read().keys())
    freed = 0
    n = 0
    for k in keys:
        f = stage_dir() / f"{k}.part"
        try:
            if f.is_file():
                freed += f.stat().st_size
                n += 1
            f.unlink(missing_ok=True)
        except OSError:
            continue

    def _mutate(data):
        if url:
            data.pop(key_for(url), None)
        else:
            data.clear()
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

    两件事一起做:
      * **自愈**: 索引里指向已不存在的文件的条目直接摘掉(用户手动删过、盘被清过);
      * **淘汰**: 先按 TTL 去掉过期的, 再按"最久没用"砍到预算以内。

    ⚠️ 淘汰**不会**下出坏文件: 清掉的只是"还能省一次重下"的资本。
    """
    if not enabled():
        return 0, 0
    now = float(now if now is not None else time.time())
    data = _index.read()
    if not data:
        return 0, 0
    ttl = _ttl_seconds()
    budget = _max_bytes()

    alive = {}
    for k, ent in data.items():
        if not isinstance(ent, dict):
            continue
        f = stage_dir() / f"{k}.part"
        if not f.is_file():
            continue                        # 内容没了 -> 索引条目是空头支票
        try:
            ent["bytes"] = int(f.stat().st_size)
        except (OSError, TypeError, ValueError):
            continue
        alive[k] = ent

    doomed = []
    if ttl > 0:
        for k, ent in alive.items():
            if now - float(ent.get("touched_at") or ent.get("saved_at") or 0) > ttl:
                doomed.append(k)
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
                doomed.append(k)

    if not doomed:
        _index.write(alive)     # 顺手把自愈结果落盘(条目可能刚被摘掉)
        return 0, 0

    n = freed = 0
    for k in set(doomed):
        f = stage_dir() / f"{k}.part"
        try:
            if f.is_file():
                freed += f.stat().st_size
                n += 1
            f.unlink(missing_ok=True)
        except OSError:
            continue

    def _mutate(d):
        for k in set(doomed):
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
        "ttl_hours": getattr(settings, "partial_ttl_hours", 0),
        "max_bytes": _max_bytes(),
        "min_bytes": _min_bytes(),
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
    for k, ent in data.items():
        if not isinstance(ent, dict):
            continue
        f = stage_dir() / f"{k}.part"
        try:
            size = f.stat().st_size
        except OSError:
            continue                    # 内容已不在: 不计入(下次 sweep 会摘掉条目)
        total += size
        items.append({
            "key": k,
            "url": ent.get("url"),
            "bytes": size,
            "saved_at": ent.get("saved_at"),
            "touched_at": ent.get("touched_at"),
            "age_seconds": max(0.0, time.time() - float(
                ent.get("touched_at") or ent.get("saved_at") or 0)),
        })
    items.sort(key=lambda x: -float(x.get("touched_at") or 0))
    out["count"] = len(items)
    out["bytes"] = total
    out["items"] = items[:_MAX_ITEMS]
    return out
