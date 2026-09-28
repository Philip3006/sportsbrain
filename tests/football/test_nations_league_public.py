from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import src.notifications.nations_league_public as nations_public
from scripts.stage_nations_league_public import stage_public_product
from src.betting.value_detector import BetSignal
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


def _redigest(artifact):
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: value for key, value in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()


def _artifact(now=None):
    captured = now or datetime.now(timezone.utc).replace(microsecond=0)
    timestamp = captured.isoformat().replace("+00:00", "Z")
    probabilities = {"home": 0.52, "draw": 0.25, "away": 0.23}
    market_probabilities = {"home": 0.48, "draw": 0.27, "away": 0.25}
    files = {"model.pkl": "b" * 64, "stacker.pkl": "c" * 64}
    artifact = {
        "schema": "nations-league-isports-shadow-v2",
        "competition": "UEFA Nations League",
        "provider_league_id": 146819,
        "run_id": f"unl-shadow-{captured.strftime('%Y%m%dT%H%M%SZ')}-012345abcdef",
        "capture_status": "complete",
        "source_sha": "a" * 40,
        "model_snapshot": {
            "identity": "wm2026-frozen-snapshot",
            "digest": hashlib.sha256(_canonical(files)).hexdigest(),
            "files": files,
        },
        "provider": "isports_api",
        "sport_key": "soccer_uefa_nations_league",
        "captured_at": timestamp,
        "input_data": {"international_results": {"sha256": "f" * 64}},
        "request_count": 2,
        "retry_count": 0,
        "provider_operation_manifest": [
            {
                "ordinal": 1,
                "operation": "schedule",
                "method": "GET",
                "path": "/sport/football/schedule/basic",
                "query": {"leagueId": "146819"},
                "status_code": 200,
                "started_at": (captured - timedelta(minutes=4))
                .isoformat()
                .replace("+00:00", "Z"),
                "completed_at": (captured - timedelta(minutes=3))
                .isoformat()
                .replace("+00:00", "Z"),
                "response_sha256": "d" * 64,
            },
            {
                "ordinal": 2,
                "operation": "odds",
                "method": "GET",
                "path": "/sport/football/odds/european/all",
                "query": {"matchId": "event-real-shape-001,event-real-shape-002"},
                "status_code": 200,
                "started_at": (captured - timedelta(minutes=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "completed_at": (captured - timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "response_sha256": "e" * 64,
            },
        ],
        "provider_rate_evidence": [],
        "provider_event_count": 2,
        "eligible_schedule_fixture_count": 2,
        "fixture_count": 2,
        "covered_fixture_count": 2,
        "skipped_fixtures": [],
        "coverage": {
            "eligible_schedule_fixtures": 2,
            "valid_odds_fixtures": 2,
            "model_fixtures": 2,
            "complete": True,
            "eligible_match_ids": [
                "event-real-shape-001",
                "event-real-shape-002",
            ],
            "market_covered_match_ids": [
                "event-real-shape-001",
                "event-real-shape-002",
            ],
            "model_covered_match_ids": [
                "event-real-shape-001",
                "event-real-shape-002",
            ],
            "skipped_match_ids": [],
        },
        "excluded_schedule_fixtures": [],
        "fixtures": [
            {
                "provider_match_id": "event-real-shape-001",
                "kickoff": (captured + timedelta(hours=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "captured_at": timestamp,
                "home_team": "Germany",
                "away_team": "France",
                "neutral": False,
                "tournament": "UEFA Nations League",
                "market": {
                    "bookmaker": "iSports European odds component-wise median",
                    "aggregation": "latest_valid_quote_per_bookmaker_then_componentwise_median",
                    "bookmaker_count": 2,
                    "bookmakers": [
                        {
                            "company_id": "101",
                            "company_name": "Bookmaker 0",
                            "change_time": 1790517300,
                            "odds_decimal": {"home": 2.1, "draw": 3.2, "away": 3.6},
                        },
                        {
                            "company_id": "102",
                            "company_name": "Bookmaker 1",
                            "change_time": 1790517301,
                            "odds_decimal": {"home": 2.1, "draw": 3.2, "away": 3.6},
                        },
                    ],
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
                "market_anchor_status": "not_applied_unbound_to_frozen_stacker_contract",
                "model_vs_market_edge_percentage_points": {
                    "home": 0,
                    "draw": 0,
                    "away": 0,
                },
            }
        ],
        "shadow": True,
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
        "scheduler_mutation": False,
        "evidence_status": "WEAK_EVIDENCE_SHADOW_ONLY",
    }
    second = deepcopy(artifact["fixtures"][0])
    second.update(
        provider_match_id="event-real-shape-002",
        home_team="Spain",
        away_team="Italy",
        neutral=True,
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


def _make_partial(artifact, skipped_fixtures):
    artifact["fixtures"] = artifact["fixtures"][:1]
    artifact["capture_status"] = "partial"
    artifact["fixture_count"] = 1
    artifact["covered_fixture_count"] = 1
    artifact["skipped_fixtures"] = skipped_fixtures
    artifact["coverage"]["valid_odds_fixtures"] = 1
    artifact["coverage"]["model_fixtures"] = 1
    artifact["coverage"]["complete"] = False
    artifact["coverage"]["market_covered_match_ids"] = ["event-real-shape-001"]
    artifact["coverage"]["model_covered_match_ids"] = ["event-real-shape-001"]
    artifact["coverage"]["skipped_match_ids"] = [
        item["provider_match_id"] for item in skipped_fixtures
    ]


def test_complete_shadow_artifact_projects_all_fixtures_and_no_action_authority():
    artifact, now = _artifact()
    public = _public(artifact, now)
    assert artifact["schema"] == "nations-league-isports-shadow-v2"
    assert artifact["capture_status"] == "complete"
    assert artifact["eligible_schedule_fixture_count"] == 2
    assert artifact["coverage"]["complete"] is True
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
    assert "request" not in public
    assert "provider_operation_manifest" not in public
    assert "quota" not in public
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
    del artifact["fixtures"][0]["provider_match_id"]
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
    artifact["provider_rate_evidence"] = {
        "quota_remaining": 27,
        "response_headers": {"x-requests-remaining": "quota-secret-never-project"},
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
    assert "quota-secret-never-project" not in serialized
    assert "provider_private" not in public
    assert "provider_rate_evidence" not in public
    assert "request_count" not in public and "retry_count" not in public


@pytest.mark.parametrize(
    ("request_count", "retry_count"),
    [(0, 0), (1, 0), (3, 0), (2, 1)],
)
def test_isports_artifact_request_provenance_is_exact_two_calls_no_retry(
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
    "mutation",
    [
        lambda manifest: manifest[0].update(path="/v1/unauthorized"),
        lambda manifest: manifest[0]["query"].update(apiKey="private-secret"),
        lambda manifest: manifest[1].update(query={"day": "28"}),
        lambda manifest: manifest[1].update(query={"date": "2026-09-28"}),
        lambda manifest: manifest[1].update(query={"min": "2"}),
        lambda manifest: manifest[1].update(
            query={"matchId": "event-real-shape-001,other-match"}
        ),
        lambda manifest: manifest[1].update(
            query={"matchId": "event-real-shape-001,event-real-shape-001"}
        ),
        lambda manifest: manifest[1].update(
            query={
                "matchId": "event-real-shape-001,event-real-shape-002",
                "api_key": "private-secret",
            }
        ),
        lambda manifest: manifest[1].update(status_code=503),
        lambda manifest: manifest[1].update(response_sha256="bad"),
        lambda manifest: manifest[1].update(ordinal=1),
        lambda manifest: manifest[1].update(
            completed_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        ),
    ],
)
def test_isports_operation_manifest_is_exact_bounded_and_secret_free(mutation):
    artifact, now = _artifact()
    mutation(artifact["provider_operation_manifest"])
    artifact["artifact_digest"] = hashlib.sha256(
        _canonical(
            {key: item for key, item in artifact.items() if key != "artifact_digest"}
        )
    ).hexdigest()
    with pytest.raises(NationsLeaguePublicError, match="iSports operation"):
        _public(artifact, now)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shadow", False),
        ("no_bet", False),
        ("publication", True),
        ("ledger_mutation", True),
        ("synthetic", True),
        ("scheduler_mutation", True),
        ("evidence_status", "PRODUCTION_APPROVED"),
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
        "malformed_neutral",
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
    elif change == "malformed_neutral":
        fixture["neutral"] = "false"
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


def test_private_capture_age_remains_strictly_limited_to_fifteen_minutes():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    captured = now - timedelta(minutes=14, seconds=59)
    artifact, _ = _artifact(captured)
    for operation in artifact["provider_operation_manifest"]:
        operation["completed_at"] = artifact["captured_at"]
    _redigest(artifact)
    assert _public(artifact, now)["fixture_count"] == 2

    too_old, _ = _artifact(now - timedelta(minutes=15, seconds=1))
    for operation in too_old["provider_operation_manifest"]:
        operation["completed_at"] = too_old["captured_at"]
    _redigest(too_old)
    with pytest.raises(NationsLeaguePublicError, match="stale"):
        _public(too_old, now)


def _public_bundle_for_retention(captured_at, now):
    public = _fixed_public_bundle(captured_at)
    for index, fixture in enumerate(public["fixtures"]):
        fixture["kickoff"] = (
            (now + timedelta(days=2, hours=index)).isoformat().replace("+00:00", "Z")
        )
    public["public_digest"] = nations_public._public_digest(public)
    return public


@pytest.mark.parametrize("age", [timedelta(minutes=16), timedelta(hours=6)])
def test_already_projected_public_shadow_is_accepted_within_twenty_four_hours(age):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _public_bundle_for_retention(now - age, now)
    assert nations_public.validate_public_nations_league(public, now=now) == public


def test_already_projected_public_shadow_expires_after_twenty_four_hours():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _public_bundle_for_retention(now - timedelta(hours=24, seconds=1), now)
    with pytest.raises(NationsLeaguePublicError, match="stale"):
        nations_public.validate_public_nations_league(public, now=now)


def test_public_bundle_digest_and_fixture_provenance_are_bound():
    artifact, now = _artifact()
    public = _public(artifact, now)
    public["fixtures"][0]["home"] = "Tampered"
    with pytest.raises(PublicFootballCompatibilityError, match="digest"):
        serialize_public_product({"nations_league": public})


def test_match_id_manifest_binds_exactly_to_eligible_schedule_identities():
    artifact, now = _artifact()
    assert artifact["provider_operation_manifest"][1]["query"] == {
        "matchId": ",".join(artifact["coverage"]["eligible_match_ids"])
    }
    assert _public(artifact, now)["fixture_count"] == 2


@pytest.mark.parametrize(
    "query_value",
    [
        "event-real-shape-001,event-real-shape-002,",
        "event-real-shape-001,event-real-shape-002,other/id",
        "event-real-shape-001,event-real-shape-002,"
        + ",".join(f"match-{index}" for index in range(99)),
    ],
)
def test_malformed_or_oversized_match_id_manifest_is_rejected(query_value):
    artifact, now = _artifact()
    artifact["provider_operation_manifest"][1]["query"] = {"matchId": query_value}
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="iSports operation"):
        _public(artifact, now)


def test_duplicate_or_malformed_eligible_schedule_ids_are_rejected():
    for invalid_ids in (
        ["event-real-shape-001", "event-real-shape-001"],
        ["event-real-shape-001", "bad,match-id"],
    ):
        artifact, now = _artifact()
        artifact["coverage"]["eligible_match_ids"] = invalid_ids
        _redigest(artifact)
        with pytest.raises(NationsLeaguePublicError, match="eligible"):
            _public(artifact, now)


def test_eligible_schedule_over_100_match_ids_is_rejected():
    artifact, now = _artifact()
    artifact["coverage"]["eligible_match_ids"] = [
        f"match-{index}" for index in range(101)
    ]
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="out of bounds"):
        _public(artifact, now)


def test_partial_market_coverage_is_accounted_privately_and_projects_covered_only():
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [
            {
                "provider_match_id": "event-real-shape-002",
                "reason": "missing_1x2_market",
            }
        ],
    )
    _redigest(artifact)

    public = _public(artifact, now)
    assert artifact["eligible_schedule_fixture_count"] == 2
    assert artifact["fixture_count"] == artifact["covered_fixture_count"] == 1
    assert artifact["fixture_count"] + len(artifact["skipped_fixtures"]) == 2
    assert public["fixture_count"] == len(public["fixtures"]) == 1
    assert [fixture["provider_event_id"] for fixture in public["fixtures"]] == [
        "event-real-shape-001"
    ]
    serialized = json.dumps(public)
    assert "missing_1x2_market" not in serialized
    assert "event-real-shape-002" not in serialized
    assert "skipped_fixtures" not in public
    product = serialize_public_product({"football": [], "nations_league": public})
    assert product["nations_league"]["fixture_count"] == 1


def test_unknown_skipped_target_reason_is_rejected():
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [{"provider_match_id": "event-real-shape-002", "reason": "model_error"}],
    )
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="unsupported skipped target"):
        _public(artifact, now)


def test_skipped_target_requires_a_valid_native_match_id():
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [{"provider_match_id": "bad/match-id", "reason": "missing_1x2_market"}],
    )
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="valid native iSports match ID"):
        _public(artifact, now)


def test_fixture_coverage_accounting_mismatch_is_rejected():
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [
            {
                "provider_match_id": "event-real-shape-002",
                "reason": "missing_1x2_market",
            }
        ],
    )
    artifact["fixture_count"] = artifact["eligible_schedule_fixture_count"]
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="coverage is incomplete"):
        _public(artifact, now)


