"""`core.transport` 的自检 —— 以及它顺手抓出来的那个"分型静默判错"。

这个文件为什么存在
==================
本项目有两种 HTTP 传输: `requests`(默认 TLS 指纹) 与 `curl_cffi`(浏览器指纹,
被 CF 挡住的站点必须用它)。两者 API 像但不等价, 差异是:

    with session.get(url, stream=True) as resp:    # requests 可以; curl-cffi: TypeError

`curl_cffi.requests.Response` **不支持上下文管理器协议**。而这个写法在本仓库里被抄了
三遍(`gallery_base._head_status_headers` / `probe_site.sniff` / `probe_site --smoke`),
每一处都包着 `except Exception`, 于是它不报错 —— 只是**静默地什么都拿不到**。

真实后果(2026-09-24 目视探针输出时发现): 站点的 403 正文取不到 -> `diagnose_block`
拿不到 CF 挑战页的标记 -> 把 **CF 挑战判成 "blocked / 看不出具体成因"**, 于是用户
最有用的那条线索("要去开真浏览器")被丢掉, 而输出看起来仍然"完成了"。

这既不是崩溃也不是文案问题, 是**结论错了** —— 与本项目头号教训同源(静默的错会在
真实使用中活很久)。所以这里要有一条**跨模块的回归用例**把它钉住, 而不只是测 `streamed`
本身能用。
"""

import http.server
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from core import transport  # noqa: E402

import probe_site  # noqa: E402
from collectors.gallery_base import _head_status_headers  # noqa: E402


def _has_curl_cffi():
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


needs_curl_cffi = pytest.mark.skipif(not _has_curl_cffi(),
                                    reason="[deps:curl-cffi] 没装 —— 这条恰恰只在装了它才有效")

#: 一个"只回 CF 挑战页"的假站点。正文就是判据 —— `diagnose_block` 靠它分型。
CF_PAGE = (b"<html><head><title>Just a moment...</title></head>"
           b"<body><div id=\"challenge-platform\"></div></body></html>")


class _CfSite(http.server.BaseHTTPRequestHandler):
    """对任何请求都回 403 + CF 挑战页正文(HEAD 也一样)。"""

    def log_message(self, *a):
        pass

    def _go(self, body):
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(CF_PAGE)))
        self.end_headers()
        if body:
            self.wfile.write(CF_PAGE)

    def do_HEAD(self):
        self._go(False)

    def do_GET(self):
        self._go(True)


@pytest.fixture
def cf_site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CfSite)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/photos/aaa/0001.jpg" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


# ------------------------------------------------------------ streamed 本身


def test_streamed_works_on_the_plain_transport(cf_site):
    session, label = transport.build()
    assert "requests" in label
    with transport.streamed(session, "get", cf_site, stream=True, timeout=10) as resp:
        assert resp.status_code == 403
        assert len(resp.content) == len(CF_PAGE)
    session.close()


@needs_curl_cffi
def test_streamed_works_on_the_browser_transport(cf_site):
    """同一次调用在 curl-cffi 下也成立 —— 这才是这个模块存在的理由。

    直接写 `with session.get(...)` 在这条传输上会抛
    `TypeError: 'Response' object does not support the context manager protocol`。
    """
    session, label = transport.build(None, "chrome")
    assert "curl-cffi" in label
    with transport.streamed(session, "get", cf_site, stream=True, timeout=10) as resp:
        assert resp.status_code == 403
    session.close()


