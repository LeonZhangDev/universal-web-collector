"""健壮性回归: 这一轮补上的四类缺口。

每个用例都对应一个"没有报错、没有日志、只能等用户发现"的失效模式 ——
它们的特点都是**静默**, 所以断言也一律盯着"最终产物对不对", 而不是"有没有抛错"。
"""

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import core.database as db
import core.disk as disk
from core.config import settings
from core.errors import (CollectorError, DiskFullError, TransientError,
                         describe, is_retryable)
from downloaders import base


# ---------------------------------------------------------------- 假网络

class Resp:
    """够用的假响应: 吐固定字节, 可指定状态码与头部。"""

    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk):
        yield self.body

    def close(self):
        pass


@pytest.fixture
def quiet(monkeypatch):
    """关掉限速与代理 —— 这些用例只验证落盘语义, 不是压测。"""
    saved = (settings.domain_min_interval, settings.domain_max_interval)
    settings.domain_min_interval = 0
    settings.domain_max_interval = 0
    yield
    settings.domain_min_interval, settings.domain_max_interval = saved


def serve(monkeypatch, body, status=200, headers=None):
    monkeypatch.setattr(base.SESSION, "get",
                        lambda *a, **k: Resp(body, status, headers))


# ---------------------------------------------------------------- 异常分类

def test_describe_keeps_human_message_intact():
    """分类异常的消息是写给用户看的, 不能再套一层类型名。"""
    assert describe(CollectorError("基址全部没命中, 试过: a, b")) == \
        "基址全部没命中, 试过: a, b"


def test_describe_adds_type_for_unclassified():
    """未分类的异常补上类型名: 光有消息常常看不出是哪一层出的问题。"""
    assert describe(ValueError("boom")) == "ValueError: boom"


def test_is_retryable_separates_our_bug_from_the_world():
    """环境问题重试多少次都一样; 只有传输类才值得重试。"""
    assert is_retryable(TransientError("connection reset")) is True
    assert is_retryable(CollectorError("磁盘满了")) is False
    assert is_retryable(ValueError("size 是字符串")) is False, \
        "我们的 bug 不该靠重试掩盖"


# ---------------------------------------------------------------- 原子落盘

def test_interrupted_download_leaves_no_file_at_final_path(quiet, monkeypatch, tmp_path):
    """中断后: 目标位置必须是干净的, 内容只留在 .part。

    旧行为是直接写最终路径 —— 半截文件留在那儿就会被当成"已下载",
    参与去重、manifest、进度统计, 用户打开才发现是坏的。
    """
    from core.cancel import TaskCancelled

    target = tmp_path / "a.jpg"
    serve(monkeypatch, b"x" * 100)

    def boom():
        raise TaskCancelled()

    with pytest.raises(Exception):
        base.stream_download("http://x/1.jpg", target, {}, retries=1,
                             progress_cb=boom)
    assert not target.exists(), "半成品不该出现在最终位置"
    assert base._part_path(target).exists(), "内容应该留在 .part 里以便续传"


def test_resume_from_other_url_discards_stale_part(quiet, monkeypatch, tmp_path):
    """续传时残片来自**另一个 URL** -> 丢弃重下, 绝不拼接。

    ⚠️ 这是本轮最难查的一类: 两份不同文件拼起来长度可能刚好对上,
    sha256 也会把"拼好的坏文件"算得毫无破绽。只有来源记录能挡住它。
    """
    target = tmp_path / "a.jpg"
    part = base._part_path(target)
    part.write_bytes(b"GARBAGE-FROM-ANOTHER-URL")
    base._write_src(base._part_src(target), "http://other/source.jpg")

    serve(monkeypatch, b"REAL-CONTENT")
    sha = base.stream_download("http://real/1.jpg", target, {}, retries=1)

    assert target.read_bytes() == b"REAL-CONTENT"
    assert sha == hashlib.sha256(b"REAL-CONTENT").hexdigest()
    assert not part.exists()
    assert not base._part_src(target).exists()


