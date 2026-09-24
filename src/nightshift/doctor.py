"""Non-mutating environment diagnostics for Night Shift operations."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .backpressure import summarize_pull_requests
from .errors import ConfigurationError
from .registry import BuilderRegistry
from .roadmap import RoadmapRegistry
from .status import operator_snapshot
from .store import DispatcherStore
from .task_states import TaskState
from .templates import TemplateRegistry
from .worktree import (
    RuntimeDirtyPolicy,
    WorktreeManager,
    default_control_remote_urls,
    default_control_repo_paths,
    default_repo_paths,
    default_runtime_dirty_policy,
    default_worktree_root,
)


def _check(
    name: str, ok: bool, detail: str, *, severity: str = "error"
) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "detail": detail if ok or severity != "secret" else "redacted",
        "severity": severity,
    }


def _run(
    command: list[str], *, timeout: int = 10
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _version_path(command: str, configured: str | None = None) -> tuple[bool, str]:
    path = configured or shutil.which(command)
    if not path:
        return False, "not found"
    result = _run([path, "--version"])
    first = (
        (result.stdout or result.stderr).splitlines()[0][:160]
        if result
        else "version check failed"
    )
    return bool(result and result.returncode == 0), first


def _remote(path: Path) -> tuple[bool, str]:
    result = _run(["git", "-C", str(path), "remote", "get-url", "origin"])
    if not result or result.returncode != 0:
        return False, "origin unavailable"
    value = (result.stdout or "").strip()
    if "@" in value and "://" in value:
        value = value.split("://", 1)[0] + "://[REDACTED]@" + value.split("@", 1)[1]
    return True, value[:240]


def _writable_directory(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="doctor-", dir=path, delete=True
        ) as handle:
            handle.write(b"ok")
        return True, str(path)
    except OSError:
        return False, "directory is not writable"


def _sqlite_integrity(path: Path) -> tuple[bool, str]:
    if not path.exists():
        try:
            with tempfile.TemporaryDirectory(prefix="nightshift-doctor-") as temp:
                temp_path = Path(temp) / "doctor.sqlite3"
                with sqlite3.connect(temp_path) as conn:
                    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                return result == "ok", "SQLite integrity check passed"
        except sqlite3.Error:
            return False, "SQLite integrity check failed"
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return result == "ok", "shared SQLite integrity check passed"
    except sqlite3.Error:
        return False, "shared SQLite integrity check failed"


def _launchd_worker_checks(builder_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    if shutil.which("launchctl") is None or os.name != "posix":
        return [
            _check(
                "night_shift_workers",
                False,
                "launchctl is unavailable; worker activity cannot be verified",
            )
        ]
    uid = os.getuid()
    checks: list[dict[str, Any]] = []
    for builder_id in builder_ids:
        label = f"com.sportsbrain.night-shift-worker.{builder_id}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        present = plist.is_file()
        status = _run(["launchctl", "print", f"gui/{uid}/{label}"], timeout=10)
        running = bool(status and status.returncode == 0)
        detail = (
            "plist present and launchd service loaded"
            if present and running
            else "plist missing"
            if not present
            else "launchd service not loaded"
        )
        checks.append(
            _check(f"night_shift_worker:{builder_id}", present and running, detail)
        )
    return checks


def run_doctor(
    *,
    repo_root: Path,
    runtime_dir: Path,
    repo_paths: dict[str, Path] | None = None,
    control_repo_paths: dict[str, Path] | None = None,
    worktree_root: Path | None = None,
    runtime_dirty_policy: RuntimeDirtyPolicy | None = None,
    state_path: Path | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a sanitized report; this function never prints command output."""

    runtime = Path(runtime_dir).expanduser()
    paths = repo_paths if repo_paths is not None else default_repo_paths(repo_root)
    controls = (
        control_repo_paths
        if control_repo_paths is not None
        else {
            repo: path
            for repo, path in default_control_repo_paths().items()
            if repo in paths
        }
    )
    policy = runtime_dirty_policy
    if policy is None:
        policy_path = Path(repo_root) / "config" / "night_shift" / "runtime_dirty.json"
        policy = (
            default_runtime_dirty_policy(repo_root)
            if policy_path.is_file()
            else RuntimeDirtyPolicy({})
        )
    manager = WorktreeManager(
        runtime,
        paths,
        control_repo_paths=controls,
        worktrees_dir=worktree_root or default_worktree_root(),
        runtime_dirty_policy=policy,
        expected_remote_urls=default_control_remote_urls(),
    )
    checks: list[dict[str, Any]] = []
    git_ok, git_detail = _version_path("git")
    checks.append(_check("git", git_ok, git_detail))
    gh_ok, gh_detail = _version_path("gh")
    checks.append(_check("gh", gh_ok, gh_detail))
    gh_path = shutil.which("gh")
    auth = _run([gh_path, "auth", "status"], timeout=15) if gh_path else None
    checks.append(
        _check(
            "github_authentication",
            bool(auth and auth.returncode == 0),
            "authenticated" if auth and auth.returncode == 0 else "not authenticated",
        )
    )
    codex_path = os.getenv("SPORTSBRAIN_CODEX_EXECUTABLE") or shutil.which("codex")
    codex_ok, codex_detail = _version_path("codex", codex_path)
    checks.append(_check("codex_executable", codex_ok, codex_detail))
    help_result = (
        _run([codex_path, "exec", "--help"], timeout=15) if codex_path else None
    )
    supported = bool(
        help_result
        and help_result.returncode == 0
        and "Run Codex non-interactively" in help_result.stdout
        and "--ephemeral" in help_result.stdout
        and "-C" in help_result.stdout
    )
    checks.append(
        _check(
            "codex_headless_execution",
            supported,
            "codex exec with stdin prompt, --ephemeral, and -C"
            if supported
            else "documented non-interactive form unavailable",
        )
    )
    login = _run([codex_path, "login", "status"], timeout=15) if codex_path else None
    checks.append(
        _check(
            "codex_authentication",
            bool(login and login.returncode == 0),
            "authenticated status reported"
            if login and login.returncode == 0
            else "not authenticated or status unavailable",
        )
    )

    for repo, path in paths.items():
        check = manager.canonical_check(repo)
        dirty_class = check.get("dirty_class", "UNKNOWN")
        if dirty_class == "CLEAN":
            detail = "clean"
        elif dirty_class == "RUNTIME_CHECKOUT_DIRTY_EXPECTED":
            detail = "RUNTIME_CHECKOUT_DIRTY_EXPECTED: " + ", ".join(
                check.get("runtime_dirty_paths", [])
            )
        else:
            detail = dirty_class
            if check.get("unexpected_dirty_paths"):
                detail += ": " + ", ".join(check["unexpected_dirty_paths"])
        checks.append(
            _check(
                f"canonical_checkout:{repo}",
                dirty_class in {"CLEAN", "RUNTIME_CHECKOUT_DIRTY_EXPECTED"}
                and bool(check.get("safe_for_allocation")),
                detail,
            )
        )
        remote_ok, remote_detail = _remote(path)
        checks.append(_check(f"origin_url:{repo}", remote_ok, remote_detail))
        try:
            control = manager.control_check(repo, base_branch="main", fetch=True)
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            control = {
                "path": "",
                "is_control_repository": False,
                "remote_ok": False,
                "fetch_ok": False,
                "origin_sha": None,
                "error": type(exc).__name__,
            }
        checks.append(
            _check(
                f"night_shift_control_repository:{repo}",
                bool(control.get("is_control_repository")),
                str(control.get("path", ""))
                if control.get("is_control_repository")
                else str(control.get("error", "control repository unavailable")),
            )
        )
        checks.append(
            _check(
                f"night_shift_control_remote:{repo}",
                bool(control.get("remote_ok")),
                "governed origin configured"
                if control.get("remote_ok")
                else "control repository origin is incorrect",
            )
        )
        checks.append(
            _check(
                f"night_shift_control_fetch:{repo}",
                bool(control.get("fetch_ok")),
                f"fetched origin/main at {control.get('origin_sha')}"
                if control.get("fetch_ok")
                else "control repository fetch failed",
            )
        )
        checks.append(
            _check(
                f"night_shift_origin_main:{repo}",
                bool(control.get("origin_sha")),
                str(control.get("origin_sha") or "origin/main unavailable"),
            )
        )

    runtime_ok, runtime_detail = _writable_directory(runtime)
    checks.append(_check("runtime_directory", runtime_ok, runtime_detail))
    root_ok, root_detail = _writable_directory(
        Path(worktree_root or default_worktree_root()).expanduser()
    )
    checks.append(_check("isolated_worktree_root", root_ok, root_detail))
    sqlite_ok, sqlite_detail = _sqlite_integrity(
        Path(state_path).expanduser() if state_path else runtime / "nightshift.sqlite3"
    )
    checks.append(_check("shared_sqlite", sqlite_ok, sqlite_detail))
    wt = _run(["git", "worktree", "list", "--porcelain"])
    checks.append(
        _check(
            "git_worktree_support",
            bool(wt and wt.returncode == 0),
            "git worktree list available"
            if wt and wt.returncode == 0
            else "git worktree unavailable",
        )
    )
    for dependency in ("pytest", "ruff"):
        available = importlib.util.find_spec(dependency) is not None
        checks.append(
            _check(
                f"dependency:{dependency}",
                available,
                "available" if available else "not importable",
            )
        )

    registry_ok = True
    registry_ids: tuple[str, ...] = ()
    registry_error = ""
    try:
        directory = config_dir or Path(repo_root) / "config" / "night_shift"
        registry = BuilderRegistry.from_file(directory / "builders.json")
        registry_ids = registry.builder_ids
        registry_ok = registry_ids == (
            "builder-1",
            "builder-2",
            "builder-3",
            "builder-4",
            "terminal-5",
        )
    except (ConfigurationError, OSError) as exc:
        registry_ok = False
        registry_error = type(exc).__name__
    checks.append(
        _check(
            "worker_registry_terminal_pool_1_to_5",
            registry_ok,
            "explicit Terminal Builders 1-5; Dispatcher is a separate control plane"
            if registry_ok
            else f"registry unavailable or contains unsupported builders ({registry_error})",
        )
    )
    checks.extend(
        _launchd_worker_checks(
            registry_ids
            or ("builder-1", "builder-2", "builder-3", "builder-4", "terminal-5")
        )
    )

    failed = [item for item in checks if not item["ok"]]
    queue_path = (
        Path(state_path).expanduser()
        if state_path
        else runtime / "nightshift.sqlite3"
    )
    queue_report: dict[str, Any] = {
        "available": queue_path.is_file(),
        "state_path": str(queue_path),
    }
    if queue_path.is_file():
        try:
            directory = config_dir or Path(repo_root) / "config" / "night_shift"
            configured_roadmap = RoadmapRegistry.from_file(directory / "roadmap.json")
            templates = TemplateRegistry.from_file(directory / "templates.json")
            configured_items = {
                item.item_id: item for item in configured_roadmap.items
            }
            store = DispatcherStore(queue_path)
            records = store.list_tasks(limit=1000)
            roadmap = store.roadmap_records()
            stats = store.stats()
            merge_limit = configured_roadmap.merge_backpressure_limit
            pr_counts = summarize_pull_requests(records)
            merge_count = pr_counts["active_substantive_pr_count"]
            roadmap_for_status: list[dict[str, Any]] = []
            for item in roadmap:
                template = templates.resolve(item["template_id"])
                configured = configured_items.get(item["item_id"])
                summary = dict(item)
                summary["generation"] = item.get(
                    "generation", configured.generation if configured else 1
                )
                summary["risk_class"] = template.risk_class.value
                read_only = (
                    template.risk_class.value == "read_only"
                    and template.requires_pr is not True
                )
                summary["merge_backpressure_blocked"] = False
                summary["soft_backpressure_preferred"] = (
                    merge_count >= merge_limit and not read_only
                )
                roadmap_for_status.append(summary)
            queue_report.update(stats)
            queue_report.update(
                {
                    "merge_backpressure_count": merge_count,
                    "merge_backpressure_limit": merge_limit,
                    "active_substantive_pr_count": merge_count,
                    "pr_classifications": pr_counts,
                    "backpressure_mode": "SOFT"
                    if merge_count >= merge_limit
                    else "NONE",
                    "backpressure_is_hard": False,
                }
            )
            queue_report["operator"] = operator_snapshot(
                records,
                roadmap_for_status,
                merge_backpressure=merge_count >= merge_limit,
                pr_counts=pr_counts,
                builders=registry_ids,
                paused=stats["paused"],
                draining=stats["draining"],
            )
            active_states = {
                TaskState.CLAIMED,
                TaskState.RUNNING,
                TaskState.VERIFYING,
                TaskState.DELIVERY_RECONCILING,
            }
            queue_report["dispatcher_health"] = {
                "status": "paused"
                if stats["paused"]
                else "draining"
                if stats["draining"]
                else "healthy",
                "control_plane_identity": "nightshift-dispatcher",
                "is_terminal_worker": False,
                "audit_chain_valid": store.verify_audit_chain(),
            }
            queue_report["app_workstreams"] = {
                app_owner: {
                    "active_tasks": [
                        record.task_id
                        for record in records
                        if record.app_owner == app_owner
                        and record.state in active_states
                    ],
                    "blockers": [
                        record.task_id
                        for record in records
                        if record.app_owner == app_owner
                        and record.state
                        in {TaskState.BLOCKED, TaskState.FAILED_SAFE, TaskState.PAUSED_QUOTA}
                    ],
                }
                for app_owner in ("APP_B1", "APP_B2", "APP_B3", "APP_B4", "APP_B5")
            }
            queue_report["terminal_capacity"] = {
                worker_id: {
                    "status": next(
                        (
                            record.state.value.lower()
                            for record in records
                            if record.terminal_worker_id == worker_id
                            and record.state in active_states
                        ),
                        "idle",
                    ),
                    "capabilities": list(
                        registry.resolve(worker_id).capabilities
                    ),
                }
                for worker_id in registry_ids
            }
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            queue_report["error"] = type(exc).__name__
    return {
        "status": "ok" if not failed else "blocked",
        "checks": checks,
        "codex": {
            "path": codex_path or "not found",
            "headless_supported": supported,
        },
        "runtime_dir": str(runtime),
        "control_repo_paths": {repo: str(path) for repo, path in controls.items()},
        "registry_builders": list(registry_ids),
        "queue": queue_report,
        "secrets_redacted": True,
    }
