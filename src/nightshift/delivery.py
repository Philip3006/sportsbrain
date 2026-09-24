"""Governed verify, commit, push, and exactly-one-PR delivery pipeline."""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from .errors import DeliveryBlocked, DeliveryError
from .executors import redact
from .models import TaskRecord, TaskSpec, TaskState, utc_now
from .reconciliation import (
    DeliveryBaseDrift,
    DeliveryReconciliationError,
    classify_drift,
)
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

    def verify_merged(self, task: TaskRecord) -> dict[str, Any]: ...


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

    def verify_merged(self, task: TaskRecord) -> dict[str, Any]:
        """Read GitHub merge state and verify every recorded delivery binding."""

        if task.pr_number is None:
            raise DeliveryError("merge verification requires a recorded pull request")
        result = self._run(
            [
                "pr",
                "view",
                str(task.pr_number),
                "--repo",
                task.repo,
                "--json",
                "number,state,mergedAt,headRefName,baseRefName,headRefOid,baseRefOid,mergeCommit",
            ],
            timeout=20,
        )
        if result.returncode != 0:
            raise DeliveryError(
                f"pull request merge verification failed: {redact(result.stderr)}"
            )
        try:
            pull = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise DeliveryError(
                "pull request merge verification returned invalid JSON"
            ) from exc
        if not isinstance(pull, dict):
            raise DeliveryError(
                "pull request merge verification returned an invalid shape"
            )
        merged_at = pull.get("mergedAt")
        if (
            str(pull.get("state", "")).upper() != "MERGED"
            or not isinstance(merged_at, str)
            or not merged_at.strip()
        ):
            raise DeliveryError("pull request is not reported as merged")
        number = pull.get("number")
        if isinstance(number, bool) or number != task.pr_number:
            raise DeliveryError("merged pull request number does not match the task")
        if pull.get("headRefName") != task.branch:
            raise DeliveryError("merged pull request head branch does not match the task")
        if pull.get("baseRefName") != task.base_branch:
            raise DeliveryError("merged pull request base branch does not match the task")
        head_oid = pull.get("headRefOid")
        if (
            not isinstance(head_oid, str)
            or not task.commit_sha
            or head_oid.lower() != task.commit_sha.lower()
            or not task.remote_sha
            or head_oid.lower() != task.remote_sha.lower()
        ):
            raise DeliveryError("merged pull request head does not match recorded SHAs")
        base_oid = pull.get("baseRefOid")
        if (
            not isinstance(base_oid, str)
            or not task.base_sha
            or base_oid.lower() != task.base_sha.lower()
            or (task.origin_sha and base_oid.lower() != task.origin_sha.lower())
        ):
            raise DeliveryError("merged pull request base does not match recorded base")
        return {
            "source": "github-readonly",
            "repository": task.repo,
            "merged": True,
            "merged_at": merged_at,
            "pr_number": number,
            "head_ref_name": pull["headRefName"],
            "head_ref_oid": head_oid.lower(),
            "base_ref_name": pull["baseRefName"],
            "base_ref_oid": base_oid.lower(),
            "merge_commit": pull.get("mergeCommit"),
        }

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
                raise DeliveryBaseDrift(
                    "pull request base does not match the authoritative base",
                    original_base_sha=task.base_sha,
                    authoritative_base_sha=base_oid,
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
        changed = self.worktrees.verify_scope(_spec(task), path)
        commit_sha = task.commit_sha
        if task.requires_pr and not commit_sha:
            if not changed:
                raise DeliveryError("code-changing task produced no reviewable change")
            # Keep the pre-commit safety gate: a lease can expire while an
            # expensive verifier is running, and must prevent any commit.
            preverification = self.verifier.run(_spec(task), path).as_dict()
            if preverification.get("passed") is not True:
                raise DeliveryError(
                    f"{preverification.get('failure_class', 'VERIFICATION_FAILED')}: required verification failed"
                )
            self._guard(lease_guard)
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
        target_sha = commit_sha or self._head_sha(path)
        verification = task.verification
        if not self._verification_matches(
            verification,
            task=task,
            target_sha=target_sha,
        ):
            cached = (
                self.store.get_verification_evidence(
                    repo=task.repo,
                    target_sha=target_sha,
                    matrix_version=task.verification_matrix_version,
                )
                if target_sha
                else None
            )
            verification = cached or self.verifier.run(_spec(task), path).as_dict()
        verification = self._bind_verification(
            verification or {}, task=task, target_sha=target_sha
        )
        if not verification.get("passed"):
            raise DeliveryError(
                f"{verification.get('failure_class', 'VERIFICATION_FAILED')}: required verification failed"
            )
        if target_sha and not self.store.get_verification_evidence(
            repo=task.repo,
            target_sha=target_sha,
            matrix_version=task.verification_matrix_version,
        ):
            verification = self.store.save_verification_evidence(
                repo=task.repo,
                target_sha=target_sha,
                matrix_version=task.verification_matrix_version,
                app_owner=task.app_owner,
                execution_worker=task.terminal_worker_id,
                evidence=verification,
                now=self.clock(),
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
        if not task.requires_pr:
            if changed:
                raise DeliveryError(
                    "read-only task changed files and cannot be delivered"
                )
            return {"verification": verification, "requires_pr": False}
        commit_sha = task.commit_sha
        if commit_sha:
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
        if task.base_sha:
            authoritative = self.worktrees.authoritative_base_sha(
                task.repo, base_branch=task.base_branch
            )
            if authoritative.lower() != task.base_sha.lower():
                drift = self._drift_evidence(task, authoritative)
                raise DeliveryBaseDrift(
                    "authoritative base advanced after task start",
                    original_base_sha=task.base_sha,
                    authoritative_base_sha=authoritative,
                    classification=str(drift["classification"]),
                )
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
        verification = {**verification, "pr_number": number}
        self._guard(lease_guard)
        self.store.record_verification(
            task.task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            evidence=verification,
            now=self.clock(),
        )
        delivery = {
            "task_id": task.task_id,
            "builder_id": task.builder_id,
            "app_owner": task.app_owner,
            "execution_worker": task.terminal_worker_id,
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
    def _verification_matches(
        verification: Mapping[str, Any] | None,
        *,
        task: TaskRecord,
        target_sha: str | None,
    ) -> bool:
        return bool(
            isinstance(verification, Mapping)
            and target_sha
            and verification.get("passed") is True
            and str(verification.get("target_sha", "")).lower() == target_sha.lower()
            and verification.get("repository") == task.repo
            and verification.get("app_owner") == task.app_owner
            and verification.get("execution_worker") == task.terminal_worker_id
            and verification.get("matrix_version") == task.verification_matrix_version
        )

    @staticmethod
    def _bind_verification(
        verification: Mapping[str, Any], *, task: TaskRecord, target_sha: str | None
    ) -> dict[str, Any]:
        bound = dict(verification)
        bound.update(
            {
                "repository": task.repo,
                "target_sha": target_sha,
                "app_owner": task.app_owner,
                "execution_worker": task.terminal_worker_id,
                "matrix_version": task.verification_matrix_version,
            }
        )
        return bound

    def _head_sha(self, path: Path) -> str | None:
        try:
            return self._run_path(path, ["rev-parse", "HEAD"], timeout=15).stdout.strip()
        except DeliveryError:
            return None

    def reconcile(
        self,
        task: TaskRecord,
        *,
        actor: str,
        lease_generation: int | None = None,
        lease_guard: Callable[[], None] | None = None,
    ) -> TaskRecord:
        """Rematerialize a preserved delta onto the current authoritative base.

        The operation is deterministic by task id and persisted attempt number.
        It only creates a fresh task branch, applies the original commit delta,
        verifies it, and opens a PR.  It never rewrites the original branch and
        never uses a force push.
        """

        current = self.store.get(task.task_id)
        if current.state is not TaskState.DELIVERY_RECONCILING:
            raise DeliveryReconciliationError(
                "task must be in DELIVERY_RECONCILING before rematerialization"
            )
        reconciliation = dict(current.reconciliation or {})
        attempt = current.reconciliation_attempts
        if not 1 <= attempt <= 2:
            raise DeliveryReconciliationError("delivery reconciliation attempt limit exhausted")
        original_base = str(
            reconciliation.get("original_base_sha") or current.base_sha or ""
        )
        original_commit = str(
            reconciliation.get("original_commit_sha") or current.commit_sha or ""
        )
        authoritative = str(
            reconciliation.get("authoritative_base_sha")
            or self.worktrees.authoritative_base_sha(
                current.repo, base_branch=current.base_branch
            )
        )
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", original_base):
            raise DeliveryReconciliationError("original task base SHA is unavailable")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", original_commit):
            raise DeliveryReconciliationError("original implementation commit SHA is unavailable")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", authoritative):
            raise DeliveryReconciliationError("authoritative recovery base SHA is invalid")
        branch = str(
            reconciliation.get("recovery_branch")
            or f"nightshift/recovery/{current.task_id}/attempt-{attempt}"
        )
        progress: dict[str, Any] = {
            "original_base_sha": original_base,
            "original_commit_sha": original_commit,
            "authoritative_base_sha": authoritative.lower(),
            "recovery_branch": branch,
        }
        self._update_reconciliation(
            current,
            actor=actor,
            evidence=progress,
            lease_generation=lease_generation,
            lease_guard=lease_guard,
        )
        current = self.store.get(current.task_id)
        reconciliation = dict(current.reconciliation or {})

        try:
            refreshed_authoritative = self.worktrees.authoritative_base_sha(
                current.repo, base_branch=current.base_branch
            )
            if refreshed_authoritative.lower() != authoritative.lower():
                raise DeliveryBaseDrift(
                    "authoritative base moved during delivery reconciliation",
                    original_base_sha=authoritative,
                    authoritative_base_sha=refreshed_authoritative,
                    classification="AUTHORITATIVE_BASE_MOVED_DURING_RECOVERY",
                )
            self.worktrees.fetch_task_branch(current.repo, current.branch)
            drift = self._drift_evidence(current, authoritative)
            progress.update(drift)
            self._update_reconciliation(
                current,
                actor=actor,
                evidence=progress,
                lease_generation=lease_generation,
                lease_guard=lease_guard,
            )
            current = self.store.get(current.task_id)
            reconciliation = dict(current.reconciliation or {})

            recovery_path_value = reconciliation.get("recovery_worktree_path")
            recovery_path = (
                Path(str(recovery_path_value)) if recovery_path_value else None
            )
            recovery_task_id = f"{current.task_id}-recovery-{attempt}"
            recovery_spec = replace(
                _spec(current),
                task_id=recovery_task_id,
                branch=branch,
                expected_base_sha=authoritative.lower(),
            )
            if recovery_path is None:
                allocation = self.worktrees.allocate(recovery_spec)
                recovery_path = allocation.path
                progress["recovery_worktree_path"] = str(recovery_path)
                self._update_reconciliation(
                    current,
                    actor=actor,
                    evidence=progress,
                    lease_generation=lease_generation,
                    lease_guard=lease_guard,
                )
            if not recovery_path.is_dir() or not self.worktrees.is_isolated_path(
                recovery_path
            ):
                raise DeliveryReconciliationError("recovery worktree is not isolated")

            recovery_commit = reconciliation.get("recovery_commit_sha")
            if recovery_commit:
                self._require_head(recovery_path, branch, str(recovery_commit))
            else:
                patch = self._task_patch(current, original_base, original_commit)
                if not patch:
                    raise DeliveryReconciliationError("task delta is empty")
                self._apply_patch(recovery_path, patch)
                changed = self.worktrees.verify_scope(recovery_spec, recovery_path)
                if not changed:
                    raise DeliveryReconciliationError(
                        "reconciliation produced no scoped change"
                    )
                recovery_commit = self._commit(
                    replace(current, task_id=recovery_task_id, branch=branch),
                    recovery_path,
                    changed,
                    lease_guard=lease_guard,
                )
                verification = self.verifier.run(
                    recovery_spec,
                    recovery_path,
                ).as_dict()
                verification["target_sha"] = recovery_commit
                verification["repository"] = current.repo
                verification["app_owner"] = current.app_owner
                verification["execution_worker"] = current.terminal_worker_id
                verification["matrix_version"] = current.verification_matrix_version
                if verification.get("passed") is not True:
                    raise DeliveryReconciliationError(
                        f"{verification.get('failure_class', 'VERIFICATION_FAILED')}: verification failed after rematerialization"
                    )
                progress.update(
                    {
                        "recovery_commit_sha": recovery_commit,
                        "commit_sha": recovery_commit,
                        "verification": verification,
                    }
                )
                self._update_reconciliation(
                    current,
                    actor=actor,
                    evidence=progress,
                    lease_generation=lease_generation,
                    lease_guard=lease_guard,
                )
            recovery_commit = str(recovery_commit)
            verification = reconciliation.get("verification") or progress.get(
                "verification"
            )
            if not isinstance(verification, Mapping) or verification.get("passed") is not True:
                verification = self.verifier.run(recovery_spec, recovery_path).as_dict()
                if verification.get("passed") is not True:
                    raise DeliveryReconciliationError(
                        "verification failed after recovery restart"
                    )
            recovery_task = replace(
                current,
                task_id=recovery_task_id,
                branch=branch,
                worktree_path=str(recovery_path),
                expected_base_sha=authoritative.lower(),
                base_sha=authoritative.lower(),
                origin_sha=authoritative.lower(),
                commit_sha=recovery_commit,
                remote_sha=str(reconciliation.get("recovery_remote_sha"))
                if reconciliation.get("recovery_remote_sha")
                else None,
            )
            remote_sha = reconciliation.get("recovery_remote_sha")
            if not remote_sha:
                remote_sha = self._push(
                    recovery_task, recovery_path, lease_guard=lease_guard
                )
                progress.update(
                    {
                        "recovery_remote_sha": remote_sha,
                        "remote_sha": remote_sha,
                    }
                )
                self._update_reconciliation(
                    current,
                    actor=actor,
                    evidence=progress,
                    lease_generation=lease_generation,
                    lease_guard=lease_guard,
                )
                current = self.store.get(current.task_id)
                reconciliation = dict(current.reconciliation or {})
                recovery_task = replace(recovery_task, remote_sha=remote_sha)
            pull_number = reconciliation.get("pr_number")
            pull_url = reconciliation.get("pr_url")
            pull: Mapping[str, Any]
            if pull_number and pull_url:
                pull = {"number": pull_number, "url": pull_url, "reused": True}
            else:
                pull = self._find_or_create(
                    recovery_task,
                    commit_sha=recovery_commit,
                    verification=verification,
                    lease_guard=lease_guard,
                )
                pull_number = pull.get("number")
                pull_url = pull.get("url")
                if (
                    isinstance(pull_number, bool)
                    or not isinstance(pull_number, int)
                    or not isinstance(pull_url, str)
                    or not pull_url
                ):
                    raise DeliveryReconciliationError(
                        "recovery PR identity is incomplete"
                    )
                progress.update(
                    {
                        "pr_number": pull_number,
                        "pr_url": pull_url,
                    }
                )
                self._update_reconciliation(
                    current,
                    actor=actor,
                    evidence=progress,
                    lease_generation=lease_generation,
                    lease_guard=lease_guard,
                )
            delivery = {
                "task_id": current.task_id,
                "builder_id": current.builder_id,
                "app_owner": current.app_owner,
                "execution_worker": current.terminal_worker_id,
                "branch": branch,
                "commit_sha": recovery_commit,
                "base_branch": current.base_branch,
                "base_sha": authoritative.lower(),
                "origin_sha": authoritative.lower(),
                "original_commit_sha": original_commit,
                "original_base_sha": original_base,
                "recovery_branch": branch,
                "recovery_commit_sha": recovery_commit,
                "recovery_base_sha": authoritative.lower(),
                "verification": dict(verification),
                "pr_number": pull_number,
                "pr_url": pull_url,
                "delivery_recovered": True,
            }
            progress.update(
                {
                    "commit_sha": recovery_commit,
                    "remote_sha": remote_sha,
                    "pr_number": pull_number,
                    "pr_url": pull_url,
                    "verification": dict(verification),
                    "delivery": delivery,
                }
            )
            return self.store.finish_delivery_reconciliation(
                current.task_id,
                actor=actor,
                success=True,
                evidence=progress,
                lease_generation=lease_generation,
                now=self.clock(),
            )
        except DeliveryBaseDrift as exc:
            latest = self.store.get(current.task_id)
            if latest.reconciliation_attempts >= 2:
                failure = dict(latest.reconciliation or {})
                failure.update(
                    {
                        "failure_reason": str(exc)[:4000],
                        "failure_class": type(exc).__name__,
                    }
                )
                return self.store.finish_delivery_reconciliation(
                    latest.task_id,
                    actor=actor,
                    success=False,
                    evidence=failure,
                    lease_generation=lease_generation,
                    now=self.clock(),
                )
            raise

    def _update_reconciliation(
        self,
        task: TaskRecord,
        *,
        actor: str,
        evidence: Mapping[str, Any],
        lease_generation: int | None,
        lease_guard: Callable[[], None] | None,
    ) -> None:
        self._guard(lease_guard)
        self.store.update_delivery_reconciliation(
            task.task_id,
            actor=actor,
            evidence=evidence,
            lease_generation=lease_generation,
            now=self.clock(),
        )

    def _drift_evidence(self, task: TaskRecord, authoritative: str) -> dict[str, Any]:
        original = task.base_sha or ""
        commit = task.commit_sha or ""
        if not original or not commit:
            return {
                "classification": "BASE_DRIFT_WITHOUT_COMPLETE_DELTA",
                "upstream_paths": [],
                "task_paths": [],
                "overlapping_paths": [],
            }
        control = self.worktrees.resolve_control_repo(task.repo)
        self.worktrees.fetch_task_branch(task.repo, task.branch)
        upstream = self._names_at(control, ["diff", "--name-only", original, authoritative])
        changed = self._names_at(control, ["diff", "--name-only", original, commit])
        return classify_drift(upstream, changed).as_dict()

    def _task_patch(self, task: TaskRecord, original_base: str, commit: str) -> str:
        control = self.worktrees.resolve_control_repo(task.repo)
        result = self._run_control(
            control, ["diff", "--binary", f"{original_base}..{commit}"]
        )
        if result.returncode != 0:
            raise DeliveryReconciliationError(
                "cannot materialize the preserved task delta"
            )
        return result.stdout

    def _apply_patch(self, path: Path, patch: str) -> None:
        checked = self._run_with_input(path, ["apply", "--3way", "--check"], patch)
        if checked.returncode != 0:
            raise DeliveryReconciliationError(
                "task delta conflicts semantically with authoritative main"
            )
        applied = self._run_with_input(path, ["apply", "--3way"], patch)
        if applied.returncode != 0:
            raise DeliveryReconciliationError(
                "task delta could not be applied cleanly to authoritative main"
            )
        checked_diff = self._run(path, ["diff", "--check"], timeout=30)
        if checked_diff.returncode != 0:
            raise DeliveryReconciliationError("recovery patch failed diff validation")

    def _names_at(self, path: Path, args: list[str]) -> tuple[str, ...]:
        result = self._run_control(path, args)
        if result.returncode != 0:
            raise DeliveryReconciliationError("cannot classify authoritative base drift")
        return tuple(item for item in result.stdout.splitlines() if item)

    def _run_control(
        self, path: Path, args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        return self._run_path(path, args, timeout=60)

    def _run_path(
        self, path: Path, args: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.git_executable, "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryReconciliationError(
                f"git reconciliation command failed: {type(exc).__name__}"
            ) from exc

    def _run_with_input(
        self, path: Path, args: list[str], value: str
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.git_executable, "-C", str(path), *args],
                input=value,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryReconciliationError(
                f"git patch operation failed: {type(exc).__name__}"
            ) from exc

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
        app_owner=task.app_owner,
        execution_worker=task.terminal_worker_id,
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
        required_capabilities=task.required_capabilities,
        authority_requirements=task.authority_requirements,
        verification_matrix_version=task.verification_matrix_version,
    )


def _pr_body(task: TaskRecord, commit_sha: str, verification: Mapping[str, Any]) -> str:
    return (
        f"Night Shift task_id: {task.task_id}\n"
        f"APP owner: {task.app_owner}\n"
        f"Execution worker: {task.terminal_worker_id}\n"
        f"Commit SHA: {commit_sha}\n"
        f"Base branch: {task.base_branch}\n"
        f"Base SHA: {task.base_sha}\n"
        f"Origin SHA: {task.origin_sha}\n\n"
        f"Verification: {json.dumps(dict(verification), sort_keys=True)}"
    )
