"""V33 下载层深度: 失败分类、直链内容终检、坏文件信号落库。

这几条都是**修缺陷**, 不是加功能 —— 它们改的是"跑通了但结果是错的"那种情况:

1. **4xx 被当成站点限流。** `raise_for_status()` 抛的 HTTPError 是
   RequestException 的子类, 于是"相册里有几张被删掉的图"会让整个域名的请求
   间隔翻倍, 而且每张还要白走 3 轮指数退避。
2. **直链媒体只校验字节数。** 长度对得上、内容被中间设备截断的 mp4/图片会被
   判成功, 然后一路冒充"已完成"。
3. **解码失败被静默丢弃。** ffmpeg 说解不开 = 最强的坏文件证据, 原来唯一后果
   是"这次没算指纹"。
4. **画像命中被静默丢弃。** `core/cdn_profile.py` 的读者不走锁, 写者的
   `replace` 又被占用挡下且被吞 —— 并发采集时命中记录丢一半。换了模块, 同一类
   静默失败; 顺带也是本仓库测试套件偶发变红的原因。

测试策略: 纯算术/纯分类的部分直接单测; 下载层用假 response 钉住**行为契约**
(重试次数、有没有记站点失败), 不发真实请求。
"""
import contextlib
import struct
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.cdn_profile as cdn_profile
import core.database as db
import core.mediacheck as mediacheck
import core.phash as phash
import core.task_manager as tm
import api.tasks as T
import downloaders.base as B
import downloaders.video as V
from core.errors import (
    KIND_CORRUPT,
    KIND_DISK,
    KIND_FORBIDDEN,
    KIND_GONE,
    KIND_NETWORK,
    CollectorError,
    CorruptMediaError,
    DiskFullError,
    GoneError,
    classify,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v33.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid: None)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), db


def _mk_task(database, url="https://example.com/album"):
    """建一个任务并返回 id。

    ⚠️ `database.create_task` 返回的是 lastrowid(int) 而不是 row —— 在这个项目里
    写成 `create_task(...)["id"]` 是很自然的手滑, 但会直接 TypeError。集中成一个
    helper 免得每处各写一遍。
    """
    return database.create_task(url, "generic")


# ============================ 1. 失败分类 ============================

def test_permanent_status_excludes_the_retryable_ones():
    """408/429/5xx 必须留在可重试那一侧。

    ⚠️ 这条是分流的**界限**: 408(请求超时)与 429(限速)恰恰是该重试的, 5xx 是
    站点自己出错(而且 5xx 走 note_failure 是对的 —— 那时站点确实吃不消)。
    手一抖把它们划进"永久失败", 症状是"偶发超时也永远不重试了"。
    """
    assert 404 in B.PERMANENT_STATUS
    assert 410 in B.PERMANENT_STATUS
    assert 403 in B.PERMANENT_STATUS
    assert 408 not in B.PERMANENT_STATUS
    assert 429 not in B.PERMANENT_STATUS
    for status in (500, 502, 503, 504):
        assert status not in B.PERMANENT_STATUS


def test_gone_kind_splits_missing_from_forbidden():
    """404 与 403 的**下一步动作**不同, 必须能分开: 前者没救, 后者可以试代理。"""
    assert classify(GoneError(404)) == KIND_GONE
    assert classify(GoneError(410)) == KIND_GONE
    assert classify(GoneError(403)) == KIND_FORBIDDEN
    assert classify(GoneError(401)) == KIND_FORBIDDEN
    assert classify(GoneError(451)) == KIND_FORBIDDEN
    assert "重试无用" in str(GoneError(404))
    assert "拒绝访问" in str(GoneError(403))


