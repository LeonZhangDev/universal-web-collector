from downloaders.base import build_headers, safe_filename
from downloaders.image import ImageDownloader
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
