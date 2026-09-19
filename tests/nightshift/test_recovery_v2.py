from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.nightshift import (
    ControlRepoLock,
    ControlRepoLockTimeout,
    ExecutionResult,
    NightShiftDispatcher,
    NightShiftNotificationWatcher,
    SafetyViolation,
    TaskSpec,
    TaskState,
    verify_github_pull_request,
)

CONFIG_DIR = Path(__file__).parents[2] / "config" / "night_shift"
UTC = timezone.utc
T0 = datetime(2026, 9, 18, tzinfo=UTC)


def _dispatcher(tmp_path: Path) -> NightShiftDispatcher:
    return NightShiftDispatcher.from_config(
        config_dir=CONFIG_DIR,
        state_path=tmp_path / "nightshift.sqlite3",
        require_isolated_worktrees=False,
        lease_seconds=30,
        retry_base_seconds=0,
        clock=lambda: T0,
    )


def _task(task_id: str, *, builder_id: str = "builder-1") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        builder_id=builder_id,
        objective="Inspect a bounded Night Shift recovery case",
        branch=f"nightshift/{builder_id}/{task_id}",
        task_type="evidence_lifecycle_audit",
        risk_class="read_only",
    )


def test_template_runtime_policy_is_explicit(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    runtimes = {
        template.template_id: template.runtime_seconds
        for template in dispatcher.templates.templates
    }
    assert runtimes["builder-1.research-shadow-evidence"] == 3600
    assert runtimes["builder-2.independent-qualification"] == 3600
    assert runtimes["builder-1.evidence-lifecycle-audit"] == 1800
    assert runtimes["builder-4.provider-health-replay"] == 1800


def test_runtime_policy_has_a_3600_second_submission_ceiling(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    with pytest.raises(SafetyViolation, match="max_runtime_seconds"):
        dispatcher.submit(
            TaskSpec(
                **{
                    **_task("runtime-too-long-0001").as_dict(),
                    "max_runtime_seconds": 3601,
                }
            )
        )


def test_repeated_identical_timeout_is_parked(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(_task("timeout-parking-0001"))
    timeout = ExecutionResult(
        False,
        summary="worker runtime timeout",
        failure_class="TIMEOUT",
        timeout_signature="codex-timeout:1800",
    )
    first = dispatcher.run_once("builder-1", lambda _: timeout)
    second = dispatcher.run_once("builder-1", lambda _: timeout)
    assert first is not None and first.state is TaskState.READY
    assert second is not None and second.state is TaskState.FAILED_SAFE
    assert second.failure_class == "REPEATED_TIMEOUT"
    assert second.attempt_count == 2
    assert second.result and second.result["timeout_signature"] == "codex-timeout:1800"
    assert dispatcher.store.get(task.task_id).state is TaskState.FAILED_SAFE


def test_delivery_block_preserves_successful_work(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_task("delivery-preserve-0001"))
    result = dispatcher.run_once(
        "builder-1",
        lambda _: ExecutionResult(
            True,
            "implementation verified; delivery base drifted",
            data={
                "delivery_status": "blocked",
                "commit_sha": "abc123",
                "remote_sha": "abc123",
                "pr_number": 89,
                "pr_url": "https://github.com/Philip3006/sportsbrain/pull/89",
                "verification_json": {"compileall": "passed"},
            },
        ),
    )
    assert result is not None and result.state is TaskState.PR_READY
    assert result.commit_sha == "abc123"
    assert result.remote_sha == "abc123"
    assert result.pr_number == 89
    assert result.verification_json == {"compileall": "passed"}
    assert dispatcher.claim_next("builder-1") is None
    assert dispatcher.store.verify_audit_chain()


def test_failed_delivery_is_reconciled_without_rerunning_worker(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(_task("delivery-reconcile-0001"))
    failed = dispatcher.run_once(
        "builder-1",
        lambda _: ExecutionResult(
            False,
            "pull request base does not match authoritative base",
            retryable=False,
            failure_class="DELIVERY_FAILED",
            data={
                "delivery_status": "blocked",
                "implementation_success": True,
                "commit_sha": "def456",
                "remote_sha": "def456",
                "pr_number": 86,
                "verification_json": {"compileall": "passed"},
            },
        ),
    )
    assert failed is not None and failed.state is TaskState.DELIVERY_FAILED
    calls: list[str] = []
    reconciled = dispatcher.reconcile_delivery(
        task.task_id,
        actor="operator",
        facts={
            "verified": True,
            "worker_execution_success": True,
            "implementation_success": True,
            "commit_sha": "def456",
            "remote_sha": "def456",
            "pr_number": 86,
            "verification_json": {"compileall": "passed"},
        },
    )
    calls.append(reconciled.task_id)
    assert calls == [task.task_id]
    assert reconciled.state is TaskState.PR_READY
    assert reconciled.attempt_count == 1
    assert dispatcher.claim_next("builder-1") is None


def test_timeout_dead_letter_cannot_be_reconciled_as_delivery(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(_task("timeout-no-reconcile-0001"))
    timeout = ExecutionResult(
        False,
        "timeout",
        failure_class="TIMEOUT",
        timeout_signature="same-timeout",
    )
    dispatcher.run_once("builder-1", lambda _: timeout)
    dispatcher.run_once("builder-1", lambda _: timeout)
    with pytest.raises(ValueError, match="timeout/dead-letter"):
        dispatcher.reconcile_delivery(
            task.task_id,
            actor="operator",
            facts={
                "verified": True,
                "worker_execution_success": True,
                "implementation_success": True,
                "commit_sha": "bad",
                "remote_sha": "bad",
                "pr_number": 1,
                "verification_json": {"compileall": "passed"},
            },
        )


def test_notification_dry_run_deduplicates_and_is_fail_open(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.submit(_task("notification-dry-run-0001"))
    dispatcher.run_once("builder-1", lambda _: ExecutionResult(True, "completed"))
    sent: list[object] = []
    seen = tmp_path / "runtime-state" / "notifications.json"
    watcher = NightShiftNotificationWatcher(
        dispatcher.store.path, seen_path=seen, sender=sent.append
    )
    preview = watcher.poll(dry_run=True)
    assert [item.state for item in preview] == ["COMPLETED"]
    assert not seen.exists()
    assert [item.state for item in watcher.poll()] == ["COMPLETED"]
    assert watcher.poll() == []
    assert len(sent) == 1
    assert dispatcher.store.get("notification-dry-run-0001").state is TaskState.COMPLETED


def test_status_reports_dead_pid_without_payloads(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(_task("dead-pid-status-0001"))
    dispatcher.claim_next("builder-1", process_id=999_999_999)
    status = dispatcher.status()
    assert task.task_id in {item["task_id"] for item in status["operator"]["dead_pid"]}
    assert status["operator"]["next_eligible_explicit_task"]["item_id"] == "roadmap-b1-research-1"
    assert all("payload" not in item for item in status["roadmap"])


def _lock_worker(lock_path: str, started: str, release: object, acquired: str) -> None:
    with ControlRepoLock(Path(lock_path), timeout_seconds=3):
        Path(started).write_text("started", encoding="utf-8")
        release.wait(3)  # type: ignore[attr-defined]
        Path(acquired).write_text("acquired", encoding="utf-8")


def test_control_repo_lock_serializes_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    lock = tmp_path / "control-repo.lock"
    release = context.Event()
    first_started = tmp_path / "first.started"
    first_acquired = tmp_path / "first.acquired"
    second_started = tmp_path / "second.started"
    second_acquired = tmp_path / "second.acquired"
    first = context.Process(
        target=_lock_worker,
        args=(str(lock), str(first_started), release, str(first_acquired)),
    )
    second = context.Process(
        target=_lock_worker,
        args=(str(lock), str(second_started), release, str(second_acquired)),
    )
    first.start()
    assert _wait_for(first_started)
    second.start()
    time.sleep(0.15)
    assert not second_started.exists()
    release.set()
    first.join(4)
    second.join(4)
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert second_started.exists() and second_acquired.exists()


def test_control_repo_lock_has_bounded_failure(tmp_path: Path) -> None:
    lock = tmp_path / "control-repo.lock"
    with ControlRepoLock(lock, timeout_seconds=0):
        context = multiprocessing.get_context("fork")
        queue = context.Queue()

        def contender(output: object) -> None:
            try:
                with ControlRepoLock(lock, timeout_seconds=0.05):
                    output.put("acquired")  # type: ignore[attr-defined]
            except ControlRepoLockTimeout:
                output.put("timed_out")  # type: ignore[attr-defined]

        process = context.Process(target=contender, args=(queue,))
        process.start()
        process.join(2)
        assert queue.get(timeout=1) == "timed_out"
        assert process.exitcode == 0


def _wait_for(path: Path, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def test_github_recovery_verifier_is_read_only_and_checks_binding() -> None:
    class Result:
        stdout = (
            '{"state":"open","html_url":"https://github.com/Philip3006/sportsbrain/pull/89",'
            '"head":{"sha":"abc123","ref":"nightshift/builder-2/recover"},'
            '"base":{"ref":"main","sha":"base123","repo":{"full_name":"Philip3006/sportsbrain"}}}'
        )

    calls: list[list[str]] = []

    def runner(command: list[str], **_: object) -> Result:
        calls.append(command)
        return Result()

    facts = verify_github_pull_request(
        "Philip3006/sportsbrain",
        89,
        expected_commit_sha="abc123",
        expected_branch="nightshift/builder-2/recover",
        runner=runner,
    )
    assert facts["verified"] is True
    assert facts["pr_number"] == 89
    assert facts["base_sha"] == "base123"
    assert calls == [["gh", "api", "repos/Philip3006/sportsbrain/pulls/89"]]