def test_classify_covers_the_other_failure_families():
    from core.errors import KIND_RATELIMIT, KIND_SERVER, KIND_UNKNOWN

    assert classify(DiskFullError("满了")) == KIND_DISK
    assert classify(CorruptMediaError("坏了")) == KIND_CORRUPT
    assert classify(OSError(28, "No space left on device")) == KIND_DISK
    assert classify(TimeoutError("slow")) == KIND_UNKNOWN   # 名字不在网络清单里

    class RateLimited(Exception):
        pass

    class ReadTimeout(Exception):
        pass

    class HTTPError(Exception):
        pass

    # 故意用同名类: classify 按**名字**识别(为了不 import downloaders 形成循环)
    assert classify(RateLimited("429")) == KIND_RATELIMIT
    assert classify(ReadTimeout("t")) == KIND_NETWORK
    assert classify(HTTPError("500")) == KIND_SERVER


def _fake_session(status, *, body=b"", headers=None):
    """造一个只会返回固定状态码的会话替身, 并记录被请求了几次。"""
    import requests

    class FakeResp:
        def __init__(self):
            self.status_code = status
            self.headers = {"Content-Type": "image/jpeg"} if headers is None else headers

        def iter_content(self, n):
            yield body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

        def close(self):
            pass

    class FakeSess:
        def __init__(self):
            self.calls = 0
            self.urls = []

        def get(self, url, *a, **k):
            self.calls += 1
            self.urls.append(url)
            return FakeResp()

    return FakeSess()


@contextlib.contextmanager
def _noop_slot(url, log=None, progress_cb=None):
    yield


def _patched_download_env(monkeypatch):
    """把限速/退避/站点记账换成可观测的替身, 返回记账列表。"""
    seen = {"fail": [], "wait": []}
    monkeypatch.setattr(B, "_timeout", lambda: 5)
    monkeypatch.setattr(B, "domain_slot", _noop_slot)
    monkeypatch.setattr(B, "note_failure", lambda url: seen["fail"].append(url))
    monkeypatch.setattr(B, "note_success", lambda url: None)
    monkeypatch.setattr(
        B, "_interruptible_wait", lambda s, cb=None: seen["wait"].append(s)
    )
    return seen


def test_404_is_permanent_no_retry_no_site_penalty(monkeypatch, tmp_path):
    """核心断言: 404 只请求一次, 不退避, **不记站点级失败**。

    ⚠️ 这三条缺一不可。尤其第三条: 记成站点失败会让该域名后续所有请求的间隔
    翻倍 —— 也就是"相册里有几张失效图"变成"整个站点都变慢", 而且没有任何报错。
    """
    sess = _fake_session(404)
    seen = _patched_download_env(monkeypatch)

    with pytest.raises(GoneError) as ei:
        B._stream_one("https://x.com/gone.jpg", tmp_path / "a.jpg", {}, 3, False,
                     sess, None)

    assert ei.value.status == 404
    assert sess.calls == 1        # 永久失败不重试
    assert seen["fail"] == []     # 不惩罚整个站点
    assert seen["wait"] == []     # 不退避等待


def test_5xx_still_retries_and_penalises_the_site(monkeypatch, tmp_path):
    """反向断言: 别把 5xx 一起划成永久失败, 那条 note_failure 是**对的**。"""
    sess = _fake_session(500)
    seen = _patched_download_env(monkeypatch)

    with pytest.raises(Exception):
        B._stream_one("https://x.com/broken.jpg", tmp_path / "b.jpg", {}, 3, False,
                     sess, None)

    assert sess.calls == 3
    # 最后一次是"重试耗尽直接上抛", 不记账 —— 只有真正退避过的两次算
    assert seen["fail"] == ["https://x.com/broken.jpg"] * 2
    assert len(seen["wait"]) == 2


