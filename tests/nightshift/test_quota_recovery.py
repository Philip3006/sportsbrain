from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.nightshift import (
    ExecutionResult,
    NightShiftDispatcher,
    NightShiftNotificationWatcher,
    TaskSpec,
    TaskState,
    classify_quota_exhaustion,
)
from src.nightshift.models import isoformat

CONFIG_DIR = Path(__file__).parents[2] / "config" / "night_shift"
UTC = timezone.utc
T0 = datetime(2026, 9, 18, tzinfo=UTC)


def _dispatcher(tmp_path: Path, clock) -> NightShiftDispatcher:
    return NightShiftDispatcher.from_config(
        config_dir=CONFIG_DIR,
        state_path=tmp_path / "nightshift.sqlite3",
        require_isolated_worktrees=False,
        lease_seconds=30,
        retry_base_seconds=0,
        clock=clock,
    )


def _task(task_id: str, *, requires_pr: bool = False) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        builder_id="builder-1",
        objective="Inspect a bounded quota recovery case",
        branch=f"nightshift/builder-1/{task_id}",
        task_type="evidence_lifecycle_audit",
        risk_class="read_only",
        requires_approval=False,
        requires_pr=requires_pr,
    )


def _quota_result(*, reset_at: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        False,
        "AI usage limit reached; please wait for the provider reset",
        data={
            "failure_class": "QUOTA_EXHAUSTED",
            "provider": "codex",
            "quota_reset_at": reset_at,
        },
        failure_class="QUOTA_EXHAUSTED",
        quota_reset_at=reset_at,
    )


def test_quota_on_first_execution_is_persisted_without_consuming_attempts(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path, lambda: T0)
    task = dispatcher.submit(_task("quota-first-0001"))

    paused = dispatcher.run_once("builder-1", lambda _: _quota_result())

    assert paused is not None and paused.state is TaskState.PAUSED_QUOTA
    assert paused.attempt_count == 0
    assert paused.failure_repeat_count == 0
    assert paused.failure_class == "QUOTA_EXHAUSTED"
    assert paused.result and paused.result["data"]["quota_pause_count"] == 1
    assert task.task_id == paused.task_id
    assert "QUOTA_EXHAUSTED" in (paused.last_error or "")
    status = dispatcher.status()
    assert status["queue_mode"] == "PAUSED_QUOTA"
    assert status["operator"]["paused_quota"][0]["task_id"] == task.task_id


def test_repeated_quota_pause_stays_paused_without_dead_letter_inflation(
    tmp_path: Path,
) -> None:
    now = [T0]
    dispatcher = _dispatcher(tmp_path, lambda: now[0])
    task = dispatcher.submit(_task("quota-repeat-0001"))

    first = dispatcher.run_once("builder-1", lambda _: _quota_result())
    assert first is not None and first.state is TaskState.PAUSED_QUOTA
    now[0] = datetime.fromisoformat(first.available_at.replace("Z", "+00:00"))
    second = dispatcher.run_once("builder-1", lambda _: _quota_result())

    assert second is not None and second.state is TaskState.PAUSED_QUOTA
    assert second.attempt_count == 0
    assert second.failure_repeat_count == 0
    assert second.failure_class == "QUOTA_EXHAUSTED"
    assert second.result and second.result["data"]["quota_pause_count"] == 2
    assert dispatcher.store.get(task.task_id).state is TaskState.PAUSED_QUOTA


def test_quota_reset_timestamp_is_respected_and_resume_is_automatic(
    tmp_path: Path,
) -> None:
    now = [T0]
    dispatcher = _dispatcher(tmp_path, lambda: now[0])
    task = dispatcher.submit(_task("quota-reset-0001"))
    reset_at = isoformat(T0 + timedelta(hours=1))
    paused = dispatcher.run_once("builder-1", lambda _: _quota_result(reset_at=reset_at))
    assert paused is not None and paused.state is TaskState.PAUSED_QUOTA
    assert paused.available_at == reset_at

    now[0] = T0 + timedelta(minutes=30)
    assert dispatcher.claim_next("builder-1") is None
    now[0] = T0 + timedelta(hours=1, seconds=1)
    resumed = dispatcher.run_once("builder-1", lambda _: ExecutionResult(True, "resumed"))

    assert resumed is not None and resumed.state is TaskState.COMPLETED
    assert resumed.attempt_count == 1
    assert dispatcher.store.get(task.task_id).state is TaskState.COMPLETED


