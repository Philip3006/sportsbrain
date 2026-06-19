"""Sends a web-push notification when a cron job fails, with state-based
throttling so a recurring failure (e.g. expired API key) doesn't spam.

Throttle logic (per job):
  - last_status == "ok"   → push on next failure   (state transition)
  - last_status == "fail" → push only if older than THROTTLE_HOURS
  - manual reset:           rm results/health/push_state.json

CLI (called from scripts/_health.sh on non-zero exit):
    python3 -m src.notifications.health_push <job> [<error_msg>]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "results" / "health" / "push_state.json"

# Renotify after this many hours even when status hasn't transitioned ok→fail.
# Keeps the user informed of persistent issues without spamming.
THROTTLE_HOURS = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(tmp, STATE_PATH)


def _should_push(job: str, state: dict[str, Any]) -> bool:
    """Decides whether we send a push for this failure, based on throttle."""
    entry = state.get(job)
    if not entry:
        return True  # first failure ever → push

    if entry.get("last_status") == "ok":
        return True  # transition ok → fail

    # Same failure persisting — renotify after THROTTLE_HOURS.
    last_iso = entry.get("last_pushed_at", "")
    try:
        last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    except Exception:
        return True
    return (_now() - last) > timedelta(hours=THROTTLE_HOURS)


def notify_failure(job: str, error: str | None = None,
                   throttle_hours: int = THROTTLE_HOURS) -> bool:
    """Sends one failure notification subject to throttling.

    Returns True if a push was attempted, False if throttled.
    """
    global THROTTLE_HOURS
    THROTTLE_HOURS = throttle_hours

    state = _load_state()
    if not _should_push(job, state):
        return False

    msg = (error or "").strip()
    if len(msg) > 200:
        msg = msg[:197] + "…"
    body = msg or f"Cron-Job {job} ist fehlgeschlagen — Details im Health-Tab."

    try:
        from src.notifications.web_push import _send_notification
    except Exception as e:
        print(f"  [health_push] web_push import failed: {e}")
        return False

    try:
        _send_notification(
            title=f"⚠️ {job} fehlgeschlagen",
            body=body,
            url="/?view=settings",
            kind="health",
            tag=f"health-{job}",
            require=False,
        )
    except Exception as e:
        print(f"  [health_push] send failed: {e}")
        # Even on send-failure, record the attempt so we don't retry every minute.

    state[job] = {
        "last_status": "fail",
        "last_pushed_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": msg[:200] if msg else None,
    }
    _save_state(state)
    return True


def mark_recovered(job: str) -> None:
    """Records that a job succeeded — clears the throttle so the next failure
    will trigger a push immediately. Called from _health.sh on status=ok."""
    state = _load_state()
    entry = state.get(job, {})
    if entry.get("last_status") != "ok":
        state[job] = {
            "last_status": "ok",
            "last_recovered_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save_state(state)


def _cli() -> int:
    if len(sys.argv) < 2:
        print("usage: python3 -m src.notifications.health_push <job> [<error_msg>]")
        return 2
    job = sys.argv[1]
    error = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None

    # Special op: --recover marks a job as ok-state, used by _health.sh.
    if error == "--recover":
        mark_recovered(job)
        print(f"[health_push] {job} marked recovered")
        return 0

    sent = notify_failure(job, error)
    print(f"[health_push] {job}: {'sent' if sent else 'throttled'}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
