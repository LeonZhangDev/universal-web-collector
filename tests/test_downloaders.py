from downloaders.base import build_headers, safe_filename
import pytest
import requests

import downloaders.video as video_mod
from downloaders.browser_session import browser_session_for
from downloaders.image import ImageDownloader, _session_for
from downloaders.video import VideoDownloader


def test_safe_filename():
    assert safe_filename("https://a.com/dir/pic.jpg?token=1&x=2") == "pic.jpg"
    assert safe_filename("https://a.com/dir/pic%20name.jpg") == "pic name.jpg"
    no_ext = safe_filename("https://a.com/noext")
    assert no_ext.endswith(".bin")


def test_build_headers():
    h = build_headers(referer="https://x.com", extra={"cookie": "a=1"})
    assert h["Referer"] == "https://x.com"
    assert h["Cookie"] == "a=1"
    assert "User-Agent" in h


def test_downloader_interfaces():
    assert callable(ImageDownloader().download)
    assert callable(VideoDownloader().download)


def test_xchina_images_use_browser_tls_session():
    assert _session_for("https://example.com/pic.jpg") is None
    session = _session_for("https://img.xchina.io/photos/id/0001.jpg")
    try:
        assert session.impersonate == "chrome"
    finally:
        session.close()


@pytest.mark.parametrize(
    ("url", "enabled"),
    [
        ("https://img.xchina.io/photos/id/0001.jpg", True),
        ("https://video.xchina.download/m3u8/id/720.m3u8", True),
        ("https://xchina.co/video/id-example.html", False),
        ("https://video.xchina.download.evil.example/x.m3u8", False),
        ("https://example.com/video.xchina.download/x.m3u8", False),
        ("http://video.xchina.download/x.m3u8", False),
    ],
)
def test_browser_tls_session_is_limited_to_exact_xchina_media_hosts(url, enabled):
    session = browser_session_for(url)
    try:
        assert (session is not None) is enabled
        if session is not None:
            assert session.impersonate == "chrome"
    finally:
        if session is not None:
            session.close()


def test_xchina_video_403_uses_browser_session_and_closes_it(monkeypatch, tmp_path):
    class BrowserSession:
        impersonate = "chrome"

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    session = BrowserSession()
    captured = {}
    monkeypatch.setattr(video_mod, "browser_session_for", lambda url: session)

    def reject_403(url, path, headers, **kwargs):
        captured.update(
            url=url,
            headers=headers,
            session=kwargs["session"],
            timeout=kwargs["request_timeout"],
        )
        raise requests.HTTPError("403 Client Error")

    monkeypatch.setattr(video_mod, "download_with_mirrors", reject_403)

    with pytest.raises(requests.HTTPError, match="403"):
        VideoDownloader().download(
            "https://video.xchina.download/media/example.mp4",
            referer="https://xchina.co/video/id-example.html",
            headers={"cookie": "session=kept"},
            save_dir=tmp_path,
        )

    assert captured["session"] is session
    assert captured["headers"]["Referer"] == "https://xchina.co/video/id-example.html"
    assert captured["headers"]["Cookie"] == "session=kept"
    assert captured["timeout"] == video_mod.SHUTDOWN_IO_TIMEOUT
    assert session.closed is True
