"""Serializable audit event model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    """One append-only, hash-chained audit event."""

    event_id: int
    task_id: str | None
    event_type: str
    actor: str
    created_at: str
    from_state: str | None
    to_state: str | None
    details: dict[str, Any]
    previous_hash: str
    event_hash: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["details"] = dict(self.details)
        return result
