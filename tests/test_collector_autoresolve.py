"""采集器自动识别测试(纯 CPU, 不触网)。

为什么值得单独测
================
"用户忘了手选采集器"是这个工具最容易出错的一步: 选错了**不像报错那样显眼**。
相册页 URL 落到 generic 上会得到一张 Cloudflare 挑战页, 然后采集到 0 个资源,
界面上什么异常都看不出来。

所以这里要钉死三件事:

1. **认领必须有凭据** —— 只凭域名不够, 必须 `parse_gid` 成功。
   否则 `img.xchina.io/gallery/abc` 会被认领, 退路把 `abc` 当图集 ID
   去枚举, 结果又是"任务成功但 0 资源"。
2. **通用采集器永远垫底** —— 它什么 URL 都能"试着抓", 一旦它的分数
   压过专用采集器, 整个自动识别就失效了, 而且失效得很安静。
3. **非 URL 输入必须报错, 不能悄悄兜底** —— 请求 `6aa5136f606fe`
   只会在 DNS 层失败; 明确告诉用户"无法识别, 请手选"要有用得多。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api.tasks as T
from collectors import COLLECTORS, resolve_collector
from collectors.gallery_base import GallerySite, SequenceGallerySpider
from collectors.scores import SCORE_ALBUM_PAGE, SCORE_BARE_ID, SCORE_GENERIC, SCORE_RESOURCE_URL

GID = "6a3654854fd25"
ALBUM_PAGE = f"https://xchina.co/photo/id-{GID}.html"
DIRECT_JPG = f"https://img.xchina.io/photos/{GID}/00001.jpg"
DIRECT_MP4 = f"https://img.xchina.io/photos/{GID}/00001.mp4"


# ---- 分数阶梯本身 ----


def test_generic_is_below_every_specialist_score():
    """通用采集器必须垫底, 否则专用采集器永远选不上。"""
    for s in (SCORE_BARE_ID, SCORE_RESOURCE_URL, SCORE_ALBUM_PAGE):
        assert s > SCORE_GENERIC


def test_scores_ordered_by_how_much_the_url_form_commits():
    """URL 形态越坐实, 分数越高(相册页 > 资源直链 > 纯 ID)。"""
    assert SCORE_ALBUM_PAGE > SCORE_RESOURCE_URL > SCORE_BARE_ID


# ---- 各种输入形态的归属 ----


@pytest.mark.parametrize("url", [ALBUM_PAGE, f"https://xchina.co/photo/id-{GID}/10.html",
                                 "https://xchina.co/photoShow.html?id=6aa113208a506"])
def test_album_page_urls_go_to_gallery(url):
    got = resolve_collector(url)
    assert got["collector"] == "xchina_gallery"
    assert got["score"] == SCORE_ALBUM_PAGE
    assert got["auto"] is True


@pytest.mark.parametrize("url", [DIRECT_JPG, DIRECT_MP4])
def test_resource_direct_links_go_to_gallery(url):
    got = resolve_collector(url)
    assert got["collector"] == "xchina_gallery"
    assert got["score"] == SCORE_RESOURCE_URL


def test_bare_album_id_goes_to_gallery():
    """纯 ID 也要能落位 —— 用户手边最常见的就是一串 ID。"""
    got = resolve_collector(GID)
    assert got["collector"] == "xchina_gallery"
    assert got["score"] == SCORE_BARE_ID


def test_unknown_http_url_falls_back_to_generic():
    got = resolve_collector("https://example.com/a/b")
    assert got["collector"] == "generic"
    assert got["auto"] is False, "通用采集器兜底不该冒充'已识别'"
    assert got["score"] == SCORE_GENERIC


@pytest.mark.parametrize("url", ["", "not a url", "abc", "/tmp/no-scheme"])
def test_non_url_input_is_not_silently_handed_to_generic(url):
    """连 URL 都不是的输入: 拒绝兜底, 让调用方明确报错(而不是 DNS 失败)。"""
    got = resolve_collector(url, fallback=None)
    assert got["collector"] is None
    assert got["reason"], "reason 要能原样显示给用户"


def test_reason_is_human_readable_for_every_branch():
    cases = [ALBUM_PAGE, DIRECT_JPG, GID, "https://example.com/x", "not a url"]
    for url in cases:
        got = resolve_collector(url, fallback=None)
        assert got["reason"] and isinstance(got["reason"], str)
        assert not got["reason"].startswith("{"), "reason 是给用户看的句子, 不是 JSON"


# ---- 认领必须有凭据(防止又出现"成功但 0 资源") ----


@pytest.mark.parametrize("url", [
    "https://xchina.co/photo/10.html",      # 末段是页码, 不是 ID
    "https://xchina.co/",                   # 什么都没带
    "https://xchina.co/tag/some-tag",       # 同域的普通列表页
])
def test_same_host_url_without_extractable_gid_is_not_claimed(url):
    """只凭域名认领会把同域任意路径都揽过来, 然后猜一个 ID 去枚举。"""
    from collectors.xchina.gallery import XChinaGallerySpider

    assert XChinaGallerySpider.match_score(url) is None


def test_host_alone_is_never_enough():
    """反 Face: 换个同域名但路径无关的最小站点, 也必须不认领。"""
    site = GallerySite(name="t", base="https://cdn.t.example/photos",
                       variants=[".jpg"], album_url_template="https://t.example/p/{gid}")
    spider = type("S", (SequenceGallerySpider,), {"site": site})
    assert spider.match_score("https://t.example/anything-else") is None


def test_too_short_id_segment_is_rejected():
    """ID 至少 6 位: 太短的片段是路径词, 不是图集 ID。"""
    site = GallerySite(name="t", base="https://cdn.t.example/photos", variants=[".jpg"])
    spider = type("S", (SequenceGallerySpider,), {"site": site})
    assert spider.match_score("https://cdn.t.example/photos/abc/00001.jpg") is None


# ---- 选择链路的健壮性 ----


def test_broken_match_score_does_not_break_resolution(monkeypatch):
    """某个采集器写错了 match_score, 不该拖垮整条选择链路 —— 匹配是纯 CPU。"""
    cls = COLLECTORS["generic"]
    monkeypatch.setattr(
        cls, "match_score",
        classmethod(lambda c, url: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    got = resolve_collector(ALBUM_PAGE)
    assert got["collector"] == "xchina_gallery"


def test_same_url_always_resolves_the_same_way():
    """可复现比'更聪明'重要: 同一输入今天 A 明天 B, 出了问题没法复盘。"""
    first = resolve_collector(DIRECT_JPG)["collector"]
    for _ in range(5):
        assert resolve_collector(DIRECT_JPG)["collector"] == first


# ---- API 层: auto 必须解析后入库存真名 ----


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离 DB + 不真的跑任务的 API 客户端。

    为什么要 stub `submit`: 自动识别只影响"建任务之前", 真跑起来会去
    抓真实站点, 与本文件要钉的东西无关, 而且会让测试时长不可控。
    """
    import core.database as db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "resolve.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    return TestClient(_make_app()), db


