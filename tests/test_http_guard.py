"""HTTP 层的防护: 429 限速退避 + **站点级冷却**。

429 不是"这个文件坏了", 而是站点在说"慢一点"。所以:
1. 等待时长要听站点的(Retry-After), 而不是套普通的指数退避 —— 普通退避最长
   8 秒, 站点要求冷静几十秒时硬闯只会让封禁更久。
2. 等待必须是**站点级**的。只让撞墙的那一个线程退避, 同批其他线程还在按原节奏
   猛冲, 站点看到的整体压力没变, 它的判断就不会变 —— 只会更快把整站封掉。
   这是本文件的核心回归点: 早先的实现只 sleep 当前线程, 等于没解决问题。
"""

import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from downloaders import base as B  # noqa: E402
from downloaders import ratelimit as RL  # noqa: E402


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

    def close(self):
        """真 requests.Response 有它; 下载层在 finally 里调用。

        不加的话 AttributeError 会在 finally 里**覆盖掉原异常**, 重试循环被莫名
        多跑几轮 —— 表现为"pop from empty list"这种和真正原因毫不相干的报错。
        """
        self.closed = True


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls += 1
        return self.script.pop(0)


@contextmanager
def _noop_slot(url, log=None):
    yield


@pytest.fixture(autouse=True)
def clean_cooldown():
    """冷却表是模块级全局状态: 不清会把污染带到别的用例(表现为莫名等待)。"""
    RL.clear_cooldown()
    yield
    RL.clear_cooldown()


@pytest.fixture()
def no_slot(monkeypatch):
    """跳过站点限速器闸门 —— 这里测的是冷却, 不是间隔。"""
    monkeypatch.setattr(B, "domain_slot", _noop_slot)


def test_429_records_site_cooldown(no_slot, tmp_path):
    """站点说等 7 秒, 就必须按 7 秒这个量级冷静, 不是套用 2 秒的普通退避。"""
    sess = FakeSession([
        FakeResp(429, {"Retry-After": "7"}),
        FakeResp(200, {"Content-Type": "image/jpeg"}, b"imagedata"),
    ])
    sha, ctype = B._stream_one(
        "https://x/a.jpg", tmp_path / "a.jpg", {}, 3, False, sess, None
    )
    assert ctype == "image/jpeg" and sha
    left = RL.cooldown_left("https://x/a.jpg")
    assert 0 < left <= 7, f"应记下约 7 秒的站点冷却, 实际 {left}"


def test_429_without_header_uses_sane_default(no_slot, tmp_path):
    """没给 Retry-After 也不能立刻重闯。"""
    sess = FakeSession([
        FakeResp(429),
        FakeResp(200, {"Content-Type": "image/jpeg"}, b"data"),
    ])
    B._stream_one("https://x/a.jpg", tmp_path / "a.jpg", {}, 3, False, sess, None)
    assert RL.cooldown_left("https://x/a.jpg") > 1


def test_cooldown_is_site_wide_not_per_url(no_slot, tmp_path):
    """同站点的其他子域/其他文件都必须一起等 —— 否则限速形同虚设。"""
    sess = FakeSession([FakeResp(429, {"Retry-After": "20"})])
    with pytest.raises(B.RateLimited):
        B._stream_one("https://img.xchina.io/a.jpg", tmp_path / "a.jpg", {}, 1,
                      False, sess, None)
    assert RL.cooldown_left("https://img.xchina.io/a.jpg") > 0
    # 同注册域的另一台机器: 也必须感受到冷却
    assert RL.cooldown_left("https://cdn.xchina.io/b.jpg") > 0
    # 别的站点不该被牵连
    assert RL.cooldown_left("https://other.example/c.jpg") == 0


def test_429_is_not_reported_as_generic_failure(no_slot, tmp_path):
    """重试次数用尽后, 报错要说明是限速, 别让用户去查文件是不是坏了。"""
    sess = FakeSession([FakeResp(429, {"Retry-After": "1"}) for _ in range(3)])
    with pytest.raises(B.RateLimited) as ei:
        B._stream_one("https://x/a.jpg", tmp_path / "a.jpg", {}, 3, False, sess, None)
    assert "429" in str(ei.value)


def test_domain_slot_waits_out_cooldown():
    """冷却必须真的挡住后续请求 —— 这才是"站点级"的落点。

    用真实(但很短)的冷却时长: 等待逻辑依赖真实时钟推进, 把 sleep 换成空操作
    会让循环永远看不到冷却结束(死循环)。
    """
    url = "https://cool.example/a.jpg"
    RL.set_interval(0.0, None, domain=RL.site_key(url))
    RL.note_rate_limited(url, 0.25)

    t0 = time.monotonic()
    with RL.domain_slot(url):
        pass
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.1, f"应该在冷却里等待, 实际只花了 {elapsed:.3f}s"
    assert RL.cooldown_left(url) == 0, "等完就该解封, 否则后续请求永远被压着"


def test_cooldown_cap_keeps_absurd_retry_after_sane():
    """站点偶尔会给离谱的 Retry-After; 照单全收等于把这个站永久拉黑。"""
    assert RL.note_rate_limited("https://evil.example/a.jpg", 86400) == RL.COOLDOWN_CAP


def test_longer_cooldown_wins_over_shorter():
    """两个线程同时撞墙: 后到的长冷却不该被短的覆盖掉。"""
    url = "https://race.example/a.jpg"
    RL.note_rate_limited(url, 30)
    RL.note_rate_limited(url, 5)
    assert RL.cooldown_left(url) > 20


def test_retry_after_http_date_is_parsed():
    """Retry-After 也可能是 HTTP 日期而不只是秒数。"""
    resp = FakeResp(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    # 过期日期 -> 不需要再等(夹到 0)
    assert B._retry_after(resp, 5.0) == 0.0
    # 认不出的值退回默认
    assert B._retry_after(FakeResp(429, {"Retry-After": "soon"}), 5.0) == 5.0
    # 没有该头也退回默认
    assert B._retry_after(FakeResp(429), 5.0) == 5.0
