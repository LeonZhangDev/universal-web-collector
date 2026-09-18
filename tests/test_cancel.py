"""取消必须穿透下载层。

这里的每个用例都对应一个真实踩过的坑: TaskCancelled 被 `except Exception`
吞掉后, 用户点了"停止"却要干等一轮退避重睡(2s+4s), 期间资源还卡在
downloading, 半成品留在磁盘上被误认为下载完成。所以这里断言的不是
"能抛出", 而是**没有退避延迟**。
"""

import time

import pytest

from core.cancel import TaskCancelled
from downloaders.base import download_with_mirrors, stream_download


@pytest.fixture(autouse=True)
def no_proxy():
    """测试不是压测: 关掉站点限速(throttle)与代理, 只验证中断语义。"""
    from core.config import settings
    from downloaders import base

    saved = dict(base.SESSION.proxies)
    saved_gap = (settings.domain_min_interval, settings.domain_max_interval)
    base.SESSION.proxies.clear()
    settings.domain_min_interval = 0
    settings.domain_max_interval = 0
    yield
    base.SESSION.proxies.clear()
    base.SESSION.proxies.update(saved)
    settings.domain_min_interval, settings.domain_max_interval = saved_gap


class _Boom:
    """第 n 次回调时抛出 TaskCancelled 的假“网络”。"""

    def __init__(self, fail_at=1):
        self.calls = 0
        self.fail_at = fail_at

    def __call__(self):
        self.calls += 1
        if self.calls >= self.fail_at:
            raise TaskCancelled()


def _serve(monkeypatch, chunks=8):
    """把 requests.Session.get 换成吐 chunks 块的假响应。"""
    import requests

    class Resp:
        def __init__(self):
            self.headers = {"Content-Type": "image/jpeg"}
            self.status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for _ in range(chunks):
                yield b"x" * size

    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Resp())


def test_cancel_not_swallowed_as_failure(tmp_path, monkeypatch):
    """取消不能被当成一次失败: 否则 image_retries 会让它退避重睡若干秒。"""
    _serve(monkeypatch)
    cb = _Boom(fail_at=2)

    t0 = time.time()
    with pytest.raises(TaskCancelled):
        stream_download("https://x/1.jpg", tmp_path / "1.jpg", {}, progress_cb=cb)
    took = time.time() - t0

    assert cb.calls == 2, "第一个生命体征后即应中断, 不该继续下载"
    assert took < 1.0, f"取消应立即生效, 实测 {took:.2f}s(疑似走了退避重试)"


def test_mirror_loop_stops_on_cancel(tmp_path, monkeypatch):
    """叫停时不要再去试下一个下载点。"""
    _serve(monkeypatch)
    calls = []

    def cb():
        calls.append(1)
        raise TaskCancelled()

    with pytest.raises(TaskCancelled):
        download_with_mirrors(
            "https://x/1.jpg", tmp_path / "1.jpg", {},
            mirrors=["https://x/1_800x0.webp", "https://x/1_1200x0.webp"],
            log=lambda m: calls.append(m),
            progress_cb=cb,
        )
    assert not any("切换下载点" in str(c) for c in calls), "取消了还在轮换镜像"


def test_ordinary_error_still_retries(tmp_path, monkeypatch):
    """反过来: 普通异常还要照旧重试, 别把"取消特判"写成"一律不重试"。"""
    import requests

    attempts = []

    class Boom(Exception):
        pass

    class Resp:
        def __init__(self):
            self.headers = {"Content-Type": "image/jpeg"}
            self.status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            attempts.append(1)
            raise Boom("boom")

    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(time, "sleep", lambda s: None)  # 别真等退避间隔

    with pytest.raises(Boom):
        stream_download("https://x/1.jpg", tmp_path / "1.jpg", {}, retries=3)
    assert len(attempts) == 3, "普通失败应当重试到上限"


def test_task_manager_reexports_cancel_type():
    """task_manager 里的 TaskCancelled 必须是同一个类型, 否则 except 抓不到。"""
    from core import task_manager as tm

    assert tm.TaskCancelled is TaskCancelled
