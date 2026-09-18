"""Fail-open local notifications for meaningful Night Shift transitions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import TaskRecord, TaskState
from .store import DispatcherStore

NotificationSender = Callable[["NightShiftNotification"], Any]


@dataclass(frozen=True)
class NightShiftNotification:
    """Sanitized operator notification payload."""

    task_id: str
    builder_id: str
    state: str
    short_task: str
    message: str
    reason: str | None = None
    pr_number: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class NightShiftNotificationWatcher:
    """Poll the queue and notify once per meaningful state/evidence change."""

    def __init__(
        self,
        db_path: Path,
        *,
        seen_path: Path | None = None,
        sender: NotificationSender | None = None,
        pid_checker: Callable[[int], bool] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.seen_path = _notification_state_path(seen_path)
        self.sender = sender or send_macos_notification
        self.pid_checker = pid_checker or _pid_exists
        self.sender_failures = 0

    def poll(self, *, dry_run: bool = False) -> list[NightShiftNotification]:
        """Return notifications; sender errors never touch queue state."""

        records = DispatcherStore(self.db_path).list_tasks(limit=1000)
        seen = self._load_seen()
        notifications: list[NightShiftNotification] = []
        next_seen = dict(seen)
        for record in sorted(records, key=lambda item: item.task_id):
            event = self._event_for(record)
            if event is None:
                continue
            signature = self._signature(event, record)
            if seen.get(record.task_id) == signature:
                continue
            notification = self._notification(record, event)
            notifications.append(notification)
            next_seen[record.task_id] = signature
            if not dry_run:
                try:
                    self.sender(notification)
                except Exception:  # noqa: BLE001 - notifications are fail-open
                    self.sender_failures += 1
        if not dry_run:
            self._save_seen(next_seen)
        return notifications

    def _event_for(self, record: TaskRecord) -> str | None:
        if record.state is TaskState.COMPLETED:
            return "COMPLETED"
        if record.state is TaskState.PR_READY:
            return "PR_READY"
        if record.state is TaskState.CEO_REVIEW:
            return "CEO_REVIEW"
        if record.state is TaskState.PAUSED_QUOTA:
            return "PAUSED_QUOTA"
        if record.state is TaskState.FAILED_SAFE:
            failure = (record.failure_class or "").upper()
            return (
                "DEAD_LETTER"
                if "TIMEOUT" in failure or "DEAD_LETTER" in failure
                else "FAILED_SAFE"
            )
        if record.state is TaskState.BLOCKED and record.delivery_blocked:
            return "DELIVERY_FAILED"
        if record.state is TaskState.CANCELLED:
            return "CANCELLED"
        if (
            record.state is TaskState.RUNNING
            and isinstance(record.process_id, int)
            and not self.pid_checker(record.process_id)
        ):
            return "RUNNING_DEAD_PID"
        return None

    @staticmethod
    def _signature(event: str, record: TaskRecord) -> str:
        return ":".join(
            (
                event,
                record.state.value,
                record.failure_class or "",
                str(record.pr_number or ""),
                record.available_at,
                "dead-pid" if event == "RUNNING_DEAD_PID" else "",
            )
        )

    @staticmethod
    def _notification(record: TaskRecord, event: str) -> NightShiftNotification:
        short_task = record.branch.rstrip("/").rsplit("/", 1)[-1] or record.task_id[-16:]
        reason = None
        if event not in {"COMPLETED", "CANCELLED"}:
            reason = _safe_short_reason(record.last_error)
        parts = [f"{record.builder_id} {event}", short_task]
        if reason:
            parts.append(reason)
        if record.pr_number is not None:
            parts.append(f"PR #{record.pr_number}")
        return NightShiftNotification(
            task_id=record.task_id,
            builder_id=record.builder_id,
            state=event,
            short_task=short_task,
            message=" — ".join(parts),
            reason=reason,
            pr_number=record.pr_number,
        )

    def _load_seen(self) -> dict[str, str]:
        try:
            raw = json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _save_seen(self, values: dict[str, str]) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.seen_path.name}.", dir=self.seen_path.parent
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(values, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.seen_path)
            temporary_name = None
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)


def _notification_state_path(path: Path | None) -> Path:
    candidate = Path(
        path
        or os.getenv(
            "SPORTSBRAIN_NIGHTSHIFT_NOTIFICATION_STATE",
            str(
                Path.home()
                / "Library"
                / "Application Support"
                / "SportsBrain"
                / "runtime-state"
                / "nightshift-notifications.json"
            ),
        )
    ).expanduser()
    if not candidate.is_absolute():
        raise ValueError("notification state path must be absolute")
    repo_root = Path(__file__).resolve().parents[2]
    resolved = candidate.resolve()
    if resolved == repo_root or repo_root in resolved.parents:
        raise ValueError("notification state must live outside the Git repository")
    return candidate


def _safe_short_reason(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(
        r"(?i)(token|secret|password|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    )
    cleaned = re.sub(r"gh[pousr]_[A-Za-z0-9_-]+", "[redacted]", cleaned)
    cleaned = re.sub(r"https?://\S+", "[url]", cleaned)
    return " ".join(cleaned.split())[:180]


def _pid_exists(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def send_macos_notification(notification: NightShiftNotification) -> None:
    """Send one user-level macOS notification through ``osascript``."""

    def applescript(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{applescript(notification.message)}" with title "SportsBrain Night Shift"',
        ],
        check=True,
        capture_output=True,
        text=True,
    )


__all__ = [
    "NightShiftNotification",
    "NightShiftNotificationWatcher",
    "send_macos_notification",
]
