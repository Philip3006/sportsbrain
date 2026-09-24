from __future__ import annotations

import subprocess
from pathlib import Path

from src.nightshift import ExecutionResult, NightShiftDispatcher, TaskSpec, TaskState
from src.nightshift.verification import VerificationRunner

CONFIG_DIR = Path(__file__).parents[2] / "config" / "night_shift"


def _dispatcher(tmp_path: Path) -> NightShiftDispatcher:
    return NightShiftDispatcher.from_config(
        config_dir=CONFIG_DIR,
        state_path=tmp_path / "nightshift.sqlite3",
        require_isolated_worktrees=False,
    )


def test_dispatcher_is_separate_from_the_five_terminal_capacity_slots(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    assert dispatcher.dispatcher_id == "nightshift-dispatcher"
    assert "terminal-5" in dispatcher.registry.builder_ids
    assert dispatcher.dispatcher_id not in dispatcher.registry.builder_ids
    status = dispatcher.status()
    assert status["dispatcher_health"]["is_terminal_worker"] is False
    assert set(status["terminal_capacity"]) == set(dispatcher.registry.builder_ids)


def test_app_owner_and_execution_worker_are_persisted_separately(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit_template(
        "builder-4.provider-health-replay",
        branch="nightshift/builder-2/app-b4-offline",
        payload={"scope": "offline provider-health replay"},
        execution_worker="builder-2",
        app_owner="APP_B4",
    )
    assert task.app_owner == "APP_B4"
    assert task.execution_worker == "builder-2"
    assert task.builder_id == "builder-2"  # compatibility alias only
    event = dispatcher.store.audit_events(task_id=task.task_id, limit=1)[0]
    assert event.details["app_owner"] == "APP_B4"
    assert event.details["execution_worker"] == "builder-2"


def test_terminal_b4_number_does_not_grant_real_provider_authority(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            builder_id="builder-4",
            objective="Use the real provider and credentials",
            branch="nightshift/builder-4/real-provider-gate",
            task_type="provider_health_replay",
            risk_class="read_only",
            required_capabilities=("integration_review",),
            authority_requirements=("APP_B4_REAL_PROVIDER",),
        )
    )
    assert task.state is TaskState.BLOCKED
    assert task.last_error and task.last_error.startswith("CEO_AUTHORIZATION_REQUIRED")


def test_terminal_builder_five_can_run_a_normal_safe_task(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(
        TaskSpec(
            builder_id="terminal-5",
            objective="Run a bounded integration review",
            branch="nightshift/terminal-5/integration-review",
            task_type="authority_review",
            risk_class="read_only",
            required_capabilities=("integration_review",),
            requires_pr=False,
        )
    )
    claimed = dispatcher.claim_next("terminal-5")
    assert claimed is not None
    assert claimed.execution_worker == "terminal-5"
    dispatcher.store.start_running(
        task.task_id,
        worker_id=claimed.lease_owner or "terminal-5",
        lease_generation=claimed.lease_generation,
    )
    dispatcher.store.begin_verifying(
        task.task_id,
        worker_id=claimed.lease_owner or "terminal-5",
        lease_generation=claimed.lease_generation,
    )
    completed = dispatcher.complete(
        task.task_id,
        worker_instance_id=claimed.lease_owner or "terminal-5",
        lease_generation=claimed.lease_generation,
        execution=ExecutionResult(True, "safe test completed", retryable=False),
    )
    assert completed.state is TaskState.COMPLETED
    assert completed.pr_number is None


def test_verification_evidence_is_exact_sha_bound_and_reusable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "fixture"],
        check=True,
    )
    task = TaskSpec(
        builder_id="terminal-5",
        objective="Run exact SHA verification",
        branch="nightshift/terminal-5/verification",
        task_type="verification",
        risk_class="read_only",
        required_capabilities=("pytest",),
            verification_commands=(("python3", "-m", "compileall", "-q", "."),),
    )
    evidence = VerificationRunner().run(task, repo).as_dict()
    assert evidence["passed"] is True
    assert evidence["target_sha"]
    assert evidence["app_owner"] == "APP_B5"
    dispatcher = _dispatcher(tmp_path)
    saved = dispatcher.store.save_verification_evidence(
        repo="Philip3006/sportsbrain",
        target_sha=evidence["target_sha"],
        matrix_version=task.verification_matrix_version,
        app_owner=task.app_owner or "APP_B5",
        execution_worker=task.execution_worker or "terminal-5",
        evidence=evidence,
    )
    reused = dispatcher.store.get_verification_evidence(
        repo="Philip3006/sportsbrain",
        target_sha=evidence["target_sha"],
        matrix_version=task.verification_matrix_version,
    )
    assert reused == saved
    assert reused["target_sha"] == evidence["target_sha"]