def test_quota_classifier_is_conservative_about_ordinary_errors() -> None:
    assert classify_quota_exhaustion("", "rate limit exceeded") is None
    assert classify_quota_exhaustion("", "pytest failed: disk quota exceeded") is None
    detection = classify_quota_exhaustion(
        "", "You've hit your usage limit; reset at 2026-09-18T01:00:00Z", now=T0
    )
    assert detection is not None
    assert detection.reset_at == "2026-09-18T01:00:00Z"


def test_quota_after_successful_delivery_preserves_pr_without_rerun(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path, lambda: T0)
    task = dispatcher.submit(_task("quota-delivery-0001", requires_pr=True))
    result = dispatcher.run_once(
        "builder-1",
        lambda _: ExecutionResult(
            False,
            "provider reset required after delivery",
            failure_class="QUOTA_EXHAUSTED",
            data={
                "failure_class": "QUOTA_EXHAUSTED",
                "commit_sha": "a" * 40,
                "remote_sha": "a" * 40,
                "pr_number": 123,
                "pr_url": "https://github.com/Philip3006/sportsbrain/pull/123",
                "verification_json": {"passed": True},
                "implementation_success": True,
            },
        ),
    )

    assert result is not None and result.state is TaskState.PR_READY
    assert result.failure_class == "QUOTA_EXHAUSTED"
    assert not result.delivery_blocked
    assert result.commit_sha == "a" * 40
    assert result.pr_number == 123
    assert dispatcher.claim_next("builder-1") is None
    assert dispatcher.store.get(task.task_id).state is TaskState.PR_READY


def test_historical_dead_letter_is_not_revived_by_quota_support(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, lambda: T0)
    task = dispatcher.submit(
        TaskSpec(**{**_task("quota-history-0001").as_dict(), "max_attempts": 1})
    )
    timeout = ExecutionResult(
        False,
        "worker runtime timeout",
        failure_class="TIMEOUT",
        timeout_signature="history-timeout",
    )
    dead = dispatcher.run_once("builder-1", lambda _: timeout)

    assert dead is not None and dead.state is TaskState.FAILED_SAFE
    assert dispatcher.claim_next("builder-1") is None
    assert dispatcher.store.get(task.task_id).state is TaskState.FAILED_SAFE


def test_independent_task_proceeds_while_another_task_is_quota_paused(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path, lambda: T0)
    paused_task = dispatcher.submit(_task("quota-independent-0001"))
    independent = dispatcher.submit(_task("quota-independent-0002"))
    paused = dispatcher.run_once("builder-1", lambda _: _quota_result())
    assert paused is not None and paused.task_id == paused_task.task_id

    completed = dispatcher.run_once("builder-1", lambda _: ExecutionResult(True, "independent"))

    assert completed is not None and completed.task_id == independent.task_id
    assert completed.state is TaskState.COMPLETED
    assert dispatcher.store.get(paused_task.task_id).state is TaskState.PAUSED_QUOTA


def test_quota_notification_sender_failure_is_fail_open(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, lambda: T0)
    task = dispatcher.submit(_task("quota-notify-0001"))
    dispatcher.run_once("builder-1", lambda _: _quota_result())
    sent: list[object] = []

    def failing_sender(notification: object) -> None:
        sent.append(notification)
        raise RuntimeError("osascript unavailable")

    watcher = NightShiftNotificationWatcher(
        dispatcher.store.path,
        seen_path=tmp_path / "notifications.json",
        sender=failing_sender,
    )
    notifications = watcher.poll()

    assert task.task_id == notifications[0].task_id
    assert notifications[0].state == "PAUSED_QUOTA"
    assert watcher.sender_failures == 1
    assert dispatcher.store.get(task.task_id).state is TaskState.PAUSED_QUOTA
