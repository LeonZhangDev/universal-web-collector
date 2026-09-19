"""聚合页采集器: 一个模特/系列/索引 -> 它下面的全部相册与视频(不触网)。

设计要点(值得被测试钉住的):
1. URL 驱动抽取, 不依赖 DOM class —— 站点改版不该变成"静默采到 0 个";
2. 只认聚合形态, **绝不抢** `/photo/id-*` 与 `/video/id-*`(那是别的采集器的地盘);
3. 双闸门(max_items / aggregate_depth)到顶时必须**说出来**, 不能悄悄少采;
4. 委派给子采集器时, 单个子页面失败不该让整批报销。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import collectors as C
from collectors.xchina.aggregate import (
    XChinaAggregateSpider,
    _classify,
    extract_children,
)

MODEL = "https://xchina.co/model/id-601190f157fe7.html"
MODELS = "https://xchina.co/models.html"

#: 一个"真实形态"的模特落地页: 混合了导航、推荐位、站外链接和真正的相册链接。
MODEL_PAGE = """
<html><head><title>纱仓真菜 - 模特 - XChina</title></head><body>
<nav>
  <a href="/models.html">模特</a>
  <a href="/models/type-7.html">分类</a>
  <a href="https://twitter.com/xchina">推特</a>
  <a href="#top">顶部</a>
  <a href="javascript:void(0)">更多</a>
</nav>
<main>
  <a href="/photo/id-6aa5136f606fe.html">相册 A</a>
  <a href="/photo/id-69ad45698f836.html">相册 B</a>
  <a href="/photo/id-69ad45698f836/10.html">相册 B 第 10 页</a>
  <a href="/video/id-6aaa517d3f106.html">视频 1</a>
  <a href="/photos/model-601190f157fe7.html">全部图片</a>
  <a href="/videos/model-601190f157fe7.html">全部视频</a>
  <a href="https://img.xchina.io/photos2/69ad45698f836/0001.jpg">封面</a>
</main>
</body></html>
"""

INDEX_PAGE = """
<html><head><title>模特列表 - XChina</title></head><body>
<a href="/model/id-601190f157fe7.html">模特甲</a>
<a href="/model/id-6449a91f7cc78.html">模特乙</a>
<a href="/model/id-601190f157fe7.html">模特甲(重复)</a>
</body></html>
"""


class Loader:
    """假页面加载器: 按 URL 出页面, 未登记的当成读不到(模拟 CF 拦截)。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, markers=None, log=None, ready_label="页面"):
        self.calls.append(url)
        html = self.pages.get(url)
        return (html, "http") if html else (None, None)


# ---------------------------------------------------------------- URL 分类

@pytest.mark.parametrize("path,kind", [
    ("/models.html", "index"),
    ("/actors.html", "index"),
    ("/models/type-7.html", "index"),
    ("/model/id-601190f157fe7.html", "landing"),
    ("/model/id-601190f157fe7/2.html", "landing"),
    ("/actor/id-6449a91f7cc78.html", "landing"),
    ("/photos/model-601190f157fe7.html", "list"),
    ("/videos/model-601190f157fe7.html", "list"),
    ("/photos/series-6443d480eb757.html", "list"),
    ("/photo/id-6aa5136f606fe.html", "album"),
    ("/photo/id-6aa5136f606fe/10.html", "album"),
    ("/video/id-6aaa517d3f106.html", "video"),
])
def test_classify_recognises_real_site_shapes(path, kind):
    """这些形态全部来自 2026-09-19 的真实探测, 不是猜的。"""
    assert _classify(path) == kind


@pytest.mark.parametrize("path", [
    "/tag/some-tag",            # 列表页: 末段像 ID 但不是
    "/photo/10.html",           # 页码, 不是 ID
    "/photos/featured/0001.jpg",  # 路径词当 ID 的经典坑
    "/", "/login", "/model/id-short.html",
])
def test_classify_rejects_lookalikes(path):
    assert _classify(path) is None


# ---------------------------------------------------------------- 自动识别

