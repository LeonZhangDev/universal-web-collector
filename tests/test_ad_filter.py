"""广告位 / 站点装饰 / 跟踪像素识别(不触网)。

核心约束: **一律路径分段精确匹配, 绝不做子串包含**。
子串匹配会把 `/photos2/my-logo-album/0001.jpg` 这种正常相册误杀,
而"静默少采几张"比"多采一张广告"难查得多 —— 宁可漏判也不误杀。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.filters import Filters, _match_ad

AD_URLS = [
    "https://cdn.example/ad/banner.jpg",
    "https://cdn.example/ads/728x90.png",
    "https://cdn.example/static/images/logo.png",
    "https://cdn.example/img/icon.png",
    "https://cdn.example/sprite.png",
    "https://cdn.example/track/pixel.gif",
    "https://cdn.example/1x1.gif",
    "https://cdn.example/spacer.gif",
    "https://cdn.example/promo/x.jpg",
]

# 这些是**正常资源**, 一条都不能被误杀 —— 它们是"为什么不能子串匹配"的理由
KEEP_URLS = [
    "https://img.xchina.io/photos2/69ad45698f836/0001.jpg",
    "https://img.xchina.io/photos/6aa5136f606fe/00001.jpg",
    # 相册名里带 logo/icon/ad 等字样: 子串匹配会误杀
    "https://img.xchina.io/photos/my-logo-album/00001.jpg",
    "https://img.xchina.io/photos/iconic-model/00001.jpg",
    "https://img.xchina.io/photos/advertise-me/00001.jpg",
    # 文件名是纯数字序号(不是 1x1 那种尺寸写法)
    "https://img.xchina.io/photos/abc123/00001.jpg",
]


def test_ad_urls_are_rejected():
    for u in AD_URLS:
        assert _match_ad(u), f"广告/装饰 URL 应被识别: {u}"


def test_normal_resources_are_never_rejected():
    for u in KEEP_URLS:
        assert _match_ad(u) is None, f"正常资源被误杀: {u}"


def test_exclude_ad_defaults_on():
    f = Filters({})
    assert f.exclude_ad is True
    assert f.match_resource("image", "https://cdn.example/static/logo.png") is not None


def test_exclude_ad_can_be_disabled():
    """有用户就是要抓站点 logo: 关掉后连广告位都不拦。"""
    f = Filters({"exclude_ad": False})
    assert f.exclude_ad is False
    assert _match_ad("https://cdn.example/ad/banner.jpg") is not None  # 函数本身不变
    assert f.match_resource("image", "https://cdn.example/ad/banner.jpg") is None


def test_disable_variants():
    for v in ("false", "0", "no", "off", False, 0):
        assert Filters({"exclude_ad": v}).exclude_ad is False
    for v in ("true", "1", "yes", "on", True, 1):
        assert Filters({"exclude_ad": v}).exclude_ad is True


def test_ad_check_runs_before_size_check():
    """URL 层判定比体积探测更省, 应该在前面。"""
    f = Filters({"min_image_bytes": "1KB"})
    reason = f.match_resource("image", "https://x/track.gif", size=150)
    assert "装饰" in reason or "像素" in reason, f"应由 URL 层先拦: {reason}"
