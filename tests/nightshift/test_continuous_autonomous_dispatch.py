from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.nightshift import (
    FakeExecutor,
    NightShiftDispatcher,
    RiskClass,
    TaskState,
    summarize_pull_requests,
)
from src.nightshift.audit import AuditIntegrityError
from src.nightshift.roadmap import RoadmapRegistry

from .test_delivery import _fixture
from .test_merge_dependencies import _make_pr_ready


def _item(
    item_id: str,
    *,
    builder_id: str = "builder-1",
    template_id: str = "builder-1.evidence-lifecycle-audit",
    priority: int = 10,
    dependencies: tuple[str, ...] = (),
    governed_paths: tuple[str, ...] = (),
    resource_locks: tuple[str, ...] = (),
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "title": item_id,
        "builder_id": builder_id,
        "template_id": template_id,
        "payload": {"scope": item_id},
        "dependency_item_ids": list(dependencies),
        "priority": priority,
        "governed_paths": list(governed_paths),
        "resource_locks": list(resource_locks),
        "debug_budget": 1,
        "repeated_failure_limit": 2,
        "mode": "bounded",
        "enabled": enabled,
    }


def _roadmap(*items: dict[str, object], threshold: int = 12) -> RoadmapRegistry:
    return RoadmapRegistry.from_mapping(
        {
            "version": 1,
            "mode": "bounded",
            "max_cycles": 10,
            "merge_backpressure_limit": threshold,
            "items": list(items),
        }
    )


def _use_roadmap(dispatcher: NightShiftDispatcher, roadmap: RoadmapRegistry) -> None:
    dispatcher.roadmap = roadmap
    dispatcher.merge_backpressure_limit = roadmap.merge_backpressure_limit
    dispatcher.store.sync_roadmap(roadmap)