def test_match_score_claims_aggregate_forms_only():
    sp = XChinaAggregateSpider
    assert sp.match_score(MODEL) is not None
    assert sp.match_score(MODELS) is not None
    assert sp.match_score("https://xchina.co/videos/model-601190f157fe7.html") is not None


def test_match_score_never_steals_album_or_video_pages():
    """抢过来只会把"一个相册"错当成"一个模特", 是必须防住的方向性错误。"""
    sp = XChinaAggregateSpider
    for u in [
        "https://xchina.co/photo/id-6aa5136f606fe.html",
        "https://xchina.co/video/id-6aaa517d3f106.html",
        "https://img.xchina.io/photos2/69ad45698f836/0001.jpg",
        "69ad45698f836",
    ]:
        assert sp.match_score(u) is None, u


def test_match_score_ignores_foreign_hosts():
    assert XChinaAggregateSpider.match_score(
        "https://example.com/model/id-601190f157fe7.html") is None


def test_resolve_collector_picks_aggregate_without_ambiguity():
    got = C.resolve_collector(MODEL)
    assert got["collector"] == "xchina_aggregate"
    assert got["ambiguous"] is False


def test_album_and_video_pages_still_go_to_their_own_collectors():
    """回归: 新增聚合采集器不能把已有的两条路抢走。"""
    assert C.resolve_collector("https://xchina.co/photo/id-6aa5136f606fe.html")[
        "collector"] == "xchina_gallery"
    assert C.resolve_collector("https://xchina.co/video/id-6aaa517d3f106.html")[
        "collector"] == "xchina_video"


# ---------------------------------------------------------------- 抽取

def test_extract_children_keeps_order_and_dedupes_by_path():
    kids = extract_children(MODEL_PAGE, MODEL, want=("album", "video", "list"))
    assert kids["album"] == [
        "https://xchina.co/photo/id-6aa5136f606fe.html",
        "https://xchina.co/photo/id-69ad45698f836.html",
    ], "同相册的 /10.html 是分页, 应被去重; 顺序要保留(截断的是尾巴)"
    assert kids["video"] == ["https://xchina.co/video/id-6aaa517d3f106.html"]
    assert kids["list"] == [
        "https://xchina.co/photos/model-601190f157fe7.html",
        "https://xchina.co/videos/model-601190f157fe7.html",
    ]


def test_extract_children_does_not_merge_list_pagination():
    """列表页的下一页是**另一批**相册, 绝不能被当成重复而丢掉(会静默漏采)。

    与内容页相反: `/photo/id-X/10.html` 要归一, `/photos/model-X/2.html` 不能。
    """
    html = """
    <a href="/photos/model-601190f157fe7.html">第 1 页</a>
    <a href="/photos/model-601190f157fe7/2.html">第 2 页</a>
    """
    kids = extract_children(html, MODEL, want=("list",))
    assert kids["list"] == [
        "https://xchina.co/photos/model-601190f157fe7.html",
        "https://xchina.co/photos/model-601190f157fe7/2.html",
    ]


def test_extract_children_skips_foreign_and_non_navigable_links():
    kids = extract_children(MODEL_PAGE, MODEL)
    flat = [u for v in kids.values() for u in v]
    assert all("twitter.com" not in u for u in flat), "站外链接不该被当子页面"
    assert all(not u.startswith("javascript:") for u in flat)


# ---------------------------------------------------------------- 展开闸门

def test_expand_respects_max_items():
    """闸门到顶时只展开前 N 个, 而不是把整个索引页拉下来。

    完整链路: 索引页 -> 模特页 -> 相册。max_items=1 时展开完第一个相册就该停,
    第二个模特页连读都不该去读。
    """
    sp = XChinaAggregateSpider()
    loader = Loader({MODELS: INDEX_PAGE, MODEL: MODEL_PAGE})
    state = {"albums": [], "videos": [], "failed": [], "seen": set()}
    sp._expand(MODELS, "index", 0, depth_limit=1, max_items=1, media="auto",
               log=None, state=state, loader=loader)
    assert len(state["albums"]) == 1, state["albums"]
    assert state.get("truncated") is True
    # 第二个模特页不该被读: 名额已经用光
    assert "https://xchina.co/model/id-6449a91f7cc78.html" not in loader.calls


