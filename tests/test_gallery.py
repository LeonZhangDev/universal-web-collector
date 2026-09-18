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
