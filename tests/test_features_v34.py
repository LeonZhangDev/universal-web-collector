"""V34 纵深: 可观测性、条件请求、带宽上限、资源库操作、熔断持久化、完整性巡检。

与 V33 的区别: V33 是**修缺陷**(跑通了但结果是错的), V34 大多是**加能力**。
加能力也会引入同一类静默失败, 所以每一组都钉住"能力没生效时会发生什么":

1. **资源级遥测**(`started_at`/`finished_at`/`attempts`)。没有它, "这个任务为什么
   慢"只能看到一个聚合总时长, 而「120 张各 1 秒」与「119 张各 0.2 秒 + 1 张卡 95
   秒」的处置完全不同。坑在口径: 早退路径(取消/磁盘满/被过滤)根本没发起下载,
   给它们记 "0 ms" 会把平均值拉成一个假象。
2. **缩略图**。网格铺原图会让一个 40 项的页面下载几百 MB —— 表现是"界面卡",
   很难归因到"用了原图"。降级必须彻底: 没有 ffmpeg / 解不开都要回退原图,
   而不是给一张破图。
3. **条件请求**(ETag/Last-Modified)。304 的前提是**本地那份真的还在** ——
   用户清过下载目录时, 304 会让我们"成功地什么都没得到", 比下载失败还难查。
4. **全局字节上限**。桶在进程启动时就建好了, 只改配置会表现为"接口成功但速度没变"。
5. **资源库批量删除**。文件是最后一步、且带三道闸: with_files 显式打开 / 引用数
   复查 / 路径必须在下载根内。删文件不可逆, 判据错一次就是用户数据没了。
6. **代理熔断持久化**。失败计数原来只活在内存, 服务重启就清零 —— 于是"上一轮
   已经发现第 2 条线是死的"下次还要重踩一遍, 代价落在真实资源上。
7. **落盘后巡检**。库里的 local_path 可能已被外部删除或截断。只标记不删除。
"""
import io
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db
import core.proxy_health as proxy_health
import core.thumbs as thumbs
import downloaders.base as B
import downloaders.ratelimit as RL
from core.config import parse_bytes_per_sec
from core.errors import KIND_CORRUPT, KIND_MISSING


# ============================ 公共夹具 ============================

