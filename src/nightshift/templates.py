"""Explicit task templates for the initial four worker Builders."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

from .errors import ConfigurationError, InvalidTaskError
from .models import (
    DEFAULT_RUNTIME_SECONDS,
    HARD_MAX_RUNTIME_SECONDS,
    RiskClass,
    TaskSpec,
)
from .registry import BuilderRegistry
from .task_validation import safe_commands, safe_labels


@dataclass(frozen=True)
class TaskTemplate:
    """A reviewed, builder-scoped task shape."""

    template_id: str
    builder_id: str
    task_type: str
    objective_template: str
    required_payload_keys: tuple[str, ...] = ()
    allowed_payload_keys: tuple[str, ...] = ()
    risk_class: RiskClass = RiskClass.CODE_CHANGE
    default_priority: int = 0
    default_max_attempts: int = 3
    required_tests: tuple[str, ...] = ()
    verification_commands: tuple[tuple[str, ...], ...] = ()
    max_runtime_seconds: int = DEFAULT_RUNTIME_SECONDS
    requires_pr: bool | None = None
    app_owner: str | None = None
    required_capabilities: tuple[str, ...] = ()
    authority_requirements: tuple[str, ...] = ()
    verification_matrix_version: str = "nightshift-verification-v1"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TaskTemplate:
        required = ("template_id", "builder_id", "task_type", "objective_template")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ConfigurationError(
                f"template entry missing fields: {', '.join(missing)}"
            )
        values = {key: raw[key] for key in required}
        if any(
            not isinstance(value, str) or not value.strip() for value in values.values()
        ):
            raise ConfigurationError(
                "template identifiers and objective_template must be non-empty strings"
            )
        required_keys = _keys(
            raw.get("required_payload_keys", ()), "required_payload_keys"
        )
        allowed_keys = _keys(
            raw.get("allowed_payload_keys", required_keys), "allowed_payload_keys"
        )
        if not set(required_keys).issubset(allowed_keys):
            raise ConfigurationError(
                f"{raw['template_id']}: required payload keys must be allowed"
            )
        try:
            risk = RiskClass(raw.get("risk_class", RiskClass.CODE_CHANGE.value))
        except ValueError as exc:
            raise ConfigurationError(
                f"{raw['template_id']}: risk_class is invalid"
            ) from exc
        priority = raw.get("default_priority", 0)
        attempts = raw.get("default_max_attempts", 3)
        required_tests = safe_labels(raw.get("required_tests", ()), "required_tests")
        verification_commands = safe_commands(raw.get("verification_commands", ()))
        # ``runtime_seconds`` was used by the short-lived recovery prototype;
        # accept it only as a migration spelling, while persisting the
        # canonical max_runtime_seconds field.
        max_runtime_seconds = raw.get(
            "max_runtime_seconds", raw.get("runtime_seconds", DEFAULT_RUNTIME_SECONDS)
        )
        requires_pr = raw.get("requires_pr")
        app_owner = raw.get("app_owner")
        if app_owner is not None and (
            not isinstance(app_owner, str) or app_owner not in {"APP_B1", "APP_B2", "APP_B3", "APP_B4", "APP_B5"}
        ):
            raise ConfigurationError(f"{raw['template_id']}: app_owner is invalid")
        required_capabilities = _keys(
            raw.get("required_capabilities", ()), "required_capabilities"
        )
        authority_requirements = _keys(
            raw.get("authority_requirements", ()), "authority_requirements"
        )
        verification_matrix_version = raw.get(
            "verification_matrix_version", "nightshift-verification-v1"
        )
        if (
            not isinstance(verification_matrix_version, str)
            or not verification_matrix_version.strip()
        ):
            raise ConfigurationError(
                f"{raw['template_id']}: verification_matrix_version is invalid"
            )
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not -100 <= priority <= 100
        ):
            raise ConfigurationError(
                f"{raw['template_id']}: default_priority is invalid"
            )
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 1 <= attempts <= 10
        ):
            raise ConfigurationError(
                f"{raw['template_id']}: default_max_attempts is invalid"
            )
        if (
            isinstance(max_runtime_seconds, bool)
            or not isinstance(max_runtime_seconds, int)
            or not 1 <= max_runtime_seconds <= HARD_MAX_RUNTIME_SECONDS
        ):
            raise ConfigurationError(
                f"{raw['template_id']}: max_runtime_seconds is invalid"
            )
        if requires_pr is not None and not isinstance(requires_pr, bool):
            raise ConfigurationError(f"{raw['template_id']}: requires_pr is invalid")
        fields = {
            name
            for _, name, _, _ in Formatter().parse(values["objective_template"])
            if name
        }
        if not fields.issubset(set(required_keys) | set(allowed_keys)):
            unknown = sorted(fields - set(required_keys) - set(allowed_keys))
            raise ConfigurationError(
                f"{raw['template_id']}: objective has unknown fields {unknown}"
            )
        return cls(
            template_id=values["template_id"],
            builder_id=values["builder_id"],
            task_type=values["task_type"],
            objective_template=values["objective_template"],
            required_payload_keys=required_keys,
            allowed_payload_keys=allowed_keys,
            risk_class=risk,
            default_priority=priority,
            default_max_attempts=attempts,
            required_tests=required_tests,
            verification_commands=verification_commands,
            max_runtime_seconds=max_runtime_seconds,
            requires_pr=requires_pr,
            app_owner=app_owner,
            required_capabilities=required_capabilities,
            authority_requirements=authority_requirements,
            verification_matrix_version=verification_matrix_version,
        )

    @property
    def runtime_seconds(self) -> int:
        """Compatibility name for the task runtime shown to operators."""

        return self.max_runtime_seconds

    def instantiate(
        self,
        *,
        registry: BuilderRegistry,
        branch: str,
        payload: Mapping[str, Any],
        requested_by: str = "operator",
        idempotency_key: str | None = None,
        priority: int | None = None,
        max_attempts: int | None = None,
        dependency_ids: tuple[str, ...] = (),
        parent_task_id: str | None = None,
        allowed_paths: tuple[str, ...] = (),
        prohibited_paths: tuple[str, ...] = (),
        resource_locks: tuple[str, ...] = (),
        expected_base_sha: str | None = None,
        base_branch: str = "main",
        required_tests: tuple[str, ...] | None = None,
        verification_commands: tuple[tuple[str, ...], ...] | None = None,
        max_runtime_seconds: int | None = None,
        requires_pr: bool | None = None,
        roadmap_item_id: str | None = None,
        debug_budget: int = 0,
        repeated_failure_limit: int = 2,
        execution_worker: str | None = None,
        app_owner: str | None = None,
        required_capabilities: tuple[str, ...] | None = None,
        authority_requirements: tuple[str, ...] | None = None,
        verification_matrix_version: str | None = None,
    ) -> TaskSpec:
        data = dict(payload)
        missing = sorted(set(self.required_payload_keys) - set(data))
        unknown = sorted(set(data) - set(self.allowed_payload_keys))
        if missing:
            raise InvalidTaskError(
                f"{self.template_id}: missing payload keys {missing}"
            )
        if unknown:
            raise InvalidTaskError(
                f"{self.template_id}: unknown payload keys {unknown}"
            )
        try:
            objective = self.objective_template.format(**data)
        except (KeyError, ValueError) as exc:
            raise InvalidTaskError(
                f"{self.template_id}: cannot render objective"
            ) from exc
        # Resolve explicitly here so a template cannot silently target an
        # unregistered or disabled worker. The dispatcher validates again.
        selected_worker = execution_worker or self.builder_id
        registry.assert_worker_target(selected_worker)
        selected_app_owner = app_owner or self.app_owner or _app_owner_for_worker(self.builder_id)
        return TaskSpec(
            builder_id=selected_worker,
            objective=objective,
            branch=branch,
            task_type=self.task_type,
            template_id=self.template_id,
            payload=data,
            risk_class=self.risk_class,
            priority=self.default_priority if priority is None else priority,
            max_attempts=self.default_max_attempts
            if max_attempts is None
            else max_attempts,
            idempotency_key=idempotency_key,
            parent_task_id=parent_task_id,
            dependency_ids=dependency_ids,
            allowed_paths=allowed_paths,
            prohibited_paths=prohibited_paths,
            resource_locks=resource_locks,
            expected_base_sha=expected_base_sha,
            base_branch=base_branch,
            required_tests=self.required_tests
            if required_tests is None
            else required_tests,
            verification_commands=self.verification_commands
            if verification_commands is None
            else verification_commands,
            max_runtime_seconds=self.max_runtime_seconds
            if max_runtime_seconds is None
            else max_runtime_seconds,
            requires_pr=self.requires_pr if requires_pr is None else requires_pr,
            roadmap_item_id=roadmap_item_id,
            debug_budget=debug_budget,
            repeated_failure_limit=repeated_failure_limit,
            requested_by=requested_by,
            app_owner=selected_app_owner,
            execution_worker=selected_worker,
            required_capabilities=(
                self.required_capabilities
                if required_capabilities is None
                else required_capabilities
            ),
            authority_requirements=(
                self.authority_requirements
                if authority_requirements is None
                else authority_requirements
            ),
            verification_matrix_version=(
                self.verification_matrix_version
                if verification_matrix_version is None
                else verification_matrix_version
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "builder_id": self.builder_id,
            "task_type": self.task_type,
            "objective_template": self.objective_template,
            "required_payload_keys": list(self.required_payload_keys),
            "allowed_payload_keys": list(self.allowed_payload_keys),
            "risk_class": self.risk_class.value,
            "default_priority": self.default_priority,
            "default_max_attempts": self.default_max_attempts,
            "required_tests": list(self.required_tests),
            "verification_commands": [
                list(command) for command in self.verification_commands
            ],
            "max_runtime_seconds": self.max_runtime_seconds,
            "requires_pr": self.requires_pr,
            "app_owner": self.app_owner,
            "required_capabilities": list(self.required_capabilities),
            "authority_requirements": list(self.authority_requirements),
            "verification_matrix_version": self.verification_matrix_version,
        }


def _keys(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigurationError(f"{field_name} must be an array")
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ConfigurationError(f"{field_name} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ConfigurationError(f"{field_name} must not contain duplicates")
    return result


def _app_owner_for_worker(worker_id: str) -> str:
    """Legacy template fallback; explicit templates should set app_owner."""

    match = re.fullmatch(r"builder-([1-5])", worker_id)
    return f"APP_B{match.group(1)}" if match else "APP_B5"


class TemplateRegistry:
    """Read-only lookup of reviewed task templates; no templates are inferred."""

    def __init__(
        self, templates: tuple[TaskTemplate, ...], *, version: int = 1
    ) -> None:
        if not templates:
            raise ConfigurationError("the task template registry cannot be empty")
        ids = [template.template_id for template in templates]
        if len(set(ids)) != len(ids):
            raise ConfigurationError("template_id values must be unique")
        self._templates = {template.template_id: template for template in templates}
        self.version = version

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TemplateRegistry:
        if not isinstance(raw, Mapping) or raw.get("version", 1) != 1:
            raise ConfigurationError("unsupported task template registry")
        entries = raw.get("templates")
        if not isinstance(entries, list):
            raise ConfigurationError("templates must be an array")
        if any(not isinstance(item, Mapping) for item in entries):
            raise ConfigurationError("template entries must be objects")
        return cls(
            tuple(TaskTemplate.from_mapping(item) for item in entries), version=1
        )

    @classmethod
    def from_file(cls, path: Path) -> TemplateRegistry:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(f"cannot read task templates: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"invalid task templates JSON: {path}") from exc
        return cls.from_mapping(raw)

    @property
    def templates(self) -> tuple[TaskTemplate, ...]:
        return tuple(self._templates.values())

    def resolve(self, template_id: str) -> TaskTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise ConfigurationError(
                f"template {template_id!r} is not explicitly registered"
            ) from exc

    def validate_against(self, registry: BuilderRegistry) -> None:
        for template in self.templates:
            definition = registry.assert_worker_target(template.builder_id)
            if template.task_type not in definition.task_types:
                raise ConfigurationError(
                    f"{template.template_id}: task_type is not allowed for its builder"
                )
            if template.risk_class not in definition.allowed_risk_classes:
                raise ConfigurationError(
                    f"{template.template_id}: risk class is not allowed for its builder"
                )