def test_three_unrelated_prs_do_not_stop_dispatch(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    for index in range(3):
        _make_pr_ready(
            dispatcher,
            base_sha,
            task_id=f"soft-pressure-{index:04d}",
            branch=f"nightshift/builder-1/soft-pressure-{index}",
        )

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-3")

    assert selected is not None
    assert dispatcher.status()["operator"]["backpressure_is_hard"] is False
    assert dispatcher.status()["queue_mode"] == "CONTINUOUS_AUTONOMOUS"


def test_twelve_unrelated_prs_still_allow_safe_non_overlapping_work(
    tmp_path: Path,
) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    dispatcher.merge_backpressure_limit = 12
    for index in range(12):
        _make_pr_ready(
            dispatcher,
            base_sha,
            task_id=f"soft-pressure-twelve-{index:04d}",
            branch=f"nightshift/builder-1/soft-pressure-twelve-{index}",
        )

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-3")

    assert selected is not None
    assert dispatcher.status()["operator"]["active_substantive_pr_count"] == 12


def test_blocked_frontier_item_does_not_stop_another_same_builder_item(
    tmp_path: Path,
) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    roadmap = _roadmap(
        _item("disabled-parent-0001", enabled=False, priority=30),
        _item(
            "dependency-blocked-01",
            dependencies=("disabled-parent-0001",),
            priority=20,
        ),
        _item("same-builder-safe-01", priority=10),
    )
    _use_roadmap(dispatcher, roadmap)

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-1")

    assert selected is not None
    assert selected.roadmap_item_id == "same-builder-safe-01"
    blocked = next(
        item
        for item in dispatcher.store.roadmap_records()
        if item["item_id"] == "dependency-blocked-01"
    )
    assert blocked["status"] == "BLOCKED"
    assert blocked["skip_reason"] == "dependency roadmap item incomplete"


def test_conflict_on_one_lane_does_not_stop_unrelated_builder_lane(
    tmp_path: Path,
) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    _make_pr_ready(dispatcher, base_sha, task_id="conflict-pr-0001")
    roadmap = _roadmap(
        _item(
            "overlap-stage-01",
            priority=30,
            governed_paths=("artifacts",),
        ),
        _item(
            "unrelated-builder-stage",
            builder_id="builder-2",
            template_id="builder-2.authority-review",
            governed_paths=("docs",),
            priority=20,
        ),
    )
    _use_roadmap(dispatcher, roadmap)

    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    selected = dispatcher.select_next_roadmap_task(builder_id="builder-2")

    assert selected is not None
    assert selected.roadmap_item_id == "unrelated-builder-stage"
    overlap = next(
        item
        for item in dispatcher.store.roadmap_records()
        if item["item_id"] == "overlap-stage-01"
    )
    assert overlap["skip_reason"].startswith("open_pr_overlap:")


def test_same_file_overlap_blocks_x_but_not_unrelated_y(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    _make_pr_ready(dispatcher, base_sha, task_id="path-pr-0001")
    roadmap = _roadmap(
        _item("same-path-stage", governed_paths=("artifacts",), priority=20),
        _item("different-path-stage", governed_paths=("docs",), priority=10),
    )
    _use_roadmap(dispatcher, roadmap)

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-1")

    assert selected is not None
    assert selected.roadmap_item_id == "different-path-stage"


def test_non_substantive_pr_classes_do_not_count_as_active_pressure(
    tmp_path: Path,
) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    active = _make_pr_ready(dispatcher, base_sha, task_id="class-active-0001")
    records = [
        active,
        replace(active, task_id="class-recovery-0001", branch="nightshift/recovery/class-recovery", reconciliation={"attempts": 1}),
        replace(active, task_id="class-stale-00001", failure_class="STALE_PR"),
        replace(active, task_id="class-super-00001", failure_class="SUPERSEDED_PR"),
        replace(active, task_id="class-readonly-001", risk_class=RiskClass.READ_ONLY),
        replace(active, task_id="class-review-0001", state=TaskState.CEO_REVIEW),
        replace(active, task_id="class-conflict-01", failure_class="SEMANTIC_CONFLICT"),
    ]

    counts = summarize_pull_requests(records)

    assert counts["ACTIVE_SUBSTANTIVE"] == 1
    assert counts["RECOVERY"] == 1
    assert counts["STALE"] == 1
    assert counts["SUPERSEDED"] == 1
    assert counts["READ_ONLY_DOCS"] == 1
    assert counts["WAITING_REVIEW"] == 1
    assert counts["CONFLICTED"] == 1
    assert counts["active_substantive_pr_count"] == 3


def test_unchanged_blocker_is_persisted_once_without_busy_loop(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    _make_pr_ready(dispatcher, base_sha, task_id="busy-loop-pr-0001")
    roadmap = _roadmap(
        _item("busy-loop-overlap", governed_paths=("artifacts",)),
    )
    _use_roadmap(dispatcher, roadmap)

    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    first = next(
        item
        for item in dispatcher.store.roadmap_records()
        if item["item_id"] == "busy-loop-overlap"
    )
    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    second = next(
        item
        for item in dispatcher.store.roadmap_records()
        if item["item_id"] == "busy-loop-overlap"
    )

    assert first["skip_count"] == second["skip_count"] == 1
    assert len(
        [
            event
            for event in dispatcher.store.audit_events(limit=1000)
            if event.event_type == "roadmap_skipped"
        ]
    ) == 1


def test_no_safe_frontier_reports_truthful_global_idle(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    roadmap = _roadmap(_item("disabled-only-stage", enabled=False))
    _use_roadmap(dispatcher, roadmap)
    for item in dispatcher.store.roadmap_records():
        dispatcher.store.set_roadmap_status(item["item_id"], status="DISABLED")

    status = dispatcher.status()

    assert status["queue_mode"] in {"GLOBAL_IDLE", "INTENTIONAL_IDLE"}
    assert status["operator"]["continuous_mode"] == "GLOBAL_IDLE"
    assert status["operator"]["global_idle_reason"] == "roadmap_exhausted"


def test_explicit_pause_still_stops_dispatch(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    dispatcher.pause(actor="operator")

    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    assert dispatcher.status()["queue_mode"] == "PAUSED"


def test_audit_integrity_failure_remains_a_global_safety_stop(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    dispatcher.submit(
        dispatcher.templates.resolve("builder-1.evidence-lifecycle-audit").instantiate(
            registry=dispatcher.registry,
            branch="nightshift/builder-1/audit-integrity-stop",
            payload={"scope": "audit integrity stop"},
        )
    )
    with dispatcher.store._write() as conn:
        conn.execute(
            "UPDATE audit_events SET details_json = ? WHERE event_id = (SELECT MAX(event_id) FROM audit_events)",
            ('{"tampered":true}',),
        )

    with pytest.raises(AuditIntegrityError):
        dispatcher.store.verify_audit_chain()


def test_safe_task_can_run_after_soft_pressure(tmp_path: Path) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    dispatcher.merge_backpressure_limit = 1
    _make_pr_ready(dispatcher, base_sha, task_id="safe-pressure-pr-0001")
    roadmap = _roadmap(
        _item("safe-pressure-stage", template_id="builder-1.evidence-lifecycle-audit"),
        threshold=1,
    )
    _use_roadmap(dispatcher, roadmap)

    task = dispatcher.select_next_roadmap_task(builder_id="builder-1")
    assert task is not None
    assert dispatcher.run_once("builder-1", FakeExecutor()) is not None
