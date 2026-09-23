"""视频下载: mp4 直链 + m3u8(HLS) + mpd(DASH)。

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
   mirrors 也不会被轮换。ffmpeg 子进程运行期间会周期性调用 progress_cb，
   让任务心跳与取消信号保持活跃；资源完成进度仍只在整个文件完成时上报一次。
   对限速/镜像敏感的站点请用 `builtin`。

内置分片下载器 (builtin)
========================

* 分片**并发**拉取(`settings.segment_concurrency`)
* 分片走**独立限速器**, 间隔 0.15~0.35s —— 不复用图片的 3~10s 慢速节奏。
  分片是同一个视频的连续片段, 播放器本来就连续拉取; 按"每张图 3~10 秒"
  节流会让一段 10 秒的视频下成十分钟(100 片 x 6.5s ≈ 11 分钟)。
* 每个分片独立重试(`settings.segment_retries`), 单片失败不再连累整个视频
* 断点续传: 分片落在 `.<stem>.parts/`(HLS) 或 `.<stem>.dash/`(DASH),
  重跑只补缺失的
* 分片先写 `.tmp` 再原子替换, 中断不会留下被误认为完整的半成品
* 按序字节拼接为 `.ts`; 若 ffmpeg 可用再 remux 成 `.mp4`(换容器, 不重编码)

三种形态的**结构差别**(决定了合并步骤完全不同)
==============================================

| 形态 | 分片清单 | 合并 |
| --- | --- | --- |
| mp4 直链 | 无 | 无需合并, 直接写盘 |
| HLS (m3u8) | 一份清单里就是完整节目 | 按序拼成 `.ts` |
| **DASH (mpd)** | **音视频是两条独立的轨**, 各自一份分片清单 | 各自拼全后再 `-c copy` mux |

⚠️ 所以 DASH 少了 ffmpeg 就**只能明确失败**: 只把视频轨落盘会得到一个没有声音的
文件, 而界面上它显示"下载成功" —— 那是假成功, 比失败更糟。`.mpd` 的解析(含
"哪些形态必须拒绝")见 `downloaders/dash.py`。

本模块只负责"给定 URL 把字节取下来", 站点规则一律留在采集器里。
"""

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core import mediacheck
from core import partials
from core.cancel import TaskCancelled
from core.config import settings
from core.errors import CorruptMediaError
from core.ffmpeg import find_ffmpeg
from collectors.hls import inspect_playlist
from .base import (
    CHUNK,
    TASK_IO_TIMEOUT,
    build_headers,
    download_with_mirrors,
    fill_info,
    resolve_target,
    safe_filename,
    sha256_file,
    task_session,
    validators_for,
)
from .browser_session import browser_session_for
from . import dash as _dash
from .dash import parse_mpd
from .ratelimit import DomainLimiter, domain_slot

# 视频 CDN 常返回 application/octet-stream, 像图片那样用白名单会误杀真实视频;
# 这里只挡 HTML —— 那一定是错误页而不是媒体。
REJECT_CT = "text/html"

ENGINES = ("auto", "ffmpeg", "builtin")
MIN_HLS_DURATION_RATIO = 0.99
SHUTDOWN_IO_TIMEOUT = TASK_IO_TIMEOUT

#: 嵌套 `sidx`(`reference_type=1`)展开时最多往下钻几层 / 取回几个索引。
#: 真实站点两级就够; 上限的意义是"清单坏掉时别无限请求下去" ——
#: 一个环形的索引引用会让递归永远走不完, 而且每层都要发一个请求。
SIDX_MAX_DEPTH = 4
SIDX_MAX_INDEXES = 64

#: 直播轮询的间隔上下限。节奏由清单声明的 `minimumUpdatePeriod` 决定, 这里只兜住
#: 两端: 太快是白打源站, 太慢会漏分片 —— 直播分片滚出窗口就再也补不回来。
LIVE_MIN_POLL = 2.0
LIVE_MAX_POLL = 30.0
LIVE_DEFAULT_POLL = 4.0
#: 连续多少次取不回清单就放弃录制(而不是永远重试下去 —— 那是一个不会结束的任务)。
LIVE_MAX_FETCH_FAILURES = 5


def _short(err, limit=180):
    """异常压成一行短文本, 便于写进任务日志。"""
    return f"{type(err).__name__}: {err}".replace("\n", " ")[:limit]


def _seg_target(seg, headers):
    """分片寻址 -> `(url, headers)`。

    分片有两种写法(见 `downloaders/dash.py` 的模块 docstring):

      * `"https://.../s1.m4s"`                     —— 一片一个文件;
      * `("https://.../m.mp4", (start, end))`      —— 同一个文件里的字节区间。

    第二种会加上 `Range: bytes=start-end`(闭区间, 与 DASH 的 range 属性同义)。

    ⚠️ 带 `Range` 时**绝不能**再带 `If-None-Match`/`If-Modified-Since`:
    服务器对"条件 + Range"回的是 **304 而不是 206**, 于是那一段字节永远取不到。
    这里直接构造 headers 而不复用调用方那份, 就是为了这个 —— 调用方的 headers
    可能带着条件请求(条件请求 ⊥ 续传, 见 `downloaders/base.py`)。
    """
    if isinstance(seg, (tuple, list)) and len(seg) == 2:
        url, rng = seg
        try:
            start, end = rng
        except (TypeError, ValueError):
            return str(url), headers
        hdrs = {k: v for k, v in (headers or {}).items()
                if k.lower() not in ("if-none-match", "if-modified-since", "range")}
        hdrs["Range"] = f"bytes={int(start)}-{int(end)}"
        return str(url), hdrs
    return seg, headers


def _seg_key(seg):
    """分片地址 -> 可哈希的身份, 用于"这一片是不是已经下过"。

    ⚠️ 必须把**字节区间**算进身份里: `SegmentBase` / `mediaRange` 的分片地址是
    `(同一个URL, 不同区间)`, 只拿 URL 当身份会让整条轨"只认作一片" ——
    于是直播录到第二片就不再录了, 而且不报错。
    """
    if isinstance(seg, (tuple, list)) and len(seg) == 2:
        url, rng = seg
        try:
            start, end = rng
            return f"{url}#{int(start)}-{int(end)}"
        except (TypeError, ValueError):
            return str(url)
    return str(seg)


def _notify_bytes(progress_cb, data):
    """把"这一笔写了多少字节"报给调用方, 并兼容无参回调。

    data 可以是 bytes(长度直接取), 或文件路径(取文件大小, 用于分片合并、
    ffmpeg 拉流这类"一次性产出整个文件"的场景)。

    ⚠️ 兼容无参回调是必须的: 测试与第三方下载器常写成 `def cb(): ...`,
    直接传参会 TypeError, 而它发生在下载循环里 —— 会被当成"下载失败"。
    """
    n = None
    try:
        if data is None:
            n = 0
        elif isinstance(data, (bytes, bytearray)):
            n = len(data)
        else:
            n = Path(data).stat().st_size
    except OSError:
        n = 0
    try:
        progress_cb(n)
    except TypeError:
        progress_cb()


def _path_only(url):
    return url.lower().split("?")[0]


def _is_hls(url):
    return _path_only(url).endswith((".m3u8", ".m3u"))


def _is_dash(url):
    return _path_only(url).endswith(".mpd")


def _resolve_ffprobe(ff):
    """找 ffprobe: 优先取 ffmpeg **同目录**下的那个, 再退回 PATH; 找不到返回 None。

    ⚠️ 必须把"有没有探测器"和"探测结果是什么"分开表示。本模块原来让
    `_probe_media_duration` 在**两种**情况下都返回 None —— 机器上没装 ffprobe,
    以及 ffprobe 说这个文件读不出来。调用方无法区分, 于是要么在没装 ffprobe 的
    机器上把每个文件都判成坏的, 要么在文件真坏了时以为"只是没有探测器"。做成
    两个函数之后, 前者决定**放行**, 后者决定**报错**, 语义不再重叠。
    """
    if not ff:
        return None
    suffix = ".exe" if Path(ff).suffix.lower() == ".exe" else ""
    sibling = Path(ff).with_name(f"ffprobe{suffix}")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe")