def test_eligible_schedule_fixture_count_must_match_coverage_ids():
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [
            {
                "provider_match_id": "event-real-shape-002",
                "reason": "missing_1x2_market",
            }
        ],
    )
    artifact["eligible_schedule_fixture_count"] = 3
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="coverage is incomplete"):
        _public(artifact, now)


@pytest.mark.parametrize(
    ("partial", "capture_status", "coverage_complete"),
    [
        (False, "partial", True),
        (False, "complete", False),
        (True, "complete", False),
        (True, "partial", True),
    ],
)
def test_capture_status_and_coverage_complete_must_agree(
    partial, capture_status, coverage_complete
):
    artifact, now = _artifact()
    if partial:
        _make_partial(
            artifact,
            [
                {
                    "provider_match_id": "event-real-shape-002",
                    "reason": "missing_1x2_market",
                }
            ],
        )
    artifact["capture_status"] = capture_status
    artifact["coverage"]["complete"] = coverage_complete
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="capture status"):
        _public(artifact, now)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market_covered_match_ids", ["event-real-shape-002"]),
        ("model_covered_match_ids", ["event-real-shape-002"]),
        ("skipped_match_ids", ["event-real-shape-001"]),
    ],
)
def test_coverage_identity_lists_must_match_fixture_and_skip_records(field, value):
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [
            {
                "provider_match_id": "event-real-shape-002",
                "reason": "missing_1x2_market",
            }
        ],
    )
    artifact["coverage"][field] = value
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="coverage identities"):
        _public(artifact, now)


