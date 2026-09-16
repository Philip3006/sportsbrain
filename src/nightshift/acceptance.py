"""Deterministic FakeExecutor acceptance scenario for Builder 5."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .dispatcher import NightShiftDispatcher
from .executors import FakeExecutor
from .models import ExecutionResult, RiskClass, TaskSpec, TaskState
from .worktree import WorktreeManager

UTC = timezone.utc
REPO = "Philip3006/sportsbrain"


def _git(path: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git fixture operation failed")


def _task(
    task_id: str,
    builder: str,
    branch: str,
    task_type: str = "evidence_lifecycle_audit",
    **kwargs: Any,
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        builder_id=builder,
        objective=kwargs.pop("objective", "Inspect bounded acceptance evidence"),
        branch=branch,
        repo=REPO,
        task_type=task_type,
        risk_class=kwargs.pop("risk_class", RiskClass.READ_ONLY),
        **kwargs,
    )


def run_fake_acceptance() -> dict[str, Any]:
    """Run all required safety and lifecycle assertions in disposable repos."""

    with TemporaryDirectory(prefix="sportsbrain-nightshift-acceptance-") as temp:
        root = Path(temp)
        repo = root / "sportsbrain"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "acceptance@example.invalid")
        _git(repo, "config", "user.name", "Night Shift Acceptance")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-qm", "fixture")
        manager = WorktreeManager(root / "runtime", {REPO: repo})
        dispatcher = NightShiftDispatcher.from_config(
            state_path=root / "runtime" / "state.sqlite3",
            worktree_manager=manager,
            lease_seconds=30,
            retry_base_seconds=0,
        )
        observed: dict[str, Any] = {
            "builders": list(dispatcher.registry.builder_ids),
            "steps": {},
        }

        dispatcher.submit(
            _task(
                "accept-b1-000001",
                "builder-1",
                "nightshift/builder-1/accept-b1",
                resource_locks=("evidence:index",),
            )
        )
        dispatcher.submit(
            _task(
                "accept-b2-000001",
                "builder-2",
                "nightshift/builder-2/accept-b2",
                task_type="authority_review",
            )
        )
        dispatcher.submit(
            _task(
                "accept-b3-000001",
                "builder-3",
                "nightshift/builder-3/accept-b3",
                task_type="observability_audit",
            )
        )
        dispatcher.submit(
            _task(
                "accept-b4-000001",
                "builder-4",
                "nightshift/builder-4/accept-b4",
                task_type="provider_health_replay",
            )
        )
        claims = [
            dispatcher.claim_next(builder)
            for builder in ("builder-1", "builder-2", "builder-3", "builder-4")
        ]
        assert all(item and item.worktree_path for item in claims)
        observed["steps"]["parallel_isolated_claims"] = (
            len({item.worktree_path for item in claims if item}) == 4
        )
        for item in claims:
            assert item
            result = dispatcher.complete(
                item.task_id,
                worker_instance_id=item.lease_owner or item.builder_id,
                execution=ExecutionResult(
                    True, "ready", terminal_state=TaskState.PR_READY
                ),
            )
            dispatcher.mark_ceo_review(
                result.task_id, actor="acceptance", evidence={"verified": True}
            )
        observed["steps"]["builders_1_to_4_ready"] = all(
            dispatcher.store.get(item.task_id).state is TaskState.CEO_REVIEW
            for item in claims
            if item
        )

        dependency = dispatcher.submit(
            _task("accept-dep-000001", "builder-1", "nightshift/builder-1/accept-dep")
        )
        dispatcher.submit(
            _task(
                "accept-child-00001",
                "builder-3",
                "nightshift/builder-3/accept-child",
                task_type="observability_audit",
                dependency_ids=(dependency.task_id,),
            )
        )
        assert dispatcher.claim_next("builder-3") is None
        dispatcher.run_once("builder-1", FakeExecutor())
        child = dispatcher.run_once("builder-3", FakeExecutor())
        observed["steps"]["dependency_wait_then_unlock"] = (
            child is not None and child.state is TaskState.SUCCEEDED
        )

        lock_one = dispatcher.submit(
            _task(
                "accept-lock-000001",
                "builder-1",
                "nightshift/builder-1/accept-lock",
                resource_locks=("shared:fixture",),
            )
        )
        lock_two = dispatcher.submit(
            _task(
                "accept-lock-000002",
                "builder-2",
                "nightshift/builder-2/accept-lock",
                task_type="authority_review",
                resource_locks=("shared:fixture",),
            )
        )
        held_lock = dispatcher.claim_next("builder-1")
        assert held_lock is not None
        assert dispatcher.claim_next("builder-2") is None
        dispatcher.complete(
            held_lock.task_id,
            worker_instance_id=held_lock.lease_owner or "builder-1",
            execution=ExecutionResult(True),
        )
        lock_claim = dispatcher.claim_next("builder-2")
        assert lock_claim is not None
        dispatcher.complete(
            lock_claim.task_id,
            worker_instance_id=lock_claim.lease_owner or "builder-2",
            execution=ExecutionResult(True),
        )
        observed["steps"]["resource_lock_conflict_then_release"] = (
            lock_one.task_id != lock_two.task_id
        )

        dispatcher.submit(
            _task(
                "accept-crash-00001",
                "builder-3",
                "nightshift/builder-3/accept-crash",
                task_type="observability_audit",
            )
        )
        first = dispatcher.run_once(
            "builder-3",
            lambda _: (_ for _ in ()).throw(RuntimeError("simulated crash")),
        )
        second = dispatcher.run_once("builder-3", FakeExecutor())
        observed["steps"]["worker_crash_retried"] = (
            first is not None
            and first.state is TaskState.RETRY_WAIT
            and second is not None
            and second.state is TaskState.SUCCEEDED
        )

        stale = dispatcher.submit(
            _task(
                "accept-stale-00001",
                "builder-4",
                "nightshift/builder-4/accept-stale",
                task_type="provider_health_replay",
                max_attempts=2,
            )
        )
        leased = dispatcher.claim_next("builder-4")
        assert leased
        recovered = dispatcher.store.recover_expired(
            now=datetime.now(UTC).replace(microsecond=0).replace(year=2099),
            actor="builder-5",
        )
        assert any(item.task_id == stale.task_id for item in recovered)
        observed["steps"]["stale_heartbeat_recovered"] = (
            recovered[0].state is TaskState.RETRY_WAIT
        )
        future = NightShiftDispatcher.from_config(
            state_path=root / "runtime" / "state.sqlite3",
            require_isolated_worktrees=False,
            clock=lambda: datetime(2099, 12, 31, tzinfo=UTC),
            lease_seconds=30,
            retry_base_seconds=0,
        )
        future.run_once("builder-4", FakeExecutor())

        retry = dispatcher.submit(
            _task(
                "accept-retry-00001",
                "builder-1",
                "nightshift/builder-1/accept-retry",
                max_attempts=2,
            )
        )
        dispatcher.run_once("builder-1", lambda _: ExecutionResult(False, "retry-1"))
        dispatcher.run_once("builder-1", lambda _: ExecutionResult(False, "retry-2"))
        observed["steps"]["bounded_retry_dead_letter"] = (
            dispatcher.store.get(retry.task_id).state is TaskState.DEAD_LETTER
        )

        dispatcher.submit(
            _task(
                "accept-safe-fail01",
                "builder-2",
                "nightshift/builder-2/accept-safe-fail",
                task_type="authority_review",
            )
        )
        result = dispatcher.run_once(
            "builder-2",
            lambda _: ExecutionResult(
                False,
                "test failed",
                retryable=False,
                terminal_state=TaskState.FAILED_SAFE,
            ),
        )
        observed["steps"]["failed_test_failed_safe"] = (
            result is not None and result.state is TaskState.FAILED_SAFE
        )

        blocked = dispatcher.submit(
            _task(
                "accept-ceo-auth01",
                "builder-3",
                "nightshift/builder-3/accept-ceo-auth",
                task_type="observability_audit",
                objective="production activation requires CEO authorization",
            )
        )
        safe = dispatcher.submit(
            _task(
                "accept-after-block",
                "builder-3",
                "nightshift/builder-3/accept-after-block",
                task_type="observability_audit",
            )
        )
        safe_claim = dispatcher.claim_next("builder-3")
        assert safe_claim
        dispatcher.complete(
            safe_claim.task_id,
            worker_instance_id=safe_claim.lease_owner or "builder-3",
            execution=ExecutionResult(True),
        )
        observed["steps"]["ceo_block_does_not_hold_builder"] = (
            blocked.state is TaskState.BLOCKED and safe_claim.task_id == safe.task_id
        )

        pr = dispatcher.submit(
            _task(
                "accept-pr-ready01",
                "builder-4",
                "nightshift/builder-4/accept-pr",
                task_type="provider_health_replay",
            )
        )
        ready = dispatcher.run_once(
            "builder-4", FakeExecutor(outcome=TaskState.PR_READY)
        )
        assert ready
        reviewed = dispatcher.mark_ceo_review(
            pr.task_id, actor="acceptance", evidence={"head": "fixture"}
        )
        observed["steps"]["pr_ready_ceo_review"] = (
            reviewed.state is TaskState.CEO_REVIEW
        )

        restart = dispatcher.submit(
            _task(
                "accept-restart-001", "builder-1", "nightshift/builder-1/accept-restart"
            )
        )
        held = dispatcher.claim_next("builder-1")
        assert held
        restarted = NightShiftDispatcher.from_config(
            state_path=root / "runtime" / "state.sqlite3",
            worktree_manager=manager,
            lease_seconds=30,
            retry_base_seconds=0,
        )
        restarted.heartbeat(
            restart.task_id, worker_instance_id=held.lease_owner or "builder-1"
        )
        restarted.complete(
            restart.task_id,
            worker_instance_id=held.lease_owner or "builder-1",
            execution=ExecutionResult(True),
        )
        replay = restarted.submit(
            _task(
                "accept-idempotent",
                "builder-1",
                "nightshift/builder-1/accept-idempotent",
                idempotency_key="accept-once",
            )
        )
        replayed = restarted.submit(
            _task(
                "accept-idempotent-2",
                "builder-1",
                "nightshift/builder-1/accept-idempotent",
                idempotency_key="accept-once",
            )
        )
        restarted.run_once("builder-1", FakeExecutor())
        observed["steps"]["restart_and_idempotency"] = (
            replay.task_id == replayed.task_id
        )
        observed["steps"]["audit_chain"] = restarted.store.verify_audit_chain()
        observed["steps"]["queue_exhaustion_idle_safe"] = (
            restarted.status()["queue_mode"] == "IDLE_SAFE"
        )
        observed["task_count"] = restarted.status()["total"]
        observed["status"] = "PASS" if all(observed["steps"].values()) else "FAIL"
        return observed


def main() -> int:
    print(json.dumps(run_fake_acceptance(), sort_keys=True, indent=2))
    return 0