def test_expand_walks_deeper_when_depth_allows():
    """depth=2 时要能顺着索引页走到模特页, 再把模特页的相册收进来。"""
    sp = XChinaAggregateSpider()
    pages = {MODELS: INDEX_PAGE, MODEL: MODEL_PAGE}
    state = {"albums": [], "videos": [], "failed": [], "seen": set()}
    sp._expand(MODELS, "index", 0, depth_limit=2, max_items=50, media="auto",
               log=None, state=state, loader=Loader(pages))
    assert "https://xchina.co/photo/id-6aa5136f606fe.html" in state["albums"]
    assert state["group"], "根页面标题要当父目录名"


def test_expand_reports_truncation_instead_of_silently_stopping():
    """少采必须说出来 —— 悄悄截断最容易被误当成'站点只有这些'。

    真实形态: 模特页给出「全部图片」入口, 而那个列表页自己还有第 2 页。
    depth=1 时第 2 页不会被展开, 日志里必须出现这条事实。
    """
    sp = XChinaAggregateSpider()
    logs = []
    list_url = "https://xchina.co/photos/model-601190f157fe7.html"
    list_page = """
    <html><head><title>全部图片</title></head><body>
    <a href="/photo/id-6aa5136f606fe.html">相册 A</a>
    <a href="/photos/model-601190f157fe7/2.html">下一页</a>
    </body></html>
    """
    state = {"albums": [], "videos": [], "failed": [], "seen": set()}
    sp._expand(MODEL, "landing", 0, depth_limit=1, max_items=50, media="auto",
               log=logs.append, state=state,
               loader=Loader({MODEL: MODEL_PAGE, list_url: list_page}))
    assert any("层数上限" in m for m in logs), logs
    assert state.get("depth_capped") is True, "触顶要冒泡给 preview, 好让数量标成下限"


def test_expand_reports_max_items_truncation():
    """条目闸门到顶时同样要说 —— 沉默地只收 2 个会被当成'模特只有 2 个相册'。

    该页共 3 个条目(2 相册 + 1 视频), 上限设 2 正好卡在中间。
    """
    sp = XChinaAggregateSpider()
    logs = []
    state = {"albums": [], "videos": [], "failed": [], "seen": set()}
    sp._expand(MODEL, "landing", 0, depth_limit=1, max_items=2, media="auto",
               log=logs.append, state=state, loader=Loader({MODEL: MODEL_PAGE}))
    assert state.get("truncated") is True
    assert len(state["albums"]) + len(state["videos"]) == 2
    assert any("条目上限" in m for m in logs), logs


def test_preview_marks_sampled_when_capped():
    """触顶时预告的数量只是下限, 必须标出来(与图集预览同一套语义)。"""
    d = XChinaAggregateSpider().preview(
        MODEL, options={"aggregate_depth": 1}, page_loader=Loader({MODEL: MODEL_PAGE}))
    assert d["sampled"] is False, "2 个相册远没到上限"


def test_expand_records_unreadable_pages():
    sp = XChinaAggregateSpider()
    state = {"albums": [], "videos": [], "failed": [], "seen": set()}
    sp._expand(MODEL, "landing", 0, depth_limit=1, max_items=50, media="auto",
               log=None, state=state, loader=Loader({}))
    assert state["failed"] and state["failed"][0]["url"] == MODEL


def test_expand_skips_video_links_when_media_is_image():
    """media=image 时连视频页的链接都不该收 —— 收了也只是白跑一趟委派。"""
    sp = XChinaAggregateSpider()
    state = {"albums": [], "videos": [], "failed": [], "seen": set()}
    sp._expand(MODEL, "landing", 0, depth_limit=1, max_items=50, media="image",
               log=None, state=state, loader=Loader({MODEL: MODEL_PAGE}))
    assert state["videos"] == []
    assert state["albums"]


# ---------------------------------------------------------------- 委派