@pytest.mark.parametrize("schema", ["nations-league-isports-shadow-v1", "unknown-v2"])
def test_v1_and_tampered_private_schemas_are_rejected(schema):
    artifact, now = _artifact()
    artifact["schema"] = schema
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="schema"):
        _public(artifact, now)


def test_covered_and_skipped_identity_overlap_is_rejected():
    artifact, now = _artifact()
    _make_partial(
        artifact,
        [
            {
                "provider_match_id": "event-real-shape-001",
                "reason": "missing_1x2_market",
            }
        ],
    )
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="coverage identities"):
        _public(artifact, now)


def test_zero_covered_fixtures_are_rejected():
    artifact, now = _artifact()
    artifact["fixtures"] = []
    artifact["fixture_count"] = 0
    artifact["covered_fixture_count"] = 0
    artifact["skipped_fixtures"] = [
        {
            "provider_match_id": match_id,
            "reason": "missing_1x2_market",
        }
        for match_id in artifact["coverage"]["eligible_match_ids"]
    ]
    artifact["coverage"]["valid_odds_fixtures"] = 0
    artifact["coverage"]["model_fixtures"] = 0
    artifact["coverage"]["market_covered_match_ids"] = []
    artifact["coverage"]["model_covered_match_ids"] = []
    artifact["coverage"]["skipped_match_ids"] = list(
        artifact["coverage"]["eligible_match_ids"]
    )
    artifact["coverage"]["complete"] = False
    artifact["capture_status"] = "partial"
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="coverage is incomplete"):
        _public(artifact, now)


