import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.content_identity import canonical_content_key


@pytest.mark.parametrize(
    "url",
    [
        "https://xchina.co/photo/id-6664761937f5a.html",
        "https://xchina.co/photo/id-6664761937f5a/1.html",
        "https://xchina.co/photo/id-6664761937f5a.html?from=listing",
        "https://xchina.co/photo/id-6664761937f5a.html#gallery",
    ],
)
def test_gallery_variants_share_canonical_content_key(url):
    assert canonical_content_key(url) == "xchina_gallery:6664761937f5a"


def test_video_has_canonical_content_key():
    url = "https://xchina.co/video/id-6aaee7c9a12e8.html"
    assert canonical_content_key(url) == "xchina_video:6aaee7c9a12e8"


def test_matching_explicit_collector_has_canonical_content_key():
    url = "https://xchina.co/photo/id-6664761937f5a.html"
    assert (
        canonical_content_key(url, collector="xchina_gallery")
        == "xchina_gallery:6664761937f5a"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://xchina.co/photo/id-6664761937f5a.html",
        "https://user@xchina.co/photo/id-6664761937f5a.html",
        "https://xchina.co:443/photo/id-6664761937f5a.html",
        "https://xchina.co:not-a-port/photo/id-6664761937f5a.html",
        "https://xchina.co:99999/photo/id-6664761937f5a.html",
        "https://evil.example\\@xchina.co/photo/id-6664761937f5a.html",
        "https://xchina.co/\nphoto/id-6664761937f5a.html",
    ],
)
def test_malformed_authority_is_rejected_with_explicit_collector(url):
    assert canonical_content_key(url, collector="xchina_gallery") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://not-xchina.co/photo/id-6664761937f5a.html",
        "https://xchina.co.evil.example/photo/id-6664761937f5a.html",
        "https://xchina.co/photo/id-too-short.html",
        "https://xchina.co/photo/id-6664761937f5g.html",
        "https://xchina.co/photo/id-6664761937f5a/extra.html",
        "not a url",
    ],
)
def test_unsupported_or_malformed_urls_have_no_content_key(url):
    assert canonical_content_key(url) is None


def test_explicit_collector_must_match_url_family():
    url = "https://xchina.co/video/id-6aaee7c9a12e8.html"
    assert canonical_content_key(url, collector="xchina_gallery") is None
