"""Webhook 投递: 把事件推给用户自己配的外部地址。

用标准库而不引新依赖
====================
投递就是"发一个 POST", 不值得为此引入一个 HTTP 客户端依赖 —— 依赖越多,
"CI 少了哪个包导致整片 collection 红"的机会越多。所以这里用 `urllib` + `hmac`
(都是标准库), 与下载链路(`core/transport.py`)分开: 那条链路要的是流式、断点
续传、指纹伪装, 这里要的是短超时、可观测。

失败必须留痕(第 G 条)
======================
投递失败只写日志的话, 用户看到的是"我配好了、但一直没动静", 而日志他不会去
翻。所以每次投递的结果(HTTP 状态码 / 错误信息)都要回写库里的 `last_status`
/ `last_error`, 界面上能直接看到"上一次是 500 / 连不上"。

⚠️ 密钥: `secret` 存在库里, 只用于算 HMAC 签名, **不进任何返回值**
(`_webhook_row` 对外只给 `has_secret`)。本模块为了签名会直接读原文, 这是
唯一该读它的地方。
"""

import hashlib
import hmac
import http.client
import json
import time
import urllib.error
import urllib.request
from socket import timeout as SocketTimeout

from core import database as db

#: 单次投递的超时(秒)。webhook 是**通知**, 不是下载 —— 卡住 30 秒只会拖慢
#: 任务收尾, 而用户真正要的是"别挡着我"。失败会留痕, 重试交给外部系统。
DEFAULT_TIMEOUT = 3.0

_SIGNATURE_HEADER = "X-UWC-Signature"
_TIMESTAMP_HEADER = "X-UWC-Timestamp"


def sign(secret, body, ts=None):
    """HMAC-SHA256 签名。带时间戳是为了让接收方能拒绝重放的旧请求。"""
    stamp = str(int(ts if ts is not None else time.time()))
    mac = hmac.new((secret or "").encode("utf-8"),
                   stamp.encode("utf-8") + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}", stamp


def _post(url, body, secret, timeout):
    """发一次 POST, 返回 `(状态码, 错误信息)`。

    ⚠️ 只捕获**网络层**会抛的那些异常, 不写 `except Exception`: 那样会把
    本模块自己的 bug(比如拼错了字段名)也当成"网络失败"记进 `last_error`,
    于是报错信息永远指向"对端有问题", 而真正的错在我们这里(第 4 条同族)。
    """
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "universal-web-collector/webhook")
    if secret:
        signature, stamp = sign(secret, body)
        req.add_header(_SIGNATURE_HEADER, signature)
        req.add_header(_TIMESTAMP_HEADER, stamp)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是"投递了但对方没接受", 状态码要留下来给用户看
        return exc.code, f"HTTP {exc.code}"
    except (urllib.error.URLError, http.client.HTTPException,
            SocketTimeout, OSError) as exc:
        return None, str(exc)


def deliver(hid, url, secret, event, payload, timeout=DEFAULT_TIMEOUT):
    """投一次, 并**把结果写回库**。返回结果字典(供测试端点回显)。"""
    body = json.dumps({
        "event": event,
        "ts": time.time(),
        "data": payload or {},
    }, ensure_ascii=False).encode("utf-8")
    status, error = _post(url, body, secret, timeout)
    db.mark_webhook_result(hid, status, error)
    return {"id": hid, "url": url, "status": status, "error": error}


def fire_event(event, payload=None, timeout=DEFAULT_TIMEOUT):
    """把事件投给**所有订阅了它**的已启用 webhook。

    返回 `(成功数, 尝试数)`: 两个都要给(第 27 条) —— 只回成功数的话,
    "没有 webhook 配这个事件"和"配了但全投失败"看起来一样。

    ⚠️ 未知事件**直接报错**而不是静默什么都不做: 事件名写错是最常见的一类
    配置错误, 而它的表现恰好是"什么都没发生"。
    """
    if event not in db.WEBHOOK_EVENTS:
        raise ValueError(f"unknown webhook event: {event}")

    targets = [h for h in db.list_webhooks(enabled_only=True)
               if event in (h.get("events") or [])]
    if not targets:
        return 0, 0

    ok = 0
    results = []
    for hook in targets:
        # 签名要用**原文**, 而列表接口做了脱敏, 所以这里单独取一次
        row = db.query_one("SELECT secret FROM webhooks WHERE id=?", (hook["id"],))
        secret = row["secret"] if row else None
        res = deliver(hook["id"], hook["url"], secret, event, payload, timeout)
        results.append(res)
        if res["status"] is not None and 200 <= res["status"] < 300:
            ok += 1
    return ok, len(targets)
