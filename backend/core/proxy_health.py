"""代理熔断状态的持久化。

为什么需要
==========
`ProxyPool` 的失败计数与熔断到期时刻原来只活在内存里: 任务跑完条目就回收, 服务
重启更是全部清零。于是"上一轮已经发现第 2 条线是死的"这件事**下次还要重新踩一遍**
—— 每条线要先失败 `FAIL_THRESHOLD` 次才熔断, 而那几次失败落在真实资源上:
代价是几个资源白下 + 几轮重试退避, 用户看到的是"明明配了 3 条代理还是慢/还是失败"。

为什么是**进程级共享的一份 JSON**, 而不是 `tasks.options`
=======================================================
`ProxyPool` 是**每个任务**新建的(见 `task_manager._download_all`), 而"这条代理
现在通不通"与哪个任务无关 —— 用户配的常常就是同一组代理。写进各自的 options 会让
同一个坏代理在 10 个任务里各失败 3 次, 熔断等于白做。所以按代理 URL 全局分桶。

⚠️ 反向的代价也要认下来: 这里记的是"这条代理**最近**不行", 而不是"永远不行"。
所以熔断只是**冷却**(到期自动半开), 且用户手动重试时 `pick()` 在全部熔断的情况下
仍会退化为按序号返回 —— 宁可让它再试一次, 也不要因为一个陈旧的结论让用户无从下手。

落盘
====
`data/proxy_health.json`(与数据库同目录)::

    {"http://a:8080": {"fails": 3, "blocked_until": 1770000000.0}}

环境变量
========
`UWC_PROXY_HEALTH` 覆盖位置, 或设成 `off` 完全关闭::

    UWC_PROXY_HEALTH=D:/tmp/proxy.json   # 换个地方放
    UWC_PROXY_HEALTH=off                 # 不读写

⚠️ **测试必须指到临时目录**(`tests/conftest.py` 已自动做), 否则一个用例里"把
a 代理打到熔断"会直接影响下一个用例的 pick 顺序 —— 而症状是"另一个用例莫名其妙
挑中了 b 代理", 与真实原因隔了两层。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from core.jsonstore import JsonStore, OFF_VALUES

#: 记录条目的上限。代理池通常只有几条到几十条, 但这个文件**永远不会自己变小**
#: (换了代理配置就多一批键), 长期运行会单调膨胀。按最近使用截断。
MAX_ENTRIES = 100


def _path():
    """状态文件位置; None 表示被显式关闭。

    ⚠️ 每次调用重新算: 数据库路径可被环境变量/测试覆盖, 导入时算死会让测试之间
    隔着磁盘互相说话(见模块 docstring 与环境变量一节)。
    """
    raw = os.environ.get("UWC_PROXY_HEALTH")
    if raw is not None and raw.strip().lower() in OFF_VALUES:
        return None
    if raw and raw.strip():
        return Path(raw.strip())
    from core import database as db

    return Path(db.DB_PATH).parent / "proxy_health.json"


_STORE = JsonStore(_path)


def _now():
    return time.time()


def load(proxy):
    """取某条代理的持久状态, 返回 `{"fails": int, "blocked_until": float}`。

    ⚠️ 读不到就返回"干净的"状态, 而不是抛异常: 这是一份**线索**, 不是事实源。
    文件损坏/被手动改坏时, 最坏后果是"熔断状态丢了", 不该让下载起不来。
    """
    entry = (_STORE.read() or {}).get(str(proxy)) or {}
    try:
        fails = int(entry.get("fails") or 0)
    except (TypeError, ValueError):
        fails = 0
    try:
        until = float(entry.get("blocked_until") or 0.0)
    except (TypeError, ValueError):
        until = 0.0
    return {"fails": max(0, fails), "blocked_until": max(0.0, until)}


def blocked_for(proxy, now=None):
    """这条代理还要冷却多少秒(0 = 没在冷却)。到期即视为已恢复, 不再需要写入。"""
    st = load(proxy)
    left = st["blocked_until"] - (_now() if now is None else now)
    return left if left > 0 else 0.0


def note_failure(proxy, threshold, cooldown):
    """记一次失败; 达到阈值则写入冷却到期时刻。返回 (fails, blocked_until)。

    阈值与冷却时长由调用方传入而不是在这里定义: `ProxyPool` 允许按实例调整
    (`fail_threshold=` / `cooldown=`), 两处各存一份默认值迟早对不上。
    """
    if not proxy:
        return 0, 0.0
    threshold = max(1, int(threshold or 1))
    cooldown = max(0.0, float(cooldown or 0.0))

    def _mutate(data):
        entry = data.get(str(proxy)) or {}
        fails = int(entry.get("fails") or 0) + 1
        until = float(entry.get("blocked_until") or 0.0)
        if fails >= threshold:
            until = _now() + cooldown
        data[str(proxy)] = {"fails": fails, "blocked_until": until, "seen": _now()}
        _trim(data)

    data, _ok = _STORE.update(_mutate)
    entry = data.get(str(proxy)) or {}
    return int(entry.get("fails") or 0), float(entry.get("blocked_until") or 0.0)


def note_success(proxy):
    """这条代理成功传完一个资源: 清零失败与冷却(半开状态下即"恢复")。"""
    if not proxy:
        return

    def _mutate(data):
        data[str(proxy)] = {"fails": 0, "blocked_until": 0.0, "seen": _now()}

    _STORE.update(_mutate)


def clear(proxy=None):
    """忘掉一条(或全部)代理的健康记录。"""
    if proxy:
        _STORE.update(lambda data: data.pop(str(proxy), None))
        return
    _STORE.write({})


def snapshot():
    """全部记录(供诊断展示: "重启后还记得哪些线有问题")。"""
    now = _now()
    out = []
    for proxy, entry in (_STORE.read() or {}).items():
        try:
            fails = int((entry or {}).get("fails") or 0)
        except (TypeError, ValueError):
            fails = 0
        try:
            until = float((entry or {}).get("blocked_until") or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        out.append({
            "proxy": proxy,
            "fails": fails,
            "blocked_for": max(0, int(until - now)),
        })
    return out


def _trim(data):
    """只留最近见过的 MAX_ENTRIES 条, 防止文件随"历史用过的代理"单调膨胀。"""
    if len(data) <= MAX_ENTRIES:
        return
    def _seen(kv):
        try:
            return float((kv[1] or {}).get("seen") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    for key, _v in sorted(data.items(), key=_seen, reverse=True)[MAX_ENTRIES:]:
        data.pop(key, None)
