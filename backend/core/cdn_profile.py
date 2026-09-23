"""站点 CDN 画像: 记住"哪条基址真的命中过", 用来给候选探测排序。

为什么需要
==========
同一站点会把不同相册分到 `photos` / `photos2` / `photos3` 等不同子路径, 序号
补零位数也可能不同。候选探测本身能兜住(见 `collectors/gallery_base._resolve_base`),
但顺序是**固定**的 —— 若该站大多数相册都在 `photos2`, 每个新相册都要先白试一次
`photos` 才轮到它, 相册一多这笔开销就显眼。

画像还回答另一个问题: **站点是不是正在迁移 CDN**。每次任务实际生效的基址都记
一笔, 攒起来就能看出某条路径的命中率何时塌掉 —— 在用户报"某天开始全失败"
之前就看得见。

落盘
====
一个 JSON 文件, 与数据库同目录(`data/cdn_profile.json`)::

    {
      "xchina_gallery": {
        "bases": {"https://img.xchina.io/photos2": 12},
        "seq_formats": {"{seq:04d}": 12},
        "last": "https://img.xchina.io/photos2"
      }
    }

⚠️ 画像只是**线索**不是结论: 排序靠前只意味着"先试它", 每条候选仍然真探一次。
画像缺失/损坏/写不进去都不影响采集 —— 一律退回站点声明的固定顺序。

并发
====
读者(`preferred_bases` / `preferred_seq_format` / `summarize`)与写者
(`record_hit` / `reset`)共用一把**可重入**锁。这不是"顺手加个锁", 而是必须:
写者靠 `tmp.replace(p)` 换文件, 而 Windows 上只要目标还有别的句柄开着就替换不了,
失败又是静默的 —— 探测和记录一并发, 命中就丢一半(见 `_read` 的实测数据)。

环境变量
========
`UWC_CDN_PROFILE` 可以覆盖画像文件位置, 或设成 `off` 完全关闭::

    UWC_CDN_PROFILE=D:/tmp/cdn.json     # 换个地方放
    UWC_CDN_PROFILE=off                 # 不读写画像

**测试必须把它指到临时目录**(`tests/conftest.py` 里已自动做)。理由不是"保持仓库
干净", 而是**测试会互相污染且污染方式是隐式的**: 一个用例探测到 photos2 命中,
下一个用例的候选顺序就被改掉了, 于是"happy path 不该多花请求"这类断言随机失败,
而失败现象与真实原因隔了两层。排查过一次就会明白这个 env 值有多值。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.jsonstore import JsonStore, OFF_VALUES

#: 每站点只留最近命中的前 N 条, 防止长期运行后文件无限膨胀
_MAX_BASES = 8

#: 视为"关闭"的取值(见 jsonstore.OFF_VALUES)
_OFF = OFF_VALUES


def _path():
    """画像文件位置; 返回 None 表示画像被显式关闭。

    ⚠️ **每次调用重新算**: 数据库路径可被环境变量/测试覆盖(见 `database.DB_PATH`),
    在导入时算死会让测试之间互相污染(见模块 docstring 的"环境变量"一节)。
    """
    raw = os.environ.get("UWC_CDN_PROFILE")
    if raw is not None and raw.strip().lower() in _OFF:
        return None
    if raw and raw.strip():
        return Path(raw.strip())
    from core import database as db

    return Path(db.DB_PATH).parent / "cdn_profile.json"


#: 读写、加锁、`replace` 退避重试全部收敛在 `JsonStore` 里(见其 docstring:
#: Windows 上目标被任何句柄占着就替换不了, 而这类失败必须重试而不是被吞掉)。
_STORE = JsonStore(_path)

#: 保留旧名字: 既有测试与诊断脚本会读它来断言"重试了整整一轮才放弃"。
_WRITE_RETRIES = _STORE.retries


def _read():
    """读画像; 文件不存在或内容不可解析都退化成"没有画像"。

    ⚠️ 读者必须与写者互斥 —— 这里修的是一个**静默丢更新**的缺陷。写者用
    `tmp.replace(p)` 换文件, 而在 Windows 上只要目标文件还有别的句柄开着(哪怕只是
    只读), `os.replace` 就会以 PermissionError 失败。旧代码把读者放在锁外, 于是
    "有任务正在探测基址(读)"和"另一个任务命中并记一笔(写)"一并发就丢 —— 实测
    "一个只读线程 + 一个写线程", 300 次写入只记下 150 次, **丢一半且毫无声响**。
    """
    return _STORE.read()


def _write(data):
    return _STORE.write(data)


def record_hit(site_name, base, seq_format=None):
    """记一次命中。⚠️ 绝不抛异常 —— 它只是优化, 不该有能力让采集失败。"""
    if not site_name or not base:
        return
    try:
        def _mutate(data):
            entry = data.setdefault(str(site_name), {})
            bases = entry.setdefault("bases", {})
            bases[str(base)] = int(bases.get(str(base)) or 0) + 1
            # 按命中次数降序截断, 只留下活跃的那几条
            entry["bases"] = dict(
                sorted(bases.items(), key=lambda kv: -kv[1])[:_MAX_BASES]
            )
            if seq_format:
                fmts = entry.setdefault("seq_formats", {})
                fmts[str(seq_format)] = int(fmts.get(str(seq_format)) or 0) + 1
            entry["last"] = str(base)

        # ⚠️ 走 update(读-改-写一次性完成), 不要"先 _read 再 _write": 那样两个线程
        # 会各自读到同一份旧数据、各自 +1, 后写的把先写的整个覆盖 —— 命中数少算,
        # 而现象只是"画像里的占比看着不太对", 没人会怀疑到并发上。
        _STORE.update(_mutate)
    except Exception:
        pass


def preferred_bases(site_name):
    """按"最近命中优先"给出该站点的基址顺序; 没记录返回空列表。

    用 `last` 打头: 同一轮任务里连续几个相册通常落在同一子路径; 其余按命中次数。
    """
    entry = (_read() or {}).get(str(site_name)) or {}
    bases = entry.get("bases") or {}
    out = []
    last = entry.get("last")
    if last and last in bases:
        out.append(last)
    for base, _n in sorted(bases.items(), key=lambda kv: -kv[1]):
        if base not in out:
            out.append(base)
    return out


def preferred_seq_format(site_name):
    """该站点最常命中的序号格式; 没有返回 None。"""
    fmts = ((_read() or {}).get(str(site_name)) or {}).get("seq_formats") or {}
    if not fmts:
        return None
    return sorted(fmts.items(), key=lambda kv: -kv[1])[0][0]


def summarize(site_name=None):
    """站点 CDN 画像快照, 供排查"是不是站点在迁移 CDN"。

    每站点给出各基址的命中次数与占比、序号格式分布、最近一次命中的基址。
    """
    data = _read()
    if site_name:
        data = {site_name: data[str(site_name)]} if str(site_name) in data else {}
    out = {}
    for name, entry in data.items():
        bases = entry.get("bases") or {}
        counts = {b: int(n or 0) for b, n in bases.items()}
        total = sum(counts.values()) or 1
        out[name] = {
            "total_hits": sum(counts.values()),
            "bases": sorted(
                (
                    {"base": b, "hits": n, "share": round(n / total, 3)}
                    for b, n in counts.items()
                ),
                key=lambda d: -d["hits"],
            ),
            "seq_formats": entry.get("seq_formats") or {},
            "last": entry.get("last"),
        }
    return out


def reset(site_name=None):
    """清空画像(测试/手动重置用)。不传 site_name 则整份删掉。"""
    if site_name:
        _STORE.update(lambda data: data.pop(str(site_name), None))
        return
    p = _path()
    if p is None:
        return
    try:
        Path(p).unlink(missing_ok=True)
    except OSError:
        pass
