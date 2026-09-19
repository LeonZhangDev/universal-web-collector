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


# ---- 站点级冷却("惩罚箱") ----
# 站点回 429 是在说"你太快了"。这时候**只让撞墙的那个线程退避是不够的**:
# 其他线程还在按原节奏猛冲, 站点看到的整体压力没变, 判断就不会变 —— 只会更快
# 把整站封掉。所以冷却按 site_key 分桶, 与限速器共用分组: 一个线程撞墙, 全站
# 一起安静下来。
_COOLDOWN = {}
_COOLDOWN_LOCK = threading.Lock()

#: 站点没给 Retry-After 时的保守等待。15 秒足够让绝大多数限速窗口滑过去,
#: 又不会让用户觉得"卡死了"。
DEFAULT_COOLDOWN = 15.0

#: 单次冷却上限。站点偶尔会给一个离谱的 Retry-After(实测见过 86400),
#: 照单全收等于把这个站点永久拉黑 —— 宁可到期后重试一次再看它的脸色。
COOLDOWN_CAP = 3600.0

#: 冷却等待的分片长度: 醒来重新看一眼截止时间, 而不是一口气睡到底。
#: 这样多线程不会在同一毫秒集体醒来(惊群), 用户中途清掉冷却也能立刻恢复。
_COOLDOWN_POLL = 5.0


def note_rate_limited(url, seconds=None):
    """记下"这个站点要求冷静 N 秒", 让同站所有请求一起等。返回实际采用的秒数。"""
    key = site_key(url)
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        secs = 0.0
    if secs <= 0:
        secs = DEFAULT_COOLDOWN
    secs = min(secs, COOLDOWN_CAP)
    until = time.monotonic() + secs
    with _COOLDOWN_LOCK:
        # 只延长不缩短: 两个线程同时撞墙时, 后到的长冷却不该被短的覆盖掉
        if until > _COOLDOWN.get(key, 0.0):
            _COOLDOWN[key] = until
    return secs


def cooldown_left(url):
    """该站点还需要安静多少秒(0 = 没在冷却)。"""
    key = site_key(url)
    with _COOLDOWN_LOCK:
        until = _COOLDOWN.get(key, 0.0)
    left = until - time.monotonic()
    return left if left > 0 else 0.0


def clear_cooldown(domain=None):
    """解除冷却(用户手动重试/测试用)。domain 传 site_key(如 xchina.io); 不传则全清。"""
    with _COOLDOWN_LOCK:
        if domain:
            _COOLDOWN.pop(domain, None)
        else:
            _COOLDOWN.clear()


def _await_cooldown(url, log=None):
    """等到该站点的冷却结束。多线程会各自重新检查截止时间, 不会集体惊醒。"""
    slept = 0.0
    while True:
        left = cooldown_left(url)
        if left <= 0:
            break
        nap = min(left, _COOLDOWN_POLL) * random.uniform(0.9, 1.1)
        if log and slept <= 0:
            log(f"站点 {site_key(url)} 被限速冷却中, 等待约 {left:.0f}s 后继续")
        time.sleep(nap)
        slept += nap
    if log and slept > 0:
        log(f"冷却结束(等了 {slept:.0f}s), 恢复请求")
    return slept


def domain_slot(url, log=None):
    """用法: with domain_slot(url): requests.get(...)

    函数名保留是为兼容既有调用方; 实际按 site_key(url) 分组,
    让同一站点的不同子域共享一个限速器, 站点级节奏才真正受控。

    进闸门前先等站点冷却结束 —— 冷却中的站点上再快的并发也只是在踩雷。
    """
    if cooldown_left(url) > 0:
        _await_cooldown(url, log=log)
    return _limiter(site_key(url)).slot()
