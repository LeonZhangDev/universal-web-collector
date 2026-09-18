"""站点级限速: 并发上限 + 请求间隔(支持随机区间), 降低封禁风险。

为什么按「站点」而不是「主机名」分桶
------------------------------------
同一站点的资源常分散在多个子域(img./cdn./static.), 若按 netloc 分桶, 每个子域
各持一个限速器, 站点整体节奏就不受控了, 限速等于失效。
`site_key()` 把子域归并到注册域(img.xchina.io / cdn.xchina.io -> xchina.io),
也可以用 config 的 `site_groups` 显式指定分组。
"""
import random
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from core.config import settings

# 常见的二级后缀: 形如 example.co.uk 的注册域要取三段
_SECOND_LEVEL = {"com", "net", "org", "gov", "edu", "co", "ac", "mil", "biz", "info"}


class DomainLimiter:
    def __init__(self, max_concurrent, min_interval, max_interval=None):
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._ilock = threading.Lock()
        self._last = 0.0
        self._min = min_interval
        # max > min 时每次等待在区间内随机取值, 避免固定节奏被识别为机器
        self._max = max_interval if (max_interval and max_interval > min_interval) else None

    def _next_gap(self):
        if self._max:
            return random.uniform(self._min, self._max)
        return self._min

    @contextmanager
    def slot(self):
        self._sem.acquire()
        try:
            with self._ilock:
                delay = self._last + self._next_gap() - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._last = time.monotonic()
            yield
        finally:
            self._sem.release()


def set_interval(min_interval, max_interval=None, domain=None):
    """运行时调整某域名(或全局默认)的请求间隔。"""
    lim = _limiters.get(domain) if domain else None
    if lim is None:
        with _lock:
            lim = _limiters.setdefault(
                domain or "__default__",
                DomainLimiter(settings.domain_concurrency, min_interval, max_interval),
            )
    with lim._ilock:
        lim._min = min_interval
        lim._max = max_interval if (max_interval and max_interval > min_interval) else None
    return lim


_limiters = {}
_lock = threading.Lock()


def _limiter(domain):
    with _lock:
        lim = _limiters.get(domain)
        if lim is None:
            lim = DomainLimiter(
                settings.domain_concurrency,
                settings.domain_min_interval,
                settings.domain_max_interval,
            )
            _limiters[domain] = lim
        return lim


def site_key(url):
    """把 URL 归到「站点」维度。

    优先用 config 里 site_groups 的显式分组, 否则按注册域推断::

        img.xchina.io    -> xchina.io
        cdn.xchina.io    -> xchina.io   (与上面共用同一个限速器)
        a.example.co.uk  -> example.co.uk
        127.0.0.1        -> 127.0.0.1
    """
    host = (urlparse(url).netloc or "").split("@")[-1].split(":")[0].lower()
    if not host:
        return "__default__"

    for key, domains in (settings.site_groups or {}).items():
        for d in domains or []:
            d = str(d).lower()
            if host == d or host.endswith("." + d):
                return key

    if re.fullmatch(r"[\d.]+", host):     # IPv4 原样返回
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if parts[-2] in _SECOND_LEVEL:        # 形如 example.co.uk
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_slot(url):
    """用法: with domain_slot(url): requests.get(...)

    函数名保留是为兼容既有调用方; 实际按 site_key(url) 分组,
    让同一站点的不同子域共享一个限速器, 站点级节奏才真正受控。
    """
    return _limiter(site_key(url)).slot()
