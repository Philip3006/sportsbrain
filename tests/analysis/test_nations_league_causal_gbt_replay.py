import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis import nations_league_causal_gbt_replay as replay


def _row(day, home, away, hg, ag, tournament="UEFA Euro qualification", neutral=False):
    return {
        "date": day,
        "home_team": home,
        "away_team": away,
        "home_score": hg,
        "away_score": ag,
        "tournament": tournament,
        "neutral": neutral,
    }


def _small_history():
    rows = []
    teams = ("Alpha", "Bravo", "Charlie", "Delta")
    start = pd.Timestamp("2016-01-01")
    for index in range(190):
        home = teams[index % 4]
        away = teams[(index + 1 + (index // 4) % 3) % 4]
        if home == away:
            away = teams[(index + 2) % 4]
        score_pair = ((2, 0), (1, 1), (0, 2), (2, 1), (0, 0), (1, 3))[index % 6]
        rows.append(_row(start + pd.Timedelta(days=8 * index), home, away, *score_pair))
    targets = (
        ("2020-09-05", "Alpha", "Bravo"),
        ("2022-06-05", "Bravo", "Charlie"),
        ("2024-03-22", "Charlie", "Delta"),
        ("2024-09-06", "Delta", "Alpha"),
    )
    for index, (day, home, away) in enumerate(targets):
        rows.append(
            _row(day, home, away, index % 3, (index + 1) % 3, "UEFA Nations League")
        )
    return pd.DataFrame(rows)


def test_date_only_cutoff_is_strictly_before_conservative_kickoff_lower_bound():
    day = pd.Timestamp("2024-09-06")
    cutoff = replay.prediction_cutoff(day)
    bounds = replay.kickoff_bounds(day)
    assert bounds["exact_timestamp_utc"] is None
    assert cutoff < pd.Timestamp(bounds["earliest_possible_utc"])
    assert cutoff.isoformat() == "2024-08-30T00:00:00+00:00"


def test_result_availability_upper_bound_must_precede_cutoff():
    cutoff = replay.prediction_cutoff("2024-09-06")
    rows = pd.DataFrame(
        [
            _row("2024-08-26", "Alpha", "Bravo", 1, 0),  # upper bound Aug 29, eligible
            _row(
                "2024-08-27", "Bravo", "Charlie", 1, 0
            ),  # upper bound Aug 30, equality excluded
            _row("2024-09-06", "Charlie", "Delta", 4, 0, "UEFA Nations League"),
        ]
    )
    normalized = replay.normalize_results(rows)
    prefix = replay._eligible_result_prefix(normalized, cutoff)
    assert prefix["date"].tolist() == [pd.Timestamp("2024-08-26")]
    max_available = replay.result_available_upper_bound(prefix["date"].max())
    assert max_available < cutoff


def test_future_results_cannot_change_asof_features():
    source = _small_history()
    normalized = replay.normalize_results(source)
    competitive = normalized.loc[
        normalized["tournament"].apply(
            lambda value: any(k in str(value) for k in replay.COMPETITIVE_TOURNAMENTS)
        )
    ].reset_index(drop=True)
    target = normalized.loc[normalized["tournament"].eq("UEFA Nations League")].iloc[0]
    baseline = replay.ResultFeatureIndex(competitive).features(target)

    changed = source.copy()
    cutoff = replay.prediction_cutoff(target["date"])
    changed_dates = pd.to_datetime(changed["date"], utc=True)
    unavailable_at_cutoff = (
        changed_dates + pd.Timedelta(days=replay.RESULT_AVAILABILITY_LAG_DAYS)
    ) >= cutoff
    changed.loc[unavailable_at_cutoff, ["home_score", "away_score"]] = [99, 0]
    normalized_changed = replay.normalize_results(changed)
    comp_changed = normalized_changed.loc[
        normalized_changed["tournament"].apply(
            lambda value: any(k in str(value) for k in replay.COMPETITIVE_TOURNAMENTS)
        )
    ].reset_index(drop=True)
    after = replay.ResultFeatureIndex(comp_changed).features(target)
    assert baseline == after


def test_feature_schema_is_finite_and_deterministic_and_excludes_unverified_fields():
    normalized = replay.normalize_results(_small_history())
    competitive = normalized.loc[
        normalized["tournament"].apply(
            lambda value: any(k in str(value) for k in replay.COMPETITIVE_TOURNAMENTS)
        )
    ].reset_index(drop=True)
    target = normalized.loc[normalized["tournament"].eq("UEFA Nations League")].iloc[0]
    first = replay.ResultFeatureIndex(competitive).features(target)
    second = replay.ResultFeatureIndex(competitive).features(target)
    assert tuple(first) == replay.FEATURE_COLUMNS
    assert first == second
    assert np.isfinite(np.asarray(list(first.values()))).all()
    assert "neutral" not in first
    assert "market_odds" not in first
    assert replay._feature_schema_digest() == replay._feature_schema_digest()


def test_walkforward_outputs_all_models_on_identical_fixture_set_and_reproducible(
    monkeypatch,
):
    monkeypatch.setattr(replay.dc, "fit", lambda matches, **kwargs: object())
    monkeypatch.setattr(
        replay.dc,
        "predict_match",
        lambda home, away, params, neutral=False: {
            "p_home": 0.4,
            "p_draw": 0.3,
            "p_away": 0.3,
        },
    )
    source = _small_history()
    first_audit, first = replay.run_replay(
        source, "a" * 64, expected_fixture_count=None, bootstrap_replicates=100
    )
    second_audit, second = replay.run_replay(
        source, "a" * 64, expected_fixture_count=None, bootstrap_replicates=100
    )
    assert first_audit["common_fixture_count"] == 4
    assert len(first) == 4
    assert [row["fixture_id"] for row in first] == [row["fixture_id"] for row in second]
    for left, right in zip(first, second, strict=True):
        assert (
            left["predictions"]["gbt"]["probabilities"]
            == right["predictions"]["gbt"]["probabilities"]
        )
        assert set(left["predictions"]) == {"elo", "dixon_coles", "gbt"}
        gbt = left["predictions"]["gbt"]
        assert gbt["training_max_timestamp"] < gbt["prediction_cutoff"]
        assert gbt["model_fit_cutoff"] <= gbt["prediction_cutoff"]
        assert gbt["prediction_cutoff"] < left["kickoff"]["earliest_possible_utc"]
        assert sum(gbt["probabilities"]) == pytest.approx(1.0)
        assert (
            gbt["training_data_digest"]
            == right["predictions"]["gbt"]["training_data_digest"]
        )
        assert (
            gbt["model_config_digest"]
            == right["predictions"]["gbt"]["model_config_digest"]
        )
    assert (
        first_audit["paired_date_cluster_bootstrap"]
        == second_audit["paired_date_cluster_bootstrap"]
    )
    assert (
        first_audit["causal_evidence_status"]
        == "BLOCKED_PENDING_POINT_IN_TIME_TIMESTAMP_PROVENANCE"
    )


def test_bootstrap_is_deterministic_and_records_paired_deltas():
    rows = []
    for day, period, outcome in (
        ("2024-01-01", "A", 0),
        ("2024-01-01", "A", 2),
        ("2024-01-02", "A", 1),
        ("2024-02-01", "B", 2),
    ):
        rows.append(
            {
                "validation_period": period,
                "kickoff": {"source_date": day},
                "outcome_index_home_draw_away": outcome,
                "predictions": {
                    "gbt": {"probabilities": [0.5, 0.25, 0.25]},
                    "dixon_coles": {"probabilities": [0.3, 0.4, 0.3]},
                    "elo": {"probabilities": [0.4, 0.3, 0.3]},
                },
            }
        )
    first = replay._paired_date_cluster_bootstrap(rows, replicates=200, seed=17)
    second = replay._paired_date_cluster_bootstrap(rows, replicates=200, seed=17)
    assert first == second
    assert set(first["comparisons"]) == {"gbt_minus_dixon_coles", "gbt_minus_elo"}
    assert (
        first["comparisons"]["gbt_minus_dixon_coles"]["multiclass_brier"]["replicates"]
        == 200
    )


def test_incomplete_block_or_out_of_order_features_fail_closed():
    normalized = replay.normalize_results(_small_history())
    index = replay.ResultFeatureIndex(normalized)
    row = next(
        normalized.loc[normalized["tournament"].eq("UEFA Nations League")].itertuples(
            index=False
        )
    )
    with pytest.raises(ValueError, match="not before"):
        index.features(row, pd.Timestamp("2024-09-06T00:00:00Z"))


def _timeline_fixture_record():
    record = {
        "fixture_id": "uefa-nl:test-fixture",
        "edition": "2024/25",
        "validation_period": "2024/25",
        "date": "2024-09-06",
        "home_team": "Türkiye",
        "away_team": "Iceland",
        "home_score": 3,
        "away_score": 1,
        "kickoff_utc": "2024-09-06T18:45:00Z",
        "result_safe_available_at": "2024-09-07T00:45:00Z",
        "provenance_status": "official_schedule_exact_crosswalk",
        "result_safe_status": "bounded_from_verified_kickoff",
        "record_digest": "",
    }
    record["record_digest"] = replay._timeline_digest(
        {key: value for key, value in record.items() if key != "record_digest"}
    )
    return record


def test_verified_timeline_loader_requires_canonical_digests_and_timestamps(tmp_path):
    record = _timeline_fixture_record()
    timeline = {
        "schema_version": replay.TIMELINE_SCHEMA,
        "competition": "UEFA Nations League",
        "records": [record],
        "dataset_digest": "",
    }
    timeline["dataset_digest"] = replay._timeline_digest(
        {key: value for key, value in timeline.items() if key != "dataset_digest"}
    )
    path = Path(tmp_path) / "timeline.json"
    path.write_text(json.dumps(timeline), encoding="utf-8")
    loaded, digest = replay.load_fixture_timeline(path, expected_record_count=1)
    assert loaded["records"][0]["fixture_id"] == "uefa-nl:test-fixture"
    assert digest == timeline["dataset_digest"]

    record["kickoff_utc"] = "2024-09-06T18:45:00"
    record["record_digest"] = replay._timeline_digest(
        {key: value for key, value in record.items() if key != "record_digest"}
    )
    timeline["records"] = [record]
    timeline["dataset_digest"] = replay._timeline_digest(
        {key: value for key, value in timeline.items() if key != "dataset_digest"}
    )
    path.write_text(json.dumps(timeline), encoding="utf-8")
    with pytest.raises(ValueError, match="timezone-aware"):
        replay.load_fixture_timeline(path, expected_record_count=1)


def test_verified_availability_feature_index_excludes_rows_not_available_at_cutoff():
    rows = pd.DataFrame(
        [
            {
                "fixture_id": "a",
                "date": "2024-01-01",
                "home_team": "Alpha",
                "away_team": "Bravo",
                "home_score": 1,
                "away_score": 0,
                "tournament": "UEFA Nations League",
                "neutral": False,
                "kickoff_utc": "2024-01-01T18:00:00Z",
                "result_safe_available_at": "2024-01-02T00:00:00Z",
            },
            {
                "fixture_id": "b",
                "date": "2024-01-02",
                "home_team": "Bravo",
                "away_team": "Charlie",
                "home_score": 0,
                "away_score": 0,
                "tournament": "UEFA Nations League",
                "neutral": False,
                "kickoff_utc": "2024-01-02T18:00:00Z",
                "result_safe_available_at": "2024-01-03T00:00:00Z",
            },
            {
                "fixture_id": "c",
                "date": "2024-01-03",
                "home_team": "Charlie",
                "away_team": "Alpha",
                "home_score": 0,
                "away_score": 2,
                "tournament": "UEFA Nations League",
                "neutral": False,
                "kickoff_utc": "2024-01-03T18:00:00Z",
                "result_safe_available_at": "2024-01-04T00:00:00Z",
            },
        ]
    )
    index = replay.ResultFeatureIndex(rows)
    target = rows.iloc[2]
    target_features = index.features(
        target, pd.Timestamp("2024-01-03T00:00:01Z")
    )
    assert target_features["home_competitive_matches"] == 1.0
    assert target_features["away_competitive_matches"] == 1.0
    with pytest.raises(ValueError, match="timezone-aware"):
        bad = rows.iloc[2].copy()
        bad["kickoff_utc"] = "2024-01-03T18:00:00"
        replay.ResultFeatureIndex(rows).features(
            bad, pd.Timestamp("2024-01-03T00:00:00Z")
        )
