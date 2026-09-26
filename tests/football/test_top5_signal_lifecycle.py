from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
)
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    LifecyclePlanStatus,
    RefinementClassification,
    SignalLifecycleError,
    SignalLifecycleStage,
    Top5SignalLifecycle,
    Top5SignalLifecycleContract,
    Top5SignalLifecycleStore,
    create_initial_signal,
    lifecycle_state_path,
    plan_signal_lifecycle,
    refine_signal,
)

UTC = timezone.utc
KICKOFF = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SOURCE_SHA = "a" * 40
RESEARCH_SHA = "b" * 40
MODEL_HASH = "c" * 64


def fixture(*, fixture_key: str = "epl-1") -> Fixture:
    return (
        Fixture("epl-1", "EPL", "Home FC", "Away FC", KICKOFF)
        if fixture_key == "epl-1"
        else Fixture(fixture_key, "EPL", "Home FC", "Away FC", KICKOFF)
    )


def snapshot(
    captured_at: datetime,
    *,
    snapshot_id: str,
    fixture_key: str = "epl-1",
    kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME,
) -> MarketSnapshot:
    return MarketSnapshot(
        fixture_key=fixture_key,
        captured_at=captured_at,
        kind=kind,
        source="the_odds_api:prematch",
        odds={"home": 2.1, "draw": 3.2, "away": 3.6},
        snapshot_id=snapshot_id,
    )


def initial(
    *, now: datetime | None = None, fixture_value: Fixture | None = None
) -> Top5SignalLifecycle:
    observed_at = now or KICKOFF - timedelta(hours=24)
    resolved_fixture = fixture_value or fixture()
    return create_initial_signal(
        fixture=resolved_fixture,
        snapshot=snapshot(
            observed_at - timedelta(minutes=5),
            snapshot_id="initial-1",
            fixture_key=resolved_fixture.fixture_key,
        ),
        now=observed_at,
        market_id="1x2-regulation",
        outcome_id="home",
        candidate_id="candidate-a",
        model_identity="model-a@" + MODEL_HASH,
        probabilities={"home": 0.5, "draw": 0.27, "away": 0.23},
        source_sha=SOURCE_SHA,
        research_sha=RESEARCH_SHA,
        model_artifact_hash=MODEL_HASH,
        eligibility_decision=True,
        decision_id="decision-initial-1",
        decision_reason="caller supplied eligibility decision",
        confidence_metadata={"method": "caller-provided"},
    )


def refinement(
    lifecycle: Top5SignalLifecycle,
    *,
    now: datetime | None = None,
    classification: RefinementClassification | None = None,
    eligible: bool = True,
    withdrawal_authorized: bool = False,
    snapshot_id: str = "refinement-1",
) -> Top5SignalLifecycle:
    observed_at = now or KICKOFF - timedelta(minutes=90)
    return refine_signal(
        lifecycle,
        fixture=fixture(),
        snapshot=snapshot(observed_at - timedelta(minutes=2), snapshot_id=snapshot_id),
        now=observed_at,
        probabilities={"home": 0.49, "draw": 0.28, "away": 0.23},
        eligibility_decision=eligible,
        withdrawal_authorized=withdrawal_authorized,
        decision_id="decision-refinement-1",
        decision_reason="explicit caller classification",
        classification=(
            classification
            or (
                RefinementClassification.WITHDRAWN
                if withdrawal_authorized
                else RefinementClassification.UNCHANGED
            )
        ),
    )


@pytest.mark.parametrize("lead_hours", [22, 24, 26])
def test_initial_stage_accepts_inclusive_22_to_26_hour_window(lead_hours: int) -> None:
    now = KICKOFF - timedelta(hours=lead_hours)
    result = initial(now=now)
    assert result.initial_version.stage is SignalLifecycleStage.INITIAL
    assert result.initial_version.prediction_generated_at == now


@pytest.mark.parametrize(
    "lead_hours",
    [21.99, 26.01],
)
def test_initial_stage_rejects_outside_window(lead_hours: float) -> None:
    now = KICKOFF - timedelta(hours=lead_hours)
    with pytest.raises(SignalLifecycleError):
        initial(now=now)


