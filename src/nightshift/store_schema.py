"""SQLite schema and transition policy for the Night Shift store."""

from __future__ import annotations

from .models import TaskState

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING_APPROVAL: frozenset(
        {TaskState.QUEUED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.QUEUED: frozenset(
        {TaskState.LEASED, TaskState.CANCELLED, TaskState.BLOCKED}
    ),
    TaskState.LEASED: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.PR_READY,
            TaskState.CEO_REVIEW,
            TaskState.RETRY_WAIT,
            TaskState.FAILED,
            TaskState.FAILED_SAFE,
            TaskState.DEAD_LETTER,
            TaskState.CANCELLED,
        }
    ),
    TaskState.RETRY_WAIT: frozenset(
        {TaskState.QUEUED, TaskState.LEASED, TaskState.CANCELLED, TaskState.BLOCKED}
    ),
    TaskState.BLOCKED: frozenset({TaskState.QUEUED, TaskState.CANCELLED}),
    TaskState.SUCCEEDED: frozenset({TaskState.CEO_REVIEW}),
    TaskState.PR_READY: frozenset({TaskState.CEO_REVIEW}),
    TaskState.CEO_REVIEW: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.FAILED_SAFE: frozenset(),
    TaskState.DEAD_LETTER: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


DDL = """
CREATE TABLE IF NOT EXISTS dispatcher_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, builder_id TEXT NOT NULL,
    objective TEXT NOT NULL, branch TEXT NOT NULL, repo TEXT NOT NULL,
    task_type TEXT NOT NULL, template_id TEXT, payload_json TEXT NOT NULL,
    risk_class TEXT NOT NULL, requires_approval INTEGER NOT NULL, priority INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL, requested_by TEXT NOT NULL, parent_task_id TEXT,
    dependency_ids_json TEXT NOT NULL, allowed_paths_json TEXT NOT NULL DEFAULT '[]',
    prohibited_paths_json TEXT NOT NULL DEFAULT '[]', resource_locks_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, available_at TEXT NOT NULL,
    lease_owner TEXT, lease_expires_at TEXT, last_error TEXT, result_json TEXT,
    worktree_path TEXT, diagnostic_path TEXT, failure_class TEXT,
    CHECK (requires_approval IN (0, 1)), CHECK (attempt_count >= 0), CHECK (max_attempts >= 1)
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (state, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_builder ON tasks (builder_id, state);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event_type TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, from_state TEXT, to_state TEXT,
    details_json TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events (task_id, event_id);
"""
