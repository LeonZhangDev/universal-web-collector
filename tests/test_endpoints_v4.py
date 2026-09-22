"""V31 新增能力: 代理池轮换、任务通知、统计时间序列/失败聚合/去重报表、采集器筛选。

沿用 v2/v3 的隔离套路: DB 指临时目录、stub 掉 task_manager.submit。
代理池与通知这两块没有 HTTP 入口的部分直接对模块做单元测试。
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import core.task_manager as tm
import api.tasks as T  # noqa: E402
from downloaders.base import ProxyPool, make_proxy_session


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v31.db")
    monkeypatch.setattr(db, "_conn", None)
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


# ---- 代理池 ----

def test_proxy_pool_empty_by_default():
    pool = ProxyPool(None)
    assert pool.empty
    assert pool.pick(0) is None
    assert ProxyPool("").empty
    assert ProxyPool("   ").empty


def test_proxy_pool_round_robin():
    pool = ProxyPool("http://a:1, http://b:2 ,https://c:3")
    # 逗号分隔 + 去空白
    assert pool.proxies == ["http://a:1", "http://b:2", "https://c:3"]
    # 按资源序号取模轮换: 第 0/3 个用 a, 第 1 个用 b, 第 2 个用 c
    assert pool.pick(0) == "http://a:1"
    assert pool.pick(1) == "http://b:2"
    assert pool.pick(2) == "https://c:3"
    assert pool.pick(3) == "http://a:1"


def test_proxy_pool_apply_sets_and_clears():
    sess = make_proxy_session(None)
    ProxyPool("http://a:1").apply(sess, 0)
    assert sess.proxies.get("http") == "http://a:1"
    # 空池 apply 应清空代理(而不是留下上一个任务的)
    ProxyPool("").apply(sess, 0)
    assert not sess.proxies


def test_make_proxy_session_isolated_from_global():
    s1 = make_proxy_session("http://a:1")
    s2 = make_proxy_session("http://b:2")
    assert s1 is not s2  # 每个任务独占, 不共享连接池
    assert s1.proxies.get("http") == "http://a:1"
    assert s2.proxies.get("http") == "http://b:2"


# ---- 通知 ----

def test_notification_created_on_settle(client):
    app, database = client
    tid = _make(app, 1)[0]
    # _settle_status 的钩子会走 _notification_for + db.add_notification
    tm._notification_for(tid, "success")  # 形状自检: 终态必须给出 level/title
    level, title, body = tm._notification_for(tid, "success")
    assert level == "success"
    assert title
    database.add_notification(tid, level, title, body)
    items = database.list_notifications(limit=10)
    assert len(items) == 1
    assert items[0]["level"] == "success"
    assert items[0]["task_id"] == tid
    assert database.unread_notification_count() == 1


def test_notification_for_cancelled_is_silent(client):
    app, _ = client
    tid = _make(app, 1)[0]
    # 用户主动取消不该推通知(噪声)
    assert tm._notification_for(tid, "cancelled") == (None, None, None)


def test_notifications_endpoint_and_mark_read(client):
    app, database = client
    tid = _make(app, 1)[0]
    database.add_notification(tid, "error", "任务失败", "403 forbidden")
    r = app.get("/notifications").json()
    assert r["unread"] == 1
    assert len(r["items"]) == 1
    assert r["items"][0]["read"] is False
    # 标记全部已读
    app.post("/notifications/read", json={"ids": None})
    r2 = app.get("/notifications").json()
    assert r2["unread"] == 0
    assert r2["items"][0]["read"] is True


def test_notification_levels_preserved(client):
    app, database = client
    tid = _make(app, 1)[0]
    for lvl in ("success", "warning", "error"):
        database.add_notification(tid, lvl, f"t-{lvl}", "")
    levels = {n["level"] for n in database.list_notifications(limit=10)}
    assert levels == {"success", "warning", "error"}


# ---- 统计增强 ----

def test_stats_has_series_and_aggregates(client):
    app, database = client
    tid = _make(app, 1)[0]
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="done", size=100)
    database.add_resource(tid, "image", "https://x.com/b.jpg", status="failed", size=50)
    database.add_resource(tid, "video", "https://x.com/c.mp4", status="failed", size=50)
    r = app.get("/tasks/stats").json()
    # 新字段必须存在且形状正确(前端图表直接吃这些)
    assert isinstance(r["by_date"], dict)
    assert isinstance(r["download_series"], dict)
    assert isinstance(r["failure_reasons"], list)
    assert isinstance(r["duplicates"], dict)
    assert "marked" in r["duplicates"]
    assert "bytes_saved" in r["duplicates"]


def test_failure_reasons_aggregated(client):
    app, database = client
    tid = _make(app, 1)[0]
    # add_resource 不吃 note, 先建后补
    for i, (st, note) in enumerate(
        [("failed", "HTTP 403 forbidden"), ("failed", "HTTP 403 forbidden"),
         ("skipped", "timeout")]
    ):
        rid = database.add_resource(tid, "image", f"https://x.com/{i}.jpg", status=st)
        database.update_resource(rid, status=st, note=note)
    r = app.get("/tasks/stats").json()
    reasons = {f["reason"]: f["count"] for f in r["failure_reasons"]}
    assert sum(reasons.values()) == 3
    # 同一原因应被聚成一条而不是三条
    assert reasons.get("HTTP 403 forbidden") == 2


def test_duplicate_stats_counts_marked(client):
    app, database = client
    tid = _make(app, 1)[0]
    r1 = database.add_resource(tid, "image", "https://x.com/a.jpg", status="done", size=500)
    r2 = database.add_resource(tid, "image", "https://x.com/b.jpg", status="done", size=500)
    database.update_resource(r2, duplicate_of=r1)
    r = app.get("/tasks/stats").json()
    assert r["duplicates"]["marked"] >= 1
    assert r["duplicates"]["bytes_saved"] >= 500


# ---- 采集器筛选 ----

def test_collector_filter_narrows_list(client):
    app, database = client
    for i in range(3):
        app.post("/tasks/create", json={"url": f"https://a.com/{i}", "collector": "generic"})
    app.post(
        "/tasks/create",
        json={"url": "https://b.com/x", "collector": "xchina_video"},
    )
    r = app.get("/tasks", params={"collector": "xchina_video"}).json()
    assert r["total"] == 1
    assert r["items"][0]["collector"] == "xchina_video"
    r2 = app.get("/tasks", params={"collector": "generic"}).json()
    assert r2["total"] == 3


def test_collector_filter_ignores_auto(client):
    app, database = client
    _make(app, 2)
    # "auto" 是输入意图不是落库名, 拿它筛不应把 generic 的任务筛没
    r = app.get("/tasks", params={"collector": "auto"}).json()
    assert r["total"] == 2
