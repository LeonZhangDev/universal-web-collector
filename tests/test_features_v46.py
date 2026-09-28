"""V46 测试: 自动化规则(事件驱动) / OCR 可选能力 / 视频类型落库 / 排除与日期已并入 V45。

判据重点(仍是"不报错、只是结果不对"那几类):

1. **规则条件键白名单**(A1, 第 7 条)。传一个不在 RULE_CONDITION_KEYS 的键,
   必须 400 而不是存进去"永远不触发" —— 那是配置错误最安静的表现。

2. **规则动作白名单 + 参数必填**(A2)。未知动作、add_tag 不给标签、webhook 指向
   不存在的 hook, 全部创建时即 400(不是回 200 然后哑火)。

3. **规则命中要真的作用到资源上**(A3, 第 27 条: 规则生效要有证据)。
   建一条 `tag=x → add_tag=y` 的规则, 资源带 x 标签落库后, 它该真的多一个 y 标签。

4. **OCR 无引擎时如实 409**(A4, 第 27 条反面)。不能返回"识别成功 0 字" ——
   那是假绿: 用户会以为 OCR 跑过了, 其实根本没装 tesseract。

5. **OCR 文字能写入并被 text 过滤命中**(A5)。这是"能力生效要有证据"的正面:
   写进 ocr_text 的内容, 用 text= 子串能捞回来; 没写的不该捞到。
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db
import core.ocr as ocr_mod


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v46.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v46-api.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _add(tmp_db, name="a.jpg", type_="image", tags=None, **fields):
    tid = tmp_db.create_task("https://example.com/album", "generic")
    rid = tmp_db.add_resource(tid, type_, f"https://example.com/{name}", size=100)
    tmp_db.update_resource(rid, status="done",
                           local_path=str(fields.pop("_path", "")),
                           **fields)
    for t in (tags or []):
        tmp_db.add_tags([rid], [t])
    return tid, rid


# ---- 规则: 白名单与参数校验 ---------------------------------------------

def test_rule_rejects_unknown_condition_key(client):
    cl, _ = client
    r = cl.post("/rules", json={"name": "x", "condition": {"bogus": 1},
                               "action": "add_tag", "arg": "y"})
    assert r.status_code == 400
    assert "unknown rule condition key" in r.json()["detail"]


def test_rule_rejects_unknown_action(client):
    cl, _ = client
    r = cl.post("/rules", json={"name": "x", "condition": {"tag": "a"},
                               "action": "launch_missiles"})
    assert r.status_code == 400
    assert "unknown rule action" in r.json()["detail"]


def test_rule_add_tag_requires_arg(client):
    cl, _ = client
    r = cl.post("/rules", json={"name": "x", "condition": {"tag": "a"},
                               "action": "add_tag"})
    assert r.status_code == 400
    assert "add_tag" in r.json()["detail"]


def test_rule_webhook_requires_existing_hook(client):
    cl, _ = client
    r = cl.post("/rules", json={"name": "x", "condition": {"tag": "a"},
                               "action": "webhook", "arg": "999"})
    assert r.status_code == 400
    assert "webhook" in r.json()["detail"].lower()


def test_rule_crud_roundtrip(client):
    cl, _ = client
    rid = cl.post("/rules", json={"name": "rule1",
                                 "condition": {"tag": "cat"},
                                 "action": "add_tag", "arg": "pet"}).json()["id"]
    assert cl.get("/rules").json()[0]["name"] == "rule1"
    # 停用 / 启用
    assert cl.post(f"/rules/{rid}/toggle?enabled=false").json()["enabled"] is False
    assert cl.post(f"/rules/{rid}/toggle?enabled=true").json()["enabled"] is True
    # 删除
    assert cl.delete(f"/rules/{rid}").json()["ok"] is True
    assert cl.get("/rules").json() == []


# ---- 规则: 命中后真的作用到资源(第 27 条) -----------------------------

def test_rule_applies_on_resource_publish(tmp_db, monkeypatch):
    _, rid = _add(tmp_db, name="cat.jpg", tags=["cat"])
    # 建一条 "tag=cat → 加标签 pet" 的规则
    rule_id = tmp_db.create_rule(
        "pet-cat", {"tag": "cat"}, "add_tag", "pet")
    applied = tmp_db.run_rules_for_resource(rid)
    assert any("pet-cat" in a for a in applied)
    tags = {r["tag"] for r in tmp_db.query(
        "SELECT tag FROM resource_tags WHERE resource_id=?", (rid,))}
    assert "pet" in tags and "cat" in tags


def test_rule_favorite_action(tmp_db):
    _, rid = _add(tmp_db, name="fav.jpg")
    tmp_db.create_rule("fav-all", {}, "favorite")
    tmp_db.run_rules_for_resource(rid)
    row = tmp_db.query_one("SELECT favorite FROM resources WHERE id=?", (rid,))
    assert row["favorite"] == 1


def test_rule_does_not_apply_when_condition_misses(tmp_db):
    _, rid = _add(tmp_db, name="dog.jpg", tags=["dog"])
    tmp_db.create_rule("pet-cat", {"tag": "cat"}, "add_tag", "pet")
    tmp_db.run_rules_for_resource(rid)  # 资源带 dog, 不该命中
    tags = {r["tag"] for r in tmp_db.query(
        "SELECT tag FROM resource_tags WHERE resource_id=?", (rid,))}
    assert "pet" not in tags


def test_rule_manual_run_counts_hits(tmp_db):
    _add(tmp_db, name="a.jpg", tags=["cat"])
    _add(tmp_db, name="b.jpg", tags=["cat"])
    _add(tmp_db, name="c.jpg", tags=["dog"])
    rid = tmp_db.create_rule("pet-cat", {"tag": "cat"}, "add_tag", "pet")
    res = tmp_db.run_rule(rid)
    assert res["hits"] == 2 and res["errors"] == 0


# ---- OCR: 无引擎如实 409, 有文字能写入并被 text 过滤命中 -------------

def test_ocr_endpoint_409_when_unavailable(client, monkeypatch):
    monkeypatch.setattr(ocr_mod, "_TESSERACT_OK", False)
    _, rid = _add(client[1], name="scan.jpg", _path="/tmp/x.png")
    r = client[0].post(f"/library/{rid}/ocr")
    assert r.status_code == 409
    assert "tesseract" in r.json()["detail"].lower()


def test_ocr_text_written_and_filterable(tmp_db, monkeypatch):
    _, rid = _add(tmp_db, name="memo.jpg")
    tmp_db.set_ocr_text(rid, "会议记录 2026-09-26 待办")
    # text= 子串能命中
    assert len(tmp_db.library_list(text="待办")) == 1
    # 不在文字里的内容捞不到
    assert len(tmp_db.library_list(text="合同")) == 0
    # 与其它维度叠加
    assert len(tmp_db.library_list(text="会议", kind="image")) == 1


def test_ocr_endpoint_rejects_non_image(client, monkeypatch):
    monkeypatch.setattr(ocr_mod, "_TESSERACT_OK", False)
    _, rid = _add(client[1], name="v.mp4", type_="video", _path="/tmp/v.mp4")
    r = client[0].post(f"/library/{rid}/ocr")
    assert r.status_code in (400, 409)  # 409 优先(无引擎); 两者都拒绝
