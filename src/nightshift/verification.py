"""Independent, structured-argv verification for task worktrees."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "commands": [dict(command) for command in self.commands],
            "failure_class": self.failure_class,
        }


class VerificationRunner:
    """Run only task-supplied argv arrays, never a shell command string."""

    def run(self, task: TaskSpec, worktree: Path) -> VerificationResult:
        commands = task.verification_commands
        joined = [" ".join(command) for command in commands]
        missing = [
            label
            for label in task.required_tests
            if not any(label in item for item in joined)
        ]
        if missing:
            return VerificationResult(
                False,
                tuple(
                    {"argv": list(command), "status": "not_run"} for command in commands
                ),
                "REQUIRED_TEST_NOT_CONFIGURED",
            )
        if not commands and task.required_tests:
            return VerificationResult(False, (), "REQUIRED_TEST_NOT_CONFIGURED")
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        for command in commands:
            remaining = task.max_runtime_seconds - (time.monotonic() - started)
            if remaining <= 0:
                return VerificationResult(False, tuple(results), "VERIFICATION_TIMEOUT")
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
                return VerificationResult(False, tuple(results), "VERIFICATION_TIMEOUT")
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
                return VerificationResult(
                    False, tuple(results), "VERIFICATION_EXECUTOR_ERROR"
                )
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
                return VerificationResult(False, tuple(results), "REQUIRED_TEST_FAILED")
        return VerificationResult(True, tuple(results))


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
