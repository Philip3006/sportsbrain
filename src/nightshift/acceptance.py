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


class _AcceptancePullRequestClient:
    """Local deterministic PR fixture; it never contacts GitHub."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def find_or_create(
        self,
        task: Any,
        *,
        commit_sha: str,
        verification: dict[str, Any],
        lease_guard: Any = None,
    ) -> dict[str, Any]:
        if lease_guard is not None:
            lease_guard()
        if task.task_id in {item["task_id"] for item in self.created}:
            return {
                **next(
                    item for item in self.created if item["task_id"] == task.task_id
                ),
                "reused": True,
            }
        pull = {
            "number": len(self.created) + 1,
            "url": f"https://example.invalid/pull/{len(self.created) + 1}",
            "task_id": task.task_id,
            "commit_sha": commit_sha,
            "verification": verification,
            "reused": False,
        }
        self.created.append(pull)
        return pull


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


def _finish_read_only(dispatcher: NightShiftDispatcher, claimed: Any) -> Any:
    """Drive a claimed read-only fixture through the canonical state path."""

    owner = claimed.lease_owner or claimed.builder_id
    dispatcher.store.start_running(
        claimed.task_id,
        worker_id=owner,
        lease_generation=claimed.lease_generation,
        now=dispatcher.clock(),
    )
    dispatcher.store.begin_verifying(
        claimed.task_id,
        worker_id=owner,
        lease_generation=claimed.lease_generation,
        now=dispatcher.clock(),
    )
    current = dispatcher.store.get(claimed.task_id)
    if dispatcher.delivery_pipeline is not None:
        dispatcher.delivery_pipeline.deliver(
            current, worker_id=owner, lease_generation=claimed.lease_generation
        )
    return dispatcher.complete(
        claimed.task_id,
        worker_instance_id=owner,
        lease_generation=claimed.lease_generation,
        execution=ExecutionResult(True, "verified", terminal_state=TaskState.COMPLETED),
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
        remote = root / "remote.git"
        _git(root, "init", "--bare", "-q", str(remote))
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "HEAD:main")
        (repo / "smoke").mkdir()
        (repo / "smoke" / "__init__.py").write_text("fixture\n", encoding="utf-8")
        _git(repo, "add", "smoke/__init__.py")
        _git(repo, "commit", "-qm", "verification fixture")
        _git(repo, "push", "-q", "origin", "HEAD:main")
        manager = WorktreeManager(root / "runtime", {REPO: repo})
        dispatcher = NightShiftDispatcher.from_config(
            state_path=root / "runtime" / "state.sqlite3",
            worktree_manager=manager,
            lease_seconds=30,
            retry_base_seconds=0,
            clock=lambda: datetime(2026, 9, 16, tzinfo=UTC),
        )
        assert dispatcher.delivery_pipeline is not None
        dispatcher.delivery_pipeline.github = _AcceptancePullRequestClient()
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
            owner = item.lease_owner or item.builder_id
            dispatcher.store.start_running(
                item.task_id,
                worker_id=owner,
                lease_generation=item.lease_generation,
                now=dispatcher.clock(),
            )
            current = dispatcher.store.get(item.task_id)
            FakeExecutor()(current)
            dispatcher.store.begin_verifying(
                item.task_id,
                worker_id=owner,
                lease_generation=item.lease_generation,
                now=dispatcher.clock(),
            )
            dispatcher.delivery_pipeline.deliver(
                dispatcher.store.get(item.task_id),
                worker_id=owner,
                lease_generation=item.lease_generation,
            )
            result = dispatcher.complete(
                item.task_id,
                worker_instance_id=owner,
                lease_generation=item.lease_generation,
                execution=ExecutionResult(
                    True, "verified", terminal_state=TaskState.COMPLETED
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
        _finish_read_only(dispatcher, held_lock)
        lock_claim = dispatcher.claim_next("builder-2")
        assert lock_claim is not None
        _finish_read_only(dispatcher, lock_claim)
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
        _finish_read_only(dispatcher, safe_claim)
        observed["steps"]["ceo_block_does_not_hold_builder"] = (
            blocked.state is TaskState.BLOCKED and safe_claim.task_id == safe.task_id
        )

        pr = dispatcher.submit(
            _task(
                "accept-pr-ready01",
                "builder-4",
                "nightshift/builder-4/accept-pr",
                task_type="provider_health_replay",
                risk_class=RiskClass.CODE_CHANGE,
                requires_pr=True,
                allowed_paths=("artifacts",),
                required_tests=("compileall",),
                verification_commands=(("python3", "-m", "compileall", "-q", "smoke"),),
            )
        )
        dispatcher.approve(
            pr.task_id, approver="acceptance", reason="fixture scope approved"
        )
        ready = dispatcher.run_once(
            "builder-4", FakeExecutor(write_file="artifacts/change.txt")
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
            clock=lambda: datetime(2026, 9, 16, tzinfo=UTC),
        )
        restarted.heartbeat(
            restart.task_id,
            worker_instance_id=held.lease_owner or "builder-1",
            lease_generation=held.lease_generation,
        )
        _finish_read_only(restarted, restarted.store.get(restart.task_id))
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
