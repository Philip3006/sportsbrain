from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
)
from src.football.top5_lifecycle_public import (
    TOP5_LIFECYCLE_SCHEMA_VERSION,
)
from src.football.top5_pwa import Top5PwaData
from src.football.top5_signal_lifecycle import (
    EXPECTED_PROVIDER_IDENTITY,
    RefinementClassification,
    SignalLifecycleError,
    Top5SignalLifecycle,
    create_initial_signal,
    refine_signal,
)
from src.football.top5_signal_lifecycle_public_adapter import (
    Top5SignalLifecyclePublicAdapterError,
    project_top5_signal_lifecycles,
)
from src.notifications.public_serializer import (
    PublicFootballCompatibilityError,
    map_prediction_to_public_football_signals,
    serialize_public_football_records,
)
from tests.football.test_top5_public_delivery import (
    BASE,
    _controlled_payload,
)

UTC = timezone.utc
FIXTURE_KEY = "EPL:fixture:lifecycle-adapter"
LEAGUE = "EPL"
CANDIDATE = "candidate:lifecycle-adapter"
MODEL = "model:lifecycle-adapter"
SOURCE_SHA = "a" * 64
RESEARCH_SHA = "b" * 64
MODEL_HASH = "c" * 64
INITIAL_PROBABILITIES = {"home": 0.50, "draw": 0.28, "away": 0.22}
REFINED_PROBABILITIES = {"home": 0.55, "draw": 0.26, "away": 0.19}
MARKET_PROBABILITIES = {"home": 0.45, "draw": 0.30, "away": 0.25}
KICKOFF = BASE + timedelta(hours=24)
INITIAL_SNAPSHOT_ID = "snapshot:lifecycle-initial"
REFINED_SNAPSHOT_ID = "snapshot:lifecycle-refined"


def _fixture(*, key: str = FIXTURE_KEY, league: str = LEAGUE) -> Fixture:
    return Fixture(key, league, "Home FC", "Away FC", KICKOFF)


def _snapshot(
    *,
    captured_at: datetime,
    snapshot_id: str,
    fixture_key: str = FIXTURE_KEY,
    kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME,
    source: str = "the_odds_api:prematch",
) -> MarketSnapshot:
    return MarketSnapshot(
        fixture_key=fixture_key,
        captured_at=captured_at,
        kind=kind,
        source=source,
        odds={"home": 2.2, "draw": 3.3, "away": 4.0},
        snapshot_id=snapshot_id,
    )


def _initial_set(
    *,
    fixture_key: str = FIXTURE_KEY,
    league: str = LEAGUE,
    candidate: str = CANDIDATE,
    model: str = MODEL,
    kickoff: datetime = KICKOFF,
    now: datetime = BASE,
    captured_at: datetime | None = None,
    snapshot_id: str = INITIAL_SNAPSHOT_ID,
    probabilities: dict[str, float] = INITIAL_PROBABILITIES,
    implied_probabilities: dict[str, float] = MARKET_PROBABILITIES,
    edges: dict[str, float] | None = None,
    source_sha: str = SOURCE_SHA,
    research_sha: str = RESEARCH_SHA,
    model_artifact_hash: str = MODEL_HASH,
) -> tuple[Top5SignalLifecycle, ...]:
    source_time = captured_at or now - timedelta(minutes=2)
    fixture = Fixture(fixture_key, league, "Home FC", "Away FC", kickoff)
    selected_edges = (
        edges
        if edges is not None
        else {
            outcome: (probabilities[outcome] - implied_probabilities[outcome]) * 100
            for outcome in probabilities
        }
    )
    return tuple(
        create_initial_signal(
            fixture=fixture,
            snapshot=_snapshot(
                captured_at=source_time,
                snapshot_id=snapshot_id,
                fixture_key=fixture_key,
            ),
            now=now,
            market_id="1x2-regulation",
            outcome_id=outcome,
            candidate_id=candidate,
            model_identity=model,
            provider_identity=EXPECTED_PROVIDER_IDENTITY,
            probabilities=probabilities,
            source_sha=source_sha,
            research_sha=research_sha,
            model_artifact_hash=model_artifact_hash,
            eligibility_decision=True,
            decision_id=f"decision:initial:{outcome}",
            decision_reason="offline caller-approved test decision",
            implied_probabilities=implied_probabilities,
            edges=selected_edges,
        )
        for outcome in ("home", "draw", "away")
    )


