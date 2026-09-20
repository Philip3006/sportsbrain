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
        "reconciliation_attempts": record.reconciliation_attempts,
        "reconciliation": {
            key: (record.reconciliation or {}).get(key)
            for key in (
                "classification",
                "original_base_sha",
                "authoritative_base_sha",
                "original_commit_sha",
                "recovery_commit_sha",
                "recovery_branch",
                "pr_number",
                "pr_url",
            )
            if (record.reconciliation or {}).get(key) is not None
        },
    }


def operator_snapshot(
    records: Sequence[TaskRecord],
    roadmap: Sequence[Mapping[str, Any]],
    *,
    merge_backpressure: bool,
    builders: Sequence[str] = (),
    paused: bool = False,
    draining: bool = False,
) -> dict[str, Any]:
    """Build the compact state categories used by status and doctor output."""

    running = [
        task_summary(record)
        for record in records
        if record.state
        in {
            TaskState.CLAIMED,
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.DELIVERY_RECONCILING,
        }
    ]
    dead_pid = [
        task_summary(record)
        for record in records
        if record.state
        in {
            TaskState.CLAIMED,
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.DELIVERY_RECONCILING,
        }
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
    delivery_reconciling = [
        task_summary(record)
        for record in records
        if record.state is TaskState.DELIVERY_RECONCILING
    ]
    delivery_recovered = [
        task_summary(record)
        for record in records
        if record.reconciliation and record.delivery and record.state is TaskState.PR_READY
    ]
    paused_quota = [
        task_summary(record)
        for record in records
        if record.state is TaskState.PAUSED_QUOTA
    ]
    ceo_review = [
        task_summary(record) for record in records if record.state is TaskState.CEO_REVIEW
    ]
    builder_ids = tuple(builders) or tuple(
        sorted({str(item.get("builder_id")) for item in roadmap})
    )
    next_item = _next_roadmap_item(
        roadmap, merge_backpressure=merge_backpressure
    )
    per_builder: dict[str, dict[str, Any]] = {}
    for builder_id in builder_ids:
        items = [item for item in roadmap if item.get("builder_id") == builder_id]
        next_for_builder = _next_roadmap_item(
            items, merge_backpressure=merge_backpressure
        )
        if paused:
            reason = "dispatcher_paused"
        elif draining:
            reason = "dispatcher_draining"
        elif next_for_builder is not None:
            reason = "eligible"
        elif any(
            item.get("merge_backpressure_blocked")
            and item.get("status") in {"PENDING", "ENQUEUED"}
            for item in items
        ):
            reason = "merge_backpressure"
        elif any(
            (item.get("blocked_reason") or "").startswith("dependency")
            for item in items
        ):
            reason = "dependency_blocked"
        elif any(item.get("status") == "BLOCKED" for item in items):
            reason = "blocked"
        elif any(item.get("status") == "ENQUEUED" for item in items):
            reason = "task_in_flight"
        else:
            reason = "roadmap_exhausted"
        per_builder[builder_id] = {
            "next_eligible_task": next_for_builder,
            "idle_reason": reason,
            "safe_read_only_work_available": bool(
                next_for_builder and next_for_builder.get("risk_class") == "read_only"
            ),
            "rolling_generation": (
                next_for_builder.get("generation") if next_for_builder else None
            ),
        }
    safe_read_only_available = any(
        value["safe_read_only_work_available"] for value in per_builder.values()
    )
    return {
        "running": running,
        "parked_timeout": parked_timeout,
        "delivery_blocked": delivery_blocked,
        "delivery_reconciling": delivery_reconciling,
        "delivery_recovered": delivery_recovered,
        "paused_quota": paused_quota,
        "awaiting_ceo_review": ceo_review,
        "dead_pid": dead_pid,
        "merge_backpressure": merge_backpressure,
        "safe_read_only_roadmap_available": safe_read_only_available,
        "builders": per_builder,
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
    *,
    merge_backpressure: bool = False,
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
        if merge_backpressure and item.get("merge_backpressure_blocked"):
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
        "risk_class": item.get("risk_class"),
        "generation": item.get("generation", 1),
    }


__all__ = ["operator_snapshot", "process_alive", "task_summary"]
