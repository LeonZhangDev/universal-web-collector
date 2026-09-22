import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import api.tasks as tasks_api
import core.database as db
from models.schemas import TaskCreateIn


URL = "https://xchina.co/photo/id-6664761937f5a.html"
COLLECTOR = "xchina_gallery"
CONTENT_KEY = "xchina_gallery:6664761937f5a"


@pytest.fixture
def quiet_submit(monkeypatch):
    submitted = []
    monkeypatch.setattr(tasks_api.task_manager, "submit", submitted.append)
    return submitted


def _create(**overrides):
    payload = {
        "url": URL,
        "collector": COLLECTOR,
        "deduplicate": True,
    }
    payload.update(overrides)
    return tasks_api.create(TaskCreateIn(**payload))


def _existing(status):
    task_id = db.create_task(
        URL,
        COLLECTOR,
        options={"content_key": CONTENT_KEY},
    )
    db.update_task_status(task_id, status)
    return task_id


def test_first_deduplicated_create_is_created_and_stores_identity(quiet_submit):
    result = _create(incremental=True)

    assert result.disposition == "created"
    assert result.content_key == CONTENT_KEY
    assert quiet_submit == [result.task_id]
    options = json.loads(db.get_task(result.task_id)["options"])
    assert options["content_key"] == CONTENT_KEY
    assert options["incremental"] is True


def test_caller_cannot_spoof_reserved_content_key_through_filters(quiet_submit):
    spoof = tasks_api.create(
        TaskCreateIn(
            url="https://example.com/unrelated",
            collector="generic",
            filters={"content_key": CONTENT_KEY},
        )
    )

    spoof_options = json.loads(db.get_task(spoof.task_id)["options"])
    assert "content_key" not in spoof_options

    legitimate = _create()
    assert legitimate.disposition == "created"
    assert legitimate.task_id != spoof.task_id


@pytest.mark.parametrize("status", ["pending", "running", "extracting", "downloading"])
def test_active_duplicate_is_reused(status, quiet_submit):
    task_id = _existing(status)

    result = _create()

    assert (result.task_id, result.status, result.disposition) == (
        task_id,
        status,
        "reused-active",
    )
    assert quiet_submit == []


def test_completed_duplicate_requires_redownload_confirmation(quiet_submit):
    task_id = _existing("success")

    result = _create()

    assert (result.task_id, result.status, result.disposition) == (
        task_id,
        "success",
        "confirm-redownload",
    )
    assert quiet_submit == []


@pytest.mark.parametrize("status", ["partial", "failed", "cancelled"])
def test_incomplete_duplicate_recommends_retry(status, quiet_submit):
    task_id = _existing(status)

    result = _create()

    assert (result.task_id, result.status, result.disposition) == (
        task_id,
        status,
        "recommend-retry",
    )
    assert quiet_submit == []


def test_paused_duplicate_recommends_resume(quiet_submit):
    task_id = _existing("paused")

    result = _create()

    assert (result.task_id, result.status, result.disposition) == (
        task_id,
        "paused",
        "recommend-resume",
    )
    assert quiet_submit == []


def test_force_new_bypasses_completed_duplicate(quiet_submit):
    old_id = _existing("success")

    result = _create(force_new=True)

    assert result.disposition == "created"
    assert result.task_id != old_id
    assert quiet_submit == [result.task_id]
    options = json.loads(db.get_task(result.task_id)["options"])
    assert options["incremental"] is True


@pytest.mark.parametrize(
    ("status", "disposition"),
    [
        ("pending", "reused-active"),
        ("running", "reused-active"),
        ("extracting", "reused-active"),
        ("downloading", "reused-active"),
        ("partial", "recommend-retry"),
        ("failed", "recommend-retry"),
        ("cancelled", "recommend-retry"),
        ("paused", "recommend-resume"),
    ],
)
def test_force_new_does_not_bypass_non_success_dispositions(
    status, disposition, quiet_submit
):
    task_id = _existing(status)

    result = _create(force_new=True)

    assert (result.task_id, result.status, result.disposition) == (
        task_id,
        status,
        disposition,
    )
    assert quiet_submit == []


def test_active_match_wins_over_newer_terminal_and_newest_active_wins(quiet_submit):
    _existing("pending")
    newest_active_id = _existing("running")
    _existing("success")

    result = _create()

    assert (result.task_id, result.status, result.disposition) == (
        newest_active_id,
        "running",
        "reused-active",
    )
    assert quiet_submit == []


def test_concurrent_deduplicated_creates_produce_one_task(
    quiet_submit, monkeypatch
):
    request_barrier = threading.Barrier(2)
    first_lookup_waiting = threading.Event()
    second_lookup_seen = threading.Event()
    first_lookup_released_by_second = []
    lookup_count = 0
    lookup_guard = threading.Lock()
    real_create_or_dispose = tasks_api.create_or_dispose
    real_find = db.find_tasks_by_content_key

    def synchronized_create_or_dispose(*args, **kwargs):
        request_barrier.wait(timeout=2)
        return real_create_or_dispose(*args, **kwargs)

    def instrumented_find(content_key):
        nonlocal lookup_count
        hits = real_find(content_key)
        with lookup_guard:
            lookup_count += 1
            call_number = lookup_count
        if call_number == 1:
            first_lookup_waiting.set()
            first_lookup_released_by_second.append(
                second_lookup_seen.wait(timeout=0.2)
            )
        else:
            second_lookup_seen.set()
        return hits

    monkeypatch.setattr(
        tasks_api, "create_or_dispose", synchronized_create_or_dispose
    )
    monkeypatch.setattr(db, "find_tasks_by_content_key", instrumented_find)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _create(), range(2)))

    assert first_lookup_waiting.is_set()
    assert first_lookup_released_by_second == [False]
    assert lookup_count == 2
    assert len({result.task_id for result in results}) == 1
    assert sorted(result.disposition for result in results) == [
        "created",
        "reused-active",
    ]
    assert len(db.list_tasks()) == 1
    assert len(quiet_submit) == 1


def test_submission_failure_marks_new_task_failed_and_reraises(monkeypatch):
    def fail_submit(task_id):
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(tasks_api.task_manager, "submit", fail_submit)

    with pytest.raises(RuntimeError, match="executor unavailable"):
        _create()

    [task] = db.list_tasks()
    assert task["status"] == "failed"
    assert "executor unavailable" in task["error"]

    monkeypatch.setattr(tasks_api.task_manager, "submit", lambda task_id: None)
    result = _create()
    assert (result.task_id, result.status, result.disposition) == (
        task["id"],
        "failed",
        "recommend-retry",
    )


def test_normal_create_remains_always_create(quiet_submit):
    first = _create(deduplicate=False)
    second = _create(deduplicate=False)

    assert first.task_id != second.task_id
    assert first.disposition == second.disposition == "created"
    assert quiet_submit == [first.task_id, second.task_id]