def test_duplicate_skipped_match_ids_are_rejected():
    artifact, now = _artifact()
    third_id = "event-real-shape-003"
    artifact["provider_event_count"] = 3
    artifact["eligible_schedule_fixture_count"] = 3
    artifact["coverage"]["eligible_schedule_fixtures"] = 3
    artifact["coverage"]["eligible_match_ids"].append(third_id)
    artifact["provider_operation_manifest"][1]["query"]["matchId"] += "," + third_id
    _make_partial(
        artifact,
        [
            {
                "provider_match_id": "event-real-shape-002",
                "reason": "missing_1x2_market",
            },
            {
                "provider_match_id": "event-real-shape-002",
                "reason": "missing_1x2_market",
            },
        ],
    )
    _redigest(artifact)
    with pytest.raises(NationsLeaguePublicError, match="skipped target identities"):
        _public(artifact, now)


def _writer_signal(match_id, home, away):
    return BetSignal(
        match_id=match_id,
        home=home,
        away=away,
        market="home",
        model_prob=0.55,
        fair_prob=0.50,
        decimal_odds=2.10,
        ev=0.10,
        kelly_f=0.05,
        stake_pct=0.05,
        confidence="MEDIUM",
        stake_eur=5.0,
    )


def _run_writer_refresh(
    tmp_path,
    monkeypatch,
    *,
    now,
    user_snapshot,
    shared_snapshot=None,
    football=None,
    tennis=None,
    schedule=None,
    user=None,
    default_snapshot=None,
):
    """Run the ordinary writer against isolated files and mocked side effects."""
    import src.notifications.web_dashboard as dashboard
    from src.data import odds_api

    active = tmp_path / "checkout"
    stage = tmp_path / "stage"
    data_dir = active / "docs" / "data"
    data_dir.mkdir(parents=True)
    default_user = dashboard._DEFAULT_USER
    selected_user = user or default_user
    user_path = data_dir / f"signals_{selected_user}.json"
    user_path.write_text(json.dumps(user_snapshot))
    if default_snapshot is not None:
        (data_dir / f"signals_{default_user}.json").write_text(
            json.dumps(default_snapshot)
        )
    shared_path = data_dir / "signals.json"
    if shared_snapshot is not None:
        shared_path.write_text(json.dumps(shared_snapshot))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(dashboard, "ROOT", active)
    monkeypatch.setattr(dashboard, "datetime", FixedDateTime)
    monkeypatch.setattr(nations_public, "datetime", FixedDateTime)
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", str(stage))
    monkeypatch.setattr(
        dashboard, "_ledger_path_for", lambda _user: tmp_path / "ledger.csv"
    )
    monkeypatch.setattr(dashboard, "_get_open_bets_from_ledger", lambda **_kwargs: [])
    monkeypatch.setattr(dashboard, "_get_closed_bets", lambda **_kwargs: [])
    monkeypatch.setattr(
        dashboard, "_get_settled_bets_for_dashboard", lambda **_kwargs: []
    )
    monkeypatch.setattr(dashboard, "_build_history", lambda **_kwargs: [])
    monkeypatch.setattr(dashboard, "_build_wm_stats", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard, "_build_tennis_stats", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard, "_build_player_form_cache", dict)
    provider_calls = []
    monkeypatch.setattr(
        odds_api,
        "fetch_wm_scores",
        lambda **_kwargs: provider_calls.append("mocked") or [],
    )
    uploaded = {}
    monkeypatch.setattr(
        dashboard,
        "upload_signals_to_cloud",
        lambda **kwargs: uploaded.update(kwargs) or True,
    )

    assert (
        dashboard.write_signals_json(
            football=football,
            tennis=tennis,
            schedule=schedule,
            user=selected_user,
        )
        is True
    )
    output_name = (
        "signals.json"
        if selected_user == default_user
        else f"signals_{selected_user}.json"
    )
    output_path = stage / "docs" / "data" / output_name
    assert output_path.is_file()
    return json.loads(output_path.read_text()), uploaded, provider_calls