def _refined_set(
    initial: tuple[Top5SignalLifecycle, ...],
    *,
    now: datetime | None = None,
    captured_at: datetime | None = None,
    snapshot_id: str = REFINED_SNAPSHOT_ID,
    probabilities: dict[str, float] = REFINED_PROBABILITIES,
    withdrawal: bool = False,
) -> tuple[Top5SignalLifecycle, ...]:
    current_time = now or KICKOFF - timedelta(minutes=90)
    source_time = captured_at or current_time - timedelta(minutes=2)
    fixture = _fixture()
    current_edges = {
        outcome: (probabilities[outcome] - MARKET_PROBABILITIES[outcome]) * 100
        for outcome in probabilities
    }
    classifications = {
        "home": RefinementClassification.STRENGTHENED,
        "draw": RefinementClassification.WEAKENED,
        "away": RefinementClassification.WEAKENED,
    }
    return tuple(
        refine_signal(
            lifecycle,
            fixture=fixture,
            snapshot=_snapshot(
                captured_at=source_time,
                snapshot_id=snapshot_id,
            ),
            now=current_time,
            probabilities=probabilities,
            eligibility_decision=not withdrawal,
            withdrawal_authorized=withdrawal,
            decision_id=f"decision:refined:{outcome}",
            decision_reason="offline caller-classified test decision",
            classification=(
                RefinementClassification.WITHDRAWN
                if withdrawal
                else classifications[outcome]
            ),
            implied_probabilities=MARKET_PROBABILITIES,
            edges=current_edges,
        )
        for lifecycle, outcome in zip(initial, ("home", "draw", "away"), strict=True)
    )


def _adapter_projection(
    lifecycles: tuple[Top5SignalLifecycle, ...], **overrides: object
) -> dict[str, dict[str, object]]:
    current = lifecycles[0].current_version
    arguments: dict[str, object] = {
        "prediction_probabilities": dict(current.probabilities),
        "fixture_identity": current.fixture_key,
        "league_identity": current.league_code,
        "candidate_identity": current.candidate_id,
        "model_identity": current.model_identity,
        "provider_authority": current.provider_identity,
        "prediction_timestamp": current.prediction_generated_at,
        "signal_timestamp": current.odds_captured_at,
        "snapshot_id": current.snapshot_id,
        "provenance": {
            "source_sha": SOURCE_SHA,
            "research_sha": RESEARCH_SHA,
            "model_artifact_hash": MODEL_HASH,
        },
    }
    arguments.update(overrides)
    return project_top5_signal_lifecycles(lifecycles, **arguments)  # type: ignore[arg-type]


