"""Atomic, opt-in duplicate disposition for task creation."""

import threading
from dataclasses import dataclass

from core import database as db


ACTIVE_STATES = frozenset({"pending", "running", "extracting", "downloading"})
RETRY_STATES = frozenset({"partial", "failed", "cancelled"})

_create_lock = threading.Lock()


@dataclass(frozen=True)
class TaskDisposition:
    task_id: int
    status: str
    disposition: str
    created: bool


def _existing_disposition(status: str) -> str | None:
    if status in ACTIVE_STATES:
        return "reused-active"
    if status == "success":
        return "confirm-redownload"
    if status in RETRY_STATES:
        return "recommend-retry"
    if status == "paused":
        return "recommend-resume"
    return None


def create_or_dispose(
    url: str,
    collector: str,
    download_dir: str | None,
    options: dict,
    *,
    content_key: str | None,
    deduplicate: bool,
    force_new: bool,
) -> TaskDisposition:
    """Look up and create under one process lock.

    Existing tasks are never mutated or restarted here.  The caller decides how
    to present the returned recommendation and submits only newly created tasks.
    """
    with _create_lock:
        existing = None
        if deduplicate and content_key:
            matches = db.find_tasks_by_content_key(content_key)
            # Rows arrive newest-first.  Active work wins over every terminal
            # recommendation; otherwise the newest recognized terminal wins.
            existing = next(
                (task for task in matches if task["status"] in ACTIVE_STATES),
                None,
            )
            if existing is None:
                existing = next(
                    (
                        task
                        for task in matches
                        if _existing_disposition(task["status"]) is not None
                    ),
                    None,
                )
            if existing is not None:
                disposition = _existing_disposition(existing["status"])
                confirmed_redownload = (
                    force_new and disposition == "confirm-redownload"
                )
                if not confirmed_redownload:
                    return TaskDisposition(
                        task_id=existing["id"],
                        status=existing["status"],
                        disposition=disposition,
                        created=False,
                    )

        stored_options = dict(options)
        # Identity is internal metadata.  FilterIn intentionally accepts extra
        # keys, so caller input must never be allowed to supply this one.
        stored_options.pop("content_key", None)
        if content_key:
            stored_options["content_key"] = content_key
        if force_new and existing is not None and existing["status"] == "success":
            stored_options["incremental"] = True
        task_id = db.create_task(url, collector, download_dir, stored_options)
        return TaskDisposition(
            task_id=task_id,
            status="pending",
            disposition="created",
            created=True,
        )