def test_build_falls_back_and_says_so_when_curl_cffi_is_unavailable(monkeypatch):
    """`--impersonate` 但没装 curl-cffi -> 降级 requests, 而且**必须说出来**。

    静默降级在这里特别坏: 用户会以为"指纹这条路也试过了", 从而排除了一个其实
    根本没试过的可能。
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("curl_cffi"):
            raise ImportError("no curl_cffi")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    session, label = transport.build(None, "chrome")
    assert "curl-cffi 不可用" in label
    assert "chrome" in label                    # 说清"哪个指纹没用上"
    session.close()


def test_streamed_does_not_mask_an_exception_from_the_body(cf_site):
    """`with` 体里抛的异常必须原样传出去, 不能被 `finally` 里的 close 盖掉。"""
    session, _label = transport.build()
    with pytest.raises(ValueError, match="boom"):
        with transport.streamed(session, "get", cf_site, stream=True, timeout=10):
            raise ValueError("boom")
    session.close()


# ------------------------------------------------- 跨模块的回归(真正的那个 bug)


@needs_curl_cffi
def test_head_fallback_still_works_on_the_browser_transport():
    """`_head_status_headers` 的流式回退在 curl-cffi 下**不能再抛 TypeError**。

    这条回退只在"HEAD 返回 200 却不给 Content-Type"时走 —— 而那恰好是 Cloudflare
    后面的图床的常见行为, 也就是**最可能需要浏览器指纹**的那种站点。写错的话这个
    回退等于从来没生效过, 且失败会被 `probe()` 吞成 PROBE_ERROR(重试耗尽 -> 任务失败,
    却看不出是回退坏掉)。
    """
    class _HeadNoCtype(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):                      # 200 但**不给** Content-Type
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):                       # 回退请求能看见 Content-Type
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HeadNoCtype)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/x.jpg" % srv.server_address[1]
        session, _label = transport.build(None, "chrome")
        status, headers = _head_status_headers(session, url, {}, 10)
        assert status == 200
        assert (headers.get("Content-Type") or "").startswith("image/")
        session.close()
    finally:
        srv.shutdown()
        srv.server_close()


@needs_curl_cffi
def test_a_cf_challenge_is_still_classified_as_cf_on_the_browser_transport(cf_site):
    """**这条是整件事的重点。**

    `sniff()` 在 curl-cffi 下曾经拿不到正文(被 `except` 吞成 `b""`), 于是
    `diagnose_block` 把 CF 挑战判成 `blocked`(看不出成因)—— 而 CF 与 blocked 的
    首要建议不同: 前者是"换指纹/走真浏览器", 后者是"按成本从低到高都试试"。
    用户按后者试, 就会一直在指纹那条路上打转。

    判据读的是 `kind` 代号, 不是文案 —— 这个 bug 正是"结论错了但输出看着正常",
    用文案去断言很可能就放过去了。
    """
    session, _label = transport.build(None, "chrome")
    status, headers, body = probe_site.sniff(session, cf_site, {}, 10)
    session.close()
    assert status == 403, "正文都没取到 —— 分型只能靠猜"
    assert body, "正文是空的 —— CF 挑战页的标记全丢了"
    kind, step, _why, _fix = probe_site.diagnose_block(
        status, headers.get("Content-Type") or "", headers, body)
    assert (kind, step) == ("cf_challenge", "impersonate"), (kind, step, body[:120])


def test_no_module_still_wraps_a_session_response_in_a_context_manager():
    """**规则门禁**(第 ⑦ 条心法): 同一个错出现第三遍就抽成可执行的判据。

    `curl_cffi` 的响应不支持 `with`, 而 `requests` 的支持 —— 所以这个错只会在
    "用了浏览器指纹"那条路上出现, 静态看一眼是看不出来的。这条用例直接把源码里
    `with <某对象>.get/head/post(...)` 这个**语法结构**找出来, 免得哪天又被"顺手写上"。

    ⚠️ 用 AST 而不是正则扫文本: 正则会把注释和文档字符串里那些"反面教材"也当成
    违规(这个模块自己的文档里就写着那个错误写法), 于是门禁第一天就红, 然后被人
    加豁免加到失效 —— 那就是一条自己坏掉的护栏。判据要落在**语法结构**上。
    """
    import ast

    bad_methods = {"get", "head", "post", "put", "request", "stream"}
    offenders = []
    for p in (list((ROOT / "scripts").glob("*.py"))
              + list((ROOT / "backend").rglob("*.py"))):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                if isinstance(fn, ast.Attribute) and fn.attr in bad_methods:
                    offenders.append("%s:%d  with %s.%s(...)"
                                     % (p.relative_to(ROOT), node.lineno,
                                        ast.unparse(fn.value), fn.attr))
    assert not offenders, (
        "这些地方把响应对象当上下文管理器用了 —— 在 curl-cffi 传输下会抛 "
        "`TypeError` 并被 except 吞成「没有正文」。改用 `core.transport.streamed(...)`: \n  "
        + "\n  ".join(offenders))