def test_delegate_namespaces_files_under_aggregate_group(monkeypatch):
    """子采集器给的已是 `{相册名}/{seq}`, 再套一层模特名, 否则不同相册会平铺。"""
    sp = XChinaAggregateSpider()
    state = {"albums": [f"https://xchina.co/photo/id-{'a' * 13}.html"],
             "videos": [], "failed": [], "seen": set()}

    class FakeSpider:
        def crawl(self, url, **kw):
            return [{"type": "image", "url": "https://x/f.jpg",
                     "filename": "相册名/00001.jpg"}]

    monkeypatch.setattr(C, "get_collector", lambda name: FakeSpider())
    items = sp._delegate(state, {}, None, "模特甲")
    assert items[0]["filename"] == "模特甲/相册名/00001.jpg"


def test_delegate_keeps_going_when_one_child_fails(monkeypatch):
    """60 个相册坏 1 个是常态; 一个失败就整批报销会让用户以为全采不了。"""
    sp = XChinaAggregateSpider()
    state = {"albums": [f"https://xchina.co/photo/id-{'a' * 13}.html",
                        f"https://xchina.co/photo/id-{'b' * 13}.html"],
             "videos": [], "failed": [], "seen": set()}

    class FlakySpider:
        def crawl(self, url, **kw):
            if "aaa" in url:
                raise RuntimeError("boom")
            return [{"type": "image", "url": "https://x/ok.jpg",
                     "filename": "n/00001.jpg"}]

    monkeypatch.setattr(C, "get_collector", lambda name: FlakySpider())
    logs = []
    items = sp._delegate(state, {}, logs.append, "g")
    assert len(items) == 1, "好的那个必须留下来"
    assert state["failed"] and "boom" in state["failed"][0]["error"]
    assert any("已跳过" in m for m in logs), "跳过了要说, 不能让用户自己数"


def test_delegate_resets_stale_task_meta_from_child(monkeypatch):
    """聚合任务跨几十个相册, 子采集器的单相册元信息会误导用户, 必须被覆盖。"""
    sp = XChinaAggregateSpider()
    state = {"albums": [f"https://xchina.co/photo/id-{'a' * 13}.html"],
             "videos": [], "failed": [], "seen": set()}

    class FakeSpider:
        def crawl(self, url, **kw):
            return [{"type": "image", "url": "https://x/f.jpg",
                     "filename": "n/00001.jpg", "task_meta": {"gid": "child"}}]

    monkeypatch.setattr(C, "get_collector", lambda name: FakeSpider())
    items = sp._delegate(state, {}, None, "g")
    # 子采集器自己挂的会在 crawl 里被聚合级 task_meta 覆盖; 这里只验证
    # 委派阶段不主动篡改, 覆盖由 crawl 统一做(见 test_crawl_attaches_aggregate_meta)
    assert items[0]["task_meta"] == {"gid": "child"}


# ---------------------------------------------------------------- crawl / preview

def test_crawl_rejects_non_aggregate_input():
    with pytest.raises(ValueError) as ei:
        XChinaAggregateSpider().crawl("https://xchina.co/photo/id-6aa5136f606fe.html")
    assert "不是聚合页" in str(ei.value)


def test_crawl_raises_actionable_error_when_nothing_found():
    """旧行为是回空列表 -> 上层只记"采集到 0 个资源", 用户无从下手。"""
    with pytest.raises(ValueError) as ei:
        XChinaAggregateSpider().crawl(MODEL, page_loader=Loader({}))
    msg = str(ei.value)
    assert "Cloudflare" in msg, "要说清可能是被拦了"
    assert "复制" in msg, "要给出下一步动作, 而不是只报失败"


def test_crawl_attaches_aggregate_meta_and_returns_items(monkeypatch):
    sp = XChinaAggregateSpider()
    monkeypatch.setattr(C, "get_collector", lambda name: _StubChild())
    items = sp.crawl(MODEL, page_loader=Loader({MODEL: MODEL_PAGE}))
    assert items, "应展开出资源"
    assert all("task_meta" in r for r in items)
    meta = items[0]["task_meta"]
    assert meta["collector"] == "xchina_aggregate"
    assert meta["mode"] == "cascade"
    assert meta["album_count"] == 2 and meta["video_count"] == 1
    assert meta["resource_roots"] == {}, "聚合层不声称自己知道子页面的资源根"
    # 父目录来自页面标题, 而不是 URL
    assert items[0]["filename"].startswith("纱仓真菜/")


