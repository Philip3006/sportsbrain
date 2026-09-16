"""Conversions between durable task records and executor task specifications."""

from __future__ import annotations

from .models import TaskRecord, TaskSpec


def claimed_to_spec(record: TaskRecord) -> TaskSpec:
    """Return the immutable executor input represented by a claimed record."""

    return TaskSpec(
        task_id=record.task_id,
        builder_id=record.builder_id,
        objective=record.objective,
        branch=record.branch,
        repo=record.repo,
        task_type=record.task_type,
        template_id=record.template_id,
        payload=record.payload,
        risk_class=record.risk_class,
        requires_approval=record.requires_approval,
        priority=record.priority,
        max_attempts=record.max_attempts,
        parent_task_id=record.parent_task_id,
        dependency_ids=record.dependency_ids,
        requested_by=record.requested_by,
        allowed_paths=record.allowed_paths,
        prohibited_paths=record.prohibited_paths,
        resource_locks=record.resource_locks,
    )


def record_to_spec(record: TaskRecord) -> TaskSpec:
    """Alias used at post-execution scope verification boundaries."""

    return claimed_to_spec(record)
