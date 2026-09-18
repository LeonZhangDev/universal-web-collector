import pytest

import core.database as db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


def test_task_lifecycle(tmp_db):
    tid = tmp_db.create_task("https://a.com/1", "xchina")
    assert tmp_db.get_task(tid)["status"] == "pending"

    assert tmp_db.transition_task(tid, "running", "pending")
    assert not tmp_db.transition_task(tid, "extracting", "pending")  # 乐观锁: 状态已变
    assert tmp_db.get_task(tid)["status"] == "running"


def test_resources_and_logs(tmp_db):
    tid = tmp_db.create_task("https://a.com/1")
    rid = tmp_db.add_resource(tid, "image", "https://a.com/p.jpg", {"referer": "https://a.com"})
    assert len(tmp_db.get_resources(tid)) == 1

    tmp_db.update_resource(rid, status="done", hash="abc", local_path="x.jpg")
    hit = tmp_db.find_by_hash("abc")
    assert hit and hit["local_path"] == "x.jpg"
    assert tmp_db.find_by_hash("nope") is None

    tmp_db.add_log(tid, "hello", "info")
    tmp_db.add_log(tid, "bad", "error")
    logs = tmp_db.get_logs(tid)
    assert [l["level"] for l in logs] == ["info", "error"]


def test_migrate_adds_hash_column(tmp_db):
    conn = tmp_db.get_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(resources)")}
    assert "hash" in cols