def test_resume_from_same_url_appends_and_hashes_prefix(quiet, monkeypatch, tmp_path):
    """同一个 URL 的残片: 续传, 且 sha256 要把已有前缀算进去。"""
    target = tmp_path / "a.jpg"
    part = base._part_path(target)
    part.write_bytes(b"AB")
    url = "http://real/1.jpg"
    base._write_src(base._part_src(target), url)

    serve(monkeypatch, b"CDEF", status=206,
          headers={"Content-Range": "bytes 2-5/6"})
    sha = base.stream_download(url, target, {}, retries=1)

    assert target.read_bytes() == b"ABCDEF"
    assert sha == hashlib.sha256(b"ABCDEF").hexdigest()


def test_length_mismatch_is_reported_not_silently_saved(quiet, monkeypatch, tmp_path):
    """声明 100 字节却只收到 3 字节 -> 报错, 且不留残片(否则下次续传接着坏文件续)。"""
    target = tmp_path / "a.jpg"
    serve(monkeypatch, b"abc", headers={"Content-Length": "100"})

    with pytest.raises(OSError):
        base.stream_download("http://x/1.jpg", target, {}, retries=1)
    assert not target.exists()
    assert not base._part_path(target).exists(), "不完整的残片必须丢弃"


def test_compressed_response_skips_length_check(quiet, monkeypatch, tmp_path):
    """有 Content-Encoding 时不校验长度 —— 那是压缩后的字节数, 比了必误判。"""
    target = tmp_path / "a.jpg"
    serve(monkeypatch, b"abc", headers={"Content-Length": "999",
                                        "Content-Encoding": "gzip"})
    base.stream_download("http://x/1.jpg", target, {}, retries=1)
    assert target.read_bytes() == b"abc"


# ---------------------------------------------------------------- 磁盘守卫

def test_disk_guard_fails_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, "free_bytes", lambda t: 1024)
    monkeypatch.setattr(settings, "min_free_bytes", 10 * 1024 * 1024)
    with pytest.raises(DiskFullError) as ei:
        disk.ensure_free(tmp_path)
    assert "磁盘空间不足" in str(ei.value)


def test_disk_guard_passes_when_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(disk, "free_bytes", lambda t: 10 * 1024 * 1024 * 1024)
    monkeypatch.setattr(settings, "min_free_bytes", 200 * 1024 * 1024)
    disk.ensure_free(tmp_path)     # 不该抛


def test_disk_guard_silent_when_usage_unknown(tmp_path, monkeypatch):
    """查不出剩余空间就不拦 —— 把正常运行判成满盘会让用户一头雾水。"""
    monkeypatch.setattr(disk, "free_bytes", lambda t: None)
    monkeypatch.setattr(settings, "min_free_bytes", 999 * 1024 ** 4)
    disk.ensure_free(tmp_path)


def test_disk_full_message_tells_what_to_do(tmp_path, monkeypatch):
    """失败必须可操作: 光说 "No space left on device" 用户不知道下一步。"""
    monkeypatch.setattr(disk, "free_bytes", lambda t: 500 * 1024 * 1024)
    monkeypatch.setattr(settings, "min_free_bytes", 1024 ** 3)
    with pytest.raises(DiskFullError) as ei:
        disk.ensure_free(tmp_path)
    msg = str(ei.value)
    assert "继续" in msg and "已下好的文件" in msg
    assert isinstance(ei.value, CollectorError)   # 属于"给用户看"的那一类


# ---------------------------------------------------------------- 数据库

def test_write_retries_on_locked(monkeypatch):
    """撞上 "database is locked" 要退避重试, 而不是冒出去被当成业务失败。

    ⚠️ 这个错一旦落在业务 try 里就会被当成"这个资源下载失败", 最后表现为
    任务莫名其妙 failed, 日志里只有一堆 locked —— 报错离真相极远。
    """
    monkeypatch.setattr(db, "DB_WRITE_RETRIES", 5)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert db._retry_write(flaky) == "ok"
    assert calls["n"] == 3


