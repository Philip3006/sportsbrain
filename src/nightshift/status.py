"""Sanitized operator-facing queue status helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from .backpressure import classify_pull_request, summarize_pull_requests
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
        "pr_classification": (
            classify_pull_request(record).category
            if classify_pull_request(record) is not None
            else None
        ),
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
    pr_counts: Mapping[str, int] | None = None,
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
    next_item = _next_roadmap_item(roadmap)
    eligible_items = _eligible_roadmap_items(roadmap)
    per_builder: dict[str, dict[str, Any]] = {}
    for builder_id in builder_ids:
        items = [item for item in roadmap if item.get("builder_id") == builder_id]
        next_for_builder = _next_roadmap_item(items)
        if paused:
            reason = "dispatcher_paused"
        elif draining:
            reason = "dispatcher_draining"
        elif next_for_builder is not None:
            reason = "eligible"
        elif any(item.get("skip_reason") for item in items):
            reason = "frontier_items_skipped"
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
    classifications = dict(pr_counts or summarize_pull_requests(records))
    active_builders = sorted(
        {
            record.builder_id
            for record in records
            if record.state
            in {
                TaskState.CLAIMED,
                TaskState.RUNNING,
                TaskState.VERIFYING,
                TaskState.DELIVERY_RECONCILING,
            }
        }
    )
    blocked_builders = sorted(
        {
            builder_id
            for builder_id in builder_ids
            if any(
                record.builder_id == builder_id
                and record.state in {TaskState.BLOCKED, TaskState.FAILED_SAFE}
                for record in records
            )
            and per_builder[builder_id]["next_eligible_task"] is None
        }
    )
    skipped_tasks = [
        {
            key: item.get(key)
            for key in (
                "item_id",
                "builder_id",
                "status",
                "skip_reason",
                "skip_signature",
                "skip_count",
            )
            if item.get(key) is not None
        }
        for item in roadmap
        if item.get("skip_reason")
    ]
    if paused:
        global_idle_reason = "dispatcher_paused"
    elif draining:
        global_idle_reason = "dispatcher_draining"
    elif next_item or running or eligible_items:
        global_idle_reason = None
    elif all(item.get("status") in {"COMPLETED", "DISABLED"} for item in roadmap):
        global_idle_reason = "roadmap_exhausted"
    else:
        global_idle_reason = "no_safe_eligible_roadmap_work"
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
        "backpressure_mode": "SOFT" if merge_backpressure else "NONE",
        "backpressure_is_hard": False,
        "active_substantive_pr_count": classifications.get(
            "active_substantive_pr_count", 0
        ),
        "pr_classifications": classifications,
        "active_builders": active_builders,
        "blocked_builders": blocked_builders,
        "skipped_tasks": skipped_tasks,
        "safe_eligible_roadmap_items": sum(
            1 for item in eligible_items if item.get("risk_class") == "read_only"
        ),
        "eligible_roadmap_items": len(eligible_items),
        "global_idle_reason": global_idle_reason,
        "continuous_mode": (
            "CONTINUOUS_AUTONOMOUS"
            if not paused and not draining and (running or next_item or eligible_items)
            else "GLOBAL_IDLE"
        ),
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
    del merge_backpressure  # retained for API compatibility; pressure is soft
    eligible = _eligible_roadmap_items(roadmap)
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


def _eligible_roadmap_items(
    roadmap: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    completed = {
        item.get("item_id")
        for item in roadmap
        if item.get("status") == "COMPLETED"
    }
    eligible: list[Mapping[str, Any]] = []
    for item in roadmap:
        if item.get("status") != "PENDING":
            continue
        dependencies = item.get("dependency_item_ids", [])
        if any(dependency not in completed for dependency in dependencies):
            continue
        eligible.append(item)
    return eligible


__all__ = ["operator_snapshot", "process_alive", "task_summary"]
