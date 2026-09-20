from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.nightshift import (
    DispatcherRecursionError,
    FakeExecutor,
    NightShiftDispatcher,
    TaskState,
)
from src.nightshift.roadmap import RoadmapRegistry
from src.nightshift.store import DispatcherStore

from .test_delivery import _fixture
from .test_merge_dependencies import _make_pr_ready


def _item(
    item_id: str,
    *,
    builder_id: str = "builder-1",
    template_id: str = "builder-1.evidence-lifecycle-audit",
    generation: int = 1,
    dependencies: tuple[str, ...] = (),
    priority: int = 10,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "title": item_id,
        "builder_id": builder_id,
        "template_id": template_id,
        "payload": {"scope": item_id},
        "dependency_item_ids": list(dependencies),
        "priority": priority,
        "generation": generation,
        "debug_budget": 1,
        "repeated_failure_limit": 2,
        "mode": "bounded",
        "enabled": True,
    }


def _roadmap(*items: dict[str, object]) -> RoadmapRegistry:
    return RoadmapRegistry.from_mapping(
        {
            "version": 1,
            "mode": "bounded",
            "max_cycles": 10,
            "merge_backpressure_limit": 3,
            "items": list(items),
        }
    )


def _use_roadmap(dispatcher: NightShiftDispatcher, roadmap: RoadmapRegistry) -> None:
    dispatcher.roadmap = roadmap
    dispatcher.merge_backpressure_limit = roadmap.merge_backpressure_limit
    dispatcher.store.sync_roadmap(roadmap)


def test_three_pr_ready_items_gate_code_but_allow_read_only_work(
    tmp_path: Path,
) -> None:
    dispatcher, _, base_sha, _ = _fixture(tmp_path)
    roadmap = _roadmap(
        _item(
            "rolling-code-stage-1",
            template_id="builder-1.research-shadow-evidence",
            priority=20,
        ),
        _item("rolling-audit-stage-1", priority=10),
    )
    _use_roadmap(dispatcher, roadmap)
    for index in range(3):
        _make_pr_ready(
            dispatcher,
            base_sha,
            task_id=f"pressure-parent-{index:04d}",
            branch=f"nightshift/builder-1/pressure-parent-{index}",
        )

    selected = dispatcher.select_next_roadmap_task(builder_id="builder-1")

    assert selected is not None
    assert selected.roadmap_item_id == "rolling-audit-stage-1"
    assert selected.risk_class.value == "read_only"
    status = dispatcher.status()
    assert status["queue_mode"] == "CONTINUOUS_AUTONOMOUS"
    assert status["operator"]["backpressure_mode"] == "SOFT"
    assert status["operator"]["safe_eligible_roadmap_items"] == 0
    assert status["operator"]["builders"]["builder-1"]["idle_reason"] == "eligible"
    code_summary = next(
        item for item in status["roadmap"] if item["item_id"] == "rolling-code-stage-1"
    )
    assert code_summary["merge_backpressure_blocked"] is False
    assert code_summary["soft_backpressure_preferred"] is True

    completed = dispatcher.run_once("builder-1", FakeExecutor())
    assert completed is not None and completed.state is TaskState.COMPLETED


def test_next_configured_generation_materializes_once_across_restart(
    tmp_path: Path,
) -> None:
    dispatcher, manager, _, _ = _fixture(tmp_path)
    roadmap = _roadmap(
        _item("rolling-stage-1", generation=1, priority=20),
        _item(
            "rolling-stage-2",
            generation=2,
            dependencies=("rolling-stage-1",),
            priority=10,
        ),
    )
    _use_roadmap(dispatcher, roadmap)

    first = dispatcher.select_next_roadmap_task(builder_id="builder-1")
    assert first is not None and first.roadmap_item_id == "rolling-stage-1"
    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    assert dispatcher.store.get_by_idempotency("roadmap:rolling-stage-1") == first
    assert dispatcher.run_once("builder-1", FakeExecutor()) is not None

    second = dispatcher.select_next_roadmap_task(builder_id="builder-1")
    assert second is not None and second.roadmap_item_id == "rolling-stage-2"
    assert (
        dispatcher.store.get_by_idempotency(
            "roadmap:rolling-stage-2:generation:2"
        )
        == second
    )
    assert len(
        [
            task
            for task in dispatcher.store.list_tasks()
            if task.roadmap_item_id == "rolling-stage-2"
        ]
    ) == 1

    restarted = NightShiftDispatcher.from_config(
        state_path=tmp_path / "runtime" / "state.sqlite3",
        worktree_manager=manager,
        roadmap=roadmap,
        lease_seconds=30,
        retry_base_seconds=0,
    )
    assert restarted.select_next_roadmap_task(builder_id="builder-1") is None
    assert len(
        [
            task
            for task in restarted.store.list_tasks()
            if task.roadmap_item_id == "rolling-stage-2"
        ]
    ) == 1


