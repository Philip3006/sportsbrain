from __future__ import annotations

from pathlib import Path

import pytest

from src.nightshift import (
    ExecutionResult,
    ExecutorUnavailable,
    FakeExecutor,
    TaskRecord,
)
from src.nightshift.executors import CodexExecutor, redact
from src.nightshift.worktree import WorktreeManager


def test_fake_executor_is_deterministic_and_does_not_spawn_process(
    tmp_path: Path,
) -> None:
    task = TaskRecord(
        task_id="executor-000001",
        builder_id="builder-1",
        objective="fixture",
        branch="nightshift/builder-1/executor",
        repo="Philip3006/sportsbrain",
        task_type="evidence_lifecycle_audit",
        template_id=None,
        payload={},
        risk_class="read_only",
        requires_approval=False,
        priority=0,
        max_attempts=1,
        attempt_count=1,
        state="leased",
        requested_by="test",
        parent_task_id=None,
        dependency_ids=(),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        available_at="2026-01-01T00:00:00Z",
        lease_owner="builder-1",
        lease_expires_at="2099-01-01T00:00:00Z",
        worktree_path=str(tmp_path),
    )
    executor = FakeExecutor(write_file="fixtures/result.txt")
    result = executor(task)
    assert result == ExecutionResult(
        True, "fake execution verified", data={"executor": "fake"}
    )
    assert (tmp_path / "fixtures" / "result.txt").read_text(
        encoding="utf-8"
    ) == "fake executor\n"
    assert executor.calls == [task.task_id]


def test_codex_executor_never_falls_back_when_executable_is_missing(
    tmp_path: Path,
) -> None:
    task = TaskRecord(
        task_id="executor-000002",
        builder_id="builder-1",
        objective="fixture",
        branch="nightshift/builder-1/executor",
        repo="Philip3006/sportsbrain",
        task_type="evidence_lifecycle_audit",
        template_id=None,
        payload={},
        risk_class="read_only",
        requires_approval=False,
        priority=0,
        max_attempts=1,
        attempt_count=1,
        state="leased",
        requested_by="test",
        parent_task_id=None,
        dependency_ids=(),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        available_at="2026-01-01T00:00:00Z",
        lease_owner="builder-1",
        lease_expires_at="2099-01-01T00:00:00Z",
        worktree_path=str(tmp_path),
    )
    executor = CodexExecutor(
        "/definitely/not/codex", WorktreeManager(tmp_path / "runtime", {})
    )
    with pytest.raises(ExecutorUnavailable):
        executor(task)


def test_executor_output_redaction_is_not_secret_output() -> None:
    value = redact("token=secret-value authorization: Bearer abc123 api_key=xyz")
    assert "secret-value" not in value
    assert "abc123" not in value
    assert "xyz" not in value
