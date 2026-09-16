from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from src.nightshift import (
    EventType,
    FakeExecutor,
    GhPullRequestClient,
    LeaseError,
    NightShiftDispatcher,
    TaskSpec,
    TaskState,
)
from src.nightshift.models import parse_timestamp
from src.nightshift.verification import VerificationResult

from .test_delivery import _code_task, _fixture, _git


def _verifying_code_task(
    dispatcher: NightShiftDispatcher, base_sha: str, task_id: str
) -> tuple[TaskSpec, Any]:
    task = TaskSpec(**{**_code_task(base_sha).as_dict(), "task_id": task_id})
    dispatcher.submit(task)
    dispatcher.approve(task.task_id, approver="operator")
    claimed = dispatcher.claim_next("builder-1")
    assert claimed is not None and claimed.worktree_path
    owner = claimed.lease_owner or "builder-1"
    now = dispatcher.clock()
    dispatcher.store.start_running(
        task.task_id,
        worker_id=owner,
        lease_generation=claimed.lease_generation,
        now=now,
    )
    dispatcher.store.begin_verifying(
        task.task_id,
        worker_id=owner,
        lease_generation=claimed.lease_generation,
        now=now,
    )
    return task, dispatcher.store.get(task.task_id)


def _expire(dispatcher: NightShiftDispatcher, task_id: str) -> Any:
    current = dispatcher.store.get(task_id)
    assert current.lease_expires_at
    expiry = parse_timestamp(current.lease_expires_at)
    recovered = dispatcher.store.recover_expired(
        now=expiry + timedelta(seconds=1), actor="test-reaper"
    )
    assert any(item.task_id == task_id for item in recovered)
    return dispatcher.store.get(task_id)


def test_expired_owner_and_generation_are_still_fenced(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            task_id="expired-fence-000001",
            builder_id="builder-1",
            objective="Inspect bounded evidence",
            branch="nightshift/builder-1/expired-fence",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
        )
    )
    claimed = dispatcher.claim_next("builder-1")
    assert claimed is not None and claimed.lease_expires_at
    expiry = parse_timestamp(claimed.lease_expires_at)
    with pytest.raises(LeaseError):
        dispatcher.store.assert_active_lease(
            task.task_id,
            worker_id=claimed.lease_owner or "builder-1",
            lease_generation=claimed.lease_generation,
            now=expiry,
        )
    with pytest.raises(LeaseError):
        dispatcher.store.start_running(
            task.task_id,
            worker_id=claimed.lease_owner or "builder-1",
            lease_generation=claimed.lease_generation,
            now=expiry,
        )


def test_lease_expiry_during_verification_prevents_commit_push_and_pr(
    tmp_path: Path,
) -> None:
    dispatcher, manager, base_sha, pull_requests = _fixture(tmp_path)
    task, current = _verifying_code_task(
        dispatcher, base_sha, "expiry-during-verification-1"
    )
    FakeExecutor(write_file="artifacts/change.txt")(current)

    class _ExpiresDuringVerification:
        def run(self, _task: TaskSpec, _path: Path) -> VerificationResult:
            _expire(dispatcher, task.task_id)
            return VerificationResult(True, ())

    assert dispatcher.delivery_pipeline is not None
    dispatcher.delivery_pipeline.verifier = _ExpiresDuringVerification()
    owner = current.lease_owner or "builder-1"
    with pytest.raises(LeaseError):
        dispatcher.delivery_pipeline.deliver(
            current,
            worker_id=owner,
            lease_generation=current.lease_generation,
            lease_guard=lambda: dispatcher.store.assert_active_lease(
                task.task_id,
                worker_id=owner,
                lease_generation=current.lease_generation,
                now=dispatcher.clock(),
            ),
        )

    after = dispatcher.store.get(task.task_id)
    assert after.state is TaskState.READY
    assert after.commit_sha is None and after.remote_sha is None
    assert pull_requests.calls == []
    assert manager.changed_paths(Path(current.worktree_path or "")) == (
        "artifacts/change.txt",
    )


def test_lease_expiry_before_commit_prevents_git_commit(tmp_path: Path) -> None:
    dispatcher, _, base_sha, pull_requests = _fixture(tmp_path)
    task, current = _verifying_code_task(
        dispatcher, base_sha, "expiry-before-commit-0001"
    )
    FakeExecutor(write_file="artifacts/change.txt")(current)
    owner = current.lease_owner or "builder-1"
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            _expire(dispatcher, task.task_id)
        dispatcher.store.assert_active_lease(
            task.task_id,
            worker_id=owner,
            lease_generation=current.lease_generation,
            now=dispatcher.clock(),
        )

    assert dispatcher.delivery_pipeline is not None
    with pytest.raises(LeaseError):
        dispatcher.delivery_pipeline.deliver(
            current,
            worker_id=owner,
            lease_generation=current.lease_generation,
            lease_guard=guard,
        )
    assert dispatcher.store.get(task.task_id).commit_sha is None
    assert pull_requests.calls == []


