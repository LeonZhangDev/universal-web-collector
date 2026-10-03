"""V47 测试: 视频进度记忆(续播) 与「继续观看」集合。

判据重点(仍是"不报错、只是结果不对"那几类):

1. **非法秒数静默忽略**(A1)。set_watch_position 收到 NaN / Inf / 负数必须**不写**,
   而不是抛 500 或把 progress 写成怪值 —— 它是播放路径上的附件能力, 上报失败只该
   "没记下进度"(第 27 条反面: 不该假装记下了)。

2. **「继续观看」的判据是『看过但没看完』**(A2)。watch_position 在 (1, duration-2)
   之间才出现; 看完的(>= duration-2)、没看过的(NULL / 0)、非视频的, 都不该出现在集合里。
   这是"没算出"与"算出来是空"必须分清的典型(同族 J)。

3. **API 端到端**(A3)。POST /library/{id}/watch-position 真写入, GET /library/continue
   回来带 file_url, 前端才能直接喂灯箱续播。
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db

from api import tasks as T


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "wp.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "wp-api.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _add_video(tmp_db, duration=100, watch_position=None, local_path="/x/v.mp4"):
    tid = tmp_db.create_task("https://example.com/a", "generic")
    rid = tmp_db.add_resource(tid, "video", "https://example.com/v.mp4", size=1024)
    upd = {"status": "done", "local_path": local_path, "duration": duration}
    if watch_position is not None:
        upd["watch_position"] = watch_position
    tmp_db.update_resource(rid, **upd)
    return tid, rid


def test_set_watch_position_ignores_invalid(tmp_db):
    _, rid = _add_video(tmp_db, duration=100, watch_position=None)
    assert tmp_db.set_watch_position(rid, float("nan")) == 0
    assert tmp_db.set_watch_position(rid, float("inf")) == 0
    assert tmp_db.set_watch_position(rid, -3) == 0
    # 库里仍应为 NULL(没被写成怪值)
    row = tmp_db.query_one("SELECT watch_position FROM resources WHERE id=?", (rid,))
    assert row["watch_position"] is None


def test_set_watch_position_writes_valid(tmp_db):
    _, rid = _add_video(tmp_db, duration=100)
    assert tmp_db.set_watch_position(rid, 42.5) == 1
    row = tmp_db.query_one("SELECT watch_position FROM resources WHERE id=?", (rid,))
    assert abs(row["watch_position"] - 42.5) < 1e-6


def test_continue_watching_excludes_finished_and_unwatched(tmp_db):
    # 看完的: 看满 99 秒(duration=100, 差 1 秒, 不算没看完)
    _add_video(tmp_db, duration=100, watch_position=99)
    # 没看过: NULL
    _add_video(tmp_db, duration=100, watch_position=None)
    # 刚打开没开始: 0 秒
    _add_video(tmp_db, duration=100, watch_position=0)
    # 看过一半: 应出现
    _, mid = _add_video(tmp_db, duration=100, watch_position=40)
    # 非视频(图片)即便有 watch_position 也不算
    tid = tmp_db.create_task("https://example.com/i", "generic")
    iid = tmp_db.add_resource(tid, "image", "https://example.com/i.jpg", size=10)
    tmp_db.update_resource(iid, status="done", local_path="/x/i.jpg", watch_position=5)

    items = tmp_db.list_continue_watching(50)
    ids = {r["id"] for r in items}
    assert mid in ids
    assert len(items) == 1  # 其余都不该出现


def test_continue_watching_with_null_duration_still_included(tmp_db):
    # 本机没装 ffprobe 时 duration 是 NULL, 看了一秒以上仍应列入(判据对 NULL 放通)
    _, rid = _add_video(tmp_db, duration=None, watch_position=10)
    items = tmp_db.list_continue_watching(50)
    assert rid in {r["id"] for r in items}


def test_api_watch_position_and_continue(client):
    cl, tmp_db = client
    _, rid = _add_video(tmp_db, duration=100, local_path="/x/v.mp4")
    r = cl.post(f"/library/{rid}/watch-position", json={"seconds": 33})
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    r2 = cl.get("/library/continue")
    assert r2.status_code == 200
    items = r2.json()["items"]
    ids = {it["id"] for it in items}
    assert rid in ids
    # 返回原始行: local_path 与 watch_position 都在, 前端据此拼可播放 URL 并续播
    hit = next(it for it in items if it["id"] == rid)
    assert hit["local_path"]
    assert abs(hit["watch_position"] - 33) < 1e-6


def test_api_watch_position_unknown_resource(client):
    cl, _ = client
    r = cl.post("/library/999999/watch-position", json={"seconds": 5})
    assert r.status_code == 404
