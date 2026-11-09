import ast
import hashlib
from pathlib import Path

import pytest

from src.analysis import nations_league_squad_research as research

PREDICTION_AT = "2030-01-10T11:30:00Z"
KICKOFF_AT = "2030-01-10T12:00:00Z"


def _source(source_id, stamp, record_id):
    return {
        "source_id": source_id,
        "observed_at": stamp,
        "source_as_of": stamp,
        "record_id": record_id,
        "source_sha256": hashlib.sha256(source_id.encode()).hexdigest(),
    }


def _side(prefix, *, out_index=None, lineup_status="confirmed", include_history=True):
    players = []
    positions = [
        "GK",
        "DEF",
        "DEF",
        "DEF",
        "DEF",
        "MID",
        "MID",
        "MID",
        "MID",
        "FWD",
        "FWD",
        "FWD",
    ]
    for index in range(12):
        status = "out" if index == out_index else "fit"
        availability = 0.0 if status == "out" else 1.0
        claim = {"status": status, "availability": availability}
        players.append(
            {
                "player_id": f"{prefix}-{index}",
                "name": f"{prefix} Test Player {index}",
                "position": positions[index],
                "roster_source_id": "roster",
                "status_claims": [
                    {
                        **claim,
                        "source_id": "roster",
                        "observed_at": "2030-01-10T10:45:00Z",
                    },
                    {
                        **claim,
                        "source_id": "availability",
                        "observed_at": "2030-01-10T11:00:00Z",
                    },
                ],
                "key_player_evidence": {
                    "value": index == 0,
                    "source_id": "roster",
                    "observed_at": "2030-01-10T10:45:00Z",
                },
                "market_value": {
                    "value_eur_m": float(index + 1),
                    "source_id": "roster",
                    "as_of": "2030-01-10T10:45:00Z",
                },
                "player_strength": {
                    "score": index / 20,
                    "source_id": "roster",
                    "as_of": "2030-01-10T10:45:00Z",
                    "methodology_id": "test-only-score-v1",
                },
            }
        )
    lineup = {
        "status": lineup_status,
        "announced_at": "2030-01-10T11:10:00Z",
        "source_id": "lineup",
        "player_ids": [player["player_id"] for player in players[:11]],
    }
    if lineup_status == "unavailable":
        lineup = {"status": "unavailable", "player_ids": []}
    prior = []
    if include_history:
        prior.append(
            {
                "fixture_id": f"previous-{prefix}",
                "kickoff_at": "2030-01-05T19:00:00Z",
                "finished_at": "2030-01-05T21:00:00Z",
                "observed_at": "2030-01-05T21:05:00Z",
                "source_id": "history",
                "players": [
                    {"player_id": f"{prefix}-{index}", "minutes": 90, "started": True}
                    for index in range(11)
                ]
                + [{"player_id": f"{prefix}-11", "minutes": 0, "started": False}],
            }
        )
    return {
        "roster_scope": "matchday_squad",
        "roster_complete": True,
        "expected_player_count": 12,
        "players": players,
        "lineup": lineup,
        "match_history_complete_since": "2029-12-01T00:00:00Z"
        if include_history
        else None,
        "match_history_source_id": "history" if include_history else None,
        "prior_international_matches": prior,
    }


def _snapshot(evidence_kind=research.TEST_EVIDENCE):
    return {
        "schema_version": research.SNAPSHOT_SCHEMA,
        "evidence_kind": evidence_kind,
        "competition": "UEFA Nations League",
        "fixture": {
            "fixture_id": "TEST-ONLY-FIXTURE",
            "home_team": "England",
            "away_team": "France",
            "kickoff_at": KICKOFF_AT,
        },
        "observed_at": "2030-01-10T11:15:00Z",
        "sources": [
            _source("roster", "2030-01-10T10:45:00Z", "test-roster-record"),
            _source("availability", "2030-01-10T11:00:00Z", "test-availability-record"),
            _source("lineup", "2030-01-10T11:10:00Z", "test-lineup-record"),
            _source("history", "2030-01-05T21:05:00Z", "test-history-record"),
        ],
        "teams": {
            "home": _side("home", out_index=0),
            "away": _side("away"),
        },
    }


def _extract(snapshot=None):
    return research.extract_match_features(
        snapshot or _snapshot(),
        fixture_id="TEST-ONLY-FIXTURE",
        home_team="England",
        away_team="France",
        kickoff_at=KICKOFF_AT,
        prediction_at=PREDICTION_AT,
        allow_test_fixture=True,
    )


