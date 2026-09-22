"""新增的三个后端接口: 列表(搜索/筛选/分页)、批量创建、环境诊断。

跟着 test_collector_autoresolve 的隔离套路: 把 DB 指到临时目录、stub 掉
task_manager.submit(否则批量创建会真去抓站点), 用 FastAPI TestClient 直击接口。
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import api.tasks as T  # noqa: E402  (T 命名沿用既有约定)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v2.db")
    monkeypatch.setattr(db, "_conn", None)
    # 不让批量/单条创建真去跑采集, 只验证"建任务之前"的逻辑
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _make(app, n, prefix="https://example.com/item"):
    ids = []
    for i in range(n):
        r = app.post(
            "/tasks/create",
            json={"url": f"{prefix}/{i}", "collector": "generic"},
        ).json()
        ids.append(r["task_id"])
    return ids


def test_list_paginates(client):
    app, _ = client
    _make(app, 25)
    # 默认每页大小, total 应等于全部, 首页条数应 <= page_size
    r = app.get("/tasks", params={"page": 1, "page_size": 10}).json()
    assert r["total"] == 25
    assert len(r["items"]) == 10
    assert r["pages"] == 3
    assert r["page"] == 1
    # 第二页从 11 条起
    r2 = app.get("/tasks", params={"page": 2, "page_size": 10}).json()
    assert len(r2["items"]) == 10
    assert r2["page"] == 2
    # 末页只剩 5 条
    r3 = app.get("/tasks", params={"page": 3, "page_size": 10}).json()
    assert len(r3["items"]) == 5


def test_list_search_matches_url_and_name(client):
    app, _ = client
    _make(app, 5, prefix="https://seed-a.com/x")
    _make(app, 5, prefix="https://seed-b.com/x")
    r = app.get("/tasks", params={"q": "seed-b"}).json()
    assert r["total"] == 5
    assert all("seed-b" in t["url"] for t in r["items"])


def test_list_status_filter_group(client):
    app, _ = client
    _make(app, 3)
    # 新建任务状态为 pending(属于 "active" 组)
    r = app.get("/tasks", params={"status": "active"}).json()
    assert r["total"] == 3
    # 找一个不存在的状态组应返回 0
    r2 = app.get("/tasks", params={"status": "success"}).json()
    assert r2["total"] == 0


def test_batch_create_counts_created_and_rejected(client):
    app, _ = client
    urls = [
        "https://x.com/1",   # ok
        "",                   # empty
        "has space here",     # invalid(含空格)
        "https://x.com/1",   # duplicate_in_batch
        "https://x.com/2",   # ok
    ]
    r = app.post(
        "/tasks/batch-create",
        json={"urls": urls, "collector": "generic"},
    ).json()
    assert r["created_count"] == 2
    assert r["rejected_count"] == 3
    reasons = {it["line"]: it["reason"] for it in r["items"]}
    assert reasons[2] == "empty"
    assert reasons[3] == "invalid"
    assert reasons[4] == "duplicate_in_batch"
    assert r["truncated_count"] == 0


def test_batch_create_detects_existing_duplicate(client):
    app, _ = client
    # 先建一个任务
    app.post("/tasks/create", json={"url": "https://dup.com/a", "collector": "generic"})
    # 再批量提交, 默认不允许重复 -> 标 duplicate_existing
    r = app.post(
        "/tasks/batch-create",
        json={"urls": ["https://dup.com/a", "https://dup.com/b"], "collector": "generic"},
    ).json()
    dup = [it for it in r["items"] if it["reason"] == "duplicate_existing"]
    assert len(dup) == 1
    assert dup[0]["existing_task_id"] is not None
    # 打开 allow_duplicates 后, 同 URL 也能建
    r2 = app.post(
        "/tasks/batch-create",
        json={
            "urls": ["https://dup.com/a"],
            "collector": "generic",
            "allow_duplicates": True,
        },
    ).json()
    assert r2["created_count"] == 1


def test_env_diagnose_shape(client):
    app, _ = client
    r = app.get("/env/diagnose").json()
    assert "items" in r and isinstance(r["items"], list)
    assert r["items"]  # 至少包含 Python / 浏览器 / ffmpeg / 磁盘 / 下载目录
    keys = {it["key"] for it in r["items"]}
    for need in ("python", "browser", "ffmpeg", "disk", "download_dir"):
        assert need in keys, f"诊断缺 {need}"
    for it in r["items"]:
        assert it["level"] in ("ok", "warn", "fail")
        assert "hint" in it  # 每项都必须能给"下一步怎么做"
    assert "all_ok" in r and "usable" in r and "checked_at" in r
