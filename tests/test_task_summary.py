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
    }


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
    }
