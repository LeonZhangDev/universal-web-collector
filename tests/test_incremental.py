"""增量续采与订阅巡检的数据层。

增量靠的是"同一个 URL 历史上下载过就算数", 订阅靠的是 claim 的幂等 ——
这两条如果错了, 表现分别是"每次重跑都全量重下"和"同一份资源被重复抢救",
都属于跑了很久才发现的问题。
"""
import pytest

import core.database as db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


def _done(tmp_db, tid, url, resolved=True):
    """把资源以"已完成"入库(模拟一次成功的下载)。

    resolved=True 表示这次真的发了请求 —— 下载层会回填 resolved_url;
    增量复用的记录没有它, 这正是 count_downloaded 区分"新增"的依据。
    """
    rid = tmp_db.add_resource(
        tid, "image", url, status="done",
        local_path=str(tid) + "/00001.jpg", hash_value="h" * 64,
    )
    if resolved:
        tmp_db.update_resource(
            rid, resolved_url=url, content_type="image/jpeg"
        )
    return rid


def test_find_done_resource_hits_across_tasks(tmp_db):
    a = tmp_db.create_task("https://a/1")
    _done(tmp_db, a, "https://img/x/00001.jpg")

    b = tmp_db.create_task("https://a/1")
    prior = tmp_db.find_done_resource("https://img/x/00001.jpg")
    assert prior is not None and prior["task_id"] == a
    assert tmp_db.find_done_resource("https://img/x/00002.jpg") is None


def test_failed_resource_does_not_block_retry(tmp_db):
    tid = tmp_db.create_task("https://a/1")
    tmp_db.add_resource(tid, "image", "https://img/x/1.jpg", status="failed")
    # 之前失败过 = 这次该重试, 不能因为历史上有记录就跳过
    assert tmp_db.find_done_resource("https://img/x/1.jpg") is None


def test_done_without_local_path_is_not_reusable(tmp_db):
    tid = tmp_db.create_task("https://a/1")
    tmp_db.update_resource(
        tmp_db.add_resource(tid, "image", "https://img/x/1.jpg"),
        status="done", hash="h",
    )    # 没有 local_path 说明文件不在了, 复用它会导致"显示成功但磁盘上没有"
    assert tmp_db.find_done_resource("https://img/x/1.jpg") is None


def test_count_downloaded_excludes_reused(tmp_db):
    tid = tmp_db.create_task("https://a/1")
    _done(tmp_db, tid, "https://img/x/1.jpg", resolved=True)   # 本次真下的
    _done(tmp_db, tid, "https://img/x/2.jpg", resolved=False)  # 增量复用的
    _done(tmp_db, tid, "https://img/x/3.jpg", resolved=True)
    tmp_db.add_resource(tid, "image", "https://img/x/4.jpg", status="failed")

    assert tmp_db.count_downloaded(tid) == 2
    assert tmp_db.count_resources(tid) == {"done": 3, "failed": 1}


# ---- 订阅巡检 ----

def test_create_watch_due_immediately_by_default(tmp_db):
    wid = tmp_db.create_watch("https://a/album", "xchina_gallery")
    w = tmp_db.get_watch(wid)
    assert w["enabled"] == 1 and w["interval_minutes"] == 360
    assert [x["id"] for x in tmp_db.due_watches()] == [wid]


def test_run_now_false_defers_first_round(tmp_db):
    tmp_db.create_watch("https://a/album", "xchina_gallery", interval_minutes=30,
                        run_now=False)
    assert tmp_db.due_watches() == []


def test_interval_is_clamped_to_at_least_one_minute(tmp_db):
    tmp_db.create_watch("https://a/album", "generic", interval_minutes=0)
    assert tmp_db.get_watch(1)["interval_minutes"] == 1


def test_claim_watch_is_idempotent(tmp_db):
    """抢占必须只成功一次 —— 否则手动触发与定时巡检会各建一个任务。"""
    wid = tmp_db.create_watch("https://a/album", "generic", interval_minutes=60)
    assert tmp_db.claim_watch(wid, 60) is True
    assert tmp_db.claim_watch(wid, 60) is False
    assert tmp_db.due_watches() == []


def test_watch_options_roundtrip(tmp_db):
    tmp_db.create_watch("https://a/album", "generic",
                        options={"quality": "1200", "download_dir": "D:/x"})
    w = tmp_db.get_watch(1)
    import json
    assert json.loads(w["options"])["quality"] == "1200"


def test_finish_watch_run_accumulates_hits(tmp_db):
    wid = tmp_db.create_watch("https://a/album", "generic")
    tmp_db.finish_watch_run(wid, 11, 3)
    tmp_db.finish_watch_run(wid, 12, 2)
    w = tmp_db.get_watch(wid)
    assert w["hits"] == 5
    assert w["last_task_id"] == 12


def test_disable_watch_stops_it_being_due(tmp_db):
    wid = tmp_db.create_watch("https://a/album", "generic")
    tmp_db.set_watch_enabled(wid, False)
    assert tmp_db.due_watches() == []
    tmp_db.set_watch_enabled(wid, True)
    assert len(tmp_db.due_watches()) == 1


def test_delete_watch(tmp_db):
    wid = tmp_db.create_watch("https://a/album", "generic")
    assert tmp_db.delete_watch(wid) == 1
    assert tmp_db.delete_watch(wid) == 0
    assert tmp_db.list_watches() == []
