from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.nations_league_live_edge import build_market_snapshot
from src.analysis.nations_league_live_runtime import (
    NationsLeagueLiveRuntimeError,
    append_live_store,
    build_fresh_input_state,
    build_live_prediction,
    due_state,
    load_active_release,
    refresh_and_activate,
    refresh_result_state,
    run_live_cycle,
)
from src.notifications.nations_league_public import (
    select_freshest_valid_public_nations_league,
    validate_public_nations_league,
)
from src.notifications.public_serializer import serialize_public_product

ROOT = Path(__file__).parents[2]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_live_release_rehydrates_the_single_committed_active_pointer():
    release = load_active_release(ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    assert release.status == "ACTIVE"
    assert release.release_id == "78161c4097c06e596efa95721aa62db0ed72a057a3fd664cbecd32f0c0be29bb"
    assert release.snapshot.training_row_count == 566


@pytest.mark.parametrize(
    ("kickoff", "as_of", "expected"),
    [
        ("2026-10-02T18:45:00Z", "2026-10-01T16:44:59Z", "TOO_EARLY"),
        ("2026-10-02T18:45:00Z", "2026-10-01T16:45:00Z", "INITIAL_DUE"),
        ("2026-10-01T18:45:00Z", "2026-10-01T20:45:00Z", "STARTED"),
    ],
)
def test_live_due_window_boundaries(kickoff, as_of, expected):
    assert due_state(kickoff, as_of) == expected


def test_live_plan_does_not_call_prediction_builder():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    state = _json("results/research/nations_league_v1_1_input_state_20260930T200124Z.json")
    release = load_active_release(ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    called = []

    def forbidden(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("plan mode must not predict")

    result = run_live_cycle(
        manifest,
        state,
        release,
        as_of="2026-10-01T16:45:00Z",
        prediction_builder=forbidden,
        execute=False,
    )
    assert result["appended_count"] == 0
    assert called == []
    assert any(row["status"] == "DUE" for row in result["predictions"])


def test_live_execute_is_release_bound_and_idempotent_with_explicit_builder():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    state = _json("results/research/nations_league_v1_1_input_state_20260930T200124Z.json")
    release = load_active_release(ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    fixture_id = state["fixtures"][0]["fixture_id"]
    manifest = {**manifest, "fixtures": [row for row in manifest["fixtures"] if row["fixture_id"] == fixture_id]}

    def builder(_state, identity, *, phase, active_release, captured_at):
        return {
            "fixture_id": identity,
            "phase": phase,
            "model_release_id": active_release.release_id,
            "status": "LIVE",
            "no_bet": True,
            "captured_at": captured_at,
        }

    first = run_live_cycle(
        manifest,
        state,
        release,
        as_of="2026-10-01T16:45:00Z",
        prediction_builder=builder,
        execute=True,
    )
    assert first["appended_count"] == 1
    second = run_live_cycle(
        manifest,
        state,
        release,
        as_of="2026-10-01T16:45:00Z",
        existing_records=first["appended_records"],
        prediction_builder=builder,
        execute=True,
    )
    assert second["appended_count"] == 0
    assert second["predictions"][0]["status"] == "ALREADY_CAPTURED"


def test_live_execute_uses_the_frozen_model_adapter_for_a_matching_input_cutoff():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    state = _json("results/research/nations_league_v1_1_input_state_20260930T200124Z.json")
    release = load_active_release(ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    ids = {row["fixture_id"] for row in state["fixtures"]}
    manifest = {**manifest, "fixtures": [row for row in manifest["fixtures"] if row["fixture_id"] in ids]}
    result = run_live_cycle(
        manifest,
        state,
        release,
        as_of=state["prediction_cutoff"].replace("+00:00", "Z"),
        execute=True,
    )
    assert result["appended_count"] == 1
    record = result["appended_records"][0]
    assert record["status"] == "LIVE"
    assert record["model_release_id"] == release.release_id
    assert record["publication_enabled"] is True
    assert record["no_bet"] is True
    assert record["source_identity"] == {
        "home_team": record["home_team"],
        "away_team": record["away_team"],
    }
    assert record["canonical_identity"] == {
        "home_team": state["identity_bindings"][record["home_team"]]["canonical_team"],
        "away_team": state["identity_bindings"][record["away_team"]]["canonical_team"],
    }


def test_result_refresh_is_noop_or_requires_new_release_without_guessing():
    state = _json("results/research/nations_league_v1_1_input_state_20260930T200124Z.json")
    current = {"result_extension_digest": state["result_extension_digest"]}
    assert refresh_result_state(current, state)["status"] == "NO_OP"
    changed = {**state, "result_extension_digest": "a" * 64}
    decision = refresh_result_state(current, changed)
    assert decision["status"] == "RESULTS_CHANGED"
    assert decision["retrain_required"] is True
    assert decision["no_bet"] is True


def test_materialized_live_bundle_uses_serializer_and_live_dispatch():
    signals = _json("docs/data/signals.json")
    live = signals["nations_league"]
    assert live["status"] == "LIVE"
    assert live["fixture_count"] == 7
    assert validate_public_nations_league(live)["public_digest"] == live["public_digest"]
    assert select_freshest_valid_public_nations_league([live]) == live
    assert serialize_public_product(signals)["nations_league"] == live


def test_non_utc_live_clock_fails_closed():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    state = _json("results/research/nations_league_v1_1_input_state_20260930T200124Z.json")
    release = load_active_release(ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    with pytest.raises(NationsLeagueLiveRuntimeError, match="UTC"):
        run_live_cycle(manifest, state, release, as_of="2026-10-01T16:45:00+02:00")


def test_fresh_input_state_uses_exact_cutoff_and_all_verified_future_targets():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    base = _json("results/research/nations_league_fixture_timeline_v1.json")
    extension = _json(
        "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
    )
    state = build_fresh_input_state(
        manifest,
        base,
        extension,
        prediction_cutoff="2026-09-30T20:01:24.572945Z",
    )
    assert state["prediction_cutoff"] == "2026-09-30T20:01:24.572945+00:00"
    assert len(state["fixtures"]) > 1
    assert set(state["team_readiness"].values()) == {"READY"}


def test_schedule_carry_forward_keeps_october_one_cutoffs_ready_and_noop():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    base = _json("results/research/nations_league_fixture_timeline_v1.json")
    extension = _json(
        "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
    )
    registry_path = ROOT / "results/audits/continuous_model_lifecycle_registry.json"
    registry = _json("results/audits/continuous_model_lifecycle_registry.json")
    for cutoff in ("2026-10-01T14:30:00Z", "2026-10-01T17:15:00Z"):
        state = build_fresh_input_state(
            manifest, base, extension, prediction_cutoff=cutoff
        )
        assert state["completeness"]["status"] == "READY"
        updated, active, decision = refresh_and_activate(
            registry,
            state,
            source_release_sha="267a5df80ee326cfe41ee6ddc27d3e8c547ab222",
            activated_at=cutoff,
        )
        assert decision["status"] == "NO_OP"
        assert updated == registry
        assert active.release_id == load_active_release(registry_path).release_id
    with pytest.raises(NationsLeagueLiveRuntimeError, match="LIVE_RESULT_REFRESH_REQUIRED"):
        build_fresh_input_state(
            manifest, base, extension, prediction_cutoff="2026-10-01T22:15:00Z"
        )


def test_live_prediction_uses_sealed_source_and_canonical_identity_for_ireland():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    base = _json("results/research/nations_league_fixture_timeline_v1.json")
    extension = _json(
        "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
    )
    state = build_fresh_input_state(
        manifest, base, extension, prediction_cutoff="2026-10-01T17:15:00Z"
    )
    fixture = next(
        row
        for row in state["fixtures"]
        if row["home_team"] == "Republic of Ireland" and row["away_team"] == "Austria"
    )
    record = build_live_prediction(
        state,
        fixture["fixture_id"],
        phase="refinement",
        active_release=load_active_release(
            ROOT / "results/audits/continuous_model_lifecycle_registry.json"
        ),
        captured_at="2026-10-01T17:15:00Z",
        market_snapshots=[
            build_market_snapshot(
                {
                    "provider": "isports_api",
                    "bookmaker": "Research bookmaker median",
                    "captured_at": "2026-10-01T17:14:00Z",
                    "fixture_id": fixture["fixture_id"],
                    "odds_decimal": {"home": 2.0, "draw": 3.5, "away": 4.0},
                }
            )
        ],
    )
    assert record["source_identity"] == {
        "home_team": "Republic of Ireland",
        "away_team": "Austria",
    }
    assert record["canonical_identity"] == {
        "home_team": "Ireland",
        "away_team": "Austria",
    }
    assert record["edge_analysis"]["edge_status"] == "EDGE_MEASURED"
    assert record["edge_analysis"]["market_snapshot"]["provider"] == "isports_api"
    assert record["no_bet"] is True
    assert record["betting_enabled"] is False
    assert record["ledger_mutation"] is False


def test_append_live_store_rejects_substitution_and_is_idempotent(tmp_path):
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    base = _json("results/research/nations_league_fixture_timeline_v1.json")
    extension = _json(
        "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
    )
    state = build_fresh_input_state(
        manifest,
        base,
        extension,
        prediction_cutoff="2026-09-30T20:01:24.572945Z",
    )
    release = load_active_release(ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    result = run_live_cycle(
        manifest,
        state,
        release,
        as_of="2026-09-30T20:01:24.572945Z",
        prediction_builder=lambda *args, **kwargs: build_live_prediction(*args, **kwargs),
        execute=True,
    )
    store = tmp_path / "live.jsonl"
    append_live_store(store, [], result["appended_records"][:1])
    append_live_store(store, result["appended_records"][:1], result["appended_records"][:1])
    tampered = dict(result["appended_records"][0])
    tampered["probabilities"] = {"home": 1.0, "draw": 0.0, "away": 0.0}
    with pytest.raises(NationsLeagueLiveRuntimeError, match="substitution"):
        append_live_store(store, result["appended_records"][:1], [tampered])