def test_feature_contract_reuses_canonical_impact_only_for_complete_timestamped_rosters():
    row = _extract()
    features = row["features"]

    assert row["schema_version"] == research.FEATURE_SCHEMA
    assert row["evidence_kind"] == "TEST_FIXTURE"
    assert row["data_quality"] == "HIGH"
    assert row["fallback_usage"] is False
    assert features["squad_availability_home"] == pytest.approx(11 / 12)
    assert features["squad_availability_away"] == 1.0
    assert features["squad_availability_diff"] == pytest.approx(-1 / 12)
    assert features["unavailable_player_count_home"] == 1
    assert features["unavailable_starter_count_home"] == 1
    assert features["goalkeeper_absence_home"] == 1
    assert features["weighted_impact_lost_home"] > 0
    assert features["key_player_risk_home"] == 1
    assert features["squad_market_value_home"] == 78
    assert features["squad_market_value_ratio"] == pytest.approx(1.0)
    assert features["squad_market_value_log_ratio"] == pytest.approx(0.0)
    assert features["starting_xi_strength_home"] is not None
    assert features["bench_strength_home"] == pytest.approx(11 / 20)
    assert features["returning_starter_count_home"] == 11
    assert features["missing_usual_starter_count_home"] == 0
    assert features["starter_turnover_rate_home"] == 0
    assert features["previous_match_starting_xi_minutes_home"] == 990
    assert features["matches_last_7d_home"] == 1
    assert features["matches_last_14d_home"] == 1
    assert row["feature_digest"] == research.canonical_json_digest(
        {key: value for key, value in row.items() if key != "feature_digest"}
    )
    assert {
        "squad_availability_home",
        "squad_availability_away",
        "squad_availability_diff",
        "unavailable_player_count_home",
        "unavailable_player_count_away",
        "unavailable_starter_count_home",
        "unavailable_starter_count_away",
        "goalkeeper_absence_home",
        "defender_absence_home",
        "midfielder_absence_home",
        "forward_absence_home",
        "weighted_impact_lost_home",
        "weighted_impact_lost_away",
        "weighted_impact_lost_diff",
        "key_player_risk_home",
        "key_player_risk_away",
        "squad_market_value_home",
        "squad_market_value_away",
        "squad_market_value_ratio",
        "squad_market_value_log_ratio",
        "starting_xi_strength_diff",
        "bench_strength_diff",
        "returning_starter_count_home",
        "missing_usual_starter_count_home",
        "starter_turnover_rate_home",
        "previous_match_starting_xi_minutes_home",
        "cumulative_international_minutes_14d_home",
        "days_since_last_international_match_home",
        "matches_last_7d_home",
        "matches_last_14d_home",
    }.issubset(features)


def test_missing_or_incomplete_squad_never_becomes_fully_fit_default():
    snapshot = _snapshot()
    snapshot["teams"]["home"]["roster_complete"] = False
    snapshot["teams"]["home"]["expected_player_count"] = 13
    row = _extract(snapshot)
    assert row["home_data_quality"] == "LOW"
    assert row["features"]["squad_availability_home"] is None
    assert row["features"]["unavailable_player_count_home"] is None
    assert row["features"]["weighted_impact_lost_home"] is None

    national_roster = _snapshot()
    national_roster["teams"]["home"]["roster_scope"] = "national_roster"
    national_row = _extract(national_roster)
    assert national_row["home_data_quality"] == "LOW"
    assert national_row["features"]["squad_availability_home"] is None


def test_missing_match_history_is_not_converted_to_zero_load():
    snapshot = _snapshot()
    snapshot["teams"]["home"] = _side("home", out_index=0, include_history=False)
    snapshot["teams"]["away"] = _side("away", include_history=False)
    row = _extract(snapshot)
    features = row["features"]
    assert features["matches_last_7d_home"] is None
    assert features["matches_last_14d_home"] is None
    assert features["cumulative_international_minutes_7d_home"] is None
    assert features["days_since_last_international_match_home"] is None


def test_conflicting_status_claims_fail_to_numeric_features_without_arbitrary_resolution():
    snapshot = _snapshot()
    player = snapshot["teams"]["home"]["players"][0]
    player["status_claims"][1]["status"] = "fit"
    player["status_claims"][1]["availability"] = 1.0
    row = _extract(snapshot)
    assert row["home_data_quality"] == "LOW"
    assert row["home_conflicting_player_status_count"] == 1
    assert row["features"]["squad_availability_home"] is None


def test_future_and_mismatched_evidence_fail_closed():
    future = _snapshot()
    future["sources"][1]["observed_at"] = "2030-01-10T11:40:00Z"
    future["sources"][1]["source_as_of"] = "2030-01-10T11:40:00Z"
    with pytest.raises(
        research.ResearchContractError, match="future-dated|after prediction"
    ):
        _extract(future)

    mismatch = _snapshot()
    mismatch["fixture"]["away_team"] = "Spain"
    with pytest.raises(research.ResearchContractError, match="participant mismatch"):
        _extract(mismatch)


