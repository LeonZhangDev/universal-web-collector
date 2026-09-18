"""视频下载: mp4 直链 + m3u8。

引擎选择 (settings.video_engine)
================================

* `auto`    探测到 ffmpeg 就交给它, 失败自动降级内置分片器(默认)
* `ffmpeg`  强制 ffmpeg, 失败直接报错**不降级**
            (避免"以为走了 ffmpeg, 其实悄悄降级成 .ts 了")
* `builtin` 强制内置分片器下载分片; 若 ffmpeg 可用, 合并后仍会 remux 成 mp4

ffmpeg 路径由 `core/ffmpeg.py` 探测(显式配置 -> PATH -> 常见安装位置),
**不直接依赖 PATH** —— PATH 是进程启动时的快照, 服务运行期间新装的 ffmpeg
读不到(实测 2026-09-18: winget 装好 ffmpeg 后 `shutil.which("ffmpeg")`
在本进程里仍返回 None)。

⚠️ 走 ffmpeg 拉流时请求由 ffmpeg 自己发出: 本项目的 DomainLimiter 不参与,
   mirrors 也不会被轮换, 分片进度也只在整个文件完成时上报一次。
   对限速/镜像敏感的站点请用 `builtin`。

内置分片下载器 (builtin)
========================

* 分片**并发**拉取(`settings.segment_concurrency`)
* 分片走**独立限速器**, 间隔 0.15~0.35s —— 不复用图片的 3~10s 慢速节奏。
  分片是同一个视频的连续片段, 播放器本来就连续拉取; 按"每张图 3~10 秒"
  节流会让一段 10 秒的视频下成十分钟(100 片 x 6.5s ≈ 11 分钟)。
* 每个分片独立重试(`settings.segment_retries`), 单片失败不再连累整个视频
* 断点续传: 分片落在 `.<stem>.parts/`, 重跑只补缺失的
* 分片先写 `.tmp` 再原子替换, 中断不会留下被误认为完整的半成品
* 按序字节拼接为 `.ts`; 若 ffmpeg 可用再 remux 成 `.mp4`(换容器, 不重编码)

本模块只负责"给定 URL 把字节取下来", 站点规则一律留在采集器里。
"""

import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import requests

from core.cancel import TaskCancelled
from core.config import settings
from core.ffmpeg import find_ffmpeg
from .base import (
    CHUNK,
    build_headers,
    download_with_mirrors,
    fill_info,
    resolve_target,
    safe_filename,
    sha256_file,
)
from .ratelimit import DomainLimiter

# 视频 CDN 常返回 application/octet-stream, 像图片那样用白名单会误杀真实视频;
# 这里只挡 HTML —— 那一定是错误页而不是媒体。
REJECT_CT = "text/html"

ENGINES = ("auto", "ffmpeg", "builtin")


def _short(err, limit=180):
    """异常压成一行短文本, 便于写进任务日志。"""
    return f"{type(err).__name__}: {err}".replace("\n", " ")[:limit]


def _path_only(url):
    return url.lower().split("?")[0]


def _is_hls(url):
    return _path_only(url).endswith((".m3u8", ".m3u"))


def _is_dash(url):
    return _path_only(url).endswith(".mpd")


