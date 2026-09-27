from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from requests import HTTPError

from src.config import LEAGUE_REGISTRY, canonical_name
from src.features.squad_context import tournament_stage_features
from src.scanner.nations_league_shadow import (
    ACTIVE_END_EXCLUSIVE,
    ACTIVE_START,
    SNAPSHOT_DIR,
    SNAPSHOT_FILES,
    SPORT_KEY,
    TOURNAMENT,
    NationsLeagueShadowError,
    _canonical_json,
    _event_market_odds,
    build_run_artifact,
    fetch_provider_events,
    load_frozen_snapshot,
    predict_fixture,
    write_immutable_artifact,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def _event(**overrides):
    event = {
        "id": "unl-event-1",
        "sport_key": SPORT_KEY,
        "sport_title": TOURNAMENT,
        "commence_time": "2026-10-05T18:45:00Z",
        "home_team": "England",
        "away_team": "France",
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "England", "price": 2.45},
                            {"name": "Draw", "price": 3.15},
                            {"name": "France", "price": 3.05},
                        ],
                    }
                ],
            }
        ],
    }
    event.update(overrides)
    return event


def _history():
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-08-01"),
                "home_team": "England",
                "away_team": "France",
                "home_score": 1,
                "away_score": 0,
                "tournament": "UEFA Nations League",
                "neutral": False,
            }
        ]
    )


def test_registry_and_active_window_are_shadow_only():
    config = LEAGUE_REGISTRY[SPORT_KEY]
    assert config["name"] == TOURNAMENT
    assert config["start_date"] == ACTIVE_START.date().isoformat()
    assert config["end_date"] == "2026-11-17"
    assert config["category_mode"] == "shadow"
    assert config["min_edge"] >= 1.0
    assert ACTIVE_END_EXCLUSIVE.date().isoformat() == "2026-11-18"


def test_nations_league_stage_is_league_phase_not_wm_knockout():
    features = tournament_stage_features(pd.Timestamp("2026-10-05"), TOURNAMENT)
    assert features == {
        "is_group_stage": 1.0,
        "is_knockout": 0.0,
        "is_final": 0.0,
        "draw_incentive": 0.0,
    }
    outside_window = tournament_stage_features(pd.Timestamp("2026-07-10"), TOURNAMENT)
    assert outside_window["is_group_stage"] == 0.0
    assert outside_window["is_knockout"] == 0.0


def test_frozen_wm2026_snapshot_cross_contracts_load_exactly():
    snapshot = load_frozen_snapshot()
    assert snapshot.identity == "wm2026-frozen-2026-07-31"
    assert len(snapshot.digest) == 64
    assert set(snapshot.file_digests) == {
        "README.md",
        "anchor.json",
        "calibrators.pkl",
        "cluster_calibrators.pkl",
        "conformal.pkl",
        "dc_current_elo.json",
        "dc_lifecycle.json",
        "dc_params_final.pkl",
        "feature_columns.json",
        "gate.json",
        "metadata.json",
        "model.pkl",
        "stacker.pkl",
        "stacker_features.json",
    }
    assert len(snapshot.feature_columns) == 91
    assert len(snapshot.stacker_columns) == 17
    assert set(snapshot.dc_params.attack) == set(snapshot.elo_ratings)


def test_frozen_snapshot_column_drift_fails_closed(tmp_path):
    for name in SNAPSHOT_FILES:
        source = SNAPSHOT_DIR / name
        target = tmp_path / name
        if name == "feature_columns.json":
            columns = json.loads(source.read_text())
            columns.pop()
            target.write_text(json.dumps(columns))
        else:
            target.symlink_to(source)
    with pytest.raises(NationsLeagueShadowError, match="feature-column order disagree"):
        load_frozen_snapshot(tmp_path)


