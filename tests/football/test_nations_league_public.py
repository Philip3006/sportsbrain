from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import src.notifications.nations_league_public as nations_public
from scripts.stage_nations_league_public import stage_public_product
from src.notifications.nations_league_public import (
    NationsLeaguePublicError,
    build_public_nations_league,
)
from src.notifications.public_serializer import (
    PublicFootballCompatibilityError,
    serialize_public_product,
)


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _artifact(now=None):
    captured = now or datetime.now(timezone.utc).replace(microsecond=0)
    timestamp = captured.isoformat().replace("+00:00", "Z")
    probabilities = {"home": 0.52, "draw": 0.25, "away": 0.23}
    market_probabilities = {"home": 0.48, "draw": 0.27, "away": 0.25}
    files = {"model.pkl": "b" * 64, "stacker.pkl": "c" * 64}
    artifact = {
        "schema": "nations-league-shadow-v1",
        "competition": "UEFA Nations League",
        "provider_league_id": 146819,
        "run_id": f"unl-shadow-{captured.strftime('%Y%m%dT%H%M%SZ')}-012345abcdef",
        "source_sha": "a" * 40,
        "model_snapshot": {
            "identity": "wm2026-frozen-snapshot",
            "digest": hashlib.sha256(_canonical(files)).hexdigest(),
            "files": files,
        },
        "provider": "isports_api",
        "captured_at": timestamp,
        "request_count": 1,
        "retry_count": 0,
        "provider_event_count": 2,
        "fixture_count": 2,
        "covered_fixture_count": 2,
        "skipped_fixtures": [],
        "fixtures": [
            {
                "matchId": "event-real-shape-001",
                "kickoff": (captured + timedelta(hours=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "captured_at": timestamp,
                "home_team": "Germany",
                "away_team": "France",
                "neutral": False,
                "tournament": "UEFA Nations League",
                "market": {
                    "bookmaker": "pinnacle",
                    "odds_decimal": {"home": 2.1, "draw": 3.2, "away": 3.6},
                    "margin_free_probabilities": market_probabilities,
                },
                "probabilities": {
                    "raw_dixon_coles": probabilities,
                    "raw_gbt": {"home": 0.49, "draw": 0.26, "away": 0.25},
                    "canonical_stacker": probabilities,
                    "final_ensemble": probabilities,
                    "market_anchored": None,
                },
            }
        ],
        "shadow": True,
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
    }
    second = deepcopy(artifact["fixtures"][0])
    second.update(
        matchId="event-real-shape-002",
        home_team="Spain",
        away_team="Italy",
        kickoff=(captured + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
    )
    artifact["fixtures"].append(second)
    artifact["artifact_digest"] = hashlib.sha256(_canonical(artifact)).hexdigest()
    return artifact, captured


def _public(artifact, now, **overrides):
    return build_public_nations_league(
        artifact,
        expected_source_sha="a" * 40,
        now=now,
        **overrides,
    )


def test_complete_shadow_artifact_projects_all_fixtures_and_no_action_authority():
    artifact, now = _artifact()
    public = _public(artifact, now)
    assert public["competition"] == "UEFA Nations League"
    assert public["provider"] == "isports_api"
    assert public["provider_league_id"] == 146819
    assert public["fixture_count"] == len(public["fixtures"]) == 2
    fixture = public["fixtures"][0]
    assert fixture["provider_event_id"] == "event-real-shape-001"
    assert fixture["home"] == "Germany" and fixture["away"] == "France"
    assert fixture["model"]["probabilities"]["home"] == 0.52
    assert fixture["market"]["probabilities"]["home"] == 0.48
    assert fixture["market"]["odds_decimal"]["away"] == 3.6
    assert public["no_bet"] is True
    assert public["lifecycle"] == "SHADOW_ONLY"
    assert public["evidence_status"] == "WEAK_EVIDENCE_SHADOW_ONLY"
    assert public["artifact_digest"] == artifact["artifact_digest"]
    assert fixture["source_sha"] == public["source_sha"] == "a" * 40
    assert "request" not in public and "quota" not in public
    product = serialize_public_product({"football": [], "nations_league": public})
    assert product["nations_league"] == public


def test_complete_public_bundle_can_be_staged_without_modifying_other_products(
    tmp_path,
):
    artifact, now = _artifact()
    public = _public(artifact, now)
    destination = tmp_path / "signals.json"
    destination.write_text(
        json.dumps({"football": [], "updated": public["captured_at"]})
    )
    stage_public_product(destination, public)
    staged = json.loads(destination.read_text())
    assert staged["nations_league"] == public
    assert staged["football"] == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda a: a["fixtures"].pop(), "digest mismatch"),
        (lambda a: a.update(source_sha="d" * 40), "digest mismatch"),
    ],
)
def test_artifact_content_changes_without_digest_rejected(mutate, message):
    artifact, now = _artifact()
    mutate(artifact)
    with pytest.raises(NationsLeaguePublicError, match=message):
        _public(artifact, now)


