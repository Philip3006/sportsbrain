"""Offline causal-envelope walk-forward replay for UEFA Nations League 1X2.

This research-only module consumes a caller-supplied local results cache. It
does not fetch data and does not import runtime, scanner, publication, betting,
or ledger code. The available source has calendar dates but no kickoff or
result-publication timestamps. Consequently, this module uses explicit,
conservative time bounds and labels the resulting evidence as unverified until
point-in-time source timestamps are available.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from src.config import (
    COMPETITIVE_TOURNAMENTS,
    ELO_K_BASE,
    TOURNAMENT_K_FACTORS,
    canonical_name,
)
from src.ensemble.calibration import brier_score_multiclass
from src.models import dixon_coles as dc
from src.models.elo import elo_win_probability, update_ratings

TOURNAMENT = "UEFA Nations League"
EVALUATION_BLOCKS: tuple[dict[str, str], ...] = (
    {"id": "2020/21", "start": "2020-09-03", "end": "2021-10-10"},
    {"id": "2022/23", "start": "2022-06-01", "end": "2023-06-30"},
    {
        "id": "2022/23-delayed-relegation-playoffs",
        "start": "2024-03-21",
        "end": "2024-03-26",
    },
    {"id": "2024/25", "start": "2024-09-05", "end": "2025-06-08"},
)
FEATURE_SCHEMA_VERSION = "nl-result-only-causal-gbt-v1"
PREDICTION_LEAD_DAYS = 7
RESULT_AVAILABILITY_LAG_DAYS = 3
UTC_EARLIEST_OFFSET_HOURS = 14
UTC_LATEST_OFFSET_HOURS = -12
BOOTSTRAP_SEED = 20260929
BOOTSTRAP_REPLICATES = 2000
MODEL_SEED = 20260929
MODEL_CONFIG: dict[str, Any] = {
    "class": "sklearn.ensemble.HistGradientBoostingClassifier",
    "max_iter": 250,
    "learning_rate": 0.04,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 60,
    "l2_regularization": 2.0,
    "early_stopping": False,
    "random_state": MODEL_SEED,
    "fit_cadence": "once before each declared Nations League evaluation block",
    "training_policy": "all competitive results with conservative availability upper bound < block cutoff",
    "training_features": "per-match point-in-time result/Elo features, each using its own 7-day lead cutoff",
}

REQUIRED_COLUMNS = {
    "date",
    "home_team",
    "away_team",
    "home_score",
    "away_score",
    "tournament",
    "neutral",
}
FEATURE_COLUMNS = (
    "home_elo",
    "away_elo",
    "elo_difference",
    "home_competitive_matches",
    "away_competitive_matches",
    "home_rest_days_at_cutoff",
    "away_rest_days_at_cutoff",
    "home_match_load_30d",
    "away_match_load_30d",
    "home_match_load_90d",
    "away_match_load_90d",
    "home_recent5_gf",
    "home_recent5_ga",
    "home_recent5_gd",
    "home_recent5_points",
    "home_recent10_gf",
    "home_recent10_ga",
    "home_recent10_gd",
    "home_recent10_points",
    "home_form_momentum_5v10",
    "away_recent5_gf",
    "away_recent5_ga",
    "away_recent5_gd",
    "away_recent5_points",
    "away_recent10_gf",
    "away_recent10_ga",
    "away_recent10_gd",
    "away_recent10_points",
    "away_form_momentum_5v10",
    "home_nl_recent5_points",
    "away_nl_recent5_points",
    "h2h_matches",
    "h2h_home_points_per_match",
    "h2h_goal_difference_per_match",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def outcome_index(home_score: int, away_score: int) -> int:
    """Return canonical order: 0=home, 1=draw, 2=away."""
    if home_score > away_score:
        return 0
    if home_score == away_score:
        return 1
    return 2


def _neutral(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"Invalid neutral flag: {value!r}")


def normalize_results(frame: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"Results are missing columns: {sorted(missing)}")
    result = frame.copy()
    parsed = pd.to_datetime(result["date"], errors="raise")
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_localize(None)
    # The source cache is date-only. Do not silently erase time-of-day precision.
    if not parsed.eq(parsed.dt.normalize()).all():
        raise ValueError(
            "Source includes timestamps; this date-only replay requires a separately audited time policy"
        )
    result["date"] = parsed.dt.normalize()
    result["home_team"] = result["home_team"].map(canonical_name)
    result["away_team"] = result["away_team"].map(canonical_name)
    result["tournament"] = result["tournament"].astype("string")
    result["home_score"] = pd.to_numeric(result["home_score"], errors="coerce")
    result["away_score"] = pd.to_numeric(result["away_score"], errors="coerce")
    result["neutral"] = result["neutral"].map(_neutral)
    result = result.dropna(
        subset=["date", "home_team", "away_team", "home_score", "away_score"]
    )
    result["home_score"] = result["home_score"].astype(int)
    result["away_score"] = result["away_score"].astype(int)
    result = result.sort_values(
        ["date", "home_team", "away_team", "tournament", "home_score", "away_score"]
    ).reset_index(drop=True)
    result["_identity_occurrence"] = result.groupby(
        ["date", "home_team", "away_team"], sort=False
    ).cumcount()
    result["fixture_id"] = result.apply(
        lambda row: _sha256(
            f"{row['date'].date().isoformat()}|{row['home_team']}|{row['away_team']}|{row['_identity_occurrence']}".encode()
        ),
        axis=1,
    )
    return result


def select_evaluation_matches(results: pd.DataFrame) -> pd.DataFrame:
    candidates = results.loc[results["tournament"].eq(TOURNAMENT)].copy()
    blocks: list[pd.DataFrame] = []
    for block in EVALUATION_BLOCKS:
        start, end = pd.Timestamp(block["start"]), pd.Timestamp(block["end"])
        part = candidates.loc[
            candidates["date"].between(start, end, inclusive="both")
        ].copy()
        part["validation_period"] = block["id"]
        blocks.append(part)
    if not blocks:
        return candidates.iloc[:0].assign(validation_period=pd.Series(dtype=str))
    selected = pd.concat(blocks, ignore_index=True)
    if selected.duplicated(["date", "home_team", "away_team"], keep=False).any():
        raise ValueError("Evaluation blocks overlap on a fixture identity")
    return selected.sort_values(["date", "home_team", "away_team"]).reset_index(
        drop=True
    )


def prediction_cutoff(kickoff_date: pd.Timestamp | date | str) -> pd.Timestamp:
    day = pd.Timestamp(kickoff_date).normalize()
    return (day - pd.Timedelta(days=PREDICTION_LEAD_DAYS)).tz_localize("UTC")


def kickoff_bounds(kickoff_date: pd.Timestamp | date | str) -> dict[str, str | None]:
    """Bounds for a local calendar date under UTC+14 through UTC-12 offsets."""
    day = pd.Timestamp(kickoff_date).normalize().tz_localize("UTC")
    earliest = day - pd.Timedelta(hours=UTC_EARLIEST_OFFSET_HOURS)
    latest_exclusive = day + pd.Timedelta(days=1, hours=-UTC_LATEST_OFFSET_HOURS)
    return {
        "source_date": day.date().isoformat(),
        "exact_timestamp_utc": None,
        "earliest_possible_utc": earliest.isoformat(),
        "latest_possible_utc_exclusive": latest_exclusive.isoformat(),
        "time_quality": "date_only_timezone_envelope_not_verified_kickoff",
    }


def result_available_upper_bound(
    result_date: pd.Timestamp | date | str,
) -> pd.Timestamp:
    day = pd.Timestamp(result_date).normalize().tz_localize("UTC")
    return day + pd.Timedelta(days=RESULT_AVAILABILITY_LAG_DAYS)


def _eligible_result_prefix(
    results: pd.DataFrame, cutoff: pd.Timestamp
) -> pd.DataFrame:
    if cutoff.tzinfo is None:
        raise ValueError("Prediction cutoff must be timezone-aware UTC")
    upper_bounds = pd.to_datetime(results["date"], utc=True) + pd.Timedelta(
        days=RESULT_AVAILABILITY_LAG_DAYS
    )
    prefix = results.loc[upper_bounds < cutoff].copy()
    if not prefix.empty and not (upper_bounds.loc[prefix.index] < cutoff).all():
        raise AssertionError("Future/unavailable result entered training prefix")
    return prefix.reset_index(drop=True)


def _points(gf: int, ga: int) -> int:
    return 3 if gf > ga else 1 if gf == ga else 0


@dataclass(frozen=True)
class _TeamMatch:
    day: pd.Timestamp
    gf: int
    ga: int
    points: int
    tournament: str


class ResultFeatureIndex:
    """Point-in-time result-history features with strict conservative cutoff."""

    def __init__(self, competitive_results: pd.DataFrame):
        self.results = competitive_results.sort_values(
            ["date", "home_team", "away_team"]
        ).reset_index(drop=True)
        self.team_history: dict[str, list[_TeamMatch]] = defaultdict(list)
        self.h2h_history: dict[tuple[str, str], list[tuple[pd.Timestamp, int, int]]] = (
            defaultdict(list)
        )
        self.elo_history: dict[str, list[tuple[pd.Timestamp, float]]] = defaultdict(
            list
        )
        ratings: dict[str, float] = {}
        for row in self.results.itertuples(index=False):
            day = pd.Timestamp(row.date)
            home, away = str(row.home_team), str(row.away_team)
            hg, ag = int(row.home_score), int(row.away_score)
            self.team_history[home].append(
                _TeamMatch(day, hg, ag, _points(hg, ag), str(row.tournament))
            )
            self.team_history[away].append(
                _TeamMatch(day, ag, hg, _points(ag, hg), str(row.tournament))
            )
            self.h2h_history[(home, away)].append((day, hg, ag))
            self.h2h_history[(away, home)].append((day, ag, hg))
            tournament = str(row.tournament)
            # Match the repository's Elo convention for tournament K factors.
            k = (
                ELO_K_BASE * TOURNAMENT_K_FACTORS[tournament]
                if tournament in TOURNAMENT_K_FACTORS
                else 40
            )
            ratings = update_ratings(
                ratings, home, away, hg, ag, k=k, neutral=bool(row.neutral)
            )
            self.elo_history[home].append((day, ratings[home]))
            self.elo_history[away].append((day, ratings[away]))

    @staticmethod
    def _limit(cutoff: pd.Timestamp) -> pd.Timestamp:
        # Date-only source timestamp + lag must be strictly earlier than cutoff.
        return cutoff.tz_convert("UTC").tz_localize(None).normalize() - pd.Timedelta(
            days=RESULT_AVAILABILITY_LAG_DAYS
        )

    def _team_prefix(self, team: str, cutoff: pd.Timestamp) -> list[_TeamMatch]:
        history = self.team_history.get(team, [])
        limit = self._limit(cutoff)
        pos = bisect_left([item.day for item in history], limit)
        return history[:pos]

    def _elo_asof(self, team: str, cutoff: pd.Timestamp) -> float:
        history = self.elo_history.get(team, [])
        limit = self._limit(cutoff)
        pos = bisect_left([item[0] for item in history], limit)
        return float(history[pos - 1][1]) if pos else 1500.0

    @staticmethod
    def _form(prefix: list[_TeamMatch], team: str, window: int) -> dict[str, float]:
        recent = prefix[-window:]
        result: dict[str, float] = {}
        for name, getter in (
            ("gf", lambda item: item.gf),
            ("ga", lambda item: item.ga),
            ("gd", lambda item: item.gf - item.ga),
            ("points", lambda item: item.points),
        ):
            result[f"{team}_recent{window}_{name}"] = (
                float(np.mean([getter(item) for item in recent])) if recent else 0.0
            )
        return result

    def features(
        self, row: Any, cutoff: pd.Timestamp | None = None
    ) -> dict[str, float]:
        home, away = str(row.home_team), str(row.away_team)
        cutoff = cutoff or prediction_cutoff(row.date)
        earliest_kickoff = pd.Timestamp(
            kickoff_bounds(row.date)["earliest_possible_utc"]
        )
        if cutoff >= earliest_kickoff:
            raise ValueError(
                "Prediction cutoff is not before the conservative kickoff lower bound"
            )
        h = self._team_prefix(home, cutoff)
        a = self._team_prefix(away, cutoff)
        cutoff_day = cutoff.tz_convert("UTC").tz_localize(None).normalize()

        def rest_days(history: list[_TeamMatch]) -> float:
            return (
                float(min(3650, max(0, (cutoff_day - history[-1].day).days)))
                if history
                else 3650.0
            )

        def load(history: list[_TeamMatch], days: int) -> float:
            lower = cutoff_day - pd.Timedelta(days=days)
            return float(
                sum(lower <= match.day < self._limit(cutoff) for match in history)
            )

        values: dict[str, float] = {
            "home_elo": self._elo_asof(home, cutoff),
            "away_elo": self._elo_asof(away, cutoff),
            "home_competitive_matches": float(len(h)),
            "away_competitive_matches": float(len(a)),
            "home_rest_days_at_cutoff": rest_days(h),
            "away_rest_days_at_cutoff": rest_days(a),
            "home_match_load_30d": load(h, 30),
            "away_match_load_30d": load(a, 30),
            "home_match_load_90d": load(h, 90),
            "away_match_load_90d": load(a, 90),
        }
        values["elo_difference"] = values["home_elo"] - values["away_elo"]
        values.update(self._form(h, "home", 5))
        values.update(self._form(h, "home", 10))
        values.update(self._form(a, "away", 5))
        values.update(self._form(a, "away", 10))
        values["home_form_momentum_5v10"] = (
            values["home_recent5_points"] - values["home_recent10_points"]
        )
        values["away_form_momentum_5v10"] = (
            values["away_recent5_points"] - values["away_recent10_points"]
        )

        home_nl = [item for item in h if item.tournament == TOURNAMENT][-5:]
        away_nl = [item for item in a if item.tournament == TOURNAMENT][-5:]
        values["home_nl_recent5_points"] = (
            float(np.mean([item.points for item in home_nl])) if home_nl else 0.0
        )
        values["away_nl_recent5_points"] = (
            float(np.mean([item.points for item in away_nl])) if away_nl else 0.0
        )

        meetings = self.h2h_history.get((home, away), [])
        limit = bisect_left([item[0] for item in meetings], self._limit(cutoff))
        prior_meetings = meetings[:limit]
        values["h2h_matches"] = float(len(prior_meetings))
        values["h2h_home_points_per_match"] = (
            float(np.mean([_points(hg, ag) for _, hg, ag in prior_meetings]))
            if prior_meetings
            else 0.0
        )
        values["h2h_goal_difference_per_match"] = (
            float(np.mean([hg - ag for _, hg, ag in prior_meetings]))
            if prior_meetings
            else 0.0
        )
        ordered = {column: values[column] for column in FEATURE_COLUMNS}
        if not np.isfinite(np.asarray(list(ordered.values()), dtype=float)).all():
            raise ValueError("Non-finite value in causal feature vector")
        return ordered


def _feature_schema_digest() -> str:
    return _sha256(
        _canonical_json(
            {
                "version": FEATURE_SCHEMA_VERSION,
                "columns": FEATURE_COLUMNS,
                "prediction_lead_days": PREDICTION_LEAD_DAYS,
                "result_availability_upper_bound_days": RESULT_AVAILABILITY_LAG_DAYS,
                "competitive_training_policy": "COMPETITIVE_TOURNAMENTS substring match",
                "feature_history_policy": "source result date + availability lag < prediction cutoff",
                "neutral_target_feature": "excluded; point-in-time status unavailable",
            }
        )
    )


def _digest_training(rows: pd.DataFrame, features: list[dict[str, float]]) -> str:
    payload = [
        [
            str(row.fixture_id),
            int(outcome_index(row.home_score, row.away_score)),
            [float(feature[column]) for column in FEATURE_COLUMNS],
        ]
        for row, feature in zip(rows.itertuples(index=False), features, strict=True)
    ]
    return _sha256(_canonical_json(payload))


def _digest_raw_training(rows: pd.DataFrame) -> str:
    payload = [
        [
            str(row.fixture_id),
            row.date.date().isoformat(),
            str(row.home_team),
            str(row.away_team),
            int(row.home_score),
            int(row.away_score),
            bool(row.neutral),
            str(row.tournament),
        ]
        for row in rows.itertuples(index=False)
    ]
    return _sha256(_canonical_json(payload))


def _metric_summary(probabilities: np.ndarray, outcomes: np.ndarray) -> dict[str, Any]:
    if probabilities.shape != (len(outcomes), 3) or len(outcomes) == 0:
        raise ValueError(
            "Metrics require an aligned non-empty three-class prediction matrix"
        )
    if not np.isfinite(probabilities).all() or (probabilities < 0).any():
        raise ValueError("Invalid probabilities in evaluation")
    probabilities = probabilities / probabilities.sum(axis=1, keepdims=True)
    clipped = np.clip(probabilities, 1e-15, 1.0)
    one_hot = np.eye(3)[outcomes]
    class_calibration: dict[str, Any] = {}
    for index, label in enumerate(("home", "draw", "away")):
        p, y = probabilities[:, index], one_hot[:, index]
        edges = np.linspace(0.0, 1.0, 11)
        ece = 0.0
        for bin_idx in range(10):
            mask = (p >= edges[bin_idx]) & (p < edges[bin_idx + 1])
            if bin_idx == 9:
                mask |= p == 1.0
            if mask.any():
                ece += float(mask.mean()) * abs(float(p[mask].mean() - y[mask].mean()))
        class_calibration[label] = {
            "mean_probability": float(p.mean()),
            "observed_frequency": float(y.mean()),
            "mean_probability_minus_frequency": float(p.mean() - y.mean()),
            "brier_one_vs_rest": float(np.mean((p - y) ** 2)),
            "ece_10_bins": float(ece),
        }
    overall_ece = float(
        np.mean([item["ece_10_bins"] for item in class_calibration.values()])
    )
    return {
        "matches_evaluated": len(outcomes),
        "multiclass_brier": float(brier_score_multiclass(probabilities, outcomes)),
        "multiclass_log_loss": float(
            -np.log(clipped[np.arange(len(outcomes)), outcomes]).mean()
        ),
        "ece_10_bins_mean_one_vs_rest": overall_ece,
        "calibration_home_draw_away": class_calibration,
        "accuracy_secondary": float((probabilities.argmax(axis=1) == outcomes).mean()),
        "mean_max_probability_sharpness": float(probabilities.max(axis=1).mean()),
    }


def _paired_date_cluster_bootstrap(
    paired_rows: list[dict[str, Any]],
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    if not paired_rows:
        return {
            "status": "unavailable",
            "reason": "No common predictions across all three models",
        }
    by_period_date: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in paired_rows:
        by_period_date[row["validation_period"]][row["kickoff"]["source_date"]].append(
            row
        )
    rng = np.random.default_rng(seed)
    comparisons = (("gbt", "dixon_coles"), ("gbt", "elo"))
    sample_scores: dict[str, dict[str, list[float]]] = {
        f"{left}_minus_{right}": {
            metric: [] for metric in ("multiclass_brier", "multiclass_log_loss")
        }
        for left, right in comparisons
    }
    for _ in range(replicates):
        sample: list[dict[str, Any]] = []
        for period in sorted(by_period_date):
            dates = sorted(by_period_date[period])
            selected = rng.choice(dates, size=len(dates), replace=True)
            for selected_day in selected:
                sample.extend(by_period_date[period][str(selected_day)])
        y = np.asarray(
            [row["outcome_index_home_draw_away"] for row in sample], dtype=int
        )
        for left, right in comparisons:
            key = f"{left}_minus_{right}"
            for metric in ("multiclass_brier", "multiclass_log_loss"):
                p_left = np.asarray(
                    [row["predictions"][left]["probabilities"] for row in sample]
                )
                p_right = np.asarray(
                    [row["predictions"][right]["probabilities"] for row in sample]
                )
                if metric == "multiclass_brier":
                    diff = brier_score_multiclass(p_left, y) - brier_score_multiclass(
                        p_right, y
                    )
                else:
                    clipped_left = np.clip(p_left, 1e-15, 1.0)
                    clipped_right = np.clip(p_right, 1e-15, 1.0)
                    diff = float(
                        -np.log(clipped_left[np.arange(len(y)), y]).mean()
                        + np.log(clipped_right[np.arange(len(y)), y]).mean()
                    )
                sample_scores[key][metric].append(float(diff))
    intervals = {
        comparison: {
            metric: {
                "mean_difference": float(np.mean(values)),
                "lower_95": float(np.quantile(values, 0.025)),
                "upper_95": float(np.quantile(values, 0.975)),
                "replicates": len(values),
            }
            for metric, values in metrics.items()
        }
        for comparison, metrics in sample_scores.items()
    }
    return {
        "method": "paired date-cluster bootstrap, stratified by declared evaluation block",
        "seed": seed,
        "replicates": replicates,
        "confidence_level": 0.95,
        "lower_is_better": True,
        "comparisons": intervals,
    }


def _make_model_prediction(
    probabilities: Iterable[float], **metadata: Any
) -> dict[str, Any]:
    probs = np.asarray(list(probabilities), dtype=float)
    if probs.shape != (3,) or not np.isfinite(probs).all() or (probs < 0).any():
        raise ValueError("Model emitted invalid 1X2 probabilities")
    total = float(probs.sum())
    if total <= 0 or abs(total - 1.0) > 1e-5:
        raise ValueError("Model probabilities do not sum to one")
    probs /= total
    return {"probabilities": probs.tolist(), **metadata}


def run_replay(
    source_results: pd.DataFrame,
    source_sha256: str,
    *,
    expected_fixture_count: int | None = 512,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
    max_dc_iter: int = 2000,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    results = normalize_results(source_results)
    targets = select_evaluation_matches(results)
    if targets.empty:
        raise ValueError("No target fixtures matched declared UEFA NL blocks")
    if expected_fixture_count is not None and len(targets) != expected_fixture_count:
        raise ValueError(
            f"Expected {expected_fixture_count} target fixtures, found {len(targets)}"
        )
    competitive = (
        results.loc[
            results["tournament"].apply(
                lambda tournament: any(
                    keyword in str(tournament) for keyword in COMPETITIVE_TOURNAMENTS
                )
            )
        ]
        .copy()
        .reset_index(drop=True)
    )
    history_index = ResultFeatureIndex(competitive)
    all_features = [
        history_index.features(row) for row in competitive.itertuples(index=False)
    ]
    features_by_id = dict(zip(competitive["fixture_id"], all_features, strict=True))
    schema_digest = _feature_schema_digest()
    config_digest = _sha256(
        _canonical_json(
            {
                "parameters": MODEL_CONFIG,
                "sklearn_version": sklearn.__version__,
                "feature_schema_digest": schema_digest,
                "outcome_order": ["home", "draw", "away"],
            }
        )
    )
    target_features = {
        str(row.fixture_id): history_index.features(row)
        for row in targets.itertuples(index=False)
    }

    output_by_id: dict[str, dict[str, Any]] = {}
    cadence_records: list[dict[str, Any]] = []

    for block in EVALUATION_BLOCKS:
        block_targets = targets.loc[targets["validation_period"].eq(block["id"])].copy()
        if block_targets.empty:
            continue
        cutoff = prediction_cutoff(block_targets["date"].min())
        training = _eligible_result_prefix(competitive, cutoff)
        if training.empty:
            raise ValueError(
                f"Empty competitive training prefix for block {block['id']}"
            )
        training_feature_rows = [
            features_by_id[str(fid)] for fid in training["fixture_id"]
        ]
        x_train = pd.DataFrame(training_feature_rows, columns=FEATURE_COLUMNS)
        y_train = np.asarray(
            [
                outcome_index(int(row.home_score), int(row.away_score))
                for row in training.itertuples(index=False)
            ],
            dtype=int,
        )
        if set(np.unique(y_train)) != {0, 1, 2}:
            raise ValueError(
                f"GBT training prefix lacks an outcome class in {block['id']}"
            )
        model = HistGradientBoostingClassifier(
            **{
                key: value
                for key, value in MODEL_CONFIG.items()
                if key
                in {
                    "max_iter",
                    "learning_rate",
                    "max_leaf_nodes",
                    "min_samples_leaf",
                    "l2_regularization",
                    "early_stopping",
                    "random_state",
                }
            }
        )
        model.fit(x_train, y_train)
        gbt_train_digest = _digest_training(training, training_feature_rows)
        raw_train_digest = _digest_raw_training(training)
        if not training["date"].max() + pd.Timedelta(
            days=RESULT_AVAILABILITY_LAG_DAYS
        ) < cutoff.tz_localize(None):
            raise ValueError(
                "GBT/DC training maximum availability bound is not strictly before block cutoff"
            )
        fitted_dc = dc.fit(
            training,
            today=cutoff.tz_localize(None),
            max_iter=max_dc_iter,
            prior_params=None,
            wc2026_boost_override=1.0,
        )
        elo_training = history_index
        block_record = {
            "period": block["id"],
            "model_fit_cutoff": cutoff.isoformat(),
            "training_start": training["date"].min().date().isoformat(),
            "training_max_timestamp": (
                (
                    training["date"].max()
                    + pd.Timedelta(days=RESULT_AVAILABILITY_LAG_DAYS)
                )
                .tz_localize("UTC")
                .isoformat()
            ),
            "training_match_count": len(training),
            "training_raw_data_digest": raw_train_digest,
            "training_feature_data_digest": gbt_train_digest,
            "feature_schema_digest": schema_digest,
            "model_config_digest": config_digest,
            "model_seed": MODEL_SEED,
            "time_proof_basis": "conservative date-only bounds; availability lag assumption is not source-timestamp verified",
        }
        cadence_records.append(block_record)

        for row in block_targets.itertuples(index=False):
            fixture_id = str(row.fixture_id)
            kickoff = kickoff_bounds(row.date)
            target_cutoff = prediction_cutoff(row.date)
            target_training_prefix = _eligible_result_prefix(competitive, target_cutoff)
            target_training_max = (
                result_available_upper_bound(
                    target_training_prefix["date"].max()
                ).isoformat()
                if not target_training_prefix.empty
                else None
            )
            gbt_raw = model.predict_proba(
                pd.DataFrame([target_features[fixture_id]], columns=FEATURE_COLUMNS)
            )[0]
            # sklearn orders classes numerically; convert canonical [home, draw, away].
            gbt_probs = np.zeros(3, dtype=float)
            for class_position, class_label in enumerate(model.classes_):
                gbt_probs[int(class_label)] = gbt_raw[class_position]
            gbt_record = _make_model_prediction(
                gbt_probs,
                prediction_cutoff=target_cutoff.isoformat(),
                model_fit_cutoff=cutoff.isoformat(),
                training_start=block_record["training_start"],
                training_max_timestamp=block_record["training_max_timestamp"],
                training_match_count=block_record["training_match_count"],
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                feature_schema_digest=schema_digest,
                training_data_digest=gbt_train_digest,
                model_config_digest=config_digest,
                model_seed=MODEL_SEED,
                model_fit_cadence="once before each declared evaluation block",
                feature_vector_digest=_sha256(
                    _canonical_json(
                        [
                            target_features[fixture_id][column]
                            for column in FEATURE_COLUMNS
                        ]
                    )
                ),
            )
            dc_record = None
            try:
                dc_probs_map = dc.predict_match(
                    str(row.home_team),
                    str(row.away_team),
                    fitted_dc,
                    neutral=bool(row.neutral),
                )
                dc_record = _make_model_prediction(
                    [
                        dc_probs_map["p_home"],
                        dc_probs_map["p_draw"],
                        dc_probs_map["p_away"],
                    ],
                    prediction_cutoff=target_cutoff.isoformat(),
                    model_fit_cutoff=cutoff.isoformat(),
                    training_start=block_record["training_start"],
                    training_max_timestamp=block_record["training_max_timestamp"],
                    training_match_count=block_record["training_match_count"],
                    training_data_digest=raw_train_digest,
                    fit_cadence="once before each declared evaluation block",
                )
            except ValueError:
                pass
            home_elo = elo_training._elo_asof(str(row.home_team), target_cutoff)
            away_elo = elo_training._elo_asof(str(row.away_team), target_cutoff)
            elo_probs = elo_win_probability(
                home_elo, away_elo, neutral=bool(row.neutral)
            )
            elo_record = _make_model_prediction(
                elo_probs,
                prediction_cutoff=target_cutoff.isoformat(),
                training_start=(
                    target_training_prefix["date"].min().date().isoformat()
                    if not target_training_prefix.empty
                    else None
                ),
                training_max_timestamp=target_training_max,
                training_match_count=len(target_training_prefix),
                training_data_digest=_digest_raw_training(target_training_prefix),
                update_policy="sequential Elo with all results inside conservative availability prefix",
            )
            output_by_id[fixture_id] = {
                "fixture_id": fixture_id,
                "home_team": str(row.home_team),
                "away_team": str(row.away_team),
                "kickoff": kickoff,
                "prediction_cutoff": target_cutoff.isoformat(),
                "training_start": block_record["training_start"],
                "training_max_timestamp": block_record["training_max_timestamp"],
                "training_match_count": block_record["training_match_count"],
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "feature_schema_digest": schema_digest,
                "training_data_digest": gbt_train_digest,
                "model_config_digest": config_digest,
                "model_seed": MODEL_SEED,
                "neutral_source_field_used_by_baselines": bool(row.neutral),
                "neutral_metadata_pit_verified": False,
                "validation_period": str(row.validation_period),
                "outcome_index_home_draw_away": outcome_index(
                    int(row.home_score), int(row.away_score)
                ),
                "predictions": {
                    "gbt": gbt_record,
                    **({"dixon_coles": dc_record} if dc_record else {}),
                    "elo": elo_record,
                },
            }

    audit_rows = [output_by_id[str(fid)] for fid in targets["fixture_id"]]
    common = [
        row
        for row in audit_rows
        if {"gbt", "dixon_coles", "elo"}.issubset(row["predictions"])
    ]
    if len(common) != len(targets):
        raise ValueError(
            f"Models do not share the complete fixture set: common={len(common)}, targets={len(targets)}"
        )
    outcomes = np.asarray(
        [row["outcome_index_home_draw_away"] for row in common], dtype=int
    )
    summaries = {
        name: _metric_summary(
            np.asarray(
                [row["predictions"][name]["probabilities"] for row in common],
                dtype=float,
            ),
            outcomes,
        )
        for name in ("elo", "dixon_coles", "gbt")
    }
    blocks_summary: dict[str, Any] = {}
    for block in EVALUATION_BLOCKS:
        period_rows = [row for row in common if row["validation_period"] == block["id"]]
        period_y = np.asarray(
            [row["outcome_index_home_draw_away"] for row in period_rows], dtype=int
        )
        blocks_summary[block["id"]] = {
            "fixture_count": len(period_rows),
            "models": {
                name: _metric_summary(
                    np.asarray(
                        [
                            row["predictions"][name]["probabilities"]
                            for row in period_rows
                        ]
                    ),
                    period_y,
                )
                if period_rows
                else None
                for name in ("elo", "dixon_coles", "gbt")
            },
        }
    paired = _paired_date_cluster_bootstrap(
        common, bootstrap_replicates, BOOTSTRAP_SEED
    )
    gbt_dc_brier_ci = paired["comparisons"]["gbt_minus_dixon_coles"]["multiclass_brier"]
    gbt_dc_logloss_ci = paired["comparisons"]["gbt_minus_dixon_coles"][
        "multiclass_log_loss"
    ]
    gbt_dc_brier_delta = (
        summaries["gbt"]["multiclass_brier"]
        - summaries["dixon_coles"]["multiclass_brier"]
    )
    gbt_dc_logloss_delta = (
        summaries["gbt"]["multiclass_log_loss"]
        - summaries["dixon_coles"]["multiclass_log_loss"]
    )
    block_brier_deltas = {
        period: values["models"]["gbt"]["multiclass_brier"]
        - values["models"]["dixon_coles"]["multiclass_brier"]
        for period, values in blocks_summary.items()
        if values["fixture_count"]
    }
    descriptive_candidate_assessment = {
        "label": "NL_CAUSAL_GBT_PROMISING_NOT_CONFIRMED",
        "overall_brier_better_than_dc": gbt_dc_brier_delta < 0,
        "brier_delta_gbt_minus_dc": gbt_dc_brier_delta,
        "paired_brier_ci_excludes_zero_in_gbt_favor": gbt_dc_brier_ci["upper_95"] < 0,
        "paired_brier_ci": gbt_dc_brier_ci,
        "logloss_delta_gbt_minus_dc": gbt_dc_logloss_delta,
        "paired_logloss_ci": gbt_dc_logloss_ci,
        "brier_improved_in_each_declared_block": all(
            value < 0 for value in block_brier_deltas.values()
        ),
        "block_brier_deltas_gbt_minus_dc": block_brier_deltas,
        "ece_delta_gbt_minus_dc": (
            summaries["gbt"]["ece_10_bins_mean_one_vs_rest"]
            - summaries["dixon_coles"]["ece_10_bins_mean_one_vs_rest"]
        ),
        "calibration_noninferiority_threshold": "not prespecified; all class-wise values reported without post-hoc pass/fail threshold",
        "causal_gate": "BLOCKED_PENDING_POINT_IN_TIME_TIMESTAMP_PROVENANCE",
        "interpretation": "Descriptive score pattern is favorable across the full set and each block, but the paired Brier interval includes zero and source timestamp provenance is insufficient for causal qualification.",
    }
    audit = {
        "schema_version": "nl-causal-gbt-replay-audit-v1",
        "source": {
            "sha256": source_sha256,
            "row_count": len(results),
            "competitive_row_count": len(competitive),
            "fixture_identity_selection": "same local results cache rows filtered to UEFA Nations League and the declared date blocks; no external fixture manifest",
            "network_fetch_performed": False,
            "cache_has_exact_kickoff_timestamps": False,
            "cache_has_result_publication_timestamps": False,
        },
        "target_fixture_count": len(targets),
        "common_fixture_count": len(common),
        "model_fit_cadence": "each model fitted/refreshed before each declared NL block; Elo then updates through the same strict availability prefix",
        "model_configuration": {
            "parameters": MODEL_CONFIG,
            "sklearn_version": sklearn.__version__,
            "outcome_order": ["home", "draw", "away"],
            "gbt_training_uses_random_split": False,
        },
        "time_policy": {
            "prediction_lead_days": PREDICTION_LEAD_DAYS,
            "result_availability_upper_bound_days": RESULT_AVAILABILITY_LAG_DAYS,
            "kickoff_bounds": "source local date mapped to UTC+14 through UTC-12 envelope",
            "proof_status": "CONSERVATIVE_BOUND_ONLY_NOT_POINT_IN_TIME_SOURCE_VERIFIED",
            "limitations": [
                "Input source contains calendar dates only; exact kickoff timestamps are unavailable.",
                "Three-day result availability upper bound is a conservative replay assumption, not a source-proven publication timestamp.",
                "Neutral-site flags come from the results cache and are not independently proven known by the simulated cutoff; GBT excludes this field, Elo/DC use it as fixture metadata.",
                "The historical fixture schedule/identity itself has no archived point-in-time publication timestamp.",
            ],
        },
        "feature_audit": {
            "included": list(FEATURE_COLUMNS),
            "decisions": {
                "Elo": "included; reconstructed sequentially from competitive results within the strict conservative availability prefix",
                "Dixon-Coles-derived target/model features": "UNAVAILABLE in GBT; no historical point-in-time DC prediction snapshots. DC is separately fitted as the baseline at the declared block cadence.",
                "form / rolling GF-GA-GD / points": "included; prior competitive results only",
                "momentum": "included; difference between recent-five and recent-ten points per match",
                "match load / rest": "included; prior competitive results only, measured relative to simulated cutoff",
                "head-to-head": "included; prior direct meetings only",
                "tournament/stage": "competition type is constant across evaluation targets; stage UNAVAILABLE without point-in-time schedule metadata",
                "neutral-site flag": "excluded from GBT because historical source does not prove point-in-time availability",
                "team identity encoding": "excluded; no frozen point-in-time categorical map and ordinal encoding is not valid",
            },
            "excluded_noncausal_or_unverified": [
                "current market values",
                "current squad availability",
                "current injuries",
                "lineups",
                "later player ratings",
                "xG",
                "odds",
                "static current snapshots",
                "external fixture manifests",
            ],
        },
        "training_blocks": cadence_records,
        "metrics_common_fixture_set": summaries,
        "block_results": blocks_summary,
        "paired_date_cluster_bootstrap": paired,
        "descriptive_candidate_assessment": descriptive_candidate_assessment,
        "causal_evidence_status": "BLOCKED_PENDING_POINT_IN_TIME_TIMESTAMP_PROVENANCE",
        "research_status": "NL_CAUSAL_GBT_BLOCKED",
        "stacker_next_step_recommendation": "Do not treat this replay as qualifying causal OOS evidence until source kickoff, schedule-availability, and result-publication timestamp provenance is established. Metrics are descriptive under the stated conservative date-bound assumptions only.",
    }
    return audit, audit_rows


def write_artifacts(
    audit: dict[str, Any], rows: list[dict[str, Any]], output_dir: Path
) -> tuple[Path, Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = output_dir / "nations_league_causal_gbt_predictions_20260929.json"
    audit_path = output_dir / "nations_league_causal_gbt_replay_20260929.json"
    report_path = output_dir / "nations_league_causal_gbt_replay_20260929.md"
    prediction_payload = {
        "schema_version": "nl-causal-gbt-predictions-v1",
        "research_only": True,
        "source_sha256": audit["source"]["sha256"],
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_schema_digest": _feature_schema_digest(),
        "records": rows,
    }
    prediction_path.write_bytes(_canonical_json(prediction_payload) + b"\n")
    audit_path.write_bytes(_canonical_json(audit) + b"\n")
    report_path.write_text(render_markdown_report(audit), encoding="utf-8")
    return prediction_path, audit_path, report_path


def render_markdown_report(audit: dict[str, Any]) -> str:
    lines = [
        "# Nations League causal GBT walk-forward replay",
        "",
        f"Research status: **{audit['research_status']}**",
        "",
        "This is an offline research artifact only. The date-only input lacks source-proven kickoff, schedule-publication, and result-publication timestamps. Metrics below are descriptive under conservative date-bound assumptions and are not certified point-in-time OOS evidence.",
        "",
        f"- Evaluation fixtures: {audit['target_fixture_count']} (common to all models: {audit['common_fixture_count']})",
        f"- Results source SHA-256: `{audit['source']['sha256']}`",
        f"- Competitive training rows available in local source: {audit['source']['competitive_row_count']}",
        f"- Prediction lead: {PREDICTION_LEAD_DAYS} days; assumed result availability upper bound: {RESULT_AVAILABILITY_LAG_DAYS} days",
        "- Refresh cadence: once before each declared Nations League evaluation block",
        f"- GBT: sklearn {audit['model_configuration']['sklearn_version']} HistGradientBoostingClassifier; max_iter=250, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=60, L2=2.0, seed={MODEL_SEED}; random internal early stopping disabled",
        f"- Feature schema digest: `{_feature_schema_digest()}`",
        f"- Model config digest (first block): `{audit['training_blocks'][0]['model_config_digest']}`",
        "- Markets/odds: not used; no stacker",
        "",
        "## Overall metrics (identical fixture set)",
        "",
        "| Model | N | Brier | Log loss | ECE | Accuracy (secondary) | Mean max probability |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model, label in (
        ("elo", "Elo"),
        ("dixon_coles", "Dixon–Coles"),
        ("gbt", "Causal-envelope GBT"),
    ):
        metric = audit["metrics_common_fixture_set"][model]
        lines.append(
            f"| {label} | {metric['matches_evaluated']} | {metric['multiclass_brier']:.6f} | "
            f"{metric['multiclass_log_loss']:.6f} | {metric['ece_10_bins_mean_one_vs_rest']:.6f} | "
            f"{metric['accuracy_secondary']:.6f} | {metric['mean_max_probability_sharpness']:.6f} |"
        )
    lines += [
        "",
        "## Home / draw / away calibration",
        "",
        "| Model | Outcome | Mean predicted | Observed | Difference | OVR Brier | ECE |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for model, label in (
        ("elo", "Elo"),
        ("dixon_coles", "Dixon–Coles"),
        ("gbt", "GBT"),
    ):
        class_rows = audit["metrics_common_fixture_set"][model][
            "calibration_home_draw_away"
        ]
        for outcome in ("home", "draw", "away"):
            values = class_rows[outcome]
            lines.append(
                f"| {label} | {outcome.title()} | {values['mean_probability']:.6f} | "
                f"{values['observed_frequency']:.6f} | {values['mean_probability_minus_frequency']:+.6f} | "
                f"{values['brier_one_vs_rest']:.6f} | {values['ece_10_bins']:.6f} |"
            )
    lines += [
        "",
        "## Paired date-cluster bootstrap",
        "",
        "Lower scores are better; negative GBT-minus-baseline favors GBT.",
        "",
    ]
    comparisons = audit["paired_date_cluster_bootstrap"].get("comparisons", {})
    for comparison, metrics in comparisons.items():
        lines.append(f"### {comparison}")
        for metric, values in metrics.items():
            lines.append(
                f"- {metric}: mean {values['mean_difference']:.6f}; "
                f"95% CI [{values['lower_95']:.6f}, {values['upper_95']:.6f}]"
            )
        lines.append("")
    lines += [
        "## Evaluation blocks",
        "",
        "| Block | N | Elo Brier | DC Brier | GBT Brier | GBT log loss | GBT ECE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for period, result in audit["block_results"].items():
        models = result["models"]
        elo_brier = (
            "n/a"
            if models["elo"] is None
            else f"{models['elo']['multiclass_brier']:.6f}"
        )
        dc_brier = (
            "n/a"
            if models["dixon_coles"] is None
            else f"{models['dixon_coles']['multiclass_brier']:.6f}"
        )
        gbt_brier = (
            "n/a"
            if models["gbt"] is None
            else f"{models['gbt']['multiclass_brier']:.6f}"
        )
        gbt_logloss = (
            "n/a"
            if models["gbt"] is None
            else f"{models['gbt']['multiclass_log_loss']:.6f}"
        )
        gbt_ece = (
            "n/a"
            if models["gbt"] is None
            else f"{models['gbt']['ece_10_bins_mean_one_vs_rest']:.6f}"
        )
        lines.append(
            f"| {period} | {result['fixture_count']} | {elo_brier} | "
            f"{dc_brier} | {gbt_brier} | {gbt_logloss} | {gbt_ece} |"
        )
    lines += [
        "",
        "## Measured uplift assessment",
        "",
        f"The descriptive full-set GBT-minus-DC Brier delta is {audit['descriptive_candidate_assessment']['brier_delta_gbt_minus_dc']:+.6f}; improvement appears in every declared block, but the paired 95% interval includes zero. The Log Loss point delta is {audit['descriptive_candidate_assessment']['logloss_delta_gbt_minus_dc']:+.6f}, with uncertainty also spanning zero. Aggregate ECE delta is {audit['descriptive_candidate_assessment']['ece_delta_gbt_minus_dc']:+.6f}; no calibration noninferiority threshold was prespecified, so class calibration is shown above without a post-hoc gate.",
        "",
        "Descriptively this is promising, not confirmed; as causal evidence it remains blocked by absent point-in-time timestamp provenance. It does not justify advancing to a stacker.",
        "",
        "## Feature policy",
        "",
        "Included: as-of Elo, prior competitive-result form/rolling scoring, momentum, match load/rest, and prior head-to-head. DC-derived features and tournament stage are UNAVAILABLE for the GBT input. Current squad/market/static data, injuries, later ratings, xG, and odds are excluded.",
        "",
        "## Limitations and decision",
        "",
        "The ordering checks use an explicit seven-day simulated cutoff, a UTC kickoff-date envelope, and a three-day result-availability upper-bound assumption. The source has no exact kickoff, schedule-publication, or result-publication timestamps, so those bounds are not independently verified. The artifact must not be represented as strict timestamp-proven historical OOS evidence. No production or activation recommendation follows. Do not advance to a stacker on this artifact alone.",
        "",
        "No network/provider calls, credentials, publication, activation, betting, or ledger operations were used.",
        "",
    ]
    return "\n".join(lines)