def test_provider_request_is_exactly_h2h_eu_and_uses_target_sport_key():
    observed = {}

    class Response:
        def __init__(self):
            self.status_code = 200
            self.headers = {"x-requests-used": "12", "x-requests-remaining": "88"}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return [_event()]

    def transport(url, *, params, timeout, allow_redirects):
        observed.update(
            url=url, params=params, timeout=timeout, allow_redirects=allow_redirects
        )
        return Response()

    recorded = []
    events, requests_made, retries, descriptor = fetch_provider_events(
        api_key="test-only",
        transport=transport,
        budget_gate=lambda: True,
        success_recorder=lambda remaining: recorded.append(remaining),
        usage_logger=lambda used, remaining: recorded.append((used, remaining)),
        sleeper=lambda _: pytest.fail("successful fake response should not retry"),
    )
    assert len(events) == 1
    assert requests_made == 1
    assert retries == 0
    assert observed["url"].endswith(f"/sports/{SPORT_KEY}/odds")
    assert observed["params"] == {
        "apiKey": "test-only",
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    assert observed["allow_redirects"] is False
    assert descriptor["query"] == {
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    assert descriptor["credential"] == "apiKey (redacted)"
    assert recorded == [(12, 88), 88]


def test_provider_budget_block_makes_zero_http_or_credential_calls():
    calls = {"transport": 0, "credential": 0}

    def transport(*args, **kwargs):
        calls["transport"] += 1
        raise AssertionError("budget-blocked request reached transport")

    def credential_loader(_key):
        calls["credential"] += 1
        return "not-used"

    with pytest.raises(NationsLeagueShadowError, match="budget circuit"):
        fetch_provider_events(
            transport=transport,
            budget_gate=lambda: False,
            credential_loader=credential_loader,
        )
    assert calls == {"transport": 0, "credential": 0}


def test_provider_retry_is_bounded_and_counted_without_redirects():
    statuses = [503, 200]
    requests_seen = []

    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise HTTPError("transient upstream failure")

        @staticmethod
        def json():
            return [_event()]

    def transport(url, **kwargs):
        requests_seen.append(kwargs)
        return Response(statuses.pop(0))

    events, request_count, retry_count, _ = fetch_provider_events(
        api_key="test-only",
        transport=transport,
        budget_gate=lambda: True,
        success_recorder=lambda _remaining: None,
        sleeper=lambda _delay: None,
    )
    assert len(events) == 1
    assert request_count == 2
    assert retry_count == 1
    assert len(requests_seen) == 2
    assert all(call["allow_redirects"] is False for call in requests_seen)


def test_auth_failure_opens_circuit_and_does_not_retry():
    circuit = []
    requests_seen = []

    class Response:
        def __init__(self):
            self.status_code = 401
            self.headers = {}

    with pytest.raises(NationsLeagueShadowError, match="HTTP 401"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: (
                requests_seen.append(True) or Response()
            ),
            budget_gate=lambda: True,
            error_recorder=lambda code, opened: circuit.append((code, opened)),
            sleeper=lambda _delay: pytest.fail("auth failure must not retry"),
        )
    assert requests_seen == [True]
    assert circuit == [(401, True)]


def test_success_accounting_or_payload_failure_never_retries_paid_request():
    class Response:
        def __init__(self):
            self.status_code = 200
            self.headers = {}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            raise ValueError("malformed payload")

    seen = []
    with pytest.raises(NationsLeagueShadowError, match="accounted for"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: seen.append("account") or Response(),
            budget_gate=lambda: True,
            success_recorder=lambda _remaining: (_ for _ in ()).throw(
                OSError("state write")
            ),
            sleeper=lambda _delay: pytest.fail("successful response must not retry"),
        )
    assert seen == ["account"]

    seen.clear()
    with pytest.raises(NationsLeagueShadowError, match="malformed JSON"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: seen.append("decode") or Response(),
            budget_gate=lambda: True,
            success_recorder=lambda _remaining: None,
            sleeper=lambda _delay: pytest.fail("malformed response must not retry"),
        )
    assert seen == ["decode"]


def test_h2h_requires_exactly_three_valid_outcomes_and_prefers_pinnacle():
    event = _event(
        bookmakers=[
            {
                "key": "zeta",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "England", "price": 2.4},
                            {"name": "Draw", "price": 3.1},
                            {"name": "France", "price": 3.0},
                        ],
                    }
                ],
            },
            _event()["bookmakers"][0],
        ]
    )
    odds, reason = _event_market_odds(event)
    assert reason is None
    assert odds["bookmaker"] == "pinnacle"
    assert (odds["home"], odds["draw"], odds["away"]) == (2.45, 3.15, 3.05)

    missing = _event(bookmakers=[])
    assert _event_market_odds(missing) == (None, "missing_h2h_odds")
    malformed = _event(
        bookmakers=[
            {
                "key": "bad",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "England", "price": 2.0},
                            {"name": "Draw", "price": "not-a-price"},
                            {"name": "France", "price": 3.0},
                        ],
                    }
                ],
            }
        ]
    )
    assert _event_market_odds(malformed) == (None, "malformed_h2h_odds")
    duplicate = _event(
        bookmakers=[
            {
                "key": "bad",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "England", "price": 2.0},
                            {"name": "Draw", "price": 3.0},
                            {"name": "England", "price": 3.0},
                        ],
                    }
                ],
            }
        ]
    )
    assert _event_market_odds(duplicate) == (None, "malformed_h2h_odds")


