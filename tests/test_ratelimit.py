import time

from downloaders.ratelimit import DomainLimiter, site_key


def test_site_key_groups_subdomains():
    """同站不同子域必须归到同一个限速器。

    否则 img./cdn./static. 各持一个限速器, 站点整体节奏不受控, 限速等于失效。
    """
    assert site_key("https://img.xchina.io/photos/a/1.jpg") == "xchina.io"
    assert site_key("https://cdn.xchina.io/hls/x.m3u8") == "xchina.io"
    assert site_key("https://xchina.io/") == "xchina.io"
    assert site_key("https://a.example.co.uk/p") == "example.co.uk"
    assert site_key("http://127.0.0.1:8000/x") == "127.0.0.1"


def test_site_key_respects_explicit_groups():
    """config 的 site_groups 可显式把不同注册域归成同一站点。"""
    from core.config import settings

    old = settings.site_groups
    settings.site_groups = {"xc": ["xchina.io", "xchina.co"]}
    try:
        assert site_key("https://img.xchina.co/a.jpg") == "xc"
        assert site_key("https://img.xchina.io/a.jpg") == "xc"
        assert site_key("https://other.com/a.jpg") == "other.com"
    finally:
        settings.site_groups = old


def test_min_interval_enforced():
    lim = DomainLimiter(max_concurrent=5, min_interval=0.15)
    start = time.monotonic()
    for _ in range(3):
        with lim.slot():
            pass
    elapsed = time.monotonic() - start
    # 3 次请求至少间隔 2 * 0.15s (留少量调度误差)
    assert elapsed >= 0.28, elapsed


def test_concurrency_cap_serializes():
    lim = DomainLimiter(max_concurrent=1, min_interval=0)
    import threading

    active = [0]
    peak = [0]
    lock = threading.Lock()

    def work():
        with lim.slot():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


# ---- 令牌桶: 突发容量 ----

def test_burst_capacity_allows_a_short_cluster():
    """capacity>1 时, 攒下来的配额可以一次花掉 —— 这是"同样的礼貌、更少的干等"。"""
    lim = DomainLimiter(max_concurrent=8, min_interval=0.2, capacity=3)
    start = time.monotonic()
    for _ in range(3):
        with lim.slot():
            pass
    # 3 个令牌是初始就有的, 不该等
    assert time.monotonic() - start < 0.15


def test_burst_does_not_change_long_run_rate():
    """⚠️ 关键: 突发**不改**长程平均速率, 它花的只是攒下来的配额。

    若这条不成立, 令牌桶就退化成"变相提速", 礼貌性就没了。
    """
    lim = DomainLimiter(max_concurrent=8, min_interval=0.1, capacity=3)
    n = 6
    start = time.monotonic()
    for _ in range(n):
        with lim.slot():
            pass
    elapsed = time.monotonic() - start
    # 首 3 个是攒的(≈0), 之后每 0.1s 一个 -> 约 (n-capacity) * 0.1
    assert elapsed >= (n - 3) * 0.1 * 0.8, elapsed


def test_default_capacity_is_one_so_nothing_bursts_by_accident():
    """直接构造的桶不许自作主张突发: 突发是**站点级**配置, 只由 _limiter 装配。"""
    lim = DomainLimiter(max_concurrent=8, min_interval=0.2)
    start = time.monotonic()
    for _ in range(3):
        with lim.slot():
            pass
    assert time.monotonic() - start >= 0.35


# ---- AIMD ----

def test_bad_widens_interval_immediately():
    """被限了一次就立即放宽; 且"增"的进度清零(不允许刚出错就继续收紧)。"""
    lim = DomainLimiter(max_concurrent=2, min_interval=0.5,
                        fast_interval=0.1, slow_interval=5.0)
    lim.note_ok()
    before = lim.interval
    lim.note_bad()
    assert lim.interval > before


def test_enough_successes_tighten_back_toward_the_floor():
    """只有"减"没有"增"的话, 站点被限过一次就永久卡在最慢档。

    这条守的是"增"那一半 —— 它是本次改造补上的缺口。
    """
    lim = DomainLimiter(max_concurrent=2, min_interval=4.0,
                        fast_interval=0.5, slow_interval=8.0)
    for _ in range(200):
        lim.note_ok()
    assert lim.interval <= 0.6, lim.interval


def test_tightening_never_goes_below_the_floor():
    """地板是硬约束: 提速省下的时间远不够赔一次封禁。"""
    lim = DomainLimiter(max_concurrent=2, min_interval=1.0,
                        fast_interval=0.4, slow_interval=5.0)
    for _ in range(500):
        lim.note_ok()
    assert lim.interval >= 0.4 - 1e-9


def test_failure_resets_the_success_streak():
    """一次失败清零计数: 否则"成功若干次 + 失败"仍会被判成可以提速。"""
    lim = DomainLimiter(max_concurrent=2, min_interval=1.0,
                        fast_interval=0.2, slow_interval=5.0)
    for _ in range(7):          # 差一个就到收紧档
        lim.note_ok()
    lim.note_bad()
    after_bad = lim.interval
    lim.note_ok()               # 只成功一次, 不该收紧
    assert lim.interval >= after_bad


def test_describe_is_reentrant_safe_and_mentions_rate():
    """describe() 曾因持锁再取同一把普通锁**当场死锁**(整个进程卡住)。

    这类 bug 从堆栈看不出所以然, 必须有一条用例直接调它。
    """
    lim = DomainLimiter(max_concurrent=3, min_interval=0.5, capacity=2,
                        fast_interval=0.1, slow_interval=5.0)
    msg = lim.describe()
    assert "req/s" in msg and "0.50s" in msg
    # 也顺便确保 interval / rate 两个属性各自可用(它们同样取锁)
    assert abs(lim.rate - 2.0) < 1e-6
    assert lim.interval == 0.5


def test_rate_is_infinite_when_interval_is_zero():
    lim = DomainLimiter(max_concurrent=2, min_interval=0)
    assert lim.rate == float("inf")
    # 零间隔不能把桶卡住(曾经会因 wait<=0 直接 return 而漏发令牌)
    for _ in range(5):
        with lim.slot():
            pass
