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
                 progress_cb=None, filename=None, info=None, session=None,
                 etag=None, last_modified=None, **kw):
        h = build_headers(referer, headers)
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        if filename:
            path = out / filename
        else:
            stem = safe_filename(url, "").rsplit(".", 1)[0] or "page"
            path = out / f"{stem}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)

        # 条件请求: 有凭据就带上, 源站可以直接回 304 省掉整个页面。
        req = dict(h)
        if etag:
            req["If-None-Match"] = etag
        if last_modified:
            req["If-Modified-Since"] = last_modified
        with domain_slot(url):
            resp = (session or SESSION).get(
                url, headers=req, timeout=settings.request_timeout
            )
        if resp.status_code == 304 and path.is_file() and path.stat().st_size > 0:
            # ⚠️ 304 **不是错误**(`raise_for_status` 不会为它抛), 不专门处理就会
            # 拿一个空响应体去提取正文, 于是写出一份 0 字节的 .txt 并且报成功 ——
            # 又是一次"跑通了但结果是错的"。复用本地那份才是正解。
            if info is not None:
                info["resolved_url"] = url
                info["not_modified"] = True
            data = path.read_bytes()
            if progress_cb:
                try:
                    progress_cb(len(data))
                except TypeError:
                    progress_cb()
            return path, hashlib.sha256(data).hexdigest()
        resp.raise_for_status()
        if info is not None:
            info["resolved_url"] = url
            info["content_type"] = (resp.headers.get("Content-Type") or "").split(";")[0] or None
            # 校验器记下来, 供下一次走 304
            et = (resp.headers.get("ETag") or "").strip()
            lm = (resp.headers.get("Last-Modified") or "").strip()
            if et:
                info["etag"] = et
            if lm:
                info["last_modified"] = lm
        title, text = extract_text(resp.text)

        content = (f"{title}\n{'=' * len(title)}\n\n{text}" if title else text) + "\n"
        data = content.encode("utf-8")
        path.write_bytes(data)
        if progress_cb:
            # 传字节数, 与流式下载保持同一种回调契约(否则文本资源的下载
            # 字节数永远不计入速率曲线, 曲线会比实际进度矮一截)
            try:
                progress_cb(len(data))
            except TypeError:
                progress_cb()
        return path, hashlib.sha256(data).hexdigest()
