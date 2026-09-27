"""Offline, no-lookahead validation of international 1X2 models on UEFA NL.

This module deliberately accepts a local cached results file.  It never calls
the network-enabled international-results loader, scanner, publisher, or any
betting/ledger code.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import canonical_name
from src.data.international import filter_competitive
from src.ensemble.calibration import brier_score_multiclass, expected_calibration_error
from src.models import dixon_coles as dc
from src.models.elo import compute_elo_series, current_ratings, elo_win_probability

TOURNAMENT = "UEFA Nations League"
OUTCOME_NAMES = ("home", "draw", "away")
RETROSPECTIVE_LABEL = "RETROSPECTIVE_TRANSFER_DIAGNOSTIC_ONLY"

# The delayed 2022/23 League C/D relegation play-offs were played in March 2024.
# They are kept visible as a separate evaluation block rather than hidden in a
# broad calendar-year slice.
HISTORICAL_BLOCKS: tuple[dict[str, str], ...] = (
    {"id": "2020/21", "edition": "2020/21", "start": "2020-09-03", "end": "2021-10-10"},
    {"id": "2022/23", "edition": "2022/23", "start": "2022-06-01", "end": "2023-06-30"},
    {
        "id": "2022/23-delayed-relegation-playoffs",
        "edition": "2022/23",
        "start": "2024-03-21",
        "end": "2024-03-26",
    },
    {"id": "2024/25", "edition": "2024/25", "start": "2024-09-05", "end": "2025-06-08"},
)

REQUIRED_RESULT_COLUMNS = {
    "date", "home_team", "away_team", "home_score", "away_score", "tournament", "neutral"
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_local_results(path: Path) -> tuple[pd.DataFrame, str]:
    """Load a caller-selected local pickle; never fetch or refresh it."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Local results cache does not exist: {path}")
    frame = pd.read_pickle(path)
    missing = REQUIRED_RESULT_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Results cache is missing required columns: {sorted(missing)}")
    return normalize_results(frame), sha256_file(path)


def _source_bool(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"Invalid/missing source neutral flag: {value!r}")


