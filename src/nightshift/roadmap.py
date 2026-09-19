"""Explicit, deterministic roadmap selection for the Night Shift queue."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, InvalidTaskError
from .registry import BuilderRegistry
from .templates import TemplateRegistry

_ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_MODES = {"bounded", "unlimited"}


@dataclass(frozen=True)
class RoadmapItem:
    """One explicitly reviewed future task; no fields are inferred."""

    item_id: str
    title: str
    builder_id: str
    template_id: str
    payload: Mapping[str, Any]
    dependency_item_ids: tuple[str, ...] = ()
    priority: int = 0
    debug_budget: int = 0
    repeated_failure_limit: int = 2
    mode: str = "bounded"
    enabled: bool = True
    generation: int = 1

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RoadmapItem:
        required = ("item_id", "title", "builder_id", "template_id", "payload")
        missing = [name for name in required if name not in raw]
        if missing:
            raise ConfigurationError(
                f"roadmap item missing fields: {', '.join(missing)}"
            )
        item_id = raw["item_id"]
        if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
            raise ConfigurationError("roadmap item_id is invalid")
        text_fields = (raw["title"], raw["builder_id"], raw["template_id"])
        if any(
            not isinstance(value, str) or not value.strip() for value in text_fields
        ):
            raise ConfigurationError(
                f"{item_id}: title, builder_id, and template_id are required"
            )
        payload = raw["payload"]
        if not isinstance(payload, Mapping):
            raise ConfigurationError(f"{item_id}: payload must be an object")
        dependencies = raw.get("dependency_item_ids", ())
        if not isinstance(dependencies, (list, tuple)) or any(
            not isinstance(value, str) or not _ITEM_ID_RE.fullmatch(value)
            for value in dependencies
        ):
            raise ConfigurationError(f"{item_id}: dependency_item_ids is invalid")
        if len(set(dependencies)) != len(dependencies) or item_id in dependencies:
            raise ConfigurationError(
                f"{item_id}: dependency_item_ids must be unique and acyclic"
            )
        priority = raw.get("priority", 0)
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not -100 <= priority <= 100
        ):
            raise ConfigurationError(f"{item_id}: priority is invalid")
        debug_budget = raw.get("debug_budget", 0)
        if (
            isinstance(debug_budget, bool)
            or not isinstance(debug_budget, int)
            or not 0 <= debug_budget <= 10
        ):
            raise ConfigurationError(f"{item_id}: debug_budget is invalid")
        repeat_limit = raw.get("repeated_failure_limit", 2)
        if (
            isinstance(repeat_limit, bool)
            or not isinstance(repeat_limit, int)
            or not 1 <= repeat_limit <= 10
        ):
            raise ConfigurationError(f"{item_id}: repeated_failure_limit is invalid")
        mode = raw.get("mode", "bounded")
        if mode not in _MODES:
            raise ConfigurationError(f"{item_id}: mode must be bounded or unlimited")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"{item_id}: enabled must be boolean")
        generation = raw.get("generation", 1)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 1 <= generation <= 10000
        ):
            raise ConfigurationError(
                f"{item_id}: generation must be between 1 and 10000"
            )
        return cls(
            item_id=item_id,
            title=raw["title"].strip(),
            builder_id=raw["builder_id"].strip(),
            template_id=raw["template_id"].strip(),
            payload=dict(payload),
            dependency_item_ids=tuple(dependencies),
            priority=priority,
            debug_budget=debug_budget,
            repeated_failure_limit=repeat_limit,
            mode=mode,
            enabled=enabled,
            generation=generation,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "builder_id": self.builder_id,
            "template_id": self.template_id,
            "payload": dict(self.payload),
            "dependency_item_ids": list(self.dependency_item_ids),
            "priority": self.priority,
            "debug_budget": self.debug_budget,
            "repeated_failure_limit": self.repeated_failure_limit,
            "mode": self.mode,
            "enabled": self.enabled,
            "generation": self.generation,
        }

    @property
    def idempotency_key(self) -> str:
        """Return the stable identity for this explicit roadmap generation."""

        # Keep the V1 spelling for generation one so existing static roadmap
        # rows and operator scripts remain compatible. Later generations are
        # explicit in the identity and cannot collide with an earlier stage.
        if self.generation == 1:
            return f"roadmap:{self.item_id}"
        return f"roadmap:{self.item_id}:generation:{self.generation}"


class RoadmapRegistry:
    """Read-only registry loaded from a governed roadmap file."""

    def __init__(
        self,
        items: tuple[RoadmapItem, ...],
        *,
        mode: str = "bounded",
        max_cycles: int = 100,
        merge_backpressure_limit: int = 3,
    ) -> None:
        if mode not in _MODES:
            raise ConfigurationError("roadmap mode must be bounded or unlimited")
        if not 1 <= max_cycles <= 10000:
            raise ConfigurationError("roadmap max_cycles must be between 1 and 10000")
        if not 1 <= merge_backpressure_limit <= 100:
            raise ConfigurationError(
                "merge_backpressure_limit must be between 1 and 100"
            )
        ids = [item.item_id for item in items]
        if len(ids) != len(set(ids)):
            raise ConfigurationError("roadmap item_id values must be unique")
        known = set(ids)
        for item in items:
            if any(dependency not in known for dependency in item.dependency_item_ids):
                raise ConfigurationError(
                    f"{item.item_id}: dependency item is not registered"
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise ConfigurationError("roadmap dependency graph contains a cycle")
            if item_id in visited:
                return
            visiting.add(item_id)
            current = next(item for item in items if item.item_id == item_id)
            for dependency in current.dependency_item_ids:
                visit(dependency)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in ids:
            visit(item_id)
        self.items = items
        self.mode = mode
        self.max_cycles = max_cycles
        self.merge_backpressure_limit = merge_backpressure_limit

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RoadmapRegistry:
        if not isinstance(raw, Mapping) or raw.get("version", 1) != 1:
            raise ConfigurationError("unsupported roadmap registry")
        entries = raw.get("items")
        if not isinstance(entries, list) or any(
            not isinstance(item, Mapping) for item in entries
        ):
            raise ConfigurationError("roadmap items must be an array of objects")
        return cls(
            tuple(RoadmapItem.from_mapping(item) for item in entries),
            mode=raw.get("mode", "bounded"),
            max_cycles=raw.get("max_cycles", 100),
            merge_backpressure_limit=raw.get("merge_backpressure_limit", 3),
        )

    @classmethod
    def from_file(cls, path: Path) -> RoadmapRegistry:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(f"cannot read roadmap registry: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"invalid roadmap registry JSON: {path}") from exc
        return cls.from_mapping(raw)

    def validate_against(
        self, builders: BuilderRegistry, templates: TemplateRegistry
    ) -> None:
        for item in self.items:
            builder = builders.assert_worker_target(item.builder_id)
            template = templates.resolve(item.template_id)
            if template.builder_id != builder.builder_id:
                raise InvalidTaskError(
                    f"{item.item_id}: roadmap template does not match its explicit builder"
                )

    def by_id(self, item_id: str) -> RoadmapItem:
        for item in self.items:
            if item.item_id == item_id:
                return item
        raise KeyError(item_id)
