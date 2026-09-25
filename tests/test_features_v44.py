"""V44 测试: 文件头规范化 / 主色检索 / 打包导出 / 虚拟相册 / Webhook / 自检面板。

判据的重点不在"功能跑得通", 而在下面几类**不报错、只是结果不对**的失败上:

1. **改名默认必须是 dry-run**(A1)。
   改名落到用户磁盘上且不可逆。默认值若写成"直接改", 那么任何一处误调用
   (包括将来的)都会立刻动用户的文件, 而没有任何一步确认。

2. **认不准就不许动手**(A1, 与 filekind 同款取舍)。
   ISOBMFF 一族(mp4/mov/heic/avif)共用容器, `.jpg` 装的其实是 ftyp 时,
   我们不能断定它"应该是 mp4 还是 heic" —— 这时改名就是**误判**。用例钉住
   这种情况必须进 `skipped` 而不是被改掉。

3. **筛选参数必须同时传给 count 与 list**(B3)。
   `library_filters` 是被按位置调用的, 少传一个 `color` 就表现为"按颜色筛了但
   没生效" —— 不报错, 只是 `total` 与 `items` 口径不一致。这是本次改动中
   **实际发生过的**一处, 用例专门钉它。

4. **"没算出" ≠ "没有"**(第 26 条): 读不出主色返回 `None` 且不写库;
   导出时磁盘上已不在的文件进 `skipped` 而不是悄悄少一个。

5. **超限/参数缺失要报错, 不许静默截断**(C2): 只导出前 N 个会让用户以为导全了。

6. **投递失败必须留痕**(C4, 第 G 条): webhook 配错的表现是"什么都没发生"。

7. **没跑的闸不许算绿**(D1): `checked=0` 的闸 `ok=False` 且写明为什么没跑。
"""
import json
import os
import subprocess
import zipfile

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.colors as colors
import core.database as db
import core.exporter as exporter
import core.webhooks as hooks
from core import filekind


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v44.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v44-api.db")
    monkeypatch.setattr(db, "_conn", None)
    # 不起真 worker(第 14 条: 测试里"顺手调真实入口"会给下一个用例写心跳)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _write(path, head):
    """写一个小文件, 文件头是 `head`(用来伪造"名字与内容不符")。"""
    path.write_bytes(head + b"\x00" * 32)
    return path


PNG_HEAD = b"\x89PNG\r\n\x1a\n"
JPEG_HEAD = b"\xff\xd8\xff\xe0"
FTYP_HEAD = b"\x00\x00\x00\x18ftypisom"


def _done(tmp_db, path, name="0001.jpg", type_="image", **fields):
    tid = tmp_db.create_task("https://example.com/album", "generic")
    rid = tmp_db.add_resource(tid, type_, f"https://example.com/{name}", size=100)
    tmp_db.update_resource(rid, status="done", local_path=str(path), **fields)
    return rid


# ==================================================================
# 1. 文件头规范化: 默认 dry-run / 真的改 / 认不准不动
# ==================================================================

def test_normalize_defaults_to_dry_run(tmp_db, tmp_path):
    """默认**不改磁盘** —— 改名不可逆, 默认必须是"只给计划"。"""
    p = _write(tmp_path / "0001.jpg", PNG_HEAD)   # 叫 jpg, 其实是 png
    rid = _done(tmp_db, p, error_kind="mismatch")

    res = db.normalize_resources(ids=[rid])       # 不传 dry_run
    assert res["dry_run"] is True
    assert p.exists(), "默认调用就把用户的文件改了 —— 默认值错了"
    assert res["changed"], "应该给出改名计划"
    assert res["changed"][0]["to"].endswith(".png")


def test_normalize_renames_and_updates_db(tmp_db, tmp_path):
    """真的执行时: 改扩展名 + 更新库里的 local_path/filename, 且清掉 mismatch。"""
    p = _write(tmp_path / "0001.jpg", PNG_HEAD)
    rid = _done(tmp_db, p, error_kind="mismatch")

    res = db.normalize_resources(ids=[rid], dry_run=False)
    assert len(res["changed"]) == 1
    new = tmp_path / "0001.png"
    assert new.exists() and not p.exists()
    row = db.get_resource(rid)
    assert row["local_path"] == str(new)
    assert row["filename"] == "0001.png"
    # 名字与内容已经对上, 这个标记该消失(留着会让体检数字一直挂着)
    assert row["error_kind"] is None


