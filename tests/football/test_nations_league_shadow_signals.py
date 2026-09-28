from __future__ import annotations

import ast
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.football.nations_league_shadow_signals as nl_signals
from src.betting.signal_contract import is_actionable_value_signal
from src.config import MAX_EV, MIN_EDGE

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = ROOT / "tests/fixtures/nations_league_public_incident_20260928.json"


@pytest.fixture(scope="module")
def frozen_bundle():
    return json.loads(SOURCE_PATH.read_text(encoding="utf-8"))


def _simple_fixture(home_probability: float) -> dict:
    return {
        "provider_event_id": "fixture-test-001",
        "home": "Home",
        "away": "Away",
        "model": {
            "probabilities": {
                "home": home_probability,
                "draw": 0.2,
                "away": 0.8 - home_probability,
            },
            "components": {
                "canonical_stacker": {
                    "home": home_probability,
                    "draw": 0.2,
                    "away": 0.8 - home_probability,
                },
                "raw_dixon_coles": {
                    "home": home_probability,
                    "draw": 0.2,
                    "away": 0.8 - home_probability,
                },
            },
        },
        "market": {
            "odds_decimal": {"home": 2.0, "draw": 4.0, "away": 2.0},
            "probabilities": {"home": 0.4, "draw": 0.2, "away": 0.4},
        },
    }


def test_exact_46_fixture_bundle_is_accepted(frozen_bundle):
    source = nl_signals.validate_frozen_public_bundle(frozen_bundle)
    assert source["public_digest"] == nl_signals.EXPECTED_PUBLIC_DIGEST
    assert source["fixture_count"] == 46
    assert source["captured_at"] == nl_signals.EXPECTED_CAPTURED_AT


def test_wrong_public_digest_is_rejected(frozen_bundle):
    tampered = dict(frozen_bundle, public_digest="0" * 64)
    with pytest.raises(nl_signals.ShadowCandidateError, match="digest"):
        nl_signals.validate_frozen_public_bundle(tampered)


def test_model_probability_binding_rejects_noncanonical_final_probability(
    frozen_bundle,
):
    fixture = dict(frozen_bundle["fixtures"][0])
    model = dict(fixture["model"])
    model["probabilities"] = dict(
        model["probabilities"],
        home=model["probabilities"]["home"] + 0.01,
        draw=model["probabilities"]["draw"] - 0.01,
    )
    fixture["model"] = model
    with pytest.raises(
        nl_signals.ShadowCandidateError, match="differ from canonical stacker"
    ):
        nl_signals._bound_fixture_model(fixture)


def test_detector_probability_and_odds_mapping_is_home_draw_away_correct(
    frozen_bundle, monkeypatch
):
    calls = []

    def fake_detect_value(home, away, model_probs, raw_odds, **kwargs):
        calls.append((home, away, model_probs.copy(), raw_odds, kwargs))
        return []

    monkeypatch.setattr(nl_signals, "detect_value", fake_detect_value)
    source_fixture = frozen_bundle["fixtures"][0]
    nl_signals.detect_fixture_candidates(source_fixture)
    home, away, model_probs, odds, kwargs = calls[0]
    assert (home, away) == (source_fixture["home"], source_fixture["away"])
    assert model_probs.tolist() == [
        source_fixture["model"]["probabilities"]["away"],
        source_fixture["model"]["probabilities"]["draw"],
        source_fixture["model"]["probabilities"]["home"],
    ]
    assert odds == tuple(
        source_fixture["market"]["odds_decimal"][side]
        for side in ("home", "draw", "away")
    )
    dc = source_fixture["model"]["components"]["raw_dixon_coles"]
    assert kwargs["dc_probs"] == {
        f"p_{side}": dc[side] for side in ("home", "draw", "away")
    }
    assert kwargs["min_edge"] == MIN_EDGE == 0.03
    assert kwargs["bankroll"] == 1000.0


def test_below_three_percent_ev_is_not_emitted():
    assert nl_signals.detect_fixture_candidates(_simple_fixture(0.5149)) == []


def test_qualifying_candidate_at_canonical_threshold_is_emitted():
    candidates = nl_signals.detect_fixture_candidates(_simple_fixture(0.515))
    home = [candidate for candidate in candidates if candidate["market"] == "home"]
    assert len(home) == 1
    assert home[0]["ev"] == pytest.approx(MIN_EDGE)


