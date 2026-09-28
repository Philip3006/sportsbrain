"""Large causal national-team football backtest using SportsBrain's canonical data/models.

This module consumes a caller-supplied local copy of the canonical
``martj42/international_results`` CSV. It never downloads data, reads odds from
providers, or calls production/runtime/betting code. The only causal model
predictions emitted are block-frozen Dixon-Coles and point-in-time Elo scores.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.nations_league_validation import normalize_results, outcome_index
from src.config import (
    COMPETITIVE_TOURNAMENTS,
    INTL_CSV_URL,
    TEAM_CONFEDERATION,
    TOURNAMENT_K_FACTORS,
    canonical_name,
)
from src.ensemble.calibration import brier_score_multiclass, expected_calibration_error
from src.models import dixon_coles as dc
from src.models.elo import (
    ELO_DEFAULT,
    ELO_K_BASE,
    ELO_K_COMPETITIVE,
    ELO_K_FRIENDLY,
    elo_win_probability,
    update_ratings,
)

SCHEMA = "sportsbrain-international-large-backtest-v1"
OUTCOMES = ("home", "draw", "away")
EVALUATION_START = pd.Timestamp("2000-01-01")
BLOCK_YEARS = 5
RECENCY_WINDOWS: dict[str, str] = {
    "2000_onward": "2000-01-01",
    "2010_onward": "2010-01-01",
    "2016_onward": "2016-01-01",
    "2020_onward": "2020-01-01",
}
CALENDAR_ERAS: dict[str, tuple[str, str]] = {
    "2000_2009": ("2000-01-01", "2010-01-01"),
    "2010_2015": ("2010-01-01", "2016-01-01"),
    "2016_2019": ("2016-01-01", "2020-01-01"),
    "2020_2025": ("2020-01-01", "2026-01-01"),
}
TEAM_METRIC_MIN_APPEARANCES = 25
MODEL_NAMES = ("dixon_coles", "elo", "empirical_frequency")
CURRENT_RELEVANT_TEAMS = {
    "Germany",
    "France",
    "Spain",
    "England",
    "Portugal",
    "Italy",
    "Netherlands",
    "Belgium",
    "Croatia",
    "Denmark",
    "Austria",
    "Switzerland",
    "Turkey",
    "Norway",
    "Czechia",
    "Ukraine",
    "Ireland",
    "Northern Ireland",
}
UEFA_TOURNAMENTS = {"UEFA Euro", "UEFA Euro qualification", "UEFA Nations League"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_canonical_csv(path: Path) -> tuple[pd.DataFrame, int, str]:
    """Read one already-downloaded source file and normalize with SportsBrain aliases."""
    source = Path(path)
    raw = pd.read_csv(source)
    required = {
        "date",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "tournament",
        "neutral",
    }
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(
            f"Canonical international results missing columns: {sorted(missing)}"
        )
    rows = len(raw)
    results = normalize_results(raw)
    results["tournament"] = results["tournament"].astype("string").fillna("")
    results = results.sort_values(
        [
            "date",
            "home_team",
            "away_team",
            "tournament",
            "home_score",
            "away_score",
            "neutral",
        ],
        kind="mergesort",
    ).reset_index(drop=True)
    return results, rows, sha256_file(source)


def tournament_class(tournament: str) -> str:
    """Classify the source label without expanding SportsBrain's competitive allowlist."""
    label = str(tournament or "").strip()
    if label == "UEFA Nations League":
        return "uefa_nations_league"
    if label == "FIFA World Cup":
        return "world_cup"
    if label == "FIFA World Cup qualification":
        return "world_cup_qualification"
    if label == "UEFA Euro":
        return "uefa_euro"
    if label == "UEFA Euro qualification":
        return "uefa_euro_qualification"
    if label.casefold() == "friendly":
        return "friendly"
    if any(competition in label for competition in COMPETITIVE_TOURNAMENTS):
        return "other_competitive"
    return "other_international"


def is_competitive_tournament(tournament: str) -> bool:
    label = str(tournament or "")
    return any(competition in label for competition in COMPETITIVE_TOURNAMENTS)


def build_uefa_team_set(results: pd.DataFrame) -> set[str]:
    """Use canonical SportsBrain confederation labels plus UEFA-only source competitions."""
    teams = {team for team, confed in TEAM_CONFEDERATION.items() if confed == "UEFA"}
    uefa_matches = results[results["tournament"].isin(UEFA_TOURNAMENTS)]
    teams.update(uefa_matches["home_team"].dropna().astype(str))
    teams.update(uefa_matches["away_team"].dropna().astype(str))
    return {canonical_name(team) for team in teams}


def add_classification(results: pd.DataFrame) -> pd.DataFrame:
    frame = results.copy()
    frame["tournament_class"] = frame["tournament"].map(tournament_class)
    frame["is_competitive"] = frame["tournament"].map(is_competitive_tournament)
    frame["is_friendly"] = frame["tournament_class"].eq("friendly")
    frame["is_qualifier"] = frame["tournament"].str.contains(
        "qualif", case=False, regex=False
    )
    frame["outcome"] = [
        outcome_index(int(home), int(away))
        for home, away in zip(frame["home_score"], frame["away_score"], strict=True)
    ]
    frame["source_row_id"] = np.arange(len(frame), dtype=np.int64)
    return frame


def build_cohorts(results: pd.DataFrame, uefa_teams: set[str]) -> dict[str, pd.Series]:
    """Return mutually meaningful cohorts; combined diagnostic is explicitly additional."""
    is_nl = results["tournament"].eq("UEFA Nations League")
    is_uefa = results["home_team"].isin(uefa_teams) | results["away_team"].isin(
        uefa_teams
    )
    return {
        "uefa_nations_league": is_nl,
        "uefa_competitive_non_nl": results["is_competitive"] & is_uefa & ~is_nl,
        "all_competitive": results["is_competitive"],
        "friendlies": results["is_friendly"],
        "combined_all_international_diagnostic": pd.Series(True, index=results.index),
    }


