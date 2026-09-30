"""Materialize the deterministic Nations League v1 research contract.

The script has no network or credential path.  It writes only declared,
versioned research artifacts and is safe to re-run byte-for-byte.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from src.analysis.nations_league_v1 import (
    IMPLEMENTATION_SOURCE_SHA,
    MODEL_SPEC,
    MODEL_VERSION,
    SCHEMA_VERSION,
    canonical_json,
    model_digest,
    sha256_json,
)

TIMELINE_PR = 215
TIMELINE_HEAD = "065c6b40eb9911df3703d2e3079730a556136ee3"
TIMELINE_DIGEST = "2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef"
SOURCE_MAIN_SHA = IMPLEMENTATION_SOURCE_SHA
FREEZE_TIMESTAMP = "2026-09-30T00:00:00Z"


def _write(path: Path, body: dict[str, Any]) -> None:
    envelope = dict(body)
    envelope["artifact_digest"] = sha256_json(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(envelope) + b"\n")


def build_artifacts() -> dict[str, dict[str, Any]]:
    return {
        "nations_league_v1_model_spec_20260930.json": {
            "schema": "nations-league-v1-model-spec",
            "freeze_timestamp": FREEZE_TIMESTAMP,
            "model": MODEL_SPEC,
            "model_digest": model_digest(),
            "source_main_sha": SOURCE_MAIN_SHA,
            "timeline": {
                "pr": TIMELINE_PR,
                "head": TIMELINE_HEAD,
                "digest": TIMELINE_DIGEST,
            },
            "promotion_state": "CANDIDATE_ONLY",
            "production_activation": False,
            "publication_enabled": False,
        },
        "nations_league_v1_lifecycle_contract_20260930.json": {
            "schema": "nations-league-v1-lifecycle-contract",
            "model_version": MODEL_VERSION,
            "model_digest": model_digest(),
            "freeze_timestamp": FREEZE_TIMESTAMP,
            "phases": {
                "initial": {
                    "target": "T-24h",
                    "window": {"minimum": "PT22H", "maximum": "PT26H"},
                    "prediction_input": True,
                },
                "refinement": {
                    "target": "T-90m",
                    "window": {"minimum": "PT60M", "maximum": "PT120M"},
                    "prediction_input": True,
                },
                "closing_benchmark": {
                    "target": "latest pre-kickoff",
                    "window": {"minimum": "PT0S", "maximum": None},
                    "prediction_input": False,
                    "role": "RESEARCH_BENCHMARK_ONLY",
                },
            },
            "legacy_zero_to_three_hour_window": {
                "accepted": False,
                "reason": "legacy timing is not a v1 prediction phase",
            },
            "update_rule": "only a validated new point-in-time model input may alter probabilities; market/runtime evidence is stored separately",
            "no_invented_features": True,
        },
        "nations_league_v1_forward_shadow_schema_20260930.json": {
            "schema": SCHEMA_VERSION,
            "model_version": MODEL_VERSION,
            "model_digest": model_digest(),
            "freeze_timestamp": FREEZE_TIMESTAMP,
            "append_only": True,
            "record_fields": {
                "record_id": "deterministic fixture_id:phase:model_digest identifier",
                "fixture_id": "canonical timeline fixture identity",
                "kickoff_utc": "verified UTC timestamp",
                "phase": ["initial", "refinement", "closing_benchmark"],
                "prediction_timestamp": "UTC timestamp of the evaluation",
                "probabilities": {"home": "float", "draw": "float", "away": "float"},
                "model_version": MODEL_VERSION,
                "model_digest": "SHA-256 of frozen model spec",
                "source_evidence": [
                    "timeline digest",
                    "fixture source digest",
                    "result-safe cutoff evidence",
                ],
                "eventual_result": "null until result-safe outcome is available",
                "brier_score": "null until eventual_result is available",
                "log_loss": "null until eventual_result is available",
                "calibration_bucket": "declared bucket, never a future-derived input",
                "shadow": True,
                "signal_status": "SHADOW_ONLY",
                "no_bet": True,
                "publication_enabled": False,
                "ledger_mutation": False,
            },
            "forbidden": [
                "provider credentials",
                "actionable stake",
                "publication authority",
                "future result before result-safe time",
            ],
        },
        "nations_league_v1_promotion_criteria_20260930.json": {
            "schema": "nations-league-v1-promotion-criteria",
            "model_version": MODEL_VERSION,
            "model_digest": model_digest(),
            "declared_before_forward_outcomes": True,
            "best_current_candidate": "causal_elo",
            "production_certified": False,
            "minimum_completed_fixtures": 100,
            "minimum_fixtures_per_prediction_phase": {"initial": 40, "refinement": 40},
            "requirements": {
                "zero_point_in_time_leakage": True,
                "lifecycle_compliance": 1.0,
                "valid_probability_outputs": 1.0,
                "operational_record_completeness": 1.0,
                "finite_brier_and_log_loss": True,
                "calibration_reported_with_sample_sizes": True,
                "paired_date_cluster_bootstrap_reported": True,
                "stability_across_editions_and_phases": True,
                "no_unapproved_inputs": True,
            },
            "decision_rule": "promotion requires every gate; a candidate can be BEST_CURRENT_CANDIDATE without being PRODUCTION_CERTIFIED",
            "market_comparison": "deferred; never silently substituted",
        },
        "nations_league_v1_negative_evidence_20260930.json": {
            "schema": "nations-league-v1-negative-evidence",
            "freeze_timestamp": FREEZE_TIMESTAMP,
            "references": [
                {
                    "pr": 214,
                    "finding": "causal Elo strongest; GBT +0.070803 Brier vs Elo, 95% CI [+0.028790,+0.116802]",
                    "decision": "exclude GBT",
                },
                {
                    "pr": 216,
                    "finding": "baseline+context +0.119547 Brier vs baseline, 95% CI [+0.077058,+0.164126]",
                    "decision": "exclude context layer",
                },
                {
                    "pr": 215,
                    "finding": "canonical 512-fixture timeline and identities",
                    "decision": "bind timeline digest",
                },
                {
                    "pr": 223,
                    "finding": "public-web odds pilot yielded zero auditable initial/refinement coverage across 20 fixtures",
                    "decision": "defer market comparison",
                },
            ],
            "unavailable": [
                "historical squad/lineup PIT evidence",
                "validated historical market comparison",
            ],
        },
        "nations_league_v1_market_deferral_20260930.json": {
            "schema": "nations-league-v1-market-deferral",
            "status": "DEFERRED_NOT_BLOCKING_V1_FREEZE",
            "provider": "the_odds_api",
            "access_requirement": "paid historical entitlement",
            "public_web_pilot": {
                "fixtures_attempted": 20,
                "auditable_initial": 0,
                "auditable_refinement": 0,
            },
            "substitution_policy": "no current odds, public-web odds, or fabricated odds may substitute for historical snapshots",
            "next_step": "separate v2 market comparison after explicit entitlement and quota approval",
            "provider_requests_in_this_freeze": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("results/research"))
    args = parser.parse_args()
    for filename, body in build_artifacts().items():
        _write(args.output_dir / filename, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
