"""XChina 视频页采集器测试(不触网、不開浏览器)。

验证三件事:
1. 自动识别认领 `/video/` 路径与直链 m3u8, 但**不**认领裸 gid(交给图集采集器)。
2. `_capture` 通过注入的假浏览器运行器拿到 m3u8, 并跑预检拦截占位/过期。
3. `crawl`/`preview` 产出形状与下载层期望一致(含 size 估算, 避免 m3u8 几 KB 误杀)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest

from collectors import resolve_collector
from collectors.xchina.spider_video import XChinaVideoSpider, parse_video_gid


PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-KEY:METHOD=AES-128,URI=\"https://video.xchina.download/key/enc.key\"\n"
    "#EXTINF:5.0,\nhttps://video.xchina.download/ts/00001.ts\n"
    "#EXT-X-ENDLIST\n"
)
PLAYLIST_URL = "https://video.xchina.download/m3u8/abc/720.m3u8?expires=9999999999&md5=x"

PLACEHOLDER = (
    "#EXTM3U\n#EXTINF:20,\n/fallback/placeholder.ts\n#EXT-X-ENDLIST\n"
)
PLACEHOLDER_URL = "https://video.xchina.download/m3u8/abc/720.m3u8?expires=9999999999&md5=x"


class FakeResp:
    def __init__(self, url):
        self.url = url


class FakeSession:
    """路由: 720.m3u8 -> 清单; enc.key -> 16 字节; .ts -> 带 Content-Length。"""

    def __init__(self, playlist, key=b"0123456789abcdef"):
        self.playlist = playlist
        self.key = key

    def get(self, url, headers=None, timeout=30, **kw):
        if "enc.key" in url:
            return FakeResp2(self.key)
        if url.endswith(".ts"):
            return FakeResp2(b"x" * 896768, headers={"Content-Length": str(896768)})
        return FakeResp2(self.playlist)

    # estimate_size 用 HEAD 探分片大小; 假 session 用 GET 顶替即可
    head = get


class FakeResp2:
    def __init__(self, content, status=200, headers=None):
        self._c = content if isinstance(content, bytes) else content.encode()
        self.status_code = status
        self.headers = headers or {}
        self.text = self._c.decode("utf-8", "ignore")

    @property
    def content(self):
        return self._c


def runner_for(playlist_url):
    def _run(url, on_response):
        on_response(FakeResp(playlist_url))
        return {"title": "测试视频 title"}
    return _run


# ---- 自动识别 ----
def test_match_score_claims_video_page():
    s = XChinaVideoSpider.match_score("https://xchina.co/video/id-6aaa517d3f106.html")
    assert s is not None


def test_match_score_ignores_bare_gid():
    # 裸 gid 同时像图集又像视频, 视频采集器**不**认领, 交给图集采集器
    assert XChinaVideoSpider.match_score("6aaa517d3f106") is None
    assert XChinaVideoSpider.match_score("https://xchina.co/photo/id-6a3654854fd25.html") is None


def test_parse_video_gid():
    assert parse_video_gid("https://xchina.co/video/id-6aaa517d3f106.html") == "6aaa517d3f106"
    assert parse_video_gid("6aaa517d3f106") is None
    assert parse_video_gid("https://example.com/a") is None


def test_resolve_routes_video_url_to_xchina_video():
    r = resolve_collector("https://xchina.co/video/id-6aaa517d3f106.html")
    assert r["collector"] == "xchina_video"
    assert r["auto"] is True


# ---- _capture + 预检 ----
def test_capture_returns_playlist_and_title():
    sp = XChinaVideoSpider()
    m3u8, title, info = sp._capture(
        "https://xchina.co/video/id-6aaa517d3f106.html",
        browser_runner=runner_for(PLAYLIST_URL),
        session=FakeSession(PLAYLIST),
    )
    assert m3u8 == PLAYLIST_URL
    assert title == "测试视频 title"
    assert info["ok"] is True
    assert info["encrypted"] is True


def test_capture_rejects_placeholder_before_returning():
    sp = XChinaVideoSpider()
    with pytest.raises(RuntimeError) as ei:
        sp._capture(
            "https://xchina.co/video/id-6aaa517d3f106.html",
            browser_runner=runner_for(PLACEHOLDER_URL),
            session=FakeSession(PLACEHOLDER),
        )
    assert "占位" in str(ei.value) or "校验未通过" in str(ei.value)


def test_capture_rejects_when_no_m3u8_fired():
    sp = XChinaVideoSpider()
    def noop_runner(url, on_response):
        return {"title": "x"}
    with pytest.raises(RuntimeError) as ei:
        sp._capture(
            "https://xchina.co/video/id-6aaa517d3f106.html",
            browser_runner=noop_runner,
            session=FakeSession(PLAYLIST),
        )
    assert "未能" in str(ei.value)


# ---- crawl / preview 产出形状 ----
def test_crawl_yields_one_video_resource_with_size_estimate():
    sp = XChinaVideoSpider()
    res = sp.crawl(
        "https://xchina.co/video/id-6aaa517d3f106.html",
        session=FakeSession(PLAYLIST),
        browser_runner=runner_for(PLAYLIST_URL),
    )
    assert len(res) == 1
    r = res[0]
    assert r["type"] == "video"
    assert r["url"] == PLAYLIST_URL
    assert r["filename"].endswith(".mp4")
    # size 由 estimate_size 给出(单片 896768 x 1 片 ≈ 876KB), 不是 m3u8 的几 KB
    assert isinstance(r["size"], int) and r["size"] > 100_000


def test_preview_reports_video_only():
    sp = XChinaVideoSpider()
    d = sp.preview(
        "https://xchina.co/video/id-6aaa517d3f106.html",
        session=FakeSession(PLAYLIST),
        browser_runner=runner_for(PLAYLIST_URL),
    )
    assert d["gid"] == "6aaa517d3f106"
    assert d["media"] == ["video"]
    assert d["photos"] == 0
    assert d["videos"] == 1
    assert d["encrypted"] is True
    assert d["sample_files"][0].endswith(".mp4")
