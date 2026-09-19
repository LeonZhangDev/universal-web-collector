"""CDN 基址 / 序号宽度自动发现(不触网)。

背景: 站点会按相册把资源分到 `photos` / `photos2` / `photos3` 等不同子路径,
甚至补零位数也不同(既有 `00001.jpg` 也有 `0001.jpg`)。写死一个 base 的后果是
"相册明明存在, 枚举的却是另一条不存在的路径 -> 全部 missing -> 0 资源 ->
任务 failed", 而用户只看到"失败", 完全看不出是路径不对。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from collectors import gallery_base as G
from collectors.xchina.gallery import XCHINA

GID = "69ad45698f836"


class Routes:
    """按 URL 前缀决定响应: 只有落在 ok_prefix 下的才算真资源。"""

    def __init__(self, ok_prefix, width=None):
        self.ok_prefix = ok_prefix
        self.width = width  # 仅当序号位数完全等于它时才算命中
        self.seen = []

    def head(self, url, **kw):
        self.seen.append(url)
        ok = url.startswith(self.ok_prefix)
        if ok and self.width is not None:
            tail = url.rsplit("/", 1)[-1].split(".")[0]
            ok = len(tail) == self.width
        return type("R", (), {
            "status_code": 200,
            "headers": {
                # 越界 URL 的指纹: 200 但返回 html(见 MEMORY 的站点特征)
                "Content-Type": "image/jpeg" if ok else "text/html",
                "Content-Length": "656713" if ok else "1024",
            },
        })()


def test_hint_reads_base_and_width_from_direct_link():
    """用户粘的直链是最可靠证据: 基址和序号宽度都写在 URL 里。"""
    h = G.parse_resource_hint(
        XCHINA, "https://img.xchina.io/photos2/69ad45698f836/0001.jpg")
    assert h is not None
    assert h["base"] == "https://img.xchina.io/photos2"
    assert h["seq_format"] == "{seq:04d}", "0001 说明补 4 位零"


def test_hint_ignores_page_urls_and_bare_ids():
    for raw in ["https://xchina.co/photo/id-69ad45698f836.html", GID, "", "随便"]:
        assert G.parse_resource_hint(XCHINA, raw) is None


def test_resolve_base_finds_photos2_when_photos_misses():
    """核心修复: 默认 base 不中时能自己找到 photos2。"""
    sess = Routes("https://img.xchina.io/photos2/")
    base, fmt = G._resolve_base(XCHINA, GID, sess, "image")
    assert base == "https://img.xchina.io/photos2"
    assert fmt == "{seq:05d}", "未配置 base_candidates 时宽度不变"


def test_resolve_base_falls_back_to_default_when_nothing_matches():
    """所有候选都不中 -> 退回默认, 由「连续缺失 -> 空结果 -> failed」处理。"""
    sess = Routes("https://nowhere.example/")
    base, fmt = G._resolve_base(XCHINA, GID, sess, "image")
    assert base == XCHINA.media("image").root(XCHINA)


def test_resolve_base_respects_probe_budget():
    """真不存在的图集不该被试探十几轮。"""
    class Count(Routes):
        pass

    sess = Count("https://nowhere.example/")
    G._resolve_base(XCHINA, GID, sess, "image", budget=2)
    assert len(sess.seen) <= 2, "探测次数必须受 budget 约束"


def test_discover_recovers_album_on_photos2():
    """端到端: 相册页 URL 输入, 资源其实在 photos2 —— 不能采到 0 个。"""
    sess = Routes("https://img.xchina.io/photos2/")
    items = list(G.discover(XCHINA, GID, session=sess, max_count=3,
                            min_interval=0, max_interval=0))
    assert len(items) == 3, f"相册在 photos2 时应正常枚举, 实际 {len(items)}"
    assert all("/photos2/" in i["url"] for i in items)


def test_discover_uses_hint_without_extra_probe():
    """有直链线索时零额外开销地用对基址与宽度。"""
    sess = Routes("https://img.xchina.io/photos2/", width=4)
    link = "https://img.xchina.io/photos2/69ad45698f836/0001.jpg"
    items = list(G.discover(XCHINA, GID, session=sess, max_count=2,
                            min_interval=0, max_interval=0, hint_url=link))
    assert len(items) == 2
    assert items[0]["url"].endswith("/0001.jpg"), "序号宽度必须是 4 位"
    assert items[0]["filename"].endswith("0001.jpg")


def test_discover_no_extra_request_on_happy_path():
    """相册就在默认基址上时, 不应为"探测"多花任何请求。"""
    sess = Routes("https://img.xchina.io/photos/")
    list(G.discover(XCHINA, GID, session=sess, max_count=3,
                    min_interval=0, max_interval=0))
    # 3 个序号各 1 次探测, 一次都不能多
    assert len(sess.seen) == 3, f"happy path 不该有额外请求: {sess.seen}"


def test_match_score_claims_direct_link_on_every_cdn_path():
    """autoresolve 必须认领 photos2/photos3 上的直链。

    ⚠️ 曾只拿 site.base 做路径前缀比对 -> /photos2/... 前缀不匹配 -> 不认领
    -> 派给 generic -> "该采集器不支持预览" 400。候选基址要一起比。
    """
    for p in ("photos", "photos2", "photos3"):
        u = f"https://img.xchina.io/{p}/{GID}/0001.jpg"
        assert G._match_score(XCHINA, u) == G.SCORE_RESOURCE_URL, f"未认领 {u}"


def test_match_score_still_rejects_other_paths_on_same_host():
    """同域但非资源路径不能认领(宁可交给 generic)。"""
    for u in [f"https://img.xchina.io/gallery/{GID}", "https://img.xchina.io/"]:
        assert G._match_score(XCHINA, u) is None


def test_seq_width_variants_cover_neighbours():
    out = G._seq_format_variants("{seq:05d}")
    assert "{seq:05d}" in out
    assert "{seq:04d}" in out and "{seq:06d}" in out
