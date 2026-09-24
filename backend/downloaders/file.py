from pathlib import Path

from .base import build_headers, resolve_target, stream_download, validators_for


class FileDownloader:
    """通用文件下载: 音频(mp3/m4a/...)、PDF、Office 文档等。"""

    def download(self, url, referer=None, save_dir="downloads", headers=None,
                 progress_cb=None, filename=None, info=None, session=None,
                 etag=None, last_modified=None, **kw):
        h = build_headers(referer, headers)
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        path = resolve_target(save_dir, url, filename, ".bin")
        sha = stream_download(url, path, h, progress_cb=progress_cb, info=info,
                              session=session,
                              validators=validators_for(etag, last_modified))
        return path, sha
