"""删除任务: 记录与文件是两件事。

默认只删记录、保留磁盘文件 —— 因为
  * 去重机制下别的任务可能正在引用这些文件(删了会让它们的 manifest 指向空),
  * "从列表里划掉一行"和"把已下载的东西删掉"应该分开决定。
另外删除时路径算错就等于删用户的目录, 所以越界校验必须有测试兜着。
"""
import time

import pytest

import core.database as db
import core.task_manager as tm


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture
def env(tmp_path, tmp_db, monkeypatch):
    """隔离的下载根目录 + 一个可正常 shutdown 的 TaskManager。"""

    class Env:
        db = tmp_db
        base = tmp_path / "dl"

        def __init__(self):
            self.created = []

        def manager(self):
            m = tm.TaskManager(max_workers=1, download_workers=1)
            self.created.append(m)
            return m

        def shutdown(self):
            for m in self.created:
                m.shutdown()

    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    e = Env()
    yield e
    e.shutdown()


def _task_with_files(env, name, n=2, size=100):
    """建一个任务并在它的下载目录里放几个文件, 返回 (task_id, 目录)。"""
    tid = env.db.create_task("https://x/album", "generic", None, {})
    d = env.base / str(tid)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        (d / f"{i:05d}.jpg").write_bytes(b"j" * size)
    return tid, d


def test_delete_keeps_files_by_default(env):
    tid, d = _task_with_files(env, "keep")
    mgr = env.manager()

    info = mgr.delete(tid)

    assert info == {"files": 0, "bytes": 0}
    assert env.db.get_task(tid) is None, "记录应被删除"
    assert d.is_dir(), "默认必须保留磁盘文件"
    assert len(list(d.iterdir())) == 2


def test_delete_with_files_removes_dir_and_reports(env):
    tid, d = _task_with_files(env, "purge", n=3, size=100)
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info["files"] == 3
    assert info["bytes"] == 300
    assert not d.exists(), "选了删文件就必须真的删掉"
    assert env.db.get_task(tid) is None


def test_delete_with_files_tolerates_missing_dir(env):
    """目录本来就不存在(比如用户手工删过)时不能报错。"""
    tid = env.db.create_task("https://x/album", "generic", None, {})
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info == {"files": 0, "bytes": 0}


def test_delete_unknown_task_is_noop(env):
    mgr = env.manager()
    assert mgr.delete(999, with_files=True) == {"files": 0, "bytes": 0}


def test_purge_never_touches_download_root_itself(env):
    """越界保护: 下载根目录里放的"别人的东西"一个都不能动。

    这里把 task_id 目录构造成不存在, 并确认根目录里的旁支文件安然无恙。
    """
    env.base.mkdir(parents=True, exist_ok=True)
    bystander = env.base / "keep-me.txt"
    bystander.write_bytes(b"important")
    tid = env.db.create_task("https://x/album", "generic", None, {})
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info == {"files": 0, "bytes": 0}
    assert bystander.exists(), "绝不能删到下载根目录下的其他内容"


def test_purge_respects_custom_download_dir(env, tmp_path):
    """自定义输出目录下同样只删 <dir>/<task_id>/ 这一层。"""
    custom = tmp_path / "user-picked"
    custom.mkdir(parents=True, exist_ok=True)
    outsider = custom / "别删我.txt"
    outsider.write_bytes(b"x")
    tid = env.db.create_task("https://x/album", "generic", str(custom), {})
    inside = custom / str(tid)
    inside.mkdir(parents=True)
    (inside / "a.jpg").write_bytes(b"y" * 10)
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info["files"] == 1 and info["bytes"] == 10
    assert not inside.exists()
    assert outsider.exists(), "自定义目录里的其他文件不能被牵连"


def test_bulk_selects_only_finished_states(env):
    """批量删除的候选集: 只挑已结束的状态, 运行中的不能碰。"""
    a = env.db.create_task("https://x/a", "generic", None, {})
    b = env.db.create_task("https://x/b", "generic", None, {})
    c = env.db.create_task("https://x/c", "generic", None, {})
    env.db.update_task_status(a, "success")
    env.db.update_task_status(b, "failed")
    # c 留在 pending(属于运行中状态)

    wanted = [s for s in ("success", "failed", "cancelled", "partial")
              if s not in tm.ACTIVE_STATES]
    rows = env.db.iter_tasks_with_status(wanted)
    ids = {r["id"] for r in rows}

    assert ids == {a, b}
    assert c not in ids, "pending 的任务不能被批量删除"


def test_storage_overview_counts(env):
    a = env.db.create_task("https://x/a", "generic", None, {})
    env.db.update_task_status(a, "success")
    rid = env.db.add_resource(a, "image", "https://img/1.jpg", status="done")
    env.db.update_resource(rid, size=1234)

    by_status = env.db.count_tasks_by_status()
    assert by_status.get("success") == 1
    row = env.db.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes"
        " FROM resources WHERE status='done'"
    )[0]
    assert (row["n"], row["bytes"]) == (1, 1234)


