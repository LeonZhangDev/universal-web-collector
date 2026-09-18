from core.task_manager import TaskStatus, can_transition


def test_happy_path():
    assert can_transition("pending", "running")
    assert can_transition("running", "extracting")
    assert can_transition("extracting", "downloading")
    assert can_transition("downloading", "success")


def test_retry_from_failed():
    assert can_transition("failed", "pending")


def test_invalid_transitions():
    assert not can_transition("pending", "success")
    assert not can_transition("success", "pending")
    assert not can_transition("running", "success")
    assert not can_transition("failed", "running")
    assert not can_transition("downloading", "extracting")


def test_task_status_constants():
    assert TaskStatus.PENDING == "pending"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.CANCELLED == "cancelled"


def test_cancel_and_retry_cancelled():
    assert can_transition("pending", "cancelled")
    assert can_transition("running", "cancelled")
    assert can_transition("extracting", "cancelled")
    assert can_transition("downloading", "cancelled")
    assert can_transition("cancelled", "pending")  # 已取消可重跑
    assert not can_transition("cancelled", "failed")
    assert not can_transition("success", "cancelled")


def test_partial_status_transitions():
    """部分失败: 有资源成功也有资源失败, 既不是 success 也不该是 failed。"""
    assert TaskStatus.PARTIAL == "partial"
    assert can_transition("downloading", "partial")
    assert can_transition("partial", "pending")  # 可重跑
    assert not can_transition("success", "partial")
    assert not can_transition("partial", "success")


def test_final_status_follows_resource_outcome(monkeypatch):
    """任务终态必须反映资源实际结果。

    旧行为是不看资源结果一律 success, 于是"56 张全部下载失败"也显示成功。
    """
    from core.task_manager import TaskManager

    mgr = TaskManager.__new__(TaskManager)  # 不启动线程池/看门狗
    cases = [
        ({"done": 5}, "success"),
        ({"done": 3, "failed": 2}, "partial"),
        ({"done": 0, "failed": 4}, "failed"),
        ({"filtered": 3}, "success"),          # 过滤是用户规则, 不算失败
        ({"done": 1, "skipped": 2}, "success"),  # 跳过是能力缺失, 也不算失败
        ({}, "success"),
    ]
    for stat, expect in cases:
        monkeypatch.setattr(
            TaskManager, "_resource_stat", staticmethod(lambda tid, s=stat: s)
        )
        assert mgr._final_status(1) == expect, stat
