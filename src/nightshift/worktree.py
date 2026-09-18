"""Safe, isolated Git worktree allocation and scope verification.

The production checkout is a runtime/data surface, not the Night Shift Git
control plane.  A task may inspect its configured runtime checkout for
diagnostics, but all fetch, branch, worktree, commit, and push operations are
performed through the dedicated control repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_repo import control_repo_lock_path, run_locked_control_repo_operation
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


@dataclass(frozen=True)
class RuntimeDirtyEvidence:
    """One narrowly governed runtime-mutated path."""

    path: str
    job: str
    script: str
    reason: str
    observed_evidence: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> RuntimeDirtyEvidence:
        fields = ("path", "job", "script", "reason", "observed_evidence")
        if any(
            not isinstance(raw.get(field), str) or not raw[field].strip()
            for field in fields
        ):
            raise WorktreeSafetyError(
                "runtime dirty evidence entries require explicit string fields"
            )
        path = raw["path"].strip().replace("\\", "/")
        if path.startswith("/") or ".." in Path(path).parts:
            raise WorktreeSafetyError("runtime dirty evidence paths must be relative")
        return cls(
            path=path,
            job=raw["job"].strip(),
            script=raw["script"].strip(),
            reason=raw["reason"].strip(),
            observed_evidence=raw["observed_evidence"].strip(),
        )


class RuntimeDirtyPolicy:
    """Classify only evidence-backed runtime mutations as expected dirtiness."""

    def __init__(self, entries: dict[str, tuple[RuntimeDirtyEvidence, ...]]) -> None:
        self._entries = {repo: tuple(values) for repo, values in entries.items()}

    @classmethod
    def from_mapping(cls, raw: Any) -> RuntimeDirtyPolicy:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise WorktreeSafetyError("runtime dirty policy must use version 1")
        repositories = raw.get("repositories")
        if not isinstance(repositories, dict):
            raise WorktreeSafetyError(
                "runtime dirty policy repositories must be an object"
            )
        entries: dict[str, tuple[RuntimeDirtyEvidence, ...]] = {}
        for repo, values in repositories.items():
            if not isinstance(repo, str) or not isinstance(values, list):
                raise WorktreeSafetyError("runtime dirty policy entries are malformed")
            parsed: list[RuntimeDirtyEvidence] = []
            for value in values:
                if not isinstance(value, dict):
                    raise WorktreeSafetyError(
                        "runtime dirty policy evidence must be objects"
                    )
                parsed.append(RuntimeDirtyEvidence.from_mapping(value))
            paths = [item.path for item in parsed]
            if len(paths) != len(set(paths)):
                raise WorktreeSafetyError(f"duplicate runtime dirty path for {repo}")
            entries[repo] = tuple(parsed)
        return cls(entries)

    @classmethod
    def from_file(cls, path: Path) -> RuntimeDirtyPolicy:
        try:
            return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorktreeSafetyError(
                f"cannot read runtime dirty policy: {path}"
            ) from exc

    def evidence_for(self, repo: str, path: str) -> RuntimeDirtyEvidence | None:
        normalized = path.replace("\\", "/").strip("/")
        return next(
            (item for item in self._entries.get(repo, ()) if item.path == normalized),
            None,
        )

    def classify(
        self, repo: str, paths: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        expected: list[str] = []
        unexpected: list[str] = []
        for path in paths:
            if self.evidence_for(repo, path) is not None:
                expected.append(path)
            else:
                unexpected.append(path)
        return tuple(expected), tuple(unexpected)


class WorktreeManager:
    """Allocate one branch/worktree per task without shell interpolation."""

    def __init__(
        self,
        runtime_dir: Path,
        repo_paths: dict[str, Path],
        *,
        control_repo_paths: dict[str, Path] | None = None,
        worktrees_dir: Path | None = None,
        runtime_dirty_policy: RuntimeDirtyPolicy | None = None,
        expected_remote_urls: dict[str, str] | None = None,
        git_executable: str = "git",
        control_lock_path: Path | None = None,
        control_lock_timeout_seconds: float = 30.0,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser()
        self.repo_paths = {
            repo: Path(path).expanduser() for repo, path in repo_paths.items()
        }
        # A direct WorktreeManager constructed by a unit fixture may use one
        # local repository for both roles.  Production construction always
        # supplies the explicit dedicated control repository.
        self.control_repo_paths = {
            repo: Path(path).expanduser()
            for repo, path in (control_repo_paths or repo_paths).items()
        }
        self.has_dedicated_control_repo = control_repo_paths is not None
        self.expected_remote_urls = dict(expected_remote_urls or {})
        self.git_executable = git_executable
        self.control_lock_path = (
            Path(control_lock_path).expanduser() if control_lock_path else None
        )
        self.control_lock_timeout_seconds = float(control_lock_timeout_seconds)
        if self.control_lock_timeout_seconds < 0:
            raise ValueError("control_lock_timeout_seconds must be non-negative")
        configured_root = os.getenv("SPORTSBRAIN_NIGHTSHIFT_WORKTREE_ROOT")
        self.worktrees_dir = Path(
            worktrees_dir or configured_root or (self.runtime_dir / "worktrees")
        ).expanduser()
        self.diagnostics_dir = self.runtime_dir / "diagnostics"
        self.runtime_dirty_policy = runtime_dirty_policy or RuntimeDirtyPolicy({})

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
        changed = self._status_paths(status.stdout) if status.returncode == 0 else ()
        expected, unexpected = self.runtime_dirty_policy.classify(repo, changed)
        result["clean"] = status.returncode == 0 and not changed
        result["runtime_dirty_paths"] = list(expected)
        result["unexpected_dirty_paths"] = list(unexpected)
        result["dirty_class"] = (
            "UNKNOWN"
            if status.returncode != 0
            else "CLEAN"
            if not changed
            else "UNEXPECTED_SOURCE_DIRTY"
            if unexpected
            else "RUNTIME_CHECKOUT_DIRTY_EXPECTED"
        )
        result["safe_for_allocation"] = status.returncode == 0 and not unexpected
        if status.returncode != 0:
            result["error"] = "git status failed"
        elif status.stdout.strip():
            result["dirty_entries"] = len(changed)
        return result

    @staticmethod
    def _status_paths(output: str) -> tuple[str, ...]:
        paths: list[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            name = line[3:]
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            paths.append(name.strip().strip('"'))
        return tuple(paths)

    def resolve_control_repo(self, repo: str) -> Path:
        try:
            return self.control_repo_paths[repo]
        except KeyError as exc:
            raise WorktreeSafetyError(
                f"no dedicated Night Shift control repository is configured for {repo}"
            ) from exc

    def control_check(
        self, repo: str, *, base_branch: str = "main", fetch: bool = False
    ) -> dict[str, Any]:
        """Validate and optionally refresh the isolated Git control repository."""

        path = self.resolve_control_repo(repo)
        result: dict[str, Any] = {
            "repo": repo,
            "path": str(path),
            "exists": path.exists(),
            "is_control_repository": False,
            "remote": None,
            "remote_ok": False,
            "fetch_ok": False,
            "origin_sha": None,
        }
        if self.has_dedicated_control_repo:
            try:
                if path.resolve() == self.resolve_repo(repo).resolve():
                    result["error"] = (
                        "control repository must be separate from the canonical checkout"
                    )
                    return result
            except OSError:
                result["error"] = "control repository path cannot be resolved"
                return result
        if not path.exists():
            result["error"] = "control repository does not exist"
            return result
        bare = self._run(
            self._git_path_args(path) + ["rev-parse", "--is-bare-repository"],
            check=False,
        )
        is_bare = bare.returncode == 0 and bare.stdout.strip() == "true"
        top = self._run(
            self._git_path_args(path) + ["rev-parse", "--is-inside-work-tree"],
            check=False,
        )
        is_worktree = top.returncode == 0 and top.stdout.strip() == "true"
        result["is_control_repository"] = is_bare or is_worktree
        if not result["is_control_repository"]:
            result["error"] = "control repository is not a Git repository"
            return result
        remote = self._run(
            self._git_path_args(path) + ["remote", "get-url", "origin"], check=False
        )
        if remote.returncode == 0:
            value = remote.stdout.strip()
            result["remote"] = value
            expected = self.expected_remote_urls.get(repo)
            result["remote_ok"] = expected is None or self._normalize_remote(
                value
            ) == self._normalize_remote(expected)
        if not result["remote_ok"]:
            result["error"] = "control repository origin is missing or incorrect"
            return result
        if fetch:
            fetched = self._locked_control_run(
                path,
                self._git_path_args(path)
                + ["fetch", "--no-tags", "origin", base_branch],
            )
            result["fetch_ok"] = fetched.returncode == 0
            if not result["fetch_ok"]:
                result["error"] = (
                    self._safe_output(fetched.stderr)
                    or "control repository fetch failed"
                )
                return result
        resolved = self._run(
            self._git_path_args(path)
            + [
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{base_branch}^{{commit}}",
            ],
            check=False,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            result["origin_sha"] = resolved.stdout.strip().splitlines()[0].lower()
            result["fetch_ok"] = result["fetch_ok"] or not fetch
        else:
            result["error"] = "origin base SHA is unavailable"
        return result

    def allocate(self, task: TaskSpec) -> WorktreeAllocation:
        base = self.resolve_base(task)
        check = self.canonical_check(task.repo)
        if not check.get("safe_for_allocation"):
            raise WorktreeSafetyError(
                f"canonical checkout for {task.repo} is not clean: unexpected source dirtiness"
            )
        path = self.worktrees_dir / task.task_id
        diagnostic = self.diagnostics_dir / f"{task.task_id}.jsonl"
        if path.exists():
            raise WorktreeSafetyError(
                f"worktree path already exists for {task.task_id}"
            )
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        control = self.resolve_control_repo(task.repo)
        branch = self._run(
            self._git_path_args(control)
            + ["show-ref", "--verify", f"refs/heads/{task.branch}"],
            check=False,
        )
        if branch.returncode == 0:
            raise WorktreeSafetyError(f"task branch already exists: {task.branch}")
        created = self._run(
            [
                *self._git_path_args(control),
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

        control = self.resolve_control_repo(task.repo)
        control_check = self.control_check(
            task.repo, base_branch=task.base_branch, fetch=False
        )
        if not control_check.get("is_control_repository") or not control_check.get(
            "remote_ok"
        ):
            raise WorktreeSafetyError(
                control_check.get(
                    "error", "Night Shift control repository is unavailable"
                )
            )
        fetched = self._locked_control_run(
            control,
            self._git_path_args(control)
            + [
                "fetch",
                "--no-tags",
                "origin",
                task.base_branch,
            ],
        )
        if fetched.returncode != 0:
            raise WorktreeSafetyError(
                self._safe_output(fetched.stderr) or "origin base fetch failed"
            )
        base_ref = f"refs/remotes/origin/{task.base_branch}"
        resolved = self._run(
            self._git_path_args(control)
            + ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
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

    def _locked_control_run(
        self, control_repo: Path, args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        """Serialize only the shared fetch/ref-update operation."""

        return run_locked_control_repo_operation(
            control_repo,
            lambda: self._run(args, check=False),
            lock_path=(
                self.control_lock_path
                or control_repo_lock_path(control_repo)
            ),
            timeout_seconds=self.control_lock_timeout_seconds,
        )

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

    @staticmethod
    def _git_path_args(path: Path) -> list[str]:
        """Return a Git invocation prefix for either a bare or normal repo."""

        if (path / "HEAD").is_file() and not (path / ".git").exists():
            return ["--git-dir", str(path)]
        return ["-C", str(path)]

    @staticmethod
    def _normalize_remote(value: str) -> str:
        normalized = value.strip().rstrip("/")
        return normalized.removesuffix(".git").lower()


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


def default_control_repo_paths() -> dict[str, Path]:
    """Return explicit Night Shift control repositories, never production paths."""

    root = Path(
        os.getenv(
            "SPORTSBRAIN_NIGHTSHIFT_CONTROL_REPO",
            Path.home()
            / "Library"
            / "Application Support"
            / "SportsBrain"
            / "night-shift"
            / "repo.git",
        )
    ).expanduser()
    paths = {"Philip3006/sportsbrain": root}
    memory = os.getenv("SPORTSBRAIN_MEMORY_CONTROL_REPO")
    if memory:
        paths["Philip3006/SportsBrainMemory"] = Path(memory).expanduser()
    return paths


def default_control_remote_urls() -> dict[str, str]:
    """Return the governed upstream URLs for the configured control repos."""

    urls = {"Philip3006/sportsbrain": "https://github.com/Philip3006/sportsbrain.git"}
    if os.getenv("SPORTSBRAIN_MEMORY_CONTROL_REPO"):
        urls["Philip3006/SportsBrainMemory"] = (
            "https://github.com/Philip3006/SportsBrainMemory.git"
        )
    return urls


def default_worktree_root() -> Path:
    """Return the isolated worktree root used by production workers."""

    return Path(
        os.getenv(
            "SPORTSBRAIN_NIGHTSHIFT_WORKTREE_ROOT",
            Path.home()
            / "Library"
            / "Application Support"
            / "SportsBrain"
            / "night-shift"
            / "worktrees",
        )
    ).expanduser()


def default_runtime_dirty_policy(repo_root: Path) -> RuntimeDirtyPolicy:
    path = Path(repo_root) / "config" / "night_shift" / "runtime_dirty.json"
    return RuntimeDirtyPolicy.from_file(path)