def _public_signals(
    lifecycles: tuple[Top5SignalLifecycle, ...],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    current = lifecycles[0].current_version
    lifecycle_by_market = _adapter_projection(lifecycles)
    provenance = {
        "source": EXPECTED_PROVIDER_IDENTITY,
        "provider": EXPECTED_PROVIDER_IDENTITY,
        "source_sha": SOURCE_SHA,
        "research_sha": RESEARCH_SHA,
        "model_artifact_hash": MODEL_HASH,
        "snapshot_id": current.snapshot_id,
        "captured_at": current.odds_captured_at.isoformat(),
    }
    envelope = {
        "record_type": "prediction_artifact",
        "prediction_artifact": {
            "league_code": current.league_code,
            "fixture_key": current.fixture_key,
            "prediction_id": "prediction:lifecycle-adapter",
            "model_identity": current.model_identity,
            "prediction_timestamp": current.prediction_generated_at.isoformat(),
            "signal_timestamp": current.odds_captured_at.isoformat(),
            "snapshot_id": current.snapshot_id,
            "snapshot_kind": "SIGNAL_TIME",
            "probabilities": dict(current.probabilities),
            "lifecycle_by_market": lifecycle_by_market,
        },
        "fixture": {
            "fixture_key": current.fixture_key,
            "home_team": "Home FC",
            "away_team": "Away FC",
            "kickoff": current.kickoff.isoformat(),
        },
        "provenance": provenance,
        "activation_state": "shadow",
        "publication_status": "UNPUBLISHED",
        "publication_enabled": False,
        "no_bet": True,
    }
    return envelope, lifecycle_by_market


def test_initial_refined_and_withdrawn_core_objects_round_trip_through_public_contract() -> (
    None
):
    initial = _initial_set()
    initial_envelope, initial_projection = _public_signals(initial)
    initial_public = map_prediction_to_public_football_signals(initial_envelope)
    assert len(initial_public) == 3
    assert {record["lifecycle"]["lifecycle_stage"] for record in initial_public} == {
        "INITIAL"
    }
    for outcome, public_lifecycle in initial_projection.items():
        core = next(
            item for item in initial if item.current_version.outcome_id == outcome
        )
        assert public_lifecycle["schema_version"] == TOP5_LIFECYCLE_SCHEMA_VERSION
        assert public_lifecycle["lifecycle_id"] == core.lifecycle_id
        assert public_lifecycle["initial_record_id"] == (
            f"top5-lifecycle-initial-v1:{core.initial_version.version_digest}"
        )
        assert public_lifecycle["lifecycle_version"] == 1
        assert public_lifecycle["initial_probability"] == INITIAL_PROBABILITIES[outcome]
        assert public_lifecycle["current_probability"] == INITIAL_PROBABILITIES[outcome]
        assert "provider_authority" not in public_lifecycle
        assert "provider" not in public_lifecycle["provenance_binding"]
        assert "evidence_digest" not in public_lifecycle["provenance_binding"]

    refined = _refined_set(initial)
    refined_envelope, refined_projection = _public_signals(refined)
    refined_public = map_prediction_to_public_football_signals(refined_envelope)
    assert len(refined_public) == 3
    by_outcome = {str(record["market"]): record for record in refined_public}
    for outcome, public_lifecycle in refined_projection.items():
        assert (
            public_lifecycle["lifecycle_id"]
            == initial_projection[outcome]["lifecycle_id"]
        )
        assert (
            public_lifecycle["initial_record_id"]
            == initial_projection[outcome]["initial_record_id"]
        )
        assert public_lifecycle["lifecycle_version"] == 2
        assert public_lifecycle["initial_probability"] == INITIAL_PROBABILITIES[outcome]
        assert public_lifecycle["current_probability"] == REFINED_PROBABILITIES[outcome]
        assert public_lifecycle["probability_delta"] == (
            REFINED_PROBABILITIES[outcome] - INITIAL_PROBABILITIES[outcome]
        )
        assert (
            public_lifecycle["initial_market_probability"]
            == MARKET_PROBABILITIES[outcome]
        )
        assert (
            public_lifecycle["current_market_probability"]
            == MARKET_PROBABILITIES[outcome]
        )
        assert public_lifecycle["edge_delta_pp"] == (
            public_lifecycle["current_edge_pp"] - public_lifecycle["initial_edge_pp"]
        )
        assert (
            public_lifecycle["provenance_binding"]["snapshot_id"] == REFINED_SNAPSHOT_ID
        )
        assert by_outcome[outcome]["provider"] == EXPECTED_PROVIDER_IDENTITY
        assert by_outcome[outcome]["no_bet"] is True

    collapsed = serialize_public_football_records(initial_public + refined_public)
    assert len(collapsed) == 3
    assert all(record["lifecycle"]["lifecycle_version"] == 2 for record in collapsed)
    assert len({record["lifecycle"]["lifecycle_id"] for record in collapsed}) == 3

    current = refined[0].current_version
    pwa = Top5PwaData(
        league=current.league_code,
        fixture=current.fixture_key,
        kickoff=current.kickoff,
        probabilities=dict(current.probabilities),
        model_identity=current.model_identity,
        signal_timestamp=current.odds_captured_at,
        provenance={
            "source_sha": SOURCE_SHA,
            "research_sha": RESEARCH_SHA,
            "model_artifact_hash": MODEL_HASH,
            "snapshot_id": current.snapshot_id,
        },
        lifecycle_by_market=refined_projection,
    )
    pwa_payload = pwa.as_payload()
    assert pwa_payload["activation_mode"] == "disabled"
    assert pwa_payload["publication_enabled"] is False
    assert pwa_payload["no_bet"] is True

    withdrawn = _refined_set(initial, withdrawal=True)
    withdrawn_envelope, withdrawn_projection = _public_signals(withdrawn)
    withdrawn_public = map_prediction_to_public_football_signals(withdrawn_envelope)
    assert all(
        item["lifecycle_stage"] == "WITHDRAWN"
        and item["refinement_classification"] == "WITHDRAWN"
        for item in withdrawn_projection.values()
    )
    assert all(record["signal_status"] != "ACTIVE" for record in withdrawn_public)
    assert all(record["no_bet"] is True for record in withdrawn_public)


def test_controlled_publisher_projects_typed_core_objects_before_public_serializer() -> (
    None
):
    payload = _controlled_payload(LEAGUE)
    record = dict(payload.football_records[0])
    fixture = dict(record["fixture"])
    fixture["kickoff"] = KICKOFF.isoformat()
    record.update(
        {
            "fixture": fixture,
            "prediction_timestamp": BASE.isoformat(),
            "signal_timestamp": BASE.isoformat(),
            "snapshot_id": payload.signal_time_experiment_id,
            "probabilities": INITIAL_PROBABILITIES,
            "odds": {},
            "market_id": "1x2-regulation",
            "top5_signal_lifecycles": _initial_set(
                fixture_key=str(fixture["fixture_key"]),
                league=LEAGUE,
                candidate=payload.candidate_id,
                model=payload.model_identity,
                kickoff=KICKOFF,
                now=BASE,
                captured_at=BASE,
                snapshot_id=payload.signal_time_experiment_id,
                source_sha=payload.source_sha,
                research_sha=payload.research_sha,
                model_artifact_hash=payload.model_artifact_hash,
            ),
        }
    )
    mapped = replace(payload, football_records=(record,))._as_public_records()
    assert len(mapped) == 3
    assert {record["lifecycle"]["lifecycle_stage"] for record in mapped} == {"INITIAL"}
    assert all(record["provider"] == "the_odds_api" for record in mapped)
    assert all(record["no_bet"] is True for record in mapped)
    assert all(record["publication_enabled"] is True for record in mapped)
    # This is an in-memory serializer test only; no writer or publisher is invoked.


@pytest.mark.parametrize(
    "lifecycles",
    [
        _initial_set()[:2],
        (_initial_set()[0], _initial_set()[1], _initial_set()[0]),
    ],
    ids=("missing-outcome", "duplicate-outcome"),
)
def test_incomplete_or_duplicate_outcome_sets_fail_closed(lifecycles) -> None:
    with pytest.raises(Top5SignalLifecyclePublicAdapterError):
        _adapter_projection(lifecycles)


def test_extra_outcome_fails_closed() -> None:
    fixture = _fixture()
    extra = create_initial_signal(
        fixture=fixture,
        snapshot=_snapshot(
            captured_at=BASE - timedelta(minutes=2), snapshot_id="extra"
        ),
        now=BASE,
        market_id="1x2-regulation",
        outcome_id="over",
        candidate_id=CANDIDATE,
        model_identity=MODEL,
        probabilities={**INITIAL_PROBABILITIES, "over": 0.0},
        source_sha=SOURCE_SHA,
        research_sha=RESEARCH_SHA,
        model_artifact_hash=MODEL_HASH,
        eligibility_decision=True,
        decision_id="decision:extra",
        decision_reason="offline test",
    )
    with pytest.raises(Top5SignalLifecyclePublicAdapterError):
        _adapter_projection((*_initial_set(), extra))


@pytest.mark.parametrize(
    "field,value",
    [
        ("fixture_identity", "EPL:fixture:wrong"),
        ("league_identity", "BL1"),
        ("candidate_identity", "candidate:wrong"),
        ("model_identity", "model:wrong"),
    ],
)
def test_fixture_league_candidate_and_model_mismatches_fail_closed(
    field, value
) -> None:
    with pytest.raises(Top5SignalLifecyclePublicAdapterError, match="binding mismatch"):
        _adapter_projection(_initial_set(), **{field: value})


def test_current_probability_snapshot_and_timestamps_must_match_prediction_artifact() -> (
    None
):
    lifecycles = _refined_set(_initial_set())
    bad_probabilities = dict(REFINED_PROBABILITIES, home=0.54)
    with pytest.raises(
        Top5SignalLifecyclePublicAdapterError, match="probability disagrees"
    ):
        _adapter_projection(lifecycles, prediction_probabilities=bad_probabilities)
    with pytest.raises(Top5SignalLifecyclePublicAdapterError, match="snapshot_id"):
        _adapter_projection(lifecycles, snapshot_id="snapshot:wrong")
    current = lifecycles[0].current_version
    with pytest.raises(
        Top5SignalLifecyclePublicAdapterError, match="prediction timestamp"
    ):
        _adapter_projection(
            lifecycles,
            prediction_timestamp=current.prediction_generated_at + timedelta(seconds=1),
        )
    with pytest.raises(Top5SignalLifecyclePublicAdapterError, match="odds timestamp"):
        _adapter_projection(
            lifecycles,
            signal_timestamp=current.odds_captured_at + timedelta(seconds=1),
        )


def test_provider_and_provenance_tampering_fail_closed_without_authority_leakage() -> (
    None
):
    with pytest.raises(
        Top5SignalLifecyclePublicAdapterError, match="provider authority"
    ):
        _adapter_projection(
            _initial_set(), provider_authority="therundown_experimental"
        )
    projected = _adapter_projection(
        _initial_set(),
        provenance={
            "source_sha": SOURCE_SHA,
            "research_sha": RESEARCH_SHA,
            "model_artifact_hash": MODEL_HASH,
            "provider_authority": "therundown_experimental",
        },
    )
    assert all(
        "provider_authority" not in lifecycle
        and "provider_authority" not in lifecycle["provenance_binding"]
        for lifecycle in projected.values()
    )

    original = _initial_set()
    tampered_version = replace(
        original[0].versions[0], source_sha="f" * 64, version_digest=""
    )
    tampered_lifecycle = replace(original[0], versions=(tampered_version,))
    with pytest.raises(
        Top5SignalLifecyclePublicAdapterError, match="provenance binding"
    ):
        _adapter_projection((tampered_lifecycle, *original[1:]))

    stale_digest_version = replace(original[0].versions[0], source_sha="f" * 64)
    stale_digest_lifecycle = replace(original[0], versions=(stale_digest_version,))
    with pytest.raises(Top5SignalLifecyclePublicAdapterError, match="digest mismatch"):
        project_top5_signal_lifecycles(
            (stale_digest_lifecycle, *original[1:]),
            prediction_probabilities=INITIAL_PROBABILITIES,
            fixture_identity=FIXTURE_KEY,
            league_identity=LEAGUE,
            candidate_identity=CANDIDATE,
            model_identity=MODEL,
            provider_authority=EXPECTED_PROVIDER_IDENTITY,
            prediction_timestamp=BASE,
            signal_timestamp=BASE - timedelta(minutes=2),
            snapshot_id=INITIAL_SNAPSHOT_ID,
            provenance={
                "source_sha": SOURCE_SHA,
                "research_sha": RESEARCH_SHA,
                "model_artifact_hash": MODEL_HASH,
            },
        )

    bad_version = replace(original[0].versions[0], version_number=2, version_digest="")
    malformed_version_lifecycle = replace(original[0], versions=(bad_version,))
    with pytest.raises(Top5SignalLifecyclePublicAdapterError):
        project_top5_signal_lifecycles(
            (malformed_version_lifecycle, *original[1:]),
            prediction_probabilities=INITIAL_PROBABILITIES,
            fixture_identity=FIXTURE_KEY,
            league_identity=LEAGUE,
            candidate_identity=CANDIDATE,
            model_identity=MODEL,
            provider_authority=EXPECTED_PROVIDER_IDENTITY,
            prediction_timestamp=BASE,
            signal_timestamp=BASE - timedelta(minutes=2),
            snapshot_id=INITIAL_SNAPSHOT_ID,
            provenance={
                "source_sha": SOURCE_SHA,
                "research_sha": RESEARCH_SHA,
                "model_artifact_hash": MODEL_HASH,
            },
        )


def test_foreign_provider_and_malformed_withdrawal_are_rejected_by_canonical_core() -> (
    None
):
    initial = _initial_set()
    foreign = replace(
        initial[0].versions[0],
        provider_identity="therundown_experimental",
        version_digest="",
    )
    with pytest.raises(
        Top5SignalLifecyclePublicAdapterError, match="provider identity"
    ):
        project_top5_signal_lifecycles(
            (replace(initial[0], versions=(foreign,)), *initial[1:]),
            prediction_probabilities=INITIAL_PROBABILITIES,
            fixture_identity=FIXTURE_KEY,
            league_identity=LEAGUE,
            candidate_identity=CANDIDATE,
            model_identity=MODEL,
            provider_authority=EXPECTED_PROVIDER_IDENTITY,
            prediction_timestamp=BASE,
            signal_timestamp=BASE - timedelta(minutes=2),
            snapshot_id=INITIAL_SNAPSHOT_ID,
            provenance={
                "source_sha": SOURCE_SHA,
                "research_sha": RESEARCH_SHA,
                "model_artifact_hash": MODEL_HASH,
            },
        )

    with pytest.raises(SignalLifecycleError, match="explicit withdrawal"):
        refine_signal(
            initial[0],
            fixture=_fixture(),
            snapshot=_snapshot(
                captured_at=KICKOFF - timedelta(minutes=92),
                snapshot_id=REFINED_SNAPSHOT_ID,
            ),
            now=KICKOFF - timedelta(minutes=90),
            probabilities=REFINED_PROBABILITIES,
            eligibility_decision=False,
            withdrawal_authorized=False,
            decision_id="decision:bad-withdrawal",
            decision_reason="offline negative test",
            classification=RefinementClassification.WITHDRAWN,
        )


def test_stale_and_closing_snapshots_remain_rejected_by_canonical_core() -> None:
    with pytest.raises(SignalLifecycleError, match="outside the stage window or stale"):
        _initial_set(captured_at=BASE - timedelta(minutes=16))
    with pytest.raises(SignalLifecycleError, match="closing odds"):
        create_initial_signal(
            fixture=_fixture(),
            snapshot=_snapshot(
                captured_at=BASE - timedelta(minutes=2),
                snapshot_id="closing-snapshot",
                kind=MarketSnapshotKind.CLOSING,
            ),
            now=BASE,
            market_id="1x2-regulation",
            outcome_id="home",
            candidate_id=CANDIDATE,
            model_identity=MODEL,
            probabilities=INITIAL_PROBABILITIES,
            source_sha=SOURCE_SHA,
            research_sha=RESEARCH_SHA,
            model_artifact_hash=MODEL_HASH,
            eligibility_decision=True,
            decision_id="decision:closing",
            decision_reason="offline negative test",
        )


def test_ambiguous_prebuilt_and_domain_lifecycle_inputs_are_rejected() -> None:
    payload = _controlled_payload(LEAGUE)
    record = dict(payload.football_records[0])
    record["top5_signal_lifecycles"] = _initial_set(
        fixture_key=f"{LEAGUE}:controlled-001",
        league=LEAGUE,
        candidate=payload.candidate_id,
        model=payload.model_identity,
        kickoff=KICKOFF,
        now=BASE,
        captured_at=BASE,
        snapshot_id=payload.signal_time_experiment_id,
    )
    record["lifecycle_by_market"] = {"home": {}}
    with pytest.raises(ProductionContractError, match="not both"):
        replace(payload, football_records=(record,))._as_public_records()


def test_public_projector_still_rejects_bad_current_probability() -> None:
    lifecycles = _initial_set()
    envelope, projected = _public_signals(lifecycles)
    bad_home = {
        **projected["home"],
        "current_probability": 0.9,
        "current_edge_pp": (0.9 - MARKET_PROBABILITIES["home"]) * 100,
        "edge_delta_pp": (0.9 - MARKET_PROBABILITIES["home"]) * 100
        - projected["home"]["initial_edge_pp"],
    }
    envelope["prediction_artifact"]["lifecycle_by_market"] = {
        **projected,
        "home": bad_home,
    }
    with pytest.raises(PublicFootballCompatibilityError, match="current_probability"):
        map_prediction_to_public_football_signals(envelope)


def test_absent_core_market_and_edge_history_remains_absent() -> None:
    no_history = _initial_set(implied_probabilities={}, edges={})
    projection = _adapter_projection(no_history)
    optional_fields = {
        "initial_market_probability",
        "current_market_probability",
        "initial_edge_pp",
        "current_edge_pp",
        "edge_delta_pp",
    }
    assert all(optional_fields.isdisjoint(value) for value in projection.values())


def test_public_chain_keeps_immutable_bindings_and_requires_snapshot_presence_in_all_versions() -> (
    None
):
    initial = _initial_set()
    refined = _refined_set(initial)
    initial_records = map_prediction_to_public_football_signals(
        _public_signals(initial)[0]
    )
    refined_records = map_prediction_to_public_football_signals(
        _public_signals(refined)[0]
    )
    assert (
        len(serialize_public_football_records(initial_records + refined_records)) == 3
    )

    altered = [dict(record) for record in refined_records]
    altered[0]["lifecycle"] = {
        **altered[0]["lifecycle"],
        "provenance_binding": {
            **altered[0]["lifecycle"]["provenance_binding"],
            "research_sha": "d" * 64,
        },
    }
    with pytest.raises(
        PublicFootballCompatibilityError, match="provenance binding mismatch"
    ):
        serialize_public_football_records(initial_records + altered)

    without_snapshot = [dict(record) for record in refined_records]
    without_snapshot[0]["lifecycle"] = {
        **without_snapshot[0]["lifecycle"],
        "provenance_binding": {
            key: value
            for key, value in without_snapshot[0]["lifecycle"][
                "provenance_binding"
            ].items()
            if key != "snapshot_id"
        },
    }
    with pytest.raises(PublicFootballCompatibilityError, match="provenance changed"):
        serialize_public_football_records(initial_records + without_snapshot)