def test_normalize_leaves_matching_files_alone(tmp_db, tmp_path):
    """名字与内容相符的不动 —— 动了好文件是**误报**, 代价远大于漏改。"""
    p = _write(tmp_path / "0002.jpg", JPEG_HEAD)
    rid = _done(tmp_db, p)
    res = db.normalize_resources(ids=[rid], dry_run=False)
    assert res["changed"] == []
    assert res["skipped"][0]["reason"] == "ok"
    assert p.exists()


def test_normalize_refuses_ambiguous_isobmff(tmp_db, tmp_path):
    """ISOBMFF 一族不许被强改: 认得出"不符", 但断定不了该改成哪个。

    `.jpg` 装的其实是 ftyp —— 它可能是 mp4 / mov / heic / avif 中的任意一个,
    而这几个共用同一个容器。改名就是把猜测写到用户磁盘上。
    """
    p = _write(tmp_path / "0003.jpg", FTYP_HEAD)
    rid = _done(tmp_db, p)
    res = db.normalize_resources(ids=[rid], dry_run=False)
    assert res["changed"] == []
    assert res["skipped"][0]["reason"] == "ambiguous"
    assert p.exists()


def test_normalize_reports_checked_count(tmp_db, tmp_path):
    """`checked` 必须给(第 27 条): 只回"改了几个"的话,
    "一条都没匹配上"与"规则配错了"看起来一样。"""
    p = _write(tmp_path / "0004.jpg", PNG_HEAD)
    _done(tmp_db, p, error_kind="mismatch")
    missing = tmp_db.create_task("https://example.com/x", "generic")
    rid2 = tmp_db.add_resource(missing, "image", "https://example.com/gone.jpg")
    tmp_db.update_resource(rid2, status="done", local_path=str(tmp_path / "gone.jpg"),
                           error_kind="mismatch")

    res = db.normalize_resources(dry_run=True)
    assert res["checked"] == 2
    assert len(res["changed"]) == 1
    assert res["skipped"][0]["reason"] == "missing"   # 盘上不在的必须说出来


def test_normalize_endpoint_defaults_to_dry_run(client, tmp_path):
    """端点同样默认 dry-run —— 默认值只有一处是对的, 两处各自写就会漏。"""
    cl, d = client
    p = _write(tmp_path / "0005.jpg", PNG_HEAD)
    rid = _done(d, p, error_kind="mismatch")
    r = cl.post("/library/normalize", json={"ids": [rid]})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert p.exists()


# ==================================================================
# 2. 主色检索
# ==================================================================

def test_family_of_rgb_buckets():
    """色系分档: 先摘黑/白/灰, 再按色相分彩色。"""
    assert colors.family_of_rgb(224, 32, 32) == "red"
    assert colors.family_of_rgb(32, 96, 224) == "blue"
    assert colors.family_of_rgb(128, 128, 128) == "gray"
    assert colors.family_of_rgb(250, 250, 250) == "white"
    assert colors.family_of_rgb(8, 8, 8) == "black"


def test_dominant_returns_none_when_unreadable(tmp_path):
    """读不出来 -> `None`(没算出), **不许**归成某个色系(第 26 条)。

    归成 gray 的话, "按灰色筛"会混进一堆根本没测过的文件。
    """
    assert colors.dominant(tmp_path / "nope.jpg") is None


def test_dominant_of_raw_pixels_finds_largest_area():
    """取**面积最大**的那一档, 不是全图平均(平均色会把蓝天绿地算成灰)。"""
    # 3/4 红 + 1/4 蓝 -> 应该判红
    data = bytes([224, 32, 32]) * 3 + bytes([32, 96, 224])
    assert colors._dominant_of_rgb24(data) == "red"


def test_unknown_color_filter_is_rejected(client):
    """未知色系**报 400**, 不是静默筛出 0 条。

    筛出 0 条会被读成"库里没有这个颜色的图", 而真相是"传了个不认识的键"。
    """
    cl, d = client
    r = cl.get("/library?color=octarine")
    assert r.status_code == 400


def test_color_filter_reaches_both_count_and_list(client, tmp_path):
    """`color` 必须同时进 count 与 list(口径一致的钉子)。

    这是本次改动里**实际发生过**的一处静默失效: `library_filters` 按位置被调用,
    少传一处就表现为"筛了但没生效" —— 不报错, 只是 total 与 items 对不上。
    """
    cl, d = client
    a = _write(tmp_path / "blue.jpg", JPEG_HEAD)
    b = _write(tmp_path / "red.jpg", JPEG_HEAD)
    rid_a = _done(d, a, name="blue.jpg")
    rid_b = _done(d, b, name="red.jpg")
    d.set_dominant_color(rid_a, "blue")
    d.set_dominant_color(rid_b, "red")

    r = cl.get("/library?color=blue")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1, "total 与筛选条件不一致(少传了 color)"
    assert len(body["items"]) == 1, "items 与筛选条件不一致(少传了 color)"
    assert body["items"][0]["id"] == rid_a


