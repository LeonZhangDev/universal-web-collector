"""Cloudflare 长期对策测试: 域级熔断 / 陈旧登录态隔离 / 降级可见。

为什么值得单独测
================
相册页在这个项目里只提供**锦上添花**的东西(目录名、自报数量、视频体积),
资源发现走的是纯 HTTP 序号枚举。所以"拿不到页面"本身不是灾难 —— 真正的坑是
另外两种:

1. **每次都白开一个 headless Chromium**。被 CF 拦住的时段里每次注定失败,
   一个任务却能连开十几次浏览器, 单次几十秒。没有熔断, 用户感知就是
   "明明在下东西, 界面却一直卡在没进度"。
2. **拿着失效的登录态反复去敲门**。带陈旧 `cf_clearance` 访问会直接得到
   `Attention Required!`, 而且是**永久拒绝**。这条路不但不通, 还比匿名更容易
   招来更严的策略 —— 所以必须在第一次识别出来就把它停掉。

顺带钉住一件容易搞反的事: **匿名被拦 ≠ 登录态失效**。只有带着登录态被拒
才能把锅记到登录态头上, 否则会无端让用户去重新登录。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest

import collectors.album_meta as AM

HOST = "https://xchina.co/photo/id-6aa5136f606fe.html"
DOMAIN = "xchina.co"

CHALLENGE_PAGE = "<html><head><title>Just a moment...</title></head><body>"
REJECT_PAGE = "<html><head><title>Attention Required! | Cloudflare</title></head><body>"


def ok_page(gid="6aa5136f606fe"):
    return (
        '<html><head><title>相册名 - 分类 - 站名</title></head><body>'
        '<div class="photo-items"></div>'
        f'<script>var favOptions = {{"objId": "{gid}"}};</script>'
        "</body></html>"
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """每条用例从干净状态开始; 并假定 Playwright 可用(这里不测它的可用性分支)。"""
    AM.reset_state()
    AM.clear_cache()
    monkeypatch.setattr(AM, "_PW_OK", True)
    monkeypatch.setattr(AM, "_playwright_missing", lambda: False)
    monkeypatch.setattr(AM, "_storage_state", lambda url: None)
    yield
    AM.reset_state()
    AM.clear_cache()


@pytest.fixture()
def loader(monkeypatch):
    """替换真正开浏览器的那一步; 记录每次调用用的登录态。"""
    calls = []

    class Fake:
        """能被用例改 HTML 的假 loader; `calls` 顺序就是浏览器的开启顺序。"""

        html = ""

        def __call__(self, url, log=None, storage_state=None, **kw):
            calls.append(storage_state)
            return self.html

    fake = Fake()
    monkeypatch.setattr(AM, "_load_html", fake)
    fake.calls = calls
    return fake


# ---- 熔断 ----


def test_breaker_trips_after_repeated_blocks(loader):
    loader.html = CHALLENGE_PAGE
    for _ in range(AM._CF_TRIP):
        assert AM.fetch_album_meta(HOST, log=lambda m: None) is None
    assert AM.cooldown_left(HOST) > 0, "连续被拦之后应该进入冷却"


def test_during_cooldown_no_browser_is_launched(loader):
    """冷却期内**一次浏览器都不开** —— 这是熔断的全部意义所在。"""
    loader.html = CHALLENGE_PAGE
    for _ in range(AM._CF_TRIP):
        AM.fetch_album_meta(HOST, log=lambda m: None)
    opened = len(loader.calls)

    logs = []
    assert AM.fetch_album_meta(HOST, log=logs.append) is None
    assert len(loader.calls) == opened, "冷却期内不该再开浏览器"
    assert any("Cloudflare" in m for m in logs), "要说清楚为什么这次不读相册页"
    assert any("图集 ID" in m for m in logs), "要说清楚降级之后会怎样"


def test_cooldown_is_per_domain(loader):
    """一个站被拦, 不该连累别的站 —— 熔断必须按域隔离。"""
    loader.html = CHALLENGE_PAGE
    for _ in range(AM._CF_TRIP):
        AM.fetch_album_meta(HOST, log=lambda m: None)
    assert AM.cooldown_left("https://other.example/a.html") == 0


def test_success_resets_the_counter(loader):
    """失败是"连续"才有意义: 中间成过一次就不该再累积。"""
    loader.html = CHALLENGE_PAGE
    AM.fetch_album_meta(HOST, log=lambda m: None)
    AM.fetch_album_meta(HOST, log=lambda m: None)

    loader.html = ok_page()
    assert AM.fetch_album_meta(HOST, gid="6aa5136f606fe", log=lambda m: None)
    assert AM.cooldown_left(HOST) == 0

    loader.html = CHALLENGE_PAGE
    AM.fetch_album_meta(HOST, log=lambda m: None)
    AM.fetch_album_meta(HOST, log=lambda m: None)
    assert AM.cooldown_left(HOST) == 0, "又在连着失败, 但不该继承成功之前的旧账"


def test_page_shape_mismatch_does_not_trip_the_breaker(loader):
    """站点改版导致页面解析不出东西, 与"被 Cloudflare 拦住"是两回事。"""
    loader.html = ok_page(gid="someone-else")   # 页面正常, 只是 objId 对不上
    AM.fetch_album_meta(HOST, gid="6aa5136f606fe", log=lambda m: None)
    AM.fetch_album_meta(HOST, gid="6aa5136f606fe", log=lambda m: None)
    assert AM.cooldown_left(HOST) == 0


def test_missing_html_counts_as_being_blocked(loader):
    """连正文都没拿到(挑战没解开)同样算一次拦截。"""
    loader.html = ""
    for _ in range(AM._CF_TRIP):
        assert AM.fetch_album_meta(HOST, log=lambda m: None) is None
    assert AM.cooldown_left(HOST) > 0


# ---- 陈旧登录态隔离 ----


def test_stale_login_state_is_quarantined_after_rejection(loader, monkeypatch):
    """带失效登录态访问 -> 立刻隔离, 之后不再拿它去敲门。"""
    monkeypatch.setattr(AM, "_storage_state",
                        lambda url: "/fake/browser_state/xchina.co.json")
    monkeypatch.setattr(AM, "_CF_TRIP", 10)   # 本用例只关心登录态隔离, 别让熔断插进来
    loader.html = REJECT_PAGE
    AM.fetch_album_meta(HOST, log=lambda m: None)

    assert DOMAIN in AM.stale_state_domains()
    loader.calls.clear()
    AM.fetch_album_meta(HOST, log=lambda m: None)
    assert loader.calls == [None], "已失效的登录态不该再出现在重试序列里"


def test_quarantined_state_is_skipped_with_a_hint(loader, monkeypatch):
    """用户应该被告知"不是站点挂了, 是你那份登录态过期了"。"""
    monkeypatch.setattr(AM, "_storage_state",
                        lambda url: "/fake/browser_state/xchina.co.json")
    monkeypatch.setattr(AM, "_CF_TRIP", 10)
    loader.html = REJECT_PAGE
    AM.fetch_album_meta(HOST, log=lambda m: None)

    logs = []
    AM.fetch_album_meta(HOST, log=logs.append)
    assert any("重新登录" in m for m in logs), "要明确指向下一步: 重新登录"


def test_anonymous_block_does_not_blame_the_login_state(loader, monkeypatch):
    """没有带登录态却被拦 —— 这是站点在拦人, 不能甩锅给用户的登录态。"""
    monkeypatch.setattr(AM, "_storage_state",
                        lambda url: "/fake/browser_state/xchina.co.json")
    loader.html = CHALLENGE_PAGE          # 匿名也拿不到, 且不是 Attention Required
    AM.fetch_album_meta(HOST, log=lambda m: None)
    AM.fetch_album_meta(HOST, log=lambda m: None)
    assert AM.stale_state_domains() == []


def test_reset_clears_quarantine(loader, monkeypatch):
    """用户重新登录后应该能立刻恢复 —— 不该要求重启后端。"""
    monkeypatch.setattr(AM, "_storage_state",
                        lambda url: "/fake/browser_state/xchina.co.json")
    loader.html = REJECT_PAGE
    AM.fetch_album_meta(HOST, log=lambda m: None)
    AM.fetch_album_meta(HOST, log=lambda m: None)
    assert AM.stale_state_domains()

    AM.reset_state()
    assert AM.stale_state_domains() == []
    # 恢复之后会重新按"先匿名、再带登录态"的顺序走
    loader.calls.clear()
    loader.html = ok_page()
    AM.fetch_album_meta(HOST, gid="6aa5136f606fe", log=lambda m: None)
    assert loader.calls and loader.calls[0] is None


# ---- 降级可见 ----


def test_playwright_missing_is_reported_as_degrade_not_block(loader, monkeypatch):
    """没装 Playwright 不是 Cloudflare 的锅, 更不能把该域拖进冷却。"""
    monkeypatch.setattr(AM, "_playwright_missing", lambda: True)
    logs = []
    assert AM.fetch_album_meta(HOST, log=logs.append) is None
    assert not loader.calls, "不该白开浏览器"
    assert AM.cooldown_left(HOST) == 0
    assert any("图集 ID" in m for m in logs)


def test_every_failure_path_states_what_will_happen(loader):
    """降级日志必须回答两个问题: 出了什么事、接下来会怎样。"""
    loader.html = CHALLENGE_PAGE
    for _ in range(AM._CF_TRIP):
        logs = []
        AM.fetch_album_meta(HOST, log=logs.append)
        assert any("图集 ID" in m or "不再开浏览器" in m for m in logs)
