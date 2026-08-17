"""Release & Publication Provenance — TASK-P0B-004.

Canonical provenance layer that distinguishes:
  1. source_release_sha        — last commit that passed ci_gates (never moves on data-only commits)
  2. runtime_data_sha          — current HEAD (advances on data-only bot commits)
  3. source_runtime_consistent — True = only data-only commits between source and runtime;
                                  False = source-changing commits detected (mismatch);
                                  None = cannot determine (fail-closed)
  4. built_at                  — when this provenance was assembled (aggregate_health timestamp)

Source release truth is stored in docs/data/provenance_meta.json.
That file is only written by scripts/record_source_release.py, which is called
from ci_gates.yml after all gates pass on main.

Fail-closed contract:
  - Missing/malformed/CI-status-invalid provenance_meta.json → source_release_sha = null
  - source_ci.head_sha != source_release_sha → fail closed (C1 mismatch guard)
  - source_runtime_consistent = None (cannot classify) → is_source_release_resolved() = False
  - source_runtime_consistent = False (source-changing commits detected) → fail closed
  - Only source_runtime_consistent = True qualifies as fully resolved
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

# Canonical source-changing paths — MUST match ci_gates.yml paths filter exactly.
# These are the paths that trigger ci_gates when changed. Any commit touching these
# is a source-changing commit and requires a new source release record.
_SOURCE_PATHS = (
    "src/",
    "scripts/",
    "tests/",
    "requirements.txt",
    ".github/workflows/",
)


def _load_provenance_meta() -> tuple[dict[str, Any] | None, str | None]:
    """Returns (meta_dict, error_str). meta_dict is None on failure (fail-closed)."""
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
    if not isinstance(ci, dict):
        return None, "provenance_meta.json source_ci is not an object"
    ci_status = ci.get("status")
    if ci_status not in _VALID_CI_STATUSES:
        return None, (
            f"provenance_meta.json source_ci.status={ci_status!r} is not in "
            f"valid set {sorted(_VALID_CI_STATUSES)}"
        )
    # C1 guard: if head_sha is recorded, it must match source_release_sha.
    # A CI run that ran on SHA X cannot legitimately certify SHA Y as a source release.
    ci_head_sha = ci.get("head_sha")
    if ci_head_sha is not None and ci_head_sha != sha:
        return None, (
            f"provenance_meta.json C1 mismatch: source_ci.head_sha={ci_head_sha!r} "
            f"!= source_release_sha={sha!r}. CI ran on a different commit."
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


def _check_source_runtime_consistency(
    source_sha: str,
    runtime_sha: str,
) -> tuple[bool | None, str | None]:
    """Determine if runtime_sha is a valid data-only descendant of source_sha.

    Uses git log with the canonical source-changing path filter from ci_gates.yml.
    Returns (consistent, error_str) where consistent is:
      True   — only data-only commits between source and runtime (or same SHA)
      False  — source-changing commits detected between source and runtime
      None   — cannot determine (shallow clone, SHA not in history, git error)

    Does NOT invent a second definition of source-changing paths. Uses _SOURCE_PATHS
    which mirrors ci_gates.yml paths filter exactly.
    """
    if source_sha == runtime_sha:
        return True, None

    try:
        # List commits between source_sha..runtime_sha that touch source-changing paths.
        # If this returns any output, source-changing commits exist → mismatch.
        result = subprocess.run(
            [
                "git", "-C", str(ROOT),
                "log", "--oneline",
                f"{source_sha}..{runtime_sha}",
                "--",
                *_SOURCE_PATHS,
            ],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if result.returncode != 0:
            # Non-zero = git error (SHA not found, shallow, etc.) → cannot determine
            return None, f"git log failed: {result.stderr.strip()}"
        source_changing_commits = result.stdout.strip()
        if source_changing_commits:
            return False, (
                f"source-changing commits detected between "
                f"{source_sha[:8]}..{runtime_sha[:8]}: "
                + source_changing_commits.splitlines()[0]
            )
        return True, None
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return None, f"git log error: {exc}"


def build_provenance(built_at: str | None = None) -> dict[str, Any]:
    """Assemble the provenance object for embedding in health.json.

    Returns a dict with schema_version, source_release_sha, source_ci,
    runtime_data_sha, source_runtime_consistent, and built_at.
    Fields are null with an errors list when truth cannot be established (fail-closed).
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
            "head_sha": ci_info.get("head_sha"),
        }
    else:
        source_release_sha = None
        source_ci = {"run_id": None, "workflow": None, "status": None, "head_sha": None}

    # C4: consistency check — can only run if both SHAs are known
    errors: list[str] = []
    if meta_err:
        errors.append(f"source_release: {meta_err}")
    if runtime_err:
        errors.append(f"runtime_data: {runtime_err}")

    if source_release_sha is not None and runtime_sha is not None:
        consistent, consistency_err = _check_source_runtime_consistency(
            source_release_sha, runtime_sha
        )
        if consistency_err and consistent is None:
            errors.append(f"source_runtime_consistency: {consistency_err}")
    else:
        consistent = None
        if not errors:
            errors.append("source_runtime_consistency: cannot check — source or runtime SHA unknown")

    provenance: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_release_sha": source_release_sha,
        "source_ci": source_ci,
        "runtime_data_sha": runtime_sha,
        "source_runtime_consistent": consistent,
        "built_at": built_at,
    }
    if errors:
        provenance["errors"] = errors

    return provenance


def is_source_release_resolved(provenance: dict[str, Any]) -> bool:
    """True iff source_release_sha is established AND runtime is a confirmed data-only descendant.

    A non-null SHA alone is insufficient (C4). Both source identity and source/runtime
    consistency must be positively confirmed. Any uncertainty → False (fail-closed).
    """
    sha = provenance.get("source_release_sha")
    if not (isinstance(sha, str) and sha):
        return False
    return provenance.get("source_runtime_consistent") is True
