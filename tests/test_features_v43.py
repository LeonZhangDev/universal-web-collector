"""V43 测试: 资源库的**排序**与**保存的搜索(智能文件夹)**。

这两项都是"从同类产品补上来的入口", 判据的重点不在"功能能用", 而在下面几条
静默失败上 —— 它们的共同点是**不报错, 只是结果不对**:

1. **排序代号必须只有一个翻译点**(第 7 条)。
   前端传的是 `size` 这样的代号, 后端拿它拼进 `ORDER BY`。若不做白名单,
   `sort=id; DROP TABLE resources` 也"跑得通"(而且没人会发现)。所以这里钉的是
   **未知代号必须抛错**, 以及**拼出来的片段里只能出现白名单里的列**。

2. **同值行的次序必须确定**(次级键)。
   同一秒落盘、同样大小、同为 5 星的行, 在 SQLite 里的相对次序由扫描顺序决定。
   翻页时同一行可能出现在两页、也可能一页都不出现 —— 这类错不报错, 只表现为
   "我明明看到过那一条"。用例用 `limit=1` 逐页取, 再和整表比。

3. **"数过了, 是 0" ≠ "没数出来"**(第 26 条)。
   保存的搜索一律带 `count`; 条件读坏了给 `None` 并置 `broken`, **不是**给 0。
   0 是"这个搜索现在匹配不到东西", `None` 是"这个搜索我算不动"。

4. **坏条件要在保存时就被拒绝**(而不是列表时才炸)。
   一个存进去的非法 `special` 会让**每次列出搜索**都抛异常 —— 整个侧栏打不开。
   保存时拒绝是唯一能保证"存下来的都跑得动"的位置。

5. **应用一个保存的搜索 = 整体替换条件**(不是合并)。
   侧栏上的命中数是按**存下来的那组条件**算的; 若带着上一轮的残留条件去查,
   列表条数会与那个数字对不上, 而这不报错, 只表现为"这个搜索的数好像不准"。
"""
import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v43.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v43-api.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _done(tmp_db, name="a.jpg", size=100, **fields):
    """建一个已落盘的资源。资源库只显示 status=done 的那些。"""
    tid = tmp_db.create_task("https://example.com/album", "generic")
    rid = tmp_db.add_resource(tid, fields.pop("type", "image"),
                              f"https://example.com/{name}", size=size)
    tmp_db.update_resource(rid, status="done", local_path=f"/tmp/{name}", **fields)
    return rid


# ==================================================================
# 1. 排序: 白名单 / 方向 / 未知代号
# ==================================================================

def test_sort_keys_are_a_whitelist(tmp_db):
    """**排序代号只能来自 LIBRARY_SORTS** —— 未知代号抛错, 不是静默用默认。

    静默回退的后果不是"排序不生效"这么轻: 界面点了"按大小排"却拿到加入顺序,
    用户会认为"这软件的大小排序是坏的", 而日志里一个字都没有。
    """
    with pytest.raises(ValueError):
        db.library_order_by("nope")
    with pytest.raises(ValueError):
        db.library_order_by("r.id; DROP TABLE resources")


def test_order_by_only_emits_whitelisted_columns(tmp_db):
    """拼出来的片段里**只能**出现白名单里的列表达式。

    (`r.id` 是每一档都带的主键兜底, 见下一条用例, 所以这里不把它算作"混进来"。)
    """
    for key, (column, _) in db.LIBRARY_SORTS.items():
        clause = db.library_order_by(key, "asc")
        assert clause.startswith(f"ORDER BY {column} "), f"{key} 没走它自己的列"
        for other_key, (other, _) in db.LIBRARY_SORTS.items():
            if other == column or other == "r.id":
                continue
            assert other not in clause, f"{key} 的片段里混进了 {other_key} 的列"


def test_every_sort_has_a_tiebreaker(tmp_db):
    """除主键外, 每一档都要带次级键 `r.id`(见本文件开头第 2 条)。"""
    for key, (column, _) in db.LIBRARY_SORTS.items():
        clause = db.library_order_by(key, "desc")
        if column == "r.id":
            assert clause == "ORDER BY r.id DESC"
        else:
            assert clause.endswith(", r.id DESC"), f"{key} 缺次级键"