def test_gone_on_primary_mirror_still_tries_the_next_one(monkeypatch, tmp_path):
    """4xx 是"这一份副本没有", 而 mirrors 的存在意义正是换一个 CDN 变体。

    ⚠️ 如果 GoneError 在 download_with_mirrors 里被直接上抛, 备用下载点就永远
    不会被尝试 —— 一个 CDN 上的 404 会让整张图判死, 哪怕主站上好好地存着。
    """
    tried = []

    def fake_stream_one(url, path, *a, **kw):
        tried.append(url)
        if url.endswith("primary.jpg"):
            raise GoneError(404, url)
        path.write_bytes(b"ok")
        return "sha", "image/jpeg"

    monkeypatch.setattr(B, "_stream_one", fake_stream_one)
    sha, real = B.download_with_mirrors(
        "https://a.com/primary.jpg", tmp_path / "m.jpg", {},
        mirrors=["https://b.com/backup.jpg"], session=object(),
    )
    assert tried == ["https://a.com/primary.jpg", "https://b.com/backup.jpg"]
    assert sha == "sha"


def test_gone_propagates_when_every_mirror_is_gone(monkeypatch, tmp_path):
    """所有候选都 4xx 才是"这个资源整体不可得", 交给上层判成 gone。"""
    def fake_stream_one(url, path, *a, **kw):
        raise GoneError(404, url)

    monkeypatch.setattr(B, "_stream_one", fake_stream_one)
    with pytest.raises(GoneError):
        B.download_with_mirrors(
            "https://a.com/x.jpg", tmp_path / "n.jpg", {},
            mirrors=["https://b.com/y.jpg"], session=object(),
        )


# ============================ 2. 容器完整性 ============================

def test_webp_declared_length_mismatch_is_detected(tmp_path):
    """RIFF 的长度字段是算术级证据: 对不上就一定被截断了。"""
    p = tmp_path / "a.webp"
    body = b"WEBP" + b"\x00" * 8
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 100) + body)
    assert "WebP 声明" in mediacheck.truncation_reason(p)


def test_webp_with_correct_length_passes(tmp_path):
    p = tmp_path / "b.webp"
    body = b"WEBP" + b"\x00" * 8
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    assert mediacheck.truncation_reason(p) is None


def test_bmp_declared_length_mismatch_is_detected(tmp_path):
    p = tmp_path / "a.bmp"
    p.write_bytes(b"BM" + struct.pack("<I", 9999) + b"\x00" * 50)
    assert "BMP 声明" in mediacheck.truncation_reason(p)


def test_bmp_with_zero_length_field_is_not_judged(tmp_path):
    """有写出器把长度字段留 0(未填写) —— 那是"不知道", 不是"坏了"。

    报错的代价是删掉一个好文件, 所以只有**能被证明**的情况才下结论。
    """
    p = tmp_path / "b.bmp"
    p.write_bytes(b"BM" + struct.pack("<I", 0) + b"\x00" * 50)
    assert mediacheck.truncation_reason(p) is None


def test_jpeg_missing_eoi_is_detected(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300)   # 没有 FF D9
    assert "EOI" in mediacheck.truncation_reason(p)


def test_jpeg_with_eoi_passes(tmp_path):
    p = tmp_path / "b.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300 + b"\xff\xd9")
    assert mediacheck.truncation_reason(p) is None


