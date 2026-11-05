"""Offline research harness for a result-only, Nations League-native GBT.

This module deliberately does not discover data, access the network, inspect
runtime state, or write a production model.  Callers must provide an explicit
local results frame.  All feature state is advanced after a whole match date,
so no same-day result can affect a prediction made on that date.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.config import ELO_K_BASE, TEAM_CONFEDERATION, TOURNAMENT_K_FACTORS
from src.ensemble.calibration import brier_score_multiclass
from src.models.elo import (
    ELO_DEFAULT,
    ELO_K_COMPETITIVE,
    ELO_K_FRIENDLY,
    update_ratings,
)
from src.models.lgbm_model import predict_proba, train

from . import nations_league_validation as validation

FEATURE_COLUMNS = (
    "elo_home",
    "elo_away",
    "elo_home_advantage_adjusted_diff",
    "home_form_points",
    "away_form_points",
    "home_form_goal_difference",
    "away_form_goal_difference",
    "home_form_goals_for",
    "away_form_goals_for",
    "home_form_goals_against",
    "away_form_goals_against",
    "home_form_matches",
    "away_form_matches",
    "home_rest_days",
    "away_rest_days",
    "home_h2h_points",
    "h2h_matches",
    "neutral",
)

RESEARCH_GBT_PARAMS = {
    "max_iter": 100,
    "learning_rate": 0.05,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 12,
    "l2_regularization": 1.0,
    "random_state": 42,
    # HistGradientBoosting's default early stopping uses a random validation
    # split.  Disable it: this research uses only chronological block cutoffs.
    "early_stopping": False,
    "verbose": 0,
}
EVALUATION_HORIZON = max(
    pd.Timestamp(block["end"]) for block in validation.HISTORICAL_BLOCKS
)


@dataclass(frozen=True)
class WeightSpec:
    """Predeclared research hyperparameters; none are production weights."""

    name: str
    ablation: str
    uefa_competitive_weight: float
    friendly_weight: float
    recency_half_life_years: float | None


def weight_specs() -> list[WeightSpec]:
    """Return an explicit sensitivity grid, including each required ablation."""
    specs = [
        WeightSpec(f"nl_only_hl_{hl or 'none'}", "nl_only", 0.0, 0.0, hl)
        for hl in (None, 3.0, 6.0)
    ]
    for alpha in (0.25, 0.5, 1.0):
        specs.append(
            WeightSpec(
                f"nl_uefa_alpha_{alpha:g}_hl_none",
                "nl_plus_uefa_competitive",
                alpha,
                0.0,
                None,
            )
        )
    specs.extend(
        [
            WeightSpec(
                f"nl_uefa_alpha_0_5_hl_{hl:g}", "nl_plus_uefa_competitive", 0.5, 0.0, hl
            )
            for hl in (3.0, 6.0)
        ]
    )
    for alpha in (0.25, 0.5, 1.0):
        specs.append(
            WeightSpec(
                f"nl_uefa_friendly_alpha_{alpha:g}_friendly_rel_0_1_hl_none",
                "nl_uefa_competitive_plus_downweighted_friendlies",
                alpha,
                alpha * 0.1,
                None,
            )
        )
    specs.extend(
        [
            WeightSpec(
                f"nl_uefa_friendly_alpha_0_5_friendly_rel_0_1_hl_{hl:g}",
                "nl_uefa_competitive_plus_downweighted_friendlies",
                0.5,
                0.05,
                hl,
            )
            for hl in (3.0, 6.0)
        ]
    )
    return specs


def classify_training_match(row: Any) -> str:
    """Classify only known source identities; ambiguous global qualifiers drop out."""
    tournament = str(row["tournament"]).strip()
    if tournament == validation.TOURNAMENT:
        return "nations_league"
    if "friendly" in tournament.casefold():
        return "friendly"
    if tournament.startswith("UEFA ") and "Nations League" not in tournament:
        return "uefa_competitive"
    # The source labels FIFA World Cup fixtures/qualifiers globally.  Include
    # them only when both teams have a known UEFA confederation mapping.
    if tournament.startswith("FIFA World Cup"):
        home_confed = TEAM_CONFEDERATION.get(str(row["home_team"]))
        away_confed = TEAM_CONFEDERATION.get(str(row["away_team"]))
        if home_confed == away_confed == "UEFA":
            return "uefa_competitive"
    return "excluded_or_unclassified"


def restrict_to_evaluation_horizon(results: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Exclude all post-evaluation results before any model/feature processing."""
    if "date" not in results:
        raise ValueError("Results data is missing date")
    keep = pd.to_datetime(results["date"]) <= EVALUATION_HORIZON
    return results.loc[keep].copy().reset_index(drop=True), int((~keep).sum())