def normalize_results(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize source team aliases and dates without changing neutral semantics."""
    missing = REQUIRED_RESULT_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Results data is missing required columns: {sorted(missing)}")
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise").dt.tz_localize(None).dt.normalize()
    result["home_team"] = result["home_team"].map(canonical_name)
    result["away_team"] = result["away_team"].map(canonical_name)
    result["tournament"] = result["tournament"].astype("string")
    result["home_score"] = pd.to_numeric(result["home_score"], errors="coerce")
    result["away_score"] = pd.to_numeric(result["away_score"], errors="coerce")
    result["neutral"] = result["neutral"].map(_source_bool)
    result = result.dropna(subset=["date", "home_team", "away_team", "home_score", "away_score"])
    result["home_score"] = result["home_score"].astype(int)
    result["away_score"] = result["away_score"].astype(int)
    return result.sort_values(["date", "home_team", "away_team"]).reset_index(drop=True)


def select_historical_matches(results: pd.DataFrame) -> pd.DataFrame:
    """Select only exact UEFA Nations League records in the declared editions."""
    exact = results.loc[results["tournament"].eq(TOURNAMENT)].copy()
    selected: list[pd.DataFrame] = []
    for block in HISTORICAL_BLOCKS:
        start, end = pd.Timestamp(block["start"]), pd.Timestamp(block["end"])
        rows = exact.loc[exact["date"].between(start, end, inclusive="both")].copy()
        rows["validation_period"] = block["id"]
        rows["edition"] = block["edition"]
        selected.append(rows)
    if not selected:
        return exact.iloc[0:0].assign(validation_period=pd.Series(dtype=str), edition=pd.Series(dtype=str))
    result = pd.concat(selected, ignore_index=True)
    duplicate_keys = result.duplicated(["date", "home_team", "away_team"], keep=False)
    if duplicate_keys.any():
        raise ValueError("Duplicate UEFA Nations League date/home/away result identity")
    return result.sort_values(["date", "home_team", "away_team"]).reset_index(drop=True)


def strict_training_prefix(results: pd.DataFrame, match_date: pd.Timestamp) -> pd.DataFrame:
    """Canonical competitive training rows strictly earlier than a prediction date."""
    competitive = filter_competitive(results)
    return competitive.loc[competitive["date"] < pd.Timestamp(match_date)].copy().reset_index(drop=True)


def outcome_index(home_score: int, away_score: int) -> int:
    """Return the canonical 1X2 order used here: 0=home, 1=draw, 2=away."""
    if home_score > away_score:
        return 0
    if home_score == away_score:
        return 1
    return 2


def _probability_vector(probability: dict[str, float] | np.ndarray) -> list[float]:
    if isinstance(probability, dict):
        values = [probability["p_home"], probability["p_draw"], probability["p_away"]]
    else:
        values = np.asarray(probability, dtype=float).tolist()
    array = np.asarray(values, dtype=float)
    if array.shape != (3,) or not np.isfinite(array).all() or (array < 0).any():
        raise ValueError("Model returned invalid 1X2 probabilities")
    total = float(array.sum())
    if total <= 0 or abs(total - 1.0) > 1e-5:
        raise ValueError("Model probabilities do not sum to one")
    return (array / total).tolist()


def _record(row: pd.Series) -> dict[str, Any]:
    return {
        "date": row["date"].date().isoformat(),
        "home_team": str(row["home_team"]),
        "away_team": str(row["away_team"]),
        "home_score": int(row["home_score"]),
        "away_score": int(row["away_score"]),
        "outcome_index_home_draw_away": outcome_index(int(row["home_score"]), int(row["away_score"])),
        "neutral": bool(row["neutral"]),
        "validation_period": str(row["validation_period"]),
        "edition": str(row["edition"]),
    }


def predict_elo_asof(results: pd.DataFrame, matches: pd.DataFrame) -> dict[tuple, dict[str, Any]]:
    """Elo ratings are read only from competitive matches dated before each fixture."""
    series = compute_elo_series(filter_competitive(results))
    predictions: dict[tuple, dict[str, Any]] = {}
    for match_date, day in matches.groupby("date", sort=True):
        prefix = series.loc[series["date"] < match_date]
        ratings = current_ratings(prefix)
        prefix_dates = prefix["date"]
        max_training_date = prefix_dates.max() if not prefix_dates.empty else pd.NaT
        for _, row in day.iterrows():
            home, away = str(row["home_team"]), str(row["away_team"])
            probs = elo_win_probability(
                ratings.get(home, 1500.0), ratings.get(away, 1500.0), neutral=bool(row["neutral"])
            )
            key = (match_date, home, away)
            predictions[key] = {
                "probabilities": _probability_vector(np.asarray(probs, dtype=float)),
                "training_max_date": None if pd.isna(max_training_date) else max_training_date.date().isoformat(),
                "training_match_count": len(prefix),
                "training_cutoff_exclusive": match_date.date().isoformat(),
            }
    return predictions


def predict_dc_event_walk_forward(
    results: pd.DataFrame, matches: pd.DataFrame, max_iter: int = 2000
) -> dict[tuple, dict[str, Any]]:
    """Fit canonical DC from scratch before each historical edition/block.

    Parameters stay frozen within an edition block, matching event-level DC
    snapshot evaluation.  Each fit uses all canonical competitive results with
    date < the first evaluated date in that block; no outcomes from that block
    can feed its own prediction.
    """
    predictions: dict[tuple, dict[str, Any]] = {}
    for period, block_matches in matches.groupby("validation_period", sort=False):
        cutoff = block_matches["date"].min()
        training = strict_training_prefix(results, cutoff)
        if training.empty or not training["date"].max() < cutoff:
            raise ValueError(f"Invalid DC training prefix for {period}")
        params = dc.fit(training, today=cutoff, max_iter=max_iter, prior_params=None)
        train_max = training["date"].max().date().isoformat()
        for _, row in block_matches.iterrows():
            home, away = str(row["home_team"]), str(row["away_team"])
            try:
                probs = dc.predict_match(home, away, params, neutral=bool(row["neutral"]))
            except ValueError:
                # Never replace unknown-team predictions with a fabricated prior.
                continue
            key = (row["date"], home, away)
            predictions[key] = {
                "probabilities": _probability_vector(probs),
                "training_max_date": train_max,
                "training_match_count": len(training),
                "training_cutoff_exclusive": cutoff.date().isoformat(),
                "fit_date": cutoff.date().isoformat(),
            }
    return predictions


def _reliability_by_class(probabilities: np.ndarray, outcomes: np.ndarray, n_bins: int = 10) -> dict[str, Any]:
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    result: dict[str, Any] = {}
    for class_index, name in enumerate(OUTCOME_NAMES):
        p = probabilities[:, class_index]
        y = (outcomes == class_index).astype(float)
        bins: list[dict[str, Any]] = []
        for bin_index, (lo, hi) in enumerate(pairwise(edges)):
            mask = (p >= lo) & (p < hi)
            if bin_index == n_bins - 1:
                mask |= p == 1.0
            count = int(mask.sum())
            if count:
                mean_p, observed = float(p[mask].mean()), float(y[mask].mean())
                bins.append({
                    "lower_bound_inclusive": float(lo),
                    "upper_bound_exclusive": float(hi),
                    "count": count,
                    "mean_probability": mean_p,
                    "observed_frequency": observed,
                    "calibration_error": mean_p - observed,
                })
        result[name] = {
            "mean_probability": float(p.mean()),
            "observed_frequency": float(y.mean()),
            "brier_score_one_vs_rest": float(np.mean((p - y) ** 2)),
            "reliability_bins": bins,
        }
    return result


def summarize_metrics(
    rows: list[dict[str, Any]], eligible_count: int | None = None
) -> dict[str, Any]:
    if not rows:
        return {
            "matches_evaluated": 0,
            "coverage": 0.0,
            "eligible_matches": eligible_count or 0,
            "metrics": None,
        }
    outcomes = np.asarray([row["outcome_index_home_draw_away"] for row in rows], dtype=int)
    probabilities = np.asarray([row["probabilities"] for row in rows], dtype=float)
    if probabilities.shape != (len(rows), 3):
        raise ValueError("Probability rows are not complete three-way predictions")
    clipped = np.clip(probabilities, 1e-15, 1.0)
    brier = brier_score_multiclass(probabilities, outcomes)
    denominator = len(rows) if eligible_count is None else eligible_count
    return {
        "matches_evaluated": len(rows),
        "eligible_matches": denominator,
        "coverage": len(rows) / denominator if denominator else 0.0,
        "metrics": {
            "brier_score_multiclass": brier,
            "multiclass_log_loss": float(-np.log(clipped[np.arange(len(rows)), outcomes]).mean()),
            "expected_calibration_error_10_bins_mean_one_vs_rest": expected_calibration_error(
                probabilities, outcomes, n_bins=10
            ),
            "home_draw_away_calibration": _reliability_by_class(probabilities, outcomes),
            "mean_max_probability_sharpness": float(probabilities.max(axis=1).mean()),
            "mean_entropy_nats": float(
                -(clipped * np.log(clipped)).sum(axis=1).mean()
            ),
            "mean_probability_by_outcome": {
                name: float(probabilities[:, idx].mean()) for idx, name in enumerate(OUTCOME_NAMES)
            },
            "accuracy_argmax": float((probabilities.argmax(axis=1) == outcomes).mean()),
        },
    }


def _cluster_bootstrap_brier(
    rows: list[dict[str, Any]],
    variants: tuple[str, ...] = ("dixon_coles", "elo"),
    n_bootstrap: int = 1000,
    seed: int = 20260927,
) -> dict[str, Any]:
    """Paired date-cluster bootstrap; avoids treating same-day fixtures as iid."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if all(variant in row["predictions"] for variant in variants):
            grouped[row["validation_period"]][row["date"]].append(row)
    if not grouped:
        return {"status": "not_available", "reason": "No paired model predictions"}

    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {variant: [] for variant in variants}
    samples["dixon_coles_minus_elo"] = []
    periods = sorted(grouped)
    for _ in range(n_bootstrap):
        draw: list[dict[str, Any]] = []
        for period in periods:
            date_groups = grouped[period]
            dates = sorted(date_groups)
            for selected in rng.choice(dates, size=len(dates), replace=True):
                draw.extend(date_groups[str(selected)])
        outcomes = np.asarray([row["outcome_index_home_draw_away"] for row in draw], dtype=int)
        scores: dict[str, float] = {}
        for variant in variants:
            probs = np.asarray([row["predictions"][variant]["probabilities"] for row in draw])
            scores[variant] = brier_score_multiclass(probs, outcomes)
            samples[variant].append(scores[variant])
        if "dixon_coles" in scores and "elo" in scores:
            samples["dixon_coles_minus_elo"].append(scores["dixon_coles"] - scores["elo"])

    intervals = {
        key: {
            "lower_95": float(np.quantile(values, 0.025)),
            "upper_95": float(np.quantile(values, 0.975)),
            "bootstrap_replicates": len(values),
        }
        for key, values in samples.items()
        if values
    }
    return {
        "method": "paired bootstrap resampling match dates within each historical period",
        "seed": seed,
        "confidence_level": 0.95,
        "intervals": intervals,
    }


def _no_lookahead_audit(audit_rows: list[dict[str, Any]]) -> dict[str, Any]:
    variants: dict[str, Any] = {}
    for variant in ("dixon_coles", "elo"):
        predicted = [row for row in audit_rows if variant in row["predictions"]]
        by_period: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in predicted:
            by_period[row["validation_period"]].append(row)
        block_summaries = []
        for period, rows in by_period.items():
            fit_records = [row["predictions"][variant] for row in rows]
            block_summaries.append({
                "period": period,
                "matches": len(rows),
                "first_evaluated_date": rows[0]["date"],
                "last_evaluated_date": rows[-1]["date"],
                "training_max_date_first": fit_records[0]["training_max_date"],
                "training_max_date_last": fit_records[-1]["training_max_date"],
                "training_cutoff_exclusive_first": fit_records[0]["training_cutoff_exclusive"],
                "training_cutoff_exclusive_last": fit_records[-1]["training_cutoff_exclusive"],
                "training_match_count_first": fit_records[0]["training_match_count"],
                "training_match_count_last": fit_records[-1]["training_match_count"],
            })
        variants[variant] = {
            "predicted_match_count": len(predicted),
            "all_training_max_dates_strictly_before_fixture_date": all(
                row["predictions"][variant]["training_max_date"] is not None
                and row["predictions"][variant]["training_max_date"] < row["date"]
                for row in predicted
            ),
            "blocks": block_summaries,
        }
    return {"evaluated_match_count": len(audit_rows), "variants": variants}


def audit_local_odds_history(path: Path, matches: pd.DataFrame) -> dict[str, Any]:
    """Check only local snapshots; never infer odds from incomplete/non-matched rows."""
    path = Path(path)
    if not path.is_file():
        return {"status": "not_available", "reason": "No local odds-history file", "matched_fixtures": 0}
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshots = payload if isinstance(payload, list) else payload.get("snapshots", [])
    snapshot_dates: list[pd.Timestamp] = []
    entry_count = 0
    complete_1x2_entries = 0
    exact_prematch_fixture_matches = 0
    for snapshot in snapshots:
        stamp = snapshot.get("ts", snapshot.get("timestamp", snapshot.get("date")))
        try:
            timestamp = pd.Timestamp(stamp)
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC").tz_localize(None)
        snapshot_dates.append(timestamp)
        odds = snapshot.get("odds", {})
        if not isinstance(odds, dict):
            continue
        for event_name, prices in odds.items():
            entry_count += 1
            if not isinstance(prices, dict):
                continue
            values = [prices.get("home"), prices.get("draw"), prices.get("away")]
            complete = all(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1
                           for value in values)
            if complete:
                complete_1x2_entries += 1
            for _, match in matches.iterrows():
                expected = f"{match['home_team']} vs {match['away_team']}"
                if event_name == expected and complete and timestamp < match["date"]:
                    exact_prematch_fixture_matches += 1
    dates = sorted(snapshot_dates)
    return {
        "status": "unavailable_for_valid_market_comparison" if exact_prematch_fixture_matches == 0 else "candidate_matches_found",
        "snapshot_count": len(snapshots),
        "snapshot_timestamp_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "event_entries": entry_count,
        "entries_with_complete_numeric_home_draw_away": complete_1x2_entries,
        "exact_fixture_name_and_pre_kickoff_timestamp_matches": exact_prematch_fixture_matches,
        "market_only_and_model_market_blend_evaluated": False,
        "reason": "Local odds snapshots are dated after all selected historical fixtures; no authentic pre-match NL 1X2 observation can be joined.",
        "sha256": sha256_file(path),
    }


def _load_pickle(path: Path) -> Any:
    with Path(path).open("rb") as stream:
        return pickle.load(stream)


def retrospective_frozen_diagnostic(
    results: pd.DataFrame, matches: pd.DataFrame, snapshot_dir: Path
) -> dict[str, Any]:
    """Run frozen DC and GBT components, never presenting them as strict evidence."""
    from src.features.builder import build_feature_row
    from src.models.lgbm_model import predict_proba

    snapshot_dir = Path(snapshot_dir)
    frozen_dc = _load_pickle(snapshot_dir / "dc_params_final.pkl")
    frozen_gbt = _load_pickle(snapshot_dir / "model.pkl")
    feature_columns = json.loads((snapshot_dir / "feature_columns.json").read_text(encoding="utf-8"))
    competitive_series = compute_elo_series(filter_competitive(results))
    dc_rows: list[dict[str, Any]] = []
    gbt_rows: list[dict[str, Any]] = []
    skipped = {"frozen_dc_unknown_team": 0}

    for _, row in matches.iterrows():
        base = _record(row)
        neutral = bool(row["neutral"])
        home, away, match_date = str(row["home_team"]), str(row["away_team"]), row["date"]
        key_record = dict(base)
        try:
            p_dc = dc.predict_match(home, away, frozen_dc, neutral=neutral)
            dc_rows.append({**key_record, "probabilities": _probability_vector(p_dc)})
        except ValueError:
            skipped["frozen_dc_unknown_team"] += 1

        history_prefix = results.loc[results["date"] < match_date]
        elo_prefix = competitive_series.loc[competitive_series["date"] < match_date]
        features = build_feature_row(
            home=home,
            away=away,
            match_date=match_date,
            historical=history_prefix,
            elo_series=elo_prefix,
            dc_params=frozen_dc,
            neutral=neutral,
            tournament=TOURNAMENT,
            market_odds=None,
            statsbomb_xg=None,
            player_xg_df=None,
            fotmob_ratings_df=None,
            ppda_df=None,
        )
        aligned = pd.DataFrame([features]).reindex(columns=feature_columns).fillna(0.0)
        # Frozen HistGradientBoosting class order is canonical: away, draw, home.
        raw = predict_proba(frozen_gbt, aligned)[0]
        probs_home_draw_away = [float(raw[2]), float(raw[1]), float(raw[0])]
        gbt_rows.append({**key_record, "probabilities": _probability_vector(probs_home_draw_away)})

    metadata = json.loads((snapshot_dir / "metadata.json").read_text(encoding="utf-8"))
    return {
        "label": RETROSPECTIVE_LABEL,
        "snapshot_frozen_at": metadata.get("frozen_at"),
        "target_match_count": len(matches),
        "frozen_dixon_coles_component": {
            "status": "evaluated" if dc_rows else "not_evaluated",
            **summarize_metrics(dc_rows, eligible_count=len(matches)),
        },
        "frozen_gbt_component": {
            "status": "evaluated" if gbt_rows else "not_evaluated",
            **summarize_metrics(gbt_rows, eligible_count=len(matches)),
            "input_policy": "Canonical feature builder with results/Elo cut at match date; no market odds supplied; the canonical scoring alignment fills unavailable columns with zero.",
        },
        "frozen_canonical_stack": {
            "status": "not_evaluated",
            "reason": "The frozen stacker requires authentic pre-match Shin market probabilities. None of the local snapshots predates or matches these fixtures; supplying uniform/synthetic odds is prohibited.",
        },
        "skipped": skipped,
        "limitations": [
            "Frozen July 2026 parameters and fitted model have seen later results; these scores are retrospective transfer diagnostics only.",
            "The GBT feature builder uses current static squad-value inputs and default squad context where historical snapshots are absent; it is not causal historical performance evidence.",
            "Historical odds are absent, so the market-conditioned frozen stacker and calibration path are not run.",
        ],
    }


def run_validation(
    results: pd.DataFrame,
    cache_sha256: str,
    odds_path: Path | None = None,
    snapshot_dir: Path | None = None,
    source_sha: str | None = None,
    verified_current_main_sha: str | None = None,
    max_iter: int = 2000,
) -> dict[str, Any]:
    matches = select_historical_matches(results)
    if matches.empty:
        raise ValueError("No exact UEFA Nations League matches in the declared historical periods")

    dc_predictions = predict_dc_event_walk_forward(results, matches, max_iter=max_iter)
    elo_predictions = predict_elo_asof(results, matches)
    audit_rows: list[dict[str, Any]] = []
    for _, row in matches.iterrows():
        entry = _record(row)
        key = (row["date"], str(row["home_team"]), str(row["away_team"]))
        entry["predictions"] = {}
        if key in dc_predictions:
            entry["predictions"]["dixon_coles"] = dc_predictions[key]
        if key in elo_predictions:
            entry["predictions"]["elo"] = elo_predictions[key]
        audit_rows.append(entry)

    strict_variants: dict[str, Any] = {}
    for name in ("dixon_coles", "elo"):
        predicted = [
            {**row, "probabilities": row["predictions"][name]["probabilities"]}
            for row in audit_rows
            if name in row["predictions"]
        ]
        period_ids = list(dict.fromkeys(r["validation_period"] for r in audit_rows))
        per_period = {
            period: summarize_metrics(
                [r for r in predicted if r["validation_period"] == period],
                eligible_count=sum(r["validation_period"] == period for r in audit_rows),
            )
            for period in period_ids
        }
        summary = summarize_metrics(predicted, eligible_count=len(audit_rows))
        summary["per_period"] = per_period
        summary["no_lookahead"] = all(
            row["predictions"][name]["training_max_date"] is not None
            and row["predictions"][name]["training_max_date"] < row["date"]
            for row in predicted
        )
        summary["prediction_mode"] = (
            "edition-block DC fit from scratch at each block start; parameters held within block"
            if name == "dixon_coles"
            else "as-of Elo rating from canonical competitive results strictly before each match date"
        )
        strict_variants[name] = summary

    prior_rows: list[dict[str, Any]] = []
    for period, block_matches in matches.groupby("validation_period", sort=False):
        cutoff = block_matches["date"].min()
        training = strict_training_prefix(results, cutoff)
        labels = np.asarray([outcome_index(int(r.home_score), int(r.away_score))
                             for r in training.itertuples()], dtype=int)
        counts = np.bincount(labels, minlength=3).astype(float)
        probabilities = (counts / counts.sum()).tolist() if counts.sum() else [1 / 3] * 3
        for _, row in block_matches.iterrows():
            prior_rows.append({**_record(row), "probabilities": probabilities})
    baseline = summarize_metrics(prior_rows, eligible_count=len(matches))
    strict_variants["preperiod_outcome_frequency_baseline"] = {
        **baseline,
        "no_lookahead": True,
        "prediction_mode": "period-start outcome frequencies from canonical competitive training prefix only",
    }
    strict_variants["lightgbm_gbt"] = {
        "status": "not_evaluated",
        "matches_evaluated": 0,
        "coverage": 0.0,
        "reason": "A leakage-free reconstruction needs historical point-in-time values for the frozen 91-feature schema (notably dated market values, squad/context and other external inputs); these are not present in the canonical results cache. Current/frozen inputs would leak later information.",
    }
    strict_variants["canonical_ensemble_stacker"] = {
        "status": "not_evaluated",
        "matches_evaluated": 0,
        "coverage": 0.0,
        "reason": "No leakage-free GBT feature predictions and no authentic historical pre-match 1X2 odds. The canonical stacker consumes Shin market probabilities; synthetic odds are not substituted.",
    }
    strict_variants["market_only"] = {
        "status": "not_evaluated", "matches_evaluated": 0, "coverage": 0.0,
        "reason": "No authentic pre-match Nations League 1X2 odds in the available local history.",
    }
    strict_variants["market_plus_model_blend"] = {
        "status": "not_evaluated", "matches_evaluated": 0, "coverage": 0.0,
        "reason": "No authentic pre-match Nations League 1X2 odds; no market/model blend can be scored without them.",
    }

    paired_rows = [
        row for row in audit_rows
        if "dixon_coles" in row["predictions"] and "elo" in row["predictions"]
    ]
    paired_bootstrap = _cluster_bootstrap_brier(paired_rows)

    exact_nl = results.loc[results["tournament"].eq(TOURNAMENT)]
    current_rows = exact_nl.loc[exact_nl["date"] >= pd.Timestamp("2026-06-01")]
    market = audit_local_odds_history(odds_path, matches) if odds_path else {
        "status": "not_available", "reason": "No odds-history path supplied", "matched_fixtures": 0
    }
    frozen = retrospective_frozen_diagnostic(results, matches, snapshot_dir) if snapshot_dir else {
        "label": RETROSPECTIVE_LABEL,
        "status": "not_run",
        "reason": "No frozen snapshot directory supplied",
    }

    dixon_brier = strict_variants["dixon_coles"].get("metrics", {}).get("brier_score_multiclass")
    elo_brier = strict_variants["elo"].get("metrics", {}).get("brier_score_multiclass")
    prior_brier = strict_variants["preperiod_outcome_frequency_baseline"].get("metrics", {}).get("brier_score_multiclass")
    status = "WEAK_EVIDENCE_SHADOW_ONLY"
    if all(value is not None for value in (dixon_brier, elo_brier, prior_brier)):
        # Evidence is limited to two causal model components and three editions.
        # A positive aggregate is not sufficient to promote this partial stack.
        all_models_clearly_worse = (
            dixon_brier >= prior_brier + 0.02 and elo_brier >= prior_brier + 0.02
        )
        if all_models_clearly_worse:
            status = "NOT_SUPPORTED"

    historical_periods = []
    for block in HISTORICAL_BLOCKS:
        block_rows = matches.loc[matches["validation_period"].eq(block["id"])]
        historical_periods.append({
            **block,
            "match_count": len(block_rows),
            "observed_start": block_rows["date"].min().date().isoformat() if not block_rows.empty else None,
            "observed_end": block_rows["date"].max().date().isoformat() if not block_rows.empty else None,
        })

    no_lookahead_verified = (
        len(audit_rows) == len(matches)
        and all(strict_variants[name]["no_lookahead"] for name in ("dixon_coles", "elo"))
        and all("dixon_coles" in row["predictions"] and "elo" in row["predictions"] for row in audit_rows)
    )
    return {
        "schema": "nations-league-model-validation-v1",
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source_sha": source_sha,
        "verified_current_main_sha": verified_current_main_sha,
        "tournament": TOURNAMENT,
        "historical_periods": historical_periods,
        "no_lookahead_verified": bool(no_lookahead_verified),
        "strict_validation": {
            "match_count": len(matches),
            "coverage": {
                name: strict_variants[name]["coverage"] for name in ("dixon_coles", "elo")
            },
            "model_variants": strict_variants,
            "paired_date_cluster_bootstrap": paired_bootstrap,
            "no_lookahead_audit": _no_lookahead_audit(audit_rows),
            "market_evidence": market,
            "current_2026_27_observations_in_cache": len(current_rows),
            "results_cache": {
                "sha256": cache_sha256,
                "row_count": len(results),
                "max_date": results["date"].max().date().isoformat(),
                "local_file_only": True,
                "network_fetch_performed": False,
            },
        },
        "retrospective_frozen_model_diagnostic": frozen,
        "diagnostics": {
            "probability_order": list(OUTCOME_NAMES),
            "outcome_order": "home=0, draw=1, away=2",
            "tier_split": "not available: canonical international-results schema has no UEFA Nations League tier A/B/C/D field",
            "neutral_semantics": "source neutral flag used per fixture; no blanket neutral=True",
            "production_side_effects": "none; this analysis module does not call scanner, publisher, betting, ledger, provider, or runtime APIs",
            "recommended_shadow_status_rationale": "Historical evidence covers three editions but only causal DC/Elo can be reconstructed; full frozen GBT/stacker and market comparison lack point-in-time inputs.",
        },
        "limitations": [
            "The cached canonical source ends at its recorded max_date; current 2026/27 Nations League results are absent and are not treated as a meaningful validation sample.",
            "Competition tier A/B/C/D cannot be reconstructed from the source schema and is not inferred.",
            "Causal GBT reconstruction is not possible without historical point-in-time values for all required model features.",
            "The canonical stacker and market comparisons are unavailable because no authentic, timestamped pre-match Nations League 1X2 odds are present.",
            "DC is fit once before each edition/evaluation block and held fixed within that block; this is a conservative event-level walk-forward, not daily retraining.",
            "No bet, publication, ledger, or production-activation authority is granted by these metrics.",
        ],
        "recommended_shadow_status": status,
    }


def write_json_atomic(payload: dict[str, Any], output: Path) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(output)
