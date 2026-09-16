"""Non-mutating environment diagnostics for Night Shift operations."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .worktree import WorktreeManager, default_repo_paths


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


def _version(command: str) -> tuple[bool, str]:
    path = shutil.which(command)
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


def run_doctor(
    *, repo_root: Path, runtime_dir: Path, repo_paths: dict[str, Path] | None = None
) -> dict[str, Any]:
    """Return a sanitized report; this function never prints command output."""

    runtime = Path(runtime_dir).expanduser()
    paths = repo_paths or default_repo_paths(repo_root)
    manager = WorktreeManager(runtime, paths)
    checks: list[dict[str, Any]] = []
    git_ok, git_detail = _version("git")
    checks.append(_check("git", git_ok, git_detail))
    gh_ok, gh_detail = _version("gh")
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
    codex_path = shutil.which("codex")
    codex_ok, codex_detail = _version("codex")
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
        checks.append(
            _check(
                f"canonical_checkout:{repo}",
                bool(check.get("clean")),
                "clean" if check.get("clean") else str(check.get("error", "dirty")),
            )
        )
        remote_ok, remote_detail = _remote(path)
        checks.append(_check(f"origin_url:{repo}", remote_ok, remote_detail))
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="doctor-", dir=runtime, delete=True
        ) as handle:
            handle.write(b"ok")
        runtime_ok = True
    except OSError:
        runtime_ok = False
    checks.append(
        _check(
            "runtime_directory",
            runtime_ok,
            str(runtime) if runtime_ok else "runtime directory is not writable",
        )
    )
    try:
        with tempfile.TemporaryDirectory(prefix="nightshift-doctor-") as temp:
            db = Path(temp) / "doctor.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE check_ok (value INTEGER)")
                conn.execute("INSERT INTO check_ok VALUES (1)")
                sqlite_ok = (
                    conn.execute("SELECT value FROM check_ok").fetchone()[0] == 1
                )
    except sqlite3.Error:
        sqlite_ok = False
    checks.append(
        _check(
            "sqlite",
            sqlite_ok,
            "WAL-capable SQLite available" if sqlite_ok else "SQLite check failed",
        )
    )
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
    failed = [item for item in checks if not item["ok"]]
    return {
        "status": "ok" if not failed else "blocked",
        "checks": checks,
        "codex": {"path": codex_path or "not found", "headless_supported": supported},
        "runtime_dir": str(runtime),
        "secrets_redacted": True,
    }
