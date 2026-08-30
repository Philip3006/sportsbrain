"""Fail-closed private-ledger publication for local settlement.

This module intentionally does not share the pending-bet consumer's durability
primitive. The consumer couples publication to ACK ordering and snapshots;
settlement only needs to publish settled ledger CSVs before reporting success.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

_CANONICAL_REMOTE_SUFFIX = "Philip3006/sportsbrain-ledger"
_SETTLEMENT_ALLOWED_PATHS = ("ledger_*.csv",)
_PUSH_ATTEMPTS = 2


class PrivateLedgerDurabilityError(RuntimeError):
    """Private-ledger state cannot be proven durable on origin/main."""


class SettlementLedgerPublisher:
    """Publish settlement-only ledger changes with remote-containment proof."""

    def __init__(self, ledger_dir: Path) -> None:
        self.ledger_dir = ledger_dir

    def prepare_for_settlement(self) -> None:
        """Resolve any prior local settlement state before new mutations.

        A prior failed push may have left a committed or dirty ledger change.
        Publish it first so a retry cannot combine an unknown partial state with
        a fresh settlement calculation.
        """
        self._validate_repository()
        self._fetch()
        if self._worktree_paths():
            self._stage_commit_rebase_push("auto: settle ledger recovery")
            return
        self._rebase()
        self._push_if_not_contained()
        self._assert_remote_containment()

    def publish_settlement_changes(self, settled_count: int) -> None:
        """Commit, rebase, push, and prove containment for a settlement write."""
        if settled_count <= 0:
            return
        self._validate_repository()
        self._fetch()
        self._stage_commit_rebase_push(f"auto: settle {settled_count} bet(s)")

    def _validate_repository(self) -> None:
        if not self.ledger_dir.is_dir():
            raise PrivateLedgerDurabilityError("private ledger directory is unavailable")
        if self._run("rev-parse", "--is-inside-work-tree").returncode != 0:
            raise PrivateLedgerDurabilityError("private ledger directory is not a git worktree")
        branch = self._run("branch", "--show-current")
        if branch.returncode != 0 or branch.stdout.strip() != "main":
            raise PrivateLedgerDurabilityError("private ledger must be on branch main")
        remote = self._run("remote", "get-url", "origin")
        normalized = remote.stdout.strip().rstrip("/").removesuffix(".git")
        if remote.returncode != 0 or not normalized.endswith(_CANONICAL_REMOTE_SUFFIX):
            raise PrivateLedgerDurabilityError("private ledger origin is not canonical")

    def _worktree_paths(self) -> list[str]:
        status = self._require("status", "--porcelain", "--untracked-files=all")
        paths: list[str] = []
        for line in status.stdout.splitlines():
            path = line[3:]
            if not self._is_allowed(path):
                raise PrivateLedgerDurabilityError(
                    "private ledger has unexpected local changes"
                )
            paths.append(path)
        return paths

    def _stage_commit_rebase_push(self, message: str) -> None:
        self._worktree_paths()
        staged_before = self._staged_paths()
        if staged_before and not all(self._is_allowed(path) for path in staged_before):
            raise PrivateLedgerDurabilityError("private ledger has unexpected staged files")

        add = self._run("add", "--", *_SETTLEMENT_ALLOWED_PATHS)
        if add.returncode != 0:
            raise PrivateLedgerDurabilityError("private ledger git add failed")
        staged = self._staged_paths()
        if not staged:
            raise PrivateLedgerDurabilityError("private ledger changes disappeared before commit")
        if not all(self._is_allowed(path) for path in staged):
            raise PrivateLedgerDurabilityError("private ledger staged files are not allowlisted")

        commit = self._run(
            "commit", "-m", message,
            "--author=SportsBrain Bot <bot@sportsbrain>",
        )
        if commit.returncode != 0:
            raise PrivateLedgerDurabilityError("private ledger git commit failed")

        self._rebase()
        self._push_if_not_contained()
        self._assert_remote_containment()

    def _fetch(self) -> None:
        if self._run("fetch", "origin", "main", "--quiet").returncode != 0:
            raise PrivateLedgerDurabilityError("private ledger git fetch failed")

    def _rebase(self) -> None:
        result = self._run("rebase", "origin/main")
        if result.returncode == 0:
            return
        self._run("rebase", "--abort")
        raise PrivateLedgerDurabilityError("private ledger rebase conflict or failure")

    def _push_if_not_contained(self) -> None:
        for _ in range(_PUSH_ATTEMPTS):
            if self._is_contained():
                return
            if self._run("push", "origin", "HEAD:main").returncode == 0:
                return
            # Another authoritative writer may have landed between rebase and
            # push. Retry only when git can reconcile without a CSV conflict.
            self._fetch()
            self._rebase()
        raise PrivateLedgerDurabilityError("private ledger git push failed")

    def _assert_remote_containment(self) -> None:
        self._fetch()
        if not self._is_contained():
            raise PrivateLedgerDurabilityError("private ledger remote containment cannot be proven")

    def _is_contained(self) -> bool:
        return self._run("merge-base", "--is-ancestor", "HEAD", "origin/main").returncode == 0

    def _staged_paths(self) -> list[str]:
        result = self._require("diff", "--cached", "--name-only")
        return [path for path in result.stdout.splitlines() if path]

    def _is_allowed(self, path: str) -> bool:
        candidate = Path(path)
        return candidate.parent == Path(".") and any(
            fnmatch.fnmatch(candidate.name, pattern) for pattern in _SETTLEMENT_ALLOWED_PATHS
        )

    def _require(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._run(*args)
        if result.returncode != 0:
            raise PrivateLedgerDurabilityError(f"private ledger git {' '.join(args)} failed")
        return result

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.ledger_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
