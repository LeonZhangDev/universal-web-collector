"""HTTP 传输层: 会话工厂 + **两种传输都支持的流式请求**。

为什么要有这个模块
==================
本项目用两种 HTTP 传输: `requests`(默认 TLS 指纹) 与 `curl_cffi`(浏览器指纹,
被 CF 挡住的站点必须用它)。两者 API 长得很像, 但**并不一样**, 差异藏在一个最容易
被写顺手的地方:

    with session.get(url, stream=True) as resp:      # requests: 可以
        ...                                          # curl-cffi: TypeError!

`curl_cffi.requests.Response` **不支持上下文管理器协议**。写成上面那样, 在
`requests` 下一切正常, 一旦换到 `--impersonate chrome` / `browser_impersonation`
就抛 `TypeError: 'Response' object does not support the context manager protocol`。

这个 bug 特别阴, 因为:

1. 它只在**最需要换指纹的场合**出现(站点在 CF 后面) —— 换了指纹反而什么都拿不到;
2. 调用点几乎都包着 `except Exception`, 于是它变成"没有正文 / 探测失败",
   而不是一条 traceback。2026-09-24 实测踩到: `probe_site` 的 403 分型拿到空正文,
   于是 **CF 挑战被报成"看不出具体成因"** —— 用户最有用的那条线索(要去开真浏览器)
   被静默丢掉, 而结论看起来仍然"完成了";
3. 同一个写法在本仓库里被复制了三遍(`gallery_base` / `probe_site` 两处)。

所以统一走 `streamed()`: 显式 `close()`, 两种传输都覆盖。
**加新调用点时不要再手写 `with session.xxx(...)`。**
"""

from __future__ import annotations

import contextlib

from core.config import settings


def build(proxy=None, impersonate=None):
    """建会话; 返回 `(session, 传输说明)`。

    ⚠️ `impersonate` 需要 `curl-cffi`, 而它可能**没装**(CI / 最小环境)。那是环境
    问题, 不该让整个流程挂掉: 降级成 `requests` 并**把降级写进说明里** —— 否则用户
    会以为"指纹这条路也试过了"(静默降级必须留痕)。
    """
    if impersonate:
        try:
            from curl_cffi import requests as curl_requests

            session = curl_requests.Session(impersonate=impersonate)
            label = "curl-cffi (impersonate=%s)" % impersonate
        except Exception as e:                 # 没装 / 版本不兼容
            import requests

            session = requests.Session()
            label = ("requests —— !! curl-cffi 不可用(%s), 没能用上 %s 指纹"
                     % (type(e).__name__, impersonate))
    else:
        import requests

        session = requests.Session()
        label = "requests (默认 TLS 指纹)"

    p = proxy if proxy is not None else settings.proxy
    if p:
        session.proxies.update({"http": p, "https": p})
    return session, label


@contextlib.contextmanager
def streamed(session, method, url, **kwargs):
    """`with streamed(session, "get", url, stream=True, ...) as resp:` —— 两种传输通用。

    与 `requests` 自带的 `with session.get(...)` 相比只有一处差别: **退出时显式
    `close()`**, 于是不依赖响应对象实现上下文管理器协议(curl-cffi 没实现)。

    `close()` 失败的处置有讲究: 不能吞(会漏连接, 本项目第 12 条的家族), 也不能
    在**正文已经出错**时抛(会盖住真正的异常)。所以只在 `with` 体正常跑完时才把
    close 的失败抛出去 —— 那时它是唯一的异常, 最有信息量。
    """
    resp = getattr(session, method)(url, **kwargs)
    body_ok = False
    try:
        yield resp
        body_ok = True
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                if body_ok:
                    raise
