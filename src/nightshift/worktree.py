"""Safe, isolated Git worktree allocation and scope verification."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ScopeViolation, WorktreeSafetyError
from .models import TaskSpec


@dataclass(frozen=True)
class WorktreeAllocation:
    task_id: str
    repo: str
    branch: str
    path: Path
    diagnostic_path: Path
    base_branch: str
    base_sha: str
    origin_sha: str


@dataclass(frozen=True)
class BaseResolution:
    """Authoritative origin reference used to create a task worktree."""

    base_branch: str
    base_sha: str
    origin_sha: str


class WorktreeManager:
    """Allocate one branch/worktree per task without shell interpolation."""

    def __init__(
        self,
        runtime_dir: Path,
        repo_paths: dict[str, Path],
        *,
        git_executable: str = "git",
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser()
        self.repo_paths = {
            repo: Path(path).expanduser() for repo, path in repo_paths.items()
        }
        self.git_executable = git_executable
        self.worktrees_dir = self.runtime_dir / "worktrees"
        self.diagnostics_dir = self.runtime_dir / "diagnostics"

    def resolve_repo(self, repo: str) -> Path:
        try:
            return self.repo_paths[repo]
        except KeyError as exc:
            raise WorktreeSafetyError(
                f"no canonical checkout is configured for {repo}"
            ) from exc

    def canonical_check(self, repo: str) -> dict[str, Any]:
        path = self.resolve_repo(repo)
        result: dict[str, Any] = {
            "repo": repo,
            "path": str(path),
            "exists": path.is_dir(),
            "clean": False,
        }
        if not path.is_dir():
            result["error"] = "canonical checkout is not a directory"
            return result
        top = self._run(["-C", str(path), "rev-parse", "--show-toplevel"], check=False)
        if top.returncode != 0:
            result["error"] = "canonical checkout is not a Git repository"
            return result
        result["top_level"] = str(Path(top.stdout.strip()).resolve())
        if Path(result["top_level"]) != path.resolve():
            result["error"] = "configured checkout is not the Git worktree root"
            return result
        status = self._run(
            ["-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
        )
        result["clean"] = status.returncode == 0 and not status.stdout.strip()
        if status.returncode != 0:
            result["error"] = "git status failed"
        elif status.stdout.strip():
            result["dirty_entries"] = len(status.stdout.splitlines())
        return result

    def allocate(self, task: TaskSpec) -> WorktreeAllocation:
        canonical = self.resolve_repo(task.repo)
        check = self.canonical_check(task.repo)
        if not check.get("clean"):
            raise WorktreeSafetyError(
                f"canonical checkout for {task.repo} is not clean"
            )
        base = self.resolve_base(task)
        path = self.worktrees_dir / task.task_id
        diagnostic = self.diagnostics_dir / f"{task.task_id}.jsonl"
        if path.exists():
            raise WorktreeSafetyError(
                f"worktree path already exists for {task.task_id}"
            )
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        branch = self._run(
            ["-C", str(canonical), "show-ref", "--verify", f"refs/heads/{task.branch}"],
            check=False,
        )
        if branch.returncode == 0:
            raise WorktreeSafetyError(f"task branch already exists: {task.branch}")
        created = self._run(
            [
                "-C",
                str(canonical),
                "worktree",
                "add",
                "-b",
                task.branch,
                str(path),
                base.base_sha,
            ],
            check=False,
        )
        if created.returncode != 0:
            raise WorktreeSafetyError(
                self._safe_output(created.stderr) or "git worktree add failed"
            )
        self.write_diagnostic(
            task.task_id,
            {"event": "allocated", "path": str(path), "branch": task.branch},
        )
        return WorktreeAllocation(
            task.task_id,
            task.repo,
            task.branch,
            path,
            diagnostic,
            base.base_branch,
            base.base_sha,
            base.origin_sha,
        )

    def resolve_base(self, task: TaskSpec) -> BaseResolution:
        """Fetch and resolve the reviewed origin base without moving checkout HEAD."""

        canonical = self.resolve_repo(task.repo)
        check = self.canonical_check(task.repo)
        if not check.get("clean"):
            raise WorktreeSafetyError(
                f"canonical checkout for {task.repo} is not clean"
            )
        fetched = self._run(
            [
                "-C",
                str(canonical),
                "fetch",
                "--no-tags",
                "origin",
                task.base_branch,
            ],
            check=False,
        )
        if fetched.returncode != 0:
            raise WorktreeSafetyError(
                self._safe_output(fetched.stderr) or "origin base fetch failed"
            )
        base_ref = f"refs/remotes/origin/{task.base_branch}"
        resolved = self._run(
            ["-C", str(canonical), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
            check=False,
        )
        if resolved.returncode != 0 or not resolved.stdout.strip():
            raise WorktreeSafetyError("configured origin base branch is unavailable")
        base_sha = resolved.stdout.strip().splitlines()[0].lower()
        if task.expected_base_sha and task.expected_base_sha.lower() != base_sha:
            raise WorktreeSafetyError(
                "expected_base_sha does not match the fetched origin base"
            )
        return BaseResolution(task.base_branch, base_sha, base_sha)

    def is_isolated_path(self, path: Path) -> bool:
        try:
            resolved = Path(path).resolve()
            root = self.worktrees_dir.resolve()
            return resolved != root and root in resolved.parents
        except OSError:
            return False

    def changed_paths(self, path: Path) -> tuple[str, ...]:
        result = self._run(
            ["-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
        )
        if result.returncode != 0:
            raise WorktreeSafetyError(
                self._safe_output(result.stderr) or "cannot inspect worktree status"
            )
        names: list[str] = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            name = line[3:]
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            names.append(name.strip('"'))
        return tuple(names)

    def verify_scope(self, task: TaskSpec, path: Path) -> tuple[str, ...]:
        if not path.is_dir() or not self.is_isolated_path(path):
            raise WorktreeSafetyError(
                "scope verification requires an existing isolated worktree"
            )
        changed = self.changed_paths(path)
        allowed = task.allowed_paths
        prohibited = set(task.prohibited_paths) | {
            ".git",
            ".env",
            ".env.local",
            "private-ledger",
            "data/bankroll_snapshot.json",
            "data/bankroll_snapshot_",
            "results/ledger",
            "models/production",
            "sealed-data",
        }
        violations = [
            item
            for item in changed
            if self._is_prohibited(item, prohibited)
            or (allowed and not any(self._under(item, root) for root in allowed))
        ]
        if violations:
            self.write_diagnostic(
                task.task_id, {"event": "scope_violation", "paths": violations}
            )
            raise ScopeViolation(
                f"worker changed paths outside its approved scope: {violations}"
            )
        self.write_diagnostic(
            task.task_id, {"event": "scope_verified", "paths": changed}
        )
        return changed

    def write_diagnostic(self, task_id: str, event: dict[str, Any]) -> None:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        target = self.diagnostics_dir / f"{task_id}.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")

    @staticmethod
    def _under(path: str, root: str) -> bool:
        return root == path or path.startswith(root.rstrip("/") + "/") or root == "*"

    @classmethod
    def _is_prohibited(cls, path: str, prohibited: set[str]) -> bool:
        return any(
            cls._under(path, root)
            or (root.endswith("_") and path.startswith(root))
            or (root in {".env", ".env.local"} and path.startswith(root + "."))
            for root in prohibited
        )

    @staticmethod
    def _safe_output(value: str) -> str:
        return value.replace("\n", " ").strip()[:1000]

    def _run(self, args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.git_executable, *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=check,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeSafetyError(
                f"Git worktree operation failed: {type(exc).__name__}"
            ) from exc
        return result


def default_repo_paths(repo_root: Path) -> dict[str, Path]:
    """Resolve canonical checkouts from explicit env vars, never discovery."""

    paths = {
        "Philip3006/sportsbrain": Path(
            os.getenv("SPORTSBRAIN_CANONICAL_REPO", repo_root)
        )
    }
    memory = os.getenv("SPORTSBRAIN_MEMORY_REPO")
    if memory:
        paths["Philip3006/SportsBrainMemory"] = Path(memory).expanduser()
    return paths