def test_fixture_inference_uses_home_advantage_and_separate_raw_models(monkeypatch):
    from src.scanner import nations_league_shadow as shadow

    snapshot = load_frozen_snapshot()
    event = _event()
    odds, reason = _event_market_odds(event)
    assert reason is None
    original_stacker_features = shadow.build_stacker_features
    stacker_inputs = []

    def capture_stacker_inputs(**kwargs):
        stacker_inputs.append(kwargs.copy())
        return original_stacker_features(**kwargs)

    monkeypatch.setattr(shadow, "build_stacker_features", capture_stacker_inputs)
    result = predict_fixture(
        event=event,
        odds=odds,
        snapshot=snapshot,
        historical=_history(),
        captured_at=NOW,
    )
    assert canonical_name("USA") == "United States"
    assert result["neutral"] is False
    assert result["tournament"] == TOURNAMENT
    probabilities = result["probabilities"]
    assert set(probabilities["raw_dixon_coles"]) == {"home", "draw", "away"}
    assert set(probabilities["raw_gbt"]) == {"home", "draw", "away"}
    assert probabilities["canonical_stacker"] == probabilities["final_ensemble"]
    assert probabilities["market_anchored"] is None
    assert (
        result["market_anchor_status"]
        == "not_applied_unbound_to_frozen_stacker_contract"
    )
    assert len(stacker_inputs) == 1
    assert stacker_inputs[0]["lgbm_probs"] is None
    assert stacker_inputs[0]["is_neutral"] is False
    assert stacker_inputs[0]["is_knockout"] is False
    alias_event = _event(
        home_team="USA",
        bookmakers=[
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "USA", "price": 2.45},
                            {"name": "Draw", "price": 3.15},
                            {"name": "France", "price": 3.05},
                        ],
                    }
                ],
            }
        ],
    )
    alias_odds, alias_error = _event_market_odds(alias_event)
    assert alias_error is None
    alias_result = predict_fixture(
        event=alias_event,
        odds=alias_odds,
        snapshot=snapshot,
        historical=_history(),
        captured_at=NOW,
    )
    assert alias_result["home_team"] == "United States"
    for distribution in (
        probabilities["raw_dixon_coles"],
        probabilities["raw_gbt"],
        probabilities["canonical_stacker"],
        result["market"]["margin_free_probabilities"],
    ):
        assert set(distribution) == {"home", "draw", "away"}
        assert sum(distribution.values()) == pytest.approx(1.0, abs=1e-6)
    assert set(result["model_vs_market_edge_percentage_points"]) == {
        "home",
        "draw",
        "away",
    }
    assert all(
        not any(term in key.casefold() for term in ("stake", "kelly", "bet"))
        for key in result
    )