@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """独立库 + 独立下载根 + 有任务/资源工厂的小上下文。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v34.db")
    monkeypatch.setattr(db, "_conn", None)
    # ⚠️ 收 **kw: resume 分支调的是 submit(task_id, resume=True),
    # 只收一个位置参数的替身会 TypeError —— 而那个错会以"resume 崩了"的形式出现。
    monkeypatch.setattr(T.task_manager, "submit", lambda tid, **kw: None)
    dl = tmp_path / "dl"
    dl.mkdir()

    class Ctx:
        root = dl

        @staticmethod
        def task(name="相册A", download_dir=None, url="https://example.com/a"):
            return db.create_task(url, "generic", download_dir=str(download_dir or dl),
                                  name=name)

        @staticmethod
        def res(task_id, rtype="image", url="https://example.com/1.jpg", **kw):
            return db.add_resource(task_id, rtype, url, **kw)

    return Ctx()


def _client():
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app)


def _done_file(ctx, task_id, name, body=b"hello world", sub="", rtype="image"):
    """在下载根下造一个真实文件并登记成 done 资源, 返回 (resource_id, path)。"""
    p = ctx.root / sub / name if sub else ctx.root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    rid = ctx.res(task_id, rtype=rtype, url=f"https://example.com/{name}",
                  status="done", local_path=str(p))
    db.update_resource(rid, size=len(body))
    return rid, p


# ============================ 1. 资源级遥测 ============================

def test_begin_attempt_stamps_start_and_clears_finish(ctx):
    """一次尝试 = started_at 写入 + finished_at 清空 + attempts 自增。"""
    t = ctx.task()
    rid = ctx.res(t)
    before = db.get_resource(rid)
    assert int(before["attempts"] or 0) == 0
    assert before["started_at"] is None

    ts = db.begin_resource_attempt(rid)
    row = db.get_resource(rid)
    assert row["started_at"] == pytest.approx(ts, abs=1e-6)
    # ⚠️ 必须清空: 它此刻代表**上一次**的结束, 留着会让界面显示一个已经"完成"的
    # 资源又回到下载中, 遥测也会把重新开始的那次算成 0 耗时。
    assert row["finished_at"] is None
    assert row["attempts"] == 1

    db.begin_resource_attempt(rid)
    assert db.get_resource(rid)["attempts"] == 2


def test_begin_attempt_is_atomic_under_concurrency(ctx):
    """并发下 attempts 不能丢计数。

    ⚠️ 用一条 `attempts=attempts+1` 的自增 SQL, 不要"读出来 +1 再写回" ——
    手动重试与自动流程同时碰到同一个资源时, 读-改-写会丢掉一次, 而次数正是
    判断"是不是一直在重试链里空转"的唯一依据, 丢一次结论就反了。
    """
    t = ctx.task()
    rid = ctx.res(t)
    n = 40
    barrier = threading.Barrier(n)

    def bump():
        barrier.wait()
        db.begin_resource_attempt(rid)

    threads = [threading.Thread(target=bump) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert db.get_resource(rid)["attempts"] == n


def test_resource_timing_excludes_incomplete(ctx):
    """没有 finished_at 的资源不算进平均耗时。

    拿 0 充数会把平均值拉低: "平均 200ms" 与 "平均 200ms 但其实一半没下完"
    是两种完全不同的结论。
    """
    t = ctx.task()
    a = ctx.res(t, url="https://e.com/a.jpg")
    b = ctx.res(t, url="https://e.com/b.jpg")
    db.begin_resource_attempt(a)
    db.update_resource(a, status="done", finished_at=db.get_resource(a)["started_at"] + 2.0)
    db.begin_resource_attempt(b)     # 只开始了, 没结束

    timing = db.resource_timing(t)
    assert timing["measured"] == 1
    assert timing["avg_ms"] == pytest.approx(2000.0, abs=1.0)
    assert [i["id"] for i in timing["slowest"]] == [a]


def test_resource_timing_reports_slowest_first_and_retried(ctx):
    t = ctx.task()
    ids = []
    for i, dur in enumerate([0.1, 3.0, 0.5]):
        rid = ctx.res(t, url=f"https://e.com/{i}.jpg")
        start = db.begin_resource_attempt(rid)
        db.update_resource(rid, status="done", finished_at=start + dur)
        ids.append(rid)
    # 再来一个重试过的: 它耗时中等, 但 attempts 高 —— 这是**另一种病**,
    # "平均很快但一堆资源重试过" 会被平均耗时的数字掩盖掉。
    slow = ctx.res(t, url="https://e.com/retry.jpg")
    st = db.begin_resource_attempt(slow)
    db.begin_resource_attempt(slow)
    db.update_resource(slow, status="done", finished_at=st + 1.0)

    timing = db.resource_timing(t)
    assert timing["measured"] == 4
    assert timing["slowest"][0]["ms"] == pytest.approx(3000.0, abs=1.0)
    assert timing["max_ms"] == pytest.approx(3000.0, abs=1.0)
    assert timing["retried"] == 1
    assert timing["slowest"][0]["attempts"] == 2 or timing["retried"] == 1


def test_task_detail_exposes_timing_and_attempts(ctx):
    t = ctx.task()
    rid = ctx.res(t)
    start = db.begin_resource_attempt(rid)
    db.update_resource(rid, status="done", finished_at=start + 1.25)

    r = _client().get(f"/tasks/{t}")
    assert r.status_code == 200
    body = r.json()
    assert body["resource_timing"]["measured"] == 1
    got = body["resources"][0]
    assert got["attempts"] == 1
    assert got["started_at"] is not None and got["finished_at"] is not None


def test_resume_clears_finished_at_so_timing_is_not_stale(ctx, monkeypatch):
    """暂停后继续: finished_at 必须清掉, 否则遥测把上一次的结束当成这一次的耗时。"""
    t = ctx.task()
    rid = ctx.res(t)
    db.begin_resource_attempt(rid)
    db.update_resource(rid, started_at=0.0, finished_at=99.0, status="failed")
    db.update_task(t, status="paused")

    ok, msg = T.task_manager.resume(t)
    assert ok, msg
    row = db.get_resource(rid)
    assert row["finished_at"] is None
    assert row["status"] == "pending"


# ============================ 2. 缩略图与文件服务 ============================

def test_thumb_key_uses_absolute_path(ctx, tmp_path):
    """缓存键必须是**绝对路径**: 同名文件落在两个下载根里不能撞成同一个 key。

    撞了的后果是"资源库里的缩略图和点开的大图不是同一张", 极难怀疑到缓存键。
    """
    a = ctx.root / "x" / "same.jpg"
    b = tmp_path / "other" / "x" / "same.jpg"
    a.parent.mkdir(parents=True, exist_ok=True)
    b.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assert thumbs.thumb_key(a) != thumbs.thumb_key(b)


def test_ensure_thumb_returns_none_without_ffmpeg(ctx, monkeypatch):
    """没有 ffmpeg 时返回 None, 让调用方回退原图 —— 不是抛异常。"""
    p = ctx.root / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(thumbs, "find_ffmpeg", lambda: None)
    assert thumbs.ensure_thumb(ctx.root, p) is None


def test_ensure_thumb_swallows_errors(ctx, monkeypatch):
    """缩略图是渲染附件: 它失败只该"慢", 不该把接口打成 500。"""
    monkeypatch.setattr(thumbs, "find_ffmpeg", lambda: "/nonexistent/ffmpeg")
    assert thumbs.ensure_thumb(ctx.root, ctx.root / "不存在.jpg") is None


def test_is_fresh_requires_thumb_newer_than_source(ctx, monkeypatch):
    src = ctx.root / "a.jpg"
    src.write_bytes(b"x" * 10)
    dest = thumbs.thumb_path(ctx.root, src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"thumb")
    # 源文件比缩略图新 -> 视为过期(内容换过)
    old = time.time() - 100
    import os

    os.utime(dest, (old, old))
    assert thumbs.is_fresh(src, dest) is False
    os.utime(dest, None)
    assert thumbs.is_fresh(src, dest) is True


def test_raw_endpoint_serves_a_registered_file(ctx):
    t = ctx.task()
    _rid, p = _done_file(ctx, t, "a.jpg", b"\xff\xd8\xff\xd9")
    r = _client().get("/files/raw", params={"path": str(p)})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff\xd9"


def test_raw_endpoint_rejects_unknown_path(ctx, tmp_path):
    """⚠️ 只判"在下载根内"等于给了任意文件读取权 —— 库里必须有这条记录。"""
    stray = ctx.root / "not-registered.jpg"
    stray.write_bytes(b"secret")
    r = _client().get("/files/raw", params={"path": str(stray)})
    assert r.status_code == 404


def test_raw_endpoint_rejects_path_outside_task_base(ctx, tmp_path):
    """记录本身也可能被手改成越界路径 —— 那时按任务的根判必须拒掉。"""
    t = ctx.task()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    rid = ctx.res(t, status="done", local_path=str(outside))
    db.update_resource(rid, size=len(b"secret"))
    r = _client().get("/files/raw", params={"path": str(outside)})
    assert r.status_code == 403


def test_thumb_endpoint_falls_back_to_original(ctx, monkeypatch):
    """生成不了缩略图时返回**原图**而不是错误 —— 用户仍然要看得到内容。"""
    t = ctx.task()
    rid, p = _done_file(ctx, t, "a.jpg", b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(T.thumbs, "ensure_thumb", lambda *a, **k: None)
    r = _client().get("/files/thumb", params={"path": str(p)})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff\xd9"


def test_thumb_endpoint_serves_generated_thumb_with_cache_header(ctx, monkeypatch):
    t = ctx.task()
    rid, p = _done_file(ctx, t, "a.jpg", b"\xff\xd8\xff\xd9")
    fake = ctx.root / "thumb.jpg"
    fake.write_bytes(b"THUMB")

    monkeypatch.setattr(T.thumbs, "ensure_thumb", lambda *a, **k: fake)
    r = _client().get("/files/thumb", params={"path": str(p)})
    assert r.status_code == 200
    assert r.content == b"THUMB"
    assert "max-age" in r.headers.get("Cache-Control", "")


# ============================ 3. 条件请求 ============================

def _fake_session(status, *, body=b"", headers=None, record=None):
    """固定状态码的会话替身; record 传 list 时记录每次请求的 headers。"""
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

        def get(self, url, *a, **k):
            self.calls += 1
            if record is not None:
                record.append(dict(k.get("headers") or {}))
            return FakeResp()

    return FakeSess()


@pytest.fixture()
def dl_env(monkeypatch):
    """把限速/退避/站点记账换成替身, 让单次 _stream_one 是纯函数的。"""
    import contextlib

    @contextlib.contextmanager
    def noop_slot(url, log=None, progress_cb=None):
        yield

    monkeypatch.setattr(B, "_timeout", lambda: 5)
    monkeypatch.setattr(B, "domain_slot", noop_slot)
    monkeypatch.setattr(B, "note_failure", lambda url: None)
    monkeypatch.setattr(B, "note_success", lambda url: None)
    monkeypatch.setattr(B, "_interruptible_wait", lambda s, cb=None: None)


def test_validators_for_shape():
    """形状只有一种, 四个下载器共用 —— 各写一遍迟早有一处键名打错。

    打错的表现是"条件请求静默没生效"(语法完全合法, 只是永远不命中), 极难发现。
    """
    assert B.validators_for(None, None) is None
    assert B.validators_for("", "") is None
    assert B.validators_for('W/"abc"', None) == {"etag": 'W/"abc"'}
    assert B.validators_for(None, "Tue, 01 Jan 2030 00:00:00 GMT") == {
        "last_modified": "Tue, 01 Jan 2030 00:00:00 GMT"
    }


def test_stream_one_sends_conditional_headers(tmp_path, dl_env):
    seen = []
    sess = _fake_session(200, body=b"\xff\xd8\xff\xd9", record=seen)
    p = tmp_path / "a.jpg"
    B._stream_one("https://e.com/a.jpg", p, {}, 1, False, sess, None,
                  require_image=True,
                  validators=B.validators_for('"v1"', "Tue, 01 Jan 2030 00:00:00 GMT"))
    assert p.read_bytes() == b"\xff\xd8\xff\xd9"
    assert seen[0].get("If-None-Match") == '"v1"'
    assert seen[0].get("If-Modified-Since") == "Tue, 01 Jan 2030 00:00:00 GMT"


def test_range_takes_precedence_over_conditional_headers(tmp_path, dl_env):
    """⚠️ 有 Range 时**不能**再带条件头。

    带 Range 时若本地那份恰是最新的, 服务器会以 304 而不是 206 回答, 于是
    "416 = 本地已完整"那条断点续传的收尾路径永远走不到, 续传再也没机会收尾。
    """
    seen = []
    sess = _fake_session(206, body=b"tail", record=seen)
    url = "https://e.com/a.jpg"
    p = tmp_path / "a.jpg"
    p.write_bytes(b"head" * 4)
    # ⚠️ 半成品必须带 `.partsrc`: 光有 .part 是"来源不明", 会被丢弃重下
    # (那正是防止"两个不同文件被拼成一份"的那道闸)。
    B._write_src(B._part_src(p), url)
    p.rename(B._part_path(p))

    B._stream_one(url, p, {}, 1, True, sess, None,
                  validators=B.validators_for('"v1"', None))
    assert "Range" in seen[0], f"续传必须带 Range, 实际请求头 {seen[0]}"
    assert "If-None-Match" not in seen[0]


def test_304_with_local_copy_reuses_without_transfer(tmp_path, dl_env):
    """源站说"没变"且本地那份还在 -> 一个字节都不传。"""
    sess = _fake_session(304)
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xd9")
    info = {}
    sha, _ctype = B._stream_one("https://e.com/a.jpg", p, {}, 1, False, sess, None,
                                info=info, validators=B.validators_for('"v1"', None))
    assert info.get("not_modified") is True
    assert sess.calls == 1
    assert p.read_bytes() == b"\xff\xd8\xff\xd9"


def test_304_without_local_copy_still_downloads(tmp_path, dl_env, monkeypatch):
    """⚠️ 本地副本不在了 -> 必须丢掉校验器重新要一份完整内容。

    否则就是"成功地什么都没得到": 任务报成功、目录里没有文件, 比下载失败还难查。
    """
    monkeypatch.setattr(B.settings, "image_retries", 2)
    # 第一次 304(本地没有), 之后 200 给全量
    import requests

    class Resp:
        def __init__(self, code):
            self.status_code = code
            self.headers = {"Content-Type": "image/jpeg"}

        def iter_content(self, n):
            yield b"\xff\xd8\xff\xd9"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError("x")

        def close(self):
            pass

    class Sess:
        def __init__(self):
            self.n = 0

        def get(self, url, *a, **k):
            self.n += 1
            return Resp(304 if self.n == 1 else 200)

    sess = Sess()
    p = tmp_path / "a.jpg"
    info = {}
    B._stream_one("https://e.com/a.jpg", p, {}, 2, False, sess, None,
                  info=info, validators=B.validators_for('"v1"', None))
    assert sess.n >= 2
    assert p.read_bytes() == b"\xff\xd8\xff\xd9"
    assert not info.get("not_modified")


def test_fill_info_records_validators_for_next_round():
    """响应给的 ETag/Last-Modified 要存下来, 否则下一轮无从发起条件请求。"""
    class R:
        headers = {"ETag": '"abc"', "Last-Modified": "Tue, 01 Jan 2030 00:00:00 GMT"}

    info = {}
    B.fill_info(info, "https://e.com/a.jpg", "image/jpeg", R())
    assert info["etag"] == '"abc"'
    assert info["last_modified"] == "Tue, 01 Jan 2030 00:00:00 GMT"


def test_image_downloader_passes_validators_through(tmp_path, monkeypatch):
    """DB 里的凭据必须真的走到下载层 —— 中间断一环就是"条件请求静默没生效"。"""
    from downloaders.image import ImageDownloader

    captured = {}

    def fake_dwm(url, path, headers, **kw):
        captured.update(kw)
        Path(path).write_bytes(b"\xff\xd8\xff\xd9")
        return "sha", Path(path)

    monkeypatch.setattr("downloaders.image.download_with_mirrors", fake_dwm)
    dl = ImageDownloader()
    dl.download("https://e.com/a.jpg", save_dir=str(tmp_path),
                etag='"v9"', last_modified="Tue, 01 Jan 2030 00:00:00 GMT")
    assert captured.get("validators") == {
        "etag": '"v9"',
        "last_modified": "Tue, 01 Jan 2030 00:00:00 GMT",
    }


# ============================ 4. 全局字节上限 ============================

def test_parse_bytes_per_sec_accepts_human_forms():
    assert parse_bytes_per_sec("5MB") == 5 * 1024 * 1024
    assert parse_bytes_per_sec("5m") == 5 * 1024 * 1024
    assert parse_bytes_per_sec("512k") == 512 * 1024
    assert parse_bytes_per_sec("1048576") == 1048576
    assert parse_bytes_per_sec(0) == 0
    assert parse_bytes_per_sec(None) == 0
    # 看不懂就退化成"不限速"而不是崩 —— 限速是优化, 不该拦住启动
    assert parse_bytes_per_sec("abc") == 0
    assert parse_bytes_per_sec("3.5MB") == int(3.5 * 1024 * 1024)


def test_byte_limiter_zero_rate_is_noop():
    lim = RL.ByteRateLimiter(0)
    assert lim.throttle(10 ** 7) == 0.0


def test_byte_limiter_throttles_above_burst():
    """桶容量 = 1 秒的量: 超出突发额度的那部分要等。

    ⚠️ `sleep` 替身的签名是 `(wait, progress_cb)` —— 与 `_interruptible_wait`
    一致。写成单参数会在第一个超额的块上 TypeError, 而不是"限速没生效"。
    """
    waits = []
    lim = RL.ByteRateLimiter(1000)      # 1000 B/s, 突发额度 1000B
    slept = lim.throttle(3000, sleep=lambda w, cb=None: waits.append(w))
    assert slept > 0
    assert waits and waits[0] > 0


def test_byte_limiter_zero_and_negative_bytes_are_ignored():
    lim = RL.ByteRateLimiter(1000)
    assert lim.throttle(0) == 0.0
    assert lim.throttle(-5) == 0.0
    assert lim.throttle("not-a-number") == 0.0


def test_set_byte_rate_syncs_settings_and_returns_effective():
    """⚠️ 必须同时写回 settings。

    桶在进程启动时就建好了, 只改桶的话下一块会被 `throttle_bytes` 的配置同步
    逻辑覆盖回去 —— 表现为"接口调了没反应", 而这最容易被归因成"限速是假的"。
    """
    from core.config import settings

    old = getattr(settings, "max_download_bytes_per_sec", 0)
    try:
        assert RL.set_byte_rate(2048) == 2048
        assert RL.bytes_limiter.rate == 2048
        assert settings.max_download_bytes_per_sec == 2048
        assert RL.set_byte_rate(-1) == 0
    finally:
        RL.set_byte_rate(old)


def test_throttle_bytes_never_raises(monkeypatch):
    """限速是优化, 不是流程的一环 —— 它绝不能把下载打挂。"""
    monkeypatch.setattr(RL.bytes_limiter, "throttle",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert RL.throttle_bytes(1024) == 0.0


def test_bandwidth_api_round_trip():
    from core.config import settings

    old = getattr(settings, "max_download_bytes_per_sec", 0)
    c = _client()
    try:
        assert c.get("/config/bandwidth").status_code == 200
        r = c.post("/config/bandwidth", json={"bytes_per_sec": 4096})
        assert r.status_code == 200
        assert r.json()["bytes_per_sec"] == 4096
        assert c.get("/config/bandwidth").json()["bytes_per_sec"] == 4096
        # 负数被夹到 0(= 不限速), 而不是塞一个非法值进桶
        assert c.post("/config/bandwidth", json={"bytes_per_sec": -100}).json()[
            "bytes_per_sec"] == 0
    finally:
        RL.set_byte_rate(old)


# ============================ 5. 资源库批量操作 ============================

def test_bulk_delete_records_only_keeps_file(ctx):
    t = ctx.task()
    rid, p = _done_file(ctx, t, "a.jpg")
    r = _client().post("/library/bulk-delete",
                       json={"ids": [rid], "with_files": False})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == 1 and body["files"] == 0
    assert db.get_resource(rid) is None
    assert p.is_file(), "默认口径是只删记录、不动文件"


def test_bulk_delete_with_files_removes_file(ctx):
    t = ctx.task()
    rid, p = _done_file(ctx, t, "a.jpg", b"x" * 100)
    r = _client().post("/library/bulk-delete",
                       json={"ids": [rid], "with_files": True})
    body = r.json()
    assert body["files"] == 1 and body["bytes"] == 100
    assert not p.exists()


def test_bulk_delete_keeps_shared_file_and_says_so(ctx):
    """⚠️ sha256 去重时后到的任务只是复用路径、不复制文件。

    删掉等于把先到的那个任务也掏空了。必须保留文件, 且**如实回报** ——
    不说的话用户下次在别的任务里又看到同一张图, 结论会是"删了没用"。
    """
    t1 = ctx.task(name="A")
    t2 = ctx.task(name="B")
    rid1, p = _done_file(ctx, t1, "shared.jpg")
    # 第二个任务复用同一个物理文件
    rid2 = ctx.res(t2, status="done", local_path=str(p), url="https://example.com/s2.jpg")
    db.update_resource(rid2, size=p.stat().st_size)
    assert db.resource_refs(str(p)) == 2

    body = _client().post("/library/bulk-delete",
                          json={"ids": [rid1], "with_files": True}).json()
    assert body["kept_files"] == [rid1]
    assert body["files"] == 0
    assert p.is_file(), "还有别的任务引用这个文件, 不能删"


def test_bulk_delete_reports_path_outside_download_root(ctx, tmp_path):
    """库里的 local_path 是**数据**, 不能当可信路径直接 unlink。"""
    t = ctx.task()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    rid = ctx.res(t, status="done", local_path=str(outside))
    body = _client().post("/library/bulk-delete",
                          json={"ids": [rid], "with_files": True}).json()
    # 记录删掉, 但文件必须留着, 并且**明确报错** —— 静默跳过会让人以为删干净了
    assert body["errors"], "越界路径必须出现在 errors 里"
    assert outside.is_file()
    assert db.get_resource(rid) is None


def test_bulk_delete_skips_unknown_ids(ctx):
    body = _client().post("/library/bulk-delete",
                          json={"ids": [99999], "with_files": False}).json()
    assert body["skipped"] == [99999] and body["deleted"] == 0


def test_library_archive_names_entries_by_album(ctx):
    """跨任务的包必须按相册分目录: 一堆 `00001.jpg` 装同一个包必然互相覆盖。"""
    t1 = ctx.task(name="相册甲")
    t2 = ctx.task(name="相册乙")
    _r1, p1 = _done_file(ctx, t1, "00001.jpg", b"AAA")
    _r2, p2 = _done_file(ctx, t2, "00001.jpg", b"BBB")
    ids = [db.find_resource_by_path(str(p1))["id"], db.find_resource_by_path(str(p2))["id"]]

    r = _client().get("/library/archive", params={"ids": ",".join(map(str, ids))})
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(zf.namelist())
    assert len(names) == 2, f"同名文件应当靠相册目录区分, 得到 {names}"
    assert all(n.startswith("相册") for n in names)


def test_library_archive_rejects_bad_ids():
    r = _client().get("/library/archive", params={"ids": "abc"})
    assert r.status_code == 400


def test_library_archive_404_when_nothing_resolvable(ctx):
    r = _client().get("/library/archive", params={"ids": "99999"})
    assert r.status_code == 404


# ============================ 6. Pexels 集合页 ============================

def test_collection_id_is_extracted_from_slug():
    from collectors.stockphotos.spider import _collection_id

    assert _collection_id("nature-2sx8z9c") == "2sx8z9c"
    assert _collection_id("2sx8z9c") == "2sx8z9c"
    # 纯词抽不出 ID -> 明确返回 None, 而不是拿必然 404 的串去试
    assert _collection_id("featured") is None
    assert _collection_id("nature") is None
    assert _collection_id("") is None


def test_pexels_claims_collection_urls():
    """集合页是列表页(多图), 必须被判成聚合页而不是"无法解析"。

    ⚠️ 判据只能认 pexels 自己的 host: `_SEARCH_RE` 是路径模板匹配, 别的站点
    只要路径里出现 /collections/ 就会被误判成 pexels 的活。
    """
    from collectors.scores import SCORE_AGGREGATE_PAGE
    from collectors.stockphotos.spider import PexelsSpider

    assert PexelsSpider.match_score(
        "https://www.pexels.com/collections/nature-2sx8z9c/") == SCORE_AGGREGATE_PAGE
    assert PexelsSpider.match_score(
        "https://www.pexels.com/search/cat/") == SCORE_AGGREGATE_PAGE
    # 别的站点: 不能认领
    assert PexelsSpider.match_score("https://evil.com/collections/nature-2sx8z9c/") is None


def test_fetch_collection_page_handles_photos_shape(monkeypatch):
    from collectors.stockphotos import spider as S

    class Sess:
        def get(self, url, **k):
            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "photos": [
                            {"id": 1, "src": {"original": "https://i.p.com/1.jpeg"}},
                        ],
                        "next_page": None,
                    }

                @staticmethod
                def raise_for_status():
                    pass

            return R()

    # /collections/{id} 必须出现在路径里, 否则等于悄悄退回了搜索端点
    seen = {}
    orig = S.PexelsSpider._api_get

    def spy(self, sess, key, path, params, what):
        seen["path"] = path
        return orig(self, sess, key, path, params, what)

    monkeypatch.setattr(S.PexelsSpider, "_api_get", spy)
    out, has_next = S.PexelsSpider()._fetch_collection_page(Sess(), "k", "2sx8z9c", 1)
    assert seen["path"] == "/collections/2sx8z9c"
    assert len(out) == 1 and out[0]["url"] == "https://i.p.com/1.jpeg"
    assert out[0]["source"] == "pexels_collection"
    assert has_next is False


def test_fetch_collection_page_handles_media_shape():
    """集合端点在不同版本里用过 `photos` 与 `media` 两种顶层键。

    只认一种的话另一种会表现为"集合是空的"(而不是报错) —— 用户完全无从判断
    到底是没图还是我们解析错了。
    """
    from collectors.stockphotos.spider import PexelsSpider

    class Sess:
        def get(self, url, **k):
            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "media": [
                            {"type": "Photo",
                             "photo": {"id": 2, "src": {"large": "https://i.p.com/2.jpeg"}}},
                            {"type": "Video"},     # 没有 photo 也没有 src -> 跳过
                        ],
                        "next_page": "x",
                    }

                @staticmethod
                def raise_for_status():
                    pass

            return R()

    out, has_next = PexelsSpider()._fetch_collection_page(Sess(), "k", "9", 1)
    assert len(out) == 1 and out[0]["url"] == "https://i.p.com/2.jpeg"
    assert has_next is True


def test_resources_of_skips_entries_without_any_url():
    """拿不到 URL 就跳过这一条, 而不是塞个空串进去 —— 下载层会报一个含糊的错。"""
    from collectors.stockphotos.spider import _resources_of

    assert _resources_of([{"id": 1, "src": {}}]) == []
    assert _resources_of([None, "x", 3]) == []


def test_collection_without_key_asks_for_one(monkeypatch):
    from collectors.stockphotos.spider import PexelsSpider

    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    with pytest.raises(ValueError) as e:
        PexelsSpider().crawl("https://www.pexels.com/collections/nature-2sx8z9c/")
    assert "PEXELS_API_KEY" in str(e.value)


def test_collection_with_unresolvable_slug_says_what_to_do(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "fake")
    from collectors.stockphotos.spider import PexelsSpider

    with pytest.raises(ValueError) as e:
        PexelsSpider().crawl("https://www.pexels.com/collections/nature/")
    msg = str(e.value)
    # 报错必须给出可执行的替代路径, 而不是只说"取不到 ID"
    assert "搜索页" in msg or "照片" in msg


# ============================ 7. 代理熔断持久化 ============================

def test_proxy_health_failure_threshold_sets_cooldown():
    proxy_health.clear()
    proxy_health.note_failure("http://a:1", 3, 300)
    proxy_health.note_failure("http://a:1", 3, 300)
    assert proxy_health.blocked_for("http://a:1") == 0.0     # 还没到阈值
    proxy_health.note_failure("http://a:1", 3, 300)
    assert proxy_health.blocked_for("http://a:1") > 0
    assert proxy_health.load("http://a:1")["fails"] == 3


def test_proxy_health_success_clears_state():
    proxy_health.clear()
    for _ in range(3):
        proxy_health.note_failure("http://a:1", 3, 300)
    proxy_health.note_success("http://a:1")
    st = proxy_health.load("http://a:1")
    assert st["fails"] == 0 and st["blocked_until"] == 0.0
    assert proxy_health.blocked_for("http://a:1") == 0.0


def test_proxy_health_survives_process_restart():
    """换一个 pool 实例(等价于重启后重建)仍然记得那条线是坏的 ——

    这是本项能力的**全部意义**: 否则"上一轮已经发现第 2 条线是死的"下次还要
    重踩一遍, 代价是几个资源白下 + 几轮重试退避。
    """
    proxy_health.clear()
    p = B.ProxyPool("http://a:1,http://b:2", fail_threshold=2, cooldown=300)
    assert p.pick(0) == "http://a:1"
    p.note_failure("http://a:1")
    p.note_failure("http://a:1")

    fresh = B.ProxyPool("http://a:1,http://b:2", fail_threshold=2, cooldown=300)
    assert fresh._is_blocked("http://a:1"), "新实例应当继承熔断状态"
    assert fresh.pick(0) == "http://b:2"


def test_proxy_pool_persist_false_is_isolated():
    """显式 persist=False 时不落盘 —— 这是测试与一次性探测用的开关。"""
    proxy_health.clear()
    p = B.ProxyPool("http://z:1", fail_threshold=1, cooldown=300, persist=False)
    p.note_failure("http://z:1")
    assert proxy_health.load("http://z:1")["fails"] == 0
    assert p._is_blocked("http://z:1")


def test_proxy_health_can_be_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UWC_PROXY_HEALTH", "off")
    proxy_health.note_failure("http://a:1", 1, 300)
    # 关闭时读写都退化成空, 且不写任何文件
    assert proxy_health.load("http://a:1")["fails"] == 0
    assert proxy_health.snapshot() == []


def test_proxy_health_has_no_lost_updates_under_concurrency():
    """⚠️ 与 cdn_profile 同型: 读者不走锁 + replace 被占用挡下 = 静默丢一半。

    这里用"多线程各自记不同代理"来验证"写入不丢"; 每个代理记一次, 全部都要在。
    """
    proxy_health.clear()
    n = 60

    def one(i):
        proxy_health.note_failure(f"http://p{i}:1", 99, 60)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    snap = {e["proxy"] for e in proxy_health.snapshot()}
    assert len(snap) == n, f"应记下 {n} 条, 实际 {len(snap)} 条"


def test_proxy_health_cooldown_expires_to_half_open():
    proxy_health.clear()
    proxy_health.note_failure("http://a:1", 1, 0)     # 冷却 0 秒 = 立刻到期
    assert proxy_health.blocked_for("http://a:1") == 0.0
    p = B.ProxyPool("http://a:1", fail_threshold=1, cooldown=0)
    assert not p._is_blocked("http://a:1")


# ============================ 8. 落盘后完整性巡检 ============================

def _truncated_png(path):
    """PNG 头齐全但没有 IEND —— mediacheck 能**算术级**地证明它被截断了。"""
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)


def test_verify_flags_missing_file(ctx):
    t = ctx.task()
    rid, p = _done_file(ctx, t, "gone.jpg")
    p.unlink()
    body = _client().post("/library/verify", json={"limit": 100}).json()
    assert body["checked"] == 1
    assert body["missing"] == 1 and body["marked"] == 1
    assert body["items"][0]["kind"] == KIND_MISSING
    assert db.get_resource(rid)["error_kind"] == KIND_MISSING


def test_verify_flags_truncated_file(ctx):
    t = ctx.task()
    p = ctx.root / "broken.png"
    _truncated_png(p)
    rid = ctx.res(t, status="done", local_path=str(p))
    db.update_resource(rid, size=p.stat().st_size)
    body = _client().post("/library/verify", json={"limit": 100}).json()
    assert body["truncated"] == 1
    assert body["items"][0]["kind"] == KIND_CORRUPT


def test_verify_flags_file_smaller_than_recorded(ctx):
    """文件比记录里小 = 被外部截断过(变大则可能是被替换, 不在这里报)。"""
    t = ctx.task()
    rid, p = _done_file(ctx, t, "shrunk.jpg", b"\xff\xd8\xff\xd9")
    db.update_resource(rid, size=10_000)      # 记录说它有一万字节
    body = _client().post("/library/verify", json={"limit": 100}).json()
    assert body["truncated"] == 1


def test_verify_passes_healthy_file(ctx):
    t = ctx.task()
    _done_file(ctx, t, "ok.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")
    body = _client().post("/library/verify", json={"limit": 100}).json()
    assert body["checked"] == 1
    assert body["marked"] == 0 and body["items"] == []


def test_verify_only_scans_done_resources(ctx):
    """失败/待处理的行没有文件可核 —— 算进分母会让"缺失率"失去意义。"""
    t = ctx.task()
    ctx.res(t, status="failed", local_path=str(ctx.root / "nope.jpg"))
    ctx.res(t, status="pending")
    body = _client().post("/library/verify", json={"limit": 100}).json()
    assert body["checked"] == 0


def test_verify_marks_but_never_deletes(ctx):
    """判据可能误报(文件被外部程序改小了), 而删文件是不可逆的。"""
    t = ctx.task()
    rid, p = _done_file(ctx, t, "gone.jpg")
    p.unlink()
    _client().post("/library/verify", json={"limit": 100})
    assert db.get_resource(rid) is not None, "巡检不能删记录"
    # 而且**不改 status** —— 文件可能仍然能看(截断的 JPEG 前半段还是好的),
    # 改成 failed 会让用户以为整份没了。
    assert db.get_resource(rid)["status"] == "done"


def test_verify_can_be_scoped_to_one_task(ctx):
    t1 = ctx.task(name="A")
    t2 = ctx.task(name="B")
    _r1, p1 = _done_file(ctx, t1, "a.jpg")
    _r2, p2 = _done_file(ctx, t2, "b.jpg")
    p1.unlink()
    body = _client().post("/library/verify", json={"task_id": t1, "limit": 100}).json()
    assert body["checked"] == 1 and body["missing"] == 1


def test_verify_clamps_limit(ctx):
    """limit 是"别把接口挂住"的闸, 不能被传成 0/负数/巨大值。"""
    assert _client().post("/library/verify", json={"limit": 0}).status_code == 200
    assert _client().post("/library/verify", json={"limit": 10 ** 9}).status_code == 200
