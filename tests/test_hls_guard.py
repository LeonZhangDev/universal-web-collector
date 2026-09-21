"""HLS 播放列表预检与"假成功"防护测试(不触网、用假响应)。

最阴险的失败模式
==============
video.xchina.download 这类站点, 带签名 m3u8 过期/无效时**不报错**:
返回 200 + 语法合法的 m3u8, 指向一个 603KB 真实可播放的占位 TS。
ffmpeg / 内置分片器会一路畅通地下完、退出码 0、任务报 success ——
用户只拿到几十秒占位画面。本测试确保下载前预检能拦下这种"假成功"。

同时覆盖: 过期凭证(URL 里带 expires)、加密流密钥可不可达、master 下钻。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest

from collectors.hls import inspect_playlist, playlist_expires_at, seconds_left
from downloaders.video import VideoDownloader


# ---- 假 session: 按 URL 路由返回不同响应 ----
class FakeResp:
    def __init__(self, text, status=200, content=None, headers=None):
        self.text = text
        self.status_code = status
        self._content = content if content is not None else text.encode()
        self.headers = headers or {}

    @property
    def content(self):
        return self._content


class FakeSession:
    """把 URL 子串映射到响应体。需要时返回 16 字节密钥。"""

    def __init__(self, routes):
        # 形如 {"720.m3u8": "<body>", "enc.key": b"...16 bytes..."}
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, timeout=30, **kw):
        self.calls.append(url)
        for needle, body in self.routes.items():
            if needle in url:
                if isinstance(body, (bytes, bytearray)):
                    return FakeResp("", content=bytes(body))
                return FakeResp(body)
        return FakeResp("not found", status=404)


# ---- inspect_playlist 直接测 ----
def test_expired_signature_is_flagged():
    # 1 小时前就过期了(用真实时间戳, 别用 1970 年的极小值 —— 那会被安全边界拒绝)
    url = f"https://video.xchina.download/m3u8/abc/720.m3u8?expires={int(time.time()) - 3600}&md5=x"
    r = inspect_playlist(url, session=FakeSession({}))
    assert r["ok"] is False
    assert r["kind"] == "expired"
    assert "过期" in r["reason"]


def test_placeholder_playlist_is_rejected():
    body = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:20\n"
        "#EXTINF:20,\n/fallback/placeholder.ts\n#EXT-X-ENDLIST\n"
    )
    url = "https://video.xchina.download/m3u8/abc/720.m3u8?expires=9999999999&md5=x"
    r = inspect_playlist(url, session=FakeSession({"720.m3u8": body}))
    assert r["ok"] is False
    assert r["kind"] == "placeholder"
    assert "占位" in r["reason"]


def test_valid_encrypted_playlist_passes_and_checks_key():
    body = (
        "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"https://h/key/enc.key\"\n"
        "#EXTINF:5.0,\nhttps://h/ts/00001.ts\n#EXT-X-ENDLIST\n"
    )
    routes = {
        "720.m3u8": body,
        "enc.key": b"0123456789abcdef",  # 16 字节 AES-128 密钥
    }
    url = "https://h/720.m3u8?expires=9999999999&md5=x"
    r = inspect_playlist(url, session=FakeSession(routes))
    assert r["ok"] is True
    assert r["encrypted"] is True
    assert r["key_bytes"] == 16
    assert r["count"] == 1
    assert r["segments"][0].endswith("00001.ts")
    assert r["endlist"] is True


@pytest.mark.parametrize("playlist_type", ["", "VOD", "EVENT"])
def test_leaf_playlist_without_endlist_is_rejected(playlist_type):
    kind = f"#EXT-X-PLAYLIST-TYPE:{playlist_type}\n" if playlist_type else ""
    body = f"#EXTM3U\n{kind}#EXTINF:5.0,\nhttps://h/ts/00001.ts\n"
    r = inspect_playlist(
        "https://h/720.m3u8?expires=9999999999&md5=x",
        session=FakeSession({"720.m3u8": body}),
    )

    assert r["ok"] is False
    assert r["kind"] in {"live", "unfinished"}
    assert "ENDLIST" in r["reason"]


def test_encrypted_but_key_unreachable_is_rejected():
    body = (
        "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"https://h/key/enc.key\"\n"
        "#EXTINF:5.0,\nhttps://h/ts/00001.ts\n#EXT-X-ENDLIST\n"
    )
    routes = {"720.m3u8": body, "enc.key": b""}  # 拿不到密钥
    url = "https://h/720.m3u8?expires=9999999999&md5=x"
    r = inspect_playlist(url, session=FakeSession(routes))
    assert r["ok"] is False
    assert r["kind"] == "bad-key"


def test_master_playlist_drills_to_leaf_and_keeps_segments():
    master = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
        "https://h/720.m3u8?expires=9999999999&md5=x\n"
    )
    leaf = (
        "#EXTM3U\n#EXTINF:5.0,\nhttps://h/ts/00001.ts\n#EXT-X-ENDLIST\n"
    )
    routes = {"index": master, "720.m3u8": leaf}
    url = "https://h/index.m3u8?expires=9999999999&md5=x"
    # 下钻发生在 _preflight_hls 里(inspect_playlist 本身故意不下钻, 只判本层)
    d = _downloader_with(FakeSession(routes))
    leaf_url, info = d._preflight_hls(url, {}, log=None)
    assert info["ok"] is True
    # master 本身没有分片; 下钻后才拿到
    assert info["count"] == 1
    assert info["segments"][0].endswith("00001.ts")
    assert "720.m3u8" in leaf_url


def test_non_playlist_response_rejected():
    routes = {"720.m3u8": "<html>login required</html>"}
    url = "https://h/720.m3u8?expires=9999999999&md5=x"
    r = inspect_playlist(url, session=FakeSession(routes))
    assert r["ok"] is False
    assert r["kind"] == "not-playlist"


def test_playlist_expires_at_parses_seconds_and_ms():
    assert playlist_expires_at("?expires=1700000000") == 1700000000
    # 毫秒级时间戳
    assert playlist_expires_at("?expires=1700000000000") == 1700000000
    # 不可信的极小值(1970 年附近)被安全边界拒绝, 不会把乱码数字当过期时间
    assert playlist_expires_at("?expires=100") is None
    assert playlist_expires_at("?foo=1") is None
    assert seconds_left(f"?expires={int(time.time()) + 3600}") > 0


# ---- VideoDownloader._preflight_hls: 拦截"假成功" ----
def _downloader_with(session):
    d = VideoDownloader()
    d.session = session
    return d


def test_preflight_rejects_placeholder_before_download():
    body = (
        "#EXTM3U\n#EXTINF:20,\n/fallback/placeholder.ts\n#EXT-X-ENDLIST\n"
    )
    url = "https://video.xchina.download/m3u8/abc/720.m3u8?expires=9999999999&md5=x"
    d = _downloader_with(FakeSession({"720.m3u8": body}))
    with pytest.raises(RuntimeError) as ei:
        d._preflight_hls(url, {}, log=None)
    assert "占位" in str(ei.value) or "校验未通过" in str(ei.value)


def test_preflight_rejects_expired_before_download():
    url = f"https://video.xchina.download/m3u8/abc/720.m3u8?expires={int(time.time()) - 3600}&md5=x"
    d = _downloader_with(FakeSession({}))
    with pytest.raises(RuntimeError) as ei:
        d._preflight_hls(url, {}, log=None)
    assert "过期" in str(ei.value)


def test_preflight_accepts_valid_leaf():
    body = (
        "#EXTM3U\n#EXTINF:5.0,\nhttps://h/ts/00001.ts\n#EXT-X-ENDLIST\n"
    )
    url = "https://h/720.m3u8?expires=9999999999&md5=x"
    d = _downloader_with(FakeSession({"720.m3u8": body}))
    leaf, info = d._preflight_hls(url, {}, log=None)
    assert info["ok"] is True
    assert info["count"] == 1
    assert leaf == url


def test_full_m3u8_download_rejects_placeholder(tmp_path, monkeypatch):
    """端到端: 把占位链接喂给 VideoDownloader.download, 必须响亮失败,
    绝不能下出一个占位视频还报 success。"""
    body = (
        "#EXTM3U\n#EXTINF:20,\n/fallback/placeholder.ts\n#EXT-X-ENDLIST\n"
    )
    url = "https://video.xchina.download/m3u8/abc/720.m3u8?expires=9999999999&md5=x"
    # 强制 builtin 引擎(无 ffmpeg 干扰), 但预检在引擎分发前就拦下
    monkeypatch.setattr("downloaders.video.settings.video_engine", "builtin")
    d = _downloader_with(FakeSession({"720.m3u8": body}))
    with pytest.raises(RuntimeError) as ei:
        d.download(url, save_dir=str(tmp_path), log=None)
    assert "校验" in str(ei.value) or "占位" in str(ei.value)
    # 绝不能落下任何文件
    assert not any(tmp_path.iterdir())


def test_builtin_encrypted_without_ffmpeg_is_rejected(tmp_path, monkeypatch):
    """加密流若走内置分片器(无 ffmpeg 解密)会产出密文垃圾 .ts —— 必须提前报错。"""
    body = (
        "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"https://h/key/enc.key\"\n"
        "#EXTINF:5.0,\nhttps://h/ts/00001.ts\n#EXT-X-ENDLIST\n"
    )
    routes = {"720.m3u8": body, "enc.key": b"0123456789abcdef"}
    url = "https://h/720.m3u8?expires=9999999999&md5=x"
    monkeypatch.setattr("downloaders.video.settings.video_engine", "builtin")
    # find_ffmpeg 返回 None, 模拟"没装 ffmpeg"
    monkeypatch.setattr("downloaders.video.find_ffmpeg", lambda: None)
    d = _downloader_with(FakeSession(routes))
    with pytest.raises(RuntimeError) as ei:
        d.download(url, save_dir=str(tmp_path), log=None)
    assert "无法解密" in str(ei.value)
