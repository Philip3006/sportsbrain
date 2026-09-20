"""Soft PR-backpressure classification and governed overlap helpers."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatch

from .models import RiskClass, TaskRecord, TaskState

PR_CATEGORIES = (
    "ACTIVE_SUBSTANTIVE",
    "RECOVERY",
    "SUPERSEDED",
    "STALE",
    "READ_ONLY_DOCS",
    "WAITING_REVIEW",
    "CONFLICTED",
)
SUBSTANTIVE_PR_CATEGORIES = frozenset(
    {"ACTIVE_SUBSTANTIVE", "WAITING_REVIEW", "CONFLICTED"}
)
ACTIVE_TASK_STATES = frozenset(
    {
        TaskState.CLAIMED,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.DELIVERY_RECONCILING,
    }
)


@dataclass(frozen=True)
class PullRequestClassification:
    """Operator-safe classification for one persisted PR-bearing task."""

    category: str
    counts_as_substantive: bool
    reason: str


def classify_pull_request(record: TaskRecord) -> PullRequestClassification | None:
    """Classify persisted PR evidence without making a network call.

    The dispatcher deliberately classifies from durable task evidence. A live
    GitHub refresh remains a separate read-only reconciliation concern and is
    never required just to decide whether an unrelated roadmap item can run.
    """

    if record.pr_number is None or record.state in {
        TaskState.COMPLETED,
        TaskState.CANCELLED,
    }:
        return None
    failure = (record.failure_class or "").upper()
    error = (record.last_error or "").upper()
    branch = record.branch.lower()
    evidence = record.reconciliation or {}
    if evidence or branch.startswith("nightshift/recovery/"):
        return PullRequestClassification(
            "RECOVERY", False, "bounded delivery-recovery PR"
        )
    if "SUPERSEDED" in failure or "SUPERSEDED" in error:
        return PullRequestClassification("SUPERSEDED", False, "superseded PR")
    if "STALE" in failure or "STALE" in error:
        return PullRequestClassification("STALE", False, "stale PR")
    if (
        record.risk_class is RiskClass.READ_ONLY
        or "DOC" in record.task_type.upper()
        or "DOCUMENT" in record.task_type.upper()
    ):
        return PullRequestClassification(
            "READ_ONLY_DOCS", False, "read-only or documentation PR"
        )
    if record.state is TaskState.CEO_REVIEW or "REVIEW" in failure:
        return PullRequestClassification(
            "WAITING_REVIEW", True, "awaiting review or CEO gate"
        )
    if "CONFLICT" in failure or "CONFLICT" in error:
        return PullRequestClassification(
            "CONFLICTED", True, "unresolved semantic conflict"
        )
    return PullRequestClassification(
        "ACTIVE_SUBSTANTIVE", True, "active substantive PR"
    )


def summarize_pull_requests(
    records: Iterable[TaskRecord],
) -> dict[str, int]:
    """Return stable category counts for status, doctor, and selection."""

    counts = Counter({category: 0 for category in PR_CATEGORIES})
    for record in records:
        classification = classify_pull_request(record)
        if classification is not None:
            counts[classification.category] += 1
    result = {category: int(counts[category]) for category in PR_CATEGORIES}
    result["open_pr_total"] = sum(result.values())
    result["active_substantive_pr_count"] = sum(
        result[category] for category in SUBSTANTIVE_PR_CATEGORIES
    )
    return result


def paths_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    """Return whether two governed path scopes can touch the same file."""

    left_values = tuple(_normalize_path(value) for value in left if value)
    right_values = tuple(_normalize_path(value) for value in right if value)
    for first in left_values:
        for second in right_values:
            if _path_matches(first, second) or _path_matches(second, first):
                return True
    return False


def record_changed_paths(record: TaskRecord) -> tuple[str, ...]:
    """Use explicit task scope or persisted delivery paths for overlap checks."""

    if record.allowed_paths:
        return record.allowed_paths
    for source in (record.delivery, record.verification, record.result):
        if isinstance(source, Mapping):
            values = source.get("changed_paths") or source.get("paths")
            if isinstance(values, (list, tuple)) and all(
                isinstance(value, str) for value in values
            ):
                return tuple(values)
    return ()


def _normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/").lstrip("./")
    return normalized.rstrip("/") or "."


def _path_matches(candidate: str, scope: str) -> bool:
    if scope in {".", "**", "*"}:
        return True
    if any(character in scope for character in "*?["):
        return fnmatch(candidate, scope) or fnmatch(candidate, f"{scope}/**")
    return candidate == scope or candidate.startswith(f"{scope}/")


__all__ = [
    "ACTIVE_TASK_STATES",
    "PR_CATEGORIES",
    "SUBSTANTIVE_PR_CATEGORIES",
    "PullRequestClassification",
    "classify_pull_request",
    "paths_overlap",
    "record_changed_paths",
    "summarize_pull_requests",
]
