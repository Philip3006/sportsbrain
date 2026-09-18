"""Bounded interprocess locking for the shared Night Shift control repository.

The lock covers only Git operations that update shared remote references.  It
uses kernel-owned ``flock`` state, so a process crash releases ownership and a
left-behind lock file is harmless.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .errors import NightShiftError


class ControlRepoLockTimeout(NightShiftError):
    """The bounded control-repository lock deadline elapsed."""


class ControlRepoLock(AbstractContextManager["ControlRepoLock"]):
    """A process-safe, bounded lock with safe stale-owner behavior."""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.05,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise ValueError("control-repo lock path must be absolute")
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self._file: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._write_owner_record()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._close()
                    raise ControlRepoLockTimeout(
                        f"timed out acquiring control-repo lock: {self.path}"
                    )
                time.sleep(
                    min(self.poll_seconds, max(0.001, deadline - time.monotonic()))
                )
            except OSError:
                self._close()
                raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._close()

    def _write_owner_record(self) -> None:
        assert self._file is not None
        self._file.seek(0)
        self._file.truncate()
        self._file.write(
            json.dumps(
                {"pid": os.getpid(), "acquired_at": time.time()}, sort_keys=True
            )
        )
        self._file.flush()

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def control_repo_lock_path(control_repo: Path, lock_path: Path | None = None) -> Path:
    """Return an absolute lock path outside the bare repository."""

    path = Path(lock_path).expanduser() if lock_path is not None else Path(control_repo).expanduser().parent / ".nightshift-control-repo.lock"
    if not path.is_absolute():
        raise ValueError("control-repo lock path must be absolute")
    return path


def run_locked_control_repo_operation(
    control_repo: Path,
    operation: Callable[[], Any],
    *,
    lock_path: Path | None = None,
    timeout_seconds: float = 30.0,
) -> Any:
    """Run an injected shared-control operation under the bounded lock."""

    with ControlRepoLock(
        control_repo_lock_path(control_repo, lock_path),
        timeout_seconds=timeout_seconds,
    ):
        return operation()


def fetch_control_repo(
    control_repo: Path,
    *,
    remote: str = "origin",
    refspec: str = "main",
    lock_path: Path | None = None,
    timeout_seconds: float = 30.0,
    runner: Callable[..., Any] = subprocess.run,
) -> Any:
    """Fetch one remote ref while serializing the bare-repo ref update."""

    repo = Path(control_repo).expanduser()
    if not repo.is_absolute():
        raise ValueError("control_repo must be an absolute path")
    return run_locked_control_repo_operation(
        repo,
        lambda: runner(
            ["git", "--git-dir", str(repo), "fetch", remote, refspec],
            check=True,
            capture_output=True,
            text=True,
        ),
        lock_path=lock_path,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "ControlRepoLock",
    "ControlRepoLockTimeout",
    "control_repo_lock_path",
    "fetch_control_repo",
    "run_locked_control_repo_operation",
]