# ---- 零资源任务必须报失败 ----
#
# 旧行为: 采集器一个资源都没发现时照常走完, 判定 success。
# 于是"URL 解析错了, 什么都没下到"和"全部下好了"在界面上长得一模一样。


@pytest.fixture
def empty_collector():
    from collectors import register

    @register("empty_spider_for_test")
    class Empty:
        def crawl(self, url, **kw):
            return []

    return "empty_spider_for_test"


def _wait_done(tid, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = db.get_task(tid)
        if t and t["status"] not in tm.ACTIVE_STATES:
            return t
        time.sleep(0.05)
    raise AssertionError(f"任务在 {timeout}s 内没有结束: {db.get_task(tid)['status']}")


def test_task_with_zero_resources_fails_not_succeeds(env, empty_collector):
    tid = env.db.create_task("https://x/no-images", empty_collector, None, {})
    mgr = env.manager()
    mgr.submit(tid)

    task = _wait_done(tid)

    assert task["status"] == "failed", "0 个资源不能算成功"
    assert "未发现任何资源" in (task["error"] or "")
    logs = [l["message"] for l in env.db.get_logs(tid)]
    assert any("未发现任何资源" in m or "task failed" in m for m in logs)


# ---- 看门狗不能用"自己那本 _active"当唯一依据 ----
#
# 真实踩到: 诊断脚本(这里就是测试)另起一个 TaskManager 去观察任务, 模块级
# 单例的看门狗同时也在跑。它看到 DB 里有个活动任务、自己的 _active 里却没有,
# 就判成"服务重启遗留"标 failed —— 而那个任务的 worker 正在正常下载。
# 这个坑很隐蔽: 生产单进程下不会出现, 但测试/脚本一创建第二个实例就中招。


@pytest.fixture
def slow_collector():
    import time as _t

    from collectors import register

    @register("slow_spider_for_test")
    class Slow:
        def crawl(self, url, **kw):
            _t.sleep(2.0)
            return []

    return "slow_spider_for_test"


def test_other_manager_watchdog_does_not_kill_running_task(env, slow_collector):
    tid = env.db.create_task("https://x/slow", slow_collector, None, {})
    a = env.manager()
    a.submit(tid)
    deadline = time.time() + 5
    while time.time() < deadline and env.db.get_task(tid)["status"] == "pending":
        time.sleep(0.05)
    assert env.db.get_task(tid)["status"] in tm.ACTIVE_STATES

    other = env.manager()  # 模拟"另一个实例"
    other._watchdog_pass()

    task = env.db.get_task(tid)
    assert task["status"] in tm.ACTIVE_STATES, "别的实例正在跑的任务不能被判成 stale"
    assert "stale" not in (task["error"] or "")


def test_watchdog_still_reclaims_orphan_task(env):
    """真正没人持有的任务(服务重启遗留)仍必须被回收。"""
    tid = env.db.create_task("https://x/orphan", "generic", None, {})
    env.db.update_task_status(tid, "downloading")

    mgr = env.manager()
    mgr._watchdog_pass()

    task = env.db.get_task(tid)
    assert task["status"] == "failed"
    assert "stale" in (task["error"] or "")


# ---- 看门狗不能用"自己那本 _active"当唯一依据 ----
#
# 真实踩到: 诊断脚本(这里就是测试)另起一个 TaskManager 去观察任务, 模块级
# 单例的看门狗同时也在跑。它看到 DB 里有个活动任务、自己的 _active 里却没有,
# 就判成"服务重启遗留"标 failed —— 而那个任务的 worker 正在正常下载。
# 这个坑很隐蔽: 生产单进程下不会出现, 但测试/脚本一创建第二个实例就中招。


@pytest.fixture
def slow_collector():
    import time as _t

    from collectors import register

    @register("slow_spider_for_test")
    class Slow:
        def crawl(self, url, **kw):
            _t.sleep(2.0)
            return []

    return "slow_spider_for_test"


def test_other_manager_watchdog_does_not_kill_running_task(env, slow_collector):
    tid = env.db.create_task("https://x/slow", slow_collector, None, {})
    a = env.manager()
    a.submit(tid)
    deadline = time.time() + 5
    while time.time() < deadline and env.db.get_task(tid)["status"] == "pending":
        time.sleep(0.05)
    assert env.db.get_task(tid)["status"] in tm.ACTIVE_STATES

    other = env.manager()  # 模拟"另一个实例"
    other._watchdog_pass()

    task = env.db.get_task(tid)
    assert task["status"] in tm.ACTIVE_STATES, "别的实例正在跑的任务不能被判成 stale"
    assert "stale" not in (task["error"] or "")


def test_watchdog_still_reclaims_orphan_task(env):
    """真正没人持有的任务(服务重启遗留)仍必须被回收。"""
    tid = env.db.create_task("https://x/orphan", "generic", None, {})
    env.db.update_task_status(tid, "downloading")

    mgr = env.manager()
    mgr._watchdog_pass()

    task = env.db.get_task(tid)
    assert task["status"] == "failed"
    assert "stale" in (task["error"] or "")
