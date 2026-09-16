"""Explicit worker executors for deterministic fake and real Codex runs."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .errors import (
    ExecutorUnavailable,
    SafetyViolation,
    ScopeViolation,
    WorktreeSafetyError,
)
from .models import ExecutionResult, TaskRecord, TaskState
from .worktree import WorktreeManager

_SECRET = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|password|secret|token)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)


def redact(value: str, limit: int = 8000) -> str:
    """Keep diagnostics useful without persisting likely credentials."""

    return _SECRET.sub(r"\1=[REDACTED]", value.replace("\x00", ""))[:limit]


class FakeExecutor:
    """Deterministic test executor; it never invokes a process or provider."""

    def __init__(
        self,
        *,
        write_file: str | None = None,
        content: str = "fake executor\n",
        outcome: TaskState | None = None,
        fail_once: bool = False,
    ) -> None:
        self.write_file = write_file
        self.content = content
        self.outcome = outcome
        self.fail_once = fail_once
        self.calls: list[str] = []

    def __call__(self, task: TaskRecord) -> ExecutionResult:
        self.calls.append(task.task_id)
        if self.fail_once and len(self.calls) == 1:
            return ExecutionResult(False, "simulated worker crash", retryable=True)
        if self.write_file:
            if not task.worktree_path:
                raise SafetyViolation("FakeExecutor requires an allocated worktree")
            root = Path(task.worktree_path).resolve()
            target = (root / self.write_file).resolve()
            if root not in target.parents:
                raise SafetyViolation(
                    "FakeExecutor write path escaped the assigned worktree"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.content, encoding="utf-8")
        return ExecutionResult(
            True,
            "fake execution verified",
            data={"executor": "fake"},
            terminal_state=self.outcome,
        )


class CodexExecutor:
    """Run the documented non-interactive ``codex exec`` command only."""

    def __init__(
        self,
        codex_path: str,
        worktrees: WorktreeManager,
        *,
        timeout_seconds: int = 15 * 60,
        sandbox: str = "workspace-write",
    ) -> None:
        if timeout_seconds < 1 or timeout_seconds > 24 * 60 * 60:
            raise ValueError("timeout_seconds must be between 1 second and 24 hours")
        if sandbox not in {"read-only", "workspace-write"}:
            raise SafetyViolation(
                "CodexExecutor only permits read-only or workspace-write sandboxes"
            )
        self.codex_path = codex_path
        self.worktrees = worktrees
        self.timeout_seconds = timeout_seconds
        self.sandbox = sandbox
        self.last_pid: int | None = None

    @staticmethod
    def prompt(task: TaskRecord) -> str:
        return (
            "You are a governed SportsBrain worker. Work only in the assigned Git worktree.\n"
            f"Task ID: {task.task_id}\nBuilder: {task.builder_id}\nRepository: {task.repo}\n"
            f"Task type: {task.task_type}\nObjective: {task.objective}\n"
            f"Allowed paths: {', '.join(task.allowed_paths) or 'all non-prohibited paths'}\n"
            f"Prohibited paths: {', '.join(task.prohibited_paths) or '.git, .env, production/private data'}\n"
            "Do not merge, push, deploy, publish, contact providers, spend money, or change production/financial data. "
            "Make only the deterministic safe engineering change requested. Report verification in your final response."
        )

    def __call__(self, task: TaskRecord) -> ExecutionResult:
        if not shutil.which(self.codex_path) and not Path(self.codex_path).is_file():
            raise ExecutorUnavailable(
                "Codex executable is unavailable; refusing fake fallback"
            )
        if not task.worktree_path:
            return ExecutionResult(
                False,
                "no isolated worktree was assigned",
                retryable=False,
                terminal_state=TaskState.FAILED_SAFE,
            )
        worktree = Path(task.worktree_path)
        if not worktree.is_dir() or not self.worktrees.is_isolated_path(worktree):
            return ExecutionResult(
                False,
                "assigned worktree does not exist",
                retryable=False,
                terminal_state=TaskState.FAILED_SAFE,
            )
        command = [
            self.codex_path,
            "exec",
            "--ephemeral",
            "-C",
            str(worktree),
            "--sandbox",
            self.sandbox,
            "--skip-git-repo-check",
            "-",
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=worktree,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutorUnavailable(f"cannot start Codex executable: {exc}") from exc
        self.last_pid = process.pid
        try:
            stdout, stderr = process.communicate(
                self.prompt(task), timeout=self.timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            summary = f"Codex timed out after {self.timeout_seconds}s"
            return ExecutionResult(
                False,
                summary,
                data={
                    "stdout": redact(exc.stdout or ""),
                    "stderr": redact(exc.stderr or ""),
                    "pid": process.pid,
                    "failure": "TIMEOUT",
                },
                retryable=True,
                process_id=process.pid,
            )
        output = {
            "stdout": redact(stdout),
            "stderr": redact(stderr),
            "pid": process.pid,
            "exit_code": process.returncode,
        }
        if process.returncode != 0:
            return ExecutionResult(
                False,
                f"Codex exited with status {process.returncode}",
                data=output,
                retryable=True,
                process_id=process.pid,
            )
        try:
            changed = self.worktrees.verify_scope(task, worktree)
        except (ScopeViolation, WorktreeSafetyError) as exc:
            return ExecutionResult(
                False,
                redact(str(exc)),
                data={
                    **output,
                    "changed_paths": list(self.worktrees.changed_paths(worktree)),
                    "failure": "SCOPE_VIOLATION",
                },
                retryable=False,
                terminal_state=TaskState.FAILED_SAFE,
            )
        return ExecutionResult(
            True,
            "Codex completed and scope was independently verified",
            data={**output, "changed_paths": list(changed)},
            terminal_state=TaskState.PR_READY,
            process_id=process.pid,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
