"""Canonical identities for supported content URLs."""

import re
import unicodedata
from urllib.parse import urlsplit


_CONTENT_PATHS = (
    (
        "xchina_gallery",
        re.compile(r"^/photo/id-([0-9a-f]{13})(?:/\d+)?\.html$", re.IGNORECASE),
    ),
    (
        "xchina_video",
        re.compile(r"^/video/id-([0-9a-f]{13})\.html$", re.IGNORECASE),
    ),
)


def canonical_content_key(url: str, collector: str | None = None) -> str | None:
    """Return the stable identity for an approved XChina content URL."""
    if not isinstance(url, str) or "\\" in url:
        return None
    if any(unicodedata.category(char) == "Cc" for char in url):
        return None

    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None

    if (
        parsed.scheme != "https"
        or host != "xchina.co"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.lower() != "xchina.co"
    ):
        return None

    for expected_collector, pattern in _CONTENT_PATHS:
        match = pattern.fullmatch(parsed.path)
        if not match:
            continue

        resolved_collector = collector
        if resolved_collector is None:
            from collectors import resolve_collector

            resolved_collector = resolve_collector(url, fallback=None)["collector"]
        if resolved_collector != expected_collector:
            return None
        return f"{resolved_collector}:{match.group(1).lower()}"

    return None
