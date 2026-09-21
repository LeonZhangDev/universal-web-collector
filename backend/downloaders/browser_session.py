"""Allowlisted browser-TLS sessions for media CDNs that require Chromium TLS."""

from urllib.parse import urlparse

from core.config import settings


_BROWSER_TLS_MEDIA_HOSTS = frozenset({
    "img.xchina.io",
    "video.xchina.download",
})


def browser_session_for(url):
    """Return an owned curl-cffi session only for exact HTTPS media hosts."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() != "https" or host not in _BROWSER_TLS_MEDIA_HOSTS:
        return None
    if port not in (None, 443):
        return None

    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate="chrome")
    if settings.proxy:
        session.proxies.update({"http": settings.proxy, "https": settings.proxy})
    return session