@pytest.mark.parametrize("lead_minutes", [60, 90, 120])
def test_refinement_stage_accepts_inclusive_60_to_120_minute_window(
    lead_minutes: int,
) -> None:
    result = refinement(initial(), now=KICKOFF - timedelta(minutes=lead_minutes))
    assert result.current_version.stage is SignalLifecycleStage.REFINED


@pytest.mark.parametrize("lead_minutes", [59.99, 120.01])
def test_refinement_stage_rejects_outside_window(lead_minutes: float) -> None:
    with pytest.raises(SignalLifecycleError):
        refinement(initial(), now=KICKOFF - timedelta(minutes=lead_minutes))


def test_both_stages_require_fresh_nonfuture_signal_time_snapshots() -> None:
    first = initial()
    first_now = KICKOFF - timedelta(hours=24)
    stale = snapshot(first_now - timedelta(seconds=901), snapshot_id="stale-initial")
    with pytest.raises(SignalLifecycleError, match="outside the stage window or stale"):
        create_initial_signal(
            fixture=fixture(),
            snapshot=stale,
            now=first_now,
            market_id="1x2",
            outcome_id="home",
            candidate_id="candidate-a",
            model_identity="model-a",
            probabilities={"home": 0.5},
            source_sha=SOURCE_SHA,
            research_sha=RESEARCH_SHA,
            model_artifact_hash=MODEL_HASH,
            eligibility_decision=True,
            decision_id="d",
            decision_reason="explicit",
        )

    future = snapshot(first_now + timedelta(seconds=1), snapshot_id="future")
    with pytest.raises(SignalLifecycleError):
        create_initial_signal(
            fixture=fixture(),
            snapshot=future,
            now=first_now,
            market_id="1x2",
            outcome_id="home",
            candidate_id="candidate-a",
            model_identity="model-a",
            probabilities={"home": 0.5},
            source_sha=SOURCE_SHA,
            research_sha=RESEARCH_SHA,
            model_artifact_hash=MODEL_HASH,
            eligibility_decision=True,
            decision_id="d",
            decision_reason="explicit",
        )

    closing = snapshot(
        first_now - timedelta(seconds=1),
        snapshot_id="closing",
        kind=MarketSnapshotKind.CLOSING,
    )
    with pytest.raises(SignalLifecycleError):
        create_initial_signal(
            fixture=fixture(),
            snapshot=closing,
            now=first_now,
            market_id="1x2",
            outcome_id="home",
            candidate_id="candidate-a",
            model_identity="model-a",
            probabilities={"home": 0.5},
            source_sha=SOURCE_SHA,
            research_sha=RESEARCH_SHA,
            model_artifact_hash=MODEL_HASH,
            eligibility_decision=True,
            decision_id="d",
            decision_reason="explicit",
        )
    assert first.initial_version.snapshot_kind is MarketSnapshotKind.SIGNAL_TIME


def test_contract_is_versioned_digest_bound_and_never_approves_production() -> None:
    contract = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT
    payload = contract.as_payload()
    assert contract.contract_id.startswith("top5-signal-lifecycle-v1-")
    assert payload["signal_time_approved_for_production"] is False
    assert payload["initial"]["retry_budget"] == 0
    assert payload["refinement"]["retry_budget"] == 0
    assert (
        Top5SignalLifecycleContract.from_payload(payload).contract_id
        == contract.contract_id
    )
    payload["signal_time_approved_for_production"] = True
    with pytest.raises(SignalLifecycleError):
        Top5SignalLifecycleContract.from_payload(payload)


