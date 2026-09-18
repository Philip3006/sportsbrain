"""Sanitized operator-facing queue status helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from .models import TaskRecord, TaskState


def process_alive(process_id: int | None) -> bool:
    if not isinstance(process_id, int) or process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def task_summary(record: TaskRecord) -> dict[str, Any]:
    """Return task identity/evidence only; never include payload or credentials."""

    return {
        "task_id": record.task_id,
        "builder_id": record.builder_id,
        "state": record.state.value,
        "short_task": record.branch.rstrip("/").rsplit("/", 1)[-1]
        or record.task_id[-16:],
        "branch": record.branch,
        "failure_class": record.failure_class,
        "last_error": record.last_error[:240] if record.last_error else None,
        "process_id": record.process_id,
        "available_at": record.available_at,
        "commit_sha": record.commit_sha,
        "remote_sha": record.remote_sha,
        "pr_number": record.pr_number,
        "pr_url": record.pr_url,
        "roadmap_item_id": record.roadmap_item_id,
    }


def operator_snapshot(
    records: Sequence[TaskRecord],
    roadmap: Sequence[Mapping[str, Any]],
    *,
    merge_backpressure: bool,
) -> dict[str, Any]:
    """Build the compact state categories used by status and doctor output."""

    running = [
        task_summary(record)
        for record in records
        if record.state in {TaskState.CLAIMED, TaskState.RUNNING, TaskState.VERIFYING}
    ]
    dead_pid = [
        task_summary(record)
        for record in records
        if record.state in {TaskState.CLAIMED, TaskState.RUNNING, TaskState.VERIFYING}
        and isinstance(record.process_id, int)
        and not process_alive(record.process_id)
    ]
    parked_timeout = [
        task_summary(record)
        for record in records
        if record.state is TaskState.FAILED_SAFE
        and (
            "TIMEOUT" in (record.failure_class or "").upper()
            or "DEAD_LETTER" in (record.failure_class or "").upper()
            or "timed out" in (record.last_error or "").lower()
        )
    ]
    delivery_blocked = [
        task_summary(record)
        for record in records
        if record.delivery_blocked
    ]
    paused_quota = [
        task_summary(record)
        for record in records
        if record.state is TaskState.PAUSED_QUOTA
    ]
    ceo_review = [
        task_summary(record) for record in records if record.state is TaskState.CEO_REVIEW
    ]
    next_item = _next_roadmap_item(roadmap)
    return {
        "running": running,
        "parked_timeout": parked_timeout,
        "delivery_blocked": delivery_blocked,
        "paused_quota": paused_quota,
        "awaiting_ceo_review": ceo_review,
        "dead_pid": dead_pid,
        "merge_backpressure": merge_backpressure,
        "intentional_idle": (
            not running
            and not paused_quota
            and not next_item
            and not merge_backpressure
        ),
        "next_eligible_explicit_task": next_item,
    }


def _next_roadmap_item(
    roadmap: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    completed = {
        item.get("item_id")
        for item in roadmap
        if item.get("status") == "COMPLETED"
    }
    eligible = []
    for item in roadmap:
        if item.get("status") not in {"PENDING", "ENQUEUED"}:
            continue
        dependencies = item.get("dependency_item_ids", [])
        if any(dependency not in completed for dependency in dependencies):
            continue
        eligible.append(item)
    if not eligible:
        return None
    item = min(
        eligible,
        key=lambda value: (
            -int(value.get("priority", 0)),
            str(value.get("item_id", "")),
        ),
    )
    return {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "builder_id": item.get("builder_id"),
        "template_id": item.get("template_id"),
        "status": item.get("status"),
    }


__all__ = ["operator_snapshot", "process_alive", "task_summary"]
