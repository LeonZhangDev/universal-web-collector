"""带并发纪律的小 JSON 状态文件(单一实现)。

为什么要单独成一个模块
======================
"一个小 JSON 文件记录运行期状态"在本项目里已经出现三次, 而它的坑**完全一样**,
三次里踩过两次:

1. **写者用 `tmp.replace(p)` 换文件** —— 原子、不会被读到半截内容;
2. **但 Windows 上只要目标文件还有别的句柄开着(哪怕只是只读), `os.replace`
   就会抛 `PermissionError`**;
3. 而这类失败通常被 `except OSError: pass` 吞掉 —— 因为"状态文件写不进去不该
   影响采集"。于是它从"数据丢失"变成了"静默丢数据"。

实测(`cdn_profile`, 2026-09-22): 一个只读线程 + 一个写线程, 300 次写入只记下
150 次 —— **丢一半且毫无声响**; 它同时是测试套件偶发变红的根源。

所以四条纪律固定在这里, 调用方不必再想:

* **读者也要进临界区**。锁只保护写者是不够的, 占用文件的正是读者;
* **锁必须是 `RLock`**。`update()` 要在同一次读-改-写里依次调 `read` / `write`,
  而这两个方法各自也要加锁 —— 普通 `Lock` 会当场自锁死, 表现是"调一次就整个
  进程卡住", 堆栈上看不出所以然;
* **`replace` 要退避重试**。进程内的占用已由锁挡掉, 剩下的只有编辑器/杀软这类
  **外部**占用, 窗口是微秒级, 重试几次就够;
* **写不进去也不抛异常**, 但**不假装成功**: `update()` 返回是否落盘, 调用方
  需要时可自行记一笔。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

#: 视为"关闭"的取值。写成集合而不是 `in ("off",)`, 因为用户会写 `0`、`no`、`false`
OFF_VALUES = {"", "0", "off", "no", "false", "none", "disable", "disabled"}


class JsonStore:
    """一个小 JSON 状态文件。`path_fn()` 每次调用都重新求值(见下)。

    ⚠️ 路径用**函数**而不是构造时定死: 数据库路径可被环境变量/测试覆盖, 在导入
    时算死会让测试之间隔着磁盘互相说话(见 tests/isolation.py 的事故记录)。
    """

    def __init__(self, path_fn, indent=1, retries=5, backoff=0.02):
        self._path_fn = path_fn
        self._indent = indent
        self._retries = max(1, int(retries))
        self._backoff = backoff
        #: ⚠️ 必须可重入, 见模块 docstring
        self._lock = threading.RLock()

    @property
    def retries(self):
        """`replace` 最多试几次。公开出来是为了让调用方/测试的断言不必去戳私有属性。"""
        return self._retries

    # ---- 路径 ----

    def path(self):
        """当前应读写的文件路径; None 表示被显式关闭。"""
        return self._path_fn()

    # ---- 读写 ----

    def read(self):
        """读出 dict; 文件不存在 / 内容不可解析 / 被关闭都返回空 dict。"""
        # 锁外取路径: path_fn 会惰性 import core.database, 别在锁里做
        p = self.path()
        if p is None:
            return {}
        with self._lock:
            if not Path(p).is_file():
                return {}
            try:
                data = json.loads(Path(p).read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (ValueError, OSError):
                return {}

    def write(self, data):
        """原子落盘。返回是否真的写成功了(调用方可据此记一笔, 但不该因此失败)。"""
        p = self.path()
        if p is None:
            return False
        p = Path(p)
        text = json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False,
                          indent=self._indent)
        with self._lock:
            for attempt in range(self._retries):
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    # 先写临时文件再替换: 半截的 JSON 会让之后每一次读取整体失效
                    tmp = p.with_name(p.name + ".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(p)
                    return True
                except OSError:
                    # 多数是"目标被外部句柄占着"。等一小会儿再换 ——
                    # **不要在这里丢掉这次更新**, 本模块唯一会静默丢数据的地方就是它。
                    if attempt + 1 < self._retries:
                        time.sleep(self._backoff * (attempt + 1))
            return False

    def update(self, mutate):
        """读-改-写。`mutate(data)` 就地改 data; 返回 (改后的 data, 是否落盘)。

        ⚠️ 整段持锁: 两个线程各自"读-改-写"同一份文件时, 后写的会把先写的整个
        覆盖掉(丢更新), 而不是合并 —— 这正是状态文件最容易出的错。
        """
        with self._lock:
            data = self.read()
            out = mutate(data)
            if isinstance(out, dict):
                data = out
            ok = self.write(data)
            return data, ok