def test_lease_expiry_before_push_prevents_task_branch_push(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    task, current = _verifying_code_task(
        dispatcher, base_sha, "expiry-before-push-000001"
    )
    path = Path(current.worktree_path or "")
    (path / "artifacts").mkdir()
    (path / "artifacts" / "change.txt").write_text("fixture\n", encoding="utf-8")
    _git(path, "add", "--", "artifacts/change.txt")
    _git(path, "commit", "-m", "fixture commit")
    owner = current.lease_owner or "builder-1"

    def guard() -> None:
        _expire(dispatcher, task.task_id)
        dispatcher.store.assert_active_lease(
            task.task_id,
            worker_id=owner,
            lease_generation=current.lease_generation,
            now=dispatcher.clock(),
        )

    assert dispatcher.delivery_pipeline is not None
    with pytest.raises(LeaseError):
        dispatcher.delivery_pipeline._push(current, path, lease_guard=guard)
    assert _git(path, "ls-remote", "origin", f"refs/heads/{task.branch}") == ""


def test_lease_expiry_before_pr_create_prevents_pr_creation(tmp_path: Path) -> None:
    dispatcher, _, base_sha, pull_requests = _fixture(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            **{
                **_code_task(base_sha).as_dict(),
                "task_id": "expiry-before-pr-000001",
            }
        )
    )
    dispatcher.approve(task.task_id, approver="operator")
    original_record_push = dispatcher.store.record_push

    def record_push_then_expire(*args: Any, **kwargs: Any) -> Any:
        result = original_record_push(*args, **kwargs)
        _expire(dispatcher, task.task_id)
        return result

    dispatcher.store.record_push = record_push_then_expire  # type: ignore[method-assign]
    result = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )
    assert result is not None
    result = dispatcher.store.get(task.task_id)
    assert result.state is TaskState.READY
    assert result.pr_number is None and pull_requests.calls == []


def test_long_verification_is_renewed_until_pr_ready(tmp_path: Path) -> None:
    dispatcher, _, base_sha, pull_requests = _fixture(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            **{
                **_code_task(base_sha).as_dict(),
                "task_id": "long-verification-0001",
            }
        )
    )
    dispatcher.approve(task.task_id, approver="operator")

    class _SlowVerifier:
        def run(self, _task: TaskSpec, _path: Path) -> VerificationResult:
            time.sleep(10.5)
            return VerificationResult(True, ())

    assert dispatcher.delivery_pipeline is not None
    dispatcher.delivery_pipeline.verifier = _SlowVerifier()
    result = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )
    assert result is not None and result.state is TaskState.PR_READY
    assert len(pull_requests.calls) == 1
    heartbeats = [
        event
        for event in dispatcher.store.audit_events(task_id=task.task_id, limit=1000)
        if event.event_type == EventType.HEARTBEAT.value
    ]
    assert len(heartbeats) >= 2


def test_github_auth_failure_cannot_produce_fake_pr_ready(tmp_path: Path) -> None:
    dispatcher, _, base_sha, pull_requests = _fixture(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            **{
                **_code_task(base_sha).as_dict(),
                "task_id": "github-auth-block-0001",
            }
        )
    )
    dispatcher.approve(task.task_id, approver="operator")
    gh = tmp_path / "gh-auth-fails"
    gh.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = auth ]; then exit 1; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)
    assert dispatcher.delivery_pipeline is not None
    dispatcher.delivery_pipeline.github = GhPullRequestClient(
        gh_executable=str(gh)
    )
    result = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )
    assert result is not None and result.state is TaskState.BLOCKED
    assert result.failure_class == "DELIVERY_BLOCKED_GITHUB_AUTH"
    assert result.pr_number is None and result.pr_url is None
    assert pull_requests.calls == []


def test_delivery_command_path_has_no_merge_or_deploy_action(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            **{
                **_code_task(base_sha).as_dict(),
                "task_id": "delivery-no-merge-0001",
            }
        )
    )
    dispatcher.approve(task.task_id, approver="operator")
    assert dispatcher.delivery_pipeline is not None
    pipeline = dispatcher.delivery_pipeline
    commands: list[tuple[str, ...]] = []
    original_run = pipeline._run

    def record_run(path: Path, args: list[str], *, timeout: int) -> Any:
        commands.append(tuple(args))
        return original_run(path, args, timeout=timeout)

    pipeline._run = record_run  # type: ignore[method-assign]
    result = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )
    assert result is not None and result.state is TaskState.PR_READY
    assert all(command[0] not in {"merge", "deploy"} for command in commands)
    assert ("push", "origin", f"HEAD:{task.branch}") in commands


def test_recovery_fences_old_worker_generation_and_delivery_evidence(
    tmp_path: Path,
) -> None:
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
    assert first is not None and first.lease_expires_at
    expiry = parse_timestamp(first.lease_expires_at)
    recovered = dispatcher.store.recover_expired(
        now=expiry + timedelta(seconds=1)
    )
    assert recovered and recovered[0].lease_generation > first.lease_generation
    with pytest.raises(LeaseError):
        dispatcher.store.record_commit(
            task.task_id,
            worker_id="builder-1:old",
            lease_generation=first.lease_generation,
            commit_sha="a" * 40,
        )
