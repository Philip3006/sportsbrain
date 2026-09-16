"""Audit-chain helpers mixed into the durable Night Shift store."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .models import AuditEvent, EventType, json_payload


class AuditIntegrityError(RuntimeError):
    """The append-only audit hash chain has been altered or is malformed."""


class AuditMixin:
    """Read and verify the audit stream owned by a DispatcherStore."""

    def _append_event(
        self,
        conn: Any,
        task_id: str | None,
        event_type: EventType | str,
        actor: str,
        created_at: str,
        from_state: str | None,
        to_state: str | None,
        details: Mapping[str, Any],
    ) -> None:
        previous = conn.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        event_value = (
            event_type.value if isinstance(event_type, EventType) else event_type
        )
        details_json = self._json(json_payload(details))
        body = self._json(
            {
                "task_id": task_id,
                "event_type": event_value,
                "actor": actor,
                "created_at": created_at,
                "from_state": from_state,
                "to_state": to_state,
                "details": json.loads(details_json),
            }
        )
        event_hash = hashlib.sha256(f"{previous_hash}:{body}".encode()).hexdigest()
        conn.execute(
            """INSERT INTO audit_events (
                task_id, event_type, actor, created_at, from_state, to_state,
                details_json, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                event_value,
                actor,
                created_at,
                from_state,
                to_state,
                details_json,
                previous_hash,
                event_hash,
            ),
        )
        conn.execute(
            "INSERT INTO dispatcher_meta(key, value) VALUES ('last_event_hash', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (event_hash,),
        )

    def audit_events(
        self, *, task_id: str | None = None, limit: int = 100
    ) -> list[AuditEvent]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._read() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_events WHERE task_id = ? ORDER BY event_id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            return [
                AuditEvent(
                    event_id=row["event_id"],
                    task_id=row["task_id"],
                    event_type=row["event_type"],
                    actor=row["actor"],
                    created_at=row["created_at"],
                    from_state=row["from_state"],
                    to_state=row["to_state"],
                    details=json.loads(row["details_json"]),
                    previous_hash=row["previous_hash"],
                    event_hash=row["event_hash"],
                )
                for row in rows
            ]

    def verify_audit_chain(self) -> bool:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY event_id"
            ).fetchall()
            stored_last = conn.execute(
                "SELECT value FROM dispatcher_meta WHERE key = 'last_event_hash'"
            ).fetchone()
        previous_hash = "0" * 64
        for row in rows:
            if row["previous_hash"] != previous_hash:
                raise AuditIntegrityError(
                    f"audit event {row['event_id']} has an invalid previous hash"
                )
            body = self._json(
                {
                    "task_id": row["task_id"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "created_at": row["created_at"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "details": json.loads(row["details_json"]),
                }
            )
            expected = hashlib.sha256(f"{previous_hash}:{body}".encode()).hexdigest()
            if row["event_hash"] != expected:
                raise AuditIntegrityError(
                    f"audit event {row['event_id']} has an invalid event hash"
                )
            previous_hash = expected
        if stored_last is None or stored_last[0] != previous_hash:
            raise AuditIntegrityError(
                "audit terminal hash does not match the event stream"
            )
        return True
