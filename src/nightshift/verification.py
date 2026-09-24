"""Independent, structured-argv verification for task worktrees."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .executors import redact
from .models import TaskSpec


@dataclass(frozen=True)
class VerificationResult:
    """Sanitized result of all reviewed commands in one verification gate."""

    passed: bool
    commands: tuple[dict[str, Any], ...]
    failure_class: str | None = None
    repository: str | None = None
    target_sha: str | None = None
    pr_number: int | None = None
    app_owner: str | None = None
    execution_worker: str | None = None
    matrix_version: str = "nightshift-verification-v1"
    started_at: str | None = None
    finished_at: str | None = None
    worktree_clean: bool | None = None
    evidence_digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "commands": [dict(command) for command in self.commands],
            "failure_class": self.failure_class,
            "repository": self.repository,
            "target_sha": self.target_sha,
            "pr_number": self.pr_number,
            "app_owner": self.app_owner,
            "execution_worker": self.execution_worker,
            "matrix_version": self.matrix_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "worktree_clean": self.worktree_clean,
            "evidence_digest": self.evidence_digest,
        }


class VerificationRunner:
    """Run only task-supplied argv arrays, never a shell command string."""

    def run(
        self,
        task: TaskSpec,
        worktree: Path,
        *,
        repository: str | None = None,
        target_sha: str | None = None,
        pr_number: int | None = None,
    ) -> VerificationResult:
        commands = task.verification_commands
        started_at = _now_iso()
        resolved_target_sha = target_sha or _git_target_sha(worktree)
        common = {
            "repository": repository or task.repo,
            "target_sha": resolved_target_sha,
            "pr_number": pr_number,
            "app_owner": task.app_owner,
            "execution_worker": task.execution_worker,
            "matrix_version": task.verification_matrix_version,
            "started_at": started_at,
        }

        def finish(
            passed: bool,
            command_results: tuple[dict[str, Any], ...],
            failure_class: str | None = None,
        ) -> VerificationResult:
            finished_at = _now_iso()
            clean = _git_worktree_clean(worktree)
            digest_payload = {
                **common,
                "commands": command_results,
                "passed": passed,
                "failure_class": failure_class,
                "finished_at": finished_at,
            }
            digest = sha256(
                json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return VerificationResult(
                passed,
                command_results,
                failure_class,
                **common,
                finished_at=finished_at,
                worktree_clean=clean,
                evidence_digest=digest,
            )

        if not resolved_target_sha:
            return finish(False, (), "TARGET_SHA_UNAVAILABLE")

        joined = [" ".join(command) for command in commands]
        missing = [
            label
            for label in task.required_tests
            if not any(label in item for item in joined)
        ]
        if missing:
            return finish(
                False,
                tuple(
                    {"argv": list(command), "status": "not_run"} for command in commands
                ),
                "REQUIRED_TEST_NOT_CONFIGURED",
            )
        if not commands and task.required_tests:
            return finish(False, (), "REQUIRED_TEST_NOT_CONFIGURED")
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        for command in commands:
            remaining = task.max_runtime_seconds - (time.monotonic() - started)
            if remaining <= 0:
                return finish(False, tuple(results), "VERIFICATION_TIMEOUT")
            began = time.monotonic()
            try:
                completed = subprocess.run(
                    list(command),
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=max(1, int(remaining)),
                    check=False,
                    shell=False,
                    env=_safe_environment(),
                )
                _remove_generated_bytecode(worktree)
            except subprocess.TimeoutExpired as exc:
                _remove_generated_bytecode(worktree)
                results.append(
                    {
                        "argv": list(command),
                        "status": "timeout",
                        "duration_seconds": round(time.monotonic() - began, 3),
                        "stdout": redact(exc.stdout or ""),
                        "stderr": redact(exc.stderr or ""),
                    }
                )
                return finish(False, tuple(results), "VERIFICATION_TIMEOUT")
            except OSError as exc:
                _remove_generated_bytecode(worktree)
                results.append(
                    {
                        "argv": list(command),
                        "status": "unavailable",
                        "duration_seconds": round(time.monotonic() - began, 3),
                        "error": type(exc).__name__,
                    }
                )
                return finish(False, tuple(results), "VERIFICATION_EXECUTOR_ERROR")
            results.append(
                {
                    "argv": list(command),
                    "status": "passed" if completed.returncode == 0 else "failed",
                    "returncode": completed.returncode,
                    "duration_seconds": round(time.monotonic() - began, 3),
                    "stdout": redact(completed.stdout),
                    "stderr": redact(completed.stderr),
                }
            )
            if completed.returncode != 0:
                return finish(False, tuple(results), "REQUIRED_TEST_FAILED")
        return finish(True, tuple(results))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_target_sha(worktree: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _git_worktree_clean(worktree: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0 and not result.stdout.strip()


def _safe_environment() -> dict[str, str]:
    """Pass ordinary process settings while excluding common credential names."""

    blocked = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in blocked)
    }


def _remove_generated_bytecode(worktree: Path) -> None:
    """Keep compile-style verification from becoming a reviewable change."""

    for cache_dir in worktree.rglob("__pycache__"):
        if not cache_dir.is_dir() or cache_dir.is_symlink():
            continue
        for bytecode in cache_dir.iterdir():
            if bytecode.is_file() and bytecode.suffix in {".pyc", ".pyo"}:
                bytecode.unlink()
        try:
            cache_dir.rmdir()
        except OSError:
            pass
