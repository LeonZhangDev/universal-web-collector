"""ffmpeg 定位 (core/ffmpeg.py) 与 video_engine 选择 (downloaders/video.py)。

全部用 monkeypatch 隔离, 不依赖机器上是否真的装了 ffmpeg ——
这样在没装 ffmpeg 的环境/CI 上也能跑。
"""

import struct
import sys
from pathlib import Path

import pytest

import core.ffmpeg as ffm
from core import config as cfgmod
from core.cancel import TaskCancelled
from core.config import settings
from core.errors import CorruptMediaError
import downloaders.video as video_mod
from downloaders.video import (
    ENGINES,
    VideoDownloader,
    _is_dash,
    _is_hls,
    _short,
    _validate_hls_duration,
)


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    # 本机配了系统代理(127.0.0.1:6109), 涉及本地地址的测试必须绕开它
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    ffm.clear_cache()
    yield
    ffm.clear_cache()


# ---- find_ffmpeg 探测顺序 ----

def test_configured_path_wins(monkeypatch, tmp_path):
    """显式配置优先级最高, 压过 PATH。"""
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(settings, "ffmpeg_path", str(exe))
    monkeypatch.setattr(ffm.shutil, "which", lambda n: "/from/path/ffmpeg")
    assert ffm.find_ffmpeg() == str(exe)


