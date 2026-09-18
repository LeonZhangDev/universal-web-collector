"""图集枚举采集器的纯逻辑测试(不触网)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from collectors import gallery_base as G
from collectors.xchina.gallery import XCHINA
from downloaders.base import _ext_of, _swap_ext


def _ok_session():
    class Sess:
        def head(self, url, **kw):
            return type("R", (), {
                "status_code": 200,
                "headers": {"Content-Type": "image/jpeg", "Content-Length": "10"},
            })()

    return Sess()


def test_parse_gid_from_plain_id():
    assert G.parse_gid(XCHINA, "6aa113208a506") == "6aa113208a506"


def test_parse_gid_from_image_url():
    for u in [
        "https://img.xchina.io/photos/6aa113208a506/00001.jpg",
        "https://img.xchina.io/photos/6aa113208a506/00046_600x0.webp",
    ]:
        assert G.parse_gid(XCHINA, u) == "6aa113208a506"


def test_parse_gid_from_page_url():
    assert G.parse_gid(
        XCHINA, "https://xchina.co/photoShow.html?id=6aa113208a506"
    ) == "6aa113208a506"


def test_parse_gid_rejects_junk():
    for bad in ["", "not a url", "abc", "   "]:
        assert G.parse_gid(XCHINA, bad) is None


def test_url_template_and_variants():
    assert XCHINA.url_for("gid1", 1) == "https://img.xchina.io/photos/gid1/00001.jpg"
    assert XCHINA.url_for("gid1", 46, "_600x0.webp").endswith("00046_600x0.webp")


def test_quality_map_selects_variant():
    assert XCHINA.variant_of("original") == ".jpg"
    assert XCHINA.variant_of("1200") == "_1200x0.webp"
    assert XCHINA.variant_of("600") == "_600x0.webp"
    assert XCHINA.variant_of(None) == ".jpg"
    assert XCHINA.variant_of("bogus") == ".jpg", "未知档位应回退最高画质"


def test_mirror_order_prefers_neighbouring_quality():
    """备用顺序按画质接近度排列。

    选 1200(index 1) 时, 原图与 800 的距离相同(都是 1), 此时**原图优先** ——
    这与 discover() 在所选档缺失时回退 top_variant 的策略一致:
    原图是站点的权威档位, 作为兜底最可靠。
    """
    order = [XCHINA.variants[i] for i in XCHINA.mirror_order("_1200x0.webp")]
    assert order == [".jpg", "_800x0.webp", "_600x0.webp"]


def test_mirror_order_for_top_quality_is_descending():
    """选原图时备用应按画质降序: 1200 -> 800 -> 600。"""
    order = [XCHINA.variants[i] for i in XCHINA.mirror_order(".jpg")]
    assert order == ["_1200x0.webp", "_800x0.webp", "_600x0.webp"]


def test_mirror_switch_keeps_extension_consistent():
    """主 URL 是 .jpg 而镜像是 .webp 时, 落盘后缀必须跟着变。"""
    p = Path("/tmp/out/00001.jpg")
    assert _swap_ext(p, "https://x/00001_800x0.webp") == Path("/tmp/out/00001.webp")
    assert _swap_ext(p, "https://x/00001.jpg") == p


def test_ext_of():
    assert _ext_of("https://a.com/b/c.JPG?x=1") == ".jpg"
    assert _ext_of("https://a.com/b/c") == ""


def test_probe_classifies_html_as_missing():
    """该站对不存在的图片返回 200 + text/html, probe 必须判定为 missing。"""

    class FakeResp:
        def __init__(self, code, ctype, length=None):
            self.status_code = code
            self.headers = {"Content-Type": ctype}
            if length is not None:
                self.headers["Content-Length"] = str(length)

    class FakeSess:
        def __init__(self, resp):
            self._resp = resp

        def head(self, *a, **kw):
            return self._resp

    ok = G.probe(FakeSess(FakeResp(200, "image/jpeg", 656713)), "http://x/1.jpg")
    assert ok[0] == G.PROBE_OK and ok[1] == 656713

    miss = G.probe(FakeSess(FakeResp(200, "text/html; charset=utf-8")), "http://x/2.jpg")
    assert miss[0] == G.PROBE_MISSING

    err = G.probe(FakeSess(FakeResp(503, "text/html")), "http://x/3.jpg")
    assert err[0] == G.PROBE_ERROR, "5xx 必须区别于不存在, 否则会提前截断图集"


def test_discover_stops_after_consecutive_misses():
    """连续 miss_stop 次判定不存在后停止, 且不计入结果。"""
    seq = {"n": 0}

    class Sess:
        def head(self, url, **kw):
            seq["n"] += 1
            ctype = "image/jpeg" if seq["n"] <= 3 else "text/html"
            return type("R", (), {
                "status_code": 200,
                "headers": {"Content-Type": ctype, "Content-Length": "100"},
            })()

    items = list(G.discover(XCHINA, "gid", session=Sess(), miss_stop=2,
                            min_interval=0, max_interval=0))
    assert [i["seq"] for i in items] == [1, 2, 3]


def test_discover_builds_mirrors():
    items = list(G.discover(XCHINA, "gid", max_count=2, session=_ok_session(),
                            min_interval=0, max_interval=0))
    assert len(items) == 2
    assert len(items[0]["mirrors"]) == len(XCHINA.variants) - 1
    assert all("_x0" in m or "webp" in m for m in items[0]["mirrors"])


def test_discover_default_quality_uses_original():
    items = list(G.discover(XCHINA, "gid", max_count=1, session=_ok_session(),
                            min_interval=0, max_interval=0))
    assert items[0]["url"].endswith(".jpg")
    assert items[0]["mirrors"][0].endswith("_1200x0.webp")


def test_discover_quality_changes_main_url():
    """选 1200 档时主 URL 应为 webp 变体, 且原图仍在 mirrors 中作为兜底。"""
    items = list(G.discover(XCHINA, "gid", quality="1200", max_count=1,
                            session=_ok_session(), min_interval=0,
                            max_interval=0))
    assert items[0]["url"].endswith("_1200x0.webp")
    assert items[0]["mirrors"][0].endswith(".jpg"), "最近的兜底应是原图"
    assert all(".jpg" in m or "webp" in m for m in items[0]["mirrors"])


def test_discover_falls_back_when_quality_variant_missing():
    """所选档位缺失时必须回退原图, 而不是把这张图判为不存在(否则会漏图)。"""

    class Sess:
        def head(self, url, **kw):
            ctype = "image/jpeg" if url.endswith(".jpg") else "text/html"
            return type("R", (), {
                "status_code": 200,
                "headers": {"Content-Type": ctype, "Content-Length": "10"},
            })()

    logs = []
    items = list(G.discover(XCHINA, "gid", quality="1200", max_count=1,
                            session=Sess(), min_interval=0, max_interval=0,
                            log=logs.append))
    assert len(items) == 1, "档位缺失不应导致漏图"
    assert items[0]["url"].endswith(".jpg")
    assert any("回退" in m for m in logs)


# ---- 2026-09-18: 相册页 URL 解析事故的回归用例 ----
#
# 事故现场: 输入 https://xchina.co/photo/id-6aa5136f606fe/10.html
# 旧实现的正则只认 /photos/, 匹配失败后退到"取路径末段", 拿到页码 "10" 当图集 ID,
# 于是去枚举 photos/10/00001.jpg —— 该站对不存在的图集同样返回 200 + text/html,
# 3 个序号全被判"不存在", 枚举停止, 任务**报 success 却 0 个资源**。


def test_parse_gid_from_album_page_url():
    """相册页的三种形态都必须解析出真正的图集 ID。"""
    for u in [
        "https://xchina.co/photo/id-6aa113208a506/10.html",
        "https://xchina.co/photo/id-6aa113208a506/1.html",
        "https://xchina.co/photo/id-6aa113208a506.html",
        "https://xchina.co/photo/id-6aa113208a506/",
    ]:
        assert G.parse_gid(XCHINA, u) == "6aa113208a506", u


def test_parse_gid_never_returns_page_number():
    """页码/普通路径词一律拒绝: 猜一个 ID 比报错严重得多。

    猜错的表现是"任务成功但什么都没下到", 用户完全看不出原因;
    返回 None 则会让采集器抛出可读的错误。
    """
    for bad in [
        "https://xchina.co/photo/10.html",
        "https://xchina.co/photo/2.html",
        "https://xchina.co/photo/",
        "https://xchina.co/photo/id-",
        "https://xchina.co/",
    ]:
        assert G.parse_gid(XCHINA, bad) is None, bad


def test_parse_gid_prefers_real_id_over_tail():
    """有 id_patterns 命中时不该走到退路(退路只在"像 ID"时才兜底)。"""
    assert G.parse_gid(
        XCHINA, "https://xchina.co/photo/id-abc123/999.html"
    ) == "abc123"


def test_gallery_page_url_end_to_end_gid():
    """从相册页 URL 直达该图集的第一张图 URL。"""
    gid = G.parse_gid(XCHINA, "https://xchina.co/photo/id-6aa5136f606fe/10.html")
    assert XCHINA.url_for(gid, 1) == "https://img.xchina.io/photos/6aa5136f606fe/00001.jpg"


def _resp(status, headers, ctx=False):
    class R:
        def __init__(self):
            self.status_code = status
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R()


def test_probe_uses_head_when_content_type_present():
    calls = []

    class Sess:
        def head(self, url, **kw):
            calls.append("head")
            return _resp(200, {"Content-Type": "image/jpeg", "Content-Length": "7"})

    state, size, _ = G.probe(Sess(), "https://x/00001.jpg")
    assert (state, size) == (G.PROBE_OK, 7)
    assert calls == ["head"], "HEAD 已经给出 Content-Type 时不该多发请求"


def test_probe_falls_back_to_get_when_head_lacks_content_type():
    """HEAD 返回 200 但不带 Content-Type 时必须退回流式 GET 再判断。

    若直接按"没有 image/ 前缀"判定, 每张图都会被判成不存在, 整个图集枚举
    在第一张就停下 —— 表现和"这个相册是空的"一模一样。
    """
    calls = []

    class Sess:
        def head(self, url, **kw):
            calls.append("head")
            return _resp(200, {})

        def get(self, url, **kw):
            calls.append("get")
            assert kw.get("stream") is True, "必须流式取响应头, 不要把正文拖下来"
            return _resp(200, {"Content-Type": "image/jpeg", "Content-Length": "42"})

    state, size, ctype = G.probe(Sess(), "https://x/00001.jpg")
    assert state == G.PROBE_OK
    assert size == 42
    assert ctype == "image/jpeg"
    assert calls == ["head", "get"]


def test_probe_get_fallback_still_reports_missing():
    """GET 兜底路径同样要能识别"越界返回 html"这种伪 200。"""

    class Sess:
        def head(self, url, **kw):
            return _resp(200, {})

        def get(self, url, **kw):
            return _resp(200, {"Content-Type": "text/html; charset=UTF-8"})

    state, _, ctype = G.probe(Sess(), "https://x/99999.jpg", retries=1)
    assert state == G.PROBE_MISSING
    assert "text/html" in ctype


def test_discover_enumerates_from_page_url_gid():
    """回归核心: 相册页 URL -> 枚举出资源(而不是 0 个)。"""
    items = list(G.discover(
        XCHINA,
        G.parse_gid(XCHINA, "https://xchina.co/photo/id-abc123/10.html"),
        max_count=2, session=_ok_session(), min_interval=0, max_interval=0,
    ))
    assert len(items) == 2
    assert items[0]["url"] == "https://img.xchina.io/photos/abc123/00001.jpg"


# ---- 建议文件名: 画质标记 ----
#
# 曾经落盘成 `00001_.jpg.jpg`: 判断"是否最高画质档"时拿**变体后缀**(".jpg")
# 去和**画质档名**("original")比, 永不相等, 于是原图也被贴上标记, 再拼上
# 从 URL 取来的扩展名就成了双后缀。


def test_filename_has_no_double_extension():
    items = list(G.discover(XCHINA, "gid", max_count=1, session=_ok_session(),
                            min_interval=0, max_interval=0))
    assert items[0]["filename"] == "gid/00001.jpg"


def test_filename_marks_non_top_quality():
    items = list(G.discover(XCHINA, "gid", quality="1200", max_count=1,
                            session=_ok_session(), min_interval=0,
                            max_interval=0))
    assert items[0]["filename"] == "gid/00001_1200.webp"


def test_filename_drops_tag_when_falling_back_to_original():
    """1200 档缺失回退原图时, 文件名也不该带 1200 标记(内容其实是原图)。"""

    class Sess:
        def head(self, url, **kw):
            ctype = "image/jpeg" if url.endswith(".jpg") else "text/html"
            return type("R", (), {
                "status_code": 200,
                "headers": {"Content-Type": ctype, "Content-Length": "10"},
            })()

    items = list(G.discover(XCHINA, "gid", quality="1200", max_count=1,
                            session=Sess(), min_interval=0, max_interval=0))
    assert items[0]["filename"] == "gid/00001.jpg"

