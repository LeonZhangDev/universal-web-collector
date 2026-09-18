"""图集采集器的「多媒体类型 + 相册标题命名」测试(不触网、不开浏览器)。

对应 V17: 同一个 gid 下图片与视频可以并存(xchina 实测 12 张图 + 4 段 mp4,
`00001.jpg` 与 `00001.mp4` 同名不同后缀), 所以媒体类型必须能枚举;
输出目录名则取相册页 `<title>`。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest

import collectors.album_meta as AM
from collectors import gallery_base as G
from collectors.xchina.gallery import XCHINA, XChinaGallerySpider
from core.config import VIDEO_ACCEPT
from core.filters import parse_size

IMAGE_ONLY = G.GallerySite(name="imgonly", base="https://img.example.com/photos",
                           variants=[".jpg"])

# 真实相册 ID 形态(≥6 字符), crawl 会校验, 不能随手写 "gid"
GID = "6a3654854fd25"

# 与真实页面同构的片段(2026-09-18 实测 xchina 相册页)
SAMPLE_PAGE = """<html><head>
<title>约啪172cm车模 完美肉体 主动求操 - 国模套图 - 各国其他套图 - 小黄书 xChina</title>
</head><body>
<h1 class="hero-title-item">约啪172cm车模 完美肉体 主动求操（FENDSON）</h1>
<script>
var domain = "https://img.xchina.io";
var favOptions = {"enabled":true,"objMode":"photo","objId":"6a3654854fd25"};
var videos = [{"url":"\\/photos\\/6a3654854fd25\\/00001.mp4","filename":"00001.mp4","filesize":"64M"}];
</script>
<div class="photo-items"></div>
</body></html>"""


def _media_session(video_ok=True):
    """假 session: `.mp4` 按 video_ok 返回 video/mp4 或越界的 text/html。"""

    class Sess:
        def head(self, url, **kw):
            if url.endswith(".mp4"):
                ctype = "video/mp4" if video_ok else "text/html"
            else:
                ctype = "image/jpeg"
            return type("R", (), {
                "status_code": 200,
                "headers": {"Content-Type": ctype, "Content-Length": "10"},
            })()

    return Sess()


# ---- 站点声明: 媒体类型可枚举 ----


def test_site_declares_image_and_video():
    assert XCHINA.media_names() == ["image", "video"]


def test_site_without_video_declaration_has_only_image():
    assert IMAGE_ONLY.media_names() == ["image"]
    assert IMAGE_ONLY.media("video") is None


def test_video_media_url_ctype_and_accept():
    m = XCHINA.media("video")
    assert m.name == "video"
    assert m.ctype_prefix == "video/", "视频的存在性判定必须看 video/ 前缀"
    assert m.variants == [".mp4"], "视频没有尺寸档位"
    assert m.default_ext == ".mp4"
    assert m.accept == VIDEO_ACCEPT
    assert m.url_for(XCHINA, "gid", 4) == "https://img.xchina.io/photos/gid/00004.mp4"


def test_parse_gid_from_video_direct_link():
    assert G.parse_gid(
        XCHINA, "https://img.xchina.io/photos/6aa113208a506/00004.mp4"
    ) == "6aa113208a506"


# ---- discover(media=...) ----


def test_discover_video_yields_mp4_without_mirrors():
    items = list(G.discover(XCHINA, "gid", media="video", max_count=2,
                            session=_media_session(), min_interval=0,
                            max_interval=0))
    assert [i["type"] for i in items] == ["video", "video"]
    assert items[0]["url"] == "https://img.xchina.io/photos/gid/00001.mp4"
    assert items[0]["mirrors"] == [], "视频没有备用档位"
    assert items[0]["filename"] == "gid/00001.mp4"


def test_discover_video_stops_when_past_end():
    """越界时该站返回 200 + text/html, 必须按 video/ 前缀判成不存在。"""
    items = list(G.discover(XCHINA, "gid", media="video", max_count=10,
                            session=_media_session(video_ok=False),
                            min_interval=0, max_interval=0))
    assert items == []


def test_discover_video_on_image_only_site_raises():
    with pytest.raises(ValueError) as e:
        list(G.discover(IMAGE_ONLY, "gid", media="video",
                        session=_media_session()))
    assert "未声明" in str(e.value)


def test_video_at_first_seq():
    assert G._video_at_first_seq(XCHINA, "gid", session=_media_session(True)) is True
    assert G._video_at_first_seq(XCHINA, "gid", session=_media_session(False)) is False


# ---- options.media 的解析 ----


def test_resolve_media_explicit_modes():
    sp = XChinaGallerySpider()
    assert sp.resolve_media(XCHINA, "gid", "image") == ["image"]
    assert sp.resolve_media(XCHINA, "gid", "video") == ["video"]
    assert sp.resolve_media(XCHINA, "gid", "both") == ["image", "video"]


def test_resolve_media_video_on_image_only_site_raises():
    sp = XChinaGallerySpider()
    with pytest.raises(ValueError):
        sp.resolve_media(IMAGE_ONLY, "gid", "video")


def test_resolve_media_auto_trusts_page_hint():
    sp = XChinaGallerySpider()
    assert sp.resolve_media(XCHINA, "gid", "auto",
                            meta={"videos": [{"url": "x"}]}) == ["image", "video"]


def test_resolve_media_auto_falls_back_to_probe(monkeypatch):
    """页面结构会变, 所以页面说没有视频时还要再探一次 `00001.mp4`。"""
    sp = XChinaGallerySpider()
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: True)
    assert sp.resolve_media(XCHINA, "gid", "auto", meta=None) == ["image", "video"]

    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)
    assert sp.resolve_media(XCHINA, "gid", "auto", meta=None) == ["image"]


def test_resolve_media_auto_on_image_only_site_never_probes(monkeypatch):
    monkeypatch.setattr(G, "_video_at_first_seq",
                        lambda *a, **kw: pytest.fail("不该去探测视频"))
    assert XChinaGallerySpider().resolve_media(IMAGE_ONLY, "gid", "auto") == ["image"]


# ---- 目录名: 相册标题 ----


def _one(gid="gid", **kw):
    return list(G.discover(XCHINA, gid, max_count=1, session=_media_session(),
                           min_interval=0, max_interval=0, **kw))[0]


def test_discover_uses_album_as_directory():
    assert _one(album="我的相册")["filename"] == "我的相册/00001.jpg"


def test_discover_sanitizes_album_into_single_segment():
    """标题里的 `/` 不能变成多一层目录, 更不能拼出 `..` 逃出任务目录。"""
    name = _one(album="../../etc")["filename"]
    assert len(name.split("/")) == 2, "只应有一层目录"
    assert name.split("/")[0] != ".."


def test_discover_falls_back_to_gid_when_album_empty():
    assert _one(album="")["filename"] == "gid/00001.jpg"
    assert _one(album=None)["filename"] == "gid/00001.jpg"


# ---- crawl: media 与 album_title 选项 ----


def _stub_discover(seen):
    """记录 discover 收到的参数, 不真枚举。"""

    def _d(site, gid, **kw):
        seen.append(kw)
        yield {
            "seq": 1,
            "type": kw.get("media", "image"),
            "url": f"https://img.example.com/{kw.get('media', 'image')}/00001.bin",
            "mirrors": [],
            "size": 10,
            "filename": "x",
        }

    return _d


def _patch_common(monkeypatch, seen):
    monkeypatch.setattr(G, "discover", _stub_discover(seen))
    monkeypatch.setattr(G, "_video_at_first_seq", lambda *a, **kw: False)


def test_crawl_clean_title_uses_first_segment(monkeypatch):
    seen = []
    _patch_common(monkeypatch, seen)
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: {
        "title": "套图名 - 分类 - 站名", "album": "套图名", "videos": []})

    items = XChinaGallerySpider().crawl(GID, {"album_title": "clean"})
    assert items[0]["album"] == "套图名"
    assert seen[0]["album"] == "套图名"


def test_crawl_full_title_mode(monkeypatch):
    seen = []
    _patch_common(monkeypatch, seen)
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: {
        "title": "套图名 - 分类 - 站名", "album": "套图名", "videos": []})

    items = XChinaGallerySpider().crawl(GID, {"album_title": "full"})
    assert items[0]["album"] == "套图名 - 分类 - 站名"


def test_crawl_id_mode_skips_page_entirely(monkeypatch):
    seen = []
    _patch_common(monkeypatch, seen)
    monkeypatch.setattr(G, "fetch_album_meta",
                        lambda *a, **kw: pytest.fail("album_title=id 不该访问相册页"))

    items = XChinaGallerySpider().crawl(GID, {"album_title": "id"})
    assert items[0]["album"] == GID


def test_crawl_falls_back_to_gid_when_page_unavailable(monkeypatch):
    """拿不到相册页只是"没名字", 绝不能让采集失败。"""
    seen = []
    _patch_common(monkeypatch, seen)
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: None)

    items = XChinaGallerySpider().crawl(GID, {})
    assert items[0]["album"] == GID


def test_crawl_media_video_only_requests_video(monkeypatch):
    seen = []
    _patch_common(monkeypatch, seen)
    monkeypatch.setattr(G, "fetch_album_meta", lambda *a, **kw: None)

    items = XChinaGallerySpider().crawl(GID, {"media": "video"})
    assert [s["media"] for s in seen] == ["video"]
    assert [i["type"] for i in items] == ["video"]


def test_crawl_rejects_unparsable_url():
    """⭐ 事故回归: 相册页 URL 里的页码绝不能被当成图集 ID 去枚举。

    `https://xchina.co/photo/.../10.html` 早期会解析出 "10", 然后去枚举
    `photos/10/*` —— 该站对不存在的图集也返回 200 + text/html, 于是"采集成功
    但 0 个资源"。现在这种 URL 直接报错。
    """
    with pytest.raises(ValueError) as e:
        XChinaGallerySpider().crawl("https://xchina.co/photo/10.html", {})
    assert "无法从" in str(e.value)


# ---- 相册页元信息解析(纯函数) ----


def test_extract_album_meta_from_real_page_shape():
    m = AM.extract_album_meta(SAMPLE_PAGE)
    assert m["title"] == ("约啪172cm车模 完美肉体 主动求操 - 国模套图"
                          " - 各国其他套图 - 小黄书 xChina")
    assert m["album"] == "约啪172cm车模 完美肉体 主动求操"
    assert m["h1"] == "约啪172cm车模 完美肉体 主动求操（FENDSON）"
    assert m["obj_id"] == "6a3654854fd25"
    assert m["challenge"] is False
    assert m["videos"] == [{
        "url": "https://img.xchina.io/photos/6a3654854fd25/00001.mp4",
        "size": parse_size("64M"),
    }]


def test_extract_album_meta_custom_split():
    m = AM.extract_album_meta(SAMPLE_PAGE, split=r"\s+—\s+")
    assert m["album"] == m["title"], "分隔符对不上时整体退回原标题"


def test_extract_album_meta_flags_challenge_page():
    m = AM.extract_album_meta("<html><title>Just a moment...</title></html>")
    assert m["challenge"] is True
    assert m["videos"] == []


def test_looks_ready_rejects_half_baked_pages():
    assert AM._looks_ready("<html><title>Loading https://xchina.co/...</title></html>") is False
    assert AM._looks_ready("") is False
    assert AM._looks_ready(SAMPLE_PAGE) is True


def test_validate_rejects_wrong_album_page():
    assert AM._validate({"title": "T", "obj_id": "gid", "challenge": False}, "gid") is None
    assert "不一致" in AM._validate(
        {"title": "T", "obj_id": "other", "challenge": False}, "gid")
    assert AM._validate({"title": "登录 - xChina", "obj_id": "", "challenge": False},
                        "gid") is not None
    assert AM._validate(None, "gid") is not None


def test_fetch_album_meta_caches_success_only(monkeypatch):
    AM.clear_cache()
    calls = []
    monkeypatch.setattr(AM, "_load_html",
                        lambda url, **kw: calls.append(url) or SAMPLE_PAGE)
    monkeypatch.setattr(AM, "_storage_state", lambda url: None)

    url = "https://xchina.co/photo/id-6a3654854fd25.html"
    m1 = AM.fetch_album_meta(url, split=None, gid="6a3654854fd25")
    m2 = AM.fetch_album_meta(url, split=None, gid="6a3654854fd25")
    assert m1["album"] == "约啪172cm车模 完美肉体 主动求操"
    assert m2 is m1 and len(calls) == 1, "命中缓存不该再开一次浏览器"
    AM.clear_cache()


def test_fetch_album_meta_returns_none_on_bad_page(monkeypatch):
    AM.clear_cache()
    monkeypatch.setattr(AM, "_load_html", lambda *a, **kw: None)
    monkeypatch.setattr(AM, "_storage_state", lambda url: None)
    assert AM.fetch_album_meta("https://xchina.co/photo/id-x.html", gid="x") is None
    AM.clear_cache()


def test_fetch_album_meta_tries_anonymous_first(monkeypatch):
    """⭐ 顺序不能反: 陈旧的 cf_clearance 会让 Cloudflare 直接拒绝, 匿名反而能过。"""
    AM.clear_cache()
    states = []

    def load(url, log=None, storage_state=None):
        states.append(storage_state)
        return None if storage_state is None else SAMPLE_PAGE

    monkeypatch.setattr(AM, "_load_html", load)
    monkeypatch.setattr(AM, "_storage_state", lambda url: "C:/state.json")

    m = AM.fetch_album_meta("https://xchina.co/photo/id-6a3654854fd25.html",
                            gid="6a3654854fd25")
    assert states == [None, "C:/state.json"]
    assert m["album"] == "约啪172cm车模 完美肉体 主动求操"
    AM.clear_cache()