def test_invalid_config_falls_back_to_autodetect(monkeypatch, tmp_path):
    """配置的路径不存在时不报错, 继续往下探测(宽容行为)。"""
    monkeypatch.setattr(settings, "ffmpeg_path", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(ffm.shutil, "which", lambda n: "/from/path/ffmpeg")
    assert ffm.find_ffmpeg() == "/from/path/ffmpeg"


def test_path_used_when_no_config(monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr(ffm.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    assert ffm.find_ffmpeg() == "/usr/bin/ffmpeg"


def test_known_install_locations_scanned(monkeypatch, tmp_path):
    """PATH 里没有时, 仍应扫到常见安装目录(winget/scoop/手工解压 …)。

    这是本模块存在的理由: 服务运行期间新装的 ffmpeg 不在进程 PATH 里。
    """
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr(ffm.shutil, "which", lambda n: None)
    monkeypatch.setattr(ffm, "_windows_candidates", lambda: [tmp_path / "no.exe", exe])
    monkeypatch.setattr(ffm, "_unix_candidates", lambda: [tmp_path / "no.exe", exe])
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    assert ffm.find_ffmpeg() == str(exe)


def test_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr(ffm.shutil, "which", lambda n: None)
    monkeypatch.setattr(ffm, "_windows_candidates", lambda: [])
    monkeypatch.setattr(ffm, "_unix_candidates", lambda: [])
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    assert ffm.find_ffmpeg() is None
    assert ffm.ffmpeg_available() is False


def test_imageio_ffmpeg_used_as_last_resort(monkeypatch, tmp_path):
    """前面都找不到时, 回退到 imageio-ffmpeg 自带的静态二进制。"""
    exe = tmp_path / "ffmpeg-static"
    exe.write_bytes(b"")

    class FakeImageio:
        @staticmethod
        def get_ffmpeg_exe():
            return str(exe)

    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr(ffm.shutil, "which", lambda n: None)
    monkeypatch.setattr(ffm, "_windows_candidates", lambda: [])
    monkeypatch.setattr(ffm, "_unix_candidates", lambda: [])
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", FakeImageio)
    assert ffm.find_ffmpeg() == str(exe)


# ---- 缓存行为 ----

def test_success_is_cached(monkeypatch):
    calls = []

    def probe(configured):
        calls.append(configured)
        return "/cached/ffmpeg"

    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr(ffm, "_probe", probe)
    assert ffm.find_ffmpeg() == "/cached/ffmpeg"
    assert ffm.find_ffmpeg() == "/cached/ffmpeg"
    assert len(calls) == 1


def test_missing_result_is_cached_but_expires(monkeypatch):
    """探测失败也会缓存一小段时间, 避免每次下载都扫一遍目录。"""
    calls = []

    def probe(configured):
        calls.append(configured)
        return None

    monkeypatch.setattr(settings, "ffmpeg_path", None)
    monkeypatch.setattr(ffm, "_probe", probe)

    assert ffm.find_ffmpeg() is None
    assert ffm.find_ffmpeg() is None
    assert len(calls) == 1, "失败结果应命中缓存"

    assert ffm.find_ffmpeg(refresh=True) is None
    assert len(calls) == 2, "refresh=True 应绕过缓存"


def test_changing_configured_path_uses_fresh_cache_key(monkeypatch, tmp_path):
    """改了 settings.ffmpeg_path 要重新探测, 不能吃到旧缓存。"""
    a, b = tmp_path / "a.exe", tmp_path / "b.exe"
    a.write_bytes(b"")
    b.write_bytes(b"")

    monkeypatch.setattr(settings, "ffmpeg_path", str(a))
    assert ffm.find_ffmpeg() == str(a)
    monkeypatch.setattr(settings, "ffmpeg_path", str(b))
    assert ffm.find_ffmpeg() == str(b)


# ---- video_engine 选择 ----

def test_engines_constant():
    assert set(ENGINES) == {"auto", "ffmpeg", "builtin"}


def test_engine_ffmpeg_without_binary_raises(monkeypatch, tmp_path):
    """engine=ffmpeg 但找不到可执行文件时必须报错, 不能静默降级成内置器 ——
    否则用户以为产物是 ffmpeg 拉的, 实际不是。"""
    monkeypatch.setattr(settings, "video_engine", "ffmpeg")
    monkeypatch.setattr("downloaders.video.find_ffmpeg", lambda refresh=False: None)
    with pytest.raises(RuntimeError, match="未找到 ffmpeg"):
        VideoDownloader().download("https://a.com/x.m3u8", save_dir=tmp_path)


def test_engine_builtin_never_calls_ffmpeg_pull(monkeypatch, tmp_path):
    """engine=builtin 时, 即使 ffmpeg 可用也不该走它的拉流分支。"""
    pulled = []
    monkeypatch.setattr(settings, "video_engine", "builtin")
    monkeypatch.setattr(
        "downloaders.video.find_ffmpeg", lambda refresh=False: "/fake/ffmpeg"
    )
    monkeypatch.setattr(
        VideoDownloader, "_ffmpeg_pull", lambda self, *a, **k: pulled.append(a)
    )
    # 指向必然连不上的本地端口: 走内置器解析 m3u8 -> 失败, 但足以确认没走 ffmpeg
    with pytest.raises(RuntimeError):
        VideoDownloader().download("http://127.0.0.1:1/x.m3u8", save_dir=tmp_path)
    assert pulled == [], "engine=builtin 时不应调用 ffmpeg 拉流"


def test_ffmpeg_pull_heartbeats_and_terminates_on_cancel(monkeypatch):
    """长 HLS 拉流必须持续心跳；取消回调抛出后要收掉精确子进程。"""
    calls = []

    class FakeProcess:
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len([c for c in calls if c[0] == "communicate"]) == 1:
                raise video_mod.subprocess.TimeoutExpired(["ffmpeg"], timeout)
            self.returncode = -15
            return b"", b""

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append(("terminate", None))

        def kill(self):
            calls.append(("kill", None))

    monkeypatch.setattr(video_mod.subprocess, "Popen", lambda *a, **k: FakeProcess())

    def cancel_tick():
        calls.append(("tick", None))
        raise TaskCancelled()

    with pytest.raises(TaskCancelled):
        VideoDownloader._run_ffmpeg(["ffmpeg"], progress_cb=cancel_tick)

    assert ("tick", None) in calls
    assert ("terminate", None) in calls
    assert ("kill", None) not in calls


def test_ffmpeg_remux_is_cancellation_aware(monkeypatch, tmp_path):
    calls = []

    class FakeProcess:
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if self.returncode is None:
                raise video_mod.subprocess.TimeoutExpired(["ffmpeg"], timeout)
            return b"", b""

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append(("terminate", None))
            self.returncode = -15

        def kill(self):
            calls.append(("kill", None))
            self.returncode = -9

    monkeypatch.setattr(video_mod.subprocess, "Popen", lambda *a, **k: FakeProcess())

    with pytest.raises(TaskCancelled):
        VideoDownloader()._ffmpeg_remux(
            tmp_path / "in.ts",
            tmp_path / "out.mp4",
            "ffmpeg",
            progress_cb=lambda: (_ for _ in ()).throw(TaskCancelled()),
        )

    assert ("terminate", None) in calls


def test_ffprobe_is_cancellation_aware(monkeypatch, tmp_path):
    calls = []

    class FakeProcess:
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if self.returncode is None:
                raise video_mod.subprocess.TimeoutExpired(["ffprobe"], timeout)
            return b"", b""

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append(("terminate", None))
            self.returncode = -15

        def kill(self):
            calls.append(("kill", None))
            self.returncode = -9

    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(video_mod.subprocess, "Popen", lambda *a, **k: FakeProcess())

    with pytest.raises(TaskCancelled):
        video_mod._probe_media_duration(
            tmp_path / "out.mp4",
            "/usr/bin/ffmpeg",
            progress_cb=lambda: (_ for _ in ()).throw(TaskCancelled()),
        )

    assert ("terminate", None) in calls


def test_hls_duration_validation_rejects_successful_but_truncated_output(
    monkeypatch, tmp_path
):
    """ffmpeg exit 0 也可能只产出前几分钟，必须与预检总时长交叉校验。"""
    path = tmp_path / "truncated.mp4"
    path.write_bytes(b"not-used")
    monkeypatch.setattr(video_mod, "_probe_media_duration", lambda *a: 270.0)

    with pytest.raises(RuntimeError, match="截断"):
        _validate_hls_duration(path, {"duration": 5442.0}, "/usr/bin/ffmpeg")


def test_hls_duration_validation_fails_closed_without_probe(monkeypatch, tmp_path):
    path = tmp_path / "unverified.mp4"
    path.write_bytes(b"not-used")
    monkeypatch.setattr(video_mod, "_probe_media_duration", lambda *a: None)

    with pytest.raises(RuntimeError, match="无法验证"):
        _validate_hls_duration(path, {"duration": 5442.0}, "/usr/bin/ffmpeg")


def test_hls_duration_probe_timeout_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "timeout.mp4"
    path.write_bytes(b"not-used")
    monkeypatch.setattr(video_mod.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        video_mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            video_mod.subprocess.TimeoutExpired(a[0], k.get("timeout"))
        ),
    )

    with pytest.raises(RuntimeError, match="无法验证"):
        _validate_hls_duration(path, {"duration": 5442.0}, "/usr/bin/ffmpeg")


@pytest.mark.parametrize(
    ("actual", "accepted"),
    [(99.0, True), (98.999, False), (9.6, True), (9.4, False)],
)
def test_hls_duration_boundary_and_short_container_tolerance(
    monkeypatch, tmp_path, actual, accepted
):
    expected = 100.0 if actual > 20 else 10.0
    path = tmp_path / "boundary.mp4"
    path.write_bytes(b"not-used")
    monkeypatch.setattr(video_mod, "_probe_media_duration", lambda *a: actual)

    if accepted:
        assert _validate_hls_duration(
            path, {"duration": expected}, "/usr/bin/ffmpeg"
        ) == actual
    else:
        with pytest.raises(RuntimeError, match="截断"):
            _validate_hls_duration(path, {"duration": expected}, "/usr/bin/ffmpeg")


def test_direct_video_does_not_require_hls_duration_probe(monkeypatch, tmp_path):
    """直链走**自己那套**内容终检, 不能套用 HLS 的时长校验。

    ⚠️ 两条路径的判据不同, 混用会让直链直接报错: HLS 的校验拿**播放列表给的**
    expected 时长当基准, 而直链根本没有播放列表。V33 给直链补了
    `_validate_direct_media`(容器 box 链 + ffprobe 时长), 这里钉住"补的是它,
    不是 HLS 那个"。
    """
    def boom(*a, **k):
        raise AssertionError("HLS-only guard")

    monkeypatch.setattr(video_mod, "_validate_hls_duration", boom)
    checked = []
    monkeypatch.setattr(
        video_mod, "_validate_direct_media", lambda *a, **k: checked.append(a) or None
    )
    monkeypatch.setattr(
        video_mod,
        "download_with_mirrors",
        lambda url, path, headers, **kw: ("a" * 64, path),
    )

    path, digest = VideoDownloader().download(
        "https://example.com/video.mp4", save_dir=tmp_path
    )
    assert path.suffix == ".mp4"
    assert digest == "a" * 64
    assert len(checked) == 1, "直链必须走自己的内容终检"


def test_direct_download_deletes_the_file_when_validation_fails(monkeypatch, tmp_path):
    """校验不过就必须**删掉那份字节**。

    ⚠️ 留在最终位置的后果是静默的: 后续任务用 sha256 查出"已有这张图"就直接复用,
    坏文件于是被传下去, 而每一环都报告成功。
    """
    def fake_download(url, path, headers, **kw):
        path.write_bytes(struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00" * 8 + b"isom"
                         + struct.pack(">I", 40000) + b"mdat" + b"\x00" * 32)
        return "a" * 64, path

    monkeypatch.setattr(video_mod, "download_with_mirrors", fake_download)
    # 只跑算术判据(不依赖本机 ffprobe), 才能确定是 box 链那次拦下的
    monkeypatch.setattr(video_mod, "_resolve_ffprobe", lambda ff: None)

    with pytest.raises(CorruptMediaError):
        VideoDownloader().download("https://example.com/broken.mp4", save_dir=tmp_path)
    assert not list(tmp_path.glob("*.mp4")), "坏文件必须被删掉"


def test_hls_duration_validation_accepts_small_container_variance(monkeypatch, tmp_path):
    path = tmp_path / "complete.mp4"
    path.write_bytes(b"not-used")
    monkeypatch.setattr(video_mod, "_probe_media_duration", lambda *a: 5400.0)

    assert _validate_hls_duration(
        path, {"duration": 5442.0}, "/usr/bin/ffmpeg"
    ) == 5400.0


# ---- URL 形态判定 ----

def test_is_hls():
    assert _is_hls("https://a.com/x.m3u8")
    assert _is_hls("https://a.com/x.m3u")
    assert _is_hls("https://a.com/X.M3U8?token=abc")
    assert not _is_hls("https://a.com/x.mp4")


def test_is_hls_ignores_query_string():
    """查询串里出现 .m3u8 不应把普通 mp4 误判成 HLS 流。"""
    assert not _is_hls("https://a.com/video.mp4?next=sample.m3u8")


def test_is_dash():
    assert _is_dash("https://a.com/x.mpd")
    assert not _is_dash("https://a.com/x.mp4")


def test_short_truncates_and_flattens():
    s = _short(ValueError("x" * 500))
    assert len(s) <= 180
    assert s.startswith("ValueError:")
    assert "\n" not in _short(ValueError("a\nb"))


# ---- 配置加载 ----

def test_config_resolves_relative_ffmpeg_path(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("ffmpeg_path: tools/ffmpeg.exe\n", encoding="utf-8")
    c = cfgmod.load(f)
    assert Path(c.ffmpeg_path).is_absolute()
    assert c.ffmpeg_path.endswith(str(Path("tools") / "ffmpeg.exe"))


def test_env_overrides_video_settings(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg"
    monkeypatch.setenv("UWC_FFMPEG", str(exe))
    monkeypatch.setenv("UWC_VIDEO_ENGINE", "builtin")
    c = cfgmod.load(tmp_path / "missing.yaml")
    assert c.ffmpeg_path == str(exe)
    assert c.video_engine == "builtin"


def test_default_video_settings():
    c = cfgmod.Config()
    assert c.video_engine == "auto"
    assert c.ffmpeg_path is None
    assert c.segment_retries == 3