def test_artifact_filters_tournament_fails_closed_and_binds_digest():
    snapshot = load_frozen_snapshot()
    events = [
        _event(),
        _event(id="other-sport", sport_key="soccer_uefa_champs_league"),
        _event(id="other-title", sport_title="UEFA Champions League"),
        _event(id="unknown-team", home_team="Unmodelled Nation"),
        _event(id="missing-odds", bookmakers=[]),
        _event(id="outside-window", commence_time="2026-11-18T18:45:00Z"),
    ]
    artifact = build_run_artifact(
        events=events,
        snapshot=snapshot,
        historical=_history(),
        history_provenance={"sha256": "a" * 64, "match_count": 1},
        source_sha="b" * 40,
        captured_at=NOW,
        request_count=1,
        retry_count=0,
        request_descriptor={
            "method": "GET",
            "query": {"regions": "eu", "markets": "h2h"},
        },
    )
    assert artifact["schema"] == "nations-league-shadow-v1"
    assert artifact["provider"] == "the_odds_api"
    assert artifact["sport_key"] == SPORT_KEY
    assert artifact["request_count"] == 1
    assert artifact["retry_count"] == 0
    assert artifact["provider_event_count"] == 6
    assert artifact["fixture_count"] == 4
    assert artifact["covered_fixture_count"] == 1
    assert {item["reason"] for item in artifact["skipped_fixtures"]} >= {
        "non_target_sport_key",
        "non_target_tournament",
        "unknown_model_team",
        "missing_h2h_odds",
        "outside_active_window",
    }
    assert artifact["shadow"] is True
    assert artifact["no_bet"] is True
    assert artifact["publication"] is False
    assert artifact["ledger_mutation"] is False
    assert len(artifact["artifact_digest"]) == 64
    unsigned = {
        key: value for key, value in artifact.items() if key != "artifact_digest"
    }
    assert (
        artifact["artifact_digest"]
        == hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    )
    fixture = artifact["fixtures"][0]
    assert fixture["neutral"] is False
    assert fixture["tournament"] == TOURNAMENT
    serialized = json.dumps(fixture).casefold()
    assert "stake" not in serialized and "kelly" not in serialized


def test_unknown_canonical_team_alias_fails_closed_in_fixture_coverage():
    snapshot = load_frozen_snapshot()
    artifact = build_run_artifact(
        events=[_event(home_team="No Such Team")],
        snapshot=snapshot,
        historical=_history(),
        history_provenance={},
        source_sha="c" * 40,
        captured_at=NOW,
        request_count=1,
        retry_count=0,
        request_descriptor={},
    )
    assert artifact["covered_fixture_count"] == 0
    assert artifact["skipped_fixtures"] == [
        {"provider_event_id": "unl-event-1", "reason": "unknown_model_team"}
    ]


def test_artifact_file_is_exclusive_and_immutable(tmp_path):
    artifact = {"schema": "nations-league-shadow-v1", "artifact_digest": "digest"}
    path = write_immutable_artifact(tmp_path / "run.json", artifact)
    assert json.loads(path.read_text()) == artifact
    with pytest.raises(NationsLeagueShadowError, match="already exists"):
        write_immutable_artifact(path, {"schema": "replacement"})
    assert json.loads(path.read_text()) == artifact


def test_shadow_modules_have_no_betting_publication_scheduler_or_cloudflare_mutators():
    module = ROOT / "src" / "scanner" / "nations_league_shadow.py"
    tree = ast.parse(module.read_text())
    forbidden_modules = {
        "src.betting.ledger",
        "src.notifications.web_push",
        "src.scanner.daily_scan",
        "src.notifications.public_serializer",
        "src.runtime.scheduler",
        "src.cloudflare",
        "src.analysis.empirical_prior",
    }
    imports = set()
    forbidden_calls = {
        "append_bets",
        "settle_from_results",
        "send_scan_alert",
        "send_bet_alert",
        "publish_signals",
        "register_job",
        "reload_launch_agents",
        "deploy_worker",
    }
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            calls.add(name)
    assert not imports.intersection(forbidden_modules)
    assert not calls.intersection(forbidden_calls)


