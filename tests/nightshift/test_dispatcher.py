from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.nightshift import (
    BuilderRegistry,
    DispatcherRecursionError,
    ExecutionResult,
    IdempotencyConflictError,
    LeaseError,
    NightShiftDispatcher,
    SafetyViolation,
    TaskSpec,
    TaskState,
    TemplateRegistry,
    UnknownBuilderError,
)
from src.nightshift.store import AuditIntegrityError
from src.nightshift.worktree import WorktreeManager

UTC = timezone.utc
T0 = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
CONFIG_DIR = Path(__file__).parents[2] / "config" / "night_shift"


def _prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    if repo.exists():
        return repo
    repo.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Night Shift Test"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), *command], check=True, capture_output=True
        )
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "src/__init__.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "fixture"],
        check=True,
        capture_output=True,
    )
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"],
        check=True,
        capture_output=True,
    )
    return repo


def _dispatcher(
    tmp_path: Path, *, now: datetime = T0, **kwargs: object
) -> NightShiftDispatcher:
    repo = _prepare_repo(tmp_path)
    return NightShiftDispatcher.from_config(
        config_dir=CONFIG_DIR,
        state_path=tmp_path / "nightshift.sqlite3",
        clock=lambda: now,
        lease_seconds=30,
        retry_base_seconds=0,
        worktree_manager=WorktreeManager(
            tmp_path / "runtime", {"Philip3006/sportsbrain": repo}
        ),
        **kwargs,
    )


def _spec(
    builder_id: str = "builder-1",
    *,
    risk: str = "read_only",
    task_id: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id or f"task-{builder_id.replace('-', '')}-123456",
        builder_id=builder_id,
        objective="Inspect bounded evidence",
        branch=f"nightshift/{builder_id}/test",
        task_type="evidence_lifecycle_audit"
        if builder_id == "builder-1"
        else "authority_review",
        risk_class=risk,
    )


def test_default_governance_registers_only_builders_one_to_four(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)

    assert dispatcher.registry.builder_ids == (
        "builder-1",
        "builder-2",
        "builder-3",
        "builder-4",
    )
    assert all(
        template.builder_id != "builder-5"
        for template in dispatcher.templates.templates
    )
    assert dispatcher.status()["dispatcher_id"] == "builder-5"


@pytest.mark.parametrize(
    "builder_id", ["builder-1", "builder-2", "builder-3", "builder-4"]
)
def test_each_registered_builder_can_submit_and_run_its_explicit_template(
    tmp_path: Path, builder_id: str
) -> None:
    dispatcher = _dispatcher(tmp_path)
    template_id = next(
        template.template_id
        for template in dispatcher.templates.templates
        if template.builder_id == builder_id
        and template.risk_class.value == "read_only"
    )
    record = dispatcher.submit_template(
        template_id,
        branch=f"nightshift/{builder_id}/template-test",
        payload={"scope": "night shift contract"},
        requested_by="test-operator",
    )

    assert record.state is TaskState.QUEUED
    completed = dispatcher.run_once(
        builder_id, lambda task: ExecutionResult(True, summary=task.objective)
    )

    assert completed is not None
    assert completed.state is TaskState.SUCCEEDED
    assert completed.attempt_count == 1
    assert dispatcher.store.verify_audit_chain()


def test_explicit_builder_alias_uses_canonical_lease_owner(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_spec(task_id="task-alias-123456"))
    record = dispatcher.run_once("b1", lambda _: {"success": True})
    assert record is not None and record.state is TaskState.SUCCEEDED


