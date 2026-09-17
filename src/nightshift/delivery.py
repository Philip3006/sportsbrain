"""Governed verify, commit, push, and exactly-one-PR delivery pipeline."""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .errors import DeliveryBlocked, DeliveryError
from .executors import redact
from .models import TaskRecord, TaskSpec, utc_now
from .verification import VerificationRunner
from .worktree import WorktreeManager


class PullRequestClient(Protocol):
    def find_or_create(
        self,
        task: TaskRecord,
        *,
        commit_sha: str,
        verification: Mapping[str, Any],
        lease_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]: ...


class GhPullRequestClient:
    """GitHub CLI adapter that refuses delivery when gh auth is not healthy."""

    def __init__(self, *, gh_executable: str = "gh") -> None:
        self.gh_executable = gh_executable

    def find_or_create(
        self,
        task: TaskRecord,
        *,
        commit_sha: str,
        verification: Mapping[str, Any],
        lease_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        auth = self._run(["auth", "status"], timeout=20)
        if auth.returncode != 0:
            raise DeliveryBlocked("DELIVERY_BLOCKED_GITHUB_AUTH")
        existing = self._list(task)
        if len(existing) > 1:
            raise DeliveryError("multiple pull requests match the exact task branch")
        if existing:
            pull = existing[0]
            self._validate_binding(task, pull, commit_sha)
            return {**pull, "reused": True}
        body = _pr_body(task, commit_sha, verification)
        if lease_guard is not None:
            lease_guard()
        created = self._run(
            [
                "pr",
                "create",
                "--repo",
                task.repo,
                "--base",
                task.base_branch,
                "--head",
                task.branch,
                "--title",
                f"Night Shift {task.task_id}",
                "--body",
                body,
            ],
            timeout=30,
        )
        if created.returncode != 0:
            raise DeliveryError(
                f"pull request creation failed: {redact(created.stderr)}"
            )
        final = self._list(task)
        if len(final) != 1:
            raise DeliveryError("pull request creation did not yield exactly one PR")
        self._validate_binding(task, final[0], commit_sha)
        return {**final[0], "reused": False}

    @staticmethod
    def _validate_binding(
        task: TaskRecord, pull: Mapping[str, Any], commit_sha: str
    ) -> None:
        body = str(pull.get("body", ""))
        required = (task.task_id, task.builder_id, commit_sha, task.base_branch)
        if any(value not in body for value in required):
            raise DeliveryError("pull request has conflicting task delivery evidence")
        if task.base_sha and task.base_sha not in body:
            raise DeliveryError("pull request is missing the authoritative base SHA")
        if (
            pull.get("headRefName") != task.branch
            or pull.get("baseRefName") != task.base_branch
        ):
            raise DeliveryError("pull request branch binding does not match the task")
        head_oid = pull.get("headRefOid")
        if not isinstance(head_oid, str) or head_oid.lower() != commit_sha.lower():
            raise DeliveryError("pull request head does not match the task commit")
        if task.base_sha:
            base_oid = pull.get("baseRefOid")
            if (
                not isinstance(base_oid, str)
                or base_oid.lower() != task.base_sha.lower()
            ):
                raise DeliveryError(
                    "pull request base does not match the authoritative base"
                )

    def _list(self, task: TaskRecord) -> list[dict[str, Any]]:
        result = self._run(
            [
                "pr",
                "list",
                "--repo",
                task.repo,
                "--head",
                task.branch,
                "--base",
                task.base_branch,
                "--state",
                "open",
                "--json",
                "number,url,headRefName,baseRefName,headRefOid,baseRefOid,body",
            ],
            timeout=20,
        )
        if result.returncode != 0:
            raise DeliveryError(f"pull request lookup failed: {redact(result.stderr)}")
        try:
            value = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise DeliveryError("pull request lookup returned invalid JSON") from exc
        if not isinstance(value, list):
            raise DeliveryError("pull request lookup returned an invalid shape")
        valid = [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("headRefName") == task.branch
            and item.get("baseRefName") == task.base_branch
        ]
        return valid

    def _run(
        self, args: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.gh_executable, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError(
                f"GitHub CLI unavailable: {type(exc).__name__}"
            ) from exc


class DeliveryPipeline:
    """Execute the independent verification and task-branch-only delivery gate."""

    def __init__(
        self,
        store: Any,
        worktrees: WorktreeManager,
        github: PullRequestClient,
        *,
        git_executable: str = "git",
        verifier: VerificationRunner | None = None,
        clock: Callable[[], Any] = utc_now,
    ) -> None:
        self.store = store
        self.worktrees = worktrees
        self.github = github
        self.git_executable = git_executable
        self.verifier = verifier or VerificationRunner()
        self.clock = clock

    def deliver(
        self,
        task: TaskRecord,
        *,
        worker_id: str,
        lease_generation: int,
        lease_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self._guard(lease_guard)
        if not task.worktree_path:
            raise DeliveryError("delivery requires an isolated worktree")
        path = Path(task.worktree_path)
        if not path.is_dir() or not self.worktrees.is_isolated_path(path):
            raise DeliveryError("delivery worktree is not isolated")
        self.worktrees.verify_scope(_spec(task), path)
        verification = (
            task.verification or self.verifier.run(_spec(task), path).as_dict()
        )
        if not verification.get("passed"):
            raise DeliveryError(
                f"{verification.get('failure_class', 'VERIFICATION_FAILED')}: required verification failed"
            )
        self._guard(lease_guard)
        self.store.record_verification(
            task.task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            evidence=verification,
            now=self.clock(),
        )
        task = self.store.get(task.task_id)
        self._guard(lease_guard)
        changed = self.worktrees.verify_scope(_spec(task), path)
        if not task.requires_pr:
            if changed:
                raise DeliveryError(
                    "read-only task changed files and cannot be delivered"
                )
            return {"verification": verification, "requires_pr": False}
        commit_sha = task.commit_sha
        if commit_sha:
            self._require_head(path, task.branch, commit_sha)
        else:
            if not changed:
                raise DeliveryError("code-changing task produced no reviewable change")
            commit_sha = self._commit(task, path, changed, lease_guard=lease_guard)
            self._guard(lease_guard)
            self.store.record_commit(
                task.task_id,
                worker_id=worker_id,
                lease_generation=lease_generation,
                commit_sha=commit_sha,
                now=self.clock(),
            )
            task = self.store.get(task.task_id)
        self._require_head(path, task.branch, commit_sha)
        remote_sha = self._push(task, path, lease_guard=lease_guard)
        self._guard(lease_guard)
        self.store.record_push(
            task.task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            remote_sha=remote_sha,
            now=self.clock(),
        )
        task = self.store.get(task.task_id)
        self._guard(lease_guard)
        pull = self._find_or_create(
            task,
            commit_sha=commit_sha,
            verification=verification,
            lease_guard=lease_guard,
        )
        number = pull.get("number")
        url = pull.get("url")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(url, str)
            or not url
        ):
            raise DeliveryError("GitHub pull request identity is incomplete")
        delivery = {
            "task_id": task.task_id,
            "builder_id": task.builder_id,
            "branch": task.branch,
            "commit_sha": commit_sha,
            "base_branch": task.base_branch,
            "base_sha": task.base_sha,
            "origin_sha": task.origin_sha,
            "verification": verification,
            "pr_number": number,
            "pr_url": url,
        }
        self._guard(lease_guard)
        self.store.record_pr(
            task.task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            number=number,
            url=url,
            evidence=delivery,
            reused=bool(pull.get("reused")),
            now=self.clock(),
        )
        return {"verification": verification, "delivery": delivery}

    @staticmethod
    def _guard(lease_guard: Callable[[], None] | None) -> None:
        """Run the caller's independent fence immediately before a mutation."""

        if lease_guard is not None:
            lease_guard()

    def _find_or_create(
        self,
        task: TaskRecord,
        *,
        commit_sha: str,
        verification: Mapping[str, Any],
        lease_guard: Callable[[], None] | None,
    ) -> dict[str, Any]:
        """Pass the fence to governed clients while keeping old fixtures usable."""

        method = self.github.find_or_create
        try:
            parameters = inspect.signature(method).parameters.values()
            accepts_guard = any(
                parameter.name == "lease_guard"
                or parameter.kind is parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_guard = False
        kwargs: dict[str, Any] = {
            "commit_sha": commit_sha,
            "verification": verification,
        }
        if accepts_guard:
            kwargs["lease_guard"] = lease_guard
        return method(task, **kwargs)

    def _commit(
        self,
        task: TaskRecord,
        path: Path,
        changed: tuple[str, ...],
        *,
        lease_guard: Callable[[], None] | None = None,
    ) -> str:
        self._guard(lease_guard)
        self._run(path, ["add", "--", *sorted(changed)], timeout=30)
        staged = self._names(path, ["diff", "--cached", "--name-only"])
        if set(staged) != set(changed):
            raise DeliveryError(
                "staged paths differ from the independently verified scope"
            )
        self._guard(lease_guard)
        self._run(
            path, ["commit", "-m", f"Night Shift task {task.task_id}"], timeout=60
        )
        sha = self._run(path, ["rev-parse", "HEAD"], timeout=15).stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            raise DeliveryError("task commit SHA is invalid")
        if self.worktrees.changed_paths(path):
            raise DeliveryError("worktree is not clean after task commit")
        return sha.lower()

    def _push(
        self,
        task: TaskRecord,
        path: Path,
        *,
        lease_guard: Callable[[], None] | None = None,
    ) -> str:
        self._guard(lease_guard)
        self._run(path, ["push", "origin", f"HEAD:{task.branch}"], timeout=120)
        remote = self._run(
            path, ["ls-remote", "origin", f"refs/heads/{task.branch}"], timeout=30
        ).stdout.strip()
        value = remote.split()[0] if remote else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
            raise DeliveryError("remote task branch SHA is unavailable")
        head = self._run(path, ["rev-parse", "HEAD"], timeout=15).stdout.strip()
        if value.lower() != head.lower():
            raise DeliveryError("remote task branch SHA does not match the task commit")
        return value.lower()

    def _require_head(self, path: Path, branch: str, commit_sha: str) -> None:
        actual_branch = self._run(
            path, ["branch", "--show-current"], timeout=15
        ).stdout.strip()
        if actual_branch != branch:
            raise DeliveryError("task worktree is on an unexpected branch")
        actual_sha = self._run(path, ["rev-parse", "HEAD"], timeout=15).stdout.strip()
        if actual_sha.lower() != commit_sha.lower():
            raise DeliveryError("task worktree HEAD is not the recorded task commit")
        if self.worktrees.changed_paths(path):
            raise DeliveryError(
                "task worktree must be clean before push or PR delivery"
            )

    def _names(self, path: Path, args: list[str]) -> tuple[str, ...]:
        return tuple(
            item
            for item in self._run(path, args, timeout=30).stdout.splitlines()
            if item
        )

    def _run(
        self, path: Path, args: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.git_executable, "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError(
                f"Git delivery command failed: {type(exc).__name__}"
            ) from exc
        if result.returncode != 0:
            raise DeliveryError(redact(result.stderr) or "Git delivery command failed")
        return result


def _spec(task: TaskRecord) -> TaskSpec:
    return TaskSpec(
        task_id=task.task_id,
        builder_id=task.builder_id,
        objective=task.objective,
        branch=task.branch,
        repo=task.repo,
        task_type=task.task_type,
        template_id=task.template_id,
        payload=task.payload,
        risk_class=task.risk_class,
        requires_approval=task.requires_approval,
        priority=task.priority,
        max_attempts=task.max_attempts,
        parent_task_id=task.parent_task_id,
        dependency_ids=task.dependency_ids,
        requested_by=task.requested_by,
        allowed_paths=task.allowed_paths,
        prohibited_paths=task.prohibited_paths,
        resource_locks=task.resource_locks,
        expected_base_sha=task.expected_base_sha,
        base_branch=task.base_branch,
        required_tests=task.required_tests,
        verification_commands=task.verification_commands,
        max_runtime_seconds=task.max_runtime_seconds,
        requires_pr=task.requires_pr,
        roadmap_item_id=task.roadmap_item_id,
        debug_budget=task.debug_budget,
        repeated_failure_limit=task.repeated_failure_limit,
    )


def _pr_body(task: TaskRecord, commit_sha: str, verification: Mapping[str, Any]) -> str:
    return (
        f"Night Shift task_id: {task.task_id}\n"
        f"Builder: {task.builder_id}\n"
        f"Commit SHA: {commit_sha}\n"
        f"Base branch: {task.base_branch}\n"
        f"Base SHA: {task.base_sha}\n"
        f"Origin SHA: {task.origin_sha}\n\n"
        f"Verification: {json.dumps(dict(verification), sort_keys=True)}"
    )
