"""「创建前预览」与「体积前置」测试(不触网、不开浏览器)。

为什么值得单独测
================
相册页其实**白给了三样东西**: 站点自报的资源数量(`12P + 4V`)、每段视频的体积
(`filesize`, 实测精确)、以及标签/厂牌。前两样让"创建任务之前"就能回答
"这次会采到什么、有多大", 不必先枚举、更不必先下载 ——
毕竟那个相册是 12 张图 + 4 段视频共 260MB, 默默拖完再告诉用户就太晚了。

`size` 前置还有一条隐性收益: 图集枚举的 probe 本来就要拿 Content-Length,
把已经拿到的值带进下载层, 大小过滤就不必再发一次 HEAD。这条一旦回归
(比如又改回无条件 `probe_size`), 图集任务会平白多出一倍请求。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest

import collectors.album_meta as AM
import core.database as db
import core.task_manager as tm
from collectors import gallery_base as G
from collectors.xchina.gallery import XCHINA, XChinaGallerySpider

GID = "6a3654854fd25"

# 与真实页面同构: 数量/厂牌/标签块都按图标锚定, 不靠 div 顺序
PAGE_WITH_COUNTS = """<html><head>
<title>套图名 - 国模套图 - 小黄书 xChina</title>
</head><body>
<h1 class="hero-title-item">套图名（FENDSON）</h1>
<div class="photo-items">
  <div class="item"><i class="fas fa-image"></i></div><div class="text">12P + 4V</div>
  <div class="item"><i class="fas fa-file"></i></div><div class="text">FENDSON</div>
  <div class="item tags-line">
    <div class="tag">丝袜</div><div class="tag">情趣内衣</div>
    <div class="tag">吊带袜</div><div class="tag">丝袜</div>
  </div>