def _make_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(T.router)
    return app


def test_resolve_endpoint_returns_same_answer_as_create(client):
    """界面**必须**能提前问到用完认真落实,'悄悄生效'的自动识别不可接受。"""
    app, _ = client
    got = app.get("/collectors/resolve", params={"url": ALBUM_PAGE}).json()
    assert got["collector"] == "xchina_gallery"

    created = app.post("/tasks/create", json={"url": ALBUM_PAGE}).json()
    row = app.get(f"/tasks/{created['task_id']}").json()
    assert row["collector"] == "xchina_gallery"
    assert created["resolved"]["collector"] == got["collector"]


def test_create_stores_resolved_name_not_auto(client):
    """库里存 'auto' 的话, 重放/巡检时同一个 auto 可能指向不同采集器。"""
    app, _ = client
    created = app.post("/tasks/create", json={"url": DIRECT_JPG}).json()
    row = app.get(f"/tasks/{created['task_id']}").json()
    assert row["collector"] == "xchina_gallery"


def test_create_rejects_unresolvable_input(client):
    app, _ = client
    r = app.post("/tasks/create", json={"url": "not a url"})
    assert r.status_code == 400
    assert "手动" in r.json()["detail"], "要告诉用户下一步该怎么办"


def test_explicit_collector_still_wins(client):
    """自动识别是兜底不是强制: 用户手选必须能压过它。"""
    app, _ = client
    created = app.post("/tasks/create",
                       json={"url": ALBUM_PAGE, "collector": "generic"}).json()
    row = app.get(f"/tasks/{created['task_id']}").json()
    assert row["collector"] == "generic"
    assert created["resolved"] is None, "没走自动识别就不该回显识别结论"


