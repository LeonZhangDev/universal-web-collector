"""V32 新增能力: 代理熔断、字节级进度回调契约、跨任务资源库、Pexels 采集器。

沿用 v2/v3/v4 的隔离套路: DB 指临时目录、stub 掉 task_manager.submit。
不需要网络的纯逻辑(代理熔断 / 字节回调)直接单测; 采集器只测**声明自检**
与**输入分类**, 不发起真实请求 —— 线上的站点结构会变, 把网络纳入断言只会
让测试变成"今天红明天绿"的噪声源(样本 URL 的自检已经能挡住绝大多数回归)。
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import core.task_manager as tm
import api.tasks as T  # noqa: E402
from downloaders.base import ProxyPool
from collectors.gallery_base import check_site, parse_gid, selfcheck_all
from collectors.stockphotos.pexels import PEXELS
from collectors.stockphotos.spider import PexelsSpider
from collectors import COLLECTORS, match_collectors, resolve_collector


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v32.db")
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


# ---- 代理熔断(失败换线) ----

def test_proxy_breaker_opens_after_threshold():
    pool = ProxyPool("http://a:1,http://b:2", fail_threshold=3, cooldown=60)
    # 未达阈值时照常轮换
    assert pool.pick(0) == "http://a:1"
    assert pool.pick(1) == "http://b:2"
    for _ in range(2):
        pool.note_failure("http://a:1")
    assert pool.pick(0) == "http://a:1"      # 2 次还不够
    pool.note_failure("http://a:1")          # 第 3 次 -> 熔断
    # 序号 0 本该是 a, 但 a 已熔断 -> 换到 b
    assert pool.pick(0) == "http://b:2"


def test_proxy_breaker_all_down_falls_back():
    """全都熔断时不该返回 None —— 那会让任务一个资源都下不了。

    ⚠️ 这条是刻意的设计取舍: 全池熔断更可能是"故障不在代理上"
    (比如目标站挂了), 此时仍按序号返回, 至少让重试链正常走完并报出真实错误。
    """
    pool = ProxyPool("http://a:1,http://b:2", fail_threshold=1, cooldown=60)
    pool.note_failure("http://a:1")
    pool.note_failure("http://b:2")
    assert pool.pick(0) == "http://a:1"
    assert pool.pick(1) == "http://b:2"


def test_proxy_breaker_success_resets_counter():
    pool = ProxyPool("http://a:1,http://b:2", fail_threshold=3, cooldown=60)
    pool.note_failure("http://a:1")
    pool.note_failure("http://a:1")
    pool.note_success("http://a:1")          # 中间成功一次 -> 清零
    pool.note_failure("http://a:1")
    assert pool.pick(0) == "http://a:1"      # 不该被熔断


def test_proxy_breaker_cooldown_expires():
    """冷却到点后应自动半开放出, 不需要任何后台线程。"""
    pool = ProxyPool("http://a:1,http://b:2", fail_threshold=1, cooldown=0.01)
    pool.note_failure("http://a:1")
    assert pool.pick(0) == "http://b:2"      # 熔断中
    import time as _t
    _t.sleep(0.05)
    assert pool.pick(0) == "http://a:1"      # 冷却结束, 放出来再试


def test_proxy_snapshot_shape():
    pool = ProxyPool("http://a:1,http://b:2", fail_threshold=2, cooldown=60)
    pool.note_failure("http://a:1")
    snap = pool.snapshot()
    assert len(snap) == 2
    a = [s for s in snap if s["proxy"] == "http://a:1"][0]
    assert a["fails"] == 1
    assert a["blocked"] is False
    assert isinstance(a["blocked_for"], int)
    # 空池的快照是空列表, 而不是 [{}]
    assert ProxyPool("").snapshot() == []


def test_proxy_endpoint_reports_configured(client):
    app, database = client
    r = app.post(
        "/tasks/create",
        json={"url": "https://x.com/a", "collector": "generic",
              "proxy": "http://u:p@a:1,http://u:p@b:2"},
    ).json()
    tid = r["task_id"]
    got = app.get(f"/tasks/{tid}/proxy").json()
    assert got["configured"] == 2
    # 凭据必须脱敏 —— 诊断面板会显示这段文本, 不能被截图外传
    assert "u:p" not in got["masked_spec"]
    assert "a:1" in got["masked_spec"]
    # 任务没在跑: lines 为空但 configured 仍要给出(否则前端以为没配)
    assert got["lines"] == []
    assert got["running"] is False


def test_proxy_endpoint_404_for_missing(client):
    app, _ = client
    assert app.get("/tasks/99999/proxy").status_code == 404


def test_mask_proxy_handles_junk():
    """脱敏函数不能因为畸形输入抛错 —— 它在诊断端点的关键路径上。"""
    assert T._mask_proxy("") == ""
    assert T._mask_proxy("not-a-url") == "***"
    assert T._mask_proxy("http://user:pass@host:8080") == "http://host:8080"


# ---- 字节级进度回调契约 ----

def test_progress_cb_receives_byte_count(monkeypatch, tmp_path):
    """下载层必须把**本 chunk 的字节数**传给 progress_cb。

    无参回调是历史契约, 也要能跑 —— 传参会 TypeError, 而它发生在下载循环里,
    会被上层当成"这个资源下载失败"。这条测试同时钉住两种签名。
    """
    import downloaders.base as B

    seen = []

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg", "Content-Length": "6"}

        def iter_content(self, n):
            yield b"abc"
            yield b"def"

        def raise_for_status(self):
            pass

        def close(self):
            pass

    class FakeSess:
        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(B, "_timeout", lambda: 5)
    import contextlib

    # 替身签名必须跟真实 domain_slot(url, log=None, progress_cb=None) 对齐：
    # 合并后下载层会传 progress_cb（让限速等待期间也能刷新进度/响应取消），
    # 替身少一个参数会直接 TypeError，而它发生在下载循环里会被当成下载失败。
    @contextlib.contextmanager
    def _slot(url, log=None, progress_cb=None):
        yield

    monkeypatch.setattr(B, "domain_slot", _slot)
    monkeypatch.setattr(B, "note_success", lambda url: None)

    path = tmp_path / "x.jpg"
    B._stream_one("https://x.com/x.jpg", path, {}, 1, False, FakeSess(),
                  lambda n: seen.append(n))
    assert seen == [3, 3]            # 两个 chunk, 各 3 字节


def test_progress_cb_noarg_still_works(monkeypatch, tmp_path):
    """兼容无参回调: 老实现/测试替身写成 `def cb(): ...` 不能被打挂。"""
    import downloaders.base as B
    import contextlib

    calls = []

    class FakeResp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg", "Content-Length": "3"}

        def iter_content(self, n):
            yield b"abc"

        def raise_for_status(self):
            pass

        def close(self):
            pass

    class FakeSess:
        def get(self, *a, **k):
            return FakeResp()

    # 同 test_progress_cb_receives_byte_count：替身要对齐真实签名
    # domain_slot(url, log=None, progress_cb=None)。
    @contextlib.contextmanager
    def _slot(url, log=None, progress_cb=None):
        yield

    monkeypatch.setattr(B, "domain_slot", _slot)
    monkeypatch.setattr(B, "note_success", lambda url: None)

    B._stream_one("https://x.com/x.jpg", tmp_path / "y.jpg", {}, 1, False,
                  FakeSess(), lambda: calls.append(1))
    assert calls == [1]


def test_task_bytes_event_published():
    """_publish_bytes 走事件总线(不落库), 且形状可供前端直接换算速率。"""
    import core.events as events

    q = events.subscribe()
    try:
        tm.TaskManager.__new__(tm.TaskManager)._publish_bytes(7, 12345)
        got = []
        while not q.empty():
            got.append(q.get_nowait())
    finally:
        events.unsubscribe(q)
    names = [n for n, _ in got]
    assert "task.bytes" in names
    ev = [d for n, d in got if n == "task.bytes"][0]
    assert ev["task_id"] == 7
    assert ev["bytes"] == 12345
    assert isinstance(ev["ts"], float)


# ---- 跨任务资源库 ----

def test_library_lists_done_resources_across_tasks(client):
    app, database = client
    t1 = _make(app, 1)[0]
    t2 = _make(app, 1, prefix="https://other.com/x")[0]
    database.add_resource(t1, "image", "https://x.com/a.jpg", status="done", size=100)
    database.add_resource(t2, "image", "https://x.com/b.jpg", status="done", size=200)
    # pending 的不该出现: 资源库是"手上有什么", 不是"所有见过的 URL"
    database.add_resource(t2, "image", "https://x.com/c.jpg", status="pending")
    r = app.get("/library").json()
    assert r["total"] == 2
    assert r["stats"]["resources"] == 2
    assert r["stats"]["bytes"] == 300


def test_library_kind_filter(client):
    app, database = client
    tid = _make(app, 1)[0]
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="done")
    database.add_resource(tid, "video", "https://x.com/b.mp4", status="done")
    assert app.get("/library", params={"kind": "video"}).json()["total"] == 1
    assert app.get("/library", params={"kind": "image"}).json()["total"] == 1
    # all 不过滤
    assert app.get("/library", params={"kind": "all"}).json()["total"] == 2


def test_library_search_escapes_wildcards(client):
    """搜 `100%` / `100_` 里的通配符必须当字面量。

    ⚠️ 不转义时 SQL LIKE 会把 `%` 当"任意串"、`_` 当"任意单字符":
    搜 `100%` 会把**所有**含 "100" 的都捞出来(这里 3 条全中), 搜 `100_`
    会连 `100x_a` 一起捞。用户看到的只是"怎么多出来几条", 不会想到是
    自己输入的字符被当成通配符吃掉了 —— 而 `_` 在文件名里极常见。
    """
    app, database = client
    tid = _make(app, 1)[0]
    # 三条数据分别用来验 % 与 _ 的转义:
    #   a 含字面 "100%" -> 只有搜 `100%` 才该命中
    #   b 含字面 "100_x" -> 只有搜 `100_` 才该命中(未转义时 a 也会被 `100_` 带出来)
    #   c 既不含 % 也不含 _
    database.add_resource(tid, "image", "https://x.com/100%_a.jpg", status="done")
    database.add_resource(tid, "image", "https://x.com/100_x.jpg", status="done")
    database.add_resource(tid, "image", "https://x.com/other.jpg", status="done")
    # 未转义: `100%` -> 3 条(a 的 % 变通配), `100_` -> 3 条(a/b 都被 _ 命中)
    # 已转义: 各自只命中字面含该串的那条
    assert app.get("/library", params={"q": "100%"}).json()["total"] == 1
    assert app.get("/library", params={"q": "100_"}).json()["total"] == 1
    # 反向确认通配没被误伤: 搜纯前缀仍然命中两条
    assert app.get("/library", params={"q": "100"}).json()["total"] == 2


def test_library_album_filter_exact(client):
    """相册名精确匹配: `ABP-123` 不该把 `ABP-1234` 也捞出来。"""
    app, database = client
    # 相册名由采集阶段写入 tasks.name, 这里直接建任务后改名(采集不在测试范围内)
    a = _make(app, 1)[0]
    b = _make(app, 1, prefix="https://other.com/x")[0]
    database.update_task(a, name="ABP-123")
    database.update_task(b, name="ABP-1234")
    database.add_resource(a, "image", "https://x.com/a.jpg", status="done")
    database.add_resource(b, "image", "https://x.com/b.jpg", status="done")
    r = app.get("/library", params={"album": "ABP-123"}).json()
    assert r["total"] == 1
    alb = app.get("/library/albums").json()["items"]
    assert {x["album"] for x in alb} == {"ABP-123", "ABP-1234"}


def test_library_refs_counts_shared_files(client):
    """同一路径被两个任务引用时 refs=2 —— 这是删除语义的依据。

    sha256 去重只复用路径、不复制文件, 所以"删任务连文件一起删"必须看这个数:
    删到 refs==1 那份才真删文件, 否则会把先到的任务也掏空。
    """
    app, database = client
    t1 = _make(app, 1)[0]
    t2 = _make(app, 1, prefix="https://other.com/x")[0]
    shared = "/dl/album/shared.jpg"
    database.add_resource(t1, "image", "https://x.com/a.jpg",
                          status="done", local_path=shared)
    database.add_resource(t2, "image", "https://x.com/a.jpg",
                          status="done", local_path=shared)
    r = app.get("/library").json()
    assert r["total"] == 2
    assert all(x["refs"] == 2 for x in r["items"])
    assert database.resource_refs(shared) == 2


def test_library_pagination_consistent(client):
    """总数与页内容必须同一口径, 否则最后一页数量对不上。"""
    app, database = client
    tid = _make(app, 1)[0]
    for i in range(7):
        database.add_resource(tid, "image", f"https://x.com/{i}.jpg", status="done")
    p1 = app.get("/library", params={"page": 1, "page_size": 3}).json()
    assert p1["total"] == 7
    assert p1["pages"] == 3
    assert len(p1["items"]) == 3
    p3 = app.get("/library", params={"page": 3, "page_size": 3}).json()
    assert len(p3["items"]) == 1     # 7 = 3+3+1


def test_library_stats_by_kind(client):
    app, database = client
    tid = _make(app, 1)[0]
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="done", size=10)
    database.add_resource(tid, "video", "https://x.com/b.mp4", status="done", size=90)
    kinds = {k["type"]: k for k in app.get("/library").json()["stats"]["by_kind"]}
    assert kinds["image"]["n"] == 1
    assert kinds["video"]["bytes"] == 90


# ---- Pexels 采集器 ----

def test_pexels_site_declaration_selfcheck():
    """声明自检必须全绿: 样本 URL 都要解析出同一个 gid。"""
    assert check_site(PEXELS) == []


def test_all_registered_sites_pass_selfcheck():
    """所有已注册站点的声明自检 —— 新站写错正则会在这里当场变红。"""
    assert selfcheck_all() == {}


def test_pexels_gid_samples():
    for raw, want in PEXELS.id_samples:
        assert parse_gid(PEXELS, raw, strict=True) == want


def test_pexels_registered_and_selectable():
    assert "pexels" in COLLECTORS
    # 图片直链 -> 认领
    got = resolve_collector(
        "https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg"
    )
    assert got["collector"] == "pexels"
    assert got["auto"] is True
    # 搜索页 -> 也认领(分数更高)
    got2 = resolve_collector("https://www.pexels.com/search/cat/")
    assert got2["collector"] == "pexels"


def test_pexels_match_score_none_for_other_sites():
    """不认领无关站点 —— 认领了就会去猜一个不存在的 id(静默失败)。"""
    assert PexelsSpider.match_score("https://xchina.co/photo/id-6aa5136f606fe.html") is None
    assert PexelsSpider.match_score("not a url") is None
    assert PexelsSpider.match_score("") is None


def test_pexels_does_not_steal_xchina():
    """接入第二个站点后, 原站点的认领不能被抢走。"""
    got = resolve_collector("https://xchina.co/photo/id-6aa5136f606fe.html")
    assert got["collector"] == "xchina_gallery"


def test_pexels_list_requires_api_key(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    spider = PexelsSpider()
    # 缺 key 时必须**明确报错并说清怎么配**, 不能静默返回空列表
    with pytest.raises(ValueError) as ei:
        spider.crawl("https://www.pexels.com/search/cat/")
    msg = str(ei.value)
    assert "PEXELS_API_KEY" in msg
    assert "api" in msg.lower()


def test_pexels_single_rejects_bad_id(monkeypatch):
    spider = PexelsSpider()
    with pytest.raises(ValueError):
        spider.crawl("https://www.pexels.com/photo/no-digits-here/")


def test_pexels_collections_unsupported(monkeypatch):
    """集合页明确报"暂不支持", 而不是把它当成搜索词搜出一个无关结果。"""
    monkeypatch.setenv("PEXELS_API_KEY", "dummy")
    spider = PexelsSpider()
    with pytest.raises(ValueError) as ei:
        spider.crawl("https://www.pexels.com/collections/nature-abc123/")
    assert "集合" in str(ei.value)


def test_pexels_single_detects_missing(monkeypatch, tmp_path):
    """探测判定为不存在时明确报错(而不是产出 1 条注定 404 的资源)。"""
    import collectors.stockphotos.spider as S

    monkeypatch.setattr(S, "probe", lambda *a, **k: "missing")
    with pytest.raises(ValueError) as ei:
        S.PexelsSpider().crawl("https://www.pexels.com/photo/a-cat-1234567/")
    assert "1234567" in str(ei.value)


def test_pexels_single_builds_resource(monkeypatch):
    import collectors.stockphotos.spider as S

    monkeypatch.setattr(S, "probe", lambda *a, **k: "ok")
    res = S.PexelsSpider().crawl("https://images.pexels.com/photos/7654321/pexels-photo-7654321.jpeg")
    assert len(res) == 1
    r = res[0]
    assert r["type"] == "image"
    assert "7654321" in r["url"]
    assert r["mirrors"]                       # 尺寸档作为备用下载点
    assert r["filename"] == "7654321.jpg"


# ---- 环境诊断新增项 ----

def test_env_diagnose_has_proxy_item(client):
    app, _ = client
    r = app.get("/env/diagnose").json()
    keys = {i["key"] for i in r["items"]}
    assert "proxy" in keys
    proxy = [i for i in r["items"] if i["key"] == "proxy"][0]
    # 代理没配不该是 fail —— 直连是正常状态
    assert proxy["level"] == "ok"
    assert proxy["detail"] == "" or "***" in proxy["detail"] or "://" in proxy["detail"]
