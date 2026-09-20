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
    """站点级闸门: 令牌桶(节奏) + 信号量(并发) + AIMD 自适应。

    三个机制各管一件事, 互不替代:

    * **桶**: 补充速率 `rate = 1/interval`(个/秒) 与容量 `capacity`。
      ⚠️ `capacity=1` 会退化成"严格每 interval 秒一个请求" —— 长程平均速率被限死
      的同时**连一点突发都不允许**, 这正是改造前的行为(旧实现每请求在锁内按
      `_last + gap` 排队, 等价于容量为 1 的桶)。
      放大 `capacity` 允许短簇突发, 而**长程平均速率不变** —— 突发花的是之前攒
      下来的配额, 不是超支。所以它是"同样的礼貌、更少的干等", 不是提速手段。
    * **信号量**: 只限同时在途的请求数, 不影响发起节奏。
    * **AIMD**: 真正决定吞吐的是间隔本身, 所以让它自适应 ——
      加性增(连续成功缓慢收紧间隔)、乘性减(429/非 2xx 立即翻倍放宽)。

    ⚠️ **"增"这一半不能省**: 只有乘性减的话, 站点被限过一次就永久卡在最慢档,
    只能靠人工改配置恢复。

    ⚠️ **延迟不作为增速依据**。纯延迟驱动(Scrapy AutoThrottle 的套路)在两种场景
    会帮倒忙: CDN 边缘缓存亚毫秒返回 → 判定"服务器很闲"→ 疯狂加速; 429 也能毫秒
    级返回 → 同样被当成健康。图集站两个条件都常命中, 所以这里**只看成功与否**。
    """

    #: 连续多少次成功才收紧一档间隔。太小会在临界点来回抖动
    _AI_EVERY = 8

    def __init__(self, max_concurrent, min_interval, max_interval=None,
                 capacity=None, fast_interval=None, slow_interval=None):
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._concurrency = max_concurrent
        # RLock 是兜底: 本类多处"读一个属性"就顺带取一次锁, 普通 Lock 会让这类
        # 嵌套直接死锁。已就地算过的地方仍刻意不依赖它 —— 可重入掩盖问题, 不解决问题
        self._ilock = threading.RLock()

        self._min = min_interval          # 当前间隔(AIMD 会改它)
        self._start = min_interval        # 起始/配置值
        self._max = max_interval if (max_interval and max_interval > min_interval) else None
        self._fast = min_interval if fast_interval is None else fast_interval
        self._slow = slow_interval or max(min_interval * 10.0, 5.0)
        if self._slow < self._fast:
            self._slow = self._fast
        if self._fast > self._start:
            self._fast = self._start

        # 默认容量为 1 = 旧行为。突发是**站点级**配置, 直接构造的桶不该自作主张
        self._capacity = max(1, int(capacity)) if capacity else 1
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()

        self._ok = 0
        self._ai_step = max(0.01, (self._start - self._fast) / 20.0)

    # ---- 可观测: 看不见的阈值才会被反复误调 ----

    @property
    def interval(self):
        with self._ilock:
            return self._min

    @property
    def rate(self):
        i = self.interval
        return (1.0 / i) if i > 0 else float("inf")

    def describe(self):
        """一行摘要, 给任务日志用。

        ⚠️ 全程只读一次锁, 且**不碰 `self.rate` / `self.interval`**: 那两个属性也
        各自取锁, 而 `threading.Lock` 不可重入 —— 持锁再取同一把锁会当场死锁,
        表现为"调了一次日志就整个进程卡住", 极难从堆栈看出原因。
        """
        with self._ilock:
            i = self._min
            r = (1.0 / i) if i > 0 else float("inf")
            return (
                f"并发 {self._concurrency} / 间隔 {i:.2f}s"
                f"(自适应范围 {self._fast:.2f}~{self._slow:.2f}) / 突发 {self._capacity}"
                f" → 长程约 {r:.1f} req/s"
            )

    # ---- 令牌桶 ----

    def _wait_token(self):
        """等到拿到一个令牌。

        ⚠️ 锁内只算不睡: 拿着锁 sleep 会把所有并发线程一起串行掉, 那正是旧实现
        "并发调多大都没用"的根源。
        """
        while True:
            with self._ilock:
                now = time.monotonic()
                # ⚠️ 就地算, 不要调 self.rate / self.interval —— 那两个属性各取一次
                # 锁, 而这里已经持锁了。普通 Lock 不可重入, 会当场死锁(表现为"调用
                # 一次就整个进程卡住", 堆栈上看不出所以然)。
                rate = (1.0 / self._min) if self._min > 0 else float("inf")
                if rate == float("inf"):
                    self._tokens = float(self._capacity)
                else:
                    self._tokens = min(
                        float(self._capacity),
                        self._tokens + (now - self._updated) * rate,
                    )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / rate if rate != float("inf") else 0.0
                if self._max:
                    # 区间随机: 固定节奏容易被识别为机器
                    wait += random.uniform(0.0, max(0.0, self._max - self._min))
            if wait <= 0:
                return
            time.sleep(wait)

    @contextmanager
    def slot(self):
        self._sem.acquire()
        try:
            self._wait_token()
            yield
        finally:
            self._sem.release()

    # ---- AIMD ----

    def note_ok(self):
        """一次**真正的成功**(2xx 且内容合规): 攒够一批就收紧一档间隔。"""
        with self._ilock:
            self._ok += 1
            if self._ok < self._AI_EVERY:
                return
            self._ok = 0
            if self._min > self._fast:
                self._min = max(self._fast, self._min - self._ai_step)

    def note_bad(self):
        """一次失败/被限: 立即翻倍放宽, 并把增速计数清零。

        ⚠️ 非 200 绝不允许触发增速 —— 错误响应通常比正常响应返回得更快,
        拿它当"服务器很闲"的信号就会越错越快。
        """
        with self._ilock:
            self._ok = 0
            self._min = min(self._slow, self._min * 2.0)


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
    """站点限速器。突发容量与 AIMD 区间只在**这里**装配 —— 直接构造 DomainLimiter
    的调用方(如视频分片的独立闸门)拿到的仍是容量为 1 的保守桶, 不会被全局配置
    意外放开。"""
    with _lock:
        lim = _limiters.get(domain)
        if lim is None:
            lim = DomainLimiter(
                settings.domain_concurrency,
                settings.domain_min_interval,
                settings.domain_max_interval,
                capacity=settings.domain_burst,
                fast_interval=(
                    settings.domain_fast_interval
                    if settings.adaptive_throttle
                    else settings.domain_min_interval
                ),
                slow_interval=(
                    settings.domain_slow_interval
                    if settings.adaptive_throttle
                    else settings.domain_min_interval
                ),
            )
            _limiters[domain] = lim
        return lim


def note_success(url):
    """记一次成功, 供 AIMD 增速。失败路径走 `note_rate_limited` / `note_failure`。"""
    try:
        _limiter(site_key(url)).note_ok()
    except Exception:
        pass    # 节流统计不该有能力让请求失败


def note_failure(url):
    """记一次失败(非 2xx / 内容不合预期), 立即放宽节奏。"""
    try:
        _limiter(site_key(url)).note_bad()
    except Exception:
        pass


def describe(url):
    """当前站点节奏的一行摘要, 给日志用(让"上限 2 req/s"这种事看得见)。"""
    try:
        return _limiter(site_key(url)).describe()
    except Exception:
        return ""


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
    # 冷却只管"这一阵别来"; 间隔本身也要放宽, 否则冷却一过又按老节奏撞回去
    note_failure(url)
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
