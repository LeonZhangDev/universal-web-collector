"""暂停/继续 + 任务名 + 有效资源定义 的回归测试。

暂停/继续的设计取舍: 线程池模型下"真·暂停"成本高, 所以用
"置取消标志让 worker 在资源边界退出 + 状态落 paused" 近似。
resume 只把被打断(downloading/skipped)与之前失败的资源复位成 pending,
不复用采集、不重新下载已 done 的文件 —— 这是它相对 cancel+retry 的核心价值。
"""

import time

import pytest

from core import database as db
from core import task_manager as tm
from core.filters import Filters


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把数据库指到一个临时文件, 与默认库隔离。"""
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_conn", None)
    yield
    monkeypatch.setattr(db, "_conn", None)


def test_pause_marks_paused_and_resume_continues(tmp_db, tmp_path, monkeypatch):
    """暂停 -> paused; resume -> 把 interrupted 资源复位并从断点补完。"""
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")

    # 离线桩: 不真下载, 直接把 pending 资源判 done。避免测试触网。
    def fake_download_all(self, task_id, referer):
        for r in db.get_resources(task_id):
            if r["status"] == "pending":
                db.update_resource(r["id"], status="done", local_path="x", size=1)
        db.update_task(task_id, progress=100)

    monkeypatch.setattr(tm.TaskManager, "_download_all", fake_download_all)

    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        tid = db.create_task("https://fake/album", "fake", None, {})
        # 模拟任务正在下载
        db.update_task_status(tid, tm.TaskStatus.DOWNLOADING)
        db.add_resource(
            tid, "image", "https://x/1.jpg", "{}", status="pending", filename="a/1.jpg"
        )

        ok, err = mgr.pause(tid)
        assert ok, err
        assert db.get_task(tid)["status"] == tm.TaskStatus.PAUSED

        # 暂停瞬间有资源正处于 downloading(被中断), 模拟这一态
        db.update_resource(1, status="downloading")

        ok, err = mgr.resume(tid)
        assert ok, err
        # resume 必须先把 downloading 复位成 pending 才能续跑
        assert db.get_resources(tid)[0]["status"] == "pending"

        deadline = time.time() + 15
        while db.get_task(tid)["status"] in tm.ACTIVE_STATES:
            if time.time() > deadline:
                break
            time.sleep(0.05)
        assert db.get_task(tid)["status"] == tm.TaskStatus.SUCCESS
        assert db.get_resources(tid)[0]["status"] == "done"
    finally:
        mgr.shutdown(wait=True)


def test_cannot_pause_terminal_task(tmp_db):
    tid = db.create_task("https://fake", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.SUCCESS)
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        ok, err = mgr.pause(tid)
        assert ok is False
        assert "paused" not in (err or "")
    finally:
        mgr.shutdown(wait=True)


def test_resume_rejects_non_paused(tmp_db):
    tid = db.create_task("https://fake", "fake", None, {})
    db.update_task_status(tid, tm.TaskStatus.SUCCESS)
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        ok, err = mgr.resume(tid)
        assert ok is False
        assert "paused" in (err or "")
    finally:
        mgr.shutdown(wait=True)


def test_infer_name_from_album_directory():
    """图集资源形如 '约啪.../00001.jpg' -> 取首段目录名; 视频 'base.mp4' -> 去扩展名。"""
    assert tm.TaskManager._infer_name([{"filename": "约啪172cm车模/00001.jpg"}]) == "约啪172cm车模"
    assert tm.TaskManager._infer_name([{"filename": "some_video.mp4"}]) == "some_video"
    assert tm.TaskManager._infer_name([]) is None


def test_valid_resource_rejects_tiny_advert_pixel():
    """有效资源定义: 开启 min_image_bytes 后, 1x1 跟踪像素/广告占位图被挡。"""
    f = Filters({"min_image_bytes": "1KB"})
    reason = f.match_resource("image", "https://x/track.gif", size=150)
    assert reason and "广告" in reason
    # 真图放行
    assert f.match_resource("image", "https://x/1.jpg", size=500 * 1024) is None
    # 视频不受图片下限约束
    assert f.match_resource("video", "https://x/1.mp4", size=150) is None


def test_match_resource_coerces_string_size():
    """size 经 JSON/DB 往返可能是字符串, 不能因此抛 TypeError(否则整任务崩在提取阶段)。"""
    f = Filters({"max_size": "1MB"})
    # 字符串 size 也能正确判出"太大"
    reason = f.match_resource("image", "https://x/1.jpg", size=str(999 * 1024 * 1024))
    assert reason and "太大" in reason
    # 字符串小体积放行
    assert f.match_resource("image", "https://x/1.jpg", size="100") is None
