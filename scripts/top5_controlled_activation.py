#!/usr/bin/env python3
"""Durable Top-5 one-league activation control commands.

Preparation/status/rollback only touch the external Top-5 state store. The
execute command is explicitly opt-in and fails closed while no reviewed real
one-shot model/provider runtime is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.production_contracts import SignalTimeContract
from src.football.top5_b2_qualification_batch_orchestrator import (
    Builder2FiveLeagueReceiptPackageV1,
)
from src.football.top5_controlled_release import (
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
)
from src.football.top5_durable_activation import (
    PRODUCTION_RUNTIME_BLOCKER,
    DurableActivationError,
    DurableTop5ActivationStore,
    Top5DurableActivationPlanV1,
    activation_health_payload,
    prepare_top5_durable_activation_plan,
)
from src.football.top5_production_activation import Top5ProductionRoutingSnapshot


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _read(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise ValueError("activation input path must be absolute")
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("activation input must be a regular file")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("activation input must be owner-only and user-owned")
        if metadata.st_size > 10_000_000:
            raise ValueError("activation input exceeds the safe size limit")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            payload = json.loads(stream.read().decode("utf-8"))
        return _object(payload, "input")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("activation input is unreadable or malformed") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _authority(raw_value: object) -> ApprovedProviderResultAuthority:
    raw = _object(raw_value, "provider_authority")
    return ApprovedProviderResultAuthority(
        authority_decision_id=raw["authority_decision_id"],
        league_code=raw["league_code"],
        approved_odds_provider=raw["approved_odds_provider"],
        approved_provider_set=tuple(raw["approved_provider_set"]),
        approved_result_source=raw["approved_result_source"],
        issued_at=raw["issued_at"],
        expires_at=raw.get("expires_at"),
        approved=raw.get("approved", True),
    )


def _activation(raw_value: object) -> ControlledActivationAuthorization:
    raw = _object(raw_value, "activation_authorization")
    signal = _object(raw["signal_time_contract"], "signal_time_contract")
    sample = _object(raw["minimum_sample_policy"], "minimum_sample_policy")
    return ControlledActivationAuthorization(
        authorization_id=raw["authorization_id"],
        activation_id=raw["activation_id"],
        league_code=raw["league_code"],
        candidate_id=raw["candidate_id"],
        model_identity=raw["model_identity"],
        source_sha=raw["source_sha"],
        research_sha=raw["research_sha"],
        model_artifact_hash=raw["model_artifact_hash"],
        signal_time_experiment_id=raw["signal_time_experiment_id"],
        signal_time_contract=SignalTimeContract(
            minimum_minutes_before_kickoff=signal["minimum_minutes_before_kickoff"],
            maximum_minutes_before_kickoff=signal["maximum_minutes_before_kickoff"],
            maximum_odds_age_seconds=signal["maximum_odds_age_seconds"],
            approval_ref=signal.get("approval_ref"),
        ),
        minimum_sample_policy=MinimumSamplePolicy(
            minimum_real_observations=sample["minimum_real_observations"],
            minimum_distinct_fixtures=sample.get("minimum_distinct_fixtures", 1),
        ),
        provider_authority=_authority(raw["provider_authority"]),
        controlled_shadow_run_id=raw["controlled_shadow_run_id"],
        qualification_session_id=raw["qualification_session_id"],
        ceo_shadow_authorization_id=raw["ceo_shadow_authorization_id"],
        fixture_scope=tuple(raw["fixture_scope"]),
        rollback_pointer=raw["rollback_pointer"],
        authorization_token=raw["authorization_token"],
        issued_at=raw["issued_at"],
        expires_at=raw["expires_at"],
        activation_authorized=raw.get("activation_authorized", False),
        no_bet=raw.get("no_bet", False),
    )


def _snapshot(raw_value: object) -> Top5ProductionRoutingSnapshot:
    raw = _object(raw_value, "current_snapshot")
    return Top5ProductionRoutingSnapshot(
        provider_order=tuple(raw["provider_order"]),
        adapter_registry=tuple(raw["adapter_registry"]),
        provider_config_digest=raw["provider_config_digest"],
        snapshot_id=raw["snapshot_id"],
        top5_scheduler_enabled=raw.get("top5_scheduler_enabled", False),
        publication_enabled=raw.get("publication_enabled", False),
        no_bet=raw.get("no_bet", True),
        ledger_mutation_enabled=raw.get("ledger_mutation_enabled", False),
        snapshot_digest=raw.get("snapshot_digest", ""),
    )


def _prepare_from_input(payload: dict[str, Any], now: datetime):
    package = Builder2FiveLeagueReceiptPackageV1.from_payload(
        payload["five_league_receipt_package"]
    )
    activation = _activation(payload["activation_authorization"])
    authority = _authority(payload["provider_authority"])
    if activation.provider_authority != authority:
        raise DurableActivationError("activation/provider authority mismatch")
    return prepare_top5_durable_activation_plan(
        receipt_package=package,
        b1_acceptance_bundle=_object(
            payload["b1_final_acceptance_bundle"], "b1_final_acceptance_bundle"
        ),
        authority=authority,
        activation=activation,
        current_snapshot=_snapshot(payload["pre_activation_snapshot"]),
        signal_time_approval_identity=payload["signal_time_approval_identity"],
        now=now,
    )


def _match_prepared_plan_timestamp(
    plan: Top5DurableActivationPlanV1, record: object, *, now: datetime
) -> Top5DurableActivationPlanV1:
    """Recompute current bindings while preserving the original plan identity."""
    saved = _object(record, "prepared activation record")
    saved_plan = _object(saved.get("plan"), "prepared activation plan")
    if saved.get("status") != "PREPARED":
        raise DurableActivationError("activation is not in PREPARED state")
    prepared_at_value = saved_plan.get("prepared_at")
    if not isinstance(prepared_at_value, str):
        raise DurableActivationError("prepared activation timestamp is missing")
    try:
        prepared_at = datetime.fromisoformat(prepared_at_value)
    except ValueError as exc:
        raise DurableActivationError(
            "prepared activation timestamp is invalid"
        ) from exc
    rebound = replace(plan, prepared_at=prepared_at)
    rebound.validate(now=now)
    if saved.get("plan_digest") != rebound.plan_digest:
        raise DurableActivationError(
            "current evidence differs from the exact prepared activation plan"
        )
    return rebound


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--input", required=True, type=Path)
    execute = commands.add_parser("execute")
    execute.add_argument("--input", required=True, type=Path)
    execute.add_argument("--activation-id", required=True)
    execute.add_argument(
        "--execute",
        action="store_true",
        help="explicit execution opt-in; default is dry-run",
    )
    status = commands.add_parser("status")
    status.add_argument("--activation-id")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--activation-id", required=True)
    rollback.add_argument("--plan-digest", required=True)
    args = parser.parse_args(argv)
    store = DurableTop5ActivationStore()
    now = datetime.now(timezone.utc)
    try:
        if args.command == "prepare":
            plan = _prepare_from_input(_read(args.input), now)
            existing = store.status(plan.activation_id).get("record")
            if isinstance(existing, dict) and existing.get("status") == "PREPARED":
                plan = _match_prepared_plan_timestamp(plan, existing, now=now)
            record = store.prepare(plan, now=now)
            output = {
                "status": "TOP5_CONTROLLED_ACTIVATION_PREPARED",
                "activation_id": plan.activation_id,
                "activation_league": plan.activation_league,
                "plan_digest": plan.plan_digest,
                "state": record["status"],
                "publication_enabled": False,
                "scheduler_registered": False,
                "betting_enabled": False,
                "ledger_mutation_enabled": False,
            }
            print(json.dumps(output, sort_keys=True))
            return 0
        if args.command == "execute":
            if not args.execute:
                raise DurableActivationError(
                    "execute is dry-run by default; explicit --execute is required"
                )
            payload = _read(args.input)
            plan = _prepare_from_input(payload, now)
            if plan.activation_id != args.activation_id:
                raise DurableActivationError(
                    "activation ID does not match exact evidence"
                )
            existing = store.status(args.activation_id).get("record")
            if not isinstance(existing, dict):
                raise DurableActivationError(
                    "exact prepared activation plan is missing"
                )
            plan = _match_prepared_plan_timestamp(plan, existing, now=now)
            store.execute(plan, now=now, explicit_execute=True)
            # Current main intentionally has no production one-shot runtime.
            raise DurableActivationError(PRODUCTION_RUNTIME_BLOCKER)
        if args.command == "status":
            print(
                json.dumps(
                    activation_health_payload(store, args.activation_id), sort_keys=True
                )
            )
            return 0
        record = store.rollback(args.activation_id, args.plan_digest, now=now)
        print(
            json.dumps(
                {
                    "status": "TOP5_CONTROLLED_ACTIVATION_ROLLED_BACK",
                    "activation_id": args.activation_id,
                    "record_status": record["status"],
                    "rollback_evidence_digest": record["rollback_evidence_digest"],
                    "publication_enabled": False,
                    "scheduler_registered": False,
                    "betting_enabled": False,
                    "ledger_mutation_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not include raw input or bearer-token fields in diagnostics.
        message = str(exc)
        safe = (
            PRODUCTION_RUNTIME_BLOCKER
            if PRODUCTION_RUNTIME_BLOCKER in message
            else message
        )
        print(
            json.dumps(
                {
                    "status": "TOP5_CONTROLLED_ACTIVATION_BLOCKED",
                    "failure": safe,
                    "activation_mode": "disabled",
                    "provider_authority": "the_odds_api",
                    "publication_enabled": False,
                    "scheduler_registered": False,
                    "betting_enabled": False,
                    "ledger_mutation_enabled": False,
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
