"""Read-only GitHub verification for explicit Night Shift delivery recovery."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from .errors import SafetyViolation


class DeliveryVerificationError(SafetyViolation, ValueError):
    """GitHub facts did not prove that preserved work is safe to reconcile."""


def verify_github_pull_request(
    repo: str,
    pr_number: int,
    *,
    expected_commit_sha: str,
    expected_base: str = "main",
    expected_branch: str | None = None,
    expected_remote_sha: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Verify PR head/base identity with read-only ``gh api``.

    A changed base *OID* is returned as evidence rather than rewritten.  The
    base repository and branch still must match the governed task so this
    command cannot silently retarget a PR.
    """

    if (
        not isinstance(repo, str)
        or not repo.strip()
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number <= 0
    ):
        raise DeliveryVerificationError("repo and a positive PR number are required")
    if not isinstance(expected_commit_sha, str) or not expected_commit_sha.strip():
        raise DeliveryVerificationError("expected commit SHA is required")
    try:
        completed = runner(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        raise DeliveryVerificationError("GitHub pull-request verification failed") from exc
    try:
        payload = json.loads(completed.stdout)
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise DeliveryVerificationError("gh returned invalid pull-request JSON") from exc
    if not isinstance(payload, Mapping):
        raise DeliveryVerificationError("gh pull-request response is not an object")
    head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
    base = payload.get("base") if isinstance(payload.get("base"), Mapping) else {}
    head_sha = head.get("sha")
    head_ref = head.get("ref")
    base_ref = base.get("ref")
    base_repo = base.get("repo") if isinstance(base.get("repo"), Mapping) else {}
    if head_sha != expected_commit_sha:
        raise DeliveryVerificationError(
            "GitHub PR head SHA does not match the preserved commit SHA"
        )
    if expected_remote_sha is not None and head_sha != expected_remote_sha:
        raise DeliveryVerificationError(
            "GitHub PR head SHA does not match the preserved remote SHA"
        )
    if expected_branch is not None and head_ref != expected_branch:
        raise DeliveryVerificationError("GitHub PR head branch does not match the task")
    if base_ref != expected_base or base_repo.get("full_name") != repo:
        raise DeliveryVerificationError("GitHub PR base is not the authoritative base")
    state = str(payload.get("state", "")).lower()
    if state not in {"open", "closed"}:
        raise DeliveryVerificationError("GitHub PR state is not recognizable")
    merged_at = payload.get("merged_at")
    return {
        "verified": True,
        "worker_execution_success": True,
        "implementation_success": True,
        "commit_sha": expected_commit_sha,
        "remote_sha": head_sha,
        "pr_number": pr_number,
        "pr_url": payload.get("html_url")
        if isinstance(payload.get("html_url"), str)
        else None,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "base_sha": base.get("sha") if isinstance(base.get("sha"), str) else None,
        "state": state,
        "merged": bool(merged_at),
        "verification_json": {
            "source": "github_pull_request_api",
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "head_ref": head_ref,
            "base_ref": base_ref,
            "base_sha": base.get("sha")
            if isinstance(base.get("sha"), str)
            else None,
            "state": state,
            "merged": bool(merged_at),
        },
    }


def facts_verifier(facts: Mapping[str, Any]) -> Callable[[Any], Mapping[str, Any]]:
    """Adapt deterministic test evidence to a recovery verifier callback."""

    if not isinstance(facts, Mapping):
        raise TypeError("facts must be a mapping")

    def verify(_: Any) -> Mapping[str, Any]:
        return facts

    return verify


__all__ = ["DeliveryVerificationError", "facts_verifier", "verify_github_pull_request"]
