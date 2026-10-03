"""Durable per-slot guard for the Tennis Scan workflow.

The receipt is committed before provider-consuming work starts.  A completed
receipt therefore makes a late native schedule event a deterministic no-op,
while a claimed receipt conservatively prevents an uncertain duplicate.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / "results" / "tennis_scan_slots"
_SLOT_RE = re.compile(r"^tennis-scan:(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z)$")
_CRON_HOURS = {
    "0 2 * * *": 2,
    "0 4 * * *": 4,
    "0 6 * * *": 6,
    "0 9 * * *": 9,
    "0 12 * * *": 12,
    "0 15 * * *": 15,
    "0 18 * * *": 18,
    "0 21 * * *": 21,
    "0 23 * * *": 23,
}
_SLOT_HOURS = frozenset(_CRON_HOURS.values())


def _utc(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_slot(slot: str) -> str:
    match = _SLOT_RE.fullmatch(slot or "")
    if not match:
        raise ValueError("expected_slot must be tennis-scan:<UTC minute>Z")
    parsed = _utc(match.group(1))
    if parsed.hour not in _SLOT_HOURS or parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("expected_slot must be an hourly nominal slot")
    return f"tennis-scan:{parsed:%Y-%m-%dT%H:%MZ}"


def derive_expected_slot(schedule: str, now: datetime | None = None) -> str:
    """Derive the nominal slot from the native GitHub cron expression."""
    if schedule not in _CRON_HOURS:
        raise ValueError("unsupported native Tennis Scan cron expression")
    current = _utc(now)
    candidate = current.replace(
        hour=_CRON_HOURS[schedule], minute=0, second=0, microsecond=0
    )
    if candidate > current:
        candidate -= timedelta(days=1)
    return f"tennis-scan:{candidate:%Y-%m-%dT%H:%MZ}"


def receipt_path(slot: str, root: Path = ROOT) -> Path:
    canonical = validate_slot(slot)
    stamp = canonical.split(":", 1)[1].replace(":", "-")
    return root / "results" / "tennis_scan_slots" / f"{stamp}.json"


def load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("slot receipt is unreadable") from exc
    if not isinstance(value, dict):
        raise TypeError("slot receipt must be an object")
    return value


def claim_slot(
    slot: str,
    trigger_type: str,
    run_id: str,
    *,
    authoritative_receipt: dict[str, Any] | None = None,
    now: datetime | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    canonical = validate_slot(slot)
    if trigger_type not in {"native_schedule", "watchdog_recovery"}:
        raise ValueError("invalid trigger_type")
    if authoritative_receipt is not None:
        if not isinstance(authoritative_receipt, dict) or authoritative_receipt.get("expected_slot") != canonical:
            raise ValueError("authoritative slot receipt binding invalid")
        return {
            "action": "noop",
            "reason": "authoritative_slot_receipt_exists",
            "receipt_path": str(receipt_path(canonical, root)),
        }
    path = receipt_path(canonical, root)
    existing = load_receipt(path)
    if existing is not None:
        return {"action": "noop", "reason": "slot_receipt_exists", "receipt_path": str(path)}
    else:
        action = "run"

    timestamp = _utc(now).isoformat().replace("+00:00", "Z")
    receipt = {
        "schema_version": "tennis-scan-slot-receipt-v1",
        "expected_slot": canonical,
        "trigger_type": trigger_type,
        "started_at": timestamp,
        "completed_at": None,
        "status": "CLAIMED",
        "run_id": str(run_id or "unknown"),
        "provider_consuming_execution": False,
        "resulting_publication_identity": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"action": action, "reason": "slot_claimed", "receipt_path": str(path)}


def complete_slot(
    slot: str,
    *,
    now: datetime | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    path = receipt_path(slot, root)
    receipt = load_receipt(path)
    if receipt is None or receipt.get("status") != "CLAIMED":
        raise ValueError("only a claimed slot can be completed")
    receipt.update(
        status="COMPLETED",
        completed_at=_utc(now).isoformat().replace("+00:00", "Z"),
        provider_consuming_execution=True,
    )
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"action": "completed", "receipt_path": str(path)}


def mark_pre_provider_failed(
    slot: str,
    *,
    now: datetime | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    path = receipt_path(slot, root)
    receipt = load_receipt(path)
    if receipt is None or receipt.get("status") != "CLAIMED":
        raise ValueError("only a claimed slot can be marked pre-provider failed")
    receipt.update(
        status="PRE_PROVIDER_FAILED",
        failed_at=_utc(now).isoformat().replace("+00:00", "Z"),
        provider_consuming_execution=False,
    )
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"action": "pre_provider_failed", "receipt_path": str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    derive = sub.add_parser("derive")
    derive.add_argument("--schedule", required=True)
    derive.add_argument("--now")

    claim = sub.add_parser("claim")
    claim.add_argument("--expected-slot", required=True)
    claim.add_argument("--trigger-type", required=True)
    claim.add_argument("--run-id", required=True)
    claim.add_argument("--authoritative-receipt-json")

    for name in ("complete", "pre-provider-failed"):
        command = sub.add_parser(name)
        command.add_argument("--expected-slot", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "derive":
            print(derive_expected_slot(args.schedule, _utc(args.now) if args.now else None))
        elif args.command == "claim":
            authoritative = None
            if args.authoritative_receipt_json:
                authoritative = json.loads(Path(args.authoritative_receipt_json).read_text(encoding="utf-8"))
            print(json.dumps(claim_slot(
                args.expected_slot,
                args.trigger_type,
                args.run_id,
                authoritative_receipt=authoritative,
            )))
        elif args.command == "complete":
            print(json.dumps(complete_slot(args.expected_slot)))
        else:
            print(json.dumps(mark_pre_provider_failed(args.expected_slot)))
    except (OSError, ValueError) as exc:
        print(f"tennis slot receipt failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
