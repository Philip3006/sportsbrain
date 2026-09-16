"""External append-only storage for durable real-shadow sessions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.football.top5_real_shadow_contracts import (
    REAL_SHADOW_SESSION_NAMESPACE,
    RealShadowContractError,
)
from src.football.top5_real_shadow_session import (
    RealShadowSession,
    RealShadowSessionStatus,
)
from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json


@dataclass(frozen=True)
class RealShadowSessionStore:
    """Atomic external store with immutable-core and append-only checks."""

    output_path: Path | None = None

    def path_for(self, session_id: str) -> Path:
        if self.output_path is not None:
            path = self.output_path
        else:
            path = runtime_state_path(f"{REAL_SHADOW_SESSION_NAMESPACE}{session_id}.json", require_external=True)
        active_root = Path(__file__).resolve().parents[2]
        resolved = path.expanduser().resolve()
        if resolved == active_root or active_root in resolved.parents:
            raise RealShadowContractError("real-shadow store cannot use the active checkout")
        if "offline_replay" in str(resolved) or "ledger" in str(resolved).lower():
            raise RealShadowContractError("real-shadow store cannot use offline or ledger paths")
        return path

    def save(self, session: RealShadowSession) -> Path:
        if session.fixture_mode and self.output_path is None:
            raise RealShadowContractError("test fixture sessions require an explicit test output path")
        path = self.path_for(session.session_id)
        if path.exists():
            existing = self.load(session.session_id)
            if existing.immutable_core() != session.immutable_core():
                raise RealShadowContractError("session immutable core cannot change on resume")
            if existing.status in {RealShadowSessionStatus.COMPLETE, RealShadowSessionStatus.FAILED_CLOSED} and existing.status is not session.status:
                raise RealShadowContractError("persisted terminal session status cannot regress")
            for name in ("observations", "predictions", "rejections", "results", "closings"):
                old = getattr(existing, name)
                new = getattr(session, name)
                if any(key not in new or new[key] != value for key, value in old.items()):
                    raise RealShadowContractError(f"session {name} cannot be removed or rewritten")
        session.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, session.as_payload())
        return path

    def load(self, session_id: str) -> RealShadowSession:
        path = self.path_for(session_id)
        if not path.is_file():
            raise RealShadowContractError("real-shadow session artifact is missing")
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RealShadowContractError("real-shadow session artifact is unreadable") from exc
        session = RealShadowSession.from_payload(payload)
        if session.session_id != session_id:
            raise RealShadowContractError("real-shadow session path identity does not match payload")
        return session


__all__ = ["RealShadowSessionStore"]