def _fixed_public_bundle(captured_at):
    artifact, _ = _artifact(captured_at)
    return _public(artifact, captured_at)


def _move_public_kickoffs_after(public, now):
    for index, fixture in enumerate(public["fixtures"]):
        fixture["kickoff"] = (
            (now + timedelta(days=2, hours=index)).isoformat().replace("+00:00", "Z")
        )
    public["public_digest"] = nations_public._public_digest(public)
    return public


def test_tennis_refresh_preserves_staged_nations_league_and_existing_merge_semantics(
    tmp_path, monkeypatch
):
    from src.notifications import public_serializer

    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _fixed_public_bundle(now)
    default_user_snapshot = {
        "football": [],
        "tennis": [],
        "schedule": [{"sport": "football", "home": "Germany", "away": "France"}],
        "all_odds": {"kept": {"home": 2.0}},
        "model_tips": {"kept": {"model": "existing"}},
    }
    validation_calls = []
    original_validator = public_serializer.validate_public_nations_league

    def spy_validator(value, **kwargs):
        validation_calls.append(value)
        return original_validator(value, **kwargs)

    monkeypatch.setattr(
        public_serializer, "validate_public_nations_league", spy_validator
    )
    output, uploaded, provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot=default_user_snapshot,
        # The stage operation writes the shared static input, not the per-user file.
        shared_snapshot={"football": [], "nations_league": public},
        tennis=[_writer_signal("tennis-1", "Alcaraz", "Sinner")],
        schedule=[{"sport": "tennis", "home": "Alcaraz", "away": "Sinner"}],
    )

    assert json.dumps(output["nations_league"], sort_keys=True) == json.dumps(
        public, sort_keys=True
    )
    assert len(output["tennis"]) == 1
    assert output["tennis"][0]["match"] == "Alcaraz vs Sinner"
    assert output["football"] == []
    assert {entry["sport"] for entry in output["schedule"]} == {"football", "tennis"}
    assert output["all_odds"] == default_user_snapshot["all_odds"]
    assert output["model_tips"] == default_user_snapshot["model_tips"]
    assert validation_calls == [public]
    assert uploaded["payload"]["nations_league"] == public
    # Writer's existing scores hook is mocked; no external provider request occurs.
    assert provider_calls == ["mocked"]


