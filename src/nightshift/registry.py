"""Explicit, governed worker registry for Night Shift.

There is intentionally no package scanning, naming convention discovery, or
fallback worker. Adding a future builder means adding and reviewing a
registry entry; the queue and state machine do not need to change.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, DispatcherRecursionError, UnknownBuilderError
from .models import APP_OWNERS, DISPATCHER_ID, RiskClass, TaskSpec

_BUILDER_ID_RE = re.compile(r"^(?:builder-[1-4]|terminal-5)$")
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _owner_for_worker(worker_id: str) -> str:
    match = re.fullmatch(r"builder-([1-4])", worker_id)
    return f"APP_B{match.group(1)}" if match else "APP_B5"


@dataclass(frozen=True)
class BuilderDefinition:
    """A single explicitly authorized worker builder."""

    builder_id: str
    display_name: str
    role: str
    repo_allowlist: tuple[str, ...]
    branch_prefix: str
    task_types: tuple[str, ...]
    allowed_risk_classes: tuple[RiskClass, ...]
    base_branch_allowlist: tuple[str, ...] = ("main",)
    max_concurrency: int = 1
    enabled: bool = True
    aliases: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    delegated_task_types: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BuilderDefinition:
        required = (
            "builder_id",
            "display_name",
            "role",
            "repo_allowlist",
            "branch_prefix",
            "task_types",
        )
        missing = [key for key in required if key not in raw]
        if missing:
            raise ConfigurationError(
                f"builder entry missing fields: {', '.join(missing)}"
            )
        builder_id = raw["builder_id"]
        if not isinstance(builder_id, str) or not _BUILDER_ID_RE.fullmatch(builder_id):
            raise ConfigurationError("builder_id must match builder-N")
        if builder_id == DISPATCHER_ID:
            raise DispatcherRecursionError(
                "Builder 5 is the dispatcher and cannot be registered as a worker"
            )
        if not isinstance(raw["display_name"], str) or not raw["display_name"].strip():
            raise ConfigurationError(f"{builder_id}: display_name is required")
        if not isinstance(raw["role"], str) or not raw["role"].strip():
            raise ConfigurationError(f"{builder_id}: role is required")
        repos = _string_tuple(
            raw["repo_allowlist"], f"{builder_id}.repo_allowlist", non_empty=True
        )
        branch_prefix = raw["branch_prefix"]
        if (
            not isinstance(branch_prefix, str)
            or not branch_prefix.strip()
            or branch_prefix.startswith("/")
        ):
            raise ConfigurationError(
                f"{builder_id}: branch_prefix must be a relative non-empty prefix"
            )
        task_types = _string_tuple(
            raw["task_types"], f"{builder_id}.task_types", non_empty=True
        )
        base_branches = _string_tuple(
            raw.get("base_branch_allowlist", ("main",)),
            f"{builder_id}.base_branch_allowlist",
            non_empty=True,
        )
        raw_risks = raw.get(
            "allowed_risk_classes",
            [RiskClass.READ_ONLY.value, RiskClass.CODE_CHANGE.value],
        )
        try:
            risks = tuple(RiskClass(value) for value in raw_risks)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{builder_id}: allowed_risk_classes contains an unknown value"
            ) from exc
        if not risks:
            raise ConfigurationError(
                f"{builder_id}: at least one risk class is required"
            )
        max_concurrency = raw.get("max_concurrency", 1)
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 8
        ):
            raise ConfigurationError(
                f"{builder_id}: max_concurrency must be between 1 and 8"
            )
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"{builder_id}: enabled must be boolean")
        aliases = _string_tuple(raw.get("aliases", ()), f"{builder_id}.aliases")
        if any(not _ALIAS_RE.fullmatch(alias) for alias in aliases):
            raise ConfigurationError(
                f"{builder_id}: aliases must be lowercase safe identifiers"
            )
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ConfigurationError(f"{builder_id}: metadata must be an object")
        capabilities = _string_tuple(
            raw.get("capabilities", ()), f"{builder_id}.capabilities"
        )
        delegated_task_types = _string_tuple(
            raw.get("delegated_task_types", ()), f"{builder_id}.delegated_task_types"
        )
        return cls(
            builder_id=builder_id,
            display_name=raw["display_name"].strip(),
            role=raw["role"].strip(),
            repo_allowlist=repos,
            branch_prefix=branch_prefix,
            task_types=task_types,
            base_branch_allowlist=base_branches,
            allowed_risk_classes=risks,
            max_concurrency=max_concurrency,
            enabled=enabled,
            aliases=aliases,
            capabilities=capabilities,
            delegated_task_types=delegated_task_types,
            metadata=dict(metadata),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "builder_id": self.builder_id,
            "display_name": self.display_name,
            "role": self.role,
            "repo_allowlist": list(self.repo_allowlist),
            "branch_prefix": self.branch_prefix,
            "task_types": list(self.task_types),
            "base_branch_allowlist": list(self.base_branch_allowlist),
            "allowed_risk_classes": [risk.value for risk in self.allowed_risk_classes],
            "max_concurrency": self.max_concurrency,
            "enabled": self.enabled,
            "aliases": list(self.aliases),
            "capabilities": list(self.capabilities),
            "delegated_task_types": list(self.delegated_task_types),
            "metadata": dict(self.metadata),
        }


def _string_tuple(
    value: Any, field_name: str, *, non_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigurationError(f"{field_name} must be an array")
    values = tuple(value)
    if any(
        not isinstance(item, str) or (non_empty and not item.strip()) for item in values
    ):
        raise ConfigurationError(f"{field_name} must contain strings")
    if len(set(values)) != len(values):
        raise ConfigurationError(f"{field_name} must not contain duplicates")
    if non_empty and not values:
        raise ConfigurationError(f"{field_name} must not be empty")
    return values


class BuilderRegistry:
    """Read-only registry with explicit lookup and dispatch authorization."""

    def __init__(
        self, builders: tuple[BuilderDefinition, ...], *, version: int = 1
    ) -> None:
        if not builders:
            raise ConfigurationError("the governed worker registry cannot be empty")
        if len({item.builder_id for item in builders}) != len(builders):
            raise ConfigurationError("worker builder_id values must be unique")
        if any(item.builder_id == DISPATCHER_ID for item in builders):
            raise DispatcherRecursionError("Builder 5 cannot be a worker")
        aliases = [alias for builder in builders for alias in builder.aliases]
        if len(set(aliases)) != len(aliases):
            raise ConfigurationError("worker aliases must be globally unique")
        self._builders = {item.builder_id: item for item in builders}
        self._aliases = {
            alias: item.builder_id for item in builders for alias in item.aliases
        }
        self.version = version

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> BuilderRegistry:
        if not isinstance(raw, Mapping):
            raise ConfigurationError("worker registry must be an object")
        version = raw.get("version", 1)
        if version != 1:
            raise ConfigurationError("unsupported worker registry version")
        dispatcher = raw.get("dispatcher")
        if (
            not isinstance(dispatcher, Mapping)
            or dispatcher.get("builder_id") != DISPATCHER_ID
        ):
            raise ConfigurationError(
                "registry must explicitly identify Builder 5 as dispatcher"
            )
        if dispatcher.get("can_be_worker", True) is not False:
            raise DispatcherRecursionError("registry must mark Builder 5 as non-worker")
        entries = raw.get("builders")
        if not isinstance(entries, list):
            raise ConfigurationError("registry builders must be an array")
        if any(not isinstance(item, Mapping) for item in entries):
            raise ConfigurationError("registry builder entries must be objects")
        return cls(
            tuple(BuilderDefinition.from_mapping(item) for item in entries),
            version=version,
        )

    @classmethod
    def from_file(cls, path: Path) -> BuilderRegistry:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(f"cannot read worker registry: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"invalid worker registry JSON: {path}") from exc
        return cls.from_mapping(raw)

    @property
    def builders(self) -> tuple[BuilderDefinition, ...]:
        return tuple(self._builders.values())

    @property
    def builder_ids(self) -> tuple[str, ...]:
        return tuple(self._builders)

    def resolve(self, builder_id: str) -> BuilderDefinition:
        if not isinstance(builder_id, str):
            raise UnknownBuilderError("builder_id must be an explicit string")
        canonical = self._aliases.get(builder_id, builder_id)
        try:
            definition = self._builders[canonical]
        except KeyError as exc:
            raise UnknownBuilderError(
                f"builder {builder_id!r} is not in the governed registry"
            ) from exc
        if not definition.enabled:
            raise UnknownBuilderError(f"builder {canonical} is registered but disabled")
        return definition

    def assert_worker_target(self, builder_id: str) -> BuilderDefinition:
        if builder_id in {DISPATCHER_ID, "builder-5", "b5"}:
            raise DispatcherRecursionError(
                "Builder 5 is the dispatcher and cannot be dispatched as a worker"
            )
        return self.resolve(builder_id)

    def validate_task(
        self, task: TaskSpec, *, enforce_risk: bool = True
    ) -> BuilderDefinition:
        worker_id = task.execution_worker or task.builder_id
        definition = self.assert_worker_target(worker_id)
        if task.repo not in definition.repo_allowlist:
            raise ConfigurationError(
                f"{worker_id}: repo is not in its explicit allowlist"
            )
        if not task.branch.startswith(definition.branch_prefix):
            raise ConfigurationError(
                f"{worker_id}: branch must start with {definition.branch_prefix!r}"
            )
        if task.base_branch not in definition.base_branch_allowlist:
            raise ConfigurationError(
                f"{task.builder_id}: base branch is not in its explicit allowlist"
            )
        if not task.task_type or (
            task.task_type not in definition.task_types
            and task.task_type not in definition.delegated_task_types
        ):
            raise ConfigurationError(
                f"{worker_id}: task_type is not explicitly authorized"
            )
        if task.app_owner not in APP_OWNERS:
            raise ConfigurationError(f"{worker_id}: app_owner is not authorized")
        missing_capabilities = sorted(
            set(task.required_capabilities) - set(definition.capabilities)
        )
        if missing_capabilities:
            raise ConfigurationError(
                f"{worker_id}: missing required capabilities {missing_capabilities}"
            )
        if (
            task.app_owner != _owner_for_worker(worker_id)
            and not task.required_capabilities
        ):
            raise ConfigurationError(
                f"{worker_id}: cross-APP execution requires a capability contract"
            )
        if enforce_risk and task.risk_class not in definition.allowed_risk_classes:
            raise ConfigurationError(f"{worker_id}: risk class is not authorized")
        return definition
