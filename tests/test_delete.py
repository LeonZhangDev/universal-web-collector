"""删除任务: 记录与文件是两件事。

默认只删记录、保留磁盘文件 —— 因为
  * 去重机制下别的任务可能正在引用这些文件(删了会让它们的 manifest 指向空),
  * "从列表里划掉一行"和"把已下载的东西删掉"应该分开决定。
另外删除时路径算错就等于删用户的目录, 所以越界校验必须有测试兜着。

⚠️ 布局改版后文件**不再**集中在 `<下载根>/<任务ID>/`: 它们在
`<下载根>/<相册名>/`(视频直接平铺在下载根), 与别的任务共用同一个根。所以
"删文件"不再是删目录, 而是**按库里的记录逐条删** —— 这里的用例正是守着这条。
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
            # wait=True 是必须的: 本文件里有个用例故意让采集器 sleep 2 秒,
            # 不等它跑完, 它就会在**下一个用例**里继续写库(DB 连接是模块级、
            # 被 monkeypatch 换过的), 把错误写进下一个用例的任务里 ——
            # 表现为"看门狗用例随机失败", 曾真的坑过一次。
            for m in self.created:
                m.shutdown(wait=True)

    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    e = Env()
    yield e
    e.shutdown()


def _task_with_files(env, name, n=2, size=100, album="套图名", tid=None):
    """建任务 + 按**新布局**落几个文件, 返回 (task_id, 文件路径列表)。

    ⚠️ 文件与库记录要配套: 删除是按库里那些 `filename`/`local_path` 走的,
    只落文件不写库(或反过来)都测不出真实行为。
    """
    if tid is None:
        tid = env.db.create_task(f"https://x/{name}", "generic", None, {})
    paths = []
    for i in range(1, n + 1):
        rel = f"{album}/{i:05d}.jpg"
        p = env.base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"j" * size)
        env.db.add_resource(tid, "image", f"https://img/{i:05d}.jpg",
                            filename=rel, status="done", local_path=str(p))
        paths.append(p)
    return tid, paths


def test_delete_keeps_files_by_default(env):
    tid, paths = _task_with_files(env, "keep")
    mgr = env.manager()

    info = mgr.delete(tid)

    assert info == {"files": 0, "bytes": 0}
    assert env.db.get_task(tid) is None, "记录应被删除"
    assert all(p.exists() for p in paths), "默认必须保留磁盘文件"


def test_delete_with_files_removes_only_recorded_files(env):
    tid, paths = _task_with_files(env, "purge", n=3, size=100)
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info["files"] == 3
    assert info["bytes"] == 300
    assert not any(p.exists() for p in paths), "选了删文件就必须真的删掉"
    assert env.db.get_task(tid) is None


def test_delete_with_files_tolerates_missing_file(env):
    """库里有记录但文件已经不在了(用户手工删过)时不能报错, 也不能算进统计。"""
    tid = env.db.create_task("https://x/album", "generic", None, {})
    env.db.add_resource(tid, "image", "https://img/1.jpg",
                        filename="套图名/00001.jpg", status="done",
                        local_path=str(env.base / "套图名" / "00001.jpg"))
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info == {"files": 0, "bytes": 0}


def test_purge_skips_file_another_task_points_at(env):
    """去重: 两个任务指向同一份文件时, 删其中一个不能动它。

    内容 hash 重复时后来者直接复用前者的文件(见 task_manager._download_one),
    删掉它等于把另一个任务的结果一并毁掉。
    """
    a = env.db.create_task("https://x/a", "generic", None, {})
    b = env.db.create_task("https://x/b", "generic", None, {})
    p = env.base / "套图名" / "00001.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"z" * 50)
    for tid, url in ((a, "https://img/1.jpg"), (b, "https://img/1.jpg?sd=600")):
        env.db.add_resource(tid, "image", url, filename="套图名/00001.jpg",
                            status="done", local_path=str(p))
    mgr = env.manager()

    info = mgr.delete(a, with_files=True)

    assert info == {"files": 0, "bytes": 0}
    assert p.exists(), "别的任务还在引用这份文件, 不能删"


def test_purge_removes_meta_dir_only_for_this_task(env):
    """清单目录 `_meta/<任务ID>/` 整棵清掉, 别的任务那份一个字节都不能碰。"""
    tid = env.db.create_task("https://x/album", "generic", None, {})
    mine = env.base / "_meta" / str(tid)
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "manifest.json").write_bytes(b"{}")
    (mine / "album.json").write_bytes(b"{}")
    other = env.base / "_meta" / "9999"
    other.mkdir(parents=True, exist_ok=True)
    (other / "manifest.json").write_bytes(b"{}")
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info["files"] == 2
    assert not mine.exists()
    assert (other / "manifest.json").exists(), "别的任务的清单不能被牵连"


def test_delete_unknown_task_is_noop(env):
    mgr = env.manager()
    assert mgr.delete(999, with_files=True) == {"files": 0, "bytes": 0}


def test_purge_never_touches_download_root_itself(env):
    """越界保护: 下载根目录里"没被记录过的东西"一个都不能动。

    ⚠️ 这正是改版后风险变大的地方 —— 用户可能把下载根设成 `D:/图片`,
    里面本来就有他自己的文件。删除只认库里的记录, 别的一律不碰。
    """
    env.base.mkdir(parents=True, exist_ok=True)
    bystander = env.base / "keep-me.txt"
    bystander.write_bytes(b"important")
    tid = env.db.create_task("https://x/album", "generic", None, {})
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info == {"files": 0, "bytes": 0}
    assert bystander.exists(), "绝不能删到下载根目录下的其他内容"


def test_purge_refuses_path_outside_download_root(env, tmp_path):
    """库里若有越界路径(手工改库/换了自定义目录), 那份文件绝不能被删。"""
    outsider = tmp_path / "外面" / "重要.jpg"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.write_bytes(b"x" * 12)
    tid = env.db.create_task("https://x/album", "generic", None, {})
    env.db.add_resource(tid, "image", "https://img/1.jpg",
                        filename="../外面/重要.jpg", status="done",
                        local_path=str(outsider))
    mgr = env.manager()

    info = mgr.delete(tid, with_files=True)

    assert info["files"] == 0
    assert outsider.exists(), "下载根之外的文件一个都不能删"


def test_purge_respects_custom_download_dir(env, tmp_path):
    """自定义下载目录下同样只删该任务记录过的文件。"""
    custom = tmp_path / "user-picked"
    custom.mkdir(parents=True, exist_ok=True)
    outsider = custom / "别删我.txt"
    outsider.write_bytes(b"x")
    tid = env.db.create_task("https://x/album", "generic", str(custom), {})
    inside = custom / "套图名" / "a.jpg"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"y" * 10)
    env.db.add_resource(tid, "image", "https://img/a.jpg",
                        filename="套图名/a.jpg", status="done",
                        local_path=str(inside))
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
