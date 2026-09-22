import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.cancel import TaskCancelled
from core.task_manager import TaskManager
from downloaders.base import _stream_one, build_headers, safe_filename
import pytest
import requests

import downloaders.base as base_mod
import downloaders.image as image_mod
import downloaders.video as video_mod
import core.filters as filters_mod
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

    assert captured["session"]._session is session
    assert captured["headers"]["Referer"] == "https://xchina.co/video/id-example.html"
    assert captured["headers"]["Cookie"] == "session=kept"
    assert captured["timeout"] == video_mod.SHUTDOWN_IO_TIMEOUT
    assert session.closed is True


def test_retry_backoff_checks_cancellation_before_waiting(monkeypatch, tmp_path):
    class FailingSession:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError("offline")

    ticks = 0

    def cancel_on_retry():
        nonlocal ticks
        ticks += 1
        if ticks >= 2:
            raise TaskCancelled()

    monkeypatch.setattr("downloaders.base.random.uniform", lambda *_: 0.05)
    started = time.monotonic()
    with pytest.raises(TaskCancelled):
        _stream_one(
            "https://example.com/file.bin",
            tmp_path / "file.bin",
            {},
            retries=3,
            resume=False,
            sess=FailingSession(),
            progress_cb=cancel_on_retry,
        )

    assert time.monotonic() - started < 1


def test_image_download_uses_shutdown_bounded_read_timeout(monkeypatch, tmp_path):
    captured = {}

    class Session:
        def close(self):
            pass

    monkeypatch.setattr(image_mod, "_session_for", lambda _url: Session())

    def download(url, path, headers, **kwargs):
        captured["timeout"] = kwargs.get("request_timeout")
        return "hash", path

    monkeypatch.setattr(image_mod, "download_with_mirrors", download)
    ImageDownloader().download(
        "https://img.xchina.io/photos/id/0001.jpg", save_dir=tmp_path
    )

    assert captured["timeout"] is not None
    assert max(captured["timeout"]) <= 4


def test_size_probe_uses_bounded_timeout_and_closes_response(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        headers = {"Content-Length": "123"}
        closed = False

        def close(self):
            self.closed = True

    response = Response()

    class Session:
        def head(self, *args, **kwargs):
            captured["timeout"] = kwargs["timeout"]
            return response

    monkeypatch.setattr(base_mod, "SESSION", Session())

    assert filters_mod.probe_size("https://example.com/file.jpg") == 123
    assert max(captured["timeout"]) <= 4
    assert response.closed is True


def test_shutdown_closes_real_hanging_image_socket_within_gate(
    monkeypatch, tmp_path
):
    entered = threading.Event()
    release = threading.Event()

    class HangingHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", "999999")
            self.end_headers()
            self.wfile.write(b"\xff\xd8")
            self.wfile.flush()
            entered.set()
            release.wait(12)
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HangingHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    monkeypatch.setattr(base_mod.settings, "image_retries", 1)
    downloader = ImageDownloader()
    manager = TaskManager(max_workers=1, download_workers=1)
    future = manager._download_executor.submit(
        downloader.download,
        f"http://127.0.0.1:{server.server_port}/hang.jpg",
        None,
        tmp_path,
    )
    assert entered.wait(2), "test server never received the image request"
    entry = {
        "future": None,
        "cancel": threading.Event(),
        "hb": time.monotonic(),
        "closers": {getattr(downloader, "close", lambda: None)},
    }
    with manager._active_lock:
        manager._active[987] = entry

    try:
        started = time.monotonic()
        manager.shutdown(wait=True)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        server.shutdown()
        server.server_close()

    assert elapsed < 10
    assert future.done()