</div>
<script>
var domain = "https://img.xchina.io";
var favOptions = {"objId": "6a3654854fd25"};
var videos = [
  {"url":"\\/photos\\/6a3654854fd25\\/00001.mp4","filename":"00001.mp4","filesize":"64M"},
  {"url":"\\/photos\\/6a3654854fd25\\/00002.mp4","filename":"00002.mp4","filesize":"105M"}
];
</script>
</body></html>"""


def _meta(**kw):
    m = {
        "title": "套图名 - 分类 - 站名",
        "album": "套图名",
        "h1": "套图名（FENDSON）",
        "videos": [],
        "photos": None,
        "videos_declared": None,
        "maker": "",
        "tags": [],
        "challenge": False,
    }
    m.update(kw)
    return m


# ---- 相册页: 数量 / 厂牌 / 标签 ----


def test_extract_declared_counts_maker_and_tags():
    m = AM.extract_album_meta(PAGE_WITH_COUNTS)
    assert m["photos"] == 12, "站点自报的 12P 要能解析出来"
    assert m["videos_declared"] == 4
    assert m["maker"] == "FENDSON"
    assert m["tags"] == ["丝袜", "情趣内衣", "吊带袜"], "标签要去重且保序"
    assert m["videos"][1]["size"] == 105 * 1024 * 1024


def test_extract_counts_are_optional_not_guessed():
    """页面没有数量块时必须是 None —— 猜一个数字比不显示更糟。"""
    m = AM.extract_album_meta("<html><title>T</title><body>photo-items</body></html>")
    assert m["photos"] is None and m["videos_declared"] is None
    assert m["tags"] == [] and m["maker"] == ""


def test_extract_photos_only_without_video_counts():
    html = ('<html><title>T</title><i class="fas fa-image"></i></div>'
            '<div class="text">30P</div></html>')
    m = AM.extract_album_meta(html)
    assert m["photos"] == 30
    assert m["videos_declared"] is None


# ---- 目录名: h1 与标签分层 ----


def test_album_name_h1_mode_uses_richer_title():
    sp = XChinaGallerySpider()
    assert sp.album_name(_meta(), "h1", GID) == "套图名（FENDSON）"


def test_album_name_h1_falls_back_when_missing():
    sp = XChinaGallerySpider()
    assert sp.album_name(_meta(h1=""), "h1", GID) == "套图名"
    assert sp.album_name(_meta(h1="", album=""), "h1", GID) == GID


def test_album_name_id_mode_ignores_meta():
    sp = XChinaGallerySpider()
    assert sp.album_name(_meta(), "id", GID) == GID


def test_group_name_is_only_the_album_folder():
    """目录树里只有相册名一层 —— "标签分层"开关已取消(布局规则定死了)。

    曾经的 `album_tags_dir` 会在相册名外套一层 `丝袜-情趣内衣/相册名/`, 但布局
    规则(下载目录的下一级只有相册文件夹)会把它削掉 —— 结果是"日志和预览显示的
    路径"与"实际落盘的路径"对不上, 用户照预览去找文件会找不到。标签本身没丢,
    完整写在 album.json / manifest 里。
    """
    sp = XChinaGallerySpider()
    assert sp.group_name("套图名", GID) == "套图名"
    # 标签不再参与目录名, 传什么标签结果都一样
    assert sp.group_name(_meta(tags=["丝袜", "情趣内衣"])["album"], GID) == "套图名"


def test_group_name_falls_back_to_gid():
    sp = XChinaGallerySpider()
    assert sp.group_name("", GID) == GID, "取不到相册名时回退图集 ID"
    assert sp.group_name("..", GID) == GID, "纯点段没有可用内容, 同样回退"


def test_group_with_slash_is_flattened_by_layout():
    """相册标题里带 `/` 时, 归位规则只保留最深一段, 不会多出一层目录。"""
    from core import layout

    sp = XChinaGallerySpider()
    group = sp.group_name("a/b", GID)
    rel, album = layout.place("image", f"{group}/00001.jpg", "a/b")
    assert rel == "b/00001.jpg"
    assert album == "b"


# ---- read_album_meta: id 模式完全不开浏览器 ----


def test_read_album_meta_skips_browser_for_id_mode(monkeypatch):
    monkeypatch.setattr(G, "fetch_album_meta",
                        lambda *a, **kw: pytest.fail("album_title=id 不该读相册页"))
    assert XChinaGallerySpider().read_album_meta(XCHINA, GID, "id") is None


# ---- preview: 有页面数据时零枚举 ----


def test_preview_uses_page_data_without_enumerating(monkeypatch):
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: _meta(
        videos=[{"url": "https://img.xchina.io/photos/x/00001.mp4", "size": 64 * 1024 * 1024},
                {"url": "https://img.xchina.io/photos/x/00002.mp4", "size": 105 * 1024 * 1024}],
        photos=12, videos_declared=4, tags=["丝袜"], maker="FENDSON"))
    monkeypatch.setattr(G, "discover",
                        lambda *a, **kw: pytest.fail("有自报数据时不该枚举序号"))

    data = XChinaGallerySpider().preview(GID, {})

    assert data["photos"] == 12 and data["videos"] == 4
    assert data["video_bytes"] == 169 * 1024 * 1024
    assert data["sampled"] is False, "来自页面自报, 不是抽样"
    assert data["page"] is True
    assert data["media"] == ["image", "video"], "页面说含视频就该提示会采视频"
    assert data["tags"] == ["丝袜"] and data["maker"] == "FENDSON"
    assert data["group"] == "套图名"
    assert data["sample_files"] == ["套图名/00001.jpg", "套图名/00002.jpg"]
    assert [v["name"] for v in data["video_items"]] == ["00001.mp4", "00002.mp4"]


def test_preview_falls_back_to_sampling_when_page_missing(monkeypatch):
    """没有页面数据时只能用受限枚举, 此时数量只是**下限**。"""
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: None)
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)
    seen = []

    def fake_discover(site, gid, **kw):
        seen.append(kw)
        for i in (1, 2, 3):
            yield {"seq": i, "type": "image", "url": f"u{i}", "mirrors": [],
                   "size": 10, "filename": f"套图名/{i:05d}.jpg"}

    monkeypatch.setattr(G, "discover", fake_discover)

    data = XChinaGallerySpider().preview(GID, {}, max_items=3)

    assert data["sampled"] is True
    assert data["page"] is False
    assert data["photos"] == 3
    assert data["album_source"] == "id", "拿不到页面就该提示命名回退到图集 ID"
    assert data["sample_files"] == ["套图名/00001.jpg", "套图名/00002.jpg", "套图名/00003.jpg"]
    assert seen[0]["max_count"] == 3, "预览必须受 max_items 限制, 否则接口会被拖住"


def test_preview_sample_files_predicted_when_nothing_found(monkeypatch):
    """一张都没枚举到时也要让用户先看到文件名长什么样(命名最容易错)。"""
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: None)
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)
    monkeypatch.setattr(G, "discover", lambda *a, **kw: iter(()))

    data = XChinaGallerySpider().preview(GID, {})

    assert data["photos"] in (0, None)
    assert data["sample_files"] == ["6a3654854fd25/00001.jpg",
                                    "6a3654854fd25/00002.jpg"]


def test_preview_respects_media_option(monkeypatch):
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: _meta())
    monkeypatch.setattr(G, "discover", lambda *a, **kw: iter(()))

    data = XChinaGallerySpider().preview(GID, {"media": "video"})

    assert data["media"] == ["video"], "用户明确只要视频时不该提示还会采图片"


def _meta_with_av():
    return _meta(
        videos=[{"url": "https://img.xchina.io/photos/x/00001.mp4", "size": 64 * 1024 * 1024},
                {"url": "https://img.xchina.io/photos/x/00002.mp4", "size": 105 * 1024 * 1024}],
        photos=12, videos_declared=4, tags=["丝袜"])


def test_preview_excludes_videos_when_media_is_image(monkeypatch):
    """⭐ media=image 时预告不能再报视频数量与体积。

    回归表现: 用户选了"仅图片", 预告却写"本次将采集 12 张图 + 4 段视频 · 视频合计
    165MiB", 照着创建之后视频根本没下。预告与真实行为不一致, 用户会以为漏下了
    东西 —— 那比不预告更糟。
    """
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: _meta_with_av())
    monkeypatch.setattr(G, "discover",
                        lambda *a, **kw: pytest.fail("有自报数据时不该枚举"))

    data = XChinaGallerySpider().preview(GID, {"media": "image"})

    assert data["media"] == ["image"]
    assert data["photos"] == 12
    assert data["videos"] is None, "不含视频就不该报视频段数"
    assert data["video_items"] == [] and data["video_bytes"] == 0
    # 相册页自报的总量仍要带出来, 否则用户无从知道"另有 4 段视频没采"
    assert data["videos_declared"] == 4
    assert data["sample_files"] == ["套图名/00001.jpg", "套图名/00002.jpg"]


def test_preview_excludes_photos_when_media_is_video(monkeypatch):
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: _meta_with_av())
    monkeypatch.setattr(G, "discover",
                        lambda *a, **kw: pytest.fail("有自报数据时不该枚举"))

    data = XChinaGallerySpider().preview(GID, {"media": "video"})

    assert data["media"] == ["video"]
    assert data["photos"] is None, "只要视频时不该报图片张数"
    assert data["videos"] == 4 and data["video_bytes"] == 169 * 1024 * 1024
    assert data["photos_declared"] == 12, "图片总量仍要保留, 供用户对照"


def test_preview_auto_keeps_both(monkeypatch):
    """默认 auto 且页面确含视频时, 两项都要如实报出来(否则用户不知道要等 165MiB)。"""
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: _meta_with_av())
    monkeypatch.setattr(G, "discover",
                        lambda *a, **kw: pytest.fail("有自报数据时不该枚举"))

    data = XChinaGallerySpider().preview(GID, {})

    assert data["media"] == ["image", "video"]
    assert data["photos"] == 12 and data["videos"] == 4
    assert data["video_bytes"] == 169 * 1024 * 1024


def test_preview_sample_files_match_actual_paths(monkeypatch):
    """预览显示的文件名示例必须与真实落盘路径一致。

    不一致的代价: 用户照预览去磁盘上找, 找不到, 于是以为"下错了目录"。
    所以分组名只规范化一次, `discover()` 与 `preview()` 共用。
    这里用恶意标题(带 `..`)来验: 规范化必须真的发生。
    """
    monkeypatch.setattr(G, "fetch_album_meta",
                        lambda *a, **kw: _meta(title="../../etc", album="../../etc",
                                               photos=9))
    monkeypatch.setattr(G, "discover",
                        lambda *a, **kw: pytest.fail("有自报数据时不该枚举"))

    data = XChinaGallerySpider().preview(GID, {})

    # 关键不是"删掉 .. 这两个字符", 而是**没有任何一段叫 `..`** ——
    # `.._.._etc` 只是一个合法目录名, 逃不出任务目录。
    segs = [s for s in data["group"].split("/") if s]
    assert segs and all(s not in ("..", ".") for s in segs)
    assert data["sample_files"][0].startswith(data["group"] + "/")


def test_preview_group_matches_discover_group(monkeypatch):
    """`preview().group` 与 `discover()` 内部用的 group 必须是同一个值。"""
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: _meta(
        title="A/B", album="A/B", tags=["t/u"]))
    seen = []

    def fake_discover(site, gid, **kw):
        seen.append(kw)
        return iter(())

    monkeypatch.setattr(G, "discover", fake_discover)
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)

    sp = XChinaGallerySpider()
    data = sp.preview(GID, {})

    assert seen[0]["album"] == data["group"]


def test_preview_rejects_unparsable_url():
    with pytest.raises(ValueError):
        XChinaGallerySpider().preview("https://xchina.co/photo/10.html", {})


# ---- crawl: 预览同款的 max_count ----


def test_crawl_passes_max_count_to_discover(monkeypatch):
    seen = []

    def fake_discover(site, gid, **kw):
        seen.append(kw)
        yield {"seq": 1, "type": "image", "url": "u", "mirrors": [],
               "size": 1, "filename": "x"}

    monkeypatch.setattr(G, "discover", fake_discover)
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: None)

    XChinaGallerySpider().crawl(GID, {}, max_count=5)

    assert seen[0]["max_count"] == 5


def test_crawl_carries_size_from_discovery(monkeypatch):
    """采集阶段的 probe 已经拿到 Content-Length, 要一路带到资源上。"""
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: None)
    monkeypatch.setattr(G, "discover", lambda *a, **kw: iter([{
        "seq": 1, "type": "image", "url": "u", "mirrors": [], "size": 4096,
        "filename": "x/00001.jpg",
    }]))

    items = XChinaGallerySpider().crawl(GID, {})

    assert items[0]["size"] == 4096


# ---- 体积前置: 已知 size 时不再发 HEAD ----


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


class FakeSpider:
    def __init__(self, items):
        self.items = items

    def crawl(self, url, **kw):
        return list(self.items)


def _run_task(monkeypatch, tmp_path, items, options, probe):
    """建一个任务并等它跑完; 返回 (task, 资源列表)。"""
    monkeypatch.setattr(tm, "DOWNLOADS_DIR", tmp_path / "dl")
    monkeypatch.setattr(tm, "get_collector", lambda name: FakeSpider(items))
    monkeypatch.setattr(tm, "probe_size", probe)
    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        tid = db.create_task("https://fake/album", "fake", None, options)
        mgr.submit(tid)
        deadline = time.time() + 15
        while time.time() < deadline:
            if db.get_task(tid)["status"] not in tm.ACTIVE_STATES:
                break
            time.sleep(0.05)
        return db.get_task(tid), db.get_resources(tid)
    finally:
        mgr.shutdown(wait=True)


def test_known_size_skips_head_probe(tmp_db, tmp_path, monkeypatch):
    """⭐ 图集任务的大小过滤应当是**零额外请求**的。

    回归表现: 又改回无条件 `probe_size`, 每个资源平白多一次 HEAD。
    """
    # 两条都超限 —— 这样整个任务不会真的去下载, 测试保持离线
    items = [
        {"type": "image", "url": "https://img/x/00001.jpg", "mirrors": [],
         "size": 100 * 1024 * 1024, "filename": "a/00001.jpg"},
        {"type": "video", "url": "https://img/x/00001.mp4", "mirrors": [],
         "size": 200 * 1024 * 1024, "filename": "a/00001.mp4"},
    ]
    task, res = _run_task(
        monkeypatch, tmp_path, items, {"max_size": "50MB"},
        lambda *a, **kw: pytest.fail("size 已知时不该再发 HEAD 探测"),
    )

    assert [r["status"] for r in res] == ["filtered", "filtered"]
    assert task["status"] == "success", "全被规则挡掉不算失败"
    assert "太大" in res[1]["note"] and "上限" in res[1]["note"]


def test_unknown_size_still_probed(tmp_db, tmp_path, monkeypatch):
    """回归反面: size 未知(采集器没给)时, 兜底的 HEAD 探测不能丢。"""
    items = [{"type": "image", "url": "https://img/x/1.jpg", "mirrors": [],
              "size": None, "filename": "a/1.jpg"}]
    calls = []

    def probe(url, *a, **kw):
        calls.append(url)
        return 999 * 1024 * 1024

    task, res = _run_task(monkeypatch, tmp_path, items, {"max_size": "1MB"}, probe)

    assert calls == ["https://img/x/1.jpg"]
    assert res[0]["status"] == "filtered"


def test_size_as_string_is_coerced(tmp_db, tmp_path, monkeypatch):
    """options/DB 里 size 可能是字符串(JSON 往返), 不能因此走错分支。"""
    items = [{"type": "image", "url": "https://img/x/1.jpg", "mirrors": [],
              "size": str(999 * 1024 * 1024), "filename": "a/1.jpg"}]

    task, res = _run_task(
        monkeypatch, tmp_path, items, {"max_size": "1MB"},
        lambda *a, **kw: pytest.fail("字符串 size 也应当被识别, 不该再探测"),
    )

    assert res[0]["status"] == "filtered"