def test_confirmed_lineup_requires_eleven_and_lineup_features_are_cutoff_bound():
    short = _snapshot()
    short["teams"]["home"]["lineup"]["player_ids"] = short["teams"]["home"]["lineup"][
        "player_ids"
    ][:-1]
    with pytest.raises(research.ResearchContractError, match="exactly 11"):
        _extract(short)

    stale = _snapshot()
    stale["teams"]["home"]["lineup"]["announced_at"] = "2030-01-10T04:00:00Z"
    stale["teams"]["away"]["lineup"]["announced_at"] = "2030-01-10T04:00:00Z"
    # The line-up source itself is also moved back to a consistent older as-of.
    for source in stale["sources"]:
        if source["source_id"] == "lineup":
            source["observed_at"] = "2030-01-10T04:00:00Z"
            source["source_as_of"] = "2030-01-10T04:00:00Z"
    row = _extract(stale)
    assert row["home_data_quality"] == "HIGH"
    assert row["features"]["starting_xi_strength_home"] is None
    assert row["features"]["unavailable_starter_count_home"] is None


def test_historical_ablation_rejects_test_fixture_and_empty_data_is_unavailable():
    row = _extract()
    eval_row = {
        "evidence_kind": row["evidence_kind"],
        "fixture_id": row["fixture_id"],
        "kickoff_at": row["kickoff_at"],
        "prediction_at": row["prediction_at"],
        "snapshot_observed_at": row["snapshot_observed_at"],
        "snapshot_digest": row["snapshot_digest"],
        "data_quality": row["data_quality"],
        "outcome": 0,
        "predictions": {name: [0.5, 0.25, 0.25] for name in research.ABLATION_VARIANTS},
    }
    with pytest.raises(research.ResearchContractError, match="non-real evidence"):
        research.evaluate_causal_ablation([eval_row], target_match_count=512)

    unavailable = research.evaluate_causal_ablation([], target_match_count=512)
    assert unavailable["status"] == "not_evaluated"
    assert unavailable["reason"] == "HISTORICAL_POINT_IN_TIME_UNAVAILABLE"
    assert unavailable["paired_match_count"] == 0
    assert all(value["coverage"] == 0 for value in unavailable["variants"].values())


def test_metric_and_date_cluster_bootstrap_math_is_deterministic():
    rows = [
        {"outcome": 0, "probabilities": [0.7, 0.2, 0.1]},
        {"outcome": 2, "probabilities": [0.2, 0.2, 0.6]},
    ]
    metrics = research._probability_metrics(rows)["metrics"]
    assert metrics["brier_score_multiclass"] == pytest.approx(0.19)
    assert metrics["log_loss_multiclass"] > 0
    assert set(metrics["home_draw_away_calibration"]) == {"home", "draw", "away"}
    assert 0 <= metrics["expected_calibration_error_10_bins_mean_one_vs_rest"] <= 1

    paired = [
        {
            "kickoff_at": f"2030-01-{day:02d}T12:00:00Z",
            "outcome": outcome,
            "predictions": {
                "baseline": [0.5, 0.3, 0.2],
                "availability": [0.6, 0.25, 0.15],
            },
        }
        for day, outcome in ((1, 0), (2, 1), (3, 2))
    ]
    a = research._cluster_bootstrap_differences(
        paired, "availability", replicates=25, seed=7
    )
    b = research._cluster_bootstrap_differences(
        paired, "availability", replicates=25, seed=7
    )
    assert a == b
    assert a["cluster_count"] == 3


def test_append_only_capture_store_rejects_test_evidence_before_any_write(tmp_path):
    store = tmp_path / "private" / "captures.jsonl"
    store.parent.mkdir(mode=0o700)
    with pytest.raises(research.ResearchContractError, match="REAL_OBSERVED"):
        research.append_forward_capture(store, _snapshot(), capture_slot="T24H")
    assert not store.exists()


def test_local_research_report_uses_existing_512_match_audit_and_zero_pit_coverage():
    repo_root = Path(__file__).resolve().parents[2]
    report = research.build_local_research_report(
        repo_root,
        source_main_sha="0ab36238451b80d0478fd130ca6209e562a9bd98",
        generated_at="2026-09-29T08:00:00+00:00",
    )
    assert report["historical_sample"]["match_count"] == 512
    assert report["historical_sample"]["verified_historical_pit_squad_captures"] == 0
    assert report["research_status"] == "NL_SQUAD_FEATURES_FORWARD_EVIDENCE_REQUIRED"
    assert report["baseline_context_only"]["causal_squad_comparison"] is False
    assert all(
        v["matches_evaluated"] == 0 for v in report["feature_ablations"].values()
    )
    assert report["safety"]["provider_requests"] == 0
    assert report["safety"]["credential_accesses"] == 0
    assert report["report_digest"] == research.canonical_json_digest(
        {key: value for key, value in report.items() if key != "report_digest"}
    )


def test_module_has_no_production_hooks_or_network_client_imports():
    module = ast.parse(Path(research.__file__).read_text(encoding="utf-8"))
    imported_modules = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden = {
        "requests",
        "urllib",
        "httpx",
        "src.scanner",
        "src.runtime",
        "src.betting",
    }
    assert not any(
        imported == root or imported.startswith(f"{root}.")
        for imported in imported_modules
        for root in forbidden
    )