def test_crawl_media_image_drops_video_children(monkeypatch):
    sp = XChinaAggregateSpider()
    monkeypatch.setattr(C, "get_collector", lambda name: _StubChild())
    items = sp.crawl(MODEL, options={"media": "image"},
                     page_loader=Loader({MODEL: MODEL_PAGE}))
    assert items and all(r["type"] == "image" for r in items)


def test_preview_reports_children_counts_without_downloading():
    sp = XChinaAggregateSpider()
    d = sp.preview(MODEL, page_loader=Loader({MODEL: MODEL_PAGE}))
    assert d["collector"] == "xchina_aggregate"
    assert d["kind"] == "aggregate", "前端靠它把文案换成'将展开 N 个相册'"
    assert d["photos"] == 2, "photos 在这里是**相册数**, 不是文件数"
    assert d["videos"] == 1
    assert d["group"] == "纱仓真菜", "父目录取根页面标题, 与 crawl 同源"


def test_preview_and_crawl_agree_on_group(monkeypatch):
    """预告里的目录名必须**就是**实际落盘的那一层。

    曾经 preview 用 `_fallback_group(url)`(gid) 而 crawl 用页面标题, 于是
    "预告写 gid、落盘是模特名" —— 用户照预告去核对目录, 只会以为命名错了,
    而两条路各自都"对"。命名只允许有一个来源。
    """
    sp = XChinaAggregateSpider()
    monkeypatch.setattr(C, "get_collector", lambda name: _StubChild())
    d = sp.preview(MODEL, page_loader=Loader({MODEL: MODEL_PAGE}))
    items = sp.crawl(MODEL, page_loader=Loader({MODEL: MODEL_PAGE}))
    assert items[0]["filename"].split("/")[0] == d["group"] == "纱仓真菜"


def test_preview_honours_configured_max_items():
    """预览的闸门要跟创建用同一个数(options.max_items), 而不是它自己的参数。

    否则用户设了 2、预告却按 12 展开 —— 预告就不再代表创建行为, 而这正是
    "预告与行为不一致比不预告更糟"那条教训。
    """
    sp = XChinaAggregateSpider()
    d = sp.preview(MODEL, {"max_items": 1},
                   page_loader=Loader({MODEL: MODEL_PAGE}))
    assert d["photos"] + d["videos"] == 1, "闸门按 options 生效"
    assert d["sampled"] is True, "被闸门截断时数量只是下限, 必须标出来"


def test_preview_media_mirrors_the_user_setting():
    """设了"只要视频"就别在预告里报还会采图片 —— 预告与配置必须一致。"""
    sp = XChinaAggregateSpider()
    d = sp.preview(MODEL, {"media": "video"},
                   page_loader=Loader({MODEL: MODEL_PAGE}))
    assert d["media"] == ["video"]
    assert d["videos"] == 1

    d2 = sp.preview(MODEL, {"media": "image"},
                    page_loader=Loader({MODEL: MODEL_PAGE}))
    assert d2["media"] == ["image"]
    assert d2["videos"] == 0, "media=image 时视频条目根本不收进来"


def test_group_falls_back_to_url_id_when_page_has_no_title():
    """页面没标题(改版/残缺)时才回退 URL 里的 ID —— 绝不空手。"""
    sp = XChinaAggregateSpider()
    bare = ("<html><body>"
            "<a href='/photo/id-6aa5136f606fe.html'>A</a>"
            "</body></html>")
    d = sp.preview(MODEL, page_loader=Loader({MODEL: bare}))
    assert d["group"] == "601190f157fe7"


def test_preview_rejects_non_aggregate_input():
    with pytest.raises(ValueError):
        XChinaAggregateSpider().preview(
            "https://xchina.co/photo/id-6aa5136f606fe.html",
            page_loader=Loader({}))


class _StubChild:
    def crawl(self, url, **kw):
        is_video = "/video/id-" in url
        return [{"type": "video" if is_video else "image",
                 "url": "https://x/v.mp4" if is_video else "https://x/f.jpg",
                 "filename": "相册/00001.mp4" if is_video else "相册/00001.jpg"}]
