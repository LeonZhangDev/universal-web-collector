from pathlib import Path
from core.config import IMAGE_ACCEPT
from .base import build_headers, download_with_mirrors, resolve_target
from .browser_session import browser_session_for


def _session_for(url):
    """Use a Chromium TLS fingerprint for XChina's Cloudflare media host."""
    return browser_session_for(url)


class ImageDownloader:
    """图片下载: 限速 + 重试 + 断点续传 + 备用下载点切换。

    hash 去重由 task_manager 处理。返回实际保存路径——主 URL 降级到
    备用下载点时扩展名可能变化(如 .jpg -> .webp)。

    filename: 外部指定的落盘相对路径, 优先级高于 URL 末段(见 core/naming.py)。
    """

    def download(self, url, referer=None, save_dir="downloads", headers=None,
                 progress_cb=None, mirrors=None, log=None, filename=None, info=None):
        h = build_headers(referer, headers, accept=IMAGE_ACCEPT)
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        path = resolve_target(save_dir, url, filename, ".jpg")
        session = _session_for(url)
        try:
            sha, real = download_with_mirrors(
                url, path, h, session=session, progress_cb=progress_cb,
                mirrors=mirrors, log=log, require_image=True, info=info,
            )
        finally:
            if session is not None:
                session.close()
        return real, sha