def test_png_missing_iend_is_detected(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    assert "IEND" in mediacheck.truncation_reason(p)


def test_png_with_iend_passes(tmp_path):
    p = tmp_path / "b.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200 + b"IEND\xaeB`\x82")
    assert mediacheck.truncation_reason(p) is None


def test_gif_missing_trailer_is_detected(tmp_path):
    p = tmp_path / "a.gif"
    p.write_bytes(b"GIF89a" + b"\x00" * 200)
    assert "0x3B" in mediacheck.truncation_reason(p)


def test_gif_with_trailer_passes(tmp_path):
    p = tmp_path / "b.gif"
    p.write_bytes(b"GIF89a" + b"\x00" * 199 + b"\x3b")
    assert mediacheck.truncation_reason(p) is None


def _ftyp_box():
    """一个长度字段与内容**自洽**的 ftyp box, 正好 24 字节。"""
    box = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00" * 8 + b"isom"
    assert len(box) == 24
    return box


def test_mp4_box_overrunning_eof_is_detected(tmp_path):
    """截断的 mp4 几乎必然在**最后一个 box** 上露馅: 它声明的长度超出文件末尾。

    这条不依赖 ffprobe —— 机器上没装 ffmpeg 时, 它是唯一的保护。
    """
    p = tmp_path / "a.mp4"
    p.write_bytes(_ftyp_box() + struct.pack(">I", 40000) + b"mdat" + b"\x00" * 32)
    reason = mediacheck.truncation_reason(p)
    assert reason and "被截断" in reason


def test_mp4_whose_boxes_fill_the_file_passes(tmp_path):
    p = tmp_path / "b.mp4"
    mdat = struct.pack(">I", 40) + b"mdat" + b"\x00" * 32
    p.write_bytes(_ftyp_box() + mdat)
    assert len(_ftyp_box() + mdat) == 64
    assert mediacheck.truncation_reason(p) is None


def test_mp4_box_size_zero_means_until_eof(tmp_path):
    """size==0 是"这个 box 一直到文件结尾", 是合法写法而不是损坏。"""
    p = tmp_path / "c.mp4"
    p.write_bytes(_ftyp_box() + struct.pack(">I", 0) + b"mdat" + b"\x00" * 32)
    assert mediacheck.truncation_reason(p) is None


def test_unknown_container_is_not_judged(tmp_path):
    """认不出格式就闭嘴 —— 误判会删掉用户真正想要的文件。"""
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00" * 200)
    assert mediacheck.truncation_reason(p) is None


def test_tiny_file_is_not_judged(tmp_path):
    p = tmp_path / "tiny.jpg"
    p.write_bytes(b"ab")
    assert mediacheck.truncation_reason(p) is None


def test_empty_file_is_reported(tmp_path):
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"")
    assert "长度为 0" in mediacheck.truncation_reason(p)


def test_missing_file_is_not_judged(tmp_path):
    assert mediacheck.truncation_reason(tmp_path / "nope.jpg") is None


def test_looks_truncated_is_the_boolean_form(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300)
    assert mediacheck.looks_truncated(p) is True
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300 + b"\xff\xd9")
    assert mediacheck.looks_truncated(p) is False


# ============================ 3. 直链媒体终检 ============================

def test_direct_media_uses_arithmetic_check_even_without_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_resolve_ffprobe", lambda ff: None)
    bad = tmp_path / "trunc.mp4"
    bad.write_bytes(_ftyp_box() + struct.pack(">I", 40000) + b"mdat" + b"\x00" * 32)
    with pytest.raises(CorruptMediaError):
        V._validate_direct_media(bad, {}, None)


def test_direct_media_passes_without_probe_on_unknown_container(monkeypatch, tmp_path):
    """缺 ffprobe 时**放行**: 辅助能力缺失不该让下载失败(与 phash 同一条约束)。"""
    monkeypatch.setattr(V, "_resolve_ffprobe", lambda ff: None)
    ok = tmp_path / "ok.mp4"
    ok.write_bytes(b"\x00" * 128)
    assert V._validate_direct_media(ok, {}, None) is None


def test_direct_media_fails_closed_when_container_unreadable(monkeypatch, tmp_path):
    """有 ffprobe 却读不出时长 = 结论, 不是"无法判断"。

    这正是修掉的那个静默缺陷: 长度校验能证明字节数, 证明不了容器可用。
    """
    monkeypatch.setattr(V, "_resolve_ffprobe", lambda ff: "/usr/bin/ffprobe")
    monkeypatch.setattr(V, "_probe_media_duration", lambda *a, **k: None)
    p = tmp_path / "x.mp4"
    p.write_bytes(b"\x00" * 128)
    with pytest.raises(CorruptMediaError, match="无法解析"):
        V._validate_direct_media(p, {}, "/usr/bin/ffmpeg")


def test_direct_media_compares_declared_duration_when_available(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_resolve_ffprobe", lambda ff: "/usr/bin/ffprobe")
    monkeypatch.setattr(V, "_probe_media_duration", lambda *a, **k: 270.0)
    p = tmp_path / "y.mp4"
    p.write_bytes(b"\x00" * 128)
    with pytest.raises(CorruptMediaError, match="截断"):
        V._validate_direct_media(p, {"duration": 5442.0}, "/usr/bin/ffmpeg")


def test_direct_media_accepts_short_clips_within_tolerance(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_resolve_ffprobe", lambda ff: "/usr/bin/ffprobe")
    monkeypatch.setattr(V, "_probe_media_duration", lambda *a, **k: 9.7)
    p = tmp_path / "z.mp4"
    p.write_bytes(b"\x00" * 128)
    assert V._validate_direct_media(p, {"duration": 10.0}, "/usr/bin/ffmpeg") == 9.7


def test_direct_media_returns_none_when_no_probe_supplied(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_resolve_ffprobe", lambda ff: None)
    p = tmp_path / "w.mp4"
    p.write_bytes(b"\x00" * 128)
    assert V._validate_direct_media(p, {}, None) is None


def test_resolve_ffprobe_finds_sibling_binary(tmp_path):
    ff = tmp_path / "ffmpeg"
    ff.write_bytes(b"x")
    probe = tmp_path / "ffprobe"
    probe.write_bytes(b"x")
    assert V._resolve_ffprobe(str(ff)) == str(probe)


def test_resolve_ffprobe_returns_none_without_ffmpeg():
    assert V._resolve_ffprobe(None) is None
    assert V._resolve_ffprobe("") is None


# ============================ 4. 解码失败信号 ============================

class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_decode_reports_missing_decoder_separately(monkeypatch, tmp_path):
    """本机没 ffmpeg ≠ 文件坏了。

    ⚠️ 这两种情况原来都返回 None, 调用方无法分辨 —— 于是要么在缺 ffmpeg 的机器上
    把所有文件都判成坏的, 要么把真损坏的文件当成"环境问题"放过去。
    """
    monkeypatch.setattr("core.ffmpeg.find_ffmpeg", lambda: None)
    raw, why = phash.decode_gray_ex(tmp_path / "x.jpg")
    assert raw is None
    assert why == phash.NO_DECODER


def test_decode_reports_decoder_failure_as_corrupt(monkeypatch, tmp_path):
    monkeypatch.setattr("core.ffmpeg.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(phash.subprocess, "run", lambda *a, **k: _Proc(returncode=1))
    raw, why = phash.decode_gray_ex(tmp_path / "x.jpg")
    assert raw is None
    assert why == phash.DECODE_FAILED


def test_decode_flags_short_output_as_corrupt(monkeypatch, tmp_path):
    """退出码 0 但吐不出一整帧 = 文件头合法、后半截没了。

    这正是 Content-Length 校验抓不到的那一类: 字节数对得上。
    """
    monkeypatch.setattr("core.ffmpeg.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        phash.subprocess, "run", lambda *a, **k: _Proc(returncode=0, stdout=b"\x00" * 10)
    )
    raw, why = phash.decode_gray_ex(tmp_path / "x.jpg")
    assert raw is None
    assert why == phash.DECODE_FAILED


def test_decode_returns_bytes_and_no_reason_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr("core.ffmpeg.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        phash.subprocess, "run",
        lambda *a, **k: _Proc(returncode=0, stdout=b"\x10" * (9 * 8)),
    )
    raw, why = phash.decode_gray_ex(tmp_path / "x.jpg")
    assert why is None
    assert len(raw) == 72


def test_dhash_still_returns_none_but_reason_survives(monkeypatch, tmp_path):
    """旧接口(dhash -> None)保持不变, 新接口把原因带出来。"""
    monkeypatch.setattr("core.ffmpeg.find_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(phash.subprocess, "run", lambda *a, **k: _Proc(returncode=1))
    assert phash.dhash(tmp_path / "x.jpg") is None
    got, why = phash.dhash_ex(tmp_path / "x.jpg")
    assert got is None and why == phash.DECODE_FAILED


# ============================ 5. 落库与接口 ============================

def test_error_kind_column_exists(client):
    _, database = client
    cols = {r["name"] for r in database.query("PRAGMA table_info(resources)")}
    assert "error_kind" in cols


def test_error_kinds_aggregates_across_statuses(client):
    """`corrupt` 会落在 done 上(文件保留只标记), 所以**不能**按 status 过滤。

    按 status 过滤恰好会漏掉最该被看见的那一类。
    """
    app, database = client
    tid = _mk_task(database)
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="failed")
    database.add_resource(tid, "image", "https://x.com/b.jpg", status="gone")
    database.add_resource(tid, "image", "https://x.com/c.jpg", status="done")
    rows = database.get_resources(tid)
    database.update_resource(rows[0]["id"], error_kind=KIND_NETWORK)
    database.update_resource(rows[1]["id"], error_kind=KIND_GONE)
    database.update_resource(rows[2]["id"], error_kind=KIND_CORRUPT)

    got = {r["kind"]: r["count"] for r in database.error_kinds()}
    assert got[KIND_NETWORK] == 1
    assert got[KIND_GONE] == 1
    assert got[KIND_CORRUPT] == 1     # 尽管它的 status 是 done


def test_summarize_resources_counts_gone_as_failure(client):
    """口径必须与 _final_status 一致, 否则"摘要说 0 失败、状态却是 failed"。"""
    _, database = client
    tid = _mk_task(database)
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="gone")
    database.add_resource(tid, "image", "https://x.com/b.jpg", status="gone")
    counts = database.summarize_resources(tid)
    assert counts["gone"] == 2
    assert counts["failed"] == 2      # gone 已并入 failed
    assert counts["done"] == 0


def test_final_status_treats_gone_as_failure(client):
    """全任务都是 gone 时不能显示成绿色 —— "要 56 张拿到 0 张"就是失败。"""
    _, database = client
    tid = _mk_task(database)
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="gone")
    assert tm.task_manager._final_status(tid) == tm.TaskStatus.FAILED

    database.add_resource(tid, "image", "https://x.com/b.jpg", status="done")
    assert tm.task_manager._final_status(tid) == tm.TaskStatus.PARTIAL


def test_stats_endpoint_exposes_kinds_and_labels(client):
    app, database = client
    tid = _mk_task(database)
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="failed")
    database.update_resource(
        database.get_resources(tid)[0]["id"], error_kind=KIND_NETWORK
    )

    body = app.get("/tasks/stats").json()
    kinds = {k["kind"]: k["count"] for k in body["error_kinds"]}
    assert kinds[KIND_NETWORK] == 1
    # 文案由后端给, 前端不再自己写一套映射
    assert body["error_kind_labels"][KIND_GONE] == "源站已无此资源"


def test_task_detail_exposes_kind_labels_and_resource_kind(client):
    app, database = client
    tid = _mk_task(database)
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="gone")
    database.update_resource(
        database.get_resources(tid)[0]["id"], error_kind=KIND_GONE
    )

    body = app.get(f"/tasks/{tid}").json()
    assert body["error_kind_labels"][KIND_GONE]
    assert body["resources"][0]["error_kind"] == KIND_GONE
    assert body["resource_counts"]["gone"] == 1


def test_submit_resource_allows_manual_retry_of_gone(client):
    """自动流程不重试 gone, 但**用户手动点**时应该放行(403 可能换代理就好了)。"""
    _, database = client
    _mk_task(database)
    tid = database.query_one("SELECT id FROM tasks ORDER BY id DESC")["id"]
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="gone")
    rid = database.get_resources(tid)[0]["id"]
    database.update_task(tid, status=tm.TaskStatus.SUCCESS)

    seen = {}

    class FakePool:
        pass

    # 替换下载执行器, 只要确认请求被接受(不真的下载)
    class FakeExec:
        def submit(self, *a, **kw):
            seen["submitted"] = True

    original = tm.task_manager._download_executor
    tm.task_manager._download_executor = FakeExec()
    try:
        ok, err = tm.task_manager.submit_resource(tid, rid)
    finally:
        tm.task_manager._download_executor = original

    assert ok is True, err
    assert seen.get("submitted") is True
    row = database.get_resource(rid)
    assert row["status"] == "pending"
    assert row["error_kind"] is None      # 旧结论必须清掉
    assert row["note"] is None


def test_submit_resource_still_rejects_done_resources(client):
    _, database = client
    tid = _mk_task(database)
    database.add_resource(tid, "image", "https://x.com/a.jpg", status="done")
    rid = database.get_resources(tid)[0]["id"]
    database.update_task(tid, status=tm.TaskStatus.SUCCESS)
    ok, err = tm.task_manager.submit_resource(tid, rid)
    assert ok is False
    assert "done" in err


def test_corrupt_media_error_is_a_collector_error():
    """它要被 CollectorError 分支接住(不打堆栈、消息直接给用户看)。"""
    assert isinstance(CorruptMediaError("x"), CollectorError)
    assert isinstance(GoneError(404), CollectorError)


# ======================================================================
# 画像写者/读者的并发契约
#
# 这一组守的是一个**换了个模块但同源**的缺陷: `cdn_profile` 的读者此前不走锁,
# 而写者用 `tmp.replace(p)` 换文件 —— 在 Windows 上只要目标还有别的句柄开着(哪怕
# 只是只读), 替换就会以 PermissionError 失败, 而它被 `except OSError: pass` 静默
# 吞掉, 于是并发采集时命中记录**丢一半**(实测 300 次写只记下 150 次)。
#
# 它与前面三条同属"静默失败"一类(只是这次丢的是画像, 不是资源), 顺手一并修掉 ——
# 而且它正是本仓库测试套件偶发变红的原因, 留着会让每次验证都不可信。
# ======================================================================


def test_profile_records_every_hit_while_readers_run():
    """读者与写者并发时, **每一次 record_hit 都必须落盘**。

    这是回归测试: 修复前在 Windows 上稳定失败(丢约一半), 症状是 total_hits 偏小。
    """
    cdn_profile.reset()
    n_write = 200
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            cdn_profile.preferred_bases("s")
            cdn_profile.summarize("s")

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    try:
        for _ in range(n_write):
            cdn_profile.record_hit("s", "https://c/p")
    finally:
        stop.set()
        worker.join(timeout=10)

    assert not worker.is_alive(), "读者线程没能退出"
    assert cdn_profile.summarize("s")["s"]["total_hits"] == n_write


def test_profile_write_retries_when_replace_is_transiently_blocked(monkeypatch):
    """外部句柄瞬时占着文件时, 写者要重试, 而不是把这次命中丢掉。"""
    cdn_profile.reset()
    calls = {"n": 0}
    real_replace = Path.replace

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:
            # 模拟 Windows 上"目标被占用" —— 这是 PermissionError, 属于 OSError
            raise PermissionError(13, "文件被另一个进程占用")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    cdn_profile.record_hit("s", "https://c/p")

    assert calls["n"] == 3, "前两次失败后应当继续重试"
    assert cdn_profile.preferred_bases("s") == ["https://c/p"]


def test_profile_gives_up_quietly_when_permanently_unwritable(monkeypatch):
    """真的写不进去(只读盘/磁盘满): 不抛异常, 也不无限重试。

    契约见模块 docstring —— 画像只是线索, 它不该有能力让采集失败。
    """
    cdn_profile.reset()
    attempts = {"n": 0}

    def always_fail(self, target):
        attempts["n"] += 1
        raise PermissionError(13, "只读文件系统")

    monkeypatch.setattr(Path, "replace", always_fail)
    cdn_profile.record_hit("s", "https://c/p")      # 不该抛

    assert attempts["n"] == cdn_profile._WRITE_RETRIES
    assert cdn_profile.preferred_bases("s") == []