def _probe_media_duration(path, ff, progress_cb=None):
    """用 ffprobe 读取最终容器时长。

    返回秒数; 无法得到有效结果(没有 ffprobe / 超时 / 输出不可解析)时返回 None。
    调用方**不要**把 None 当成"文件坏了" —— 先用 `_resolve_ffprobe` 确认本机
    有没有探测能力。
    """
    probe = _resolve_ffprobe(ff)
    if not probe:
        return None
    cmd = [
        probe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        if progress_cb is None:
            result = subprocess.run(
                cmd, check=True, capture_output=True, text=True, timeout=30
            )
            stdout = result.stdout
        else:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            stdout, _ = VideoDownloader._communicate_process(
                proc, cmd, timeout=30, progress_cb=progress_cb
            )
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, cmd)
        value = float(stdout.strip())
        return value if value > 0 else None
    except TaskCancelled:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _note_duration(info, measured):
    """把已经量到的容器时长记回调用方的 `info`, 供 `resources.duration` 落库。

    ⚠️ 这不是"多算一次": 直链 / HLS / DASH 三条收尾路径**本来就会** ffprobe 一次
    最终容器, 只是把结果用完即弃。再在别处为落库单独探测一遍, 等于每个视频
    spawn 两次 ffprobe, 而两处判据将来必然漂移。

    与 `_resolve_ffprobe` 同一条约束: **拿不到就留空, 不要编一个 0**。
    没装 ffprobe 时留 NULL 是正确的("缺探测器"不等于"视频坏"), 写 0 会让界面
    显示成"时长 0 秒", 把能力缺失伪装成内容问题。
    """
    if not measured or not isinstance(info, dict):
        return
    try:
        info["probed_duration"] = float(measured)
    except (TypeError, ValueError):
        pass


def _validate_hls_duration(path, info, ff, progress_cb=None):
    """拒绝 ffmpeg 退出码为 0、但只封装了播放列表前一小段的假成功。"""
    return _check_duration(path, float((info or {}).get("duration") or 0), ff,
                           progress_cb=progress_cb, what="播放列表")


def _check_duration(path, expected, ff, progress_cb=None, what="清单"):
    """把实测时长与清单**自报**的时长对一遍, 返回实测值(或 None)。

    两个调用方(HLS 与 DASH)共用这一份 —— 容差、缺 ffprobe 怎么办、报错措辞
    都是同一套判据, 分开写第二遍必然有一份先漂移。
    """
    actual = _probe_media_duration(path, ff, progress_cb=progress_cb)
    if expected <= 0:
        # 清单没声明时长 -> 没有可比对的基线, 不下结论(只回报实测值)
        return actual
    if actual is None:
        raise RuntimeError(
            f"产物无法验证时长: ffprobe 缺失、超时或返回无效结果"
            f"(清单声明 {expected:.1f}s)"
        )
    # For normal/long videos this is exactly the 99% gate. Very short
    # containers can differ by a few hundred milliseconds due to timestamp
    # rounding, so allow at most 0.5s there rather than rejecting valid clips.
    tolerance = max(expected * (1.0 - MIN_HLS_DURATION_RATIO), 0.5)
    if expected - actual > tolerance:
        raise RuntimeError(
            "产物疑似截断: "
            f"实际 {actual:.1f}s / {what} {expected:.1f}s "
            f"(允许误差 {tolerance:.1f}s)"
        )
    return actual


def _validate_direct_media(path, info, ff, progress_cb=None):
    """直链容器(非 HLS)的内容终检, 返回实测时长或 None。

    ⚠️ 修的是一个静默缺陷: `_download_file` 此前**只有 Content-Length 校验**。
    长度校验只能证明"字节数没少", 证明不了内容可用 —— 被中间设备截断却保留正确
    Content-Length 的 mp4 会以"下载成功"落盘。而 HLS 路径早就用 ffprobe 验时长了
    (`_validate_hls_duration`), 偏偏直链这条**绕过 ffmpeg 自己写字节**的路没有。

    判据与 HLS 相同(容器时长), 但直链没有播放列表给的 expected, 所以多一条:
    "ffprobe 在、却读不出任何时长"**本身就是结论** —— mp4 的 moov 通常在文件尾部,
    被截断的文件连元数据都读不到, ffprobe 会直接失败。

    ffprobe 不在时返回 None(放行): 与 `core/phash.py` 同一条约束 —— 辅助能力缺失
    不该让下载失败。容器级的截断仍会被 `core/mediacheck.py` 的算术判据抓到, 那条
    路径不依赖任何外部程序, 所以放在探测器检查**之前**。
    """
    # 先做零依赖的算术判据(mp4 的 box 链走不通 = 被截断)。这一步不需要 ffprobe,
    # 所以哪怕本机没装 ffmpeg 也有一层保护。
    reason = mediacheck.truncation_reason(path)
    if reason:
        raise CorruptMediaError(reason, path)
    if not _resolve_ffprobe(ff):
        return None
    actual = (
        _probe_media_duration(path, ff, progress_cb=progress_cb)
        if progress_cb is not None
        else _probe_media_duration(path, ff)
    )
    if actual is None:
        raise CorruptMediaError(
            "容器无法解析 —— ffprobe 读不出时长, 疑似被截断或不是有效媒体", path
        )
    expected = float((info or {}).get("duration") or 0)
    if expected > 0:
        tolerance = max(expected * (1.0 - MIN_HLS_DURATION_RATIO), 0.5)
        if expected - actual > tolerance:
            raise CorruptMediaError(
                f"疑似截断: 实测 {actual:.1f}s / 声明 {expected:.1f}s"
                f"(允许误差 {tolerance:.1f}s)",
                path,
            )
    return actual