def test_logical_identity_is_stable_across_versions_and_binds_signal_dimensions() -> (
    None
):
    original = initial()
    later_capture = replace(
        original.initial_version,
        odds_captured_at=original.initial_version.odds_captured_at
        - timedelta(seconds=1),
        prediction_generated_at=original.initial_version.prediction_generated_at
        + timedelta(seconds=1),
        version_digest="",
    )
    assert later_capture.lifecycle_id == original.lifecycle_id
    assert refinement(original).lifecycle_id == original.lifecycle_id
    assert (
        initial(fixture_value=fixture(fixture_key="epl-2")).lifecycle_id
        != original.lifecycle_id
    )
    different_candidate = create_initial_signal(
        fixture=fixture(),
        snapshot=snapshot(
            KICKOFF - timedelta(hours=24, minutes=5), snapshot_id="candidate-b"
        ),
        now=KICKOFF - timedelta(hours=24),
        market_id="1x2-regulation",
        outcome_id="home",
        candidate_id="candidate-b",
        model_identity="model-a@" + MODEL_HASH,
        probabilities={"home": 0.5},
        source_sha=SOURCE_SHA,
        research_sha=RESEARCH_SHA,
        model_artifact_hash=MODEL_HASH,
        eligibility_decision=True,
        decision_id="decision",
        decision_reason="explicit",
    )
    assert different_candidate.lifecycle_id != original.lifecycle_id


@pytest.mark.parametrize(
    "classification",
    [
        RefinementClassification.STRENGTHENED,
        RefinementClassification.WEAKENED,
        RefinementClassification.UNCHANGED,
    ],
)
def test_refinement_preserves_initial_and_records_explicit_classification(
    classification: RefinementClassification,
) -> None:
    first = initial()
    refined = refinement(first, classification=classification)
    assert len(refined.versions) == 2
    assert refined.versions[0] == first.versions[0]
    assert refined.current_version.classification is classification
    assert (
        refined.current_version.predecessor_version_digest
        == first.initial_version.version_digest
    )
    assert (
        refined.initial_version.prediction_generated_at
        == first.initial_version.prediction_generated_at
    )
    assert refined.current_version.snapshot_id != first.initial_version.snapshot_id
    assert refined.current_version.no_bet is True
    assert refined.current_version.publication_enabled is False
    assert refined.current_version.activation_enabled is False


def test_unavailable_refinement_preserves_initial_until_explicit_withdrawal() -> None:
    first = initial()
    with pytest.raises(SignalLifecycleError, match="explicit withdrawal"):
        refinement(first, eligible=False)
    assert first.versions == (first.initial_version,)
    withdrawn = refinement(first, eligible=False, withdrawal_authorized=True)
    assert withdrawn.current_version.stage is SignalLifecycleStage.WITHDRAWN
    assert withdrawn.current_version.withdrawal_authorized is True


def test_refinement_is_idempotent_but_conflicting_replay_fails() -> None:
    first = initial()
    refined = refinement(first)
    assert refinement(first) == refined
    with pytest.raises(SignalLifecycleError, match="conflicting replay"):
        refine_signal(
            refined,
            fixture=fixture(),
            snapshot=snapshot(
                KICKOFF - timedelta(minutes=92), snapshot_id="different-snapshot"
            ),
            now=KICKOFF - timedelta(minutes=90),
            probabilities={"home": 0.51},
            eligibility_decision=True,
            withdrawal_authorized=False,
            decision_id="other",
            decision_reason="different",
            classification=RefinementClassification.WEAKENED,
        )


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (27, LifecyclePlanStatus.INITIAL_NOT_DUE),
        (24, LifecyclePlanStatus.INITIAL_DUE),
        (21, LifecyclePlanStatus.INITIAL_MISSED),
    ],
)
def test_initial_planner_boundaries(hours: int, expected: LifecyclePlanStatus) -> None:
    plan = plan_signal_lifecycle(fixture(), KICKOFF - timedelta(hours=hours))
    assert plan.status is expected
    assert (plan.due_stage is SignalLifecycleStage.INITIAL) is (
        expected is LifecyclePlanStatus.INITIAL_DUE
    )


