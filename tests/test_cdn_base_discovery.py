"""CDN 基址 / 序号宽度自动发现(不触网)。

背景: 站点会按相册把资源分到 `photos` / `photos2` / `photos3` 等不同子路径,
甚至补零位数也不同(既有 `00001.jpg` 也有 `0001.jpg`)。写死一个 base 的后果是
"相册明明存在, 枚举的却是另一条不存在的路径 -> 全部 missing -> 0 资源 ->
任务 failed", 而用户只看到"失败", 完全看不出是路径不对。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from collectors import gallery_base as G
from collectors.xchina.gallery import XCHINA, XChinaGallerySpider

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

    def close(self):
        """discover 在自建 session 时会关它(测试里传进来的假 session 也一样)。"""


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


def test_xchina_opts_into_browser_tls_impersonation():
    """The WSL transport must match Chromium or the live CDN returns 403."""
    assert XCHINA.browser_impersonation == "chrome"


# ---- 候选基址: 单一来源 + 自动展开 ----

def test_base_candidates_expands_digit_suffixes():
    """数字后缀靠自动展开, 不再手写三个 —— 手写的话下次出现 photos4 就整批判空。"""
    out = G._base_candidates(XCHINA)
    assert out[0] == "https://img.xchina.io/photos", "默认基址必须排第一"
    assert "https://img.xchina.io/photos2" in out
    assert "https://img.xchina.io/photos5" in out


def test_base_candidates_dedup_and_keep_order():
    site = G.GallerySite(
        name="t", base="https://c/photos", variants=[".jpg"],
        base_candidates=["https://c/other", "https://c/photos2"],
        base_candidate_digits=2,
    )
    assert G._base_candidates(site) == [
        "https://c/photos", "https://c/other", "https://c/photos2",
    ]


def test_resolve_base_reaches_photos2_four_digits_beyond_old_budget():
    """回归: 候选扩到 5 个后, photos2 + 4 位 落在第 7 个组合上。

    旧版把预算写死 6(按"3 基址 × 2 宽度"定的), 这个组合根本轮不到 ->
    静默退回默认基址 -> 0 资源 -> failed —— 正是这套探测要消灭的那个坑。
    """
    sess = Routes("https://img.xchina.io/photos2/", width=4)
    base, fmt = G._resolve_base(XCHINA, GID, sess, "image")
    assert base == "https://img.xchina.io/photos2"
    assert fmt == "{seq:04d}"


# ---- ID 形状: 自动识别必须用最严判据 ----

def test_gid_shape_blocks_path_words_in_strict_mode():
    """`featured` 含 t/u/r, 不是十六进制 —— 手选时放行, 自动识别必须拒绝。

    放行的后果是去枚举一个不存在的图集, 表现为"成功但 0 个资源"(静默);
    拒绝的后果只是"请手动选择采集器"(响亮)。两种错代价不对称, 所以选后者。
    """
    u = "https://img.xchina.io/photos/featured/0001.jpg"
    assert G.parse_gid(XCHINA, u) == "featured", "用户手选采集器时不该被卡住"
    assert G.parse_gid(XCHINA, u, strict=True) is None
    assert G._match_score(XCHINA, u) is None


def test_gid_shape_accepts_real_ids_on_every_form():
    for u in [
        "https://img.xchina.io/photos2/69ad45698f836/0001.jpg",
        "https://xchina.co/photo/id-6aa5136f606fe.html",
        "69ad45698f836",
    ]:
        assert G.parse_gid(XCHINA, u, strict=True), f"{u} 不该被形状规则挡下"


def test_direct_link_regex_requires_a_media_filename_after_gid():
    """只抓中间一段会把路径词当 gid; 必须锚定到「gid / 文件.扩展名」。"""
    assert G.parse_gid(XCHINA, "https://img.xchina.io/photos/featured/",
                       strict=True) is None


def test_hint_survives_variant_suffix():
    """直链可能带变体后缀(00046_600x0.webp), 不能因"不是纯扩展名"白丢这条线索。"""
    h = G.parse_resource_hint(
        XCHINA, "https://img.xchina.io/photos/6aa5136f606fe/00046_600x0.webp")
    assert h is not None
    assert h["base"] == "https://img.xchina.io/photos"
    assert h["seq_format"] == "{seq:05d}"


# ---- 0 资源必须大声失败, 且给出下一步 ----

def test_empty_hint_uses_album_declared_counts():
    msg = G._empty_hint(XCHINA, GID, ["image"], {"photos": 12, "videos_declared": 4})
    assert "12 张图" in msg and "4 段视频" in msg, "自报数量是判断「是不是路径问题」的关键"
    assert "路径" in msg


def test_crawl_raises_actionable_error_when_nothing_found(monkeypatch):
    """旧行为是返回空列表, 上层只记"采集到 0 个资源" —— 用户无从下手。"""
    spider = XChinaGallerySpider()
    # 不真去读相册页(那会开浏览器); 这里只验"一个都没采到"时的报错质量
    monkeypatch.setattr(spider, "read_album_meta", lambda *a, **kw: None)
    monkeypatch.setattr(G, "discover", lambda *a, **kw: iter(()))
    with pytest.raises(ValueError) as ei:
        spider.crawl(
            "https://xchina.co/photo/id-6aa5136f606fe.html",
            options={"album_title": "id", "media": "image"},
        )
    msg = str(ei.value)
    assert "6aa5136f606fe" in msg, "要说清是哪个图集"
    assert "photos" in msg, "要列出试过的基址, 否则用户无从下手"
    assert "直链" in msg, "要给出下一步动作, 而不是只报失败"


# ---- 相册页 HTML 里的直链: 最硬的那份证据 ----

def test_page_hint_beats_probing():
    """相册页自己引用了真实图片地址 -> 直接采信, 一次候选探测都不做。

    页面写着 `photos2/.../0001.jpg` 时, 靠候选探测要试到第 7 个组合才命中
    (见 test_resolve_base_reaches_photos2_four_digits_beyond_old_budget);
    读页面能把这个代价清零, 而且能发现候选清单里没有的新子路径。
    """
    sess = Routes("https://img.xchina.io/photos2/", width=4)
    items = list(G.discover(
        XCHINA, GID, session=sess, max_count=2, min_interval=0, max_interval=0,
        page_hints=[f"https://img.xchina.io/photos2/{GID}/0001.jpg"],
    ))
    assert len(items) == 2
    assert items[0]["url"].endswith("/0001.jpg"), "4 位序号来自页面线索"
    # 2 个序号各 1 次探测 —— 没有为"找基址"额外花任何请求
    assert len(sess.seen) == 2, f"页面线索应零探测开销: {sess.seen}"


def test_page_hint_from_another_album_is_ignored():
    """页面里混着推荐位别的相册的图, 采信错了就把枚举引向不存在的路径。"""
    other = "https://img.xchina.io/photos3/deadbeefcafe/0001.jpg"
    assert G._first_hint(XCHINA, GID, [other]) is None
    # 自己的那条仍然认得出
    mine = f"https://img.xchina.io/photos2/{GID}/0001.jpg"
    assert G._first_hint(XCHINA, GID, [other, mine])["base"].endswith("photos2")


def test_user_direct_link_still_wins_over_page():
    """用户直链 > 页面线索: 页面上是缩略图路径时直链更贴近他要的那份。"""
    sess = Routes("https://img.xchina.io/photos2/", width=4)
    items = list(G.discover(
        XCHINA, GID, session=sess, max_count=1, min_interval=0, max_interval=0,
        hint_url=f"https://img.xchina.io/photos2/{GID}/0001.jpg",
        page_hints=[f"https://img.xchina.io/photos/{GID}/00001.jpg"],
    ))
    assert items[0]["url"].endswith("/0001.jpg")


def test_extract_album_meta_reports_resource_urls():
    """页面解析本身要把媒体直链带出来, 否则线索链路第一步就断了。"""
    import collectors.album_meta as AM

    html = (
        '<html><head><title>相册 - 分类 - 站名</title></head><body>'
        f'<div class="photo-items"></div>'
        f'<img src="https://img.xchina.io/photos2/{GID}/0001.jpg">'
        f'<script>var favOptions = {{"objId": "{GID}"}};</script>'
        "</body></html>"
    )
    meta = AM.extract_album_meta(html, page_url="https://xchina.co/photo/x.html")
    assert any(GID in u and "photos2" in u for u in meta["resource_urls"])


# ---- 预览: 预告必须与实际落盘一致 ----

def _meta(**extra):
    m = {"album": "相册名", "title": "相册名 - 分类", "h1": "相册名",
         "obj_id": GID, "videos": [], "photos": None, "videos_declared": None,
         "maker": "", "tags": [], "challenge": False, "resource_urls": []}
    m.update(extra)
    return m


def _preview_with_meta(monkeypatch, meta, **opts):
    spider = XChinaGallerySpider()
    monkeypatch.setattr(spider, "read_album_meta", lambda *a, **kw: meta)
    return spider.preview(f"https://xchina.co/photo/id-{GID}.html", opts)


def test_preview_sample_name_uses_page_seq_width(monkeypatch):
    """零枚举路径下示例文件名也要用页面的序号位数。

    写死 5 位会让用户在核对命名时看到 `00001.jpg`, 而实际落盘是 `0001.jpg` ——
    他会以为采集器改错了文件名, 跑去查一个根本不存在的问题。
    """
    meta = _meta(photos=12,
                 resource_urls=[f"https://img.xchina.io/photos2/{GID}/0001.jpg"])
    d = _preview_with_meta(monkeypatch, meta, media="image")
    assert d["sampled"] is False
    assert d["sample_files"][0].endswith("0001.jpg"), d["sample_files"]
    assert d["resource_roots"]["image"]["base"].endswith("photos2")
    assert d["resource_roots"]["image"]["seq_format"] == "{seq:04d}"


def test_preview_reports_resource_roots_after_enumeration(monkeypatch):
    """走枚举的路径由 diag 给出实际生效的根, 这是"为什么没采到"的第一手证据。"""
    sess = Routes("https://nowhere.example/")
    monkeypatch.setattr(G, "_session", lambda proxy=None, impersonate=None: sess)
    d = _preview_with_meta(monkeypatch, _meta(), media="image")
    assert d["sampled"] is True
    got = d["resource_roots"]["image"]
    assert got["base"] == XCHINA.media("image").root(XCHINA)
    assert got["source"] == "default", "没命中任何候选时应如实标注来源"


def test_preview_reports_hint_source_when_not_enumerating(monkeypatch):
    """零枚举(页面自报数量)时也要知道用的是哪条 CDN 路径, 并标明来源是线索。"""
    meta = _meta(photos=12,
                 resource_urls=[f"https://img.xchina.io/photos2/{GID}/0001.jpg"])
    d = _preview_with_meta(monkeypatch, meta, media="image")
    got = d["resource_roots"]["image"]
    assert got["base"].endswith("photos2")
    assert got["seq_format"] == "{seq:04d}"
    assert got["source"] == "hint"


def test_preview_warns_when_enumeration_finds_nothing(monkeypatch):
    """采不到时不能只回一个 0, 要把"下一步做什么"一并给出。"""
    sess = Routes("https://nowhere.example/")
    monkeypatch.setattr(G, "_session", lambda proxy=None, impersonate=None: sess)
    d = _preview_with_meta(monkeypatch, _meta(), media="image")
    assert d["warning"], "0 资源的预告必须带上可行动提示"
    assert GID in d["warning"]
    assert "nowhere" not in d["warning"], "报的是试过的**站点**基址, 不是假 session"


def test_preview_no_warning_on_normal_photo_album(monkeypatch):
    """正常情况下不该乱报警告。"""
    meta = _meta(photos=12, resource_urls=[])
    d = _preview_with_meta(monkeypatch, meta, media="image")
    assert d["warning"] == ""