def _recency_weight(
    dates: pd.Series, cutoff: pd.Timestamp, half_life: float | None
) -> np.ndarray:
    if half_life is None:
        return np.ones(len(dates), dtype=float)
    age_days = (cutoff - pd.to_datetime(dates)).dt.total_seconds().to_numpy() / 86400.0
    if (age_days < 0).any():
        raise ValueError("Training row is at or after the exclusive cutoff")
    return np.exp2(-age_days / (365.2425 * half_life))


def training_weights(
    rows: pd.DataFrame, cutoff: pd.Timestamp, spec: WeightSpec
) -> np.ndarray:
    """Produce auditable per-row weights for one declared ablation/grid point."""
    if not rows.empty and not (rows["date"] < cutoff).all():
        raise ValueError(
            "Training data must be strictly earlier than prediction cutoff"
        )
    classes = rows.apply(classify_training_match, axis=1)
    category_weight = (
        classes.map(
            {
                "nations_league": 1.0,
                "uefa_competitive": spec.uefa_competitive_weight,
                "friendly": spec.friendly_weight,
            }
        )
        .fillna(0.0)
        .to_numpy(dtype=float)
    )
    return category_weight * _recency_weight(
        rows["date"], cutoff, spec.recency_half_life_years
    )


def _form(history: list[dict[str, Any]]) -> tuple[float, float, float, float, int]:
    recent = history[-5:][::-1]
    if not recent:
        return 0.0, 0.0, 0.0, 0.0, 0
    weights = np.asarray([0.85**idx for idx in range(len(recent))], dtype=float)
    weights /= weights.sum()
    return (
        float(np.dot(weights, [item["points"] for item in recent])),
        float(np.dot(weights, [item["gf"] - item["ga"] for item in recent])),
        float(np.dot(weights, [item["gf"] for item in recent])),
        float(np.dot(weights, [item["ga"] for item in recent])),
        len(recent),
    )


def causal_feature_frame(results: pd.DataFrame) -> pd.DataFrame:
    """Build result-only features, freezing all input state within each date."""
    required = validation.REQUIRED_RESULT_COLUMNS | {"native_row_id"}
    missing = required - set(results.columns)
    if missing:
        raise ValueError(f"Results data is missing columns: {sorted(missing)}")
    ordered = results.sort_values(["date", "native_row_id"]).copy()
    ratings: dict[str, float] = {}
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    head_to_head: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
    feature_rows: dict[int, dict[str, float]] = {}

    for match_date, day in ordered.groupby("date", sort=True):
        date = pd.Timestamp(match_date)
        # Every fixture on a calendar date sees identical pre-date team state.
        for row in day.itertuples(index=False):
            home, away = str(row.home_team), str(row.away_team)
            home_form = _form(history[home])
            away_form = _form(history[away])
            pair = head_to_head[frozenset((home, away))]
            h2h_recent = pair[-5:][::-1]
            h2h_weights = np.asarray(
                [0.85**idx for idx in range(len(h2h_recent))], dtype=float
            )
            h2h_points = 0.0
            if h2h_recent:
                h2h_weights /= h2h_weights.sum()
                h2h_points = float(
                    np.dot(
                        h2h_weights,
                        [
                            item["home_points"]
                            if item["home"] == home
                            else item["away_points"]
                            for item in h2h_recent
                        ],
                    )
                )
            home_rest = (date - history[home][-1]["date"]).days if history[home] else 90
            away_rest = (date - history[away][-1]["date"]).days if history[away] else 90
            home_elo, away_elo = (
                ratings.get(home, ELO_DEFAULT),
                ratings.get(away, ELO_DEFAULT),
            )
            feature_rows[int(row.native_row_id)] = {
                "elo_home": home_elo,
                "elo_away": away_elo,
                "elo_home_advantage_adjusted_diff": home_elo
                - away_elo
                + (0.0 if bool(row.neutral) else 100.0),
                "home_form_points": home_form[0],
                "away_form_points": away_form[0],
                "home_form_goal_difference": home_form[1],
                "away_form_goal_difference": away_form[1],
                "home_form_goals_for": home_form[2],
                "away_form_goals_for": away_form[2],
                "home_form_goals_against": home_form[3],
                "away_form_goals_against": away_form[3],
                "home_form_matches": float(home_form[4]),
                "away_form_matches": float(away_form[4]),
                "home_rest_days": float(home_rest),
                "away_rest_days": float(away_rest),
                "home_h2h_points": h2h_points,
                "h2h_matches": float(len(h2h_recent)),
                "neutral": float(bool(row.neutral)),
            }

        # Apply same-day outcomes only after every same-day feature has been
        # built. Elo deltas are calculated from the shared pre-date ratings.
        deltas: dict[str, float] = defaultdict(float)
        base_ratings = dict(ratings)
        for row in day.itertuples(index=False):
            home, away = str(row.home_team), str(row.away_team)
            tournament = str(row.tournament)
            if "friendly" in tournament.casefold():
                k = ELO_K_FRIENDLY
            elif tournament in TOURNAMENT_K_FACTORS:
                k = ELO_K_BASE * TOURNAMENT_K_FACTORS[tournament]
            else:
                k = ELO_K_COMPETITIVE
            after = update_ratings(
                base_ratings,
                home,
                away,
                int(row.home_score),
                int(row.away_score),
                k=k,
                neutral=bool(row.neutral),
            )
            deltas[home] += after[home] - base_ratings.get(home, ELO_DEFAULT)
            deltas[away] += after[away] - base_ratings.get(away, ELO_DEFAULT)
            hg, ag = int(row.home_score), int(row.away_score)
            history[home].append(
                {
                    "date": date,
                    "gf": hg,
                    "ga": ag,
                    "points": 3.0 if hg > ag else (1.0 if hg == ag else 0.0),
                }
            )
            history[away].append(
                {
                    "date": date,
                    "gf": ag,
                    "ga": hg,
                    "points": 3.0 if ag > hg else (1.0 if hg == ag else 0.0),
                }
            )
            head_to_head[frozenset((home, away))].append(
                {
                    "home": home,
                    "away": away,
                    "home_points": 3.0 if hg > ag else (1.0 if hg == ag else 0.0),
                    "away_points": 3.0 if ag > hg else (1.0 if hg == ag else 0.0),
                }
            )
        for team, delta in deltas.items():
            ratings[team] = base_ratings.get(team, ELO_DEFAULT) + delta

    return (
        pd.DataFrame.from_dict(feature_rows, orient="index")
        .reindex(columns=FEATURE_COLUMNS)
        .sort_index()
    )


