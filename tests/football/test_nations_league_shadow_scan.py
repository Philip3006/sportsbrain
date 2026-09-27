from __future__ import annotations

import ast
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest
import requests

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
    run_shadow_scan,
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
        credential_loader=lambda key: key,
        success_recorder=lambda remaining: recorded.append(remaining),
        usage_logger=lambda used, remaining: recorded.append((used, remaining)),
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


def test_provider_503_fails_after_exactly_one_request():
    requests_seen = []

    class Response:
        status_code = 503
        headers: ClassVar[dict[str, str]] = {}

    with pytest.raises(NationsLeagueShadowError, match="HTTP 503"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: requests_seen.append(1) or Response(),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
        )
    assert requests_seen == [1]


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
def test_provider_transport_failure_fails_after_exactly_one_request(failure):
    requests_seen = []

    def transport(*_args, **_kwargs):
        requests_seen.append(1)
        raise failure("synthetic network failure")

    with pytest.raises(NationsLeagueShadowError, match="single h2h request failed"):
        fetch_provider_events(
            api_key="test-only",
            transport=transport,
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
        )
    assert requests_seen == [1]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_quota_failures_open_circuit_after_one_request(status):
    circuit = []
    requests_seen = []

    class Response:
        status_code = status
        headers: ClassVar[dict[str, str]] = {}

    with pytest.raises(NationsLeagueShadowError, match=f"HTTP {status}"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: requests_seen.append(1) or Response(),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
            error_recorder=lambda code, opened: circuit.append((code, opened)),
        )
    assert requests_seen == [1]
    assert circuit == [(status, True)]


@pytest.mark.parametrize("status", [301, 302])
def test_provider_redirect_fails_after_exactly_one_request(status):
    requests_seen = []

    class Response:
        status_code = status
        headers: ClassVar[dict[str, str]] = {}

    with pytest.raises(NationsLeagueShadowError, match="unexpected redirect"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **kwargs: (
                requests_seen.append(kwargs) or Response()
            ),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
        )
    assert len(requests_seen) == 1
    assert requests_seen[0]["allow_redirects"] is False


@pytest.mark.parametrize("status", [400, 404])
def test_other_client_errors_fail_after_exactly_one_request(status):
    requests_seen = []

    class Response:
        status_code = status
        headers: ClassVar[dict[str, str]] = {}

    with pytest.raises(NationsLeagueShadowError, match=f"HTTP {status}"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: requests_seen.append(1) or Response(),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
        )
    assert requests_seen == [1]


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "omitted required x-requests-used"),
        ({"x-requests-used": "12"}, "omitted required x-requests-remaining"),
        (
            {"x-requests-used": "bad", "x-requests-remaining": "88"},
            "malformed x-requests-used",
        ),
        (
            {"x-requests-used": "12", "x-requests-remaining": "bad"},
            "malformed x-requests-remaining",
        ),
        (
            {"x-requests-used": "-1", "x-requests-remaining": "88"},
            "negative x-requests-used",
        ),
        (
            {"x-requests-used": "12", "x-requests-remaining": "-1"},
            "negative x-requests-remaining",
        ),
    ],
)
def test_invalid_quota_headers_fail_after_one_request_without_accounting(
    headers, message
):
    requests_seen = []
    usage = []
    success = []

    class Response:
        status_code = 200

        def __init__(self):
            self.headers = headers

        @staticmethod
        def json():
            return [_event()]

    with pytest.raises(NationsLeagueShadowError, match=message):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: requests_seen.append(1) or Response(),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
            usage_logger=lambda used, remaining: usage.append((used, remaining)),
            success_recorder=lambda remaining: success.append(remaining),
        )
    assert requests_seen == [1]
    assert usage == []
    assert success == []


def test_zero_remaining_is_persisted_and_completed_response_is_accepted(
    tmp_path, monkeypatch
):
    from src.signals import provider_budget

    quota_path = tmp_path / "api_usage.json"
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", quota_path)
    monkeypatch.setattr(
        provider_budget, "_BUDGET_PATH", tmp_path / "provider_budget.json"
    )
    usage_log = []
    success = []
    fake_odds_api = ModuleType("src.data.odds_api")
    fake_odds_api._log_usage = lambda used, remaining: (
        usage_log.append((used, remaining))
        or provider_budget.persist_odds_api_quota_usage(
            used,
            remaining,
            source="the_odds_api_response_headers",
            path=quota_path,
        )
    )
    monkeypatch.setitem(sys.modules, "src.data.odds_api", fake_odds_api)

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "x-requests-used": "500",
            "x-requests-remaining": "0",
        }

        @staticmethod
        def json():
            return [_event()]

    events, request_count, retry_count, _ = fetch_provider_events(
        api_key="test-only",
        transport=lambda *_args, **_kwargs: Response(),
        budget_gate=lambda: True,
        credential_loader=lambda _key: "synthetic-key",
        success_recorder=lambda remaining: success.append(remaining),
    )
    assert events == [_event()]
    assert request_count == 1
    assert retry_count == 0
    assert usage_log == [(500, 0)]
    assert success == [0]
    quota = json.loads(quota_path.read_text())
    assert quota["requests_used"] == 500
    assert quota["requests_remaining"] == 0
    assert quota["state"] == "QUOTA_EXHAUSTED"
    assert quota["source"] == "the_odds_api_response_headers"
    assert provider_budget.is_provider_available("the_odds_api") is False


