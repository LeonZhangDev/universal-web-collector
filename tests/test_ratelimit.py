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