def test_rolling_roadmap_never_invents_a_generation(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    roadmap = _roadmap(_item("rolling-only-stage", generation=1))
    _use_roadmap(dispatcher, roadmap)

    first = dispatcher.select_next_roadmap_task(builder_id="builder-1")
    assert first is not None
    dispatcher.run_once("builder-1", FakeExecutor())

    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    assert [
        task.roadmap_item_id for task in dispatcher.store.list_tasks()
    ] == ["rolling-only-stage"]


def test_pause_and_drain_still_block_rolling_selection(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    roadmap = _roadmap(_item("rolling-pause-stage"))
    _use_roadmap(dispatcher, roadmap)

    dispatcher.store.set_paused(True, actor="test")
    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    dispatcher.store.set_paused(False, actor="test")
    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is not None

    dispatcher.store.set_draining(True, actor="test")
    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None


def test_dependency_blocking_remains_fail_closed(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    roadmap = _roadmap(
        {
            **_item("rolling-disabled-parent", priority=20),
            "enabled": False,
        },
        {
            **_item(
                "rolling-dependent-stage",
                dependencies=("rolling-disabled-parent",),
                priority=10,
            ),
        },
    )
    _use_roadmap(dispatcher, roadmap)

    assert dispatcher.select_next_roadmap_task(builder_id="builder-1") is None
    row = next(
        item
        for item in dispatcher.store.roadmap_records()
        if item["item_id"] == "rolling-dependent-stage"
    )
    assert row["status"] == "BLOCKED"
    assert row["blocked_reason"] == "dependency roadmap item incomplete"


def test_builder_5_cannot_select_rolling_work(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    with pytest.raises(DispatcherRecursionError):
        dispatcher.select_next_roadmap_task(builder_id="builder-5")


def test_static_roadmap_defaults_to_generation_one_and_migrates(tmp_path: Path) -> None:
    dispatcher, _, _, _ = _fixture(tmp_path)
    db = dispatcher.store.path
    legacy_item = _item("legacy-static-stage")
    legacy_item.pop("generation")
    parsed = RoadmapRegistry.from_mapping(
        {"version": 1, "items": [legacy_item]}
    ).items[0]
    assert parsed.generation == 1
    assert parsed.idempotency_key == "roadmap:legacy-static-stage"

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE roadmap_items RENAME TO roadmap_items_current")
    conn.execute(
        """CREATE TABLE roadmap_items (
            item_id TEXT PRIMARY KEY, title TEXT NOT NULL, builder_id TEXT NOT NULL,
            template_id TEXT NOT NULL, payload_json TEXT NOT NULL,
            dependency_item_ids_json TEXT NOT NULL DEFAULT '[]', priority INTEGER NOT NULL,
            status TEXT NOT NULL, task_id TEXT, blocked_reason TEXT,
            next_eligible_at TEXT NOT NULL, debug_budget INTEGER NOT NULL DEFAULT 0,
            repeated_failure_limit INTEGER NOT NULL DEFAULT 2,
            mode TEXT NOT NULL DEFAULT 'bounded', enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """INSERT INTO roadmap_items
            (item_id, title, builder_id, template_id, payload_json,
             dependency_item_ids_json, priority, status, task_id, blocked_reason,
             next_eligible_at, debug_budget, repeated_failure_limit, mode, enabled,
             updated_at)
            SELECT item_id, title, builder_id, template_id, payload_json,
                   dependency_item_ids_json, priority, status, task_id, blocked_reason,
                   next_eligible_at, debug_budget, repeated_failure_limit, mode, enabled,
                   updated_at
              FROM roadmap_items_current"""
    )
    conn.execute("DROP TABLE roadmap_items_current")
    conn.commit()
    conn.close()

    migrated = DispatcherStore(db)
    columns = {
        row[1]
        for row in sqlite3.connect(db).execute("PRAGMA table_info(roadmap_items)")
    }
    assert "generation" in columns
    assert all(row["generation"] == 1 for row in migrated.roadmap_records())