class VideoDownloader:
    """视频下载: mp4 直链 + m3u8。

    download() 返回 (实际路径, sha256)。走降级路径时实际路径可能是 .ts。
    """
    # 合并要点：
    #   HEAD 侧（V30）给了 download(session=...) 任务级代理入口 + 基于全局
    #   settings.proxy 的实例默认会话；分支侧给了 browser_session_for() 的
    #   Chromium TLS 指纹（绕 Cloudflare 媒体 host 的前提）+ 会话所有权/关闭语义。
    #   三者可共存，优先级：caller session > 全局 settings.proxy > TLS 指纹。

    def __init__(self):
        # 由 download() 自建的会话归该次调用所有，成功/失败都要关掉；
        # 测试与采集器可以注入自己的会话，注入的不由我们关。
        self.session = None
        self._owns_session = False
        if settings.proxy:
            self.session = requests.Session()
            self.session.proxies.update({"http": settings.proxy, "https": settings.proxy})
            self._owns_session = True

    def close(self):
        session, self.session = self.session, None
        self._owns_session = False
        if session is not None:
            session.close()

    def download(self, url, referer=None, save_dir="downloads", headers=None,
                 progress_cb=None, mirrors=None, log=None, filename=None,
                 info=None, session=None, etag=None, last_modified=None, **kw):
        # 任务级代理(session)覆盖实例默认(基于全局 settings.proxy)。
        # 每个资源下载前 task_manager 都新建实例(见 DOWNLOADERS[type]()),
        # 故这里覆盖 self.session 不会与并发任务互相污染。
        if session is not None:
            # 调用方注入：用它的，且不归我们关
            self.session = session
            self._owns_session = False
        elif self.session is None:
            # 既没注入、也没有全局 proxy 会话 → 自建 TLS 指纹会话（XChina 媒体必需）
            self.session = task_session(browser_session_for(url))
            self._owns_session = True
        try:
            h = build_headers(referer, headers)
            if _is_dash(url):
                return self._download_mpd(url, h, save_dir, progress_cb, log, mirrors,
                                          filename, info)
            if _is_hls(url):
                return self._download_m3u8(url, h, save_dir, progress_cb, log, mirrors,
                                           filename, info)
            return self._download_file(url, h, save_dir, mirrors, progress_cb, log,
                                       filename, info,
                                       # 条件请求只对 mp4 直链有意义; m3u8 是播放
                                       # 列表, 它的"变没变"由分片自身决定, 把播放
                                       # 列表的 ETag 当作整段视频的身份是错的。
                                       validators=validators_for(etag, last_modified))
        finally:
            if self._owns_session:
                self.close()

    # ---- mp4 直链 ----

    def _download_file(self, url, headers, save_dir, mirrors, progress_cb, log,
                       filename=None, info=None, validators=None):
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
            request_timeout=SHUTDOWN_IO_TIMEOUT,
            validators=validators,
        )
        # ⚠️ 落盘后再校验内容。放在这里(而不是下载前)是因为只有此时手上才是完整
        # 文件: 零额外请求、零误判。校验不过就删掉 —— 一份坏字节留在最终位置上会
        # 一路冒充"已完成", 比明确失败糟糕得多。
        try:
            _note_duration(info, _validate_direct_media(
                real, info, find_ffmpeg(), progress_cb=progress_cb))
        except TaskCancelled:
            raise
        except Exception:
            Path(real).unlink(missing_ok=True)
            raise
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

        # engine=ffmpeg 但找不到可执行文件: 这是确定性的本地配置错误, 应 fail-fast
        # 优先报, 不要绕一圈网络预检才暴露(而且不能静默降级成内置器 —— 用户以为
        # 产物是 ffmpeg 拉的, 实际不是)
        if engine == "ffmpeg" and not ff:
            raise RuntimeError(
                "video_engine=ffmpeg 但未找到 ffmpeg 可执行文件; "
                "请安装 ffmpeg, 或用 UWC_FFMPEG 显式指定路径"
            )

        # 0) 预检: 下载前先读一遍播放列表本体, 拦掉"假成功"。
        #    签名过期/无效时站点回一个语法合法、指向占位分片的 m3u8, ffmpeg 会
        #    一路畅通地下完并报告 success —— 用户只拿到几十秒占位画面。这里在开工
        #    前就判定凭证是否还活着、分片是不是真的, 不通过就响亮地失败。
        #    主 URL 失败时依次试备用下载点(镜像)。
        #
        # ⚠️ `pl`(播放列表信息)与 `info`(调用方的回填槽)**不是一回事**: `info` 是
        # task_manager 传进来要 `fill_info` 写 `resolved_url`/`content_type` 的那只
        # dict。这里曾经把两者混用同一个名字, 于是这次预检的结果把调用方那只 dict
        # 顶掉了 —— 结局是 HLS 视频的 `resolved_url` 永远是空的, 而 DASH/直链都有。
        # 不报错、不影响下载, 只是那条溯源信息静静地没了。
        base, pl, last_err = murl, None, None
        for cand in [murl] + [m for m in (mirrors or []) if m and m != murl]:
            try:
                base, pl = self._preflight_hls(
                    cand, headers, log, progress_cb=progress_cb
                )
                break
            except Exception as e:
                last_err = e
                if log:
                    log(f"播放列表校验失败 {cand.split('/')[-1]}: {_short(e, 80)}")
        if pl is None or not pl["ok"]:
            raise RuntimeError(
                f"m3u8 未通过校验: {_short(last_err or '空响应', 160)}"
            )

        # 1) ffmpeg 一步到位(也能处理 AES-128 加密流)
        if pull_with_ffmpeg:
            path = out / f"{stem}.mp4"
            try:
                # 两侧都要：_ffmpeg_pull 传 progress_cb（分支：让 ffmpeg 拉流期间
                # 也能刷新进度）+ 时长校验（分支：拒绝"退出码 0 但只封了前一小段"的
                # 假成功）；而进度上报用 _notify_bytes（HEAD/V32：报**字节数**，
                # 前端据此画速率曲线，兼容无参回调）。
                self._ffmpeg_pull(base, headers, path, ff, progress_cb=progress_cb)
                _note_duration(info, _validate_hls_duration(
                    path, pl, ff, progress_cb=progress_cb))
                if progress_cb:
                    _notify_bytes(progress_cb, path)
                if log:
                    log(f"ffmpeg 拉流完成: {path.name}")
                fill_info(info, base, "video/mp4")
                return path, sha256_file(path, progress_cb=progress_cb)
            except TaskCancelled:
                # ⚠️ 用户点了停止。这不是"ffmpeg 拉流失败" —— 落到下面的
                # `except Exception` 里会被当成一次普通失败, 于是在 engine=auto
                # 下**降级到内置分片下载**, 继续去下一个已经被叫停的视频。
                # 用户的体感就是"点了停止没反应, 它还在下"。
                path.unlink(missing_ok=True)
                raise
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

        # 2) 复用预检已解析的绝对化分片清单(主 URL 失败时会落到这里, 此时已是镜像)
        segments = pl["segments"]
        if not segments:
            raise RuntimeError("播放列表没有任何分片")
        # 加密流必须用 ffmpeg 解密: 内置分片器只会把密文 .ts 拼在一起, 得到垃圾文件
        if pl["encrypted"] and not pull_with_ffmpeg:
            method = next(
                (k.get("method") for k in (pl.get("keys") or []) if k.get("method")),
                "AES-128",
            )
            n_keys = len(pl.get("keys") or []) or 1
            raise RuntimeError(
                f"播放列表声明 {method} 加密({n_keys} 把密钥), 内置分片器无法解密; "
                f"请安装 ffmpeg 或改用 video_engine=ffmpeg(当前 engine={engine}, "
                f"ffmpeg={'可用' if ff else '未找到'})"
            )
        if log:
            log(f"分片清单: {len(segments)} 个 (来源 {base.split('/')[-1]})")

        # 3) 并发下载分片(带续传与分片级重试)
        #
        # ⚠️ 缓存目录不能只躺在目标路径旁边: 换过相册名/命名模板之后, 谁也找不到
        # 它了。所以这里做两件事 ——
        #   ① 先用**清单指纹**确认原地那堆分片是不是这一批(清单 URL 没变而分片
        #      换代是最隐蔽的那种错配, 见 partials.ensure_fingerprint), 不符就清掉;
        #   ② 再从按**清单 URL**寻址的暂存区取回上一次中断留下的分片。
        parts_dir = out / f".{stem}.parts"
        if not partials.ensure_fingerprint(parts_dir, segments) and log:
            log("分片缓存与当前清单不一致(清单变过或来路不明), 已丢弃重下")
        resumed = partials.take_segments(base, parts_dir, segments)
        if resumed and log:
            log(f"暂存区取回 {resumed} 字节分片缓存 (共 {len(segments)} 片)")
        try:
            parts = self._fetch_segments(
                segments, headers, parts_dir, limiter, progress_cb, log
            )
            # 4) 按序合并; 合并成功后才清掉分片缓存
            merged = self._concat_parts(parts, out / f"{stem}.ts", progress_cb)
        except TaskCancelled:
            # 用户点了停止: 已下好的分片收进暂存区, 再让信号原样穿透
            # (⚠️ 这一条必须**显式**写出来, 不能只靠下面的 BaseException 兜着 ——
            #  `tests/test_cancel_guard.py` 会当成吞掉取消信号, 见第 4 条静默坑)
            partials.park_segments(base, parts_dir, segments)
            raise
        except BaseException:
            # 其它失败(分片重试耗尽 / 磁盘错 / 进程级中断): 同样把字节收进暂存区。
            # 原地留着也行, 但换个目标路径就找不到了 —— 那正是这次要解决的事。
            partials.park_segments(base, parts_dir, segments)
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
                self._ffmpeg_remux(
                    merged, mp4, ff, progress_cb=progress_cb
                )
                merged.unlink(missing_ok=True)
                if log:
                    log(f"已 remux 为 {mp4.name}")
                fill_info(info, base, "video/mp4")
                return mp4, sha256_file(mp4, progress_cb=progress_cb)
            except Exception as e:
                if log:
                    log(f"remux 失败, 保留 .ts 容器: {_short(e)}")

        fill_info(info, base, "video/mp2t")
        return merged, sha256_file(merged, progress_cb=progress_cb)

    # ---- DASH (.mpd) ----

    def _fetch_mpd(self, url, headers, log, progress_cb=None):
        """取 MPD 本体并解析。返回 `(清单 URL, 规格 dict)`。

        与 HLS 的 `_preflight_hls` 同一个思路: **先读清单再开工**。区别是 DASH 的
        拒绝理由更多(DRM / 字节区间), 而那些理由必须原样带到用户面前 ——
        它们不是我们的 bug, 是"这份清单不能这么下", 用户看到才知道下一步做什么。

        ⚠️ 直播(`type="dynamic"`)**在这里是允许的**: 由 `_download_mpd` 决定走
        "录制"那条路(见 `_record_live`)。传 `now=time.time()` 是因为直播窗口要靠
        "现在"减出来 —— 少给这个参数会退回清单的 `publishTime`, 而重放一份几小时前
        抓到的清单时那个时间戳会算出早已滚走的窗口。
        """
        with domain_slot(url, progress_cb=progress_cb):
            resp = self.session.get(url, headers=headers, timeout=SHUTDOWN_IO_TIMEOUT)
        try:
            resp.raise_for_status()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" in ctype:
                # ⚠️ 不能让它落进 XML 解析: 有些错误页恰好是良构 XML, 会被解析成
                # "没有 Representation 的 MPD", 报错离真相很远(用户看到的是
                # "MPD 里没有可下载的轨", 而真正的问题是没带 Referer)。
                raise ValueError(
                    f"源站返回的是 HTML(Content-Type: {ctype}), 不是 MPD ——"
                    f" 多半是错误页, 或清单需要登录态/Referer。"
                )
            return url, parse_mpd(resp.text, url, now=time.time(),
                                  allow_dynamic=True)
        finally:
            resp.close()

    def _download_mpd(self, murl, headers, save_dir, progress_cb, log, mirrors=None,
                      filename=None, info=None):
        """DASH: 视频轨与音频轨分别下全, 再用 ffmpeg 合并。

        ⚠️ 与 HLS 最大的结构差别 —— HLS 是"一条分片清单里就是完整节目", 而 DASH
        通常是**音视频两条独立的轨**(各自一个 fMP4 序列)。所以:

        * 没有"只下一份清单"这回事, 必须按轨分别下全再 mux;
        * 也因此**没装 ffmpeg 时只能明确失败**: 只把视频轨落盘会得到一个没有声音
          的文件, 而它在界面上显示"下载成功" —— 那是假成功, 比失败更糟。
          (只有视频轨、没有音频轨的 MPD 除外, 那种情况不需要 mux。)
        """
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        if filename:
            stem = Path(filename).stem
            out = (out / filename).parent
        else:
            stem = safe_filename(murl, "").rsplit(".", 1)[0]
        if not stem:
            stem = "video"
        out.mkdir(parents=True, exist_ok=True)

        # 0) 清单: 主 URL 失败就依次试镜像(与 m3u8 同一套语义)
        leaf, spec, last_err = murl, None, None
        for cand in [murl] + [m for m in (mirrors or []) if m and m != murl]:
            try:
                leaf, spec = self._fetch_mpd(cand, headers, log, progress_cb)
                break
            except TaskCancelled:
                raise
            except Exception as e:
                last_err = e
                if log:
                    log(f"MPD 解析失败 {cand.split('/')[-1]}: {_short(e, 100)}")
        if spec is None:
            # ⚠️ 把原始原因带出去: "DRM 加密""只有字幕轨""多时段"这类结论是用户
            # 唯一能照做的事情, 吞掉它就变成"任务失败但不知道为什么"。
            raise RuntimeError(f"MPD 不可用: {_short(last_err or '空响应', 260)}")

        for note in spec.get("notes") or []:
            if log:
                log(f"DASH {note}")
        if log:
            log(f"清单来源 {leaf.split('/')[-1] or leaf}")

        if spec.get("live"):
            # 直播走另一条路: 它不是"把清单里的分片下完", 而是"按时间录一段"。
            return self._record_live(leaf, spec, headers, out, stem, progress_cb,
                                     log, info, mirrors)

        ff = find_ffmpeg()
        per = spec.get("periods") or []
        multi = len(per) > 1
        if any(p.get("audio") for p in per) and not ff:
            raise RuntimeError(
                "这段 DASH 的音视频分成两条轨, 合并需要 ffmpeg, 但现在没找到它。"
                " 请安装 ffmpeg 或用 UWC_FFMPEG 指定路径 ——"
                " 只下视频轨会得到一个没有声音的文件, 那不是成品。"
            )
        if multi and not ff:
            raise RuntimeError(
                f"这段 DASH 分成 {len(per)} 个时段, 需要 ffmpeg 把各时段逐轨拼起来,"
                f" 但现在没找到它。请安装 ffmpeg 或用 UWC_FFMPEG 指定路径 ——"
                f" 只留一个时段会得到一个少了后半段的文件, 那不是成品。"
            )

        limiter = DomainLimiter(
            settings.segment_concurrency,
            settings.segment_min_interval,
            settings.segment_max_interval,
        )
        # 分片缓存目录沿用 HLS 那套语义: 中断后重跑能续传已下好的分片,
        # 且**换个目标路径也照样能续**(按清单 URL 存在暂存区里)。
        work = out / f".{stem}.dash"
        done = False
        ctype = "video/mp4"
        #: 分片缓存落点 -> (目录, 分片清单)。失败/取消时按这份表 park 进暂存区。
        parked = {}
        try:
            # ---- 逐个时段、逐条轨下全分片 ----
            got_by_kind = {"video": [], "audio": []}
            for pi, period in enumerate(per):
                # 单时段时不套一层 p1/ 目录: 与旧行为完全一致, 也让 "换个相册名
                # 接着下" 的缓存地址不变(它是按清单 URL 寻址的)。
                sub = work / f"p{pi + 1}" if multi else work
                for kind in ("video", "audio"):
                    track = period.get(kind)
                    if not track:
                        continue
                    self._resolve_index(track, headers, log=log)
                    # 初始化段必须排在分片前面: 它是 fMP4 的轨道元数据(moov)。
                    # 少了它, 拼出来的文件播放器直接判为损坏 —— 而字节数是"够"的。
                    segs = ([track["init"]] if track.get("init") else []) + list(
                        track["segments"]
                    )
                    if not segs:
                        raise RuntimeError(
                            f"DASH {kind} 轨没有任何分片可下 —— 清单里的索引是空的。"
                        )
                    if log:
                        tag = f"时段 {pi + 1} " if multi else ""
                        log(f"{tag}{kind} 轨: {len(segs)} 个文件"
                            + ("(含初始化段)" if track.get("init") else ""))
                    # 分片缓存的地址带轨后缀(多时段再加时段后缀): 各轨各有各的
                    # 一份, 不能互相顶掉 —— 顶掉之后拼出来的是"视频头 + 音频身"。
                    addr = f"{leaf}#{kind}" if not multi else f"{leaf}#p{pi + 1}#{kind}"
                    d = sub / kind
                    if not partials.ensure_fingerprint(d, segs) and log:
                        log(f"{kind} 轨: 分片缓存与当前清单不一致, 已丢弃重下")
                    resumed = partials.take_segments(addr, d, segs)
                    if resumed and log:
                        log(f"{kind} 轨: 暂存区取回 {resumed} 字节分片缓存")
                    parked[addr] = (d, segs)
                    got = self._fetch_segments(segs, headers, d, limiter,
                                              progress_cb, log)
                    ext = ".ts" if "mp2t" in (track.get("mime") or "") else ".mp4"
                    got_by_kind[kind].append(
                        self._concat_parts(got, sub / f"{kind}{ext}", progress_cb)
                    )

            # ---- 多时段: 逐轨按序把各时段的产物拼起来 ----
            # ⚠️ 必须**逐轨**拼: 每个时段各有自己的 moov, 字节拼起来是坏文件;
            # 而音视频混着拼会得到音画错位。所以这里交给 ffmpeg 重新封一遍
            # (仍 `-c copy`, 不重编码)。
            parts = {}
            for kind, files in got_by_kind.items():
                if not files:
                    continue
                if len(files) == 1:
                    parts[kind] = files[0]
                else:
                    dst = work / f"{kind}{files[0].suffix or '.mp4'}"
                    self._ffmpeg_concat(files, dst, ff, progress_cb=progress_cb)
                    parts[kind] = dst
                    if log:
                        log(f"{kind} 轨: {len(files)} 个时段的产物已拼接")

            final, ctype = self._finalize_tracks(parts, out, stem, ff,
                                                 progress_cb, log, multi=multi)

            # 落盘后终检: 拿 MPD **自报**的时长对一遍。ffmpeg 退出码 0 但只封了
            # 前一小段的情况是真实存在的(与 HLS 同一种假成功)。
            _note_duration(info, _check_duration(
                final, float(spec.get("duration") or 0), ff,
                progress_cb=progress_cb, what="MPD"))
            if progress_cb:
                _notify_bytes(progress_cb, final)
            fill_info(info, leaf, ctype)
            done = True
            return final, sha256_file(final, progress_cb=progress_cb)
        finally:
            # ⚠️ 只有**成功**才清分片缓存。失败/取消时把分片收进暂存区 —— 那是续传
            # 的资本(与 `_fetch_segments` 的"已下好的分片保留在 parts_dir"同一套语义)。
            if done:
                shutil.rmtree(work, ignore_errors=True)
            else:
                left = False
                for addr, (d, segs) in parked.items():
                    partials.park_segments(addr, d, segs, origin=murl)
                    if d.exists():
                        # 没搬走(体积太小/搬不动) -> 原地那份**保持不动**,
                        # 连同 work 一起留着, 别顺手删掉(见 partials.park 的方向性说明)
                        left = True
                if not left:
                    shutil.rmtree(work, ignore_errors=True)

    def _finalize_tracks(self, parts, out, stem, ff, progress_cb, log, multi=False):
        """把各轨的成品文件落成一个最终文件, 返回 `(路径, content_type)`。

        `parts` 是 `{"video": 路径, "audio": 路径}`(缺轨就是没有那个键)。**点播与
        直播共用这一份**: 三种组合(只有音频 / 只有视频 / 音视频合并)的判据必须
        只有一处, 分开写第二遍迟早有一份漂移 —— 而漂移的表现是"下载成功但没有
        声音", 退出码还是 0。

        ⚠️ 纯音频落成 `.m4a` 而不是 `.mp4`: 拿 `.mp4` 出去, 播放器会按"没有视频轨
        的 mp4"处理, 有的直接报错。
        """
        video = parts.get("video")
        audio = parts.get("audio")
        if video is None:
            # 纯音频的 MPD: 它本身就是成品, 直接按容器后缀落盘
            final = out / f"{stem}.m4a"
            os.replace(audio, final)
            ctype = "audio/mp4"
            if log:
                log(f"这段 MPD 只有音频轨, 直接作为 {final.name} 落盘")
        elif audio is None:
            if (video.suffix == ".ts" or multi) and ff:
                final = out / f"{stem}.mp4"
                self._ffmpeg_remux(video, final, ff, progress_cb=progress_cb)
            else:
                final = out / f"{stem}{video.suffix}"
                os.replace(video, final)
            ctype = "video/mp4"
            if log:
                log("该 MPD 只有视频轨, 无需合并音轨")
        else:
            final = out / f"{stem}.mp4"
            self._ffmpeg_mux(video, audio, final, ff, progress_cb=progress_cb)
            ctype = "video/mp4"
        return final, ctype

    # ---- 直播(DASH type="dynamic")----

    def _record_live(self, leaf, spec, headers, out, stem, progress_cb, log, info,
                     mirrors=None):
        """录一段直播: 反复取清单, 把**新出现的**分片下下来, 到点或源站结束时收工。

        与点播那条路的三点结构差别(每一点都是"照抄会错"的地方):

        1. **清单要反复取**。点播取一次就够; 直播的清单是滚动窗口, 只取一次等于只
           录到取回那一刻的几秒。节奏用清单声明的 `minimumUpdatePeriod`(夹在
           `LIVE_MIN_POLL`~`LIVE_MAX_POLL`), 没声明就退到 4s 且只录一轮
           (那种清单不会变, 再取也是同一份 —— 见 `dash.py` 的说明)。
        2. **分片按"发现顺序"落盘**, 不按它在清单里的位置。清单每轮都在平移, 按位置
           编号会让新分片覆盖旧位置的文件, 拼出来是乱序的。
        3. **取不到老分片不算失败**。窗口一直往前滚, 上一轮的分片这一轮可能已经没了
           (404)—— 那是直播的常态, 记为"漏录"并继续。但**一片都没取到**仍是硬失败:
           那说明地址模板或鉴权不对, "全漏"和"漏几片"是两件事。

        什么时候停: 时间上限(`settings.live_max_seconds`, 0 = 不限时, 录到取消为止)、
        源站把清单改成 `static`(直播结束)、或用户取消。

        ⚠️ 取消时**仍然把已录到的封成文件** —— 与点播"留半成品等续传"故意不同: 直播
        窗口滚过去就再也补不回来, 取消那一刻手上的分片就是全部产物, 丢掉等于把用户
        已经花掉的时间扔了。任务状态照样是"已取消"。
        """
        limit = max(0.0, float(settings.live_max_seconds))
        ff = find_ffmpeg()
        per = spec.get("periods") or []
        if any(p.get("audio") for p in per) and not ff:
            raise RuntimeError(
                "这段直播的音视频分成两条轨, 合并需要 ffmpeg, 但现在没找到它。"
                " 请安装 ffmpeg 或用 UWC_FFMPEG 指定路径 ——"
                " 只录视频轨会得到一个没有声音的文件, 那不是成品。"
            )

        limiter = DomainLimiter(
            settings.segment_concurrency,
            settings.segment_min_interval,
            settings.segment_max_interval,
        )
        work = out / f".{stem}.dash"
        work.mkdir(parents=True, exist_ok=True)
        mup = spec.get("minimum_update_period")
        poll = min(LIVE_MAX_POLL, max(LIVE_MIN_POLL,
                                      float(mup or LIVE_DEFAULT_POLL)))
        started = time.monotonic()
        if log:
            budget = f"{limit:.0f}s" if limit else "不限时(直到取消或流结束)"
            log(f"直播录制开始: 每 {poll:.0f}s 取一次清单, 录制上限 {budget}")

        #: kind -> 已经"记过账"的分片地址(含初始化段)。⚠️ 失败的分片也记进来 ——
        #: 见 `_fetch_one_segment` 内部已有重试; 反复把同一片放进待下队列, 只会让
        #: "漏了多少"这个数字失真。
        seen = {"video": set(), "audio": set()}
        #: kind -> 已经下过的**初始化段**地址。多时段时每个时段各有自己的一份,
        #: 所以判据是"这个 init 见过没有"而不是"是不是第一轮"。
        inited = {"video": set(), "audio": set()}
        #: kind -> 已下好的分片路径, **按发现顺序**(就是拼接顺序)
        parts = {"video": [], "audio": []}
        #: `(时段序号, kind)` -> 上一轮该轨的最后一片。⚠️ 键里必须带时段序号:
        #: 多时段时每段各有自己的滚动窗口, 只按 kind 记会把后面时段的末尾当成整条
        #: 轨的末尾, 于是**前面时段新出现的分片全被当成"已经过去了"跳过**, 而且
        #: 不报错(录出来的文件就是缺了那几秒)。
        tail = {}
        #: kind -> 下一片文件名的序号(只保证唯一与递增, 不是拼接依据)
        seq = {"video": 0, "audio": 0}
        missed = 0
        total = 0
        scrolled_warned = False
        fetch_failures = 0
        first_error = []

        def absorb(cur):
            """把这一轮清单里**没见过的**分片下下来。

            返回 `(新增片数, 失败片数, 上一轮末尾是否已滚出窗口)`。
            """
            added = failed = 0
            moved = False
            for pi, period in enumerate(cur.get("periods") or []):
                for kind in ("video", "audio"):
                    track = period.get(kind)
                    if not track:
                        continue
                    self._resolve_index(track, headers, log=log)
                    items = []
                    init = track.get("init")
                    if init:
                        ikey = _seg_key(init)
                        # 初始化段排在**它所属时段的分片前面** —— 少了它拼出来的
                        # 文件没有轨道元数据, 播放器直接判为损坏, 而字节数是"够"的。
                        # ⚠️ 这里**不能**顺手把它塞进 `seen`: `seen` 是下面那条
                        # "没见过的才下"的过滤器, 塞进去等于把 init 自己过滤掉 ——
                        # 表现是文件能播但打不开/没画面, 长度一切正常。
                        # 防重复靠 `inited`, 它管的就是这件事。
                        if ikey not in inited[kind]:
                            if inited[kind] and log:
                                log(f"直播 {kind}: 出现新的初始化段"
                                    f"(时段切分或编码器重启) —— 按顺序接在后面")
                            inited[kind].add(ikey)
                            items.append(init)
                    items += list(track["segments"])
                    keys = [_seg_key(s) for s in items]
                    slot = (pi, kind)
                    prev = tail.get(slot, "")
                    # ⚠️ 只往前录, 不回头: 上一轮末尾之前的分片一律不再考虑。两个理由都
                    # 跟"窗口只前进"有关 —— ① 已经滚出去的那些再请求只会换回 404;
                    # ② 流结束时源站会把清单换成 static(带完整时长), 那时"回头"意味着
                    # 从节目开头重下一整遍。
                    cut = keys.index(prev) + 1 if prev in keys else 0
                    fresh = [(s, k) for s, k in zip(items[cut:], keys[cut:])
                             if k not in seen[kind]]
                    if prev and cut == 0 and fresh:
                        moved = True         # 我们的末尾已经不在窗口里了
                    for _, k in fresh:
                        seen[kind].add(k)
                    if fresh and log:
                        log(f"直播 {kind}: 本轮新增 {len(fresh)} 片"
                            f"(已录 {len(parts[kind])})")
                    for seg, _ in fresh:
                        n = seq[kind]
                        seq[kind] = n + 1
                        d = work / kind
                        d.mkdir(parents=True, exist_ok=True)
                        part = d / f"{n:06d}.part"
                        try:
                            self._fetch_one_segment(part, seg, headers, limiter,
                                                    progress_cb,
                                                    label=f"直播 {kind} #{n}")
                        except TaskCancelled:
                            raise
                        except Exception as e:
                            failed += 1
                            if not first_error:
                                first_error.append(e)
                            if log:
                                log(f"直播 {kind} #{n} 没取到: {_short(e, 120)}"
                                    f"(直播分片滚出窗口后就补不回来了)")
                            continue
                        parts[kind].append(part)
                        added += 1
                    if keys:
                        tail[slot] = keys[-1]
            return added, failed, moved

        try:
            round_no = 0
            while True:
                round_no += 1
                if progress_cb:
                    progress_cb()          # 顺带在这里响应取消
                added, failed, moved = absorb(spec)
                total += added
                missed += failed
                if moved and not scrolled_warned:
                    scrolled_warned = True
                    if log:
                        log("直播窗口已经滚过上一轮的末尾 —— 中间可能有分片没录到"
                            "(下一轮不会再回头补)")
                if not total:
                    # 一片都没录到: 地址模板/鉴权/时钟三者之一不对。**明确失败**,
                    # 不产出一个"能播但空的"文件。
                    why = f" (首个错误: {_short(first_error[0], 200)})" if first_error else ""
                    raise RuntimeError(
                        f"直播录制期间一个分片都没取到{why} —— 清单里的分片地址"
                        f"可能不对, 或这些分片都已经滚出可用窗口。"
                    )
                if not spec.get("live"):
                    # 源站把清单改成了 static: 节目结束了, 这一轮下完就是完整内容
                    if log:
                        log("直播清单已变成 static —— 流已结束, 录到这里为止")
                    break
                elapsed = time.monotonic() - started
                if limit and elapsed >= limit:
                    if log:
                        log(f"直播已录 {elapsed:.0f}s(达到上限 {limit:.0f}s), 收工")
                    break
                wait = poll if not limit else min(poll, max(0.0, limit - elapsed))
                if wait <= 0:
                    break
                self._interruptible_sleep(wait, progress_cb)
                # ---- 取下一份清单 ----
                nxt, last = None, None
                for cand in [leaf] + [m for m in (mirrors or []) if m and m != leaf]:
                    try:
                        nxt = self._fetch_mpd(cand, headers, log, progress_cb)[1]
                        break
                    except TaskCancelled:
                        raise
                    except Exception as e:
                        last = e
                if nxt is None:
                    fetch_failures += 1
                    if log:
                        log(f"第 {round_no} 轮取清单失败({fetch_failures}/"
                            f"{LIVE_MAX_FETCH_FAILURES}): {_short(last, 120)}")
                    if fetch_failures >= LIVE_MAX_FETCH_FAILURES:
                        raise RuntimeError(
                            f"直播录制连续 {fetch_failures} 次取不回清单, 放弃:"
                            f" {_short(last, 200)} —— 已经录到的分片留在 "
                            f"{work.name}/ 里。"
                        )
                    continue
                fetch_failures = 0
                spec = nxt

            final, ctype = self._finalize_live(work, out, stem, parts, spec, ff,
                                               progress_cb, log)
            # 成品已经在 out 里了, 工作目录里那堆分片是纯冗余 —— 一次五分钟的录制
            # 会让磁盘上多出一份等大的垃圾, 而且没有任何地方会来清它。
            # (失败路径**不删**: 那种情况下它是唯一的证据与续录材料, 报错信息也
            # 指向它。)
            shutil.rmtree(work, ignore_errors=True)
            elapsed = time.monotonic() - started
            if log:
                # ⚠️ "漏了几片"必须说出来。它不影响退出码, 也不影响文件能不能播 ——
                # 用户唯一的感知是"这几秒怎么跳过去了", 而没有任何地方告诉他。
                gap = f", 其中 {missed} 片没取到(已滚出窗口)" if missed else ""
                log(f"直播录制结束: 共录 {total + missed} 片, 成功 {total} 片{gap}"
                    f", 墙钟 {elapsed:.0f}s")
            # ⚠️ 直播没有"清单声明的时长"可对, expected 传 0 = 只回报实测值, 不下
            # 截断结论(录多久是我们自己定的, 拿它当"应该多长"是循环论证)。
            _note_duration(info, _check_duration(final, 0.0, ff,
                                                 progress_cb=progress_cb,
                                                 what="直播"))
            if progress_cb:
                _notify_bytes(progress_cb, final)
            fill_info(info, leaf, ctype)
            return final, sha256_file(final, progress_cb=progress_cb)
        except TaskCancelled:
            # ⚠️ 直播与点播在这件事上**故意不一样**: 点播取消留的是"下次能接着下"的
            # 半成品; 直播窗口滚过去就补不回来了, 取消那一刻手上的分片就是全部产物。
            # 所以这里仍封一次文件再让取消信号照原样穿出去(任务状态照旧是"已取消",
            # 但文件是能看的)。封文件时 `progress_cb` 传 None: 取消信号会让它立刻
            # 再抛一次, 那不是"取消生效了", 是"文件没封出来"。
            try:
                final, _ = self._finalize_live(work, out, stem, parts, spec, ff,
                                               None, log)
                if log:
                    gap = f"(另有 {missed} 片没取到)" if missed else ""
                    log(f"取消时已把录到的 {total} 片封成 {final.name}{gap}")
                shutil.rmtree(work, ignore_errors=True)
            except Exception as e:
                # 封不出来不算失败: 取消本身就要发生, 别让它被这里掩盖
                if log:
                    log(f"取消时封文件失败({_short(e, 120)}), 分片留在 {work.name}/ 里")
            raise

    def _finalize_live(self, work, out, stem, parts, spec, ff, progress_cb, log):
        """把直播录到的分片按发现顺序拼起来并落盘, 返回 `(路径, content_type)`。

        ⚠️ 顺序取 `parts` 的**列表顺序**(= 发现顺序), 不按文件名排序 —— 文件名里的
        序号只保证唯一, 失败的分片会造成空洞, 依赖排序是自找麻烦。
        """
        got = {}
        for kind in ("video", "audio"):
            if not parts.get(kind):
                continue
            # 容器后缀按轨的 mime 定: TS 分片拼出来是 .ts, fMP4 是 .mp4
            track = None
            for period in spec.get("periods") or []:
                if period.get(kind):
                    track = period[kind]
                    break
            ext = ".ts" if "mp2t" in ((track or {}).get("mime") or "") else ".mp4"
            dst = work / f"{kind}{ext}"
            got[kind] = self._concat_parts(parts[kind], dst, progress_cb)
        if not got:
            raise RuntimeError("直播录制期间没有任何分片可拼接 —— 无法产出文件。")
        return self._finalize_tracks(got, out, stem, ff, progress_cb, log)

    def _fetch_range(self, media, rng, headers):
        """取回 `media` 文件里 `rng` 那段字节, 返回 `bytes`。

        ⚠️ 服务器忽略 `Range` 直接把整个文件返回时**明确报错**: 那种情况下 `blob`
        从文件开头开始, 而调用方是拿"索引区间的绝对偏移"去解它的, 解出来的分片
        边界会整体错位 —— 拼出来长度对得上、能播、但每隔几秒糊一下。

        判据是"状态 206 **或** 字节数恰好等于区间长度", 两者有一个成立就说明区间
        被尊重了(个别 CDN 回 200 + 精确长度)。
        """
        start, end = rng
        url, hdrs = _seg_target((media, (start, end)), headers)
        resp = None
        try:
            resp = self.session.get(url, headers=hdrs, timeout=SHUTDOWN_IO_TIMEOUT)
            resp.raise_for_status()
            blob = resp.content
            status = getattr(resp, "status_code", 0)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        want = end - start + 1
        if status != 206 and len(blob) != want:
            raise RuntimeError(
                f"请求区间 {start}-{end} 时服务器没有按区间返回"
                f"(状态 {status}, 返回 {len(blob)} 字节)。无法确定分片边界,"
                f" 硬解会得到错位的分片。"
            )
        return blob

    def _expand_sidx(self, media, start, blob, headers, log=None):
        """展开一段 `sidx`, 返回 `[(起, 止), ...]`(**按文件里的先后顺序**)。

        单层直接解; 遇到 `reference_type=1`(嵌套索引)就取回那一小段字节再解一层。
        真实站点两三层就到头了, 但这里仍然设上限(`SIDX_MAX_DEPTH` /
        `SIDX_MAX_INDEXES`)—— 因为引用可以成环, 而上限到了**报错**而不是截断:
        少下几片的表现是"长度对得上、画面断了一截", 比失败难发现得多。

        ⚠️ 顺序不能重排: 嵌套索引的字节区间与它展开出来的媒体区间在文件里是紧挨着
        的前后关系。所以这里必须**原地** DFS —— 读到 `reference_type=1` 就立刻把
        那一层走完再回到父层的下一条引用。先收集父层全部媒体、事后再补子层的话,
        子层的分片会被排到父层后半段的后面, 拼出来是错位的文件(长度对得上、能播)。

        上限到了**报错**而不是截断: 少下几片的表现是"长度对得上、画面断了一截",
        比失败难发现得多。
        """
        out = []
        state = {"fetched": 0}

        def walk(box_start, data, depth):
            for kind, rng in _dash.parse_sidx_refs(data, box_start):
                if kind == "media":
                    out.append(rng)
                    continue
                if depth + 1 > SIDX_MAX_DEPTH:
                    raise RuntimeError(
                        f"sidx 的嵌套索引超过 {SIDX_MAX_DEPTH} 层 —— 这份索引结构"
                        f" 异常(可能是坏的或成环的), 不继续展开。"
                    )
                state["fetched"] += 1
                if state["fetched"] > SIDX_MAX_INDEXES:
                    raise RuntimeError(
                        f"sidx 展开需要取回超过 {SIDX_MAX_INDEXES} 个索引 ——"
                        f" 这份索引结构异常, 不继续展开。"
                    )
                walk(rng[0], self._fetch_range(media, rng, headers), depth + 1)

        walk(start, blob, 0)
        return out

    def _resolve_index(self, track, headers, log=None):
        """`SegmentBase` 的分片要先取回 `sidx` 才算得出来 —— 在这里补上。

        只请求 `indexRange` 那一段字节(几百字节), 不是整个文件。嵌套索引会按需
        再取它自己那一小段。
        """
        idx = track.get("index")
        if not idx:
            return track
        media = idx["media"]
        start, end = idx["range"]
        blob = self._fetch_range(media, (start, end), headers)
        ranges = self._expand_sidx(media, start, blob, headers, log=log)
        track["segments"] = [(media, r) for r in ranges]
        if log:
            log(f"sidx 解出 {len(ranges)} 个分片 (索引 {start}-{end})")
        return track

    def _ffmpeg_concat(self, files, dst, ff, progress_cb=None):
        """按序把多个**成品**文件拼成一个(`-c copy`, 不重编码)。

        与 `_concat_parts` 的分工: 那个是**字节拼接**, 只能用于"同一个序列的分片";
        这里是**容器级**拼接 —— 每个时段各有自己的 moov, 字节拼起来是坏文件,
        必须让 ffmpeg 重新封一遍。
        """
        if not ff:
            raise RuntimeError(
                "这段 DASH 分成多个时段, 逐轨拼接需要 ffmpeg, 但现在没找到它。"
                " 请安装 ffmpeg 或用 UWC_FFMPEG 指定路径 ——"
                " 只留一个时段会得到一个少了后半段的文件, 那不是成品。"
            )
        lst = Path(dst).with_name(Path(dst).name + ".concat.txt")

        def esc(p):
            # ⚠️ concat demuxer 的清单里单引号是字符串结束符: 相册名/文件名里
            # 出现 `'` 会让它读到半个路径 -> "文件不存在", 而文件明明在那里。
            return str(Path(p).resolve()).replace("'", "'\\''")

        lst.write_text(
            "\n".join(f"file '{esc(p)}'" for p in files) + "\n",
            encoding="utf-8", newline="",
        )
        try:
            self._run_ffmpeg(
                [ff, "-y", "-nostats", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c", "copy", str(dst)],
                progress_cb=progress_cb,
            )
        finally:
            lst.unlink(missing_ok=True)

    def _preflight_hls(self, murl, headers, log, progress_cb=None):
        """下载前预检播放列表, 返回 (leaf_url, info)。

        info.ok 为 False 时本方法**抛异常**(带人话原因), 调用方不该继续下载。
        对 master playlist 自动下钻到最高码率的子列表, 并对叶子节点再校验一遍
        (占位列表常藏在叶子层, 只在 master 上校验会漏)。
        """
        seen = set()
        url = murl
        for _ in range(4):
            if progress_cb:
                progress_cb()
            info = inspect_playlist(
                url,
                headers=headers,
                session=self.session,
                log=log,
                timeout=SHUTDOWN_IO_TIMEOUT,
            )
            if progress_cb:
                progress_cb()
            if not info["ok"]:
                raise RuntimeError(f"播放列表校验未通过: {info['reason']}")
            # master: 没有分片、只有变体 -> 选最高码率继续下钻
            if info["variants"] and not info["segments"]:
                best = max(
                    info["variants"], key=lambda v: v.get("bandwidth", 0)
                )["url"]
                if best in seen:
                    raise RuntimeError("master 播放列表成环, 无法定位子列表")
                seen.add(best)
                url = best
                continue
            return url, info
        raise RuntimeError("播放列表嵌套过深, 疑似 master 链成环")

    def _fetch_one_segment(self, part, seg, headers, limiter, progress_cb,
                           label="分片"):
        """下载**一个**分片到 `part`: 先写 `.tmp`, 读完再原子替换。失败抛异常。

        抽出来是因为直播录制也得下分片, 但它的目标文件名不是"按位置编号"而是"按
        发现顺序编号" —— 差别在**调用方**, "怎么安全地取一个分片"必须只有一份实现。
        尤其是 `finally` 里那句 `resp.close()`: 抄第二遍必然漏, 而漏掉它的症状是
        "任务卡死"而不是报错(见下)。带 `Range` 的分片由 `_seg_target` 负责加头。
        """
        url, hdrs = _seg_target(seg, headers)
        last = None
        for attempt in range(1, settings.segment_retries + 1):
            tmp = part.with_name(part.name + ".tmp")
            resp = None
            try:
                with limiter.slot():
                    resp = self.session.get(
                        url, headers=hdrs, stream=True,
                        timeout=SHUTDOWN_IO_TIMEOUT,
                    )
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        if chunk:
                            f.write(chunk)
                            # 每块都回调: 单片可能很大, 只按分片回调的话
                            # "停止"要等整片下完才生效
                            if progress_cb:
                                _notify_bytes(progress_cb, chunk)
                tmp.replace(part)
                return
            except TaskCancelled:
                tmp.unlink(missing_ok=True)
                raise
            except Exception as e:
                last = e
                tmp.unlink(missing_ok=True)
                if attempt < settings.segment_retries:
                    self._interruptible_sleep(min(2 ** attempt, 5), progress_cb)
            finally:
                # ⚠️ **必须关**。会走到这里的失败路径(404 → raise_for_status、
                # 读中断、取消)都没把响应体读完, 连接不会被自动归还; 而会话是
                # `pool_block=True` 的 —— 泄漏够多之后后面的分片会**永久阻塞在
                # `_get_conn`**, 症状是"任务卡死"而不是报错, 与真实原因(某个 URL
                # 404)看起来毫无关系。实测: 10 个分片全 404, 池一满整个下载就不动了。
                # 池大小 = `max(10, domain_concurrency*2)`(见 base._build_session)。
                # 读完的路径 close() 是幂等的, 顺带把连接还回池子。
                # (与 base.py `_stream_one` 里那条纪律同一型 —— 这是第二处。)
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
        raise RuntimeError(f"{label} 失败: {_short(last)}")

    def _fetch_segments(self, segments, headers, parts_dir, limiter, progress_cb, log):
        """并发下载分片, 返回按序排列的 part 路径。

        「分片」有两种寻址:
          * URL 字符串 —— 一片一个文件;
          * `(URL, (起, 止))` —— 同一个文件里的字节区间(`SegmentBase` 的 sidx
            分片 / `SegmentList@mediaRange`), 会带上 `Range` 头。

        ⚠️ 带 `Range` 时**不发**任何条件请求(ETag/If-None-Match)。服务器对
        "条件 + Range" 会回 304 而不是 206, 于是拿到的不是那一段字节 ——
        与 `downloaders/base.py` 里那条纪律是同一件事(条件请求 ⊥ 续传)。

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
            self._fetch_one_segment(part, segments[i], headers, limiter,
                                    progress_cb, label=f"分片 {i + 1}/{total}")

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
                        # 字节数已在每个 chunk 回调时上报过, 这里只做"完成一片"
                        # 的心跳(顺带响应停止); 传 0 避免同一批字节被计两次
                        _notify_bytes(progress_cb, None)

        if errors:
            # 已下好的分片保留在 parts_dir, 重跑时可续传
            raise RuntimeError(
                f"{len(errors)}/{total} 个分片下载失败 (首条: {errors[0]})"
            )
        return paths

    @staticmethod
    def _interruptible_sleep(seconds, progress_cb=None):
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))
            if progress_cb:
                progress_cb()

    # ---- ffmpeg ----

    def _ffmpeg_pull(self, url, headers, path, ff, progress_cb=None):
        """让 ffmpeg 直接拉 m3u8 并输出到 path(`-c copy` 不重编码)。

        请求头经 `-headers` 传给 http 协议, 必须放在 `-i` 之前。
        注意 Accept 不可省 —— 部分 WAF 缺了它直接 403。
        `-nostats` 抑制进度行: 我们用 capture_output 收 stderr,
        长视频的进度输出会白白堆积在内存里。
        """
        args = []
        for k, v in headers.items():
            args += ["-headers", f"{k}: {v}\r\n"]
        # 网络加固: 一部长片可以上千个分片, 几分钟里必然遇到几次瞬断。不重连的话
        # 一次抖动就得从头再来 —— 而这期间那把短命签名可能已经过期了(实测约 30 分钟)。
        # `-reconnect_streamed 1` 针对非 seekable 流(正是 HLS), 缺了它重连不生效。
        net = [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
        ]
        # `allowed_extensions` 默认只放行一份白名单扩展名; 正规站点都是 .ts/.m4s,
        # 但有些 CDN 用 .jpg/.php 之类的伪装分片 URL, 默认值会直接拒绝拉取。
        # 放在 -i 之前(它是 demuxer 选项)。
        self._run_ffmpeg([
            ff, "-y", "-nostats", "-xerror", *args, *net,
            "-allowed_extensions", "ALL",
            "-i", url, "-c", "copy", str(path),
        ], progress_cb=progress_cb)

    def _ffmpeg_remux(self, src, dst, ff, progress_cb=None):
        self._run_ffmpeg(
            [ff, "-y", "-nostats", "-i", str(src), "-c", "copy", str(dst)],
            progress_cb=progress_cb,
        )

    def _ffmpeg_mux(self, video, audio, dst, ff, progress_cb=None):
        """把分开的视频轨与音频轨合进一个容器(`-c copy`, 不重编码)。

        `-map` 显式指定取哪条流: 不写的话 ffmpeg 的默认流选择规则会随输入而变
        (比如它可能挑到 fMP4 里的时间码轨), 选错的表现是"合并成功但没有声音",
        而且退出码是 0。显式写死 `0:v:0` + `1:a:0` 让结果可预测。
        """
        self._run_ffmpeg(
            [ff, "-y", "-nostats",
             "-i", str(video), "-i", str(audio),
             "-map", "0:v:0", "-map", "1:a:0",
             "-c", "copy", str(dst)],
            progress_cb=progress_cb,
        )

    @staticmethod
    def _concat_parts(parts, dst, progress_cb=None):
        """按序把分片拼成一个文件。取消时删掉半截产物再上抛。

        HLS 与 DASH 共用这一份 —— 拼接顺序错了(HLS 那层是静默缺陷的高发区)
        只在一个地方需要修。
        """
        dst = Path(dst)
        try:
            with open(dst, "wb") as out:
                for p in parts:
                    with open(p, "rb") as src:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            if progress_cb:
                                progress_cb()
                    # 长视频几百片, 合并阶段也要能响应"停止"
                    if progress_cb:
                        _notify_bytes(progress_cb, p)
        except TaskCancelled:
            dst.unlink(missing_ok=True)
            raise
        return dst

    @staticmethod
    def _run_ffmpeg(cmd, progress_cb=None):
        """执行 ffmpeg; 失败时把 stderr 尾行带出来。

        旧实现是 `except Exception: pass`, 真实原因(协议不支持/编码不兼容)
        全部丢失, 排查时只能看到降级后那条误导性的报错。
        """
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = VideoDownloader._communicate_process(
                proc, cmd, timeout=3600, progress_cb=progress_cb
            )
            if proc.returncode:
                raise subprocess.CalledProcessError(
                    proc.returncode, cmd, output=stdout, stderr=stderr
                )
        except TaskCancelled:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            raise
        except subprocess.TimeoutExpired:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            raise
        except FileNotFoundError as e:
            # 路径来自 core.ffmpeg 探测, 到这里说明探测后又被动过(卸载/移动)
            raise RuntimeError(f"ffmpeg 不可执行: {cmd[0]}") from e
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            raise RuntimeError(
                f"ffmpeg 退出码 {e.returncode}: {tail[-1] if tail else '无 stderr'}"
            ) from e

    @staticmethod
    def _communicate_process(proc, cmd, timeout, progress_cb=None):
        """Poll a child so cancellation can terminate it within the Host gate."""
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    return proc.communicate(timeout=min(0.5, remaining))
                except subprocess.TimeoutExpired:
                    if progress_cb:
                        progress_cb()
        except (TaskCancelled, subprocess.TimeoutExpired):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            raise
