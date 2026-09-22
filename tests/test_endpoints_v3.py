"""V30 新增接口与能力: 批量操作、失败资源重试、统计面板、任务级代理。

沿用 test_endpoints_v2 的隔离套路: DB 指临时目录、stub 掉 task_manager.submit
(避免真实采集), 这里额外 stub submit_resource(只验证"哪些资源被挑出来重试")。
"""
import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import api.tasks as T  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v3.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    # 记录被重试的资源, 验证 retry-failed 是否正确挑出 failed/skipped
    calls = []

    def fake_submit_resource(tid, rid):
        calls.append((tid, rid))
        return True, None

    monkeypatch.setattr(T.task_manager, "submit_resource", fake_submit_resource)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db, calls


def _make(app, n, prefix="https://example.com/item"):
    ids = []
    for i in range(n):
        r = app.post(
            "/tasks/create",
            json={"url": f"{prefix}/{i}", "collector": "generic"},
        ).json()
        ids.append(r["task_id"])
    return ids


def test_bulk_action_pause_and_not_found(client):
    app, _, _ = client
    ids = _make(app, 3)
    # pending 属于 active, pause 应当成功
    r = app.post("/tasks/bulk-action", json={"action": "pause", "task_ids": ids}).json()
    assert sorted(r["ok"]) == sorted(ids)
    assert r["skipped"] == []
    assert r["not_found"] == []
    # 已经 paused 的任务再 pause 应被 skipped(409)
    r2 = app.post("/tasks/bulk-action", json={"action": "pause", "task_ids": ids}).json()
    assert sorted(r2["skipped"]) == sorted(ids)
    # 不存在的 id 进 not_found
    r3 = app.post(
        "/tasks/bulk-action", json={"action": "pause", "task_ids": [9999]}
    ).json()
    assert r3["not_found"] == [9999]


def test_bulk_action_invalid_action(client):
    app, _, _ = client
    ids = _make(app, 1)
    r = app.post(
        "/tasks/bulk-action", json={"action": "explode", "task_ids": ids}
    )
    assert r.status_code == 400


def test_bulk_action_one_failure_does_not_block_others(client):
    app, _, _ = client
    ids = _make(app, 2)
    # 一个存在、一个不存在, 存在的应进 ok, 不存在的进 not_found
    r = app.post(
        "/tasks/bulk-action",
        json={"action": "cancel", "task_ids": [ids[0], 9999]},
    ).json()
    assert r["ok"] == [ids[0]]
    assert r["not_found"] == [9999]


def test_retry_failed_picks_only_failed_and_skipped(client):
    app, database, calls = client
    tid = _make(app, 1)[0]
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="done")
    database.add_resource(tid, "image", "https://x.com/b.jpg", status="failed")
    database.add_resource(tid, "image", "https://x.com/c.jpg", status="skipped")
    database.add_resource(tid, "image", "https://x.com/d.jpg", status="pending")
    # filtered 不应被重试(那是用户规则主动排除的资源)
    database.add_resource(tid, "image", "https://x.com/e.jpg", status="filtered")
    r = app.post(f"/tasks/{tid}/retry-failed").json()
    assert r["count"] == 2  # 仅 failed + skipped
    assert len(calls) == 2
    assert all(c[0] == tid for c in calls)


def test_stats_aggregates(client):
    app, _, _ = client
    _make(app, 4)
    r = app.get("/tasks/stats").json()
    assert r["total_tasks"] == 4
    assert r["by_status"].get("pending") == 4
    assert r["active"] == 4
    assert r["by_collector"].get("generic") == 4
    # 资源总量 / 体积在无资源时为零, 不应报错
    assert r["total_resources"] == 0
    assert r["total_bytes"] == 0


def test_stats_reflects_resources_and_bytes(client):
    app, database, _ = client
    tid = _make(app, 1)[0]
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="done", size=2048)
    database.add_resource(tid, "image", "https://x.com/b.jpg", status="failed", size=1024)
    r = app.get("/tasks/stats").json()
    assert r["total_resources"] == 2
    assert r["total_bytes"] == 2048  # 仅 done 计入体积
    assert r["by_status"].get("pending") == 1  # 任务本身仍是 pending(未提交)


def test_create_stores_proxy_in_options(client):
    app, database, _ = client
    r = app.post(
        "/tasks/create",
        json={
            "url": "https://example.com/p",
            "collector": "generic",
            "proxy": "http://127.0.0.1:7890",
        },
    ).json()
    tid = r["task_id"]
    task = dict(database.get_task(tid))
    opts = json.loads(task["options"])
    assert opts.get("proxy") == "http://127.0.0.1:7890"


def test_batch_create_stores_proxy_in_options(client):
    app, database, _ = client
    r = app.post(
        "/tasks/batch-create",
        json={
            "urls": ["https://example.com/b1"],
            "collector": "generic",
            "proxy": "socks5://127.0.0.1:1080",
        },
    ).json()
    assert r["created_count"] == 1
    tid = r["items"][0]["task_id"]
    opts = json.loads(database.get_task(tid)["options"])
    assert opts.get("proxy") == "socks5://127.0.0.1:1080"
