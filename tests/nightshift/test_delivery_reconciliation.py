from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.nightshift import (
    DeliveryError,
    DeliveryReconciliationError,
    ExecutionResult,
    FakeExecutor,
    NightShiftDispatcher,
    TaskState,
)

from .test_delivery import REPO, _code_task, _fixture, _git


def _advance_main(repo: Path, relative: str, content: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-qm", f"upstream {relative}")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    return _git(repo, "rev-parse", "HEAD")


def _approved_code_task(dispatcher: NightShiftDispatcher, base_sha: str):
    task = dispatcher.submit(_code_task(base_sha))
    dispatcher.approve(task.task_id, approver="operator", reason="bounded fixture")
    return task


def _advance_after_execution(repo: Path, relative: str, content: str):
    fake = FakeExecutor(write_file="artifacts/change.txt")

    def run(task):
        result = fake(task)
        _advance_main(repo, relative, content)
        return result

    return run, fake


def test_runtime_health_drift_is_reconciled_without_rerunning_worker(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, pull_requests = _fixture(tmp_path)
    task = _approved_code_task(dispatcher, base_sha)
    executor, fake = _advance_after_execution(
        manager.resolve_repo(REPO), "docs/data/health.json", "runtime\n"
    )

    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.PR_READY
    assert fake.calls == [task.task_id]
    assert result.reconciliation_attempts == 1
    assert result.reconciliation
    assert result.reconciliation["classification"] == "RUNTIME_HEALTH_DRIFT"
    assert result.reconciliation["original_commit_sha"] != result.commit_sha
    assert result.delivery and result.delivery["recovery_branch"] == result.reconciliation["recovery_branch"]
    assert result.delivery["base_sha"] == result.reconciliation["authoritative_base_sha"]
    assert len(pull_requests.calls) == 1
    assert dispatcher.store.verify_audit_chain()


def test_unrelated_code_drift_is_reconciled(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, _ = _fixture(tmp_path)
    _approved_code_task(dispatcher, base_sha)
    executor, _ = _advance_after_execution(
        manager.resolve_repo(REPO), "src/unrelated.py", "upstream\n"
    )

    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.PR_READY
    assert result.reconciliation["classification"] == "UNRELATED_UPSTREAM_DRIFT"


def test_same_file_nonconflicting_three_way_overlap_is_reconciled(tmp_path: Path) -> None:
    dispatcher, manager, _, pull_requests = _fixture(tmp_path)
    repo = manager.resolve_repo(REPO)
    base_content = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n"
    (repo / "smoke" / "__init__.py").write_text(base_content, encoding="utf-8")
    _git(repo, "add", "smoke/__init__.py")
    _git(repo, "commit", "-qm", "fixture base lines")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    task_spec = replace(
        _code_task(_git(repo, "rev-parse", "HEAD")),
        task_id="delivery-overlap-000001",
        branch="nightshift/builder-1/delivery-overlap",
        allowed_paths=("smoke",),
        required_tests=(),
        verification_commands=(),
    )
    task = dispatcher.submit(task_spec)
    dispatcher.approve(task.task_id, approver="operator", reason="bounded fixture")

    def executor(record):
        assert record.worktree_path
        target = Path(record.worktree_path) / "smoke" / "__init__.py"
        target.write_text(
            "one\nworker\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n",
            encoding="utf-8",
        )
        _advance_main(
            repo,
            "smoke/__init__.py",
            "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nupstream\nten\n",
        )
        return ExecutionResult(True, "overlap fixture")

    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.PR_READY
    assert result.reconciliation["classification"] == "OVERLAP_REQUIRES_THREE_WAY_VALIDATION"
    assert len(pull_requests.calls) == 1


def test_true_conflicting_delta_is_hard_blocked(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, pull_requests = _fixture(tmp_path)
    _approved_code_task(dispatcher, base_sha)
    executor, _ = _advance_after_execution(
        manager.resolve_repo(REPO), "artifacts/change.txt", "upstream\n"
    )

    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.BLOCKED
    assert result.failure_class == "DELIVERY_RECONCILIATION_FAILED"
    assert result.reconciliation_attempts == 1
    assert not pull_requests.calls
    assert any(
        event.event_type == "delivery_reconciliation_failed"
        for event in dispatcher.store.audit_events(task_id=result.task_id, limit=100)
    )


def test_reconciliation_verification_regression_is_hard_blocked(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, _ = _fixture(tmp_path)
    task = _approved_code_task(dispatcher, base_sha)
    executor, _ = _advance_after_execution(
        manager.resolve_repo(REPO), "results/health/scan.json", "upstream\n"
    )

    class SequenceVerifier:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *_: object):
            self.calls += 1
            if self.calls == 1:
                return type("Result", (), {"as_dict": lambda self: {"passed": True}})()
            return type(
                "Result",
                (),
                {"as_dict": lambda self: {"passed": False, "failure_class": "REGRESSION"}},
            )()

    verifier = SequenceVerifier()
    assert dispatcher.delivery_pipeline is not None
    dispatcher.delivery_pipeline.verifier = verifier

    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.BLOCKED
    assert result.task_id == task.task_id
    assert result.last_error and "verification failed" in result.last_error


def test_reconciliation_is_bounded_when_main_moves_twice(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, pull_requests = _fixture(tmp_path)
    _approved_code_task(dispatcher, base_sha)
    repo = manager.resolve_repo(REPO)
    executor, _ = _advance_after_execution(repo, "docs/data/health.json", "first\n")
    first = base_sha

    original = manager.authoritative_base_sha
    calls = 0

    def advancing(repo_name: str, *, base_branch: str = "main") -> str:
        nonlocal calls
        calls += 1
        value = original(repo_name, base_branch=base_branch)
        if calls == 2:
            _advance_main(repo, "results/health/second.json", "second\n")
            value = original(repo_name, base_branch=base_branch)
        return value

    manager.authoritative_base_sha = advancing  # type: ignore[method-assign]
    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.PR_READY
    assert result.reconciliation_attempts == 2
    assert result.reconciliation["authoritative_base_sha"] != first
    assert len(pull_requests.calls) == 1


def test_third_reconciliation_attempt_is_parked_safely(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, _ = _fixture(tmp_path)
    _approved_code_task(dispatcher, base_sha)
    repo = manager.resolve_repo(REPO)
    executor, _ = _advance_after_execution(repo, "docs/data/health.json", "first\n")
    original = manager.authoritative_base_sha
    calls = 0

    def always_advancing(repo_name: str, *, base_branch: str = "main") -> str:
        nonlocal calls
        calls += 1
        value = original(repo_name, base_branch=base_branch)
        if calls >= 2:
            _advance_main(repo, f"results/health/move-{calls}.json", f"{calls}\n")
            value = original(repo_name, base_branch=base_branch)
        return value

    manager.authoritative_base_sha = always_advancing  # type: ignore[method-assign]
    result = dispatcher.run_once("builder-1", executor)

    assert result is not None and result.state is TaskState.BLOCKED
    assert result.reconciliation_attempts == 2
    assert result.failure_class == "DELIVERY_RECONCILIATION_FAILED"


def test_duplicate_reconciliation_invocation_is_idempotent(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, pull_requests = _fixture(tmp_path)
    _approved_code_task(dispatcher, base_sha)
    executor, _ = _advance_after_execution(
        manager.resolve_repo(REPO), "docs/data/health.json", "runtime\n"
    )
    result = dispatcher.run_once("builder-1", executor)
    assert result is not None and result.state is TaskState.PR_READY
    events_before = len(dispatcher.store.audit_events(task_id=result.task_id, limit=100))

    replay = dispatcher.store.get(result.task_id)
    assert dispatcher.delivery_pipeline is not None
    with pytest.raises(DeliveryReconciliationError):
        dispatcher.delivery_pipeline.reconcile(replay, actor="builder-1")
    assert len(pull_requests.calls) == 1
    assert len(dispatcher.store.audit_events(task_id=result.task_id, limit=100)) == events_before


def test_preserved_blocked_delivery_can_resume_without_worker_rerun(tmp_path: Path) -> None:
    dispatcher, _manager, base_sha, pull_requests = _fixture(tmp_path)
    task = _approved_code_task(dispatcher, base_sha)

    class FailingPR:
        def find_or_create(self, *_: object, **__: object):
            raise DeliveryError("simulated PR transport outage")

    assert dispatcher.delivery_pipeline is not None
    dispatcher.delivery_pipeline.github = FailingPR()
    blocked = dispatcher.run_once(
        "builder-1", FakeExecutor(write_file="artifacts/change.txt")
    )
    assert blocked is not None and blocked.state is TaskState.BLOCKED
    assert blocked.delivery_blocked
    dispatcher.delivery_pipeline.github = pull_requests
    resumed = dispatcher.reconcile_preserved_delivery(task.task_id)
    assert resumed.state is TaskState.PR_READY
    assert resumed.attempt_count == 1
    assert len(pull_requests.calls) == 1


def test_recovery_commands_never_force_push_or_rebase(tmp_path: Path) -> None:
    dispatcher, manager, base_sha, _ = _fixture(tmp_path)
    _approved_code_task(dispatcher, base_sha)
    executor, _ = _advance_after_execution(
        manager.resolve_repo(REPO), "docs/data/health.json", "runtime\n"
    )
    result = dispatcher.run_once("builder-1", executor)
    assert result is not None and result.state is TaskState.PR_READY
    calls = dispatcher.store.audit_events(task_id=result.task_id, limit=100)
    assert all("force" not in str(event.details).lower() for event in calls)
    assert all("rebase" not in str(event.details).lower() for event in calls)


def test_status_exposes_reconciliation_and_provenance(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    task = _approved_code_task(dispatcher, base_sha)
    claimed = dispatcher.claim_next("builder-1")
    assert claimed is not None
    owner = claimed.lease_owner or "builder-1"
    dispatcher.store.start_running(
        task.task_id, worker_id=owner, lease_generation=claimed.lease_generation
    )
    dispatcher.store.begin_verifying(
        task.task_id, worker_id=owner, lease_generation=claimed.lease_generation
    )
    dispatcher.store.start_delivery_reconciliation(
        task.task_id,
        actor=owner,
        lease_generation=claimed.lease_generation,
        evidence={
            "original_base_sha": base_sha,
            "original_commit_sha": "a" * 40,
            "authoritative_base_sha": "b" * 40,
            "classification": "UNRELATED_UPSTREAM_DRIFT",
        },
    )

    operator = dispatcher.status()["operator"]

    assert operator["delivery_reconciling"][0]["reconciliation_attempts"] == 1
    assert operator["delivery_reconciling"][0]["reconciliation"]["original_commit_sha"] == "a" * 40