def test_default_sort_keeps_old_behaviour(tmp_db):
    """不传 sort 与显式传 `added` 必须**逐位一致** —— 这是老行为, 不能变。"""
    for i in range(4):
        _done(tmp_db, f"{i}.jpg")
    a = [r["id"] for r in db.library_list()]
    b = [r["id"] for r in db.library_list(sort="added")]
    assert a == b == sorted(a, reverse=True)


def test_sort_by_size_and_order(tmp_db):
    """按大小排: 降序在前的是最大的, 升序反过来。"""
    small = _done(tmp_db, "s.jpg", size=10)
    big = _done(tmp_db, "b.jpg", size=9999)
    mid = _done(tmp_db, "m.jpg", size=500)

    desc = [r["id"] for r in db.library_list(sort="size", order="desc")]
    assert desc == [big, mid, small]
    asc = [r["id"] for r in db.library_list(sort="size", order="asc")]
    assert asc == [small, mid, big]


def test_sort_by_rating(tmp_db):
    """按星级排。没评过的按 0 算, 但不许因为 NULL 把它们排到最前面。"""
    a = _done(tmp_db, "a.jpg", size=1)
    b = _done(tmp_db, "b.jpg", size=1)
    tmp_db.set_rating([b], 5)
    order = [r["id"] for r in db.library_list(sort="rating", order="desc")]
    assert order == [b, a]


def test_sort_by_name(tmp_db):
    """按文件名(本地路径)。"""
    _done(tmp_db, "c.jpg", size=1)
    _done(tmp_db, "a.jpg", size=1)
    _done(tmp_db, "b.jpg", size=1)
    got = [r["local_path"] for r in db.library_list(sort="name", order="asc")]
    assert got == ["/tmp/a.jpg", "/tmp/b.jpg", "/tmp/c.jpg"]


def test_paging_over_tied_rows_has_no_gaps_or_repeats(tmp_db):
    """**同值行逐页取, 不重不漏** —— 这条只有把每页单独取出来才验得到。

    3 条同样大小的资源, 用 limit=1 翻三页。没有次级键时, 页与页之间会出现
    重复(同一行出现在两页)或空洞(某一行一次都不出现), 而两种都不报错。
    """
    for i in range(3):
        _done(tmp_db, f"t{i}.jpg", size=1234)
    whole = [r["id"] for r in db.library_list(sort="size", order="desc")]
    paged = []
    for offset in range(3):
        page = db.library_list(sort="size", order="desc", limit=1, offset=offset)
        assert len(page) == 1, "翻页翻出了空页 —— 次级键缺失的典型症状"
        paged.append(page[0]["id"])
    assert paged == whole, f"逐页取到 {paged}, 整表是 {whole}"
    assert len(set(paged)) == 3


def test_api_echoes_effective_sort(client):
    """`/library` 要回显**实际生效**的排序, 前端才能把当前档位高亮对。

    回显 None 或原样回传请求值都是不够的: 前者让界面不知道自己在按什么排,
    后者在"请求被兜底成默认"时会高亮错的档位。
    """
    c, _ = client
    _done(db, "a.jpg", size=5)
    r = c.get("/library", params={"sort": "size", "order": "asc"})
    assert r.status_code == 200
    body = r.json()
    assert body["sort"] == "size" and body["order"] == "asc"
    assert body["default_sort"] == db.LIBRARY_DEFAULT_SORT
    assert [s["key"] for s in body["sorts"]] == list(db.LIBRARY_SORTS)


def test_api_rejects_unknown_sort_with_400(client):
    """未知代号回 **400**, 不是静默按默认排。"""
    c, _ = client
    r = c.get("/library", params={"sort": "drop-table"})
    assert r.status_code == 400, r.text
    assert "sort" in r.json()["detail"]