def test_color_is_persistable_in_saved_search(tmp_db):
    """`color` 在保存搜索的白名单里 —— 少一个键等于"这个搜索少筛了一项"。"""
    assert "color" in db.LIBRARY_QUERY_KEYS
    sid, _ = db.save_search("蓝色的图", {"color": "blue"})
    row = db.get_search(sid)
    assert row["params"]["color"] == "blue"
    with pytest.raises(ValueError):
        db.save_search("坏色系", {"color": "octarine"})


def test_extract_colors_reports_written_and_checked(tmp_db, tmp_path):
    """`written` 与 `checked` 两个都给(第 27 条)。"""
    # ffmpeg 不在时 dominant 一律 None -> written=0 但 checked 仍是 1
    p = tmp_path / "e.jpg"
    p.write_bytes(JPEG_HEAD + b"\x00" * 64)
    _done(tmp_db, p, name="e.jpg")
    written, checked = db.extract_colors()
    assert checked == 1
    assert 0 <= written <= 1


def test_facets_publish_color_families(client):
    """色系清单由后端下发(键 + 中文名 + 代表色), 前端不自带一份。"""
    cl, d = client
    body = cl.get("/library/facets").json()
    assert body["colors"], "facets 没有下发色系"
    assert {"key", "label", "hex"} <= set(body["colors"][0])


# ==================================================================
# 3. 虚拟相册: 保存的搜索变成能点进去的实体
# ==================================================================

def test_virtual_album_items(client, tmp_path):
    """`GET /library/searches/{sid}/items` 按**存下来的条件**返回资源。"""
    cl, d = client
    a = _write(tmp_path / "v1.jpg", JPEG_HEAD)
    b = _write(tmp_path / "v2.jpg", JPEG_HEAD)
    rid_a = _done(d, a, name="v1.jpg")
    _done(d, b, name="v2.jpg")
    d.set_dominant_color(rid_a, "blue")

    sid, _ = d.save_search("蓝色的", {"color": "blue"})
    r = cl.get(f"/library/searches/{sid}/items")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == rid_a


def test_virtual_album_is_live_not_a_snapshot(client, tmp_path):
    """条件是**实时**算的: 新下的图符合条件就应出现, 不是存下来的快照。"""
    cl, d = client
    sid, _ = d.save_search("蓝色的", {"color": "blue"})
    assert cl.get(f"/library/searches/{sid}/items").json()["total"] == 0

    p = _write(tmp_path / "new.jpg", JPEG_HEAD)
    rid = _done(d, p, name="new.jpg")
    d.set_dominant_color(rid, "blue")
    assert cl.get(f"/library/searches/{sid}/items").json()["total"] == 1


def test_virtual_album_missing_is_404(client):
    cl, _ = client
    assert cl.get("/library/searches/9999/items").status_code == 404


# ==================================================================
# 4. 打包导出
# ==================================================================

def test_export_with_ids_and_manifest(tmp_db, tmp_path):
    p = _write(tmp_path / "x1.jpg", JPEG_HEAD)
    rid = _done(tmp_db, p, name="x1.jpg")
    res = exporter.export(ids=[rid], out_dir=str(tmp_path / "out"))
    assert res["count"] == 1
    with zipfile.ZipFile(res["path"]) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["count"] == 1
    assert manifest["items"][0]["name"] == "x1.jpg"


def test_export_records_missing_files(tmp_db, tmp_path):
    """盘上已不在的文件必须进 `skipped` —— 不记的话"导出数"和"库里有"对不上。"""
    tid = tmp_db.create_task("https://example.com/a", "generic")
    rid = tmp_db.add_resource(tid, "image", "https://example.com/gone.jpg")
    tmp_db.update_resource(rid, status="done",
                           local_path=str(tmp_path / "gone.jpg"))
    res = exporter.export(ids=[rid], out_dir=str(tmp_path / "out"))
    assert res["count"] == 0
    assert res["skipped"] and res["skipped"][0]["reason"] == "missing"


def test_export_refuses_to_truncate(tmp_db, tmp_path):
    """超限**报错**而不是只导前 N 个: 用户会以为导全了。"""
    ids = []
    for i in range(3):
        p = _write(tmp_path / f"t{i}.jpg", JPEG_HEAD)
        ids.append(_done(tmp_db, p, name=f"t{i}.jpg"))
    with pytest.raises(ValueError):
        exporter.export(ids=ids, out_dir=str(tmp_path / "out"), max_items=2)