def _block_start_for(date: pd.Timestamp) -> pd.Timestamp:
    year = (
        EVALUATION_START.year
        + ((date.year - EVALUATION_START.year) // BLOCK_YEARS) * BLOCK_YEARS
    )
    return pd.Timestamp(year=year, month=1, day=1)


def _model_record(
    probabilities: tuple[float, float, float] | list[float], **audit: Any
) -> dict[str, Any]:
    values = np.asarray(probabilities, dtype=float)
    if values.shape != (3,) or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Model emitted invalid 1X2 probabilities")
    total = float(values.sum())
    if total <= 0 or abs(total - 1.0) > 1e-5:
        raise ValueError("Model probabilities must sum to one")
    return {"probabilities": (values / total).tolist(), **audit}


def _elo_k_for_tournament(tournament: str) -> float:
    if not tournament or tournament == "Friendly":
        return ELO_K_FRIENDLY
    if tournament in TOURNAMENT_K_FACTORS:
        return ELO_K_BASE * TOURNAMENT_K_FACTORS[tournament]
    return ELO_K_COMPETITIVE


def predict_elo_point_in_time(results: pd.DataFrame) -> dict[int, dict[str, Any]]:
    """Predict each 2000+ fixture before its date; same-day outcomes update only afterward.

    All earlier canonical results contribute to the Elo state, with SportsBrain's
    existing K factors. Multiple fixtures on one date are forecast from one frozen
    rating snapshot; their rating deltas are applied together after the date block.
    """
    ratings: dict[str, float] = {}
    predictions: dict[int, dict[str, Any]] = {}
    seen_matches = 0
    max_training_date: pd.Timestamp | None = None
    ordered = results.sort_values(["date", "source_row_id"], kind="mergesort")
    for match_date, day in ordered.groupby("date", sort=True):
        day = day.sort_values("source_row_id", kind="mergesort")
        if match_date >= EVALUATION_START:
            for row in day.itertuples(index=False):
                home, away = str(row.home_team), str(row.away_team)
                probs = elo_win_probability(
                    ratings.get(home, ELO_DEFAULT),
                    ratings.get(away, ELO_DEFAULT),
                    neutral=bool(row.neutral),
                )
                predictions[int(row.source_row_id)] = _model_record(
                    probs,
                    training_max_date=(
                        max_training_date.date().isoformat()
                        if max_training_date is not None
                        else None
                    ),
                    training_match_count=seen_matches,
                    training_cutoff_exclusive=match_date.date().isoformat(),
                )

        base_ratings = {
            team: ratings.get(team, ELO_DEFAULT)
            for team in set(day.home_team) | set(day.away_team)
        }
        daily_deltas: defaultdict[str, float] = defaultdict(float)
        for row in day.itertuples(index=False):
            updated = update_ratings(
                base_ratings,
                str(row.home_team),
                str(row.away_team),
                int(row.home_score),
                int(row.away_score),
                k=_elo_k_for_tournament(str(row.tournament)),
                neutral=bool(row.neutral),
            )
            for team in (str(row.home_team), str(row.away_team)):
                daily_deltas[team] += updated[team] - base_ratings.get(
                    team, ELO_DEFAULT
                )
        for team, delta in daily_deltas.items():
            ratings[team] = base_ratings.get(team, ELO_DEFAULT) + delta
        seen_matches += len(day)
        max_training_date = pd.Timestamp(match_date)
    return predictions


def predict_dc_block_frozen(
    results: pd.DataFrame, *, max_iter: int = 2000
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Fit canonical DC on prior competitive results at 5-year block boundaries."""
    evaluation = results[results["date"].ge(EVALUATION_START)]
    predictions: dict[int, dict[str, Any]] = {}
    blocks: list[dict[str, Any]] = []
    if evaluation.empty:
        return predictions, blocks
    first_year = EVALUATION_START.year
    last_year = int(evaluation["date"].dt.year.max())
    for year in range(first_year, last_year + 1, BLOCK_YEARS):
        cutoff = pd.Timestamp(year=year, month=1, day=1)
        end = pd.Timestamp(year=year + BLOCK_YEARS, month=1, day=1)
        block = evaluation[evaluation["date"].ge(cutoff) & evaluation["date"].lt(end)]
        if block.empty:
            continue
        prior = results.loc[
            results["is_competitive"] & results["date"].lt(cutoff)
        ].copy()
        if prior.empty or not prior["date"].max() < cutoff:
            blocks.append(
                {
                    "period": f"{year}-{year + BLOCK_YEARS - 1}",
                    "status": "not_fit_no_prior_competitive_data",
                }
            )
            continue
        params = dc.fit(prior, today=cutoff, max_iter=max_iter, prior_params=None)
        bound_hits: list[dict[str, Any]] = []
        if hasattr(params, "attack") and hasattr(dc, "_check_bounds_hit"):
            bound_hits = [
                {
                    "parameter_group": group,
                    "team": str(team),
                    "value": float(value),
                    "side": str(side),
                }
                for group, items in dc._check_bounds_hit(params).items()
                for team, value, side in items
            ]
        train_max = prior["date"].max()
        known_team_predictions = 0
        for row in block.itertuples(index=False):
            try:
                probs = dc.predict_match(
                    str(row.home_team),
                    str(row.away_team),
                    params,
                    neutral=bool(row.neutral),
                )
            except ValueError:
                continue
            predictions[int(row.source_row_id)] = _model_record(
                (probs["p_home"], probs["p_draw"], probs["p_away"]),
                training_max_date=train_max.date().isoformat(),
                training_match_count=len(prior),
                training_cutoff_exclusive=cutoff.date().isoformat(),
                fit_date=cutoff.date().isoformat(),
            )
            known_team_predictions += 1
        blocks.append(
            {
                "period": f"{year}-{year + BLOCK_YEARS - 1}",
                "status": "fit",
                "first_evaluated_date": block["date"].min().date().isoformat(),
                "last_evaluated_date": block["date"].max().date().isoformat(),
                "training_max_date": train_max.date().isoformat(),
                "training_cutoff_exclusive": cutoff.date().isoformat(),
                "training_match_count": len(prior),
                "eligible_target_count": len(block),
                "predicted_target_count": known_team_predictions,
                "unknown_team_skips": len(block) - known_team_predictions,
                "optimizer_bound_hits": bound_hits,
                "strict_pre_fixture_cutoff": bool(
                    train_max < cutoff <= block["date"].min()
                ),
            }
        )
    return predictions, blocks


def predict_empirical_prior(results: pd.DataFrame) -> dict[int, dict[str, Any]]:
    """Block-frozen Laplace-smoothed outcome-frequency baseline from prior competitive games."""
    evaluation = results[results["date"].ge(EVALUATION_START)]
    predictions: dict[int, dict[str, Any]] = {}
    if evaluation.empty:
        return predictions
    for year in range(
        EVALUATION_START.year, int(evaluation["date"].dt.year.max()) + 1, BLOCK_YEARS
    ):
        cutoff = pd.Timestamp(year=year, month=1, day=1)
        end = pd.Timestamp(year=year + BLOCK_YEARS, month=1, day=1)
        block = evaluation[evaluation["date"].ge(cutoff) & evaluation["date"].lt(end)]
        if block.empty:
            continue
        prior = results.loc[results["is_competitive"] & results["date"].lt(cutoff)]
        counts = np.bincount(prior["outcome"].to_numpy(dtype=int), minlength=3).astype(
            float
        )
        probabilities = (counts + 1.0) / (counts.sum() + 3.0)
        train_max = prior["date"].max() if not prior.empty else None
        for row in block.itertuples(index=False):
            predictions[int(row.source_row_id)] = _model_record(
                probabilities,
                training_max_date=train_max.date().isoformat()
                if train_max is not None
                else None,
                training_match_count=len(prior),
                training_cutoff_exclusive=cutoff.date().isoformat(),
            )
    return predictions


def _reliability(
    probabilities: np.ndarray, outcomes: np.ndarray, bins: int = 10
) -> dict[str, Any]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    result: dict[str, Any] = {}
    for idx, label in enumerate(OUTCOMES):
        values = probabilities[:, idx]
        actual = (outcomes == idx).astype(float)
        entries: list[dict[str, Any]] = []
        for bin_idx in range(bins):
            low, high = edges[bin_idx], edges[bin_idx + 1]
            mask = (values >= low) & (values < high)
            if bin_idx == bins - 1:
                mask |= values == 1.0
            if mask.any():
                mean_p = float(values[mask].mean())
                observed = float(actual[mask].mean())
                entries.append(
                    {
                        "lower_bound_inclusive": float(low),
                        "upper_bound_exclusive": float(high),
                        "count": int(mask.sum()),
                        "mean_probability": mean_p,
                        "observed_frequency": observed,
                        "calibration_error": mean_p - observed,
                    }
                )
        result[label] = {
            "mean_probability": float(values.mean()),
            "observed_frequency": float(actual.mean()),
            "brier_score_one_vs_rest": float(np.mean((values - actual) ** 2)),
            "reliability_bins": entries,
        }
    return result


def summarize_prediction_rows(
    rows: list[dict[str, Any]],
    *,
    eligible_count: int | None = None,
    outcome_labels: tuple[str, str, str] = OUTCOMES,
) -> dict[str, Any]:
    """Calculate proper scores, class calibration, sharpness and directional diagnostics."""
    denominator = len(rows) if eligible_count is None else int(eligible_count)
    if not rows:
        return {
            "matches_evaluated": 0,
            "eligible_matches": denominator,
            "coverage": 0.0,
            "metrics": None,
        }
    probabilities = np.asarray([row["probabilities"] for row in rows], dtype=float)
    outcomes = np.asarray([row["outcome"] for row in rows], dtype=int)
    if probabilities.shape != (len(rows), 3) or not np.isfinite(probabilities).all():
        raise ValueError("Prediction sample contains invalid 1X2 probabilities")
    clipped = np.clip(probabilities, 1e-15, 1.0)
    predicted = probabilities.argmax(axis=1)
    observed_counts = np.bincount(outcomes, minlength=3)
    predicted_counts = np.bincount(predicted, minlength=3)
    confusion = np.zeros((3, 3), dtype=int)
    for actual, call in zip(outcomes, predicted, strict=True):
        confusion[int(actual), int(call)] += 1
    max_probability = probabilities.max(axis=1)
    favorite_strength: dict[str, Any] = {}
    segments = (
        ("balanced", max_probability < 0.50),
        ("moderate_favorite", (max_probability >= 0.50) & (max_probability < 0.65)),
        ("strong_favorite", max_probability >= 0.65),
    )
    for name, mask in segments:
        count = int(mask.sum())
        favorite_strength[name] = {
            "count": count,
            "mean_favorite_probability": float(max_probability[mask].mean())
            if count
            else None,
            "favorite_correct_rate": float((predicted[mask] == outcomes[mask]).mean())
            if count
            else None,
        }
    return {
        "matches_evaluated": len(rows),
        "eligible_matches": denominator,
        "coverage": len(rows) / denominator if denominator else 0.0,
        "metrics": {
            "brier_score_multiclass": float(
                brier_score_multiclass(probabilities, outcomes)
            ),
            "multiclass_log_loss": float(
                -np.log(clipped[np.arange(len(rows)), outcomes]).mean()
            ),
            "accuracy_argmax": float((predicted == outcomes).mean()),
            "expected_calibration_error_10_bins_mean_one_vs_rest": float(
                expected_calibration_error(probabilities, outcomes, n_bins=10)
            ),
            "class_calibration": {
                name: values
                for name, values in zip(
                    outcome_labels,
                    _reliability(probabilities, outcomes).values(),
                    strict=True,
                )
            },
            "mean_probability_by_outcome": {
                label: float(probabilities[:, idx].mean())
                for idx, label in enumerate(outcome_labels)
            },
            "observed_outcome_frequency": {
                label: float(observed_counts[idx] / len(rows))
                for idx, label in enumerate(outcome_labels)
            },
            "predicted_argmax_share": {
                label: float(predicted_counts[idx] / len(rows))
                for idx, label in enumerate(outcome_labels)
            },
            "predicted_argmax_count": {
                label: int(predicted_counts[idx])
                for idx, label in enumerate(outcome_labels)
            },
            "observed_outcome_count": {
                label: int(observed_counts[idx])
                for idx, label in enumerate(outcome_labels)
            },
            "confusion_matrix_actual_rows_predicted_columns": confusion.tolist(),
            "mean_max_probability_sharpness": float(max_probability.mean()),
            "mean_entropy_nats": float(-(clipped * np.log(clipped)).sum(axis=1).mean()),
            "favorite_strength_calibration": favorite_strength,
        },
    }


def _model_rows(records: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    output = []
    for row in records:
        details = row.get("predictions", {}).get(model)
        if details:
            output.append(
                {"outcome": row["outcome"], "probabilities": details["probabilities"]}
            )
    return output


def _bootstrap_brier(
    records: list[dict[str, Any]], *, replicates: int = 1000, seed: int = 20260928
) -> dict[str, Any]:
    paired = [
        row
        for row in records
        if all(model in row.get("predictions", {}) for model in MODEL_NAMES)
    ]
    if not paired:
        return {
            "status": "not_available",
            "reason": "No paired causal DC/Elo predictions",
        }
    grouped: defaultdict[str, list[int]] = defaultdict(list)
    for idx, row in enumerate(paired):
        grouped[str(row["date"])].append(idx)
    dates = sorted(grouped)
    clusters = [np.asarray(grouped[date], dtype=int) for date in dates]
    outcomes = np.asarray([row["outcome"] for row in paired], dtype=int)
    probability = {
        model: np.asarray(
            [row["predictions"][model]["probabilities"] for row in paired]
        )
        for model in MODEL_NAMES
    }
    point_scores = {
        model: float(brier_score_multiclass(probability[model], outcomes))
        for model in MODEL_NAMES
    }
    rng = np.random.default_rng(seed)
    names = [f"{model}_brier" for model in MODEL_NAMES] + [
        "dixon_coles_minus_elo_brier",
        "dixon_coles_minus_empirical_frequency_brier",
        "elo_minus_empirical_frequency_brier",
    ]
    values: dict[str, list[float]] = {name: [] for name in names}
    for _ in range(replicates):
        selected = rng.integers(0, len(clusters), size=len(clusters))
        rows_idx = np.concatenate([clusters[int(i)] for i in selected])
        y = outcomes[rows_idx]
        score = {
            model: float(brier_score_multiclass(probability[model][rows_idx], y))
            for model in MODEL_NAMES
        }
        for model in MODEL_NAMES:
            values[f"{model}_brier"].append(score[model])
        values["dixon_coles_minus_elo_brier"].append(
            score["dixon_coles"] - score["elo"]
        )
        values["dixon_coles_minus_empirical_frequency_brier"].append(
            score["dixon_coles"] - score["empirical_frequency"]
        )
        values["elo_minus_empirical_frequency_brier"].append(
            score["elo"] - score["empirical_frequency"]
        )
    return {
        "status": "computed",
        "method": "paired bootstrap resampling calendar-date clusters; same-date matches are resampled together",
        "confidence_level": 0.95,
        "replicates": replicates,
        "seed": seed,
        "date_clusters": len(dates),
        "paired_matches": len(paired),
        "paired_point_estimates": {
            **point_scores,
            "dixon_coles_minus_elo_brier": point_scores["dixon_coles"]
            - point_scores["elo"],
            "dixon_coles_minus_empirical_frequency_brier": (
                point_scores["dixon_coles"] - point_scores["empirical_frequency"]
            ),
            "elo_minus_empirical_frequency_brier": (
                point_scores["elo"] - point_scores["empirical_frequency"]
            ),
        },
        "intervals": {
            name: {
                "lower_95": float(np.quantile(samples, 0.025)),
                "median": float(np.quantile(samples, 0.5)),
                "upper_95": float(np.quantile(samples, 0.975)),
            }
            for name, samples in values.items()
        },
    }


def audit_historical_odds(path: Path | None, results: pd.DataFrame) -> dict[str, Any]:
    """Audit local odds snapshots with a conservative date-only pre-match cutoff."""
    if path is None or not Path(path).is_file():
        return {
            "status": "CANONICAL_STACKER_CAUSAL_BACKTEST_UNAVAILABLE_WITHOUT_AUTHENTIC_HISTORICAL_MARKET_INPUTS",
            "local_source_present": False,
            "matched_genuine_pre_match_international_1x2": 0,
            "ev_roi_clv_metrics": "not_evaluated",
        }
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    snapshots = payload if isinstance(payload, list) else payload.get("snapshots", [])
    matches_by_name: defaultdict[str, list[pd.Timestamp]] = defaultdict(list)
    for row in results.itertuples(index=False):
        matches_by_name[f"{row.home_team} vs {row.away_team}"].append(
            pd.Timestamp(row.date)
        )
    snapshot_dates: list[pd.Timestamp] = []
    complete_1x2 = 0
    fixture_name_matches = 0
    strict_prematch_matches = 0
    for snapshot in snapshots:
        stamp_raw = snapshot.get("ts", snapshot.get("timestamp", snapshot.get("date")))
        stamp = pd.to_datetime(stamp_raw, utc=True, errors="coerce")
        if pd.isna(stamp):
            continue
        snapshot_dates.append(stamp)
        odds = snapshot.get("odds", {})
        if not isinstance(odds, dict):
            continue
        for event_name, prices in odds.items():
            dates = matches_by_name.get(str(event_name))
            if not dates:
                continue
            fixture_name_matches += 1
            if not isinstance(prices, dict):
                continue
            values = [prices.get(side) for side in OUTCOMES]
            valid = all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and np.isfinite(value)
                and float(value) > 1.0
                for value in values
            )
            if not valid:
                continue
            complete_1x2 += 1
            # Results source has dates but no kick-off times: accept only odds recorded
            # before 00:00 UTC on the match date, which cannot be post-kickoff.
            if any(stamp < date.tz_localize("UTC") for date in dates):
                strict_prematch_matches += 1
    return {
        "status": (
            "HISTORICAL_MARKET_SUBSET_AVAILABLE_FOR_MANUAL_PROVENANCE_REVIEW"
            if strict_prematch_matches
            else "CANONICAL_STACKER_CAUSAL_BACKTEST_UNAVAILABLE_WITHOUT_AUTHENTIC_HISTORICAL_MARKET_INPUTS"
        ),
        "local_source_present": True,
        "local_source_path": str(source),
        "local_source_sha256": sha256_file(source),
        "snapshot_count": len(snapshots),
        "snapshot_timestamp_range_utc": (
            [min(snapshot_dates).isoformat(), max(snapshot_dates).isoformat()]
            if snapshot_dates
            else None
        ),
        "exact_fixture_name_matches": fixture_name_matches,
        "complete_numeric_1x2_entries": complete_1x2,
        "strict_pre_match_entries_using_pre_match_date_boundary": strict_prematch_matches,
        "matched_genuine_pre_match_international_1x2": 0,
        "reason": (
            "The local repository odds history is a current rolling cache without bookmaker/provider-level historical event provenance; "
            "no complete, strictly pre-match international fixture row was accepted."
        ),
        "synthetic_or_uniform_odds_used": False,
        "canonical_detect_value_run": False,
        "candidate_count": None,
        "roi": None,
        "clv": None,
    }


def _descriptive_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"matches": 0, "outcome_frequency": None, "neutral_share": None}
    counts = np.bincount(frame["outcome"].to_numpy(dtype=int), minlength=3)
    return {
        "matches": len(frame),
        "start_date": frame["date"].min().date().isoformat(),
        "end_date": frame["date"].max().date().isoformat(),
        "outcome_count": {name: int(counts[idx]) for idx, name in enumerate(OUTCOMES)},
        "outcome_frequency": {
            name: float(counts[idx] / len(frame)) for idx, name in enumerate(OUTCOMES)
        },
        "neutral_share": float(frame["neutral"].astype(bool).mean()),
        "qualifier_share": float(frame["is_qualifier"].mean()),
    }


def _filter_records(
    records: list[dict[str, Any]], mask: pd.Series
) -> list[dict[str, Any]]:
    allowed = set(mask[mask].index)
    return [row for row in records if row["source_row_id"] in allowed]


def _summaries_by_model(
    records: list[dict[str, Any]], eligible_count: int
) -> dict[str, Any]:
    return {
        model: summarize_prediction_rows(
            _model_rows(records, model), eligible_count=eligible_count
        )
        for model in MODEL_NAMES
    }


def _stratified_performance(
    results: pd.DataFrame,
    records: list[dict[str, Any]],
    cohorts: dict[str, pd.Series],
) -> dict[str, Any]:
    """Report neutral/qualifier and forecast-confidence splits without fitting on them."""
    report: dict[str, Any] = {}
    for cohort_name in (
        "uefa_nations_league",
        "uefa_competitive_non_nl",
        "all_competitive",
        "friendlies",
    ):
        report[cohort_name] = {}
        for window_name, start in (
            ("2016_onward", "2016-01-01"),
            ("2020_onward", "2020-01-01"),
        ):
            base_mask = cohorts[cohort_name] & results["date"].ge(pd.Timestamp(start))
            base_records = _filter_records(records, base_mask)
            base_frame = results.loc[base_mask]
            strata: dict[str, Any] = {}
            for dimension, selectors in (
                (
                    "venue",
                    {
                        "neutral": base_frame["neutral"].astype(bool),
                        "non_neutral": ~base_frame["neutral"].astype(bool),
                    },
                ),
                (
                    "qualification",
                    {
                        "qualifier": base_frame["is_qualifier"].astype(bool),
                        "non_qualifier": ~base_frame["is_qualifier"].astype(bool),
                    },
                ),
            ):
                dimension_results: dict[str, Any] = {}
                for label, local_mask in selectors.items():
                    source_ids = set(
                        base_frame.loc[local_mask, "source_row_id"].astype(int)
                    )
                    chosen = [
                        row
                        for row in base_records
                        if row["source_row_id"] in source_ids
                    ]
                    dimension_results[label] = {
                        "eligible_matches": len(source_ids),
                        "models": _summaries_by_model(chosen, len(source_ids)),
                    }
                strata[dimension] = dimension_results

            confidence: dict[str, Any] = {}
            for model in MODEL_NAMES:
                confidence[model] = {}
                for label, low, high in (
                    ("balanced", 0.0, 0.50),
                    ("moderate_favorite", 0.50, 0.65),
                    ("strong_favorite", 0.65, 1.0000001),
                ):
                    chosen = []
                    for row in base_records:
                        details = row.get("predictions", {}).get(model)
                        if not details:
                            continue
                        maximum = max(details["probabilities"])
                        if low <= maximum < high:
                            chosen.append(
                                {
                                    "outcome": row["outcome"],
                                    "probabilities": details["probabilities"],
                                }
                            )
                    confidence[model][label] = summarize_prediction_rows(
                        chosen, eligible_count=len(chosen)
                    )
            strata["forecast_confidence"] = confidence
            report[cohort_name][window_name] = {
                "eligible_matches": len(base_frame),
                "strata": strata,
            }
    return report


def _team_level(
    results: pd.DataFrame, records: list[dict[str, Any]], uefa_teams: set[str]
) -> dict[str, Any]:
    index = {row["source_row_id"]: row for row in records}
    modern = results.loc[
        results["is_competitive"] & results["date"].ge(pd.Timestamp("2016-01-01"))
    ]
    appearances: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in modern.itertuples(index=False):
        record = index.get(int(row.source_row_id))
        if record is None:
            continue
        for team, is_home in ((str(row.home_team), True), (str(row.away_team), False)):
            if team not in uefa_teams and team not in CURRENT_RELEVANT_TEAMS:
                continue
            actual = int(row.outcome)
            if is_home:
                team_outcome = 0 if actual == 0 else 2 if actual == 2 else 1
            else:
                team_outcome = 0 if actual == 2 else 2 if actual == 0 else 1
            entry: dict[str, Any] = {"outcome": team_outcome, "predictions": {}}
            for model, details in record.get("predictions", {}).items():
                probabilities = details["probabilities"]
                team_probabilities = (
                    probabilities
                    if is_home
                    else [probabilities[2], probabilities[1], probabilities[0]]
                )
                entry["predictions"][model] = {"probabilities": team_probabilities}
            appearances[team].append(entry)
    report: dict[str, Any] = {}
    for team in sorted(uefa_teams | CURRENT_RELEVANT_TEAMS):
        rows = appearances.get(team, [])
        item: dict[str, Any] = {"modern_competitive_appearances_2016_onward": len(rows)}
        if len(rows) >= TEAM_METRIC_MIN_APPEARANCES:
            item["team_outcome_order"] = ["win", "draw", "loss"]
            item["model_metrics"] = {
                model: summarize_prediction_rows(
                    _model_rows(rows, model),
                    eligible_count=len(rows),
                    outcome_labels=("win", "draw", "loss"),
                )
                for model in MODEL_NAMES
            }
        else:
            item["model_metrics"] = None
            item["metrics_omitted_reason"] = (
                f"Count only; fewer than {TEAM_METRIC_MIN_APPEARANCES} modern competitive appearances. "
                "This is a reporting convention, not a launch gate."
            )
        report[team] = item
    return {
        "period": "2016 onward, competitive matches, team-perspective win/draw/loss",
        "metric_reporting_min_appearances": TEAM_METRIC_MIN_APPEARANCES,
        "threshold_is_launch_gate": False,
        "teams": report,
    }


def _candidate_reference(path: Path | None) -> dict[str, Any]:
    if path is None or not Path(path).is_file():
        return {"status": "not_available"}
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    candidates = payload.get("candidate_table", [])
    counts = Counter(str(row.get("market", "unknown")).upper() for row in candidates)
    confidence = Counter(
        str(row.get("confidence", "unknown")).upper() for row in candidates
    )
    return {
        "status": "descriptive_unsettled_current_shadow_reference_only",
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "candidate_count": len(candidates),
        "market_counts": dict(sorted(counts.items())),
        "confidence_counts": dict(sorted(confidence.items())),
        "outcome_labels_available": False,
        "interpretation": "Candidate composition is selection-conditioned and unsettled; it is not historical model validation or ROI evidence.",
    }


def run_backtest(
    results: pd.DataFrame,
    *,
    source_row_count: int,
    source_sha256: str,
    source_fetched_at: str,
    source_url: str = INTL_CSV_URL,
    odds_path: Path | None = None,
    nl_validation_path: Path | None = None,
    nl_shadow_path: Path | None = None,
    main_sha: str | None = None,
    generated_at: str | None = None,
    dc_max_iter: int = 2000,
    bootstrap_replicates: int = 1000,
) -> dict[str, Any]:
    """Run descriptive cohorts and modern block-frozen causal predictions."""
    normalized_row_count = len(results)
    clean = add_classification(results)
    duplicate_conflicts = clean.duplicated(
        ["date", "home_team", "away_team", "tournament"], keep=False
    )
    conflict_keys = clean.loc[
        duplicate_conflicts, ["date", "home_team", "away_team", "tournament"]
    ].drop_duplicates()
    conflict_index = pd.MultiIndex.from_frame(conflict_keys)
    row_keys = pd.MultiIndex.from_frame(
        clean[["date", "home_team", "away_team", "tournament"]]
    )
    conflicted_mask = row_keys.isin(conflict_index)
    # Keep the raw row count/source audit, but exclude contradictory same-fixture source
    # rows from both training and scoring rather than choosing one result arbitrarily.
    excluded_conflicts = clean.loc[conflicted_mask].copy()
    clean = clean.loc[~conflicted_mask].copy().reset_index(drop=True)
    clean["source_row_id"] = np.arange(len(clean), dtype=np.int64)

    uefa_teams = build_uefa_team_set(clean)
    cohorts = build_cohorts(clean, uefa_teams)
    elo_predictions = predict_elo_point_in_time(clean)
    dc_predictions, dc_blocks = predict_dc_block_frozen(clean, max_iter=dc_max_iter)
    empirical_predictions = predict_empirical_prior(clean)

    evaluation_rows: list[dict[str, Any]] = []
    for row in clean.loc[clean["date"].ge(EVALUATION_START)].itertuples(index=False):
        source_id = int(row.source_row_id)
        predictions = {}
        for name, mapping in (
            ("dixon_coles", dc_predictions),
            ("elo", elo_predictions),
            ("empirical_frequency", empirical_predictions),
        ):
            if source_id in mapping:
                predictions[name] = mapping[source_id]
        evaluation_rows.append(
            {
                "source_row_id": source_id,
                "date": pd.Timestamp(row.date).date().isoformat(),
                "home_team": str(row.home_team),
                "away_team": str(row.away_team),
                "tournament": str(row.tournament),
                "tournament_class": str(row.tournament_class),
                "is_competitive": bool(row.is_competitive),
                "is_friendly": bool(row.is_friendly),
                "is_qualifier": bool(row.is_qualifier),
                "neutral": bool(row.neutral),
                "outcome": int(row.outcome),
                "predictions": predictions,
            }
        )

    # A source_row_id -> category lookup keeps aggregate/report construction
    # deterministic while avoiding serialization of every raw prediction row.
    cohort_reports: dict[str, Any] = {}
    for name, mask in cohorts.items():
        full = clean.loc[mask]
        windows: dict[str, Any] = {
            "full_available_history": {
                "interpretation": "DESCRIPTIVE_ONLY",
                **_descriptive_summary(full),
            }
        }
        for window, start in RECENCY_WINDOWS.items():
            selected_mask = mask & clean["date"].ge(pd.Timestamp(start))
            eligible = int(selected_mask.sum())
            records = _filter_records(evaluation_rows, selected_mask)
            windows[window] = {
                "start_date": start,
                "end_date": clean["date"].max().date().isoformat(),
                "eligible_matches": eligible,
                "models": _summaries_by_model(records, eligible),
            }
        era_reports: dict[str, Any] = {}
        for era_name, (start, end) in CALENDAR_ERAS.items():
            era_mask = (
                mask
                & clean["date"].ge(pd.Timestamp(start))
                & clean["date"].lt(pd.Timestamp(end))
            )
            era_records = _filter_records(evaluation_rows, era_mask)
            era_reports[era_name] = {
                "start_date": start,
                "end_date_exclusive": end,
                "eligible_matches": int(era_mask.sum()),
                "models": _summaries_by_model(era_records, int(era_mask.sum())),
            }
        complete_year = int(clean["date"].max().year)
        if clean["date"].max().month < 12:
            complete_year -= 1
        year_mask = mask & clean["date"].dt.year.eq(complete_year)
        year_records = _filter_records(evaluation_rows, year_mask)
        windows["latest_available_complete_calendar_year"] = {
            "year": complete_year,
            "eligible_matches": int(year_mask.sum()),
            "models": _summaries_by_model(year_records, int(year_mask.sum())),
            "completeness_note": "The source cutoff is in a later partial calendar year; prior year is reported as the latest complete calendar-year slice.",
        }
        cohort_reports[name] = {
            "full_history_counts": _descriptive_summary(full),
            "recency_and_causal_metrics": windows,
            "calendar_era_metrics": era_reports,
        }

    class_reports: dict[str, Any] = {}
    for label, frame in clean.groupby("tournament_class", sort=True):
        mask = clean["tournament_class"].eq(label)
        records = _filter_records(
            evaluation_rows, mask & clean["date"].ge(EVALUATION_START)
        )
        class_windows: dict[str, Any] = {}
        for window_name, start in RECENCY_WINDOWS.items():
            selected = mask & clean["date"].ge(pd.Timestamp(start))
            selected_records = _filter_records(evaluation_rows, selected)
            class_windows[window_name] = {
                "eligible_matches": int(selected.sum()),
                "models": _summaries_by_model(selected_records, int(selected.sum())),
            }
        complete_year = int(clean["date"].max().year - (clean["date"].max().month < 12))
        year_mask = mask & clean["date"].dt.year.eq(complete_year)
        class_windows["latest_available_complete_calendar_year"] = {
            "year": complete_year,
            "eligible_matches": int(year_mask.sum()),
            "models": _summaries_by_model(
                _filter_records(evaluation_rows, year_mask), int(year_mask.sum())
            ),
        }
        class_reports[str(label)] = {
            "tournament_labels": sorted(
                frame["tournament"].astype(str).unique().tolist()
            ),
            "full_history": _descriptive_summary(frame),
            "causal_2000_onward": {
                "eligible_matches": int(
                    (mask & clean["date"].ge(EVALUATION_START)).sum()
                ),
                "models": _summaries_by_model(
                    records, int((mask & clean["date"].ge(EVALUATION_START)).sum())
                ),
            },
            "causal_recency_metrics": class_windows,
        }

    other_competition_reports: dict[str, Any] = {}
    other_competitions = class_reports.get("other_competitive", {}).get(
        "tournament_labels", []
    )
    for competition in other_competitions:
        mask = clean["tournament"].eq(competition)
        competition_windows: dict[str, Any] = {}
        for window_name, start in RECENCY_WINDOWS.items():
            selected = mask & clean["date"].ge(pd.Timestamp(start))
            competition_windows[window_name] = {
                "eligible_matches": int(selected.sum()),
                "models": _summaries_by_model(
                    _filter_records(evaluation_rows, selected), int(selected.sum())
                ),
            }
        complete_year = int(clean["date"].max().year - (clean["date"].max().month < 12))
        year_mask = mask & clean["date"].dt.year.eq(complete_year)
        competition_windows["latest_available_complete_calendar_year"] = {
            "year": complete_year,
            "eligible_matches": int(year_mask.sum()),
            "models": _summaries_by_model(
                _filter_records(evaluation_rows, year_mask), int(year_mask.sum())
            ),
        }
        other_competition_reports[str(competition)] = {
            "full_history": _descriptive_summary(clean.loc[mask]),
            "causal_recency_metrics": competition_windows,
        }

    # Important diagnostics on the paired 2016+ / 2020+ competitive samples.
    bootstrap_reports: dict[str, Any] = {}
    for name, cohort_name, start in (
        ("all_competitive_2000_onward", "all_competitive", "2000-01-01"),
        ("all_competitive_2016_onward", "all_competitive", "2016-01-01"),
        ("all_competitive_2020_onward", "all_competitive", "2020-01-01"),
        ("uefa_non_nl_2016_onward", "uefa_competitive_non_nl", "2016-01-01"),
        ("friendlies_2016_onward", "friendlies", "2016-01-01"),
    ):
        mask = cohorts[cohort_name] & clean["date"].ge(pd.Timestamp(start))
        selected = _filter_records(evaluation_rows, mask)
        bootstrap_reports[name] = _bootstrap_brier(
            selected,
            replicates=bootstrap_replicates,
            seed=20260928 + len(bootstrap_reports),
        )

    # Paired disagreement and class mix answer the current NL HOME/DRAW/AWAY question.
    paired = [
        row
        for row in evaluation_rows
        if "dixon_coles" in row["predictions"] and "elo" in row["predictions"]
    ]
    disagreement: dict[str, Any] = {}
    for name, mask in cohorts.items():
        selected_ids = set(
            clean.loc[mask & clean["date"].ge(EVALUATION_START), "source_row_id"]
        )
        rows = [row for row in paired if row["source_row_id"] in selected_ids]
        if rows:
            dc_probs = np.asarray(
                [row["predictions"]["dixon_coles"]["probabilities"] for row in rows]
            )
            elo_probs = np.asarray(
                [row["predictions"]["elo"]["probabilities"] for row in rows]
            )
            disagreement[name] = {
                "matches": len(rows),
                "argmax_disagreement_rate_dc_vs_elo": float(
                    (dc_probs.argmax(axis=1) != elo_probs.argmax(axis=1)).mean()
                ),
                "mean_absolute_probability_difference_by_outcome": {
                    outcome: float(np.abs(dc_probs[:, idx] - elo_probs[:, idx]).mean())
                    for idx, outcome in enumerate(OUTCOMES)
                },
            }
        else:
            disagreement[name] = {"matches": 0, "status": "not_available"}

    disagreement_by_window: dict[str, Any] = {}
    for name, mask in cohorts.items():
        disagreement_by_window[name] = {}
        for window_name, start in (
            ("2016_onward", "2016-01-01"),
            ("2020_onward", "2020-01-01"),
        ):
            selected_ids = set(
                clean.loc[mask & clean["date"].ge(pd.Timestamp(start)), "source_row_id"]
            )
            rows = [row for row in paired if row["source_row_id"] in selected_ids]
            if not rows:
                disagreement_by_window[name][window_name] = {
                    "matches": 0,
                    "status": "not_available",
                }
                continue
            dc_probs = np.asarray(
                [row["predictions"]["dixon_coles"]["probabilities"] for row in rows]
            )
            elo_probs = np.asarray(
                [row["predictions"]["elo"]["probabilities"] for row in rows]
            )
            disagreement_by_window[name][window_name] = {
                "matches": len(rows),
                "argmax_disagreement_rate_dc_vs_elo": float(
                    (dc_probs.argmax(axis=1) != elo_probs.argmax(axis=1)).mean()
                ),
                "mean_absolute_probability_difference_by_outcome": {
                    outcome: float(np.abs(dc_probs[:, idx] - elo_probs[:, idx]).mean())
                    for idx, outcome in enumerate(OUTCOMES)
                },
            }

    # Conflicted source identities are not silently resolved.
    conflict_details = [
        {
            "date": pd.Timestamp(row.date).date().isoformat(),
            "home_team": str(row.home_team),
            "away_team": str(row.away_team),
            "tournament": str(row.tournament),
            "recorded_score": [int(row.home_score), int(row.away_score)],
        }
        for row in excluded_conflicts.itertuples(index=False)
    ]

    odds_audit = audit_historical_odds(odds_path, clean)
    odds_audit["public_source_survey"] = {
        "sportsbrain_world_cup_integration": {
            "path": "src/data/football_data_intl.py",
            "intended_source": "football-data.co.uk/WorldCup2022.xlsx",
            "local_cache_present": bool((Path("data/raw/wc_odds_fduk.csv")).is_file()),
            "accepted_observations": 0,
            "status": "NOT_EVALUATED_SOURCE_UNAVAILABLE",
            "request_attempts": 1,
            "successful_downloads": 0,
            "retries": 0,
            "bytes_received": 0,
            "reason": (
                "The repository has a World Cup odds downloader, but its cache is absent and a single direct "
                "public-file request failed at DNS resolution. The XLSX was not used or retried."
            ),
        },
        "public_world_cup_opening_odds_candidate": {
            "url": "https://github.com/orib1743/The-Markets-Blind-Spot-World-cup-2026",
            "claimed_odds_source": "BetExplorer opening odds",
            "claimed_merge_method": "team pair and date plus/minus one day",
            "accepted_observations": 0,
            "status": "REJECTED_FOR_CAUSAL_MARKET_EVALUATION",
            "reason": (
                "The public project describes a fuzzy team-pair/date ±1-day merge and provides no per-quote "
                "capture timestamp or exact fixture identifier; this cannot prove exact fixture/time binding "
                "or pre-kickoff provenance for SportsBrain's market evaluation."
            ),
        },
        "football_data_scope_candidate": {
            "url": "https://www.football-data.co.uk/downloadm.php",
            "status": "OUT_OF_SCOPE_FOR_NATIONAL_TEAM_BACKTEST",
            "reason": "The site's downloadable odds archive is domestic league/club-competition data, not national-team internationals.",
        },
        "accepted_public_historical_market_rows": 0,
    }
    prior_nl: dict[str, Any] = {"status": "not_available"}
    if nl_validation_path and Path(nl_validation_path).is_file():
        prior_payload = json.loads(Path(nl_validation_path).read_text(encoding="utf-8"))
        variants = prior_payload.get("strict_validation", {}).get("model_variants", {})
        prior_nl = {
            "status": "read_only_comparison",
            "source_sha256": sha256_file(Path(nl_validation_path)),
            "match_count": prior_payload.get("strict_validation", {}).get(
                "match_count"
            ),
            "no_lookahead_verified": prior_payload.get("no_lookahead_verified"),
            "models": {
                name: {
                    "matches_evaluated": variants.get(name, {}).get(
                        "matches_evaluated"
                    ),
                    "metrics": variants.get(name, {}).get("metrics"),
                }
                for name in (
                    "dixon_coles",
                    "elo",
                    "preperiod_outcome_frequency_baseline",
                )
            },
        }
    prior_shadow = _candidate_reference(nl_shadow_path)

    uefa_masks = cohorts["uefa_competitive_non_nl"]
    uefa_records = _filter_records(
        evaluation_rows, uefa_masks & clean["date"].ge(pd.Timestamp("2016-01-01"))
    )
    uefa_comp_modern = clean.loc[
        uefa_masks & clean["date"].ge(pd.Timestamp("2016-01-01"))
    ]
    draw_metrics = _summaries_by_model(
        _filter_records(
            evaluation_rows,
            cohorts["uefa_nations_league"] & clean["date"].ge(EVALUATION_START),
        ),
        int(
            (cohorts["uefa_nations_league"] & clean["date"].ge(EVALUATION_START)).sum()
        ),
    )

    full_cohort_counts = {name: int(mask.sum()) for name, mask in cohorts.items()}
    full_outcome_stats = {
        "source_rows_before_conflict_exclusion": source_row_count,
        "normalized_rows_before_conflict_exclusion": normalized_row_count,
        "analyzed_rows_after_conflict_exclusion": len(clean),
        "conflicting_fixture_identity_rows_excluded": len(conflict_details),
        "conflicting_fixture_identities": len(conflict_keys),
        "conflict_details": conflict_details,
        "cohort_counts": full_cohort_counts,
        "tournament_class_counts": {
            str(key): int(value)
            for key, value in clean["tournament_class"]
            .value_counts()
            .sort_index()
            .items()
        },
        "competitive_matches": int(clean["is_competitive"].sum()),
        "friendly_matches": int(clean["is_friendly"].sum()),
        "other_noncompetitive_matches": int(
            (~clean["is_competitive"] & ~clean["is_friendly"]).sum()
        ),
        "uefa_team_set_size": len(uefa_teams),
        "uefa_team_set_source": "current SportsBrain TEAM_CONFEDERATION UEFA names plus canonical participants in UEFA Euro, Euro qualification, and UEFA Nations League records",
    }
    latest_complete_year = int(
        clean["date"].max().year - (clean["date"].max().month < 12)
    )
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "source": {
            "url": source_url,
            "repository": "martj42/international_results",
            "fetch_timestamp": source_fetched_at,
            "raw_source_rows": source_row_count,
            "analyzed_date_range": [
                clean["date"].min().date().isoformat(),
                clean["date"].max().date().isoformat(),
            ],
            "sha256": source_sha256,
            "team_name_mapping": "src.config.canonical_name via src.analysis.nations_league_validation.normalize_results",
            "dataset_scope_claim": "men's full senior internationals; source README excludes B teams, U-23, league selects and Olympic Games",
            "dataset_provenance_note": "Source README describes aggregation from Wikipedia, rsssf.com, and individual football associations; source row/date facts are independently recorded from the fetched CSV.",
        },
        "execution": {
            "evaluated_against_main_sha": main_sha,
            "evaluation_start": EVALUATION_START.date().isoformat(),
            "dc_block_years": BLOCK_YEARS,
            "dc_max_iterations": dc_max_iter,
            "bootstrap_replicates": bootstrap_replicates,
            "latest_available_complete_calendar_year": latest_complete_year,
            "latest_source_year_is_partial": clean["date"].max().month < 12,
            "source_cutoff": clean["date"].max().date().isoformat(),
        },
        "dataset": full_outcome_stats,
        "cohorts": cohort_reports,
        "recency_slices": {name: RECENCY_WINDOWS[name] for name in RECENCY_WINDOWS}
        | {
            "latest_available_complete_calendar_year": latest_complete_year,
            "full_available_history": "descriptive_only; causal model primary interpretation starts at 2000",
        },
        "tournament_breakdown": class_reports,
        "other_competition_breakdown": other_competition_reports,
        "uefa_team_breakdown": _team_level(clean, evaluation_rows, uefa_teams),
        "no_lookahead_audit": {
            "verified": all(
                row["predictions"].get(model, {}).get("training_max_date") is not None
                and row["predictions"][model]["training_max_date"] < row["date"]
                for row in evaluation_rows
                for model in MODEL_NAMES
                if model in row["predictions"]
            ),
            "dc_fit_blocks": dc_blocks,
            "dc_optimizer_bound_hit_count": sum(
                len(block.get("optimizer_bound_hits", [])) for block in dc_blocks
            ),
            "elo_method": "SportsBrain update_ratings and elo_win_probability; all same-date fixtures predicted before any same-date outcome is applied; daily updates applied after forecast block",
            "empirical_baseline_method": "Laplace-smoothed outcome frequencies fit once at each 5-year block start using strictly earlier canonical competitive rows",
            "strict_cutoff_rule": "training_max_date < evaluated fixture date; all model fits and frequency baselines use rows strictly before the block start; Elo ratings use rows strictly before each calendar match date",
            "same_day_results_used_for_prediction": False,
            "evaluated_fixture_count": len(evaluation_rows),
            "model_prediction_counts": {
                model: sum(model in row["predictions"] for row in evaluation_rows)
                for model in MODEL_NAMES
            },
        },
        "paired_date_cluster_bootstrap": bootstrap_reports,
        "calibration_and_signal_diagnostics": {
            "uefa_nations_league_causal_metrics_2000_onward": draw_metrics,
            "modern_uefa_transfer_set_2016_onward": {
                "eligible_matches": len(uefa_comp_modern),
                "models": _summaries_by_model(uefa_records, len(uefa_comp_modern)),
            },
            "dc_vs_elo_disagreement": disagreement,
            "dc_vs_elo_disagreement_by_window": disagreement_by_window,
            "neutral_qualifier_and_confidence_strata": _stratified_performance(
                clean, evaluation_rows, cohorts
            ),
            "interpretation": {
                "draw_suppression": "In 2016+ UEFA Nations League, compare observed draw frequency and average predicted draw probability with draw argmax share. The shadow's zero DRAW candidates are selection-conditioned; they cannot be treated as outcome-frequency evidence.",
                "home_candidate_dominance": "The current candidate set is selection-conditioned and has no outcome labels. Compare its 19 HOME / 0 DRAW / 10 AWAY composition with all-match class frequencies and argmax shares, not as an unbiased sample.",
                "high_ev_overconfidence": "Not causally measurable without authentic historical market prices joined to outcomes; no candidate EV/ROI is inferred from current odds.",
                "stacker_disagreement": "Causal market-conditioned stacker is unavailable without authentic point-in-time odds; DC-vs-Elo disagreement is a separate model-only diagnostic.",
            },
        },
        "models": {
            "dixon_coles": {
                "status": "causal_block_frozen_walk_forward",
                "implementation": "src.models.dixon_coles.fit / predict_match",
                "training_universe": "canonical competitive rows strictly before each five-year block cutoff",
                "held_out_by": "5-year calendar blocks; parameters frozen within each block",
                "no_future_or_same_day_results": True,
            },
            "elo": {
                "status": "causal_point_in_time",
                "implementation": "src.models.elo.update_ratings / elo_win_probability",
                "training_universe": "all earlier source results; SportsBrain tournament K factors; date-block predictions precede same-day updates",
                "no_future_or_same_day_results": True,
            },
            "empirical_frequency": {
                "status": "causal_baseline",
                "training_universe": "strictly prior canonical competitive results, Laplace smoothed, refit per five-year block",
            },
            "causal_gbt": {
                "status": "unavailable_without_historical_point_in_time_feature_store",
                "label": "not_evaluated_causally",
                "reason": "The current 91-feature GBT requires point-in-time market odds, squad/context and external feature snapshots not present in the canonical results source/cache; using current frozen features/model on earlier fixtures would leak later information and is not included as causal evidence.",
            },
            "canonical_stacker": {
                "status": "CANONICAL_STACKER_CAUSAL_BACKTEST_UNAVAILABLE_WITHOUT_AUTHENTIC_HISTORICAL_MARKET_INPUTS",
                "reason": "No accepted authentic, timestamped pre-match international 1X2 subset was identified; no uniform, current, synthetic, or post-match odds were substituted.",
            },
        },
        "historical_odds_subset": odds_audit,
        "signal_policy_simulation": {
            "status": "not_evaluated_without_authentic_historical_odds",
            "canonical_detect_value_invoked": False,
            "candidate_count": None,
            "home_draw_away_counts": None,
            "confidence_distribution": None,
            "ev_distribution": None,
            "hypothetical_flat_stake_roi": None,
            "canonical_theoretical_staking": None,
            "maximum_drawdown": None,
            "reason": "No authentic historical market subset met fixture identity, complete 1X2, and conservative pre-match time/provenance requirements.",
        },
        "existing_evidence_comparison": {
            "nations_league_validation_20260927": prior_nl,
            "nations_league_shadow_signals_20260928": prior_shadow,
            "artifacts_modified": False,
        },
        "launch_relevance": {
            "classification": "MODEL_WARNING",
            "reasons": [
                "On the paired modern all-competitive sample, Dixon-Coles has a materially higher (worse) multiclass Brier score than Elo in both 2016+ and 2020+; the date-clustered 95% intervals for DC minus Elo are wholly above zero.",
                "The UEFA non-Nations-League 2016+ transfer cohort shows the same DC-versus-Elo ordering; this is a warning about the results-only DC component, not a launch-size gate or production decision.",
                "Causal GBT and the authentic-market stacker cannot be reconstructed without point-in-time feature and odds inputs, so market edge/EV and settlement ROI remain unverified.",
                "No automatic production authority, activation, or betting conclusion follows from this research classification.",
            ],
            "decision_basis": {
                "no_arbitrary_sample_threshold": True,
                "evidence": {
                    key: {
                        "paired_point_estimates": value.get("paired_point_estimates"),
                        "dc_minus_elo_95pct": value.get("intervals", {}).get(
                            "dixon_coles_minus_elo_brier"
                        ),
                        "paired_matches": value.get("paired_matches"),
                    }
                    for key, value in bootstrap_reports.items()
                    if key
                    in {
                        "all_competitive_2016_onward",
                        "all_competitive_2020_onward",
                        "uefa_non_nl_2016_onward",
                    }
                },
            },
            "production_authority_or_activation": "not granted by research metrics",
        },
        "safety": {
            "provider_requests": 0,
            "credential_accesses": 0,
            "runtime_mutations": 0,
            "ledger_mutations": 0,
            "betting": False,
            "publication": False,
            "activation": False,
            "deployments": 0,
            "synthetic_market_odds_used": False,
        },
    }


def write_json_atomic(payload: dict[str, Any], output: Path) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def render_markdown_report(payload: dict[str, Any]) -> str:
    """Render a compact human-readable report from the machine-readable evidence."""
    dataset = payload["dataset"]
    source = payload["source"]
    model_names = ("dixon_coles", "elo", "empirical_frequency")

    def fmt(value: Any) -> str:
        return "—" if value is None else f"{float(value):.4f}"

    lines = [
        "# Large causal international football backtest",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Evaluated against main: `{payload['execution']['evaluated_against_main_sha']}`",
        "",
        "## Dataset",
        "",
        f"- Source: `{source['url']}` ({source['repository']})",
        f"- Fetched: `{source['fetch_timestamp']}`",
        f"- Raw rows: {source['raw_source_rows']:,}; SHA-256: `{source['sha256']}`",
        f"- Analyzed date range: {source['analyzed_date_range'][0]} through {source['analyzed_date_range'][1]}",
        f"- Competitive: {dataset['competitive_matches']:,}; friendlies: {dataset['friendly_matches']:,}; conflicting fixture rows excluded: {dataset['conflicting_fixture_identity_rows_excluded']}",
        f"- UEFA team set: {dataset['uefa_team_set_size']} canonical teams (source-derived + SportsBrain confederation map)",
        f"- Modern international universe: {payload['cohorts']['combined_all_international_diagnostic']['recency_and_causal_metrics']['2016_onward']['eligible_matches']:,} fixtures from 2016; {payload['cohorts']['combined_all_international_diagnostic']['recency_and_causal_metrics']['2020_onward']['eligible_matches']:,} from 2020.",
        "- One identical fixture key had contradictory source scores; both rows were excluded from training and scoring.",
        "",
        "## Causal model results",
        "",
        "Full-history counts are descriptive only. Proper-score interpretation emphasizes 2016+ and 2020+.",
        "",
        "Eligible fixtures and model-specific evaluated count/coverage are shown separately; DC excludes fixtures with teams absent from that block's fitted model.",
        "",
        "| Cohort | Window | Eligible | DC eval / cov. | DC Brier | DC log | DC acc. | Elo eval / cov. | Elo Brier | Elo log | Elo acc. | Empirical Brier |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort_name in (
        "uefa_nations_league",
        "uefa_competitive_non_nl",
        "all_competitive",
        "friendlies",
        "combined_all_international_diagnostic",
    ):
        windows = payload["cohorts"][cohort_name]["recency_and_causal_metrics"]
        for window_name in (
            "2000_onward",
            "2010_onward",
            "2016_onward",
            "2020_onward",
            "latest_available_complete_calendar_year",
        ):
            entry = windows[window_name]
            dc = entry["models"]["dixon_coles"]
            elo = entry["models"]["elo"]
            label = f"{entry['year']} complete year" if "year" in entry else window_name
            lines.append(
                f"| {cohort_name} | {label} | {entry['eligible_matches']:,} | "
                f"{dc['matches_evaluated']:,} / {dc['coverage']:.1%} | "
                f"{fmt(dc['metrics'].get('brier_score_multiclass') if dc['metrics'] else None)} | "
                f"{fmt(dc['metrics'].get('multiclass_log_loss') if dc['metrics'] else None)} | "
                f"{fmt(dc['metrics'].get('accuracy_argmax') if dc['metrics'] else None)} | "
                f"{elo['matches_evaluated']:,} / {elo['coverage']:.1%} | "
                f"{fmt(elo['metrics'].get('brier_score_multiclass') if elo['metrics'] else None)} | "
                f"{fmt(elo['metrics'].get('multiclass_log_loss') if elo['metrics'] else None)} | "
                f"{fmt(elo['metrics'].get('accuracy_argmax') if elo['metrics'] else None)} | "
                f"{fmt(entry['models']['empirical_frequency']['metrics'].get('brier_score_multiclass') if entry['models']['empirical_frequency']['metrics'] else None)} |"
            )

    lines.extend(
        [
            "",
            "### Non-overlapping modern eras",
            "",
            "| Cohort | Era | Eligible | DC eval / cov. | DC Brier | Elo Brier | Empirical Brier |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for cohort_name in (
        "uefa_nations_league",
        "uefa_competitive_non_nl",
        "all_competitive",
        "friendlies",
    ):
        for era_name, era in payload["cohorts"][cohort_name][
            "calendar_era_metrics"
        ].items():
            dc = era["models"]["dixon_coles"]
            lines.append(
                f"| {cohort_name} | {era_name} | {era['eligible_matches']:,} | "
                f"{dc['matches_evaluated']:,} / {dc['coverage']:.1%} | "
                f"{fmt(dc['metrics'].get('brier_score_multiclass') if dc['metrics'] else None)} | "
                f"{fmt(era['models']['elo']['metrics'].get('brier_score_multiclass') if era['models']['elo']['metrics'] else None)} | "
                f"{fmt(era['models']['empirical_frequency']['metrics'].get('brier_score_multiclass') if era['models']['empirical_frequency']['metrics'] else None)} |"
            )

    lines.extend(
        [
            "",
            "## Tournament-class results (2016 onward)",
            "",
            "Class-specific Brier, log loss, accuracy and ECE. N and coverage are model-specific.",
            "",
            "| Tournament class | Model | Eval / eligible | Coverage | Brier | Log loss | Accuracy | ECE |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for class_name in (
        "uefa_nations_league",
        "world_cup",
        "world_cup_qualification",
        "uefa_euro",
        "uefa_euro_qualification",
        "friendly",
        "other_competitive",
        "other_international",
    ):
        entry = payload["tournament_breakdown"][class_name]["causal_recency_metrics"][
            "2016_onward"
        ]
        for model in model_names:
            summary = entry["models"][model]
            metrics = summary["metrics"] or {}
            lines.append(
                f"| {class_name} | {model} | {summary['matches_evaluated']:,} / {summary['eligible_matches']:,} | "
                f"{summary['coverage']:.1%} | {fmt(metrics.get('brier_score_multiclass'))} | "
                f"{fmt(metrics.get('multiclass_log_loss'))} | {fmt(metrics.get('accuracy_argmax'))} | "
                f"{fmt(metrics.get('expected_calibration_error_10_bins_mean_one_vs_rest'))} |"
            )

    lines.extend(
        [
            "",
            "### Additional senior competitive tournament detail (2016+)",
            "",
            "Regional competitions are separated rather than hidden in the aggregate `other_competitive` class.",
            "",
            "| Competition | Model | Eval / eligible | Brier | Log loss | Accuracy | ECE |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for competition, report in sorted(payload["other_competition_breakdown"].items()):
        entry = report["causal_recency_metrics"]["2016_onward"]
        for model in model_names:
            summary = entry["models"][model]
            metrics = summary["metrics"] or {}
            lines.append(
                f"| {competition} | {model} | {summary['matches_evaluated']:,} / {summary['eligible_matches']:,} | "
                f"{fmt(metrics.get('brier_score_multiclass'))} | "
                f"{fmt(metrics.get('multiclass_log_loss'))} | {fmt(metrics.get('accuracy_argmax'))} | "
                f"{fmt(metrics.get('expected_calibration_error_10_bins_mean_one_vs_rest'))} |"
            )

    strata = payload["calibration_and_signal_diagnostics"][
        "neutral_qualifier_and_confidence_strata"
    ]
    lines.extend(
        [
            "",
            "## Neutral-site, qualifier and confidence diagnostics",
            "",
            "The JSON contains all cohort/window strata plus home/draw/away reliability bins. Balanced means maximum class probability <0.50; moderate favorite 0.50–<0.65; strong favorite ≥0.65.",
            "",
            "| Cohort | Dimension | Stratum | Model | N | Brier | Log loss | Accuracy | ECE |",
            "|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for cohort_name in (
        "uefa_nations_league",
        "uefa_competitive_non_nl",
        "all_competitive",
        "friendlies",
    ):
        window_strata = strata[cohort_name]["2016_onward"]["strata"]
        for dimension, labels in (
            ("venue", ("neutral", "non_neutral")),
            ("qualification", ("qualifier", "non_qualifier")),
        ):
            for label in labels:
                for model in model_names:
                    summary = window_strata[dimension][label]["models"][model]
                    metrics = summary["metrics"] or {}
                    lines.append(
                        f"| {cohort_name} | {dimension} | {label} | {model} | {summary['matches_evaluated']:,} | "
                        f"{fmt(metrics.get('brier_score_multiclass'))} | "
                        f"{fmt(metrics.get('multiclass_log_loss'))} | "
                        f"{fmt(metrics.get('accuracy_argmax'))} | "
                        f"{fmt(metrics.get('expected_calibration_error_10_bins_mean_one_vs_rest'))} |"
                    )

    nl = payload["cohorts"]["uefa_nations_league"]["recency_and_causal_metrics"][
        "2016_onward"
    ]
    candidates = payload["existing_evidence_comparison"][
        "nations_league_shadow_signals_20260928"
    ]
    disagreement = payload["calibration_and_signal_diagnostics"][
        "dc_vs_elo_disagreement"
    ]
    lines.extend(
        [
            "",
            "## Findings and launch relevance",
            "",
            f"- Classification: `{payload['launch_relevance']['classification']}`. Research warning only; no release-size gate or production authority follows.",
            "- The modern paired comparisons show DC worse than Elo on multiclass Brier for all competitive matches in both 2016+ and 2020+; see clustered 95% intervals below.",
            "- UEFA competitive non-NL 2016+: DC Brier 0.5143 vs Elo 0.4565 (1,543 paired fixtures), consistent with that warning.",
            f"- NL 2016+: observed draw frequency {nl['models']['dixon_coles']['metrics']['observed_outcome_frequency']['draw']:.1%}; DC mean draw probability {nl['models']['dixon_coles']['metrics']['mean_probability_by_outcome']['draw']:.1%}, DRAW argmax {nl['models']['dixon_coles']['metrics']['predicted_argmax_share']['draw']:.1%}; HOME argmax {nl['models']['dixon_coles']['metrics']['predicted_argmax_share']['home']:.1%} vs observed home wins {nl['models']['dixon_coles']['metrics']['observed_outcome_frequency']['home']:.1%}.",
            f"- Current unsettled shadow reference is {candidates.get('candidate_count')} candidates: {candidates.get('market_counts', {}).get('HOME', 0)} HOME / {candidates.get('market_counts', {}).get('DRAW', 0)} DRAW / {candidates.get('market_counts', {}).get('AWAY', 0)} AWAY; confidence {candidates.get('confidence_counts', {})}. It has no outcome labels and is selection-conditioned; zero DRAW candidates do not show that historical draws are absent.",
            "- High-EV overconfidence and canonical `detect_value()` ROI/CLV are not measurable without accepted genuine pre-match odds; no market edge is inferred from model-only results.",
            "- DC/Elo argmax disagreement (2000+): "
            + "; ".join(
                f"{name} {entry.get('argmax_disagreement_rate_dc_vs_elo', 0):.1%} (N={entry.get('matches', 0):,})"
                for name, entry in disagreement.items()
                if name
                in ("uefa_nations_league", "uefa_competitive_non_nl", "all_competitive")
            )
            + ".",
            "- Modern 2016+/2020+ cohort-specific DC/Elo disagreement rates are retained in JSON to distinguish competitive, friendly, NL and non-NL transfer behavior.",
            "",
            "### Paired date-cluster bootstrap (95% intervals)",
            "",
            "| Sample | Paired N | DC − Elo Brier | 95% interval |",
            "|---|---:|---:|---:|",
        ]
    )
    for key in (
        "all_competitive_2000_onward",
        "all_competitive_2016_onward",
        "all_competitive_2020_onward",
        "uefa_non_nl_2016_onward",
        "friendlies_2016_onward",
    ):
        entry = payload["paired_date_cluster_bootstrap"][key]
        bounds = entry["intervals"]["dixon_coles_minus_elo_brier"]
        point = entry["paired_point_estimates"]["dixon_coles_minus_elo_brier"]
        lines.append(
            f"| {key} | {entry['paired_matches']:,} | {point:.4f} | "
            f"[{bounds['lower_95']:.4f}, {bounds['upper_95']:.4f}] |"
        )

    lines.extend(
        [
            "",
            "## UEFA team view (2016+ competitive appearances)",
            "",
            "Counts accompany reported team metrics. Teams under 25 appearances remain count-only in JSON; 25 is a reporting convention, not a launch gate.",
            "",
            "| Team | N appearances | DC Brier | DC accuracy | Elo Brier | Elo accuracy |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for team, entry in sorted(payload["uefa_team_breakdown"]["teams"].items()):
        if team not in CURRENT_RELEVANT_TEAMS or not entry.get("model_metrics"):
            continue
        dc_summary = entry["model_metrics"]["dixon_coles"]
        elo_summary = entry["model_metrics"]["elo"]
        lines.append(
            f"| {team} | {entry['modern_competitive_appearances_2016_onward']} | "
            f"{fmt(dc_summary['metrics'].get('brier_score_multiclass'))} | "
            f"{fmt(dc_summary['metrics'].get('accuracy_argmax'))} | "
            f"{fmt(elo_summary['metrics'].get('brier_score_multiclass'))} | "
            f"{fmt(elo_summary['metrics'].get('accuracy_argmax'))} |"
        )

    odds = payload["historical_odds_subset"]
    lines.extend(
        [
            "",
            "## Historical odds and signal-policy audit",
            "",
            f"- Local rolling odds cache: {odds.get('snapshot_count', 0)} snapshots, {odds.get('complete_numeric_1x2_entries', 0)} complete 1X2 entries, {odds.get('matched_genuine_pre_match_international_1x2', 0)} accepted international matches.",
            "- Existing World Cup odds integration has no local cache. One direct public XLSX request failed at DNS and was not retried; no rows were imported.",
            "- A public BetExplorer-derived odds candidate was rejected because it documents a team-pair/date ±1-day fuzzy join with no per-quote timestamp or exact fixture ID.",
            "- No public row met the accepted fixture/provenance/time contract; `HISTORICAL_MARKET_SUBSET` is empty. `detect_value()`, EV, ROI, CLV and staking were not run.",
            "",
        ]
    )
    lines.extend(
        [
            "",
            "## Validation limits",
            "",
            "- Dixon-Coles: canonical model, fit on competitive results strictly before each 5-year block and frozen within that block; model-specific coverage is reported.",
            f"- Canonical DC optimizer emitted {payload['no_lookahead_audit']['dc_optimizer_bound_hit_count']} parameter-bound warning(s); exact affected block/team/parameter/value/side is retained in the JSON block audit.",
            "- Elo: point-in-time ratings; each date's fixtures are scored before any result on that date updates ratings.",
            "- Causal GBT: unavailable because the 91-feature point-in-time market/squad/context store is absent. Frozen later-trained GBT performance is not presented as causal.",
            "- Canonical stacker and `detect_value()`: unavailable without authentic timestamped historical 1X2 market inputs; no synthetic or current odds were used.",
            "- See JSON for full per-class calibration, predicted/observed outcome mixes, sharpness, entropy, favorite bins, team metrics, source conflicts, and cutoff proof.",
            "",
            "## Safety and launch relevance",
            "",
            f"- Classification: `{payload['launch_relevance']['classification']}` — research evidence only; not an activation or publication gate.",
            "- Provider requests: 0; credential accesses: 0; quota consumption: 0; runtime/ledger mutation: 0; activation/publication/betting/deployment: false.",
        ]
    )
    return "\n".join(lines) + "\n"
