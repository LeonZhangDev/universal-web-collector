"""运行期共享磁盘状态的隔离清单（测试专用）。

⚠️ 为什么单开一个文件: 这类事故的根因从来不是逻辑错, 而是**两个用例隔着磁盘
互相说话**。第一次是 CDN 画像(见 conftest), 这次轮到数据库与下载目录 —— 后者
比画像危险得多, 因为里面装的是**用户的真实数据**: 一个忘了加夹具的新用例调一
次 `db.create_task`, 用户的任务列表里就多出一条假记录, 界面上看不出是测试写的。

集中登记在这里, 是为了让"新增一处共享状态"只有**唯一入口**:

- 加一行, 所有用例自动隔离;
- 忘登记, 后果也很清楚 —— 测试会往用户真实目录里写, 而且不报错。

用法: `from isolation import isolate` 然后 `isolate(monkeypatch, tmp_path)`。
`tests/conftest.py` 已对所有用例自动施加, 单个用例不需要做任何事。
"""

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 项目根

#: (模块, 属性) -> 重定向到 root 下的相对路径。
#: ⚠️ 只列**模块级把配置拷成常量**的地方 —— 它们是 `monkeypatch.setattr(settings, ...)`
#: 打不到的那批: 导入时就取值一分钱转手就再也不看 settings 了。
#: (目前两处: database.DB_PATH 与 task_manager.DOWNLOADS_DIR, 踩过)
MODULE_TARGETS = {
    ("core.database", "DB_PATH"): "collector.db",
    ("core.task_manager", "DOWNLOADS_DIR"): "downloads",
}

#: settings 对象上的路径字段。运行时才读取, 所以 redirect 之后立即生效。
SETTINGS_TARGETS = {
    "db_path": "collector.db",
    "download_dir": "downloads",
    "browser_state_dir": "browser_state",
}


def real_paths():
    """未经隔离时的真实路径, 用于"有没有被写脏"的守卫。

    直接写死默认布局而不是读 settings: 跑到守卫这一步时 settings 已经被改了,
    读出来的是隔离后的假路径, 守卫就永远绿。
    """
    return {
        "db": ROOT / "data" / "collector.db",
        "downloads": ROOT / "downloads",
        "browser_state": ROOT / "browser_state",
    }


def isolate(monkeypatch, root):
    """把清单上的所有共享路径指到 root 之下。

    ⚠️ raising 保持默认(True): 名字写错就该立刻炸 —— 静默跳过等于隔离失效,
    而失效的表现是"测试写了用户的真库", 离发现那天可能隔好几轮提交。
    """
    root = Path(root)
    for (mod_name, attr), rel in MODULE_TARGETS.items():
        mod = importlib.import_module(mod_name)
        monkeypatch.setattr(mod, attr, root / rel)

    from core.config import settings
    for field, rel in SETTINGS_TARGETS.items():
        monkeypatch.setattr(settings, field, root / rel)

    # ⚠️ 连接单例必须跟着换: `_conn` 一旦在上一个用例里建过, 它就永远指向那个
    # 文件 —— 光改 DB_PATH 没用, 后面所有写操作仍然落在旧库里, 症状是
    # "用例之间突然能看见彼此的数据", 而 DB_PATH 显示是对的。
    import core.database as db
    monkeypatch.setattr(db, "_conn", None)
    return root


def changed_keys(before, after):
    """对比两次指纹, 返回被写脏的键。单独成函数是为了能被测试直接验证 ——

    守卫逻辑塞在 conftest 的 fixture 里, 就没法单测了; 而"守卫本身失效"恰恰是
    最需要先锁住的东西。
    """
    return [k for k in before if before.get(k) != after.get(k)]


def snapshot_real():
    """给真实目录拍一张指纹, 用来判断本轮有没有脏写落在外面。

    只看 size/mtime, 不打开数据库 —— 打开本身就是在碰用户的东西,
    而且 WAL 模式下只读连接也可能生成 -wal/-shm 文件, 反而制造噪声。
    """
    out = {}
    for key, p in real_paths().items():
        try:
            st = p.stat()
            out[key] = (st.st_size, st.st_mtime_ns)
        except OSError:
            out[key] = None
    return out
