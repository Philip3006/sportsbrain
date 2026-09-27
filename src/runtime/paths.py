"""Keep launchd runtime state and staged public artifacts out of the checkout."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_STATE_DIR = (
    Path.home() / "Library" / "Application Support" / "SportsBrain" / "runtime-state"
)
RUNTIME_STATE_ENV = "SPORTSBRAIN_RUNTIME_STATE_DIR"


def governed_runtime_root() -> Path:
    """Resolve the operator-owned runtime root used by governed observers."""

    configured = os.getenv(RUNTIME_STATE_ENV, "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_RUNTIME_STATE_DIR
    if not root.is_absolute():
        raise RuntimeError(f"{RUNTIME_STATE_ENV} must be an absolute path")
    resolved = root.resolve()
    active = ROOT.resolve()
    temporary_roots = (Path("/tmp"), Path("/private/tmp"))
    if (
        resolved == active
        or active in resolved.parents
        or any(resolved == item or item in resolved.parents for item in temporary_roots)
        or any(part.casefold() in {"test", "tests", "__tests__"} for part in resolved.parts)
        or resolved.name != "runtime-state"
        or not resolved.is_dir()
    ):
        raise RuntimeError("governed runtime root is missing or unsafe")
    return resolved


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


def _seed_runtime_state(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.close(fd)
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_state_path(relative_path: str, *, require_external: bool = False) -> Path:
    """Return durable state, optionally requiring an external operator-owned path."""
    root = _external_root(RUNTIME_STATE_ENV)
    if not root and require_external:
        root = DEFAULT_RUNTIME_STATE_DIR
    if not root:
        return ROOT / relative_path
    target = root / relative_path
    source = ROOT / relative_path
    if not target.exists() and source.is_file():
        _seed_runtime_state(source, target)
    return target


def runtime_artifact_path(relative_path: str, *, active_root: Path | None = None) -> Path:
    """Return a per-run staged public artifact path when launchd configured it."""
    root = _external_root("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR")
    return (root / relative_path) if root else ((active_root or ROOT) / relative_path)
