import pytest

import core.database as db
from core.task_manager import TaskManager


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    manager = TaskManager(max_workers=1, download_workers=1)
    try:
        yield manager
    finally:
        manager.shutdown()


def test_watchdog_fails_orphan_task(env):
    # 模拟服务重启遗留: DB 中 running 任务无对应 worker
    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    env._watchdog_pass()
    assert db.get_task(tid)["status"] == "failed"
    assert "stale" in db.get_task(tid)["error"]


def test_watchdog_cancels_stalled_task(env):
    import threading
    import time

    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    # 注入心跳极其陈旧的 worker 条目(不用真实线程, 避免竞态)
    entry = {"future": None, "cancel": threading.Event(),
             "hb": time.monotonic() - 10**9}
    with env._active_lock:
        env._active[tid] = entry
    env._watchdog_pass()
    assert db.get_task(tid)["status"] == "cancelled"
    assert entry["cancel"].is_set()


def test_watchdog_ignores_healthy_task(env):
    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    entry = {"future": None, "cancel": __import__("threading").Event(),
             "hb": __import__("time").monotonic()}
    with env._active_lock:
        env._active[tid] = entry
    env._watchdog_pass()
    assert db.get_task(tid)["status"] == "running"


def test_cancel_task(env):
    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    ok, err = env.cancel(tid)
    assert ok, err
    assert db.get_task(tid)["status"] == "cancelled"


def test_cancel_finished_task_rejected(env):
    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    db.transition_task(tid, "extracting", "running")
    db.transition_task(tid, "downloading", "extracting")
    db.transition_task(tid, "success", "downloading")
    ok, err = env.cancel(tid)
    assert not ok


def test_delete_task_cascades(env):
    tid = db.create_task("https://a.com/1")
    db.add_resource(tid, "image", "https://a.com/p.jpg")
    db.add_log(tid, "hello")
    env.delete(tid)
    assert db.get_task(tid) is None
    assert db.get_resources(tid) == []
    assert db.get_logs(tid) == []


class _RunningFuture:
    """模拟"已经开始执行"的 future: cancel() 必定返回 False。"""

    def cancel(self):
        return False


class _QueuedFuture:
    """模拟"还排在队列里没启动"的 future: cancel() 成功返回 True。"""

    def cancel(self):
        return True


def _inject(env, tid, future):
    import threading
    import time

    entry = {"future": future, "cancel": threading.Event(), "hb": time.monotonic()}
    with env._active_lock:
        env._active[tid] = entry
    return entry


def test_delete_keeps_cancel_flag_visible_to_running_worker(env):
    """删除运行中的任务时, 取消标志必须对仍在跑的 worker 保持可见。

    回归: delete() 曾无条件 pop _active 条目, 导致 _cancelled() 读不到
    取消标志, worker 会把剩余资源全部下完才退出 —— 长时间占住 worker
    槽位, 后续任务一直排队。
    """
    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    entry = _inject(env, tid, _RunningFuture())

    env.delete(tid)

    assert db.get_task(tid) is None  # 任务行已删除
    assert entry["cancel"].is_set()  # 取消标志已置
    assert env._cancelled(tid) is True  # worker 仍能读到 -> 会在资源边界退出


def test_cancel_reclaims_entry_when_worker_never_started(env):
    """worker 尚未启动就被取消时, _active 条目要兜底回收, 不能泄漏。"""
    tid = db.create_task("https://a.com/1")
    db.transition_task(tid, "running", "pending")
    _inject(env, tid, _QueuedFuture())

    env.cancel(tid)

    assert env._entry(tid) is None  # 不会被 _run 的 finally 清理, 必须在此回收
    assert db.get_task(tid)["status"] == "cancelled"