def test_football_refresh_preserves_per_user_nations_league_and_updates_football(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _fixed_public_bundle(now)
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={
            "football": [],
            "tennis": [],
            "nations_league": public,
            "schedule": [{"sport": "tennis", "home": "Alcaraz", "away": "Sinner"}],
        },
        football=[_writer_signal("football-1", "Arsenal", "Chelsea")],
        schedule=[{"sport": "football", "home": "Arsenal", "away": "Chelsea"}],
    )

    assert output["nations_league"] == public
    assert len(output["football"]) == 1
    assert output["football"][0]["match"] == "Arsenal vs Chelsea"
    assert output["tennis"] == []
    assert {entry["sport"] for entry in output["schedule"]} == {"football", "tennis"}
    assert all("provider_event_id" not in signal for signal in output["football"])


def test_freshest_valid_snapshot_wins_across_target_default_and_shared_files(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    target = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=6)), now
    )
    default = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=4)), now
    )
    shared = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=2)), now
    )
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user="reader",
        user_snapshot={"football": [], "tennis": [], "nations_league": target},
        default_snapshot={"football": [], "nations_league": default},
        shared_snapshot={"football": [], "nations_league": shared},
        tennis=[_writer_signal("tennis-2", "Alcaraz", "Sinner")],
    )

    assert output["nations_league"] == shared
    assert len(output["tennis"]) == 1


