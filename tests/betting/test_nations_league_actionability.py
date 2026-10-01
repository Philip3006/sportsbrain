from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.betting.nations_league_actionability as actionability
from scripts.capture_nations_league_bet_quote import preflight
from src.analysis.nations_league_live_edge import build_market_snapshot
from src.betting.nations_league_actionability import (
    NationsLeagueActionabilityError,
    build_nations_league_actionable_projection,
    validate_nations_league_actionable_projection,
)
from src.football.top5_b4_provider_neutral_evidence import canonical_evidence_digest
from src.notifications.nations_league_live_public import _public_digest
from src.notifications.public_serializer import serialize_public_product

ROOT = Path(__file__).resolve().parents[2]
_SOURCE_PUBLIC = json.loads(
    (ROOT / "docs/data/signals.json").read_text(encoding="utf-8")
)["nations_league"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _future_public() -> dict:
    """Keep the projection tests deterministic as the checked-in snapshot ages."""

    now = _now()
    public = copy.deepcopy(_SOURCE_PUBLIC)
    for fixture in public["fixtures"]:
        if fixture["phase"] == "refinement":
            fixture["kickoff_utc"] = (
                (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            )
            fixture["prediction_cutoff"] = now.isoformat().replace("+00:00", "Z")
            fixture["updated_at"] = now.isoformat().replace("+00:00", "Z")
    public["updated_at"] = max(fixture["updated_at"] for fixture in public["fixtures"])
    public["public_digest"] = _public_digest(
        {key: value for key, value in public.items() if key != "public_digest"}
    )
    return public


PUBLIC = _future_public()


def _snapshots(now: datetime, *, provider: str = "isports_api") -> list[dict]:
    snapshots = []
    for index, fixture in enumerate(PUBLIC["fixtures"], start=1):
        if fixture["phase"] != "refinement":
            continue
        snapshots.append(
            build_market_snapshot(
                {
                    "provider": provider,
                    "bookmaker": "iSports European median",
                    "captured_at": now.isoformat().replace("+00:00", "Z"),
                    "fixture_id": fixture["fixture_id"],
                    "provider_match_id": str(index),
                    "odds_decimal": {"home": 2.0, "draw": 4.0, "away": 3.0},
                }
            )
        )
    return snapshots


def _provenance() -> dict:
    return {
        "provider": "isports_api",
        "provider_operation_manifest": [],
        "provider_rate_evidence": [],
    }


def test_fresh_isports_projection_is_actionable_but_preserves_no_bet_source():
    now = _now()
    projection = build_nations_league_actionable_projection(
        PUBLIC, _snapshots(now), now=now, request_provenance=_provenance()
    )
    assert projection["source_evidence_no_bet"] is True
    assert projection["provider_authority"] == "the_odds_api"
    assert projection["evidence_provider"] == "isports_api"
    assert projection["signals"]
    assert projection["quote_evidence"]
    assert any(
        item["actionable_signal_count"] > 0 for item in projection["quote_evidence"]
    )
    assert all(signal["phase"] == "refinement" for signal in projection["signals"])
    assert validate_nations_league_actionable_projection(projection) == projection
    assert (
        serialize_public_product({"nations_league_value_signals": projection})[
            "nations_league_value_signals"
        ]
        == projection
    )


def test_fresh_quote_without_actionable_outcome_remains_displayable_no_bet(monkeypatch):
    now = _now()
    monkeypatch.setattr(actionability, "detect_value", lambda *args, **kwargs: [])
    projection = build_nations_league_actionable_projection(
        PUBLIC, _snapshots(now), now=now, request_provenance=_provenance()
    )
    assert projection["signals"] == []
    assert projection["quote_evidence"]
    for evidence in projection["quote_evidence"]:
        assert evidence["no_bet"] is True
        assert evidence["no_bet_reason"] == "NO_CANONICAL_ACTIONABLE_OUTCOME"
        edge = evidence["edge_analysis"]
        assert set(edge["outcomes"]) == {"home", "draw", "away"}
        assert edge["market_snapshot"]["provider"] == "isports_api"
    assert validate_nations_league_actionable_projection(projection) == projection
    assert (
        serialize_public_product({"nations_league_value_signals": projection})[
            "nations_league_value_signals"
        ]["signals"]
        == []
    )


def test_stale_quote_fails_closed():
    now = _now()
    snapshots = _snapshots(now - timedelta(minutes=31))
    with pytest.raises(NationsLeagueActionabilityError, match="stale"):
        build_nations_league_actionable_projection(
            PUBLIC, snapshots, now=now, request_provenance=_provenance()
        )


def test_wrong_provider_and_fixture_coverage_fail_closed():
    now = _now()
    with pytest.raises(NationsLeagueActionabilityError, match="not iSports"):
        build_nations_league_actionable_projection(
            PUBLIC,
            _snapshots(now, provider="the_odds_api"),
            now=now,
            request_provenance=_provenance(),
        )
    snapshots = _snapshots(now)
    snapshots[0]["fixture_id"] = "uefa-nl:wrong"
    with pytest.raises(NationsLeagueActionabilityError, match="coverage is incomplete"):
        build_nations_league_actionable_projection(
            PUBLIC, snapshots, now=now, request_provenance=_provenance()
        )


def test_digest_mutation_and_unsupported_provenance_fail_closed():
    now = _now()
    projection = build_nations_league_actionable_projection(
        PUBLIC, _snapshots(now), now=now, request_provenance=_provenance()
    )
    mutated = copy.deepcopy(projection)
    mutated["artifact_digest"] = "0" * 64
    with pytest.raises(NationsLeagueActionabilityError, match="digest"):
        validate_nations_league_actionable_projection(mutated)
    with pytest.raises(NationsLeagueActionabilityError, match="unsupported fields"):
        build_nations_league_actionable_projection(
            PUBLIC,
            _snapshots(now),
            now=now,
            request_provenance={**_provenance(), "api_key": "must-not-be-accepted"},
        )


def test_quote_evidence_signal_count_is_bound_to_canonical_signals():
    now = _now()
    projection = build_nations_league_actionable_projection(
        PUBLIC, _snapshots(now), now=now, request_provenance=_provenance()
    )
    mutated = copy.deepcopy(projection)
    evidence = mutated["quote_evidence"][0]
    fixture_id = evidence["fixture_id"]
    evidence["actionable_signal_count"] = (
        sum(signal["fixture_key"] == fixture_id for signal in mutated["signals"]) + 1
    )
    evidence["no_bet_reason"] = "ACTIONABLE_SIGNAL_AVAILABLE"
    mutated["artifact_digest"] = actionability._digest(
        actionability._projection_body(mutated)
    )
    with pytest.raises(
        NationsLeagueActionabilityError,
        match="does not match canonical signals",
    ):
        validate_nations_league_actionable_projection(mutated)


def test_preflight_is_zero_network_and_binds_current_refinement_scope(
    tmp_path, monkeypatch
):
    now = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
    input_path = tmp_path / "signals.json"
    input_path.write_text(json.dumps({"nations_league": PUBLIC}), encoding="utf-8")
    targets = [
        fixture["fixture_id"]
        for fixture in PUBLIC["fixtures"]
        if fixture["phase"] == "refinement"
        and datetime.fromisoformat(fixture["kickoff_utc"].replace("Z", "+00:00")) > now
    ]
    body = {
        "schema_version": "nations-league-bet-time-quote-authorization-v1",
        "authorization_id": "nl-quote-test-001",
        "provider": "isports_api",
        "fixture_scope": targets,
        "phase": "refinement",
        "issued_at": "2026-10-01T17:00:00Z",
        "expires_at": "2026-10-01T19:00:00Z",
        "maximum_request_count": 2,
        "retry_count": 0,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "betting": False,
    }
    body["authorization_digest"] = canonical_evidence_digest(body)
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(
        "scripts.capture_nations_league_bet_quote._fetch_isports_market_snapshots",
        lambda *args, **kwargs: pytest.fail("preflight must not call transport"),
    )
    result = preflight(
        input_path=input_path, authorization_path=authorization_path, now=now
    )
    assert result["status"] == "PREFLIGHT_READY"
    assert result["request_count"] == 0
    assert result["credential_access_count"] == 0
