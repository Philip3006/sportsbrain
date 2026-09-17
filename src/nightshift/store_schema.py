"""SQLite schema and transition policy for the Night Shift store."""

from __future__ import annotations

from .models import TaskState

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.BACKLOG: frozenset(
        {TaskState.READY, TaskState.CANCELLED, TaskState.FAILED_SAFE}
    ),
    TaskState.READY: frozenset(
        {
            TaskState.CLAIMED,
            TaskState.WAITING_DEPENDENCY,
            TaskState.CANCELLED,
            TaskState.BLOCKED,
        }
    ),
    TaskState.WAITING_DEPENDENCY: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.CLAIMED: frozenset(
        {TaskState.RUNNING, TaskState.FAILED_SAFE, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.VERIFYING,
            TaskState.READY,
            TaskState.BLOCKED,
            TaskState.FAILED_SAFE,
            TaskState.CANCELLED,
        }
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.PR_READY,
            TaskState.COMPLETED,
            TaskState.READY,
            TaskState.BLOCKED,
            TaskState.FAILED_SAFE,
        }
    ),
    TaskState.PR_READY: frozenset({TaskState.CEO_REVIEW}),
    TaskState.COMPLETED: frozenset({TaskState.CEO_REVIEW}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.CEO_REVIEW: frozenset(),
    TaskState.FAILED_SAFE: frozenset(),
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
    expected_base_sha TEXT, base_branch TEXT NOT NULL DEFAULT 'main', base_sha TEXT,
    origin_sha TEXT, required_tests_json TEXT NOT NULL DEFAULT '[]',
    verification_commands_json TEXT NOT NULL DEFAULT '[]',
    max_runtime_seconds INTEGER NOT NULL DEFAULT 900, requires_pr INTEGER NOT NULL DEFAULT 0,
    lease_generation INTEGER NOT NULL DEFAULT 0, process_id INTEGER,
    commit_sha TEXT, remote_sha TEXT, pr_number INTEGER, pr_url TEXT,
    verification_json TEXT, delivery_json TEXT,
    roadmap_item_id TEXT, debug_budget INTEGER NOT NULL DEFAULT 0,
    debug_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_failure_signature TEXT, failure_repeat_count INTEGER NOT NULL DEFAULT 0,
    repeated_failure_limit INTEGER NOT NULL DEFAULT 2,
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
CREATE TABLE IF NOT EXISTS roadmap_items (
    item_id TEXT PRIMARY KEY, title TEXT NOT NULL, builder_id TEXT NOT NULL,
    template_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    dependency_item_ids_json TEXT NOT NULL DEFAULT '[]', priority INTEGER NOT NULL,
    status TEXT NOT NULL, task_id TEXT, blocked_reason TEXT,
    next_eligible_at TEXT NOT NULL, debug_budget INTEGER NOT NULL DEFAULT 0,
    repeated_failure_limit INTEGER NOT NULL DEFAULT 2, mode TEXT NOT NULL DEFAULT 'bounded',
    enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    CHECK (enabled IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_roadmap_status ON roadmap_items (status, priority DESC, item_id);
"""
