#!/usr/bin/env python3
"""Run the side-effect-free Top-5 activation precheck.

The command only validates operator-supplied JSON.  It never calls a
provider, enables a scheduler, publishes an artifact, or writes runtime,
ledger, or Cloudflare state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.production_contracts import ActivationMode
from src.football.top5_runtime_operations import (
    TOP5_RUNTIME_LEAGUES,
    Top5ActivationPrecheckInput,
    Top5RuntimeConfig,
    top5_activation_precheck,
)


def _load(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read precheck input: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("precheck input must be a JSON object")
    return payload


def _config(raw: object) -> Top5RuntimeConfig:
    if raw is None:
        return Top5RuntimeConfig()
    if not isinstance(raw, dict):
        raise TypeError("runtime_config must be a JSON object")
    return Top5RuntimeConfig(
        provider_identity=str(raw.get("provider_identity", "the_odds_api")),
        activation_mode=ActivationMode(raw.get("activation_mode", "disabled")),
        scheduler_enabled=raw.get("scheduler_enabled", False),
        publication_enabled=raw.get("publication_enabled", False),
        betting_enabled=raw.get("betting_enabled", False),
        ledger_mutation_enabled=raw.get("ledger_mutation_enabled", False),
        max_retries=raw.get("max_retries", 0),
        timeout_seconds=raw.get("timeout_seconds", 30.0),
        maximum_odds_age_seconds=raw.get("maximum_odds_age_seconds", 900),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="offline precheck JSON"
    )
    args = parser.parse_args(argv)
    try:
        payload = _load(args.input)
        values = Top5ActivationPrecheckInput(
            evidence_reference=payload.get("evidence_reference"),
            five_league_evidence_valid=payload.get("five_league_evidence_valid", False),
            builder1_acceptance_passed=payload.get("builder1_acceptance_passed", False),
            provider_authority=payload.get("provider_authority", "the_odds_api"),
            provider_authority_granted=payload.get("provider_authority_granted", False),
            model_bound=payload.get("model_bound", False),
            research_bound=payload.get("research_bound", False),
            signal_time_approved=payload.get("signal_time_approved", False),
            one_shot_operator_path_ready=payload.get(
                "one_shot_operator_path_ready", False
            ),
            recurring_scheduler_registered=payload.get(
                "recurring_scheduler_registered", False
            ),
            scheduler_ready=payload.get("scheduler_ready", False),
            health_ready=payload.get("health_ready", False),
            rollback_ready=payload.get("rollback_ready", False),
            activation_authorized=payload.get("activation_authorized", False),
            publication_preflight_ready=payload.get(
                "publication_preflight_ready", False
            ),
            no_synthetic_evidence=payload.get("no_synthetic_evidence", False),
            league_scope=tuple(payload.get("league_scope", TOP5_RUNTIME_LEAGUES)),
            runtime_config=_config(payload.get("runtime_config")),
        )
        report = top5_activation_precheck(values)
    except (OSError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "TOP5_RUNTIME_ACTIVATION_BLOCKED",
                    "ready": False,
                    "failures": [str(exc)],
                    "warnings": [],
                    "activation_mode": "disabled",
                    "mutation_performed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report.as_payload(), indent=2, sort_keys=True))
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