def test_invalid_newer_candidate_does_not_displace_older_valid_snapshot(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    valid = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=6)), now
    )
    invalid_newer = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=1)), now
    )
    invalid_newer["public_digest"] = "f" * 64
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={"football": [], "tennis": [], "nations_league": valid},
        shared_snapshot={"football": [], "nations_league": invalid_newer},
        football=[_writer_signal("football-2", "Arsenal", "Chelsea")],
    )

    assert output["nations_league"] == valid
    assert output["football"][0]["match"] == "Arsenal vs Chelsea"


def test_writer_preserves_public_shadow_snapshot_for_several_hours(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=6)), now
    )
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={"football": [], "tennis": [], "nations_league": public},
        football=[_writer_signal("football-1", "Arsenal", "Chelsea")],
    )
    assert output["nations_league"] == public
    assert output["football"][0]["match"] == "Arsenal vs Chelsea"


def test_health_only_writer_refresh_preserves_valid_nations_league(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _move_public_kickoffs_after(
        _fixed_public_bundle(now - timedelta(hours=6)), now
    )
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={
            "football": [],
            "tennis": [],
            "nations_league": public,
        },
    )

    assert output["nations_league"] == public
    assert output["football"] == []
    assert output["tennis"] == []
    assert output["updated"] == "2026-09-28T12:00:00Z"


def test_missing_nations_league_is_not_fabricated(tmp_path, monkeypatch):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={"football": [], "tennis": []},
        football=[_writer_signal("football-1", "Arsenal", "Chelsea")],
    )
    assert "nations_league" not in output
    assert output["football"][0]["match"] == "Arsenal vs Chelsea"


@pytest.mark.parametrize("invalid_kind", ["stale", "malformed", "private_artifact"])
def test_invalid_nations_league_is_omitted_without_blocking_other_refreshes(
    invalid_kind, tmp_path, monkeypatch
):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    captured_at = (
        now - timedelta(hours=24, seconds=1) if invalid_kind == "stale" else now
    )
    public = _fixed_public_bundle(captured_at)
    if invalid_kind == "malformed":
        public["public_digest"] = "f" * 64
    elif invalid_kind == "stale":
        _move_public_kickoffs_after(public, now)
    elif invalid_kind == "private_artifact":
        public, _ = _artifact(now)

    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={
            "football": [],
            "tennis": [],
            "nations_league": public,
        },
        tennis=[_writer_signal("tennis-1", "Alcaraz", "Sinner")],
    )

    assert "nations_league" not in output
    assert len(output["tennis"]) == 1
    assert output["tennis"][0]["match"] == "Alcaraz vs Sinner"


def test_public_serializer_rejects_private_fields_on_preserved_nations_league():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    private_value = {**_fixed_public_bundle(now), "provider_operation_manifest": []}
    with pytest.raises(PublicFootballCompatibilityError, match="Nations League"):
        serialize_public_product({"nations_league": private_value})


def test_independent_serializer_rejection_drops_only_nations_league(
    tmp_path, monkeypatch
):
    from src.notifications import public_serializer

    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    public = _fixed_public_bundle(now)
    original_validator = public_serializer.validate_public_nations_league
    rejected_values = []

    def reject_at_final_gate(value, **kwargs):
        if value == public:
            rejected_values.append(value)
            raise NationsLeaguePublicError("became stale at serialization")
        return original_validator(value, **kwargs)

    monkeypatch.setattr(
        public_serializer, "validate_public_nations_league", reject_at_final_gate
    )
    output, _uploaded, _provider_calls = _run_writer_refresh(
        tmp_path,
        monkeypatch,
        now=now,
        user_snapshot={
            "football": [],
            "tennis": [],
            "nations_league": public,
        },
        football=[_writer_signal("football-1", "Arsenal", "Chelsea")],
    )

    assert rejected_values == [public]
    assert "nations_league" not in output
    assert output["football"][0]["match"] == "Arsenal vs Chelsea"
