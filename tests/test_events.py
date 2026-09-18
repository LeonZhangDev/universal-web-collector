from core import events


def test_pubsub_roundtrip():
    q = events.subscribe()
    try:
        events.publish("task.updated", {"id": 1, "status": "running"})
        name, data = q.get(timeout=1)
        assert name == "task.updated"
        assert data == {"id": 1, "status": "running"}
    finally:
        events.unsubscribe(q)


def test_unsubscribe_stops_delivery():
    q = events.subscribe()
    events.unsubscribe(q)
    events.publish("task.updated", {"id": 2})
    assert q.empty()


def test_slow_consumer_does_not_block():
    q = events.subscribe()
    try:
        for i in range(1500):  # 超过 maxsize=1000, 不应抛异常
            events.publish("task.log", {"i": i})
    finally:
        events.unsubscribe(q)