def test_facets_and_library_share_one_sort_source(client):
    """两处下发的档位必须**同源** —— 各写一份迟早会漂。"""
    c, _ = client
    lib = c.get("/library").json()
    facets = c.get("/library/facets").json()
    assert lib["sorts"] == facets["sorts"]
    assert lib["default_sort"] == facets["default_sort"]


# ==================================================================
# 2. 保存的搜索: 条件校验(保存时拒绝坏条件)
# ==================================================================

def test_unknown_param_key_is_rejected(tmp_db):
    """未知筛选键**抛错**而不是悄悄丢掉。

    丢掉是"这个搜索少筛了一项"(看得见结果不对), 但更糟的是它会被原样回灌到
    `library_filters` —— 那时多出来的键就是个永远走不到的幽灵。
    """
    with pytest.raises(ValueError):
        db.clean_search_params({"q": "x", "evil": "1"})


def test_empty_values_are_dropped(tmp_db):
    """"不限"的取值不存: 存下来只会让这个搜索的定义读起来含混(到底筛没筛?)。"""
    kept = db.clean_search_params(
        {"q": "x", "album": "", "favorite": False, "min_rating": 0,
         "tag": None, "kind": "all", "special": ""}
    )
    assert kept == {"q": "x"}


def test_invalid_special_is_rejected_at_save_time(tmp_db):
    """非法 `special` 在**保存时**就拒绝。

    放进去的后果不是"这个搜索查不出东西", 而是**每次列出搜索**都抛异常 ——
    整个侧栏打不开。所以拒绝点必须在保存这一侧。
    """
    with pytest.raises(ValueError):
        db.save_search("坏的", {"special": "not-a-facet"})


def test_min_rating_out_of_range_is_rejected(tmp_db):
    with pytest.raises(ValueError):
        db.save_search("越界", {"min_rating": 9})


def test_blank_name_is_rejected(tmp_db):
    with pytest.raises(ValueError):
        db.save_search("   ", {"q": "x"})


# ==================================================================
# 3. 保存的搜索: 新建 / 覆盖 / 删除
# ==================================================================

def test_save_then_list_roundtrip(tmp_db):
    rid = _done(tmp_db, "a.jpg")
    tmp_db.set_favorite([rid], True)
    sid, created = db.save_search("只看收藏", {"favorite": True, "q": "a"})
    assert created is True
    rows = db.list_searches()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == sid and row["name"] == "只看收藏"
    assert row["params"] == {"favorite": True, "q": "a"}
    # 命中数是**必须**的(第 27 条): 没有它, "配了但一条都没匹配"和"没配"
    # 在界面上长得一模一样。
    assert row["count"] == 1


def test_same_name_overwrites_instead_of_duplicating(tmp_db):
    """同名覆盖。用户的常态是"改完条件再存", 报"已存在"只会逼他先删再建。"""
    db.save_search("我的", {"q": "a"})
    sid, created = db.save_search("我的", {"q": "b"})
    assert created is False
    rows = db.list_searches()
    assert len(rows) == 1, "同名应当覆盖, 不该出现两条"
    assert rows[0]["id"] == sid
    assert rows[0]["params"] == {"q": "b"}


def test_count_zero_is_zero_not_none(tmp_db):
    """**匹配 0 条 ⇒ count 是 0**, 不是 None(第 26 条)。

    这是本文件最要紧的一条: 界面必须能说"这个搜索现在没有东西", 而不是
    "这个搜索我算不出来"。两者混在一起时, 用户会去查库、查权限, 而真相只是
    条件太窄。
    """
    _done(tmp_db, "a.jpg")
    db.save_search("永远为空的", {"q": "zzz-no-such-thing"})
    row = db.list_searches()[0]
    assert row["count"] == 0
    assert row.get("broken") is not True