def test_export_requires_album_or_ids(tmp_db, tmp_path):
    """两个都不给 = 打包整个库, 那不是用户点这一下时想要的。"""
    with pytest.raises(ValueError):
        exporter.export(out_dir=str(tmp_path / "out"))


def test_export_endpoint_rejects_empty_request(client):
    cl, _ = client
    assert cl.post("/library/export", json={}).status_code == 400


# ==================================================================
# 5. Webhook
# ==================================================================

def test_webhook_rejects_unknown_event(tmp_db):
    """未知事件代号直接拒: 存一个永远不会被命中的代号, 界面上看不出它配错了。"""
    with pytest.raises(ValueError):
        db.add_webhook("https://example.com/h", ["task.done", "no.such.event"])
    with pytest.raises(ValueError):
        db.add_webhook("ftp://example.com/h", ["task.done"])


def test_webhook_never_exposes_secret(tmp_db):
    """密钥不进返回值 —— 进了响应体就会被日志/插件顺手带走。"""
    hid = db.add_webhook("https://example.com/h", ["task.done"], secret="s3cret")
    row = db.get_webhook(hid)
    assert "secret" not in row
    assert row["has_secret"] is True


def test_webhook_records_delivery_failure(tmp_db):
    """投递失败必须留痕(第 G 条): 否则用户看到的是"配好了、没动静"。"""
    hid = db.add_webhook("http://127.0.0.1:1/hook", ["task.done"])
    ok, attempted = hooks.fire_event("task.done", {"id": 1})
    assert attempted == 1 and ok == 0
    row = db.get_webhook(hid)
    assert row["last_error"], "投递失败了但 last_error 是空的"


def test_webhook_signature_is_stable():
    """HMAC 签名: 同样的输入必须是同样的输出(接收方要能验)。"""
    sig1, ts1 = hooks.sign("key", b"{}", ts=1700000000)
    sig2, _ = hooks.sign("key", b"{}", ts=1700000000)
    assert sig1 == sig2 and sig1.startswith("sha256=")
    assert ts1 == "1700000000"
    assert hooks.sign("other", b"{}", ts=1700000000)[0] != sig1


def test_webhook_list_endpoint(client):
    cl, d = client
    r = cl.post("/webhooks", json={"url": "https://example.com/h",
                                   "events": ["task.done"], "secret": "k"})
    assert r.status_code == 200
    assert r.json()["has_secret"] is True
    r = cl.post("/webhooks", json={"url": "https://example.com/h",
                                   "events": ["nope"]})
    assert r.status_code == 400
    assert len(cl.get("/webhooks").json()) == 1


def test_webhook_test_endpoint_reports_result(client):
    cl, d = client
    hid = d.add_webhook("http://127.0.0.1:1/hook", ["task.done"])
    r = cl.post(f"/webhooks/{hid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["attempted"] == 1 and body["delivered"] == 0
    assert cl.get("/webhooks").json()[0]["last_error"]


# ==================================================================
# 6. 自检面板
# ==================================================================

def test_gates_report_checked_counts(client):
    """每道闸都要报"核了几项" —— 没有它, 空转的闸看起来是全绿的。"""
    cl, _ = client
    body = cl.get("/system/gates").json()
    assert body["gates"]
    assert all("checked" in g for g in body["gates"])
    assert body["events"], "webhook 事件代号清单应一起下发"


def test_gate_that_did_not_run_is_not_green(client):
    """没跑的闸必须**红**且写明原因 —— "没验到"≠"验过了没问题"。"""
    cl, _ = client
    body = cl.get("/system/gates").json()
    slow = [g for g in body["gates"] if g["name"] == "doc-counts"]
    assert slow, "doc-counts 这道闸应在列表里(即使不跑)"
    assert slow[0]["checked"] == 0
    assert slow[0]["ok"] is False
    assert any(p["kind"] == "not-run" for p in slow[0]["problems"])


# ==================================================================
# 7. filekind 的格式知识: 改名建议的边界
# ==================================================================

def test_suggest_ext_keeps_stem_and_skips_ambiguous(tmp_path):
    """只换扩展名不动主名; 认不准的族不给建议。"""
    p = _write(tmp_path / "0001.jpg", PNG_HEAD)
    assert filekind.suggest_rename(p) == "0001.png"
    assert filekind.suggest_ext(p) == "png"

    q = _write(tmp_path / "0002.jpg", FTYP_HEAD)
    assert filekind.suggest_ext(q) is None      # isobmff: 断定不了, 不给建议
    assert "isobmff" not in filekind.KIND_PRIMARY_EXT

    ok = _write(tmp_path / "0003.jpg", JPEG_HEAD)
    assert filekind.suggest_ext(ok) is None     # 相符: 没的可改
