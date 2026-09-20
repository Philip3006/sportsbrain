#!/usr/bin/env python3
"""Prepare or explicitly execute the governed Top-5 public delivery seam.

Default behavior is read-only dry-run.  ``--execute`` is required before the
isolated runtime publisher or Worker ``/signals`` transport can be called.
The command never requests odds, activates a provider, places bets, writes the
ledger, or deploys Cloudflare.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.football.top5_public_delivery import (
    TOP5_DELIVERY_ACCEPTANCE_REQUIRED,
    TOP5_DELIVERY_IDEMPOTENT,
    FileControlledDeliveryCapabilityConsumer,
    HttpWorkerDeliveryTransport,
    RuntimePublisherStaticTransport,
    Top5CanonicalDeliveryAdapter,
    Top5DeliveryAttestation,
    Top5DeliveryError,
    Top5DeliveryExecutor,
    artifact_from_mapping,
    plan_from_mapping,
    plan_to_mapping,
)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Top5DeliveryError(f"cannot read governed JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise Top5DeliveryError(f"governed JSON input must be an object: {path}")
    return value


def _write_object(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Top5DeliveryError("--now must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5DeliveryError("--now must include a timezone")
    return parsed.astimezone(timezone.utc)


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True))


def _prepare(args: argparse.Namespace) -> int:
    artifact = artifact_from_mapping(_read_object(args.artifact))
    current = _read_object(args.current_snapshot)
    plan = Top5CanonicalDeliveryAdapter().build_plan(current, artifact)
    plan_payload = plan_to_mapping(plan)
    if args.plan_output:
        _write_object(args.plan_output, plan_payload)
    result = {
        "status": "TOP5_DELIVERY_PREPARED",
        "plan_output": str(args.plan_output) if args.plan_output else None,
        "manifest": plan.manifest(),
    }
    _print(result)
    return 0


def _execute(args: argparse.Namespace) -> int:
    artifact = artifact_from_mapping(_read_object(args.artifact))
    current = _read_object(args.current_snapshot)
    plan = plan_from_mapping(_read_object(args.plan))
    attestation = Top5DeliveryAttestation.from_mapping(
        _read_object(args.attestation)
    )
    capability = _read_object(args.capability_token)
    if Path(args.capability_token).resolve().is_relative_to(ROOT):
        raise Top5DeliveryError("capability token must remain outside the repository")

    if not args.execute:
        result = Top5DeliveryExecutor(clock=lambda: _now(args.now)).execute(
            artifact=artifact,
            current_public_snapshot=current,
            plan=plan,
            attestation=attestation,
            capability=capability,
            dry_run=True,
        )
        _print(result.as_payload())
        return 0

    if not args.active_checkout or not args.stage_directory or not args.runtime_log:
        raise Top5DeliveryError(
            "--execute requires --active-checkout, --stage-directory, and --runtime-log"
        )
    static_transport = RuntimePublisherStaticTransport(
        active_checkout=args.active_checkout,
        stage_directory=args.stage_directory,
        log_path=args.runtime_log,
        commit_message=args.commit_message,
        publish_script=args.publish_script,
    )
    worker_transport = HttpWorkerDeliveryTransport(signals_url=args.worker_url)
    result = Top5DeliveryExecutor(
        static_transport=static_transport,
        worker_transport=worker_transport,
        capability_consumer=FileControlledDeliveryCapabilityConsumer(),
        clock=lambda: _now(args.now),
    ).execute(
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        dry_run=False,
    )
    _print(result.as_payload())
    return 0 if result.status in {
        TOP5_DELIVERY_ACCEPTANCE_REQUIRED,
        TOP5_DELIVERY_IDEMPOTENT,
    } else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="build a no-write delivery plan")
    prepare.add_argument("--artifact", type=Path, required=True)
    prepare.add_argument("--current-snapshot", type=Path, required=True)
    prepare.add_argument("--plan-output", type=Path)
    prepare.set_defaults(handler=_prepare)

    execute = subparsers.add_parser("execute", help="dry-run unless --execute is explicit")
    execute.add_argument("--artifact", type=Path, required=True)
    execute.add_argument("--current-snapshot", type=Path, required=True)
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--attestation", type=Path, required=True)
    execute.add_argument("--capability-token", type=Path, required=True)
    execute.add_argument("--now")
    execute.add_argument("--execute", action="store_true")
    execute.add_argument("--active-checkout", type=Path)
    execute.add_argument("--stage-directory", type=Path)
    execute.add_argument("--runtime-log", type=Path)
    execute.add_argument("--publish-script", type=Path)
    execute.add_argument("--worker-url")
    execute.add_argument("--commit-message", default="publish: controlled Top5 public generation")
    execute.set_defaults(handler=_execute)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, TypeError, ValueError, Top5DeliveryError) as exc:
        _print({"status": "TOP5_DELIVERY_BLOCKED", "reason": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
