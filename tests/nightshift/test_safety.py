from __future__ import annotations

from pathlib import Path

import pytest

from src.nightshift import NightShiftDispatcher, TaskSpec, TaskState


@pytest.mark.parametrize(
    "objective",
    [
        "PR merge",
        "merge to main",
        "production deploy",
        "Cloudflare deploy",
        "production activation",
        "Controlled Activation Run",
        "real Controlled Shadow provider execution",
        "provider monetary spend",
        "subscription billing change",
        "betting",
        "financial ledger mutation",
        "sealed-data access",
        "production model approval",
        "production Signal-Time approval",
        "force push",
        "rebase",
        "reset --hard",
    ],
)
def test_prohibited_autonomous_intent_is_blocked(
    tmp_path: Path, objective: str
) -> None:
    dispatcher = NightShiftDispatcher.from_config(
        state_path=tmp_path / "state.sqlite3",
        require_isolated_worktrees=False,
    )
    record = dispatcher.submit(
        TaskSpec(
            task_id=f"safety-{abs(hash(objective))}-01",
            builder_id="builder-1",
            objective=objective,
            branch="nightshift/builder-1/safety",
            task_type="evidence_lifecycle_audit",
            risk_class="read_only",
        )
    )
    assert record.state is TaskState.BLOCKED
    assert record.last_error and (
        "CEO_AUTHORIZATION_REQUIRED" in record.last_error
        or "PROHIBITED_AUTONOMOUS" in record.last_error
    )
