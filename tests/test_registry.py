import pytest

from collectors import COLLECTORS, get_collector
from downloaders.text import TextDownloader


def test_builtin_collectors_registered():
    assert "xchina" in COLLECTORS
    assert "generic" in COLLECTORS


def test_get_collector_instances():
    assert callable(get_collector("xchina").crawl)
    assert callable(get_collector("generic").crawl)


def test_unknown_collector():
    with pytest.raises(ValueError):
        get_collector("nope")


def test_text_downloader_exists():
    assert callable(TextDownloader().download)
