from pathlib import Path
from core.config import IMAGE_ACCEPT
from .base import (
    TASK_IO_TIMEOUT,
    build_headers,
    download_with_mirrors,
    resolve_target,
    task_session,
)
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

    def __init__(self):
        self.session = None

    def close(self):
        session, self.session = self.session, None
        if session is not None:
            session.close()

    def download(self, url, referer=None, save_dir="downloads", headers=None,
                 progress_cb=None, mirrors=None, log=None, filename=None, info=None,
                 session=None):
        h = build_headers(referer, headers, accept=IMAGE_ACCEPT)
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        path = resolve_target(save_dir, url, filename, ".jpg")
        # 合并要点（V30 的任务级代理 session  ×  分支的 TLS 指纹 + 超时）：
        #   分支用 browser_session_for(url) 拿 Chromium TLS 指纹绕 XChina 的 Cloudflare，
        #   这是媒体下载能成功的前提，**不能为了 caller session 丢掉**。
        #   V30 又要求调用方能指定 session（任务级代理）。
        #   task_session(session) 本身就是"包一层"，所以两种来源都能统一走它：
        #   调用方给了 session 就用它（代理优先），没给才自建指纹会话。
        self._owns_session = session is None
        self.session = task_session(_session_for(url) if session is None else session)
        try:
            sha, real = download_with_mirrors(
                url, path, h, session=self.session, progress_cb=progress_cb,
                mirrors=mirrors, log=log, require_image=True, info=info,
                request_timeout=TASK_IO_TIMEOUT,
            )
        finally:
            # 只关自己建的会话；调用方的会话归调用方，擅自关掉会踩坏它的连接池
            if self._owns_session:
                self.close()
        return real, sha
