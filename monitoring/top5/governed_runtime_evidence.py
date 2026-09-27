"""Read-only governed Top-5 runtime evidence observation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from src.football.top5_activation_route_state import (
    DurableTop5ProductionRouteStateStore,
    Top5RouteStateError,
    TOP5_ROUTE_STATE_PATH,
    TOP5_ROUTE_STORE_SCHEMA,
)
from src.football.top5_durable_activation import _sha as canonical_sha
from src.football.top5_final_acceptance import MAX_EVIDENCE_AGE_SECONDS
from src.runtime.paths import ROOT, governed_runtime_root

ARTIFACT_SCHEMA = "top5-governed-runtime-evidence-v1"
STATE_SCHEMA = "top5-governed-runtime-state-v1"
ACTIVE_PROVIDER = "the_odds_api"
CANDIDATE_PROVIDER = "therundown_experimental"
STATE_RELATIVE_PATH = "football/top5/governed-runtime/runtime-state-v1.json"
MAX_STATE_BYTES = 64 * 1024
MAX_STATUS_BYTES = 16 * 1024
MAX_RUNTIME_STATE_AGE_SECONDS = MAX_EVIDENCE_AGE_SECONDS


class GovernedRuntimeEvidenceError(ValueError):
    """Raised internally when operator-owned evidence is malformed."""


def _blocked(reason: str) -> dict[str, object]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "status": "BLOCKED",
        "blockers": [reason[:256]],
    }


def _canonical_digest(payload: dict[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "artifact_digest"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def verify_artifact_digest(payload: dict[str, object]) -> bool:
    claimed = payload.get("artifact_digest")
    return isinstance(claimed, str) and claimed == _canonical_digest(payload)


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernedRuntimeEvidenceError(f"{name} is required")
    result = value.strip()
    if len(result) > max_length:
        raise GovernedRuntimeEvidenceError(f"{name} exceeds the bounded length")
    return result


def _sha_text(value: object, name: str, *, lengths: tuple[int, ...] = (40, 64)) -> str:
    result = _text(value, name, max_length=64).lower()
    if len(result) not in lengths or any(char not in "0123456789abcdef" for char in result):
        raise GovernedRuntimeEvidenceError(f"{name} is not a hexadecimal SHA")
    return result


def _absolute_path(value: object, name: str) -> Path:
    path = Path(_text(value, name)).expanduser()
    if not path.is_absolute():
        raise GovernedRuntimeEvidenceError(f"{name} must be absolute")
    return path.resolve()


def _utc_timestamp(value: object, name: str) -> tuple[str, datetime]:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernedRuntimeEvidenceError(f"{name} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernedRuntimeEvidenceError(f"{name} must be timezone-aware UTC")
    if parsed.utcoffset().total_seconds() != 0:
        raise GovernedRuntimeEvidenceError(f"{name} must be UTC")
    return text, parsed.astimezone(timezone.utc)


def _read_owner_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GovernedRuntimeEvidenceError(f"missing governed state: {path}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise GovernedRuntimeEvidenceError(f"governed state is not user-owned: {path}")
    if metadata.st_mode & 0o077 or metadata.st_size > MAX_STATE_BYTES:
        raise GovernedRuntimeEvidenceError(f"governed state permissions/size are unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernedRuntimeEvidenceError(f"governed state is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise GovernedRuntimeEvidenceError(f"governed state must be an object: {path}")
    return value


def _git_clean(path: Path) -> tuple[bool, str]:
    if not path.is_dir():
        return False, "workspace is missing"
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return False, f"bounded git status failed: {exc.__class__.__name__}"
    if result.returncode != 0:
        return False, "workspace is not a readable git checkout"
    if len(result.stdout.encode("utf-8")) > MAX_STATUS_BYTES:
        return False, "workspace status exceeds bounded output"
    return not result.stdout.strip(), "clean" if not result.stdout.strip() else "uncommitted changes"


def _read_route_state(runtime_root: Path) -> dict[str, object]:
    path = runtime_root / TOP5_ROUTE_STATE_PATH
    raw = _read_owner_file(path)
    expected = {
        "schema_version",
        "revision",
        "current_route",
        "records",
        "consumed_nonces",
        "audit_history",
        "state_digest",
    }
    if set(raw) != expected or raw.get("schema_version") != TOP5_ROUTE_STORE_SCHEMA:
        raise GovernedRuntimeEvidenceError("route state envelope is invalid")
    claimed = raw["state_digest"]
    body = {key: value for key, value in raw.items() if key != "state_digest"}
    if not isinstance(claimed, str) or canonical_sha(body) != claimed:
        raise GovernedRuntimeEvidenceError("route state digest is invalid")
    route = raw.get("current_route")
    try:
        DurableTop5ProductionRouteStateStore._validate_route(route)
    except (Top5RouteStateError, TypeError, ValueError) as exc:
        raise GovernedRuntimeEvidenceError("route state current route is invalid") from exc
    assert isinstance(route, dict)
    if route.get("provider_authority") != ACTIVE_PROVIDER:
        raise GovernedRuntimeEvidenceError("route state provider authority is not the_odds_api")
    return route


def _resolution(
    value: object, name: str, resolved_sha: str, state_path: Path
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GovernedRuntimeEvidenceError(f"{name} must explain SHA resolution")
    source = _text(value.get("source"), f"{name}.source")
    method = _text(value.get("method"), f"{name}.method")
    recorded = _sha_text(value.get("resolved_sha"), f"{name}.resolved_sha")
    if recorded != resolved_sha:
        raise GovernedRuntimeEvidenceError(f"{name}.resolved_sha does not match the observed SHA")
    if source != str(state_path):
        raise GovernedRuntimeEvidenceError(f"{name}.source is not the external operator record")
    return {"source": source, "method": method, "resolved_sha": recorded}


def observe_governed_runtime() -> dict[str, object]:
    """Return a fresh B1-compatible payload or a machine-readable BLOCKED result."""

    try:
        runtime_root = governed_runtime_root()
        state_path = runtime_root / STATE_RELATIVE_PATH
        state = _read_owner_file(state_path)
        if state.get("schema_version") != STATE_SCHEMA:
            raise GovernedRuntimeEvidenceError("governed runtime state schema is invalid")
        if state.get("runtime_root_role") != "governed-runtime":
            raise GovernedRuntimeEvidenceError("operator state does not identify a governed runtime")
        runtime_state_observed_at, observed_at = _utc_timestamp(
            state.get("observed_at"), "observed_at"
        )
        state_now = datetime.now(timezone.utc)
        if observed_at > state_now:
            raise GovernedRuntimeEvidenceError("underlying runtime state is from the future")
        if (state_now - observed_at).total_seconds() > MAX_RUNTIME_STATE_AGE_SECONDS:
            raise GovernedRuntimeEvidenceError("underlying runtime state is stale")

        source_release_sha = _sha_text(state.get("source_release_sha"), "source_release_sha")
        runtime_data_sha = _sha_text(state.get("runtime_data_sha"), "runtime_data_sha")
        source_resolution = _resolution(
            state.get("source_release_resolution"),
            "source_release_resolution",
            source_release_sha,
            state_path,
        )
        runtime_resolution = _resolution(
            state.get("runtime_data_resolution"),
            "runtime_data_resolution",
            runtime_data_sha,
            state_path,
        )
        order = state.get("active_provider_order")
        if (
            not isinstance(order, list)
            or not order
            or any(not isinstance(item, str) for item in order)
            or ACTIVE_PROVIDER not in order
            or CANDIDATE_PROVIDER in order
        ):
            raise GovernedRuntimeEvidenceError("active provider order is not governed")
        if state.get("provider_authority") != ACTIVE_PROVIDER:
            raise GovernedRuntimeEvidenceError("provider authority is not the_odds_api")
        if CANDIDATE_PROVIDER in json.dumps(order, sort_keys=True):
            raise GovernedRuntimeEvidenceError("candidate provider appears in active provider order")

        route = _read_route_state(runtime_root)
        if route.get("provider_authority") != state.get("provider_authority"):
            raise GovernedRuntimeEvidenceError("route and operator provider authority disagree")
        if route.get("activation_mode") != "DISABLED":
            raise GovernedRuntimeEvidenceError("observer refuses an active production route")

        publisher_root = _absolute_path(
            state.get("publisher_workspace_root"), "publisher_workspace_root"
        )
        if publisher_root == ROOT.resolve() or ROOT.resolve() in publisher_root.parents:
            raise GovernedRuntimeEvidenceError("publisher workspace must be external to checkout")
        checkout_clean, checkout_reason = _git_clean(ROOT.resolve())
        publisher_clean, publisher_reason = _git_clean(publisher_root)
        if not checkout_clean:
            raise GovernedRuntimeEvidenceError(f"checkout is dirty or unreadable: {checkout_reason}")
        if not publisher_clean:
            raise GovernedRuntimeEvidenceError(
                f"publisher workspace is dirty or unreadable: {publisher_reason}"
            )

        if state.get("no_bet") is not True:
            raise GovernedRuntimeEvidenceError("no_bet must be true")
        if state.get("publication_enabled") is not False:
            raise GovernedRuntimeEvidenceError("publication_enabled must be false")
        activation_state = _text(state.get("activation_state"), "activation_state")
        scheduler_state = _text(state.get("scheduler_state"), "scheduler_state")
        if activation_state not in {"DISABLED", "SHADOW"}:
            raise GovernedRuntimeEvidenceError("activation state is not read-only")
        if scheduler_state not in {"disabled", "inactive", "not_registered", "paused"}:
            raise GovernedRuntimeEvidenceError("scheduler state is not read-only")
        health_authority = _text(state.get("health_authority"), "health_authority")
        health_status = _text(state.get("health_status"), "health_status")
        health_source = _text(state.get("health_source"), "health_source")
        if health_authority != "governed" or health_status not in {"ok", "degraded"}:
            raise GovernedRuntimeEvidenceError("governed health evidence is invalid")

        captured_at_dt = datetime.now(timezone.utc)
        if captured_at_dt < observed_at:
            raise GovernedRuntimeEvidenceError("evidence capture predates runtime observation")
        captured_at = captured_at_dt.isoformat()
        payload: dict[str, object] = {
            "schema_version": ARTIFACT_SCHEMA,
            "status": "READY",
            "runtime_root": str(runtime_root),
            "runtime_root_role": state["runtime_root_role"],
            "source_release_sha": source_release_sha,
            "source_release_resolution": source_resolution,
            "runtime_data_sha": runtime_data_sha,
            "runtime_data_resolution": runtime_resolution,
            "runtime_state_observed_at": runtime_state_observed_at,
            "active_provider_order": list(order),
            "provider_authority": ACTIVE_PROVIDER,
            "checkout_clean": True,
            "publisher_clean": True,
            "cleanliness_checks": {
                "checkout": {
                    "root": str(ROOT.resolve()),
                    "method": "git status --porcelain=v1",
                    "result": checkout_reason,
                },
                "publisher": {
                    "root": str(publisher_root),
                    "method": "git status --porcelain=v1",
                    "result": publisher_reason,
                },
            },
            "health_authority": health_authority,
            "health_source": health_source,
            "health_status": health_status,
            "captured_at": captured_at,
            "no_bet": True,
            "publication_enabled": False,
            "activation_state": activation_state,
            "scheduler_state": scheduler_state,
        }
        payload["artifact_digest"] = _canonical_digest(payload)
        return payload
    except (OSError, GovernedRuntimeEvidenceError, RuntimeError) as exc:
        return _blocked(str(exc))


__all__ = ["ARTIFACT_SCHEMA", "observe_governed_runtime", "verify_artifact_digest"]
