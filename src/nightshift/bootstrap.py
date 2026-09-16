"""Memory bootstrap seam; V1 does not embed Memory V4 retrieval logic."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import TaskRecord


class BootstrapProvider(Protocol):
    """Provide bounded, non-authoritative context to a worker adapter."""

    def bootstrap(self, task: TaskRecord) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class StaticBootstrapProvider:
    """Small V1 provider used for deterministic context and tests."""

    values: Mapping[str, Any] = field(default_factory=dict)

    def bootstrap(self, task: TaskRecord) -> Mapping[str, Any]:
        return {"task_id": task.task_id, **dict(self.values)}


class MemoryV4BootstrapProvider:
    """Future seam; Memory V4 remains an optional, separate integration."""

    def bootstrap(self, task: TaskRecord) -> Mapping[str, Any]:
        raise RuntimeError(
            "Memory V4 bootstrap integration is not merged or enabled in Dispatcher V1"
        )
