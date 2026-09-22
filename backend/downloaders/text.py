import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

from core.config import settings
from .base import SESSION, build_headers, safe_filename
from .ratelimit import domain_slot

_SKIP_TAGS = ("script", "style", "noscript", "template")


def extract_text(html):
    """提取页面 title 与正文文本(去除 script/style 等)。"""

    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.title = []
            self.in_title = False
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag == "title":
                self.in_title = True
            elif tag in _SKIP_TAGS:
                self.skip += 1

        def handle_endtag(self, tag):
            if tag == "title":
                self.in_title = False
            elif tag in _SKIP_TAGS and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if self.skip:
                return
            (self.title if self.in_title else self.parts).append(data)

    p = _P()
    p.feed(html)
    title = " ".join("".join(p.title).split())
    text = "\n".join(
        line.strip()
        for line in "".join(p.parts).splitlines()
        if line.strip()
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text.strip()


class TextDownloader:
    """网页正文下载: 抓取页面并保存为纯文本 .txt。

    注意: 基于原始 HTML 提取, JS 渲染的内容可能不完整。
    """

    def download(self, url, referer=None, save_dir="downloads", headers=None,
                 progress_cb=None, filename=None, info=None, session=None, **kw):
        h = build_headers(referer, headers)
        with domain_slot(url):
            resp = (session or SESSION).get(url, headers=h, timeout=settings.request_timeout)
        resp.raise_for_status()
        if info is not None:
            info["resolved_url"] = url
            info["content_type"] = (resp.headers.get("Content-Type") or "").split(";")[0] or None
        title, text = extract_text(resp.text)

        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        if filename:
            path = out / filename
            stem = Path(filename).stem
        else:
            stem = safe_filename(url, "").rsplit(".", 1)[0] or "page"
            path = out / f"{stem}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (f"{title}\n{'=' * len(title)}\n\n{text}" if title else text) + "\n"
        data = content.encode("utf-8")
        path.write_bytes(data)
        if progress_cb:
            progress_cb()
        return path, hashlib.sha256(data).hexdigest()