def test_broken_params_give_none_not_zero(tmp_db):
    """库里的条件读不出来 ⇒ `count=None` + `broken=True`(**不是** 0)。

    手改过库、或旧版本写下的脏值都会走到这里。给 0 等于说谎: 我们并没有
    数过, 只是算不动。降级本身可以接受, 但**必须留痕**, 否则这条搜索会静默
    变成"匹配全部"(params 为空)。
    """
    execute = db.execute
    execute("INSERT INTO library_searches(name, params, created_at) VALUES(?,?,?)",
            ("脏的", "{not json", 1.0))
    row = db.list_searches()[0]
    assert row["broken"] is True
    assert row["count"] is None, "算不动就是算不动, 不能报 0"


def test_saved_search_survives_unknown_special_in_db(tmp_db):
    """即使库里真存了一个非法 `special`, 列表也不许 500 —— 降级 + 留痕。"""
    db.execute(
        "INSERT INTO library_searches(name, params, created_at) VALUES(?,?,?)",
        ("坏的", json.dumps({"special": "not-a-facet"}), 1.0),
    )
    row = db.list_searches()[0]
    assert row["broken"] is True and row["count"] is None


def test_delete_is_idempotent(tmp_db):
    sid, _ = db.save_search("删我", {"q": "x"})
    assert db.delete_search(sid) == 1
    assert db.delete_search(sid) == 0, "删不存在的应当是 0, 不是报错"
    assert db.list_searches() == []


def test_list_is_ordered_by_name(tmp_db):
    """按名字序, 与新建顺序无关 —— 否则每次新建都会把列表顺序全打乱。"""
    for n in ("c", "a", "b"):
        db.save_search(n, {"q": n})
    assert [r["name"] for r in db.list_searches()] == ["a", "b", "c"]


# ==================================================================
# 4. 保存的搜索: HTTP 层
# ==================================================================

def test_search_endpoints_roundtrip(client):
    c, _ = client
    _done(db, "a.jpg")

    r = c.post("/library/searches", json={"name": "收藏", "params": {"favorite": True}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    sid = body["id"]

    listed = c.get("/library/searches").json()
    assert len(listed) == 1 and listed[0]["id"] == sid
    assert listed[0]["count"] == 0, "这条没有匹配, 必须是 0 而不是 null"

    # 覆盖同名: created=False
    again = c.post("/library/searches",
                   json={"name": "收藏", "params": {"q": "a"}}).json()
    assert again["created"] is False and again["id"] == sid
    assert len(c.get("/library/searches").json()) == 1

    d = c.delete(f"/library/searches/{sid}").json()
    assert d["deleted"] is True and d["name"] == "收藏"
    assert c.get("/library/searches").json() == []
    # 再删一次: 幂等
    assert c.delete(f"/library/searches/{sid}").json()["deleted"] is False


def test_search_post_rejects_bad_params_with_400(client):
    """坏条件回 400 且**不许落库** —— 落进去会让侧栏永远打不开。"""
    c, _ = client
    r = c.post("/library/searches", json={"name": "x", "params": {"bogus": 1}})
    assert r.status_code == 400, r.text
    assert c.get("/library/searches").json() == []


def test_applying_a_search_replaces_all_conditions(client):
    """应用一个保存的搜索 = 把所有筛选条件**整体替换**。

    合并的后果: 侧栏上写着"这个搜索有 12 条", 点进去列表却只有 3 条(被上一轮
    的标签/星级继续卡着)。这类偏差不报错, 只表现为"这个搜索的数好像不准"。

    这里从**后端口径**验: 一个只存了 `favorite` 的搜索, 用它的 params 去查,
    结果必须与"只按 favorite 查"逐位一致。
    """
    c, _ = client
    fav = _done(db, "fav.jpg")
    _done(db, "other.jpg")
    db.set_favorite([fav], True)

    sid = c.post("/library/searches",
                 json={"name": "只有收藏", "params": {"favorite": True}}).json()["id"]
    saved = [s for s in c.get("/library/searches").json() if s["id"] == sid][0]

    # 用存下来的参数查 —— 与侧栏上那个 count 必须对得上
    got = c.get("/library", params=saved["params"]).json()
    assert got["total"] == saved["count"] == 1
    assert [i["id"] for i in got["items"]] == [fav]