def test_candidate_ids_are_deterministic():
    fixture = _simple_fixture(0.56)
    first = nl_signals.detect_fixture_candidates(fixture)
    second = nl_signals.detect_fixture_candidates(fixture)
    assert [candidate["candidate_id"] for candidate in first] == [
        candidate["candidate_id"] for candidate in second
    ]
    for candidate in first:
        expected = nl_signals._candidate_id(
            nl_signals.EXPECTED_PUBLIC_DIGEST,
            fixture["provider_event_id"],
            candidate["market"],
        )
        assert candidate["candidate_id"] == expected


def test_selection_is_independent_of_generation_clock(frozen_bundle):
    first = nl_signals.build_shadow_candidate_artifact(
        frozen_bundle, generated_at=datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
    )
    second = nl_signals.build_shadow_candidate_artifact(
        frozen_bundle, generated_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    assert first["candidate_table"] == second["candidate_table"]
    assert [item["detected_candidates"] for item in first["fixtures"]] == [
        item["detected_candidates"] for item in second["fixtures"]
    ]
    assert first["summary"] == second["summary"]
    assert (
        first["provenance"]["candidate_generation_timestamp"]
        != second["provenance"]["candidate_generation_timestamp"]
    )


def test_candidate_is_non_actionable_under_canonical_contract(frozen_bundle):
    artifact = nl_signals.build_shadow_candidate_artifact(
        frozen_bundle, generated_at=datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
    )
    for candidate in artifact["candidate_table"]:
        assert candidate["shadow"] is True
        assert candidate["signal_status"] != "ACTIVE"
        assert candidate["no_bet_flag"] is True
        actionability_probe = {**candidate, "signal_id": candidate["candidate_id"]}
        actionable, reason = is_actionable_value_signal(
            actionability_probe, bankroll=EVAL_BANKROLL, active_bet_count=0
        )
        assert not actionable
        assert "signal_status" in reason


EVAL_BANKROLL = nl_signals.EVALUATION_BANKROLL_EUR


def test_source_and_detector_safety_flags_and_stake_label(frozen_bundle):
    artifact = nl_signals.build_shadow_candidate_artifact(
        frozen_bundle, generated_at=datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
    )
    assert artifact["safety"] == {
        "shadow": True,
        "no_bet": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "provider_requests": 0,
        "results_consumed": False,
        "live_scores_consumed": False,
    }
    assert artifact["evaluation_bankroll_eur"] == 1000.0
    assert all(
        candidate["stake_label"] == "THEORETICAL_EVALUATION_ONLY"
        for candidate in artifact["candidate_table"]
    )
    assert all(
        candidate["ledger_mutation"] is False
        for candidate in artifact["candidate_table"]
    )


def test_no_network_or_result_ingestion_dependency(monkeypatch, frozen_bundle):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden in candidate materialization")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    artifact = nl_signals.build_shadow_candidate_artifact(
        frozen_bundle, generated_at=datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
    )
    assert artifact["safety"]["provider_requests"] == 0
    assert artifact["safety"]["results_consumed"] is False
    assert artifact["safety"]["live_scores_consumed"] is False
    for path in (
        ROOT / "src/football/nations_league_shadow_signals.py",
        ROOT / "scripts/nations_league_shadow_signal_candidates.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert imported_roots.isdisjoint(
            {"requests", "httpx", "urllib", "socket", "settlement", "ledger"}
        )


def test_max_ev_safety_ceiling_is_applied():
    fixture = _simple_fixture(0.71)
    candidates = nl_signals.detect_fixture_candidates(fixture)
    assert all(candidate["ev"] <= MAX_EV for candidate in candidates)


def test_artifact_counts_and_sorted_candidate_table(frozen_bundle):
    artifact = nl_signals.build_shadow_candidate_artifact(
        frozen_bundle, generated_at=datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
    )
    assert artifact["fixture_count"] == artifact["summary"]["total_fixtures"] == 46
    assert len(artifact["candidate_table"]) == artifact["summary"]["total_candidates"]
    assert artifact["candidate_table"] == sorted(
        artifact["candidate_table"],
        key=lambda item: (-item["ev"], item["provider_event_id"], item["market"]),
    )
    digest_body = {
        key: value for key, value in artifact.items() if key != "artifact_digest"
    }
    assert artifact["artifact_digest"] == nl_signals._sha256_bytes(
        nl_signals._canonical_json(digest_body)
    )
