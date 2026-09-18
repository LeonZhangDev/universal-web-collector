"""线程安全的 SSE 事件总线: worker 线程 publish, /events 订阅者各持一个队列。"""
import queue
import threading

_lock = threading.Lock()
_subs = []


def subscribe():
    q = queue.Queue(maxsize=1000)
    with _lock:
        _subs.append(q)
    return q


def unsubscribe(q):
    with _lock:
        try:
            _subs.remove(q)
        except ValueError:
            pass


def publish(name, data):
    with _lock:
        subs = list(_subs)
    for q in subs:
        try:
            q.put_nowait((name, data))
        except queue.Full:
            pass  # 慢消费者丢消息, 前端靠整行刷新兜底
