from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.nightshift import (
    ConfigurationError,
    ExecutionResult,
    FakeExecutor,
    NightShiftDispatcher,
    SafetyViolation,
    TaskSpec,
    TaskState,
    UnknownBuilderError,
)
from src.nightshift.roadmap import RoadmapRegistry
from src.nightshift.worktree import WorktreeManager

from .test_dispatcher import _prepare_repo

CONFIG_DIR = Path(__file__).parents[2] / "config" / "night_shift"


def _dispatcher(tmp_path: Path, **kwargs: object) -> NightShiftDispatcher:
    repo = _prepare_repo(tmp_path)
    return NightShiftDispatcher.from_config(
        config_dir=CONFIG_DIR,
        state_path=tmp_path / "state.sqlite3",
        require_isolated_worktrees=False,
        clock=lambda: __import__("datetime").datetime(
            2026, 9, 16, tzinfo=__import__("datetime").timezone.utc
        ),
        worktree_manager=WorktreeManager(
            tmp_path / "runtime", {"Philip3006/sportsbrain": repo}
        ),
        retry_base_seconds=0,
        lease_seconds=30,
        **kwargs,
    )


def _debug_task(task_id: str, **kwargs: object) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        builder_id="builder-1",
        objective="Bounded debug fixture",
        branch=f"nightshift/builder-1/{task_id}",
        task_type="evidence_lifecycle_audit",
        risk_class="read_only",
        debug_budget=kwargs.pop("debug_budget", 3),
        repeated_failure_limit=kwargs.pop("repeated_failure_limit", 2),
        **kwargs,
    )


def test_roadmap_selection_is_explicit_and_registry_scoped(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    selected = dispatcher.select_next_roadmap_task(builder_id="builder-1")
    assert selected is not None
    assert selected.roadmap_item_id == "roadmap-b1-evidence"
    assert selected.builder_id == "builder-1"
    with pytest.raises(UnknownBuilderError):
        dispatcher.select_next_roadmap_task(builder_id="builder-6")
    results = dispatcher.run_autonomous_cycle("builder-1", FakeExecutor(), max_tasks=1)
    assert results and results[0].state is TaskState.COMPLETED
    assert any(
        item["item_id"] == "roadmap-b1-evidence" and item["status"] == "COMPLETED"
        for item in dispatcher.store.roadmap_records()
    )


def test_bounded_debug_repair_can_retest_and_succeed(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(_debug_task("debug-repair-0001"))
    first = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(False, "first diagnostic failure")
    )
    assert first is not None and first.state is TaskState.READY
    second = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(True, "repair verified")
    )
    assert second is not None and second.state is TaskState.COMPLETED
    assert dispatcher.store.get(task.task_id).debug_attempt_count == 1


def test_repeated_debug_failure_is_parked_and_not_spun(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(
        _debug_task("debug-repeat-0001", debug_budget=4, repeated_failure_limit=2)
    )
    dispatcher.run_once("builder-1", lambda _: ExecutionResult(False, "same failure"))
    parked = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(False, "same failure")
    )
    assert parked is not None and parked.state is TaskState.BLOCKED
    assert dispatcher.reevaluate_blocked() == []
    assert dispatcher.store.release_eligible_blocked(
        now=datetime(2026, 9, 16, tzinfo=timezone.utc) + timedelta(seconds=61)
    ) == [task.task_id]
    assert dispatcher.store.get(task.task_id).failure_class == (
        "AUTONOMOUS_DEBUG_REPEATED_FAILURE"
    )


def test_exhausted_debug_budget_cannot_reenter_queue(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    task = dispatcher.submit(_debug_task("debug-budget-0001", debug_budget=1))
    parked = dispatcher.run_once(
        "builder-1", lambda _: ExecutionResult(False, "bounded failure")
    )
    assert parked is not None and parked.state is TaskState.BLOCKED
    assert dispatcher.reevaluate_blocked() == []
    assert dispatcher.store.get(task.task_id).state is TaskState.BLOCKED


def test_roadmap_rejects_unknown_builder_and_cycle() -> None:
    with pytest.raises(ConfigurationError):
        RoadmapRegistry.from_mapping(
            {
                "version": 1,
                "items": [
                    {
                        "item_id": "roadmap-cycle-a",
                        "title": "a",
                        "builder_id": "builder-1",
                        "template_id": "builder-1.evidence-lifecycle-audit",
                        "payload": {"scope": "a"},
                        "dependency_item_ids": ["roadmap-cycle-b"],
                    },
                    {
                        "item_id": "roadmap-cycle-b",
                        "title": "b",
                        "builder_id": "builder-1",
                        "template_id": "builder-1.evidence-lifecycle-audit",
                        "payload": {"scope": "b"},
                        "dependency_item_ids": ["roadmap-cycle-a"],
                    },
                ],
            }
        )


def test_autonomous_debug_and_unlimited_modes_have_finite_bounds(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path)
    with pytest.raises(SafetyViolation):
        dispatcher.run_debug_loop("builder-1", FakeExecutor(), max_cycles=11)
    with pytest.raises(SafetyViolation):
        dispatcher.run_autonomous("builder-1", FakeExecutor(), max_cycles=101)
    with pytest.raises(SafetyViolation):
        dispatcher.run_autonomous("builder-1", FakeExecutor(), max_cycles=0)
