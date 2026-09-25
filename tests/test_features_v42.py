"""V42 测试: 从同类产品吸收的四项 + 一项自己找出来的。

吸收了什么, 以及各自的判据为什么要钉住
======================================
1. **评分**(Eagle / digiKam / TagStudio / Immich 都让"收藏"与"评分"并存)。
   判据不是"能存下星级", 而是**上限由后端硬拦** —— 一个 7 星的资源会让"≥5 星"
   这个口径说不清, 而界面那一侧只负责画星星, 它不知道上限是多少。
2. **体检视图**(TagStudio 的 `special:` 语法 / Czkawka 的十种扫描模式)。
   判据是 **0 就是 0**: "没有这类"与"没算出这类"必须是两个结论(第 26 条)。
   所以空库上每个维度都要有条目且 n=0, 而不是返回空列表。
3. **重复分组**(dupeGuru 的 reference / Czkawka 的 `-D AEN/AEB`)。
   判据是**建议理由必须是代号**: 像素最高 / 体积最大 / 最早, 三个理由各有一条
   用例走它自己的那条路 —— 混在一起时"理由对不对"没人验。
4. **URL 归档**(yt-dlp / gallery-dl 的 `--download-archive`)。
   判据是 **added 与 skipped 都给**(第 27 条): 只回"导入成功"的话, 用户没法
   判断这份归档是不是真被吃进去了。
5. **扩展名与文件头不符**(Czkawka 的 "bad extensions")。
   判据是**宁可漏报不可误报**: 不认识的扩展名、认不出的文件头, 一律不下结论。
   典型现场是 CDN 回了 HTML 错误页而长度正好对得上 —— 长度校验通过, 之后缩略图
   与指纹全建立在这份 HTML 上, 全程不报错。

"0 就是 0" 这条在本文件里出现三次(体检 / 评分区间 / 归档计数), 是因为它们
是同一类失败: 把"数过了, 是 0"读成"没数"。
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db
from core import filekind


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v42.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v42-api.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _done(tmp_db, name="a.jpg", **fields):
    """建一个已落盘的资源。资源库只显示 status=done 的那些。"""
    tid = tmp_db.create_task("https://example.com/album", "generic")
    rid = tmp_db.add_resource(tid, fields.pop("type", "image"),
                              f"https://example.com/{name}")
    tmp_db.update_resource(rid, status="done", local_path=f"/tmp/{name}", **fields)
    return rid


# ==================================================================
# 1. 扩展名与文件头(core/filekind.py)
# ==================================================================

def test_html_error_page_saved_as_jpg_is_reported(tmp_path):
    """最要紧的一类: CDN 回了一个 HTML 错误页, 长度还正好是它的长度。"""
    p = tmp_path / "00001.jpg"
    p.write_bytes(b"<!DOCTYPE html><html><body>403</body></html>")
    reason = filekind.mismatch_reason(p)
    assert reason, "HTML 被当成 .jpg 存下来, 必须报出来 —— 否则它全程不报错地"
    # 说明里要同时说清"声称是什么 / 实际是什么", 用户才能判断下一步做什么
    assert "JPEG" in reason and "网页" in reason


def test_matching_file_is_not_reported(tmp_path):
    p = tmp_path / "ok.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    assert filekind.mismatch_reason(p) is None


def test_png_head_under_jpg_name_is_reported(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert filekind.mismatch_reason(p) and "PNG" in filekind.mismatch_reason(p)


def test_unknown_extension_is_not_judged(tmp_path):
    """不认识的扩展名 -> 不下结论。判据的松紧按"误报的代价"定(见模块 docstring)。"""
    p = tmp_path / "weird.xyz"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert filekind.ext_kind(p) is None
    assert filekind.mismatch_reason(p) is None


def test_unrecognized_head_is_not_judged(tmp_path):
    p = tmp_path / "mystery.jpg"
    p.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")
    assert filekind.sniff_kind(p) is None
    assert filekind.mismatch_reason(p) is None


def test_isobmff_family_is_not_a_false_positive(tmp_path):
    """.heic / .mp4 共用 ISOBMFF 容器, 只靠前 12 字节分不出来 —— 细分会误报。"""
    head = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8
    for name in ("a.heic", "a.mp4", "a.mov", "a.avif"):
        p = tmp_path / name
        p.write_bytes(head)
        assert filekind.sniff_kind(p) == "isobmff"
        assert filekind.mismatch_reason(p) is None, f"{name} 不该被判成名字不对"


# ==================================================================
# 2. 评分
# ==================================================================

def test_rating_round_trip_and_zero_means_unrated(tmp_db):
    rid = _done(tmp_db)
    assert db.set_rating([rid], 5) == 1
    # 0 = 清除评分回到"未评分", 不是"打 0 分"
    assert db.set_rating([rid], 0) == 1
    assert tmp_db.query_one("SELECT rating AS r FROM resources WHERE id=?", (rid,))["r"] == 0


@pytest.mark.parametrize("bad", [6, -1, 99])
def test_rating_out_of_range_is_rejected(tmp_db, bad):
    """上限由后端硬拦: 一个 7 星的资源会让"≥5 星"的口径说不清。"""
    rid = _done(tmp_db)
    with pytest.raises(ValueError):
        db.set_rating([rid], bad)


def test_rating_non_integer_is_rejected(tmp_db):
    rid = _done(tmp_db)
    with pytest.raises(ValueError):
        db.set_rating([rid], "five")


def test_min_rating_filters_by_lower_bound(tmp_db):
    a = _done(tmp_db, "a.jpg")
    b = _done(tmp_db, "b.jpg")
    c = _done(tmp_db, "c.jpg")
    db.set_rating([a], 2)
    db.set_rating([b], 4)
    db.set_rating([c], 5)
    got = {r["id"] for r in db.library_list(min_rating=4)}
    assert got == {b, c}
    # 0 = 不限, 不是"只要未评分的"
    assert len(db.library_list(min_rating=0)) == 3


def test_empty_library_facets_still_report_zero(tmp_db):
    """空库上每个维度都要有条目, 且 n=0 —— "没有这类"与"没算出这类"是两个结论。"""
    f = db.library_facets()
    assert f["items"], "维度列表不许为空: 空列表会被读成没法数"
    assert all(it["n"] == 0 for it in f["items"])
    # 评分区间**六档都在**(0..5), 全空的库也要给全 —— 否则"5 星有多少"没法问
    assert [b["stars"] for b in f["ratings"]] == [0, 1, 2, 3, 4, 5]
    assert all(b["n"] == 0 for b in f["ratings"])
    assert f["total"] == 0


def test_facets_count_is_real(tmp_db):
    a = _done(tmp_db, "a.jpg")
    b = _done(tmp_db, "b.jpg")
    db.set_rating([a], 3)
    tmp_db.update_resource(b, duplicate_of=a)
    tmp_db.add_tags([a], ["系列"])
    f = db.library_facets()
    n = {it["key"]: it["n"] for it in f["items"]}
    assert n["untagged"] == 1          # 只有 b 没标签
    assert n["unrated"] == 1           # 只有 b 没评分
    assert n["duplicate"] == 1
    assert n["corrupt"] == 0           # 0 就是 0, 不是"没数"
    stars = {b_["stars"]: b_["n"] for b_ in f["ratings"]}
    assert stars[3] == 1 and stars[0] == 1
    assert f["total"] == 2


def test_unknown_special_filter_raises_instead_of_ignoring(tmp_db):
    """未知键必须报错(API 层转 400): 静默忽略的表现是"点了没反应"。"""
    _done(tmp_db)
    with pytest.raises(ValueError):
        db.library_list(special="nope")
    with pytest.raises(ValueError):
        db.library_count(special="nope")


def test_special_filters_are_consistent_with_list(tmp_db):
    """count 与 list 必须同口径 —— 两处各写一遍是分页错位最常见的来源。"""
    a = _done(tmp_db, "a.jpg")
    _done(tmp_db, "b.jpg")
    db.set_rating([a], 1)
    assert db.library_count(special="rated") == len(db.library_list(special="rated")) == 1
    assert db.library_count(special="unrated") == 1


# ==================================================================
# 3. 重复分组
# ==================================================================

def _group(tmp_db, members):
    """members: [(width, height, size), ...] 第一个是"原件", 其余指向它。"""
    ids = []
    for i, (w, h, size) in enumerate(members):
        rid = _done(tmp_db, f"{i}.jpg", width=w, height=h, size=size)
        ids.append(rid)
    for other in ids[1:]:
        tmp_db.update_resource(other, duplicate_of=ids[0])
    return ids


def test_keep_picks_highest_resolution(tmp_db):
    ids = _group(tmp_db, [(800, 600, 100), (1600, 1200, 900), (400, 300, 50)])
    g = db.duplicate_groups()[0]
    assert g["keep_id"] == ids[1]
    assert g["keep_reason"] == "highest_res"
    assert g["n"] == 3
    assert g["bytes"] == 1050


def test_keep_falls_back_to_largest_when_pixels_unknown(tmp_db):
    """没量到尺寸时(pixels 全 0)退到体积 —— "没量到"不能退化成"随便挑一个"。"""
    ids = _group(tmp_db, [(None, None, 100), (None, None, 900)])
    g = db.duplicate_groups()[0]
    assert g["keep_id"] == ids[1]
    assert g["keep_reason"] == "largest"


def test_keep_falls_back_to_oldest_when_all_equal(tmp_db):
    ids = _group(tmp_db, [(100, 100, 100), (100, 100, 100)])
    g = db.duplicate_groups()[0]
    assert g["keep_id"] == ids[0]
    assert g["keep_reason"] == "oldest"


def test_no_duplicates_returns_empty_list(tmp_db):
    _done(tmp_db)
    assert db.duplicate_groups() == []


def test_bigger_groups_come_first(tmp_db):
    _group(tmp_db, [(10, 10, 10), (10, 10, 10)])                       # 2 条
    _group(tmp_db, [(20, 20, 20), (20, 20, 20), (20, 20, 20)])         # 3 条
    gs = db.duplicate_groups()
    assert [g["n"] for g in gs] == [3, 2]


# ==================================================================
# 4. URL 归档
# ==================================================================

def test_archive_import_reports_added_and_skipped(tmp_db):
    """只回"导入成功"的话, 用户没法判断归档是不是真被吃进去了。"""
    added, skipped = db.url_archive_add([("https://a/1", None), ("https://a/2", None)])
    assert (added, skipped) == (2, 0)
    added2, skipped2 = db.url_archive_add([("https://a/1", None), ("https://a/3", None)])
    assert (added2, skipped2) == (1, 1)
    assert db.url_archive_count() == 3


def test_archive_has_is_batch_and_exact(tmp_db):
    db.url_archive_add([(f"https://a/{i}", None) for i in range(1200)])
    hits = db.url_archive_has(["https://a/0", "https://a/1199", "https://a/nope"])
    assert hits == {"https://a/0", "https://a/1199"}


def test_archive_accepts_yt_dlp_two_column_lines(tmp_db):
    """yt-dlp 的归档是 `extractor id` 两列, 不是 URL —— 但也收下:
    它同样能回答"这个我下过没有", 而且用户很可能直接贴过来。"""
    added, skipped = db.url_archive_add([("youtube dQw4w9WgXcQ", None)])
    assert (added, skipped) == (1, 0)
    assert list(db.url_archive_has(["youtube dQw4w9WgXcQ"])) == ["youtube dQw4w9WgXcQ"]


def test_archive_clear_touches_only_the_archive(tmp_db):
    rid = _done(tmp_db)
    db.url_archive_add([("https://a/1", None)])
    assert db.url_archive_clear() == 1
    assert db.url_archive_count() == 0
    assert db.library_count() == 1, "清空归档不许碰资源库"


# ==================================================================
# 5. 接口层: 两个"必须报错而不是静默"的地方
# ==================================================================

def test_unknown_special_is_400_not_silently_ignored(client):
    app, _ = client
    r = app.get("/library", params={"special": "nope"})
    assert r.status_code == 400, "静默忽略的表现是点了没反应, 比报错难查得多"


def test_rating_out_of_range_is_400(client):
    app, d = client
    r = app.post("/library/rate", json={"ids": [1], "rating": 9})
    assert r.status_code == 400


def test_facets_endpoint_shape(client):
    app, d = client
    r = app.get("/library/facets").json()
    assert r["items"] and all("n" in it and "key" in it for it in r["items"])
    assert len(r["ratings"]) == 6
    assert r["total"] == 0


def test_rate_endpoint_updates_and_library_returns_rating(client):
    app, d = client
    tid = d.create_task("https://example.com/album", "generic")
    rid = d.add_resource(tid, "image", "https://example.com/a.jpg")
    d.update_resource(rid, status="done", local_path="/tmp/a.jpg")
    r = app.post("/library/rate", json={"ids": [rid], "rating": 4}).json()
    assert r["updated"] == 1 and r["rating"] == 4
    item = app.get("/library").json()["items"][0]
    assert item["rating"] == 4
    # 徽章中文由后端下发, 界面不自己编
    assert "mismatch" in app.get("/library").json()["error_kind_labels"]


def test_url_archive_round_trip(client):
    app, _ = client
    r = app.post("/library/url-archive", json={
        "text": "# comment\n\nhttps://a/1\nhttps://a/2 abc\nhttps://a/1\n",
    }).json()
    # 空行与 # 行被跳过; 第三次是重复的 -> skipped
    assert r["added"] == 2 and r["skipped"] == 1 and r["total"] == 2
    exp = app.get("/library/url-archive").json()
    assert exp["urls"] == ["https://a/1", "https://a/2"]
    assert app.delete("/library/url-archive").json()["cleared"] == 2
    assert app.get("/library/url-archive").json()["total"] == 0


def test_duplicates_endpoint_has_reason_labels(client):
    app, d = client
    tid = d.create_task("https://example.com/album", "generic")
    a = d.add_resource(tid, "image", "https://example.com/a.jpg")
    b = d.add_resource(tid, "image", "https://example.com/b.jpg")
    for rid, (w, h, s) in ((a, (100, 100, 10)), (b, (200, 200, 40))):
        d.update_resource(rid, status="done", local_path=f"/tmp/{rid}.jpg",
                          width=w, height=h, size=s)
    d.update_resource(b, duplicate_of=a)
    r = app.get("/library/duplicates").json()
    assert r["total"] == 1
    # keep_reason 是代号, 中文由 reasons 下发 —— 判据不能挂在中文串上(第 9 条)
    assert r["groups"][0]["keep_reason"] == "highest_res"
    assert {x["key"] for x in r["reasons"]} == {"highest_res", "largest", "oldest"}


def test_verify_kinds_are_downstream_labels(client):
    """巡检现在有三类; 中文随结果下发, 前端加一类时不会静默显示成代号。"""
    app, _ = client
    r = app.post("/library/verify", json={"limit": 10}).json()
    assert {k["kind"] for k in r["kinds"]} == {"missing", "corrupt", "mismatch"}
    assert all(k["label"] for k in r["kinds"])
