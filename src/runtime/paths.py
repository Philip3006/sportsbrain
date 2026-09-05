"""Keep launchd runtime state and staged public artifacts out of the checkout."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _external_root(variable: str) -> Path | None:
    value = os.getenv(variable, "").strip()
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise RuntimeError(f"{variable} must be an absolute path")
    resolved = root.resolve()
    active = ROOT.resolve()
    if resolved == active or active in resolved.parents:
        raise RuntimeError(f"{variable} must not point into the active checkout")
    return root


def runtime_state_path(relative_path: str) -> Path:
    """Return durable local state outside the checkout when launchd configured it."""
    root = _external_root("SPORTSBRAIN_RUNTIME_STATE_DIR")
    if not root:
        return ROOT / relative_path
    target = root / relative_path
    source = ROOT / relative_path
    if not target.exists() and source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target


def runtime_artifact_path(relative_path: str, *, active_root: Path | None = None) -> Path:
    """Return a per-run staged public artifact path when launchd configured it."""
    root = _external_root("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR")
    return (root / relative_path) if root else ((active_root or ROOT) / relative_path)