def test_source_sha_must_match_independently_verified_release():
    artifact, now = _artifact()
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical({k: v for k, v in artifact.items() if k != "artifact_digest"})
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError, match="source SHA"):
        build_public_nations_league(artifact, expected_source_sha="d" * 40, now=now)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("provider", "the_odds_api", "iSports competition/provider identity"),
        ("provider_league_id", 999, "iSports competition/provider identity"),
        ("provider_league_id", 146819.0, "iSports competition/provider identity"),
        ("competition", "FIFA World Cup", "iSports competition/provider identity"),
    ],
)
def test_only_canonical_isports_competition_identity_is_accepted(field, value, message):
    artifact, now = _artifact()
    artifact[field] = value
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: item for key, item in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError, match=message):
        _public(artifact, now)


def test_isports_match_id_is_required_and_used_as_public_fixture_identity():
    artifact, now = _artifact()
    del artifact["fixtures"][0]["matchId"]
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: item for key, item in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError, match="identity is incomplete"):
        _public(artifact, now)


def test_private_provider_request_and_query_secrets_are_not_projected():
    artifact, now = _artifact()
    artifact["provider_private"] = {
        "api_key": "fixture-secret-never-project",
        "query": {"api_key": "query-secret-never-project"},
    }
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: item for key, item in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()
    public = _public(artifact, now)
    serialized = json.dumps(public)
    assert "fixture-secret-never-project" not in serialized
    assert "query-secret-never-project" not in serialized
    assert "provider_private" not in public


@pytest.mark.parametrize(
    ("request_count", "retry_count"),
    [(0, 0), (2, 0), (1, 1)],
)
def test_isports_artifact_request_provenance_is_one_shot_only(
    request_count, retry_count
):
    artifact, now = _artifact()
    artifact["request_count"] = request_count
    artifact["retry_count"] = retry_count
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: item for key, item in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError, match="request/retry bounds"):
        _public(artifact, now)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shadow", False),
        ("no_bet", False),
        ("publication", True),
        ("ledger_mutation", True),
        ("synthetic", True),
    ],
)
def test_shadow_only_authority_and_synthetic_provenance_are_required(field, value):
    artifact, now = _artifact()
    artifact[field] = value
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical({k: v for k, v in artifact.items() if k != "artifact_digest"})
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError, match="synthetic|shadow-only"):
        build_public_nations_league(artifact, expected_source_sha="a" * 40, now=now)


def test_runtime_loader_rejects_synthetic_fixture_artifacts(tmp_path, monkeypatch):
    artifact, now = _artifact()
    artifact["synthetic"] = True
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: value for key, value in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()
    runtime_root = tmp_path / "runtime-state"
    artifact_dir = runtime_root / "data" / "nations-league-shadow"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / f"{artifact['run_id']}.json").write_text(json.dumps(artifact))
    monkeypatch.setattr(nations_public, "governed_runtime_root", lambda: runtime_root)
    with pytest.raises(NationsLeaguePublicError, match="synthetic"):
        nations_public.load_public_nations_league_from_runtime(
            artifact["run_id"], expected_source_sha="a" * 40, now=now
        )


@pytest.mark.parametrize(
    "change",
    [
        "wrong_tournament",
        "missing_coverage",
        "bad_model_probability",
        "bad_market_probability",
        "missing_1x2_market",
        "bad_odds",
        "skipped_target",
    ],
)
def test_invalid_or_partial_target_artifact_fails_closed(change):
    artifact, now = _artifact()
    fixture = artifact["fixtures"][0]
    if change == "wrong_tournament":
        fixture["tournament"] = "FIFA World Cup"
    elif change == "missing_coverage":
        artifact["covered_fixture_count"] = 0
    elif change == "bad_model_probability":
        fixture["probabilities"]["final_ensemble"]["home"] = 1.2
    elif change == "bad_market_probability":
        fixture["market"]["margin_free_probabilities"]["draw"] = "0.3"
    elif change == "missing_1x2_market":
        del fixture["market"]["odds_decimal"]["draw"]
    elif change == "bad_odds":
        fixture["market"]["odds_decimal"]["away"] = 1.0
    else:
        artifact["skipped_fixtures"] = [
            {"provider_event_id": "uncovered", "reason": "unknown_model_team"}
        ]
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical({k: v for k, v in artifact.items() if k != "artifact_digest"})
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError):
        _public(artifact, now)


def test_stale_artifact_and_malformed_digest_are_rejected():
    artifact, now = _artifact()
    stale_now = now + timedelta(minutes=16)
    with pytest.raises(NationsLeaguePublicError, match="stale"):
        _public(artifact, stale_now)
    artifact["artifact_digest"] = "bad"
    with pytest.raises(NationsLeaguePublicError, match="digest"):
        _public(artifact, now)


def test_public_bundle_digest_and_fixture_provenance_are_bound():
    artifact, now = _artifact()
    public = _public(artifact, now)
    public["fixtures"][0]["home"] = "Tampered"
    with pytest.raises(PublicFootballCompatibilityError, match="digest"):
        serialize_public_product({"nations_league": public})
