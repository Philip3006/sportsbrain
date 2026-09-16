"""Validation helpers for persisted Night Shift task envelopes."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .errors import InvalidTaskError

_SHELL_NAMES = {"ash", "bash", "cmd", "fish", "powershell", "pwsh", "sh", "zsh"}
_SHELL_FLAGS = {"-c", "--command", "-e", "--eval", "-exec"}
_MUTATING_GIT_ACTIONS = {"clean", "checkout", "merge", "push", "rebase", "reset"}
_MUTATING_EXECUTABLES = {"gh", "npm", "npx", "rm", "rmdir", "wrangler"}


def safe_relative_paths(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidTaskError(f"{field_name} must be an array")
    paths = tuple(value)
    for path in paths:
        if not isinstance(path, str) or not path.strip() or path.startswith("/"):
            raise InvalidTaskError(f"{field_name} must contain relative paths")
        parts = path.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise InvalidTaskError(f"{field_name} contains an unsafe path")
    if len(set(paths)) != len(paths):
        raise InvalidTaskError(f"{field_name} must not contain duplicates")
    return paths


def safe_locks(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidTaskError("resource_locks must be an array")
    locks = tuple(value)
    if any(
        not isinstance(lock, str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:._/-]{0,127}", lock)
        for lock in locks
    ):
        raise InvalidTaskError("resource_locks contain an unsafe identifier")
    if len(set(locks)) != len(locks):
        raise InvalidTaskError("resource_locks must be unique")
    return locks


def safe_commands(value: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidTaskError("verification_commands must be an array")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if not isinstance(command, (list, tuple)) or not 1 <= len(command) <= 32:
            raise InvalidTaskError(
                "each verification command must be a non-empty argv array"
            )
        argv = tuple(command)
        if any(
            not isinstance(item, str) or not item or "\x00" in item for item in argv
        ):
            raise InvalidTaskError(
                "verification command argv must contain safe strings"
            )
        executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable in _SHELL_NAMES or any(
            item.lower() in _SHELL_FLAGS for item in argv[1:]
        ):
            raise InvalidTaskError(
                "verification commands may not invoke a shell or evaluator"
            )
        if executable in _MUTATING_EXECUTABLES or (
            executable == "git"
            and any(item.lower() in _MUTATING_GIT_ACTIONS for item in argv[1:])
        ):
            raise InvalidTaskError(
                "verification commands may not perform delivery or repository mutation"
            )
        commands.append(argv)
    return tuple(commands)


def safe_labels(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidTaskError(f"{field_name} must be an array")
    labels = tuple(value)
    if any(
        not isinstance(item, str)
        or not item.strip()
        or "\x00" in item
        or len(item) > 256
        for item in labels
    ):
        raise InvalidTaskError(f"{field_name} must contain safe non-empty strings")
    if len(set(labels)) != len(labels):
        raise InvalidTaskError(f"{field_name} must not contain duplicates")
    return labels


def _json_safe(value: Any, *, path: str = "payload") -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidTaskError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise InvalidTaskError(f"{path} keys must be non-empty strings")
            normalized[key] = _json_safe(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise InvalidTaskError(f"{path} contains unsupported value {type(value).__name__}")


def json_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe deep copy of a task payload."""

    normalized = _json_safe(value)
    if not isinstance(normalized, dict):
        raise InvalidTaskError("payload must be an object")
    try:
        json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidTaskError("payload is not JSON serializable") from exc
    return normalized
