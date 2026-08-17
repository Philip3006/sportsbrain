"""Release & Publication Provenance — TASK-P0B-004.

Canonical provenance layer that distinguishes:
  1. source_release_sha  — last commit that passed ci_gates (never moves on data-only commits)
  2. runtime_data_sha    — current HEAD (advances on data-only bot commits)
  3. built_at            — when this provenance was assembled (aggregate_health timestamp)

Source release truth is stored in docs/data/provenance_meta.json.
That file is only written by scripts/record_source_release.py, which is called
from ci_gates.yml after all gates pass on main.

Fail-closed contract:
  - If provenance_meta.json is missing or malformed → source_release_sha = null, error field set
  - If runtime_data_sha cannot be resolved → null, error field set
  - Never substitute HEAD for source_release_sha silently
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
PROVENANCE_META_PATH = ROOT / "docs" / "data" / "provenance_meta.json"

SCHEMA_VERSION = "1"

# Allowlist of CI statuses that constitute a valid source release
_VALID_CI_STATUSES = frozenset({"success"})


def _load_provenance_meta() -> tuple[dict[str, Any] | None, str | None]:
    """Returns (meta_dict, error_str). meta_dict is None on failure."""
    if not PROVENANCE_META_PATH.exists():
        return None, "provenance_meta.json not found — no source release recorded yet"
    try:
        raw = PROVENANCE_META_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"provenance_meta.json unreadable or malformed: {exc}"
    if not isinstance(data, dict):
        return None, "provenance_meta.json is not a JSON object"
    sha = data.get("source_release_sha")
    if not isinstance(sha, str) or not sha:
        return None, "provenance_meta.json missing or empty source_release_sha"
    ci = data.get("source_ci", {})
    ci_status = ci.get("status") if isinstance(ci, dict) else None
    if ci_status not in _VALID_CI_STATUSES:
        return None, (
            f"provenance_meta.json source_ci.status={ci_status!r} is not in "
            f"valid set {sorted(_VALID_CI_STATUSES)}"
        )
    return data, None


def _resolve_runtime_data_sha() -> tuple[str | None, str | None]:
    """Returns (sha, error_str). Reads GITHUB_SHA env or falls back to git."""
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha and len(sha) >= 7:
        return sha, None
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            s = result.stdout.strip()
            if s:
                return s, None
        return None, f"git rev-parse failed: {result.stderr.strip()}"
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return None, f"git rev-parse error: {exc}"


def build_provenance(built_at: str | None = None) -> dict[str, Any]:
    """Assemble the provenance object for embedding in health.json.

    Returns a dict with schema_version, source_release_sha, source_ci,
    runtime_data_sha, and built_at. Fields are null with an error sub-key
    when the corresponding truth cannot be established (fail-closed).
    """
    if built_at is None:
        built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    meta, meta_err = _load_provenance_meta()
    runtime_sha, runtime_err = _resolve_runtime_data_sha()

    if meta is not None:
        source_release_sha = meta["source_release_sha"]
        ci_info = meta.get("source_ci", {})
        source_ci: dict[str, Any] = {
            "run_id": ci_info.get("run_id"),
            "workflow": ci_info.get("workflow"),
            "status": ci_info.get("status"),
        }
    else:
        source_release_sha = None
        source_ci = {"run_id": None, "workflow": None, "status": None}

    provenance: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_release_sha": source_release_sha,
        "source_ci": source_ci,
        "runtime_data_sha": runtime_sha,
        "built_at": built_at,
    }

    errors: list[str] = []
    if meta_err:
        errors.append(f"source_release: {meta_err}")
    if runtime_err:
        errors.append(f"runtime_data: {runtime_err}")
    if errors:
        provenance["errors"] = errors

    return provenance


def is_source_release_resolved(provenance: dict[str, Any]) -> bool:
    """True iff source_release_sha is a non-null, non-empty string."""
    sha = provenance.get("source_release_sha")
    return isinstance(sha, str) and bool(sha)