def test_shared_scoring_kernel_propagates_venue_state_and_preserves_wm(monkeypatch):
    import src.features.builder as feature_builder
    from src.betting.odds_utils import remove_margin_shin
    from src.ensemble import stacking
    from src.models import lgbm_model
    from src.scanner import scoring

    outcome = remove_margin_shin((2.45, 3.15, 3.05))
    fair = {"home": outcome[0], "draw": outcome[1], "away": outcome[2]}
    calls = {
        "dc": [],
        "feature": [],
        "stacker": [],
        "elo": [],
        "scoreline": [],
        "totals": [],
        "ah": [],
        "goals": [],
        "halves": [],
        "ftts": [],
        "venue": [],
    }

    def dc_match(*_args, **kwargs):
        calls["dc"].append(kwargs["neutral"])
        calls["venue"].append(
            (
                kwargs.get("host_boost"),
                kwargs.get("altitude_factors"),
                kwargs.get("turf_penalty"),
            )
        )
        return {"p_home": fair["home"], "p_draw": fair["draw"], "p_away": fair["away"]}

    def feature_row(**kwargs):
        calls["feature"].append(kwargs["neutral"])
        return {"feature": 1.0}

    def stacker_features(*_args, **kwargs):
        calls["stacker"].append(kwargs["is_neutral"])
        return np.zeros(17)

    class FakeStacker:
        @staticmethod
        def predict_proba(_values):
            return np.array([[fair["away"], fair["draw"], fair["home"]]])

    monkeypatch.setattr(scoring.dc, "predict_match_staged", dc_match)
    monkeypatch.setattr(scoring.dc, "get_stage_rho", lambda *_args, **_kwargs: -0.1)
    monkeypatch.setattr(
        scoring.dc,
        "predict_scoreline",
        lambda *_args, **kwargs: (
            calls["scoreline"].append(kwargs["neutral"]) or np.full((2, 2), 0.25)
        ),
    )
    monkeypatch.setattr(
        scoring.dc,
        "predict_totals_all",
        lambda *_args, **kwargs: (
            calls["totals"].append(kwargs["neutral"])
            or {"quarter_ball": False, "p_over": 0.5}
        ),
    )
    monkeypatch.setattr(
        scoring.dc,
        "predict_asian_handicap_all",
        lambda *_args, **kwargs: (
            calls["ah"].append(kwargs["neutral"])
            or {"quarter_ball": False, "p_ah_home": 0.5}
        ),
    )
    monkeypatch.setattr(
        scoring.dc,
        "predict_goals_range",
        lambda *_args, **kwargs: (
            calls["goals"].append(kwargs["neutral"]) or {"p_in": 0.5}
        ),
    )
    monkeypatch.setattr(
        scoring.dc,
        "predict_half_goals_range",
        lambda *_args, **kwargs: (
            calls["halves"].append(kwargs["neutral"]) or {"p_in": 0.5}
        ),
    )
    monkeypatch.setattr(
        scoring.dc,
        "predict_first_scorer",
        lambda *_args, **kwargs: calls["ftts"].append(kwargs["neutral"]) or {},
    )
    monkeypatch.setattr(
        scoring,
        "elo_win_probability",
        lambda *_args, **kwargs: (
            calls["elo"].append(kwargs["neutral"])
            or (fair["home"], fair["draw"], fair["away"])
        ),
    )
    monkeypatch.setattr(feature_builder, "build_feature_row", feature_row)
    monkeypatch.setattr(stacking, "build_stacker_features", stacker_features)
    monkeypatch.setattr(
        lgbm_model,
        "predict_proba",
        lambda *_args: np.array(
            [
                [
                    fair["away"],
                    fair["draw"],
                    fair["home"],
                ]
            ]
        ),
    )
    monkeypatch.setattr(scoring, "squad_report", lambda *_args: object())
    monkeypatch.setattr(scoring, "_squad_adjust", lambda probs, *_args: probs)
    monkeypatch.setattr(scoring, "_rank_adjust", lambda probs, *_args: probs)
    monkeypatch.setattr(scoring, "_form_context", lambda *_args: {})
    monkeypatch.setattr(scoring, "_top_scorelines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        scoring, "_apply_ledger_performance_gate", lambda signals: signals
    )
    monkeypatch.setattr(scoring, "detect_value", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scoring, "detect_value_totals", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scoring, "detect_value_ah", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scoring, "detect_value_ftts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        scoring, "derive_goals_range_implied", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(scoring, "GOALS_RANGE_ENABLED", True)
    monkeypatch.setattr(scoring, "HOST_BOOST_ENABLED", True)
    monkeypatch.setattr(scoring, "HOST_LAMBDA_BOOST", 1.2)
    monkeypatch.setattr(scoring, "HOST_NATIONS", {"United States"})
    monkeypatch.setattr(scoring, "ALTITUDE_BOOST_MAP", {"United States": (1.1, 1.1)})
    monkeypatch.setattr(scoring, "ARTIFICIAL_TURF_STADIUMS", {"United States"})
    monkeypatch.setattr(scoring, "TURF_AWAY_PENALTY", 0.8)

    match = {
        "match_id": "neutral-test",
        "home_team": "United States",
        "away_team": "France",
        "commence_time": "2026-10-05T18:45:00Z",
        "home_odds": 2.45,
        "draw_odds": 3.15,
        "away_odds": 3.05,
        "sport_key": SPORT_KEY,
        "tournament": TOURNAMENT,
        "neutral": False,
        "totals_lines": {"2.5": {"over": 2.0, "under": 2.0}},
        "spreads": {"-0.5": {"home": 2.0, "away": 2.0}},
        "ftts_home_odds": 2.0,
        "ftts_away_odds": 2.0,
    }
    models = {
        "dc_params": object(),
        "lgbm_model": SimpleNamespace(feature_names_in_=["feature"]),
        "calibrators": None,
        "cluster_calibrators": None,
        "dc_weight": 0.5,
        "stacker": FakeStacker(),
        "conformal": None,
    }
    data = {
        "historical": pd.DataFrame([{"date": pd.Timestamp("2026-08-01")}]),
        "elo_series": pd.DataFrame(),
        "elo_ratings": {"United States": 1600.0, "France": 1700.0},
        "statsbomb_xg": pd.DataFrame(),
        "player_xg_df": pd.DataFrame(),
        "ppda_df": pd.DataFrame(),
        "fotmob_ratings_df": pd.DataFrame(),
    }

    def run_one(fixture):
        before = {name: len(values) for name, values in calls.items()}
        scoring.score_matches(
            [fixture],
            models,
            data,
            bankroll=1000.0,
            scan_date=pd.Timestamp("2026-09-27"),
        )
        return {name: values[before[name] :] for name, values in calls.items()}

    nl = run_one(match)
    assert nl["dc"] == nl["feature"] == nl["stacker"] == nl["elo"] == [False]
    assert nl["scoreline"] == nl["totals"] == nl["ah"] == nl["goals"] == [False]
    assert nl["halves"] == [False, False]
    assert nl["ftts"] == [False]
    assert nl["venue"] == [(1.0, None, 1.0)]

    wm_match = {
        **match,
        "match_id": "wm-neutral-default",
        "sport_key": "soccer_fifa_world_cup",
        "tournament": "FIFA World Cup",
        "commence_time": "2026-07-10T18:45:00Z",
    }
    wm_match.pop("neutral")
    wm = run_one(wm_match)
    assert wm["dc"] == wm["feature"] == wm["stacker"] == wm["elo"] == [True]
    assert wm["scoreline"] == wm["totals"] == wm["ah"] == wm["goals"] == [True]
    assert wm["halves"] == [True, True]
    assert wm["ftts"] == [True]
    assert wm["venue"] == [(1.2, (1.1, 1.1), 0.8)]
