"""Run the offline Nations League native-model research harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import nations_league_validation as validation
from src.analysis.nations_league_native_research import (
    run_native_research,
    weight_specs,
)

DEFAULT_OUTPUT = Path(
    "results/audits/nations_league_native_model_research_20260929.json"
)
DEFAULT_PRIOR_AUDIT = Path(
    "results/audits/nations_league_model_validation_20260927.json"
)


def _prior_reference(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    audit = json.loads(path.read_text(encoding="utf-8"))
    strict = audit.get("strict_validation", {})
    variants = strict.get("model_variants", {})
    return {
        "audit_path": str(path),
        "audit_generated_at": audit.get("generated_at"),
        "audit_source_sha": audit.get("source_sha"),
        "results_cache": strict.get("results_cache"),
        "evaluation_periods": audit.get("historical_periods", []),
        "strict_elo_metrics": variants.get("elo"),
        "strict_dixon_coles_metrics": variants.get("dixon_coles"),
        "strict_paired_date_cluster_bootstrap": strict.get(
            "paired_date_cluster_bootstrap"
        ),
        "frozen_wm2026_retrospective_diagnostic": audit.get(
            "retrospective_frozen_model_diagnostic"
        ),
        "interpretation": (
            "Historical reference only, copied from a prior committed audit; not rerun in this task. "
            "Frozen WM2026 values are retrospective and not a fair causal baseline."
        ),
    }


def blocked_report(
    *,
    source_sha: str | None,
    verified_current_main_sha: str | None,
    requested_cache: Path | None,
    prior_audit: Path,
) -> dict[str, Any]:
    prior = _prior_reference(prior_audit)
    prior_periods = prior.get("evaluation_periods", []) if prior else []
    cache_status = "not_supplied"
    if requested_cache is not None:
        cache_status = "missing" if not requested_cache.is_file() else "not_loaded"
    comparison = []
    for label, key in (("Elo", "elo"), ("Dixon-Coles", "dixon_coles")):
        variant = (
            prior.get(
                f"strict_{'dixon_coles' if key == 'dixon_coles' else 'elo'}_metrics"
            )
            if prior
            else None
        )
        metrics = (variant or {}).get("metrics") or {}
        comparison.append(
            {
                "model": label,
                "status": "REFERENCE_ONLY_NOT_RERUN",
                "n": (variant or {}).get("matches_evaluated"),
                "coverage": (variant or {}).get("coverage"),
                "multiclass_brier": metrics.get("brier_score_multiclass"),
                "multiclass_log_loss": metrics.get("multiclass_log_loss"),
                "ece_10_bins": metrics.get(
                    "expected_calibration_error_10_bins_mean_one_vs_rest"
                ),
                "accuracy_secondary": metrics.get("accuracy_argmax"),
                "mean_max_probability": metrics.get("mean_max_probability_sharpness"),
                "provenance": "Prior committed audit only; no source rows were available to rerun this task.",
            }
        )
    comparison.extend(
        [
            {
                "model": "Frozen WM2026 approach",
                "status": "UNAVAILABLE_AS_FAIR_CAUSAL_BASELINE",
                "n": None,
                "coverage": None,
                "multiclass_brier": None,
                "multiclass_log_loss": None,
                "ece_10_bins": None,
                "accuracy_secondary": None,
                "mean_max_probability": None,
                "provenance": "Prior audit contains a retrospective transfer diagnostic only; frozen parameters saw later outcomes and required point-in-time features are absent.",
            },
            {
                "model": "nations_league_v1 weighted result-only GBT",
                "status": "NOT_EVALUATED",
                "n": None,
                "coverage": None,
                "multiclass_brier": None,
                "multiclass_log_loss": None,
                "ece_10_bins": None,
                "accuracy_secondary": None,
                "mean_max_probability": None,
                "provenance": "Candidate harness and predeclared grid are ready; source data absent.",
            },
        ]
    )
    return {
        "schema": "nations-league-native-model-research-v1",
        "status": "BLOCKED_MISSING_LOCAL_RESULTS_CACHE",
        "research_harness_ready": True,
        "source_sha": source_sha,
        "verified_current_main_sha": verified_current_main_sha,
        "data_provenance": {
            "results_cache_status": cache_status,
            "explicit_results_cache_path": str(requested_cache)
            if requested_cache
            else None,
            "network_fetch_performed": False,
            "provider_requests": 0,
            "live_scores_or_settlement_data_read": False,
            "prior_audit_reference": prior,
        },
        "dataset_sizes": {
            "current_task_local_result_rows": None,
            "current_task_evaluation_rows": None,
            "historical_block_counts_from_prior_audit_only": [
                {"period": row.get("id"), "matches": row.get("match_count")}
                for row in prior_periods
            ],
            "prior_audit_cache_row_count": (
                prior.get("results_cache", {}).get("row_count") if prior else None
            ),
        },
        "evaluation_blocks": [row.get("id") for row in prior_periods],
        "model_comparison": [
            {
                "model": "Elo",
                "status": "REFERENCE_ONLY_NOT_RERUN",
                "metrics": prior.get("strict_elo_metrics") if prior else None,
            },
            {
                "model": "Dixon-Coles",
                "status": "REFERENCE_ONLY_NOT_RERUN",
                "metrics": prior.get("strict_dixon_coles_metrics") if prior else None,
            },
            {
                "model": "Frozen WM2026 approach",
                "status": "UNAVAILABLE_AS_FAIR_CAUSAL_BASELINE",
                "retrospective_diagnostic": prior.get(
                    "frozen_wm2026_retrospective_diagnostic"
                )
                if prior
                else None,
            },
            {
                "model": "nations_league_v1 weighted result-only GBT",
                "status": "NOT_EVALUATED",
                "reason": "No historical local results cache is present to train or score the predeclared candidate.",
                "weight_grid_points": len(weight_specs()),
            },
        ],
        "model_comparison_table": comparison,
        "paired_bootstrap": {
            "native_candidate": "UNAVAILABLE: candidate predictions were not computed",
            "prior_elo_vs_dixon_coles_reference": (
                prior.get("strict_paired_date_cluster_bootstrap") if prior else None
            ),
        },
        "recommendation": "Do not create or promote a native snapshot until the explicit local historical cache is restored and this harness completes.",
        "snapshot_written": False,
        "production_or_publication_side_effects": "none",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-cache",
        type=Path,
        help="Explicit local canonical results pickle. Omit to emit a blocked audit; never fetched automatically.",
    )
    parser.add_argument("--source-sha", help="Exact code revision being evaluated.")
    parser.add_argument(
        "--verified-current-main-sha", help="Freshly verified origin/main SHA."
    )
    parser.add_argument("--prior-audit", type=Path, default=DEFAULT_PRIOR_AUDIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.results_cache is None or not args.results_cache.is_file():
        report = blocked_report(
            source_sha=args.source_sha,
            verified_current_main_sha=args.verified_current_main_sha,
            requested_cache=args.results_cache,
            prior_audit=args.prior_audit,
        )
        validation.write_json_atomic(report, args.output)
        print(
            f"Wrote blocked audit {args.output}: local results cache unavailable; network was not used"
        )
        return 0

    results, cache_digest = validation.load_local_results(args.results_cache)
    baseline = validation.run_validation(
        results=results,
        cache_sha256=cache_digest,
        source_sha=args.source_sha,
        verified_current_main_sha=args.verified_current_main_sha,
    )
    native = run_native_research(results)
    report = {
        "schema": "nations-league-native-model-research-v1",
        "status": "EVALUATED_RESEARCH_ONLY",
        "research_harness_ready": True,
        "source_sha": args.source_sha,
        "verified_current_main_sha": args.verified_current_main_sha,
        "dataset_sizes": {
            "results_cache_rows": len(results),
            "nations_league_evaluation_matches": native["evaluation_match_count"],
            "historical_blocks": [
                {"period": row["id"], "matches": row["match_count"]}
                for row in baseline["historical_periods"]
            ],
            "results_cache_sha256": cache_digest,
        },
        "baseline_validation": baseline,
        "native_candidate": native,
        "model_comparison_table": _evaluated_comparison_table(baseline, native),
        "snapshot_written": False,
        "production_or_publication_side_effects": "none",
    }
    validation.write_json_atomic(report, args.output)
    print(
        f"Wrote research audit {args.output}: NL={native['evaluation_match_count']}; "
        "no model snapshot or production side effect"
    )
    return 0


def _evaluated_comparison_table(
    baseline: dict[str, Any], native: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    strict_variants = baseline["strict_validation"]["model_variants"]
    for label, key in (
        ("Elo", "elo"),
        ("Dixon-Coles", "dixon_coles"),
        (
            "Preperiod outcome-frequency baseline",
            "preperiod_outcome_frequency_baseline",
        ),
    ):
        item = strict_variants[key]
        metrics = item.get("metrics") or {}
        rows.append(
            {
                "model": label,
                "status": "EVALUATED"
                if item.get("matches_evaluated")
                else "UNAVAILABLE",
                "n": item.get("matches_evaluated"),
                "coverage": item.get("coverage"),
                "multiclass_brier": metrics.get("brier_score_multiclass"),
                "multiclass_log_loss": metrics.get("multiclass_log_loss"),
                "ece_10_bins": metrics.get(
                    "expected_calibration_error_10_bins_mean_one_vs_rest"
                ),
                "accuracy_secondary": metrics.get("accuracy_argmax"),
                "mean_max_probability": metrics.get("mean_max_probability_sharpness"),
            }
        )
    rows.append(
        {
            "model": "Frozen WM2026 approach",
            "status": "UNAVAILABLE_AS_FAIR_CAUSAL_BASELINE",
            "n": None,
            "coverage": None,
            "multiclass_brier": None,
            "multiclass_log_loss": None,
            "ece_10_bins": None,
            "accuracy_secondary": None,
            "mean_max_probability": None,
            "reason": "Point-in-time frozen snapshot inputs are not available; retrospective values are not fair causal evidence.",
        }
    )
    for item in native["comparison_table"]:
        rows.append(
            {
                "model": item["variant"],
                "status": "EVALUATED" if item["n"] else "UNAVAILABLE",
                **item,
            }
        )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
