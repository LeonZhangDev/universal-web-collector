"""`scripts/add_site.py` 加站门禁的自检。

为什么这些用例是**必要的**(而不是"顺手补的覆盖")
==================================================
门禁脚本本身的失败方式是"**它什么也没查, 却印着一行 ok**" —— 与第 18 条坑同源:
该断言的地方没断言。所以每一条闸都必须有一个**故意造坏**的输入, 证明它会报错:
闸只要全是"通过"的用例, 就没法区分"查了没问题"和"根本没查"。

另一个专门用例是 `pexels`: 它有 `site` 声明却不是 `SequenceGallerySpider`, 于是
四道闸会在一份**不存在的 URL** 图案上"全绿"。这正是本项目反复踩的家族 F(通过但
理由已经不对), 所以要用一条用例把它钉死 —— 宁可不验, 不给假绿。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import add_site  # noqa: E402
from collectors.gallery_base import GallerySite  # noqa: E402

GID = "6aa5136f606fe"


def _site(**over):
    """一个最小的、能通过全部闸的声明; 用例只改坏其中一个字段。"""
    kw = dict(
        name="demo",
        base="https://cdn.example.com/photos",
        variants=[".jpg"],
        seq_format="{seq:05d}",
        gid_shape=r"[0-9a-f]{8,}",
        id_patterns=[r"/photos/([0-9a-f]{8,})/"],
        id_samples=[("https://cdn.example.com/photos/%s/00001.jpg" % GID, GID)],
    )
    kw.update(over)
    return GallerySite(**kw)


# ------------------------------------------------------------ 闸 1 声明自洽


def test_gate1_passes_on_a_self_consistent_declaration():
    assert add_site.gate_declaration(_site()) == []


def test_gate1_catches_a_sample_that_parses_to_a_different_gid():
    """样本期望值与正则解析结果不一致 —— 这是"站点换 ID 形态"的第一现场。"""
    s = _site(id_samples=[("https://cdn.example.com/photos/ffffffffffff/00001.jpg", GID)])
    problems = add_site.gate_declaration(s)
    assert problems, "样本与正则矛盾却报 ok = 这一闸没在查"
    assert any("期望" in p for p in problems)


# ------------------------------------------------------------ 闸 2 认领


def test_gate2_catches_a_url_no_specialised_collector_claims():
    """没人认领 = 会静默落到通用采集器, 行为完全不同。

    用 `.invalid` 顶级域造一个**任何采集器都认不了**的 URL。
    """
    s = _site(id_samples=[("https://cdn.example.invalid/photos/%s/00001.jpg" % GID, GID)])
    problems = add_site.gate_claim(s, "demo")
    assert problems
    assert any("没有专用采集器认领" in p for p in problems), problems


def test_gate2_catches_a_url_stolen_by_another_site():
    """样本 URL 其实是**别人**的 —— 传错名字 / 正则写太宽都会这样。"""
    seq_sites, _others = add_site.registered_sites()
    if "xchina_gallery" not in seq_sites:
        return                                     # 站点没注册就没什么可验的
    real = seq_sites["xchina_gallery"]
    problems = add_site.gate_claim(real, "some_other_site")   # 故意报错的名字
    assert problems
    assert any("抢先认领" in p for p in problems), problems


def test_gate2_says_so_when_there_is_nothing_to_verify():
    """没有 URL 样本时**不能说 ok** —— "没验到"与"验过了没问题"是两件事。"""
    s = _site(id_samples=[(GID, GID)])             # 只有纯 ID 形态
    problems = add_site.gate_claim(s, "demo")
    assert problems
    assert any("没有验到" in p for p in problems), problems


# ------------------------------------------------------------ 闸 3 过滤


def test_gate3_passes_on_an_ordinary_cdn_path():
    problems, rows = add_site.gate_filter(_site())
    assert problems == [], problems
    assert rows and rows[0][2] is None             # 第三格是 match_resource 的原因


def test_gate3_catches_a_path_segment_that_looks_like_an_ad():
    """资源路径里有个 `banner` 段 -> 默认过滤规则会**全部**拦掉 -> 任务 success + 0 资源。

    这是"静默少采"里最难查的一种: 探针、声明自洽、认领三道闸全绿, 唯独资源一个都
    下不来。所以要专门造一个。判据用的是 filters 里的 `_AD_SEGMENTS`(路径分段精确
    匹配), 不是子串 —— 造 `banner` 整段, 而不是 `my-banner-album`。
    """
    s = _site(base="https://cdn.example.com/banner")
    problems, _rows = add_site.gate_filter(s)
    assert problems, "广告段被静默过滤却报 ok = 这一闸没在查"
    assert any("被默认过滤规则拦下" in p for p in problems), problems


def test_gate3_says_so_when_no_url_can_be_built():
    s = _site(id_samples=[("https://cdn.example.com/photos/%s/00001.jpg" % GID, "")])
    problems, rows = add_site.gate_filter(s)
    assert problems and rows == []
    assert any("没有验到" in p for p in problems), problems


# ------------------------------------------------------------ 闸 4 落盘/命名


def test_gate4_keeps_the_album_folder_and_flattens_video():
    s = _site(video_variants=[".mp4"])
    problems, rows = add_site.gate_layout(s)
    assert problems == [], problems
    paths = dict(rows)
    assert paths["image"] == "%s/00001.jpg" % add_site._ALBUM     # 图片进相册文件夹
    assert "/" not in paths["video"]                              # 视频平铺
    assert paths["claim"] != "%s/00001.jpg" % add_site._ALBUM     # 撞名被消解


def test_gate4_catches_a_sequence_format_that_cannot_render():
    """`seq_format` 写错的表现是"每张都判 MISSING -> 0 资源 -> failed", 用户只看到失败。"""
    s = _site(seq_format="{seq:05d}{oops}")
    problems, _rows = add_site.gate_layout(s)
    assert problems
    assert any("无法渲染" in p for p in problems), problems


# ------------------------------------------------------------ 站点选择器


def test_selector_excludes_sites_that_are_not_sequence_based():
    """`pexels` 有 `site` 声明但**不是** `SequenceGallerySpider` —— 必须被排除。

    否则四道闸会在 `url_for` 拼出的一个**根本不存在**的 URL 图案上"全绿"。这是
    家族 F(通过但理由已经不对): 比失败更危险, 因为它给的是**假的信心**。
    """
    seq_sites, others = add_site.registered_sites()
    assert "xchina_gallery" in seq_sites
    assert "pexels" not in seq_sites, "非序号枚举型站点混进了本门禁"
    assert "pexels" in others or not others       # 允许注册表变化, 但不许错判
    for name, site in seq_sites.items():
        assert getattr(site, "variants", None) is not None, name