def test_successful_response_quota_persistence_failure_fails_closed_after_one_call():
    requests_seen = []
    success = []

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "x-requests-used": "12",
            "x-requests-remaining": "88",
        }

        @staticmethod
        def json():
            return [_event()]

    with pytest.raises(
        NationsLeagueShadowError, match="quota evidence could not be persisted"
    ):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: requests_seen.append(1) or Response(),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
            usage_logger=lambda *_args: (_ for _ in ()).throw(OSError("write failed")),
            success_recorder=lambda remaining: success.append(remaining),
        )
    assert requests_seen == [1]
    assert success == []


def test_success_recorder_or_malformed_payload_failure_never_retries():
    requests_seen = []

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "x-requests-used": "12",
            "x-requests-remaining": "88",
        }

        def __init__(self, malformed=False):
            self.malformed = malformed

        def json(self):
            if self.malformed:
                raise ValueError("malformed payload")
            return [_event()]

    with pytest.raises(NationsLeagueShadowError, match="could not be accounted for"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: (
                requests_seen.append("account") or Response()
            ),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
            usage_logger=lambda _used, _remaining: None,
            success_recorder=lambda _remaining: (_ for _ in ()).throw(
                OSError("circuit state write")
            ),
        )
    assert requests_seen == ["account"]

    requests_seen.clear()
    with pytest.raises(NationsLeagueShadowError, match="malformed JSON"):
        fetch_provider_events(
            api_key="test-only",
            transport=lambda *_args, **_kwargs: (
                requests_seen.append("decode") or Response(malformed=True)
            ),
            budget_gate=lambda: True,
            credential_loader=lambda _key: "synthetic-key",
            usage_logger=lambda _used, _remaining: None,
            success_recorder=lambda _remaining: pytest.fail(
                "malformed payload must not be recorded as a successful fetch"
            ),
        )
    assert requests_seen == ["decode"]


def test_missing_quota_header_prevents_artifact_creation(tmp_path, monkeypatch):
    import src.scanner.nations_league_shadow as shadow
    from src.signals import provider_budget

    snapshot = load_frozen_snapshot()
    monkeypatch.setattr(shadow, "load_frozen_snapshot", lambda _path: snapshot)
    monkeypatch.setattr(
        shadow,
        "load_cached_history",
        lambda *_args, **_kwargs: (_history(), {"sha256": "a" * 64}),
    )
    monkeypatch.setattr(shadow, "current_source_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(shadow, "_default_api_key_loader", lambda _key: "synthetic-key")
    monkeypatch.setattr(provider_budget, "is_provider_available", lambda _name: True)

    calls = []
    artifact_path = tmp_path / "must-not-exist.json"

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"x-requests-used": "12"}

        @staticmethod
        def json():
            return [_event()]

    with pytest.raises(NationsLeagueShadowError, match="x-requests-remaining"):
        run_shadow_scan(
            snapshot_dir=SNAPSHOT_DIR,
            history_path=tmp_path / "history.pkl",
            source_root=ROOT,
            output_path=artifact_path,
            provider_transport=lambda *_args, **_kwargs: calls.append(1) or Response(),
        )
    assert calls == [1]
    assert not artifact_path.exists()


def test_quota_persistence_failure_prevents_artifact_creation(tmp_path, monkeypatch):
    import src.scanner.nations_league_shadow as shadow
    from src.signals import provider_budget

    snapshot = load_frozen_snapshot()
    monkeypatch.setattr(shadow, "load_frozen_snapshot", lambda _path: snapshot)
    monkeypatch.setattr(
        shadow,
        "load_cached_history",
        lambda *_args, **_kwargs: (_history(), {"sha256": "a" * 64}),
    )
    monkeypatch.setattr(shadow, "current_source_sha", lambda _root: "b" * 40)
    monkeypatch.setattr(shadow, "_default_api_key_loader", lambda _key: "synthetic-key")
    monkeypatch.setattr(provider_budget, "is_provider_available", lambda _name: True)

    fake_odds_api = ModuleType("src.data.odds_api")

    def fail_quota_persistence(_used, _remaining):
        raise OSError("synthetic quota persistence failure")

    fake_odds_api._log_usage = fail_quota_persistence
    monkeypatch.setitem(sys.modules, "src.data.odds_api", fake_odds_api)
    calls = []
    artifact_path = tmp_path / "must-not-exist.json"

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "x-requests-used": "12",
            "x-requests-remaining": "88",
        }

        @staticmethod
        def json():
            return [_event()]

    with pytest.raises(
        NationsLeagueShadowError, match="quota evidence could not be persisted"
    ):
        run_shadow_scan(
            snapshot_dir=SNAPSHOT_DIR,
            history_path=tmp_path / "history.pkl",
            source_root=ROOT,
            output_path=artifact_path,
            provider_transport=lambda *_args, **_kwargs: calls.append(1) or Response(),
        )
    assert calls == [1]
    assert not artifact_path.exists()


def test_provider_fetch_has_one_transport_call_site_and_no_retry_loop():
    module = ROOT / "src" / "scanner" / "nations_league_shadow.py"
    tree = ast.parse(module.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "fetch_provider_events"
    )
    transport_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "request_transport"
    ]
    assert len(transport_calls) == 1
    assert not any(
        isinstance(node, (ast.For, ast.While)) for node in ast.walk(function)
    )


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
    neutral_result = predict_fixture(
        event=event,
        odds=odds,
        snapshot=snapshot,
        historical=_history(),
        captured_at=NOW,
        neutral=True,
    )
    assert neutral_result["neutral"] is True
    assert stacker_inputs[-1]["is_neutral"] is True
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