def test_unknown_explicit_collector_is_400(client):
    app, _ = client
    r = app.post("/tasks/create", json={"url": ALBUM_PAGE, "collector": "nope"})
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


def test_preview_accepts_auto(client, monkeypatch):
    """预览与创建共用同一套识别, 否则会出现'预览通过、创建被拒'。"""
    app, _ = client
    import collectors.gallery_base as G

    monkeypatch.setattr(G.SequenceGallerySpider, "preview",
                        lambda self, url, options=None, log=None, max_items=12:
                        {"gid": GID, "group": "t", "photos": 1, "videos": 0,
                         "sampled": False})
    r = app.post("/tasks/preview", json={"url": ALBUM_PAGE})
    assert r.status_code == 200
    assert r.json()["resolved"]["collector"] == "xchina_gallery"


def test_pick_collector_treats_blank_as_auto():
    """前端可能传空字符串; 空值等价于 auto, 而不是'未知采集器'。"""
    assert T._pick_collector(ALBUM_PAGE, "")[0] == "xchina_gallery"
    assert T._pick_collector(ALBUM_PAGE, None)[0] == "xchina_gallery"
    with pytest.raises(HTTPException):
        T._pick_collector("not a url", "")


# ---- 聚合页: 选项必须"创建 / 预览 / 订阅"三处一致 ----

AGG = "https://xchina.co/model/id-601190f157fe7.html"


def test_aggregate_url_is_claimed_by_auto(client):
    app, _ = client
    got = app.get("/collectors/resolve", params={"url": AGG}).json()
    assert got["collector"] == "xchina_aggregate"
    assert got["ambiguous"] is False


def test_aggregate_depth_out_of_range_is_400(client):
    """越界值必须报错而不是被悄悄夹到边界 —— 夹过的值用户以为自己设对了。"""
    app, _ = client
    for bad in (0, -1, 99):
        r = app.post("/tasks/create", json={"url": AGG, "aggregate_depth": bad})
        assert r.status_code == 400, bad
        assert "aggregate_depth" in r.json()["detail"]


def test_aggregate_options_reach_task_options(client):
    """`aggregate_depth`/`max_items` 要真落进 options, 否则采集器拿不到。"""
    app, db = client
    created = app.post(
        "/tasks/create",
        json={"url": AGG, "aggregate_depth": 2, "max_items": 30},
    ).json()
    import json as _json

    row = db.get_task(created["task_id"])
    opts = _json.loads(row["options"] or "{}")
    assert opts["aggregate_depth"] == 2
    assert opts["max_items"] == 30


def test_watch_shares_the_same_option_builder(client, monkeypatch):
    """订阅原先手抄了一份校验, 于是新选项会被静默丢掉。

    订阅是长期反复跑的, 悄悄少一个选项比直接报错难查得多 —— 现在三处
    共用 `_gallery_options`。
    """
    app, db = client
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    got = app.post(
        "/watches",
        json={
            "url": AGG,
            "interval_minutes": 60,
            "aggregate_depth": 3,
            "max_items": 7,
            "album_title": "h1",
            "quality": "800",
            "run_now": False,
        },
    )
    assert got.status_code == 200, got.text
    import json as _json

    row = db.get_watch(got.json()["id"])
    opts = _json.loads(row["options"] or "{}")
    assert opts["aggregate_depth"] == 3
    assert opts["max_items"] == 7
    # 走的是同一个构造器: 采集器侧的选项一个都不能少
    assert opts["album_title"] == "h1"
    assert opts["quality"] == "800"


def test_watch_rejects_bad_aggregate_depth(client):
    """订阅与创建共用构造器, 校验也必须一致(否则'创建被拒、订阅静默通过')。"""
    app, _ = client
    r = app.post("/watches", json={"url": AGG, "aggregate_depth": 0,
                                  "interval_minutes": 60})
    assert r.status_code == 400
    assert "aggregate_depth" in r.json()["detail"]


def test_config_exposes_aggregate_limits(client):
    """前端要拿它给输入框设上限, 硬编码在两端必然漂移。"""
    app, _ = client
    cfg = app.get("/config").json()
    assert cfg["aggregate_max_depth"] >= 1
    assert cfg["aggregate_max_items"] >= 1
