from collectors.parsers import (
    base_identity,
    classify,
    is_thumbnail,
    merge_by_priority,
    select_quality,
)


def test_classify():
    assert classify("https://a.com/x.mp4") == "video"
    assert classify("https://a.com/x.M3U8?token=1") == "video"
    assert classify("https://a.com/x.jpg") == "image"


def test_merge_priority():
    api = [{"type": "image", "url": "https://a.com/pic.jpg", "headers": {}, "source": "api"}]
    net = [{"type": "image", "url": "https://a.com/pic.jpg?x=1", "headers": {}, "source": "network"}]
    dom = [{"type": "image", "url": "https://a.com/other.png", "headers": {}, "source": "dom"}]
    merged = merge_by_priority({"api": api, "network": net, "dom": dom})
    assert len(merged) == 2
    sources = {r["url"]: r["source"] for r in merged}
    assert sources["https://a.com/pic.jpg"] == "api"


def test_thumbnail_detection():
    assert is_thumbnail("https://a.com/thumbs/pic.jpg")
    assert is_thumbnail("https://a.com/120x90-pic.jpg")
    assert not is_thumbnail("https://a.com/pic.jpg")


def test_select_quality_prefers_original():
    thumb = {"type": "image", "url": "https://a.com/thumbs/pic.jpg", "headers": {}, "source": "network"}
    orig = {"type": "image", "url": "https://a.com/pic.jpg", "headers": {}, "source": "dom"}
    out = select_quality([thumb, orig])
    assert len(out) == 1
    assert out[0]["url"] == "https://a.com/pic.jpg"


def test_select_quality_prefers_larger_thumb():
    small = {"type": "image", "url": "https://a.com/thumbs/120x90-pic.jpg", "headers": {}, "source": "dom"}
    large = {"type": "image", "url": "https://a.com/thumbs/960x720-pic.jpg", "headers": {}, "source": "dom"}
    out = select_quality([small, large])
    assert len(out) == 1
    assert "960x720" in out[0]["url"]


def test_base_identity_normalizes():
    assert base_identity("https://a.com/thumbs/pic.jpg") == base_identity("https://a.com/pic.jpg")


def test_videos_kept():
    v1 = {"type": "video", "url": "https://a.com/v/720.m3u8", "headers": {}, "source": "network"}
    v2 = {"type": "video", "url": "https://a.com/v/1080.m3u8", "headers": {}, "source": "api"}
    out = select_quality([v1, v2])
    assert len(out) == 2
