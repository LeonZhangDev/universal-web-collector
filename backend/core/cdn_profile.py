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

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
#: 每站点只留最近命中的前 N 条, 防止长期运行后文件无限膨胀
_MAX_BASES = 8

#: 视为"关闭"的取值。写成集合而不是 `in ("off",)`, 因为用户会写 `0`、`no`、`false`
_OFF = {"", "0", "off", "no", "false", "none", "disable", "disabled"}


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


def _read():
    p = _path()
    if p is None or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _write(data):
    p = _path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换: 半截的 JSON 会让之后每一次读取整体失效
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(p)
    except OSError:
        pass    # 画像写不进去不该影响采集


def record_hit(site_name, base, seq_format=None):
    """记一次命中。⚠️ 绝不抛异常 —— 它只是优化, 不该有能力让采集失败。"""
    if not site_name or not base:
        return
    try:
        with _LOCK:
            data = _read()
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
            _write(data)
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
    with _LOCK:
        if site_name:
            data = _read()
            data.pop(str(site_name), None)
            _write(data)
            return
        p = _path()
        if p is None:
            return
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
