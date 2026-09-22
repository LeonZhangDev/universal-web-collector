from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.tasks as tasks_api
import core.database as db


def test_task_detail_includes_resource_counts():
    task_id = db.create_task("https://example.com/gallery", "generic")
    for status in ("done", "failed", "filtered", "pending", "skipped"):
        db.add_resource(
            task_id,
            "image",
            f"https://example.com/{status}.jpg",
            status=status,
        )

    app = FastAPI()
    app.include_router(tasks_api.router)
    response = TestClient(app).get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert response.json()["resource_counts"] == {
        "total": 5,
        "done": 1,
        "failed": 1,
        "filtered": 1,
        # gone 单独给一份, 但它**已经算进 failed** —— 见下面那条测试
        "gone": 0,
    }


def test_task_detail_counts_gone_resources_as_failed():
    """`gone`(源站已无)的**口径**: 单独可见, 但计入 failed。

    ⚠️ 两件事都要成立, 缺一个都会出问题:
      * 单独可见 —— 界面才说得出"其中 N 张是源站已经删了, 不用重试";
      * 计入 failed —— 否则"要 56 张只拿到 0 张"会因为 failed=0/done=0 被判成
        success(见 task_manager._final_status), 那是最糟的一类静默错误。
    """
    task_id = db.create_task("https://example.com/gone", "generic")
    db.add_resource(task_id, "image", "https://example.com/a.jpg", status="gone")
    db.add_resource(task_id, "image", "https://example.com/b.jpg", status="gone")
    db.add_resource(task_id, "image", "https://example.com/c.jpg", status="done")

    app = FastAPI()
    app.include_router(tasks_api.router)
    counts = TestClient(app).get(f"/tasks/{task_id}").json()["resource_counts"]

    assert counts["gone"] == 2
    assert counts["failed"] == 2      # gone 已并入 failed
    assert counts["done"] == 1


def test_task_detail_resource_counts_are_zero_without_resources():
    task_id = db.create_task("https://example.com/empty", "generic")

    app = FastAPI()
    app.include_router(tasks_api.router)
    response = TestClient(app).get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert response.json()["resource_counts"] == {
        "total": 0,
        "done": 0,
        "failed": 0,
        "filtered": 0,
        "gone": 0,
    }
