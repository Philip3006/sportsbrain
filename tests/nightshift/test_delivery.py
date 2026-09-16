from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.nightshift import (
    ExecutionResult,
    FakeExecutor,
    LeaseError,
    NightShiftDispatcher,
    SafetyViolation,
    TaskSpec,
    TaskState,
)
from src.nightshift.errors import InvalidTaskError
from src.nightshift.worktree import WorktreeManager

REPO = "Philip3006/sportsbrain"


class _PullRequestFixture:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def find_or_create(
        self, task: Any, *, commit_sha: str, verification: dict[str, Any]
    ) -> dict[str, Any]:
        existing = next(
            (item for item in self.calls if item["task_id"] == task.task_id), None
        )
        if existing:
            return {**existing, "reused": True}
        result = {
            "number": len(self.calls) + 1,
            "url": f"https://example.invalid/pull/{len(self.calls) + 1}",
            "task_id": task.task_id,
            "commit_sha": commit_sha,
            "verification": verification,
            "reused": False,
        }
        self.calls.append(result)
        return result


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fixture(
    tmp_path: Path,
) -> tuple[NightShiftDispatcher, WorktreeManager, str, _PullRequestFixture]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Night Shift Test")
    (repo / "smoke").mkdir()
    (repo / "smoke" / "__init__.py").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "smoke/__init__.py")
    _git(repo, "commit", "-qm", "fixture")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "HEAD:main")
    base_sha = _git(repo, "rev-parse", "HEAD")
    manager = WorktreeManager(tmp_path / "runtime", {REPO: repo})
    dispatcher = NightShiftDispatcher.from_config(
        state_path=tmp_path / "runtime" / "state.sqlite3",
        worktree_manager=manager,
        lease_seconds=30,
        retry_base_seconds=0,
    )
    pull_requests = _PullRequestFixture()
    assert dispatcher.delivery_pipeline is not None
    dispatcher.delivery_pipeline.github = pull_requests
    return dispatcher, manager, base_sha, pull_requests


def _code_task(base_sha: str) -> TaskSpec:
    return TaskSpec(
        task_id="delivery-test-000001",
        builder_id="builder-1",
        objective="Create a bounded smoke artifact",
        branch="nightshift/builder-1/delivery-test",
        task_type="research_shadow_evidence",
        risk_class="code_change",
        expected_base_sha=base_sha,
        allowed_paths=("artifacts",),
        required_tests=("compileall",),
        verification_commands=(("python3", "-m", "compileall", "-q", "smoke"),),
    )


def test_code_change_has_independent_verification_commit_push_and_one_pr(
    tmp_path: Path,
) -> None:
    dispatcher, manager, base_sha, pull_requests = _fixture(tmp_path)
    task = dispatcher.submit(_code_task(base_sha))
    dispatcher.approve(task.task_id, approver="operator", reason="fixture scope")

    result = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )

    assert result is not None
    assert result.state is TaskState.PR_READY
    assert result.base_sha == base_sha
    assert result.origin_sha == base_sha
    assert result.commit_sha and result.commit_sha == result.remote_sha
    assert result.pr_number == 1
    assert result.pr_url == "https://example.invalid/pull/1"
    assert result.verification and result.verification["passed"] is True
    assert manager.changed_paths(Path(result.worktree_path or "")) == ()
    assert len(pull_requests.calls) == 1
    assert [
        event.event_type
        for event in dispatcher.store.audit_events(task_id=task.task_id, limit=1000)
    ]
    assert (
        dispatcher.mark_ceo_review(task.task_id, actor="operator").state
        is TaskState.CEO_REVIEW
    )


def test_expected_base_sha_mismatch_fails_safe_before_worktree_creation(
    tmp_path: Path,
) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    dispatcher.submit(
        TaskSpec(
            task_id="base-mismatch-00001",
            builder_id="builder-1",
            objective="Inspect bounded evidence",
            branch="nightshift/builder-1/base-mismatch",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
            expected_base_sha="0" * 40,
        )
    )

    result = dispatcher.claim_next("builder-1")

    assert result is not None
    assert result.state is TaskState.FAILED_SAFE
    assert result.failure_class == "BASE_SHA_MISMATCH"
    assert result.worktree_path is None


def test_store_cannot_complete_code_change_without_pr_evidence(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    task = dispatcher.submit(_code_task(base_sha))
    dispatcher.approve(task.task_id, approver="operator")
    claimed = dispatcher.claim_next("builder-1")
    assert claimed is not None
    owner = claimed.lease_owner or "builder-1"
    dispatcher.store.start_running(
        task.task_id, worker_id=owner, lease_generation=claimed.lease_generation
    )
    dispatcher.store.begin_verifying(
        task.task_id, worker_id=owner, lease_generation=claimed.lease_generation
    )

    with pytest.raises(SafetyViolation):
        dispatcher.store.complete(
            task.task_id,
            worker_id=owner,
            lease_generation=claimed.lease_generation,
            execution=ExecutionResult(True),
        )

    assert dispatcher.store.get(task.task_id).state is TaskState.VERIFYING


def test_late_owner_generation_is_fenced_after_recovery(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    dispatcher.worktree_manager = None
    task = dispatcher.submit(
        TaskSpec(
            task_id="generation-fence-0001",
            builder_id="builder-1",
            objective="Inspect bounded evidence",
            branch="nightshift/builder-1/generation-fence",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
        )
    )
    first = dispatcher.claim_next("builder-1", worker_instance_id="builder-1:old")
    assert first is not None
    recovered = dispatcher.store.recover_expired(
        now=__import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00")
    )
    assert recovered and recovered[0].state is TaskState.READY
    second = dispatcher.store.claim_next(
        "builder-1",
        worker_id="builder-1:new",
        lease_seconds=30,
        now=__import__("datetime").datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
    )
    assert second is not None and second.lease_generation > first.lease_generation
    with pytest.raises(LeaseError):
        dispatcher.heartbeat(
            task.task_id,
            worker_instance_id="builder-1:old",
            lease_generation=first.lease_generation,
        )


def test_verification_commands_are_structured_and_non_shell() -> None:
    with pytest.raises(InvalidTaskError):
        TaskSpec(
            builder_id="builder-1",
            objective="Inspect bounded evidence",
            branch="nightshift/builder-1/unsafe-command",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
            verification_commands=(("sh", "-c", "echo unsafe"),),
        )
