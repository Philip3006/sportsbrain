"""Deterministic classification contracts for delivery base reconciliation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import DeliveryError


class DeliveryBaseDrift(DeliveryError):
    """The task was built on an older authoritative base."""

    def __init__(
        self,
        message: str,
        *,
        original_base_sha: str | None = None,
        authoritative_base_sha: str | None = None,
        classification: str | None = None,
    ) -> None:
        super().__init__(message)
        self.original_base_sha = original_base_sha
        self.authoritative_base_sha = authoritative_base_sha
        self.classification = classification


class DeliveryReconciliationError(DeliveryError):
    """A bounded reconciliation attempt could not be completed safely."""


@dataclass(frozen=True)
class DriftClassification:
    """File-level evidence used before attempting a three-way patch."""

    classification: str
    upstream_paths: tuple[str, ...]
    task_paths: tuple[str, ...]
    overlapping_paths: tuple[str, ...]
    recoverable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "upstream_paths": list(self.upstream_paths),
            "task_paths": list(self.task_paths),
            "overlapping_paths": list(self.overlapping_paths),
            "recoverable": self.recoverable,
        }


def classify_drift(
    upstream_paths: Iterable[str], task_paths: Iterable[str]
) -> DriftClassification:
    """Classify path overlap without pretending overlap alone is a conflict.

    The actual three-way apply remains the authority for semantic safety.  An
    overlap is therefore ``OVERLAP_REQUIRES_THREE_WAY_VALIDATION`` rather than
    an automatic block.
    """

    upstream = tuple(sorted(set(upstream_paths)))
    task = tuple(sorted(set(task_paths)))
    overlap = tuple(sorted(set(upstream) & set(task)))
    if not overlap:
        classification = (
            "RUNTIME_HEALTH_DRIFT"
            if upstream
            and all(
                path.startswith(
                    ("data/cache/", "docs/data/", "results/health/", "results/")
                )
                for path in upstream
            )
            else "UNRELATED_UPSTREAM_DRIFT"
            if upstream
            else "NO_UPSTREAM_DRIFT"
        )
    else:
        classification = "OVERLAP_REQUIRES_THREE_WAY_VALIDATION"
    return DriftClassification(
        classification=classification,
        upstream_paths=upstream,
        task_paths=task,
        overlapping_paths=overlap,
        recoverable=True,
    )


__all__ = [
    "DeliveryBaseDrift",
    "DeliveryReconciliationError",
    "DriftClassification",
    "classify_drift",
]