def test_non_locked_error_is_not_retried():
    """SQL 语法错误之类不能被重试掩盖 —— 重试只是把真 bug 藏起来。"""
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        db._retry_write(broken)
    assert calls["n"] == 1


def test_retry_gives_up_instead_of_hanging(monkeypatch):
    """一直撞锁不能无限重试 —— 有上限才会变成一条可诊断的错误。"""
    monkeypatch.setattr(db, "DB_WRITE_RETRIES", 2)
    calls = {"n": 0}

    def always_locked():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        db._retry_write(always_locked)
    assert calls["n"] == 2


def test_logs_are_trimmed(tmp_db, monkeypatch):
    """日志不能无限增长: 任务删掉后那些行不会自己消失。"""
    monkeypatch.setattr(db, "LOG_KEEP_PER_TASK", 20)
    monkeypatch.setattr(db, "LOG_TRIM_EVERY", 1)
    tid = tmp_db.create_task("http://x/1")
    for i in range(50):
        tmp_db.add_log(tid, f"line {i}")
    rows = tmp_db.get_logs(tid)
    assert len(rows) == 20
    assert rows[-1]["message"] == "line 49", "保留的必须是最新的那些"


# ---------------------------------------------------------------- 孤儿任务

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """每个用例一份库: 用例之间不能靠磁盘文件偷偷通信。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_conn", None)
    return db


def _make_active(tmp_db, status="downloading", hb=None, with_resource=True):
    tid = tmp_db.create_task("http://x/1")
    tmp_db.update_task(tid, status=status, hb=hb)
    if with_resource:
        tmp_db.add_resource(tid, "image", "http://x/1.jpg")
    return tid


def test_orphan_with_resources_becomes_resumable(tmp_db):
    """有资源清单 -> 复位成 paused, 已下好的文件还能接着用。"""
    from core.task_manager import recover_orphans

    tid = _make_active(tmp_db, hb=time.time() - 100000, with_resource=True)
    got = recover_orphans(stale_timeout=60)

    assert tid in got
    assert tmp_db.get_task(tid)["status"] == "paused"
    logs = tmp_db.get_logs(tid)
    assert any("启动补偿" in l["message"] for l in logs)


def test_orphan_without_resources_becomes_failed(tmp_db):
    """没有资源清单 -> failed。续跑一个空清单只会得到"成功但 0 资源"。"""
    from core.task_manager import recover_orphans

    tid = _make_active(tmp_db, hb=time.time() - 100000, with_resource=False)
    recover_orphans(stale_timeout=60)

    task = tmp_db.get_task(tid)
    assert task["status"] == "failed"
    assert "重试" in (task["error"] or ""), "失败信息必须告诉用户下一步"


def test_fresh_heartbeat_is_left_alone(tmp_db):
    """心跳新鲜 = 别的进程正在跑, 绝不能碰 —— 否则又是一次"误杀活任务"。 """
    from core.task_manager import recover_orphans

    tid = _make_active(tmp_db, hb=time.time(), with_resource=True)
    got = recover_orphans(stale_timeout=60)

    assert got == []
    assert tmp_db.get_task(tid)["status"] == "downloading"


# ---------------------------------------------------------------- 同路径独占

def test_two_writers_to_same_part_one_fails_loudly(quiet, monkeypatch, tmp_path):
    """两个任务采到同一张图 -> 后到的一方报错退出, 而不是双方都"成功"。

    ⚠️ 交错写入的结果没有任何报错: 两边都报 done, 文件也在、大小也差不多,
    只有打开的那一刻才发现是坏的 —— 而且 sha256 是把错字节算出来的, 去重
    和 manifest 都会把它当成一份正常的成果。
    """
    target = tmp_path / "a.jpg"
    part = base._part_path(target)
    real_open = open
    stolen = {"n": 0}

    def racing_open(p, mode="r", *a, **k):
        # 模拟"另一个任务刚抢先建了这个 .part"
        if Path(p) == part and "x" in mode and stolen["n"] == 0:
            stolen["n"] += 1
            raise FileExistsError(str(p))
        return real_open(p, mode, *a, **k)

    # ⚠️ raising=False: `open` 是内置函数, 模块里本来没有这个属性。
    # 设进模块全局即可生效 —— Python 查名字时模块全局优先于 builtins。
    monkeypatch.setattr(base, "open", racing_open, raising=False)
    serve(monkeypatch, b"REAL")

    with pytest.raises(FileExistsError) as ei:
        base.stream_download("http://x/1.jpg", target, {}, retries=1)
    assert "另一处正在下载" in str(ei.value), "失败信息要说清为什么放弃"
    assert not target.exists(), "没抢到的不能假装成功落盘"


# ---------------------------------------------------------------- 采集可取消

def test_crawl_can_be_stopped_midway(tmp_db, tmp_path, monkeypatch):
    """逐张枚举几百个序号要跑十几分钟, 点"停止"必须马上停。

    取消检查点原本只装在下载循环里 —— 采集阶段点了停止, 界面变灰了,
    后台却还在把剩下的序号一个个探完, 用户以为停了其实没有。
    """
    import threading

    from core import task_manager as tm
    from core.cancel import TaskCancelled

    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        tid = tmp_db.create_task("http://x/album", "fake", None, {})
        # 直接构造活动条目: 这里只验证 _crawl 的检查点, 不走完整 submit 流程
        mgr._active[tid] = {"future": None, "cancel": threading.Event(),
                            "hb": time.monotonic()}
        mgr._active[tid]["cancel"].set()

        seen = {"n": 0}

        class SlowSpider:
            def crawl(self, url, log=None):
                for i in range(1000):
                    seen["n"] += 1
                    log(f"probing {i}")
                    time.sleep(0.01)
                return []

        t0 = time.time()
        with pytest.raises(TaskCancelled):
            mgr._crawl(SlowSpider(), "http://x/album", tid)

        assert time.time() - t0 < 1, "取消后不该继续枚举"
        assert seen["n"] <= 2, f"第一次 log 就该停, 实际枚举了 {seen['n']} 次"
    finally:
        mgr.shutdown(wait=True)


def test_crawl_log_does_not_hide_the_real_error(tmp_db, tmp_path, monkeypatch):
    """采集器在 except 里调 log 报错误时, 取消检查不能把真因顶掉。

    否则用户看到的是"任务已取消", 而真相是"基址全没命中" —— 又是一次
    报错离真相很远, 而且连日志都指向错误的方向。
    """
    from core import task_manager as tm

    mgr = tm.TaskManager(max_workers=1, download_workers=1)
    try:
        tid = tmp_db.create_task("http://x/album", "fake", None, {})
        import threading

        mgr._active[tid] = {"future": None, "cancel": threading.Event(),
                            "hb": time.monotonic()}
        mgr._active[tid]["cancel"].set()

        class FailingSpider:
            def crawl(self, url, log=None):
                try:
                    raise ValueError("基址全部 MISSING")
                except ValueError:
                    log("基址探测失败")      # 异常传播中调 log: 不该被取消顶掉
                    raise

        with pytest.raises(ValueError) as ei:
            mgr._crawl(FailingSpider(), "http://x/album", tid)
        assert "MISSING" in str(ei.value)
    finally:
        mgr.shutdown(wait=True)


# ---------------------------------------------------------------- API 兜底

def test_unhandled_error_returns_stable_json(caplog):
    """漏网的异常要给前端稳定结构, 且不能把堆栈泄漏出去。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from main import install_exception_handlers

    app = install_exception_handlers(FastAPI())

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret path C:\\internal\\x.db")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level("ERROR"):
        resp = client.get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["type"] == "RuntimeError"
    assert "secret path" not in resp.text, "内部细节不能出现在响应里"
    assert "服务器内部错误" in body["detail"]
    assert "secret path" in caplog.text, "完整信息要留在服务端日志里供排查"
