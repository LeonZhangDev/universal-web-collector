"""V45 测试: 相似检索 / 排除筛选 / 日期区间 / Webhook 重试与历史 / cron 排期。

判据的重点仍在"不报错、只是结果不对"的那几类:

1. **"相似"与"重复"是两个阈值**(A2)。共用一个常量会两头不讨好: 调高则
   "疑似重复"一片红, 调低则"找相似"永远 0 条。用例钉住"距离 8"这一档 ——
   它**不该**被判成重复, 但**应该**出现在相似结果里。

2. **"没法比" ≠ "没有像的"**(A3, 第 26 条)。没指纹 / 纯色图 / 没这条资源
   都必须是 `ok=False` + 各自的 reason, 不许一律返回空列表。

3. **排除是 `NOT EXISTS` 而不是 `EXISTS(NOT ...)`**(B1)。
   后者在"这个资源还有别的标签"时会整体为真, 于是"排除 A"变成"只要有任意
   非 A 标签就留下" —— 等于没筛, 而且不报错。

4. **排除时 NULL 要算"不是"**(B2)。`NULL <> 'blue'` 在 SQL 里是 NULL 而非真,
   于是"还没提过色的图"会被一起排掉。

5. **日期非法要报错**(C2, 第 28 条)。"2026-9-1" 若被静默忽略, 表现是
   "选了日期没反应", 用户会以为那段时间没有东西。

6. **cron 的优先级只能有一处定义**(D1)。`claim_watch` 若只按间隔顺延, 一个配
   了 cron 的源跑完第一轮就退回固定间隔 —— **第一轮看起来是对的**。

7. **4xx 不重试**(E1)。404 再发三次还是三次 404, 唯一后果是让用户多等两秒。
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db
import core.webhooks as hooks
from core import cron as cronmod
from core import phash

SEED = "aaaaaaaaaaaaaaaa"


def _fp(base, n):
    """在 base 上恰好翻转 n 个最低位 —— 海明距离正好是 n。"""
    v = int(base, 16)
    for i in range(n):
        v ^= 1 << i
    return "%016x" % v


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v45.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v45-api.db")
    monkeypatch.setattr(db, "_conn", None)
    # 不起真 worker(第 14 条: 测试里"顺手调真实入口"会给下一个用例写心跳)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _add(tmp_db, name="a.jpg", type_="image", **fields):
    tid = tmp_db.create_task("https://example.com/album", "generic")
    rid = tmp_db.add_resource(tid, type_, f"https://example.com/{name}", size=100)
    tmp_db.update_resource(rid, status="done",
                           local_path=str(fields.pop("_path", "")) or "", **fields)
    return tid, rid


# ==========================================================================
# A. 相似检索
# ==========================================================================

def test_similar_returns_nearest_first(tmp_db):
    _, a = _add(tmp_db, "a.jpg", phash=SEED)
    _, b = _add(tmp_db, "b.jpg", phash=_fp(SEED, 2))
    _, c = _add(tmp_db, "c.jpg", phash=_fp(SEED, 9))

    got = tmp_db.similar_to(a)
    assert got["ok"] is True
    assert [i["id"] for i in got["items"]] == [b, c]
    assert [i["distance"] for i in got["items"]] == [2, 9]


def test_similar_threshold_is_not_the_duplicate_threshold(tmp_db):
    """距离 8: 判"重复"的阈值(4)不该认, 判"相似"的阈值(12)该认。"""
    _, a = _add(tmp_db, "a.jpg", phash=SEED)
    _, mid = _add(tmp_db, "mid.jpg", phash=_fp(SEED, 8))

    assert phash.is_duplicate(SEED, _fp(SEED, 8)) is False, "8 不该被判成重复"
    got = tmp_db.similar_to(a)
    assert mid in [i["id"] for i in got["items"]], "8 应该出现在相似结果里"
    # 两个常量必须真的不同 —— 被"顺手统一"了这条用例就会红
    assert db.SIMILAR_MAX_DISTANCE > phash.DEFAULT_THRESHOLD


def test_similar_says_why_when_it_cannot_compare(tmp_db):
    """三种"没法比"必须是三种输出, 不许一律空列表(第 26 条)。"""
    _, nohash = _add(tmp_db, "nohash.jpg")
    got = tmp_db.similar_to(nohash)
    assert got["ok"] is False and got["reason"] == "no_phash"

    _, flat = _add(tmp_db, "flat.jpg", phash="0000000000000000")
    assert tmp_db.similar_to(flat)["reason"] == "flat"

    assert tmp_db.similar_to(999999)["reason"] == "not_found"


def test_similar_reports_candidate_truncation(tmp_db, monkeypatch):
    """候选被截断要报出来: 默默只比前 N 条, "有更像的没比到"会变成"没有相似的"。"""
    _, a = _add(tmp_db, "a.jpg", phash=SEED)
    _add(tmp_db, "b.jpg", phash=_fp(SEED, 2))
    monkeypatch.setattr(db, "SIMILAR_SCAN_LIMIT", 1)
    got = tmp_db.similar_to(a)
    assert got["truncated"] is True
    assert got["scanned"] == 1


def test_similar_api_distinguishes_cannot_compare_from_empty(client):
    cl, tmp_db = client
    _, rid = _add(tmp_db, "a.jpg", phash=SEED)
    r = cl.get(f"/library/{rid}/similar")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["reason_label"] is None

    _, nohash = _add(tmp_db, "nohash.jpg")
    r2 = cl.get(f"/library/{nohash}/similar")
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is False and body["items"] == []
    # 文案由后端下发, 且必须是中文而不是代号(第 9 条)
    assert body["reason_label"] and body["reason_label"] != body["reason"]

    assert cl.get("/library/999999/similar").status_code == 404


# ==========================================================================
# B. 排除筛选
# ==========================================================================

def test_exclude_tag_uses_not_exists(tmp_db):
    """资源同时有 A 与 B 标签时, 排除 A 必须让它消失。

    写成 `EXISTS(... AND rt.tag <> 'A')` 的话, B 那一条满足 NOT, 整体为真 ——
    "排除 A" 就变成了"只要有任意非 A 标签就留下"。
    """
    tid, keep = _add(tmp_db, "keep.jpg")
    _, drop = _add(tmp_db, "drop.jpg")
    tmp_db.add_tags([keep], ["A"])
    tmp_db.add_tags([drop], ["A"])
    tmp_db.add_tags([drop], ["B"])

    assert db.library_count(exclude_tag="A") == 0
    assert db.library_count() == 2


def test_exclude_keeps_rows_with_null_value(tmp_db):
    """`NULL <> 'x'` 是 NULL 而不是真 —— 没提过色的不许被"排除色系"排掉。"""
    _, colorized = _add(tmp_db, "blue.jpg", dominant_color="blue")
    _, plain = _add(tmp_db, "plain.jpg")

    ids = {r["id"] for r in db.library_list(exclude_color="blue")}
    assert ids == {plain}, "没提过色的资源必须留下"
    assert db.library_count(exclude_color="blue") == 1


def test_exclude_album_keeps_unnamed_tasks(tmp_db):
    tid, rid = _add(tmp_db, "a.jpg")
    db.update_task(tid, name="目标相册")
    _, other = _add(tmp_db, "b.jpg")

    ids = {r["id"] for r in db.library_list(exclude_album="目标相册")}
    assert other in ids, "没名字的任务不该被排除掉"


def test_exclude_unknown_special_raises(tmp_db):
    with pytest.raises(ValueError):
        db.library_count(exclude_special="nope")


# ==========================================================================
# C. 日期区间 / 日期排序 / applied
# ==========================================================================

def test_date_range_is_inclusive_of_the_last_day(tmp_db):
    """上界必须补到当天最后一秒, 否则"选了今天"会一条都没有。"""
    _, rid = _add(tmp_db, "a.jpg")
    db.execute("UPDATE resources SET created_time=? WHERE id=?",
               ("2026-09-26 18:30:00", rid))

    assert db.library_count(date_from="2026-09-26", date_to="2026-09-26") == 1
    assert db.library_count(date_from="2026-09-27") == 0
    assert db.library_count(date_to="2026-09-25") == 0


def test_bad_date_is_rejected_not_ignored(tmp_db):
    """静默忽略的表现是"选了日期没反应"(第 28 条)。"""
    with pytest.raises(ValueError):
        db.library_count(date_from="2026-9-1")
    with pytest.raises(ValueError):
        db.library_count(date_to="not-a-date")


def test_date_sort_exists_but_unknown_sort_still_400(client):
    cl, tmp_db = client
    body = cl.get("/library", params={"sort": "date"}).json()
    assert body["sort"] == "date"
    assert "date" in {s["key"] for s in body["sorts"]}
    # 档位中文名必须写"落盘" —— 我们没有 EXIF, 写"拍摄时间"就是安静地错
    label = {s["key"]: s["label"] for s in body["sorts"]}["date"]
    assert "落盘" in label and "拍摄" not in label
    assert cl.get("/library", params={"sort": "nope"}).status_code == 400


def _applied_keys(body):
    """`applied` 是 [{key,label}], 取键名出来断言(界面用的是 label)。"""
    return [c["key"] for c in body["applied"]]


def test_applied_reports_only_real_filters(client):
    """`kind="all"` 表示不限, 不许被当成一条生效的条件(第 27 条)。"""
    cl, _ = client
    body = cl.get("/library", params={"kind": "all", "tag": "x"}).json()
    assert "tag" in _applied_keys(body)
    assert "kind" not in _applied_keys(body)
    # 每个生效条件都要带**中文名**: 界面不许自己维护一份映射(第 9 条)。
    # 断言"没有裸键名形状的项" —— 少了这层, 后端改成一串字符串界面也不会红。
    assert all(isinstance(c, dict) and c.get("label") for c in body["applied"])
    lab = {c["key"]: c["label"] for c in body["applied"]}
    assert lab["tag"] and lab["tag"] != "tag"


def test_every_applied_key_has_a_chinese_label(tmp_db):
    """applied 里出现的每个键都必须有中文名。

    ⚠️ 这条断言防的是**键名对不上**: `library_filters` 里 `applied.append("x")`
    与 `LIBRARY_FILTER_LABELS` 的键不一致时, 不报错、测试也照样绿, 只在界面上
    露出一枚写着英文键名的胶囊(第 9 条那一类静默失效)。
    """
    _, rid = _add(tmp_db, "a.jpg")
    db.add_tags([rid], ["A"])
    _where, _args, keys = db.library_filters(
        q="a", kind="image", exclude_kind="video", album="x", exclude_album="y",
        task_id=1, tag="A", exclude_tag="B", favorite=True, min_rating=1,
        special="untagged", exclude_special="unrated", color="red",
        exclude_color="blue", date_from="2026-01-01", date_to="2026-12-31",
    )
    assert keys, "至少要有一项生效, 否则这条断言等于没跑"
    missing = [k for k in keys if k not in db.LIBRARY_FILTER_LABELS]
    assert not missing, f"这些键没有中文名: {missing}"


def test_exclude_and_date_reach_both_count_and_list(client):
    """count 与 list 必须同口径, 否则"翻到最后一页数量对不上"。"""
    cl, tmp_db = client
    _, rid = _add(tmp_db, "a.jpg")
    db.execute("UPDATE resources SET created_time=? WHERE id=?",
               ("2026-09-26 10:00:00", rid))
    db.add_tags([rid], ["A"])

    p = {"exclude_tag": "A", "date_from": "2026-09-01"}
    body = cl.get("/library", params=p).json()
    assert body["total"] == 0
    assert body["items"] == []
    assert "exclude_tag" in _applied_keys(body) and "date_from" in _applied_keys(body)


# ==========================================================================
# D. Webhook 重试与投递历史
# ==========================================================================

@pytest.fixture
def _no_http(monkeypatch):
    """替身: 按脚本依次返回 (status, error), 并记下发了几次。"""
    calls = []

    def fake(url, body, secret, timeout):
        calls.append(url)
        script = getattr(fake, "script", [(200, None)])
        return script[min(len(calls) - 1, len(script) - 1)]

    monkeypatch.setattr(hooks, "_post", fake)
    return fake, calls


def test_retry_on_5xx_and_no_retry_on_4xx(tmp_db, _no_http):
    fake, calls = _no_http
    hid = db.add_webhook("https://hook.example/a", ["task.done"])

    fake.script = [(500, "HTTP 500"), (500, "HTTP 500"), (200, None)]
    got = hooks.deliver(hid, "https://hook.example/a", None, "task.done", {},
                        backoff=(0, 0, 0))
    assert got["attempts"] == 3 and got["status"] == 200
    assert len(db.list_webhook_deliveries(hid)) == 3

    db.execute("DELETE FROM webhook_deliveries")
    fake.script = [(404, "HTTP 404")]
    calls.clear()
    got2 = hooks.deliver(hid, "https://hook.example/a", None, "task.done", {},
                         backoff=(0, 0, 0))
    assert got2["attempts"] == 1, "4xx 不该重试: 再发还是 404, 只是让用户多等"
    assert got2["status"] == 404


def test_each_attempt_is_recorded_separately(tmp_db, _no_http):
    """合并成"最后一次"的话, "第一次超时第二次成了"就看不见了。"""
    fake, calls = _no_http
    hid = db.add_webhook("https://hook.example/b", ["task.done"])
    fake.script = [(None, "timed out"), (200, None)]

    hooks.deliver(hid, "https://hook.example/b", None, "task.done", {},
                  backoff=(0, 0))
    rows = db.list_webhook_deliveries(hid)
    assert [(r["attempt"], r["status"]) for r in reversed(rows)] == [(1, None), (2, 200)]


def test_network_failure_is_retried(tmp_db, _no_http):
    fake, _ = _no_http
    hid = db.add_webhook("https://hook.example/c", ["task.done"])
    fake.script = [(None, "connection refused")]
    got = hooks.deliver(hid, "https://hook.example/c", None, "task.done", {},
                        backoff=(0, 0, 0))
    assert got["attempts"] == hooks.MAX_ATTEMPTS
    # 失败必须留痕(第 G 条)
    assert db.get_webhook(hid)["last_status"] is None
    assert db.get_webhook(hid)["last_error"]


def test_deleting_webhook_removes_its_history(tmp_db, _no_http):
    fake, _ = _no_http
    fake.script = [(200, None)]
    hid = db.add_webhook("https://hook.example/d", ["task.done"])
    hooks.deliver(hid, "https://hook.example/d", None, "task.done", {}, backoff=(0,))
    assert len(db.list_webhook_deliveries(hid)) == 1
    db.delete_webhook(hid)
    assert db.list_webhook_deliveries(hid) == []


def test_deliveries_endpoint(client, _no_http):
    fake, _ = _no_http
    cl, tmp_db = client
    fake.script = [(200, None)]
    hid = tmp_db.add_webhook("https://hook.example/e", ["task.done"])
    hooks.deliver(hid, "https://hook.example/e", None, "task.done", {}, backoff=(0,))
    r = cl.get(f"/webhooks/{hid}/deliveries")
    assert r.status_code == 200
    assert r.json()["total"] == 1 and r.json()["items"][0]["attempt"] == 1
    assert cl.get("/webhooks/999999/deliveries").status_code == 404


# ==========================================================================
# E. cron 排期
# ==========================================================================

def test_cron_next_is_strictly_after_now():
    now = datetime(2026, 9, 26, 10, 0)
    got = cronmod.next_after("0 3 * * *", now=now)
    assert got == datetime(2026, 9, 27, 3, 0)

    # 同一分钟里不允许返回"现在": 否则调度器会把它当成到期、反复触发
    assert cronmod.next_after("* * * * *", now=now) == datetime(2026, 9, 26, 10, 1)


def test_cron_step_and_list():
    now = datetime(2026, 9, 26, 10, 0)
    assert cronmod.next_after("*/15 * * * *", now=now) == datetime(2026, 9, 26, 10, 15)
    assert cronmod.next_after("0 9,18 * * *", now=now) == datetime(2026, 9, 26, 18, 0)


def test_cron_dom_and_dow_is_or_not_and():
    """日与周同时限定 = 满足其一。取"且"的话这个表达式一年只跑一次。"""
    now = datetime(2026, 9, 26, 0, 0)      # 周六
    # 每月 1 号 **或** 每周一, 凌晨 3 点 -> 下一个触发是 9/28(周一)
    got = cronmod.next_after("0 3 1 * 1", now=now)
    assert got == datetime(2026, 9, 28, 3, 0)


def test_cron_sunday_zero_maps_to_python_weekday():
    """cron 的周日是 0, Python 的 weekday() 周日是 6; 差一天不报错, 只是每周跑错一天。"""
    now = datetime(2026, 9, 26, 0, 0)      # 周六
    got = cronmod.next_after("0 3 * * 0", now=now)
    assert got.weekday() == 6 and got.day == 27


def test_cron_bad_expression_is_rejected():
    for bad in ["", "* * * *", "a b c d e", "*/0 * * * *", "70 * * * *", "5-1 * * * *"]:
        assert cronmod.describe(bad), f"{bad!r} 应该被拒绝"


def test_watch_uses_cron_for_next_run(tmp_db):
    wid = tmp_db.create_watch("https://example.com/a", "generic",
                              interval_minutes=60, cron="0 3 * * *", run_now=False)
    row = tmp_db.get_watch(wid)
    assert row["cron"] == "0 3 * * *"
    assert row["next_run"].endswith("03:00:00"), row["next_run"]


def test_claim_watch_keeps_using_cron(tmp_db):
    """跑完第一轮若退回固定间隔, **第一轮看起来是对的**, 第二天才发现。"""
    wid = tmp_db.create_watch("https://example.com/a", "generic",
                              interval_minutes=60, cron="0 3 * * *", run_now=False)
    db.execute("UPDATE watches SET next_run='2020-01-01 00:00:00' WHERE id=?", (wid,))
    assert db.claim_watch(wid, 60, "0 3 * * *") is True
    assert db.get_watch(wid)["next_run"].endswith("03:00:00")


def test_interval_watch_is_unchanged(tmp_db):
    wid = tmp_db.create_watch("https://example.com/a", "generic",
                              interval_minutes=60, run_now=False)
    row = tmp_db.get_watch(wid)
    assert not row["cron"]
    assert row["next_run"] != "2020-01-01 00:00:00"


def test_watch_api_rejects_bad_cron(client):
    cl, _ = client
    r = cl.post("/watches", json={"url": "https://example.com/a", "cron": "* * *"})
    assert r.status_code == 400
    assert "cron" in r.json()["detail"]

    ok = cl.post("/watches", json={"url": "https://example.com/b", "cron": "0 3 * * *"})
    assert ok.status_code == 200 and ok.json()["cron"] == "0 3 * * *"


def test_next_run_for_prefers_cron(tmp_db):
    """优先级只有一处定义: 这里既是 create_watch 也是 claim_watch 走的那条路。"""
    now = datetime(2026, 9, 26, 10, 0)
    assert db.next_run_for(60, "0 3 * * *", now) == "2026-09-27 03:00:00"
    # ⚠️ `now` 也可能是 `_now()` 的字符串(调用方常原样传回来) —— 不能炸
    assert db.next_run_for(60, "0 3 * * *", "2026-09-26 10:00:00") == "2026-09-27 03:00:00"
