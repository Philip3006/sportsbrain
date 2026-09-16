"""Queue control and health helpers mixed into the durable store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import EventType, TaskState, isoformat, utc_now


class QueueControlMixin:
    """Durable pause control and compact queue health reporting."""

    def count_pending(self) -> int:
        with self._read() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE state IN ('BACKLOG', 'READY', 'WAITING_DEPENDENCY', 'CLAIMED', 'RUNNING', 'VERIFYING', 'BLOCKED')"
                ).fetchone()[0]
            )

    def is_paused(self) -> bool:
        with self._read() as conn:
            return (
                conn.execute(
                    "SELECT value FROM dispatcher_meta WHERE key = 'paused'"
                ).fetchone()[0]
                == "1"
            )

    def set_paused(
        self, paused: bool, *, actor: str, now: datetime | None = None
    ) -> None:
        timestamp = isoformat(now or utc_now())
        event = EventType.PAUSED if paused else EventType.RESUMED
        with self._write() as conn:
            conn.execute(
                "INSERT INTO dispatcher_meta(key, value) VALUES ('paused', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if paused else "0",),
            )
            self._append_event(
                conn, None, event, actor, timestamp, None, None, {"paused": paused}
            )

    def stats(self) -> dict[str, Any]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state"
            ).fetchall()
        counts = {state.value: 0 for state in TaskState}
        counts.update({row["state"]: row["count"] for row in rows})
        return {
            "paused": self.is_paused(),
            "total": sum(counts.values()),
            "by_state": counts,
        }
