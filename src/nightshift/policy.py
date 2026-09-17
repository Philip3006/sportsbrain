"""Fail-closed safety policy for autonomous development tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import SafetyViolation
from .models import HARD_MAX_RUNTIME_SECONDS, RiskClass, TaskSpec
from .registry import BuilderDefinition

# These are intentionally checked at the queue boundary as well as documented
# in the operator runbook. The dispatcher never needs to execute shell text,
# but rejecting dangerous intent before persistence prevents unsafe adapters
# from being attached later by accident.
_FORBIDDEN_TEXT = (
    re.compile(
        r"\bgit\s+(?:merge|push|reset\s+--hard|checkout\s+--|rebase)\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:wrangler|npm|npx)\s+(?:deploy|publish)\b", re.IGNORECASE),
    re.compile(r"\b(?:rebase|reset\s+--hard)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:merge|push|deploy|publish)\s+(?:this|the|changes?|branch|pr|pull|to|into|production)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:deploy|production\s+release|force[- ]push)\b", re.IGNORECASE),
    re.compile(r"\b(?:rm|rmdir)\s+-rf\b", re.IGNORECASE),
    re.compile(
        r"\b(?:production|cloudflare)\s+(?:deploy|activation|release)\b", re.IGNORECASE
    ),
    re.compile(r"\bcontrolled\s+(?:activation|shadow\s+provider)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:provider(?:\s+monetary)?\s+spend|subscription|billing|place\s+(?:a\s+)?bet|betting)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:financial\s+ledger|sealed[- ]data|production\s+model|signal[- ]time)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:pr\s+)?merge\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class SafetyPolicy:
    """Limits that can be tightened without changing queue semantics."""

    max_pending_tasks: int = 1000
    max_attempts: int = 10
    max_dependencies: int = 32
    # 30 minutes is the safe generic default; reviewed heavy templates may
    # use 60 minutes, but no queue submission may exceed that policy ceiling.
    max_runtime_seconds: int = 60 * 60
    allow_external_side_effects: bool = False
    allow_destructive: bool = False
    forbidden_text: tuple[re.Pattern[str], ...] = _FORBIDDEN_TEXT

    def validate(self, task: TaskSpec, definition: BuilderDefinition) -> None:
        """Reject unsafe or out-of-scope task envelopes before enqueue."""

        if task.max_attempts > self.max_attempts:
            raise SafetyViolation(
                f"max_attempts exceeds policy limit {self.max_attempts}"
            )
        if task.max_runtime_seconds > self.max_runtime_seconds:
            raise SafetyViolation(
                f"max_runtime_seconds exceeds policy limit {self.max_runtime_seconds}"
            )
        if task.max_runtime_seconds > HARD_MAX_RUNTIME_SECONDS:
            raise SafetyViolation("max_runtime_seconds exceeds the hard safety maximum")
        if len(task.dependency_ids) > self.max_dependencies:
            raise SafetyViolation(
                f"dependency count exceeds policy limit {self.max_dependencies}"
            )
        reason = self.block_reason(task)
        if reason:
            raise SafetyViolation(reason)
        if task.risk_class is not RiskClass.READ_ONLY and not task.approval_required:
            raise SafetyViolation("non-read-only tasks require explicit approval")
        # Keep the definition argument part of the contract: this method is
        # only valid after explicit registry resolution and cannot be called
        # with a guessed builder shape.
        if not definition.enabled:
            raise SafetyViolation("builder is disabled")

    def validate_worker_id(self, worker_id: str, dispatcher_id: str) -> None:
        if worker_id == dispatcher_id or worker_id.startswith(f"{dispatcher_id}:"):
            raise SafetyViolation(
                "dispatcher identity cannot claim or execute worker tasks"
            )
        if not re.fullmatch(
            r"builder-[1-9][0-9]*(?::[A-Za-z0-9._-]{1,64})?", worker_id
        ):
            raise SafetyViolation(
                "worker identity is not a safe registered-Builder identifier"
            )

    def block_reason(self, task: TaskSpec) -> str | None:
        if (
            task.risk_class is RiskClass.EXTERNAL_SIDE_EFFECT
            and not self.allow_external_side_effects
        ):
            return "CEO_AUTHORIZATION_REQUIRED: external side effects are disabled in Night Shift V1"
        if task.risk_class is RiskClass.DESTRUCTIVE and not self.allow_destructive:
            return "PROHIBITED_AUTONOMOUS: destructive tasks are disabled in Night Shift V1"
        text = "\n".join(
            [task.objective, task.branch, task.task_type, _string_values(task.payload)]
        )
        for pattern in self.forbidden_text:
            if pattern.search(text):
                label = (
                    "PROHIBITED_AUTONOMOUS"
                    if "reset" in pattern.pattern
                    or "rebase" in pattern.pattern
                    or "force" in pattern.pattern
                    else "CEO_AUTHORIZATION_REQUIRED"
                )
                return f"{label}: forbidden autonomous operation"
        return None


def _string_values(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_string_values(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_string_values(item) for item in value)
    return str(value) if isinstance(value, str) else ""
