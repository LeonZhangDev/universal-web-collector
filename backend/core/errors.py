"""异常分类。

⚠️ 为什么要分类: 之前所有失败都是裸 `Exception`, 于是这三种完全不同性质的事
长得一模一样 ——

    1. 站点挂了 / URL 不对      (用户能在下一次重试中自行恢复)
    2. 磁盘满了 / 没有写权限    (环境问题, 继续跑只会空转)
    3. 我们自己写错了           (bug, 必须留堆栈才能查)

调用方只能靠 `str(e)` 去猜, 而**猜测往往发生在 try 块里**, 于是一错再错 ——
典型事故: `sqlite3.Row.get()` 抛的 AttributeError 落在业务 try 中, 被当成
"这个资源下载失败", 最后报成 `failed == filtered`。分类之后每种错有确定的去处。
"""


class CollectorError(Exception):
    """外部世界的问题: 站点、URL、输入、环境。

    消息是**写给用户看的**, 所以必须回答"接下来怎么办"。
    这类错不打堆栈 —— 堆栈对用户没有意义, 只会把真正的原因挤到屏幕外。
    """


class TransientError(Exception):
    """可以重试的失败(连接中断、超时、5xx)。

    与 CollectorError 的区别在于**谁负责**: 这个交给重试逻辑,
    重试耗尽后才升级成 CollectorError 交给用户。
    """


class DiskFullError(CollectorError):
    """磁盘空间不足(或不可写)。

    ⚠️ 必须**中止整个任务**而不是跳过当前资源: 磁盘满了之后, 剩下的每个资源
    都会各自走完一整条重试链才失败 —— 几百个资源就是长时间空转, 而用户看到的是
    一堆毫无意义的 "No space left on device"。
    """


def is_retryable(exc):
    """这个异常值得重试吗(用于把裸异常升级成 TransientError)。"""
    if isinstance(exc, (CollectorError,)):
        # 用户/环境的问题, 重试多少次都一样
        return False
    if isinstance(exc, TransientError):
        return True
    name = type(exc).__name__
    return name in ("ConnectionError", "Timeout", "ReadTimeout", "ChunkedEncodingError")


def describe(exc):
    """给用户看的一句话: 分类异常自带人话, 其它异常补上类型名。

    ⚠️ 不要把 `str(e)` 原样丢给界面: 底层异常的消息常常是 "No space left on
    device" 这类只对开发者有意义的话, 用户需要的下一步动作一个字都没有。
    """
    if isinstance(exc, CollectorError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