def _outcome(row: Any) -> int:
    return validation.outcome_index(int(row.home_score), int(row.away_score))


def _gbt_probabilities(model: Any, features: pd.DataFrame) -> np.ndarray:
    """Convert existing GBT API order [away, draw, home] to audit order."""
    raw = predict_proba(model, features)
    by_class = {int(label): raw[:, index] for index, label in enumerate(model.classes_)}
    if set(by_class) != {0, 1, 2}:
        raise ValueError("Research GBT did not fit all three outcome classes")
    return np.column_stack((by_class[2], by_class[1], by_class[0]))


def paired_date_cluster_bootstrap(
    rows: list[dict[str, Any]],
    pairs: list[tuple[str, str]],
    *,
    n_bootstrap: int = 1000,
    seed: int = 20260929,
) -> dict[str, Any]:
    """Paired Brier-difference intervals; resample match dates inside blocks."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        if all(a in row["predictions"] and b in row["predictions"] for a, b in pairs):
            grouped[row["validation_period"]][row["date"]].append(row)
    if not grouped:
        return {"status": "not_available", "reason": "No paired date clusters"}
    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {f"{a}_minus_{b}": [] for a, b in pairs}
    for _ in range(n_bootstrap):
        draw: list[dict[str, Any]] = []
        for period in sorted(grouped):
            dates = sorted(grouped[period])
            for picked in rng.choice(dates, size=len(dates), replace=True):
                draw.extend(grouped[period][str(picked)])
        outcomes = np.asarray(
            [row["outcome_index_home_draw_away"] for row in draw], dtype=int
        )
        for a, b in pairs:
            pa = np.asarray(
                [row["predictions"][a]["probabilities"] for row in draw], dtype=float
            )
            pb = np.asarray(
                [row["predictions"][b]["probabilities"] for row in draw], dtype=float
            )
            samples[f"{a}_minus_{b}"].append(
                brier_score_multiclass(pa, outcomes)
                - brier_score_multiclass(pb, outcomes)
            )
    return {
        "status": "computed",
        "method": "paired resampling of match dates within each historical evaluation block",
        "seed": seed,
        "bootstrap_replicates": n_bootstrap,
        "confidence_level": 0.95,
        "brier_difference_intervals": {
            key: {
                "lower_95": float(np.quantile(vals, 0.025)),
                "upper_95": float(np.quantile(vals, 0.975)),
            }
            for key, vals in samples.items()
        },
    }


def run_native_research(results: pd.DataFrame) -> dict[str, Any]:
    """Evaluate predeclared weighted GBTs at each NL block's strict start cutoff."""
    if "native_row_id" not in results:
        results = results.copy().reset_index(drop=True)
        results["native_row_id"] = np.arange(len(results), dtype=int)
    results, _ = restrict_to_evaluation_horizon(results)
    features = causal_feature_frame(results)
    source = results.copy()
    source["source_class"] = source.apply(classify_training_match, axis=1)
    nl_matches = validation.select_historical_matches(source)
    if nl_matches.empty:
        raise ValueError("No exact Nations League evaluation rows in declared periods")

    specs = weight_specs()
    records_by_spec: dict[str, list[dict[str, Any]]] = {spec.name: [] for spec in specs}
    block_reports: dict[str, dict[str, Any]] = {}
    for period, block in nl_matches.groupby("validation_period", sort=False):
        cutoff = pd.Timestamp(block["date"].min())
        target_ids = block["native_row_id"].astype(int).to_numpy()
        eval_x = features.loc[target_ids, list(FEATURE_COLUMNS)]
        block_report: dict[str, Any] = {
            "cutoff_exclusive": cutoff.date().isoformat(),
            "target_matches": len(block),
            "variants": {},
        }
        eligible = source.loc[
            (source["date"] < cutoff)
            & source["source_class"].isin(
                ("nations_league", "uefa_competitive", "friendly")
            )
        ].copy()
        for spec in specs:
            weights = training_weights(eligible, cutoff, spec)
            selected = weights > 0
            training = eligible.loc[selected]
            train_x = features.loc[
                training["native_row_id"].astype(int), list(FEATURE_COLUMNS)
            ]
            labels = np.asarray(
                [
                    2
                    if int(row.home_score) > int(row.away_score)
                    else 1
                    if int(row.home_score) == int(row.away_score)
                    else 0
                    for row in training.itertuples()
                ],
                dtype=int,
            )
            if len(training) == 0 or set(labels.tolist()) != {0, 1, 2}:
                block_report["variants"][spec.name] = {
                    "status": "UNAVAILABLE",
                    "reason": "Strict training prefix lacks rows or all three outcome classes",
                    "training_match_count": len(training),
                }
                continue
            model = train(
                train_x.reset_index(drop=True),
                pd.Series(labels),
                params=RESEARCH_GBT_PARAMS,
                sample_weight=weights[selected],
            )
            probs = _gbt_probabilities(model, eval_x.reset_index(drop=True))
            evaluated: list[dict[str, Any]] = []
            for offset, (_, target) in enumerate(block.iterrows()):
                rec = {
                    "native_row_id": int(target["native_row_id"]),
                    "date": pd.Timestamp(target["date"]).date().isoformat(),
                    "validation_period": str(period),
                    "neutral": bool(target["neutral"]),
                    "outcome_index_home_draw_away": validation.outcome_index(
                        int(target["home_score"]), int(target["away_score"])
                    ),
                    "probabilities": probs[offset].tolist(),
                }
                evaluated.append(rec)
                records_by_spec[spec.name].append(rec)
            block_report["variants"][spec.name] = {
                "status": "evaluated",
                "training_match_count": len(training),
                "training_category_counts": {
                    key: int(training["source_class"].eq(key).sum())
                    for key in ("nations_league", "uefa_competitive", "friendly")
                },
                "training_max_date": pd.Timestamp(training["date"].max())
                .date()
                .isoformat(),
                "no_lookahead": bool((training["date"] < cutoff).all()),
                "metrics": validation.summarize_metrics(
                    evaluated, eligible_count=len(block)
                ),
            }
        block_reports[str(period)] = block_report

    variants: dict[str, Any] = {}
    comparison_table: list[dict[str, Any]] = []
    for spec in specs:
        rows = records_by_spec[spec.name]
        eligible_count = len(nl_matches)
        summary = validation.summarize_metrics(rows, eligible_count=eligible_count)
        summary.update(
            {
                "status": "evaluated" if rows else "UNAVAILABLE",
                "ablation": spec.ablation,
                "hyperparameters": asdict(spec),
                "no_random_split": True,
                "training_cutoff_policy": "new model fitted at each evaluation-block start; every training row strictly predates that block",
            }
        )
        variants[spec.name] = summary
        metrics = summary.get("metrics") or {}
        comparison_table.append(
            {
                "variant": spec.name,
                "ablation": spec.ablation,
                "n": summary["matches_evaluated"],
                "coverage": summary["coverage"],
                "multiclass_brier": metrics.get("brier_score_multiclass"),
                "multiclass_log_loss": metrics.get("multiclass_log_loss"),
                "ece_10_bins": metrics.get(
                    "expected_calibration_error_10_bins_mean_one_vs_rest"
                ),
                "accuracy_secondary": metrics.get("accuracy_argmax"),
                "mean_max_probability": metrics.get("mean_max_probability_sharpness"),
            }
        )

    strata: dict[str, dict[str, Any]] = {}
    for spec in specs:
        by_id = {int(row["native_row_id"]): row for row in records_by_spec[spec.name]}
        strata[spec.name] = {}
        for stratum, predicate in (
            ("neutral", lambda row: bool(row["neutral"])),
            ("non_neutral", lambda row: not bool(row["neutral"])),
            ("modern_since_2016", lambda row: row["date"] >= "2016-01-01"),
            ("modern_since_2020", lambda row: row["date"] >= "2020-01-01"),
        ):
            stratum_rows = [
                {
                    "outcome_index_home_draw_away": by_id[int(target["native_row_id"])][
                        "outcome_index_home_draw_away"
                    ],
                    "probabilities": by_id[int(target["native_row_id"])][
                        "probabilities"
                    ],
                }
                for _, target in nl_matches.iterrows()
                if int(target["native_row_id"]) in by_id
                and predicate(by_id[int(target["native_row_id"])])
            ]
            strata[spec.name][stratum] = validation.summarize_metrics(
                stratum_rows, eligible_count=len(stratum_rows)
            )

    dc_predictions = validation.predict_dc_event_walk_forward(results, nl_matches)
    elo_predictions = validation.predict_elo_asof(results, nl_matches)
    records_by_id = {
        spec.name: {
            int(row["native_row_id"]): row for row in records_by_spec[spec.name]
        }
        for spec in specs
    }
    prediction_rows: list[dict[str, Any]] = []
    for _, target in nl_matches.iterrows():
        key = (target["date"], str(target["home_team"]), str(target["away_team"]))
        predictions: dict[str, Any] = {}
        row_id = int(target["native_row_id"])
        for spec in specs:
            native = records_by_id[spec.name].get(row_id)
            if native is not None:
                predictions[spec.name] = {"probabilities": native["probabilities"]}
        if key in dc_predictions:
            predictions["dixon_coles"] = dc_predictions[key]
        if key in elo_predictions:
            predictions["elo"] = elo_predictions[key]
        prediction_rows.append(
            {
                "date": pd.Timestamp(target["date"]).date().isoformat(),
                "validation_period": str(target["validation_period"]),
                "outcome_index_home_draw_away": validation.outcome_index(
                    int(target["home_score"]), int(target["away_score"])
                ),
                "predictions": predictions,
            }
        )
    bootstrap = {
        spec.name: paired_date_cluster_bootstrap(
            prediction_rows,
            [(spec.name, "dixon_coles"), (spec.name, "elo")],
            n_bootstrap=1000,
        )
        for spec in specs
    }
    return {
        "status": "evaluated",
        "feature_provenance": {
            "source": "past international match results only",
            "fields": list(FEATURE_COLUMNS),
            "form_recency": "existing SportsBrain form decay default 0.85 across last five prior results",
            "current_rosters_market_odds_player_value_live_scores": "not used; point-in-time histories unavailable",
            "same_day_information": "features for all fixtures on a date are frozen before any same-day result is applied",
        },
        "source_dataset_class_counts": {
            name: int(source["source_class"].eq(name).sum())
            for name in (
                "nations_league",
                "uefa_competitive",
                "friendly",
                "excluded_or_unclassified",
            )
        },
        "evaluation_match_count": len(nl_matches),
        "evaluation_blocks": block_reports,
        "variants": variants,
        "comparison_table": comparison_table,
        "stratified_metrics": strata,
        "additional_strata_availability": {
            "league_A_B_C_D": "UNAVAILABLE: no UEFA tier field in canonical historical results",
            "favorite_balanced_underdog": "UNAVAILABLE without authentic historical pre-match odds; no synthetic labels used",
        },
        "paired_date_cluster_bootstrap": bootstrap,
        "limitations": [
            "UEFA tier A/B/C/D is absent in the canonical international-results schema.",
            "Market favorites/underdogs and market-conditioned model comparisons require authentic historical 1X2 odds and remain unavailable when those odds are absent.",
            "Other confederation-ambiguous global competitions are excluded rather than assigned to UEFA.",
            "The native result-derived GBT is a research candidate only; this harness does not save a model snapshot.",
        ],
    }