def test_atomic_claim_prevents_double_claim_after_dispatcher_restart(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_spec(task_id="task-atomic-123456"))
    restarted = _dispatcher(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(
            pool.map(lambda item: item.claim_next("builder-1"), (dispatcher, restarted))
        )
    assert sum(item is not None for item in claimed) == 1


def test_resource_lock_conflict_does_not_hold_other_builder(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    first = TaskSpec(
        **{
            **_spec(task_id="task-lock-a-123456").as_dict(),
            "resource_locks": ["resource:shared"],
        }
    )
    second = TaskSpec(
        task_id="task-lock-b-123456",
        builder_id="builder-2",
        objective="Inspect bounded authority evidence",
        branch="nightshift/builder-2/lock-test",
        task_type="authority_review",
        risk_class="read_only",
        resource_locks=("resource:shared",),
    )
    dispatcher.submit(first)
    dispatcher.submit(second)
    held = dispatcher.claim_next("builder-1")
    assert held is not None
    assert dispatcher.claim_next("builder-2") is None
    dispatcher.store.start_running(
        held.task_id, worker_id="builder-1", lease_generation=held.lease_generation
    )
    dispatcher.store.begin_verifying(
        held.task_id, worker_id="builder-1", lease_generation=held.lease_generation
    )
    dispatcher.complete(
        held.task_id,
        worker_instance_id="builder-1",
        lease_generation=held.lease_generation,
        execution=ExecutionResult(True),
    )
    released = dispatcher.claim_next("builder-2")
    assert released is not None


def test_code_change_template_is_pending_until_approved(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    record = dispatcher.submit_template(
        "builder-1.research-shadow-evidence",
        branch="nightshift/builder-1/approval-test",
        payload={"scope": "bounded shadow evidence"},
    )

    assert record.state is TaskState.PENDING_APPROVAL
    assert dispatcher.claim_next("builder-1") is None
    approved = dispatcher.approve(
        record.task_id, approver="philip", reason="scope checked"
    )
    assert approved.state is TaskState.QUEUED


def test_builder_five_is_never_a_worker_or_execution_target(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    with pytest.raises(DispatcherRecursionError):
        dispatcher.submit(_spec("builder-5"))
    with pytest.raises(DispatcherRecursionError):
        dispatcher.claim_next("builder-5")
    with pytest.raises(DispatcherRecursionError):
        BuilderRegistry.from_mapping(
            {
                "version": 1,
                "dispatcher": {"builder_id": "builder-5", "can_be_worker": False},
                "builders": [
                    {
                        "builder_id": "builder-5",
                        "display_name": "recursive",
                        "role": "invalid",
                        "repo_allowlist": ["Philip3006/sportsbrain"],
                        "branch_prefix": "nightshift/builder-5/",
                        "task_types": ["invalid"],
                    }
                ],
            }
        )


def test_unknown_builder_is_not_inferred_from_task_type(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    with pytest.raises(UnknownBuilderError) as caught:
        dispatcher.submit(_spec("builder-6"))
    assert "not in the governed registry" in str(caught.value)


def test_idempotency_replays_same_task_and_rejects_mutation(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    first = dispatcher.submit(
        TaskSpec(**{**_spec().as_dict(), "idempotency_key": "same-request"})
    )
    replay = dispatcher.submit(
        TaskSpec(
            **{
                **_spec().as_dict(),
                "idempotency_key": "same-request",
                "task_id": "task-replay-123456",
            }
        )
    )
    assert replay.task_id == first.task_id
    with pytest.raises(IdempotencyConflictError):
        dispatcher.submit(
            TaskSpec(
                **{
                    **_spec().as_dict(),
                    "idempotency_key": "same-request",
                    "objective": "changed",
                }
            )
        )


def test_lease_heartbeat_and_expiry_recovery(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    record = dispatcher.submit(_spec())
    leased = dispatcher.claim_next("builder-1", worker_instance_id="builder-1:runner-a")
    assert leased is not None
    assert (
        dispatcher.heartbeat(
            record.task_id,
            worker_instance_id="builder-1:runner-a",
            lease_generation=leased.lease_generation,
        ).lease_expires_at
        is not None
    )
    with pytest.raises(LeaseError):
        dispatcher.complete(
            record.task_id,
            worker_instance_id="builder-1:runner-b",
            execution=ExecutionResult(True),
        )

    # Use the store directly with a later timestamp so this test exercises
    # the lease reaper without making the production clock mutable.
    recovered = dispatcher.store.recover_expired(now=T0 + timedelta(seconds=31))
    assert recovered[0].state is TaskState.RETRY_WAIT
    assert recovered[0].lease_owner is None


def test_retry_budget_then_dead_letter(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    record = dispatcher.submit(_spec(task_id="task-retry-123456"))
    first = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(False, summary="transient")
    )
    assert first is not None and first.state is TaskState.RETRY_WAIT
    second = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(False, summary="still broken")
    )
    assert second is not None and second.state is TaskState.RETRY_WAIT
    # The default task budget is three; a third attempt becomes dead-lettered.
    third = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(False, summary="exhausted")
    )
    assert third is not None and third.state is TaskState.DEAD_LETTER
    assert dispatcher.store.get(record.task_id).attempt_count == 3


def test_non_retryable_failure_is_terminal_failed(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_spec(task_id="task-fail-123456"))
    record = dispatcher.run_once(
        "builder-1",
        lambda _: ExecutionResult(False, summary="bad input", retryable=False),
    )
    assert record is not None and record.state is TaskState.FAILED


def test_dependencies_wait_then_block_on_failed_dependency(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dependency = dispatcher.submit(_spec(task_id="task-dep-123456"))
    dependent = dispatcher.submit(
        TaskSpec(
            task_id="task-child-123456",
            builder_id="builder-1",
            objective="Inspect bounded evidence after prerequisite",
            branch="nightshift/builder-1/dependency-test",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
            dependency_ids=(dependency.task_id,),
        )
    )
    claimed = dispatcher.claim_next("builder-1")
    assert claimed is not None
    dispatcher.complete(
        dependency.task_id,
        worker_instance_id="builder-1",
        lease_generation=claimed.lease_generation,
        execution=ExecutionResult(False, "no evidence", retryable=False),
    )
    assert dispatcher.claim_next("builder-1") is None
    assert dispatcher.store.get(dependent.task_id).state is TaskState.BLOCKED


def test_blocked_task_can_only_be_released_after_dependencies_succeed(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path)
    dependency = dispatcher.submit(_spec(task_id="task-blocked-dep-123456"))
    dependent = dispatcher.submit(
        TaskSpec(
            task_id="task-blocked-child-123456",
            builder_id="builder-1",
            objective="Inspect bounded evidence after prerequisite",
            branch="nightshift/builder-1/unblock-test",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
            dependency_ids=(dependency.task_id,),
        )
    )
    claimed = dispatcher.claim_next("builder-1")
    assert claimed is not None
    dispatcher.complete(
        dependency.task_id,
        worker_instance_id="builder-1",
        lease_generation=claimed.lease_generation,
        execution=ExecutionResult(False, "blocked", retryable=False),
    )
    dispatcher.claim_next("builder-1")
    with pytest.raises(SafetyViolation):
        dispatcher.unblock(dependent.task_id, actor="operator")


def test_pause_is_durable_kill_switch(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_spec())
    dispatcher.pause(actor="operator")
    assert dispatcher.claim_next("builder-1") is None
    assert dispatcher.status()["paused"] is True
    dispatcher.resume(actor="operator")
    assert dispatcher.claim_next("builder-1") is not None


def test_policy_rejects_merge_deploy_and_external_side_effect_intent(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path)
    blocked_merge = dispatcher.submit(
        TaskSpec(
            builder_id="builder-1",
            objective="merge this change",
            branch="nightshift/builder-1/safety-test",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
        )
    )
    blocked_external = dispatcher.submit(
        TaskSpec(
            builder_id="builder-1",
            objective="call external provider",
            branch="nightshift/builder-1/safety-test-2",
            task_type="evidence_lifecycle_audit",
            risk_class="external_side_effect",
        )
    )
    assert blocked_merge.state is TaskState.BLOCKED
    assert blocked_external.state is TaskState.BLOCKED
    with pytest.raises(SafetyViolation):
        dispatcher.unblock(blocked_merge.task_id, actor="operator")


def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_spec())
    assert dispatcher.store.verify_audit_chain()
    with sqlite3.connect(dispatcher.store.path) as conn:
        conn.execute(
            "UPDATE audit_events SET details_json = ? WHERE event_id = 1",
            (json.dumps({"tampered": True}),),
        )
    with pytest.raises(AuditIntegrityError):
        dispatcher.store.verify_audit_chain()


def test_template_registry_rejects_unregistered_builder_template() -> None:
    registry = BuilderRegistry.from_file(CONFIG_DIR / "builders.json")
    templates = TemplateRegistry.from_mapping(
        {
            "version": 1,
            "templates": [
                {
                    "template_id": "builder-6.fake",
                    "builder_id": "builder-6",
                    "task_type": "fake",
                    "objective_template": "fake {scope}",
                    "required_payload_keys": ["scope"],
                    "allowed_payload_keys": ["scope"],
                    "risk_class": "read_only",
                }
            ],
        }
    )
    with pytest.raises(UnknownBuilderError):
        templates.validate_against(registry)
