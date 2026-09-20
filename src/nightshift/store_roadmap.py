"""Durable roadmap selection metadata layered on the V1 task state machine."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .models import EventType, TaskState, isoformat, state_from_value, utc_now
from .roadmap import RoadmapRegistry
from .task_states import DEPENDENCY_SATISFIED_STATES


class StoreRoadmapMixin:
    """Persist explicit roadmap rows and bounded blocked-task re-eligibility."""

    def sync_roadmap(
        self, roadmap: RoadmapRegistry, *, now: datetime | None = None
    ) -> None:
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            for item in roadmap.items:
                conn.execute(
                    """INSERT INTO roadmap_items (
                        item_id, title, builder_id, template_id, payload_json,
                        dependency_item_ids_json, priority, status, next_eligible_at,
                        debug_budget, repeated_failure_limit, mode, enabled, generation,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        title = excluded.title, builder_id = excluded.builder_id,
                        template_id = excluded.template_id, payload_json = excluded.payload_json,
                        dependency_item_ids_json = excluded.dependency_item_ids_json,
                        priority = excluded.priority, debug_budget = excluded.debug_budget,
                        repeated_failure_limit = excluded.repeated_failure_limit,
                        mode = excluded.mode, enabled = excluded.enabled,
                        generation = excluded.generation, updated_at = excluded.updated_at""",
                    (
                        item.item_id,
                        item.title,
                        item.builder_id,
                        item.template_id,
                        self._json(item.payload),
                        self._json(list(item.dependency_item_ids)),
                        item.priority,
                        "PENDING" if item.enabled else "DISABLED",
                        timestamp,
                        item.debug_budget,
                        item.repeated_failure_limit,
                        item.mode,
                        int(item.enabled),
                        item.generation,
                        timestamp,
                    ),
                )

    def roadmap_records(self) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM roadmap_items ORDER BY priority DESC, item_id"
            ).fetchall()
        return [
            {
                "item_id": row["item_id"],
                "title": row["title"],
                "builder_id": row["builder_id"],
                "template_id": row["template_id"],
                "payload": json.loads(row["payload_json"]),
                "dependency_item_ids": json.loads(row["dependency_item_ids_json"]),
                "priority": row["priority"],
                "status": row["status"],
                "task_id": row["task_id"],
                "blocked_reason": row["blocked_reason"],
                "next_eligible_at": row["next_eligible_at"],
                "debug_budget": row["debug_budget"],
                "repeated_failure_limit": row["repeated_failure_limit"],
                "mode": row["mode"],
                "enabled": bool(row["enabled"]),
                "generation": row["generation"] or 1,
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_roadmap_status(
        self,
        item_id: str,
        *,
        status: str,
        task_id: str | None = None,
        reason: str | None = None,
        next_eligible_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        timestamp = isoformat(now or utc_now())
        allowed = {"PENDING", "ENQUEUED", "BLOCKED", "COMPLETED", "DISABLED"}
        if status not in allowed:
            raise ValueError(f"unknown roadmap status: {status}")
        with self._write() as conn:
            conn.execute(
                """UPDATE roadmap_items SET status = ?, task_id = COALESCE(?, task_id),
                   blocked_reason = ?, next_eligible_at = ?, updated_at = ? WHERE item_id = ?""",
                (
                    status,
                    task_id,
                    reason,
                    isoformat(next_eligible_at) if next_eligible_at else timestamp,
                    timestamp,
                    item_id,
                ),
            )

    def release_eligible_blocked(
        self, *, now: datetime | None = None, actor: str = "builder-5"
    ) -> list[str]:
        """Release only dependency/debug parking; policy blocks remain closed."""

        current = now or utc_now()
        timestamp = isoformat(current)
        released: list[str] = []
        with self._write() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE state = 'BLOCKED' AND available_at <= ?
                   AND (last_error LIKE 'dependency%' OR last_error LIKE 'AUTONOMOUS_DEBUG%')
                   ORDER BY task_id""",
                (timestamp,),
            ).fetchall()
            for row in rows:
                dependencies = tuple(json.loads(row["dependency_ids_json"]))
                if dependencies:
                    placeholders = ",".join("?" for _ in dependencies)
                    dep_rows = conn.execute(
                        f"SELECT task_id, state FROM tasks WHERE task_id IN ({placeholders})",
                        dependencies,
                    ).fetchall()
                    dep_states = {
                        dep["task_id"]: state_from_value(dep["state"])
                        for dep in dep_rows
                    }
                    if any(
                        dep_id not in dep_states
                        or dep_states[dep_id] not in DEPENDENCY_SATISFIED_STATES
                        for dep_id in dependencies
                    ):
                        continue
                if (
                    row["failure_class"] == "AUTONOMOUS_DEBUG_BUDGET_EXHAUSTED"
                    or row["debug_budget"] <= row["debug_attempt_count"]
                    and row["debug_budget"] > 0
                ):
                    continue
                conn.execute(
                    """UPDATE tasks SET state = 'READY', updated_at = ?, available_at = ?,
                       last_error = NULL WHERE task_id = ?""",
                    (timestamp, timestamp, row["task_id"]),
                )
                self._append_event(
                    conn,
                    row["task_id"],
                    EventType.RELEASED,
                    actor,
                    timestamp,
                    TaskState.BLOCKED.value,
                    TaskState.READY.value,
                    {"reason": "bounded blocker re-eligibility"},
                )
                released.append(row["task_id"])
        return released