def test_refinement_planner_preserves_initial_and_never_widens_window() -> None:
    first = initial()
    assert (
        plan_signal_lifecycle(fixture(), KICKOFF - timedelta(hours=3), first).status
        is LifecyclePlanStatus.WAITING_FOR_REFINEMENT
    )
    due = plan_signal_lifecycle(fixture(), KICKOFF - timedelta(minutes=90), first)
    assert due.status is LifecyclePlanStatus.REFINEMENT_DUE
    assert due.due_stage is SignalLifecycleStage.REFINED
    assert (
        plan_signal_lifecycle(fixture(), KICKOFF - timedelta(minutes=59), first).status
        is LifecyclePlanStatus.REFINEMENT_MISSED
    )
    assert (
        plan_signal_lifecycle(
            fixture(), KICKOFF - timedelta(minutes=90), refinement(first)
        ).status
        is LifecyclePlanStatus.COMPLETE
    )
    assert (
        plan_signal_lifecycle(
            fixture(),
            KICKOFF - timedelta(minutes=90),
            refinement(first, eligible=False, withdrawal_authorized=True),
        ).status
        is LifecyclePlanStatus.WITHDRAWN
    )


def test_external_store_survives_restart_appends_once_and_detects_digest_corruption(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime-state"
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(runtime_root))
    first = initial()
    path = lifecycle_state_path(first.lifecycle_id)
    assert path.is_relative_to(runtime_root)
    assert not path.is_relative_to(
        __import__("src.runtime.paths", fromlist=["ROOT"]).ROOT
    )

    store = Top5SignalLifecycleStore()
    store.save(first)
    assert store.load(first.lifecycle_id) == first
    assert Top5SignalLifecycleStore().load(first.lifecycle_id) == first
    assert store.save(first) == first

    refined = refinement(first)
    store.save(refined)
    assert Top5SignalLifecycleStore().load(first.lifecycle_id) == refined
    assert path.stat().st_mode & 0o777 == 0o600

    path.write_text(
        path.read_text(encoding="utf-8").replace(first.lifecycle_id, "tampered"),
        encoding="utf-8",
    )
    with pytest.raises(SignalLifecycleError):
        store.load(first.lifecycle_id)


def test_durable_store_rejects_conflicting_nonappend_update(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    store = Top5SignalLifecycleStore()
    first = initial()
    store.save(first)
    alternative = create_initial_signal(
        fixture=fixture(),
        snapshot=snapshot(
            KICKOFF - timedelta(hours=24, minutes=5), snapshot_id="other-initial"
        ),
        now=KICKOFF - timedelta(hours=24),
        market_id="1x2-regulation",
        outcome_id="home",
        candidate_id="candidate-a",
        model_identity="model-a@" + MODEL_HASH,
        probabilities={"home": 0.5, "draw": 0.27},
        source_sha=SOURCE_SHA,
        research_sha=RESEARCH_SHA,
        model_artifact_hash=MODEL_HASH,
        eligibility_decision=True,
        decision_id="other",
        decision_reason="caller decision",
    )
    with pytest.raises(SignalLifecycleError, match="conflicting lifecycle replay"):
        store.save(alternative)


def test_canonical_contract_rejects_retries_closing_odds_and_authority_mutation() -> (
    None
):
    payload = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.as_payload()
    payload["initial"]["retry_budget"] = 1
    with pytest.raises(SignalLifecycleError):
        Top5SignalLifecycleContract.from_payload(payload)

    payload = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.as_payload()
    payload["initial"]["retry_budget"] = False
    with pytest.raises(SignalLifecycleError):
        Top5SignalLifecycleContract.from_payload(payload)

    with pytest.raises(SignalLifecycleError, match="timing policy is immutable"):
        Top5SignalLifecycleContract(
            initial=replace(
                DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.initial,
                maximum_minutes_before_kickoff=10_800,
            )
        ).validate()

    payload = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.as_payload()
    payload["no_closing_odds"] = False
    with pytest.raises(SignalLifecycleError):
        Top5SignalLifecycleContract.from_payload(payload)

    record = initial().initial_version
    altered = replace(record, no_bet=False, version_digest="")
    with pytest.raises(SignalLifecycleError):
        altered.validate()
    altered = replace(record, activation_enabled=True, version_digest="")
    with pytest.raises(SignalLifecycleError):
        altered.validate()
