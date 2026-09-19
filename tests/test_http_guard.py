"""HTTP 层的防护: 429 限速退避。

429 不是"这个文件坏了", 而是站点在说"慢一点"。所以退避时长必须听站点的
(Retry-After), 而不是套用普通的指数退避 —— 普通退避最长 8 秒, 站点要求
冷静几十秒时硬闯只会让封禁更久。
"""

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from downloaders import base as B  # noqa: E402


class FakeResp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, _chunk):
        yield self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls += 1
        return self.script.pop(0)


@contextmanager
def _noop_slot(url):
    yield


@pytest.fixture()
def no_sleep(monkeypatch):
    """记录 sleep 时长而不真的睡, 同时跳过站点限速器。"""
    slept = []
    monkeypatch.setattr(B.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(B, "domain_slot", _noop_slot)
    return slept


def test_429_honors_retry_after(no_sleep, tmp_path):
    """站点说等 7 秒, 就必须按 7 秒这个量级等, 不是套用 2 秒的普通退避。"""
    sess = FakeSession([
        FakeResp(429, {"Retry-After": "7"}),
        FakeResp(200, {"Content-Type": "image/jpeg"}, b"imagedata"),
    ])
    sha, ctype = B._stream_one(
        "https://x/a.jpg", tmp_path / "a.jpg", {}, 3, False, sess, None
    )
    assert ctype == "image/jpeg"
    assert sha
    assert no_sleep, "被 429 后必须等待, 不能立刻重试"
    # 抖动因子 0.8~1.4
    assert 7 * 0.8 <= no_sleep[0] <= 7 * 1.4


def test_429_without_header_uses_sane_default(no_sleep, tmp_path):
    """没给 Retry-After 也不能立刻重闯。"""
    sess = FakeSession([
        FakeResp(429),
        FakeResp(200, {"Content-Type": "image/jpeg"}, b"data"),
    ])
    B._stream_one("https://x/a.jpg", tmp_path / "a.jpg", {}, 3, False, sess, None)
    assert no_sleep and no_sleep[0] > 1


def test_429_is_not_reported_as_generic_failure(no_sleep, tmp_path):
    """重试次数用尽后, 报错要说明是限速, 别让用户去查文件是不是坏了。"""
    sess = FakeSession([FakeResp(429, {"Retry-After": "1"}) for _ in range(3)])
    with pytest.raises(B.RateLimited) as ei:
        B._stream_one("https://x/a.jpg", tmp_path / "a.jpg", {}, 3, False, sess, None)
    assert "429" in str(ei.value)


def test_retry_after_http_date_is_parsed():
    """Retry-After 也可能是 HTTP 日期而不只是秒数。"""
    resp = FakeResp(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    # 过期日期 -> 不需要再等(夹到 0)
    assert B._retry_after(resp, 5.0) == 0.0
    # 认不出的值退回默认
    assert B._retry_after(FakeResp(429, {"Retry-After": "soon"}), 5.0) == 5.0
    # 没有该头也退回默认
    assert B._retry_after(FakeResp(429), 5.0) == 5.0