class VideoDownloader:
    """视频下载: mp4 直链 + m3u8。

    download() 返回 (实际路径, sha256)。走降级路径时实际路径可能是 .ts。
    """

    def __init__(self):
        self.session = requests.Session()
        if settings.proxy:
            self.session.proxies.update({"http": settings.proxy, "https": settings.proxy})

    def download(self, url, referer=None, save_dir="downloads", headers=None,
                 progress_cb=None, mirrors=None, log=None, filename=None,
                 info=None, **kw):
        h = build_headers(referer, headers)
        if _is_dash(url):
            raise RuntimeError("暂不支持 DASH(.mpd), 请改用 ffmpeg 手工下载")
        if _is_hls(url):
            return self._download_m3u8(url, h, save_dir, progress_cb, log, mirrors,
                                       filename, info)
        return self._download_file(url, h, save_dir, mirrors, progress_cb, log,
                                   filename, info)

    # ---- mp4 直链 ----

    def _download_file(self, url, headers, save_dir, mirrors, progress_cb, log,
                       filename=None, info=None):
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = resolve_target(save_dir, url, filename, ".mp4")
        sha, real = download_with_mirrors(
            url, path, headers,
            retries=settings.video_retries,
            session=self.session,
            progress_cb=progress_cb,
            mirrors=mirrors,
            log=log,
            reject_ct=REJECT_CT,
            info=info,
        )
        return real, sha

    # ---- m3u8 ----

    def _download_m3u8(self, murl, headers, save_dir, progress_cb, log, mirrors=None,
                       filename=None, info=None):
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        # filename 可能是 "相册名/0001.mp4" 这样的相对路径: 沿用它的目录部分,
        # 主名取 stem(后缀由实际容器决定: .ts 或 remux 后的 .mp4)。
        if filename:
            dest = (out / filename).parent
            stem = Path(filename).stem
        else:
            dest = out
            stem = safe_filename(murl, "").rsplit(".", 1)[0]
        if not stem:
            stem = "video"
        dest.mkdir(parents=True, exist_ok=True)
        # 后续所有落盘都写 out, 这里把它指向最终目录, 免去逐个改动
        out = dest

        # 引擎选择: builtin 时完全不看 ffmpeg(用于验证内置器或规避其拉流行为)
        engine = (getattr(settings, "video_engine", "auto") or "auto").lower()
        if engine not in ENGINES:
            engine = "auto"
        ff = find_ffmpeg()
        # 只有 auto/ffmpeg 且确实找到可执行文件时才交给它拉流;
        # remux 是另一回事, 只要 ff 在就还能用(见步骤 5)。
        pull_with_ffmpeg = bool(ff) and engine in ("auto", "ffmpeg")

        # 1) ffmpeg 一步到位(也能处理 AES-128 加密流)
        if pull_with_ffmpeg:
            path = out / f"{stem}.mp4"
            try:
                self._ffmpeg_pull(murl, headers, path, ff)
                if progress_cb:
                    progress_cb()
                if log:
                    log(f"ffmpeg 拉流完成: {path.name}")
                fill_info(info, murl, "video/mp4")
                return path, sha256_file(path)
            except Exception as e:
                path.unlink(missing_ok=True)
                if engine == "ffmpeg":
                    raise RuntimeError(
                        f"ffmpeg 拉流失败, 且 video_engine=ffmpeg 不降级: {_short(e)}"
                    ) from e
                if log:
                    log(f"ffmpeg 拉流失败, 降级内置分片下载: {_short(e)}")
        elif engine == "ffmpeg":
            raise RuntimeError(
                "video_engine=ffmpeg 但未找到 ffmpeg 可执行文件; "
                "请安装 ffmpeg, 或用 UWC_FFMPEG 显式指定路径"
            )
        elif log:
            log(f"使用内置分片下载器 (engine={engine})")

        # 分片走独立限速器: 与图片的慢速节奏解耦, 但仍有节流避免被 WAF 挡
        limiter = DomainLimiter(
            settings.segment_concurrency,
            settings.segment_min_interval,
            settings.segment_max_interval,
        )

        # 2) 解析播放列表; 主 URL 失败时依次试备用下载点
        candidates = [murl] + [m for m in (mirrors or []) if m and m != murl]
        segments, base, last_err = [], murl, None
        for cand in candidates:
            try:
                segments = self._resolve_segments(cand, headers, limiter)
                base = cand
                break
            except Exception as e:
                last_err = e
                if log:
                    log(f"播放列表解析失败 {cand.split('/')[-1]}: {_short(e, 80)}")
        if not segments:
            raise RuntimeError(f"m3u8 未解析出分片: {_short(last_err or 'empty')}")
        if log:
            log(f"分片清单: {len(segments)} 个 (来源 {base.split('/')[-1]})")

        # 3) 并发下载分片(带续传与分片级重试)
        parts_dir = out / f".{stem}.parts"
        parts = self._fetch_segments(
            segments, headers, parts_dir, limiter, progress_cb, log
        )

        # 4) 按序合并; 合并成功后才清掉分片缓存
        merged = out / f"{stem}.ts"
        try:
            with open(merged, "wb") as dst:
                for p in parts:
                    with open(p, "rb") as src:
                        shutil.copyfileobj(src, dst, CHUNK)
                    # 长视频几百片, 合并阶段也要能响应"停止"
                    if progress_cb:
                        progress_cb()
        except TaskCancelled:
            merged.unlink(missing_ok=True)
            raise
        shutil.rmtree(parts_dir, ignore_errors=True)
        if log:
            log(f"分片合并完成: {merged.name} ({len(parts)} 片)")

        # 5) 有 ffmpeg 就 remux 成 mp4(容器转换, 不重编码, 很快)。
        #    builtin 模式下下载虽由内置器完成, 但 remux 不受影响 —— 用户装 ffmpeg
        #    的目的就是拿到通用容器, 没必要因为强制内置下载而退回 .ts。
        if ff:
            mp4 = out / f"{stem}.mp4"
            try:
                self._ffmpeg_remux(merged, mp4, ff)
                merged.unlink(missing_ok=True)
                if log:
                    log(f"已 remux 为 {mp4.name}")
                fill_info(info, base, "video/mp4")
                return mp4, sha256_file(mp4)
            except Exception as e:
                if log:
                    log(f"remux 失败, 保留 .ts 容器: {_short(e)}")

        fill_info(info, base, "video/mp2t")
        return merged, sha256_file(merged)

    def _resolve_segments(self, url, headers, limiter, depth=0):
        """解析 m3u8, 返回分片 URL 列表(自动下钻 master playlist 取最高码率)。"""
        with limiter.slot():
            resp = self.session.get(url, headers=headers, timeout=settings.request_timeout)
        resp.raise_for_status()
        text = resp.text

        if "#EXT-X-KEY:" in text and "METHOD=NONE" not in text:
            raise RuntimeError("加密 m3u8(AES-128) 需要 ffmpeg 支持, 当前无法合并")

        variants = []
        for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n#][^\n]*)", text):
            bw = re.search(r"BANDWIDTH=(\d+)", m.group(1))
            variants.append((int(bw.group(1)) if bw else 0, m.group(2).strip()))
        if variants and depth < 3:
            best = urljoin(url, max(variants, key=lambda x: x[0])[1])
            return self._resolve_segments(best, headers, limiter, depth + 1)

        return [
            urljoin(url, line.strip())
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def _fetch_segments(self, segments, headers, parts_dir, limiter, progress_cb, log):
        """并发下载分片, 返回按序排列的 part 路径。

        续传: 已存在且非空的 part 直接跳过。因为落盘走"先 .tmp 再原子替换",
        不会有半个 part 被误认为完整, 所以只需判断存在即可。
        """
        parts_dir.mkdir(parents=True, exist_ok=True)
        total = len(segments)
        paths = [parts_dir / f"{i:06d}.part" for i in range(total)]

        cached = sum(1 for p in paths if p.exists() and p.stat().st_size > 0)
        if cached and log:
            log(f"续传: 已缓存 {cached}/{total} 个分片, 只补缺失的")

        counter = [cached]
        clock = threading.Lock()

        def fetch(i):
            part = paths[i]
            if part.exists() and part.stat().st_size > 0:
                return
            last = None
            for attempt in range(1, settings.segment_retries + 1):
                tmp = part.with_name(part.name + ".tmp")
                try:
                    with limiter.slot():
                        resp = self.session.get(
                            segments[i], headers=headers, stream=True,
                            timeout=settings.request_timeout,
                        )
                    resp.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(CHUNK):
                            if chunk:
                                f.write(chunk)
                                # 每块都回调: 单片可能很大, 只按分片回调的话
                                # "停止"要等整片下完才生效
                                if progress_cb:
                                    progress_cb()
                    tmp.replace(part)
                    return
                except TaskCancelled:
                    tmp.unlink(missing_ok=True)
                    raise
                except Exception as e:
                    last = e
                    tmp.unlink(missing_ok=True)
                    if attempt < settings.segment_retries:
                        time.sleep(min(2 ** attempt, 5))
            raise RuntimeError(f"分片 {i + 1}/{total} 失败: {_short(last)}")

        errors = []
        with ThreadPoolExecutor(
            max_workers=max(1, settings.segment_concurrency), thread_name_prefix="seg"
        ) as pool:
            futures = [pool.submit(fetch, i) for i in range(total)]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except TaskCancelled:
                    # 别等到全部分片跑完再报错: 已下好的 part 留在磁盘上,
                    # 下次重跑能续传, 这里直接让取消信号穿透出去
                    raise
                except Exception as e:
                    errors.append(str(e))
                with clock:
                    counter[0] += 1
                    if progress_cb:
                        progress_cb()

        if errors:
            # 已下好的分片保留在 parts_dir, 重跑时可续传
            raise RuntimeError(
                f"{len(errors)}/{total} 个分片下载失败 (首条: {errors[0]})"
            )
        return paths

    # ---- ffmpeg ----

    def _ffmpeg_pull(self, url, headers, path, ff):
        """让 ffmpeg 直接拉 m3u8 并输出到 path(`-c copy` 不重编码)。

        请求头经 `-headers` 传给 http 协议, 必须放在 `-i` 之前。
        注意 Accept 不可省 —— 部分 WAF 缺了它直接 403。
        `-nostats` 抑制进度行: 我们用 capture_output 收 stderr,
        长视频的进度输出会白白堆积在内存里。
        """
        args = []
        for k, v in headers.items():
            args += ["-headers", f"{k}: {v}\r\n"]
        self._run_ffmpeg([ff, "-y", "-nostats", *args, "-i", url, "-c", "copy", str(path)])

    def _ffmpeg_remux(self, src, dst, ff):
        self._run_ffmpeg(
            [ff, "-y", "-nostats", "-i", str(src), "-c", "copy", str(dst)]
        )

    @staticmethod
    def _run_ffmpeg(cmd):
        """执行 ffmpeg; 失败时把 stderr 尾行带出来。

        旧实现是 `except Exception: pass`, 真实原因(协议不支持/编码不兼容)
        全部丢失, 排查时只能看到降级后那条误导性的报错。
        """
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
        except FileNotFoundError as e:
            # 路径来自 core.ffmpeg 探测, 到这里说明探测后又被动过(卸载/移动)
            raise RuntimeError(f"ffmpeg 不可执行: {cmd[0]}") from e
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            raise RuntimeError(
                f"ffmpeg 退出码 {e.returncode}: {tail[-1] if tail else '无 stderr'}"
            ) from e
