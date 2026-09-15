"""Deterministic, no-network tests for the independent provider cascade gate."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import nan

import pytest

import src.football.top5_provider_cascade_validation as cascade_module
from src.football.top5_provider_cascade_validation import (
    CASCADE_PROVIDER_ORDER,
    BudgetDecision,
    CascadeAttempt,
    CascadeEvidence,
    CascadeOutcome,
    CascadeProvenance,
    CascadeQuotaSnapshot,
    CascadeSafety,
    CascadeValidationError,
    CascadeValidationPolicy,
    ExecutionMode,
    ExpectedCascadeFixture,
    MarketPhase,
    ProviderReadinessState,
    RequestCostClassification,
    SkippedProvider,
    ValidationCode,
    cascade_to_shadow_evidence_bundle,
    cascade_to_shadow_observation_evidence,
    validate_cascade_evidence,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_provider_redundancy import make_fixture_key

UTC = timezone.utc
CAPTURED = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
KICKOFF = CAPTURED + timedelta(hours=2)
EXPECTED = ExpectedCascadeFixture(
    league="EPL",
    fixture_key=make_fixture_key("EPL", "Manchester United", "Arsenal", KICKOFF),
    home_team="Manchester United",
    away_team="Arsenal",
    kickoff=KICKOFF,
)
READY = ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
SAFE = CascadeSafety(
    no_bet=True,
    publication_enabled=False,
    ledger_mutated=False,
    production_activation=False,
    sealed_data_accessed=False,
    research_mutated=False,
    monetary_spend_authorized=False,
)


def _policy(
    *,
    ready: bool = False,
    expected: ExpectedCascadeFixture | None = EXPECTED,
    order: tuple[str, ...] = CASCADE_PROVIDER_ORDER,
):
    return CascadeValidationPolicy(
        maximum_odds_age_seconds=900,
        kickoff_tolerance_seconds=300,
        configured_provider_order=order,
        provider_readiness=(
            {provider: READY for provider in CASCADE_PROVIDER_ORDER} if ready else {}
        ),
        expected_fixture=expected,
    )


def _provenance() -> CascadeProvenance:
    return CascadeProvenance(
        evidence_id="cascade-evidence-1",
        artifact_id="cascade-artifact-1",
        artifact_sha="a" * 64,
        source_sha="b" * 64,
        research_sha=FROZEN_RESEARCH_SHA,
        candidate_id="candidate-shadow-v1",
        model_identity="unbound-model-slot",
        generated_at=CAPTURED,
    )


def _quota(*, remaining: int, used: int) -> CascadeQuotaSnapshot:
    return CascadeQuotaSnapshot(
        authenticated=True,
        quota_used=used,
        quota_remaining=remaining,
    )


def _attempt(
    provider: str,
    index: int,
    *,
    outcome: CascadeOutcome = CascadeOutcome.SUCCESS,
    network_called: bool | None = None,
    preflight_allowed: bool | None = None,
    budget_decision: BudgetDecision = BudgetDecision.ALLOWED,
    cost: RequestCostClassification = RequestCostClassification.QUOTA_CONSUMING_REQUEST,
    cost_units: float | None = 1.0,
    network_request_count: int | None = None,
    credentials_available: bool | None = True,
    configured_order: tuple[str, ...] = CASCADE_PROVIDER_ORDER,
    quota_before: CascadeQuotaSnapshot | None = None,
    quota_after: CascadeQuotaSnapshot | None = None,
    readiness: ProviderReadinessState | None = None,
    start: datetime | None = None,
    market_phase: MarketPhase = MarketPhase.PRE_MATCH,
) -> CascadeAttempt:
    failed = outcome is not CascadeOutcome.SUCCESS
    if network_called is None:
        network_called = not (outcome is CascadeOutcome.QUOTA_EXHAUSTED)
    if network_request_count is None:
        network_request_count = int(network_called)
    if preflight_allowed is None:
        preflight_allowed = outcome is not CascadeOutcome.BUDGET_REJECTED and not (
            outcome is CascadeOutcome.QUOTA_EXHAUSTED and not network_called
        )
    start = start or CAPTURED + timedelta(seconds=index * 3)
    end = start + timedelta(seconds=1)
    if quota_before is None:
        quota_before = _quota(remaining=1, used=500 + index)
    if quota_after is None:
        before_remaining = quota_before.quota_remaining or 0
        quota_after = _quota(
            remaining=max(0, before_remaining - int(network_called)),
            used=quota_before.quota_used or 0,
        )
    if outcome is CascadeOutcome.QUOTA_EXHAUSTED:
        quota_before = _quota(remaining=0, used=500)
        quota_after = _quota(remaining=0, used=500)
        cost_units = 0.0
    return CascadeAttempt(
        league=EXPECTED.league,
        fixture_key=EXPECTED.fixture_key,
        home_team=EXPECTED.home_team,
        away_team=EXPECTED.away_team,
        kickoff=EXPECTED.kickoff,
        configured_provider_order=configured_order,
        provider_attempt_index=index,
        fallback_depth=index,
        provider_identity=provider,
        network_called=network_called,
        start_timestamp=start,
        end_timestamp=end,
        capture_timestamp=end,
        outcome=outcome,
        failure_classification=outcome if failed else None,
        market_type="h2h_1x2",
        home_odds=2.2,
        draw_odds=3.4,
        away_odds=3.0,
        bookmaker_identity="test-bookmaker",
        source_identity=provider,
        market_phase=market_phase,
        source_timestamp=CAPTURED - timedelta(minutes=5),
        request_latency_ms=100 + index,
        quota_before=quota_before,
        quota_after=quota_after,
        preflight_allowed=preflight_allowed,
        budget_decision=budget_decision,
        request_cost_classification=cost,
        network_request_count=network_request_count,
        quota_cost_units=cost_units,
        credentials_available=credentials_available,
        provider_record_id=f"{provider}-record-{index}",
        adapter_version=f"{provider}-adapter-v1",
        raw_record_digest="d" * 64,
        request_identity=f"request-{provider}-{index}",
        provider_readiness_state=readiness
        or (READY if not failed else ProviderReadinessState.CONTRACT_SUPPORTED),
    )


def _evidence(
    attempts: tuple[CascadeAttempt, ...],
    *,
    selected_provider: str | None,
    prediction_input_allowed: bool,
    skipped: tuple[SkippedProvider, ...] = (),
    mode: ExecutionMode = ExecutionMode.SEQUENTIAL,
    order: tuple[str, ...] | None = None,
) -> CascadeEvidence:
    order = order or attempts[0].configured_provider_order
    return CascadeEvidence(
        provenance=_provenance(),
        configured_provider_order=order,
        execution_mode=mode,
        attempts=attempts,
        skipped_providers=skipped,
        selected_provider=selected_provider,
        prediction_input_allowed=prediction_input_allowed,
        safety=SAFE,
    )


def _all_failed(
    *,
    outcomes: tuple[CascadeOutcome, ...] | None = None,
    order: tuple[str, ...] = CASCADE_PROVIDER_ORDER,
) -> CascadeEvidence:
    outcomes = outcomes or (
        CascadeOutcome.QUOTA_EXHAUSTED,
        CascadeOutcome.TIMEOUT,
        CascadeOutcome.HTTP_403,
        CascadeOutcome.PROVIDER_UNAVAILABLE,
    )
    return _evidence(
        tuple(
            _attempt(provider, index, outcome=outcome)
            for index, (provider, outcome) in enumerate(
                zip(order, outcomes, strict=True)
            )
        ),
        selected_provider=None,
        prediction_input_allowed=False,
        order=order,
    )


def test_policy_requires_caller_supplied_timing_and_exact_fixture() -> None:
    with pytest.raises(CascadeValidationError, match="expected fixture"):
        CascadeValidationPolicy(900, 300).validate()
    with pytest.raises(CascadeValidationError, match="timing"):
        CascadeValidationPolicy(0, 300, expected_fixture=EXPECTED).validate()
    with pytest.raises(CascadeValidationError, match="fuzzy"):
        _policy(expected=None).validate()


ALT_ORDER = (
    "odds_api_io",
    "api_football",
    "betfair_delayed",
    "the_odds_api",
)
SUBSET_ORDER = ("odds_api_io", "api_football", "betfair_delayed")


def _skipped_after(index: int, order: tuple[str, ...]) -> tuple[SkippedProvider, ...]:
    return tuple(
        SkippedProvider(provider, skipped_index, "terminal injected failure")
        for skipped_index, provider in enumerate(order[index + 1 :], start=index + 1)
    )


def test_default_alternative_and_subset_orders_are_configuration_not_authority() -> (
    None
):
    default = validate_cascade_evidence(
        _evidence(
            (_attempt("the_odds_api", 0, readiness=READY),),
            selected_provider="the_odds_api",
            prediction_input_allowed=True,
        ),
        _policy(ready=True),
    )
    assert default.accepted is True

    alternative = validate_cascade_evidence(
        _evidence(
            (
                _attempt(
                    "odds_api_io",
                    0,
                    configured_order=ALT_ORDER,
                    readiness=READY,
                ),
            ),
            selected_provider="odds_api_io",
            prediction_input_allowed=True,
            order=ALT_ORDER,
        ),
        _policy(ready=True, order=ALT_ORDER),
    )
    assert alternative.accepted is True
    assert alternative.selected_provider == "odds_api_io"

    subset = validate_cascade_evidence(
        _evidence(
            (
                _attempt(
                    "odds_api_io",
                    0,
                    configured_order=SUBSET_ORDER,
                    readiness=READY,
                ),
            ),
            selected_provider="odds_api_io",
            prediction_input_allowed=True,
            order=SUBSET_ORDER,
        ),
        _policy(ready=True, order=SUBSET_ORDER),
    )
    assert subset.accepted is True


def test_evidence_order_duplicate_and_unknown_provider_are_rejected() -> None:
    alternative = _evidence(
        (
            _attempt(
                "odds_api_io",
                0,
                configured_order=ALT_ORDER,
                readiness=READY,
            ),
        ),
        selected_provider="odds_api_io",
        prediction_input_allowed=True,
        order=ALT_ORDER,
    )
    mismatch = validate_cascade_evidence(alternative, _policy(ready=True))
    assert ValidationCode.CONFIGURATION_MISMATCH in mismatch.errors

    with pytest.raises(CascadeValidationError, match="duplicates"):
        _policy(order=("odds_api_io", "odds_api_io")).validate()
    with pytest.raises(CascadeValidationError, match="unknown"):
        _policy(order=("unknown_provider",)).validate()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("no_bet", False, ValidationCode.NO_BET_VIOLATION),
        ("publication_enabled", True, ValidationCode.PUBLICATION_ENABLED),
        ("ledger_mutated", True, ValidationCode.LEDGER_MUTATION),
        ("production_activation", True, ValidationCode.PRODUCTION_ACTIVATION),
        ("sealed_data_accessed", True, ValidationCode.SEALED_DATA_ACCESS),
        ("research_mutated", True, ValidationCode.RESEARCH_MUTATION),
        (
            "monetary_spend_authorized",
            True,
            ValidationCode.MONETARY_SPEND_AUTHORIZED,
        ),
    ],
)
def test_each_safety_violation_preserves_its_exact_code(field, value, code) -> None:
    unsafe = replace(SAFE, **{field: value})
    evidence = replace(
        _evidence(
            (_attempt("the_odds_api", 0),),
            selected_provider=None,
            prediction_input_allowed=False,
        ),
        safety=unsafe,
    )
    report = validate_cascade_evidence(evidence, _policy())
    assert report.accepted is False
    assert report.prediction_input_allowed is False
    assert report.selected_provider is None
    assert report.errors == (code,)


def test_multiple_safety_violations_preserve_all_codes_in_contract_order() -> None:
    unsafe = replace(
        SAFE,
        no_bet=False,
        publication_enabled=True,
        ledger_mutated=True,
        production_activation=True,
        sealed_data_accessed=True,
        research_mutated=True,
        monetary_spend_authorized=True,
    )
    evidence = replace(
        _evidence(
            (_attempt("the_odds_api", 0),),
            selected_provider="the_odds_api",
            prediction_input_allowed=True,
        ),
        safety=unsafe,
    )
    report = validate_cascade_evidence(evidence, _policy())
    assert report.errors == (
        ValidationCode.NO_BET_VIOLATION,
        ValidationCode.PUBLICATION_ENABLED,
        ValidationCode.LEDGER_MUTATION,
        ValidationCode.PRODUCTION_ACTIVATION,
        ValidationCode.SEALED_DATA_ACCESS,
        ValidationCode.RESEARCH_MUTATION,
        ValidationCode.MONETARY_SPEND_AUTHORIZED,
    )
    with pytest.raises(CascadeValidationError):
        cascade_to_shadow_observation_evidence(evidence, _policy())


def test_request_count_semantics_are_bounded_by_network_called() -> None:
    false_zero = _attempt(
        "the_odds_api",
        0,
        outcome=CascadeOutcome.TIMEOUT,
        network_called=False,
        cost_units=0.0,
    )
    assert (
        validate_cascade_evidence(
            _evidence(
                (false_zero,),
                selected_provider=None,
                prediction_input_allowed=False,
                skipped=_skipped_after(0, CASCADE_PROVIDER_ORDER),
            ),
            _policy(),
        ).accepted
        is True
    )

    false_one = replace(false_zero, network_request_count=1)
    false_one_report = validate_cascade_evidence(
        _evidence(
            (false_one,),
            selected_provider=None,
            prediction_input_allowed=False,
            skipped=_skipped_after(0, CASCADE_PROVIDER_ORDER),
        ),
        _policy(),
    )
    assert ValidationCode.REQUEST_COUNT_MISMATCH in false_one_report.errors

    true_one = _attempt("the_odds_api", 0, outcome=CascadeOutcome.TIMEOUT)
    assert (
        validate_cascade_evidence(
            _evidence(
                (true_one,),
                selected_provider=None,
                prediction_input_allowed=False,
                skipped=_skipped_after(0, CASCADE_PROVIDER_ORDER),
            ),
            _policy(),
        ).accepted
        is True
    )

    true_zero = replace(true_one, network_request_count=0)
    true_zero_report = validate_cascade_evidence(
        _evidence(
            (true_zero,),
            selected_provider=None,
            prediction_input_allowed=False,
            skipped=_skipped_after(0, CASCADE_PROVIDER_ORDER),
        ),
        _policy(),
    )
    assert ValidationCode.REQUEST_COUNT_MISMATCH in true_zero_report.errors

    too_many = replace(true_one, network_request_count=2)
    too_many_report = validate_cascade_evidence(
        _evidence(
            (too_many,),
            selected_provider=None,
            prediction_input_allowed=False,
            skipped=_skipped_after(0, CASCADE_PROVIDER_ORDER),
        ),
        _policy(),
    )
    assert ValidationCode.UNKNOWN_REQUEST_COUNT in too_many_report.errors


def test_valid_serialized_success_passes_and_selects_only_the_observed_source() -> None:
    evidence = _evidence(
        (_attempt("the_odds_api", 0, readiness=READY),),
        selected_provider="the_odds_api",
        prediction_input_allowed=True,
    )
    report = validate_cascade_evidence(evidence.as_payload(), _policy(ready=True))
    assert report.accepted is True
    assert report.prediction_input_allowed is True
    assert report.selected_provider == "the_odds_api"
    assert report.errors == ()
    assert report.metrics.provider_success_count == 1
    assert report.metrics.fixture_match_rate == 1.0
    assert report.metrics.complete_1x2_rate == 1.0
    assert report.metrics.freshness_acceptance_rate == 1.0
    assert report.metrics.latency_observations_ms == (100.0,)


def test_quota_exhaustion_denies_the_odds_api_credit_and_allows_serial_fallback() -> (
    None
):
    evidence = _evidence(
        (
            _attempt("the_odds_api", 0, outcome=CascadeOutcome.QUOTA_EXHAUSTED),
            _attempt("odds_api_io", 1, readiness=READY),
        ),
        selected_provider="odds_api_io",
        prediction_input_allowed=True,
    )
    report = validate_cascade_evidence(evidence, _policy(ready=True))
    assert report.accepted is True
    assert report.selected_provider == "odds_api_io"
    assert report.metrics.fallback_depth == 1
    assert report.metrics.successful_fallback_count == 1
    assert report.metrics.quota_rejected_before_network_count == 1
    first_attempt = evidence.attempts[0]
    assert first_attempt.network_called is False
    assert first_attempt.network_request_count == 0
    assert (
        first_attempt.request_cost_classification
        is RequestCostClassification.QUOTA_CONSUMING_REQUEST
    )
    assert SAFE.monetary_spend_authorized is False


def test_exhausted_provider_is_not_retried() -> None:
    retry = _evidence(
        (
            _attempt("the_odds_api", 0, outcome=CascadeOutcome.QUOTA_EXHAUSTED),
            _attempt("the_odds_api", 1, outcome=CascadeOutcome.QUOTA_EXHAUSTED),
        ),
        selected_provider=None,
        prediction_input_allowed=False,
    )
    report = validate_cascade_evidence(retry, _policy())
    assert ValidationCode.DUPLICATE_ATTEMPT in report.errors
    assert report.prediction_input_allowed is False


def test_all_four_failures_are_accepted_as_evidence_but_fail_closed() -> None:
    report = validate_cascade_evidence(_all_failed(), _policy())
    assert report.accepted is True
    assert report.fail_closed is True
    assert report.prediction_input_allowed is False
    assert report.selected_provider is None
    assert report.errors == ()
    assert report.metrics.provider_attempts == 4
    assert report.metrics.provider_success_count == 0
    assert report.metrics.fail_closed_count == 1


def test_success_stops_cascade_and_later_attempt_is_illegal() -> None:
    evidence = _evidence(
        (
            _attempt("the_odds_api", 0, readiness=READY),
            _attempt("odds_api_io", 1, outcome=CascadeOutcome.TIMEOUT),
        ),
        selected_provider="the_odds_api",
        prediction_input_allowed=True,
    )
    report = validate_cascade_evidence(evidence, _policy(ready=True))
    assert report.accepted is False
    assert ValidationCode.ILLEGAL_EXTRA_CALL in report.errors
    assert ValidationCode.FALLBACK_WITHOUT_FAILURE in report.errors
    assert report.prediction_input_allowed is False


def test_order_duplicate_and_skipped_provider_contracts_fail_closed() -> None:
    out_of_order = replace(_attempt("the_odds_api", 0), provider_attempt_index=1)
    report = validate_cascade_evidence(
        _evidence(
            (out_of_order,), selected_provider=None, prediction_input_allowed=False
        ),
        _policy(),
    )
    assert ValidationCode.ORDER_MISMATCH in report.errors

    duplicate = _evidence(
        (
            _attempt("the_odds_api", 0),
            _attempt("the_odds_api", 1, outcome=CascadeOutcome.TIMEOUT),
        ),
        selected_provider=None,
        prediction_input_allowed=False,
    )
    duplicate_report = validate_cascade_evidence(duplicate, _policy())
    assert ValidationCode.DUPLICATE_ATTEMPT in duplicate_report.errors

    skipped = SkippedProvider(
        "odds_api_io", 1, "provider disabled in injected scenario"
    )
    wrong_skipped = replace(skipped, provider_identity="api_football")
    skipped_evidence = _evidence(
        (_attempt("the_odds_api", 0, outcome=CascadeOutcome.TIMEOUT),),
        selected_provider=None,
        prediction_input_allowed=False,
        skipped=(wrong_skipped,),
    )
    skipped_report = validate_cascade_evidence(skipped_evidence, _policy())
    assert ValidationCode.SKIPPED_PROVIDER in skipped_report.errors


def test_parallel_or_overlapping_attempts_are_fanout_not_sequential_fallback() -> None:
    parallel = _evidence(
        (_attempt("the_odds_api", 0, outcome=CascadeOutcome.TIMEOUT),),
        selected_provider=None,
        prediction_input_allowed=False,
        mode=ExecutionMode.PARALLEL,
    )
    parallel_report = validate_cascade_evidence(parallel, _policy())
    assert ValidationCode.FANOUT_DETECTED in parallel_report.errors

    overlap = _evidence(
        (
            _attempt("the_odds_api", 0, outcome=CascadeOutcome.TIMEOUT),
            _attempt(
                "odds_api_io",
                1,
                start=CAPTURED + timedelta(milliseconds=500),
            ),
        ),
        selected_provider="odds_api_io",
        prediction_input_allowed=True,
    )
    overlap_report = validate_cascade_evidence(overlap, _policy(ready=True))
    assert ValidationCode.FANOUT_DETECTED in overlap_report.errors


def test_selected_provider_must_be_the_first_valid_success() -> None:
    success = _evidence(
        (_attempt("the_odds_api", 0, readiness=READY),),
        selected_provider="odds_api_io",
        prediction_input_allowed=True,
    )
    report = validate_cascade_evidence(success, _policy(ready=True))
    assert ValidationCode.SELECTED_PROVIDER_MISMATCH in report.errors

    rejected = _all_failed()
    rejected = replace(rejected, selected_provider="the_odds_api")
    rejected_report = validate_cascade_evidence(rejected, _policy())
    assert ValidationCode.SELECTED_REJECTED_PROVIDER in rejected_report.errors


def test_denied_network_and_budget_paths_fail_closed() -> None:
    preflight = _attempt(
        "the_odds_api",
        0,
        outcome=CascadeOutcome.TIMEOUT,
        preflight_allowed=False,
        network_called=True,
    )
    preflight_report = validate_cascade_evidence(
        _evidence((preflight,), selected_provider=None, prediction_input_allowed=False),
        _policy(),
    )
    assert ValidationCode.INVALID_SOURCE_CONTRACT in preflight_report.errors

    quota_network = _attempt(
        "the_odds_api",
        0,
        outcome=CascadeOutcome.QUOTA_EXHAUSTED,
        network_called=True,
        preflight_allowed=True,
    )
    quota_report = validate_cascade_evidence(
        _evidence(
            (quota_network,), selected_provider=None, prediction_input_allowed=False
        ),
        _policy(),
    )
    assert ValidationCode.NETWORK_AFTER_QUOTA_EXHAUSTED in quota_report.errors

    budget = _attempt(
        "the_odds_api",
        0,
        outcome=CascadeOutcome.BUDGET_REJECTED,
        preflight_allowed=False,
        network_called=False,
        budget_decision=BudgetDecision.REJECTED,
        cost_units=0.0,
    )
    budget_report = validate_cascade_evidence(
        _evidence(
            (budget,),
            selected_provider=None,
            prediction_input_allowed=False,
            skipped=tuple(
                SkippedProvider(
                    provider, index, "budget denied before provider attempt"
                )
                for index, provider in enumerate(CASCADE_PROVIDER_ORDER[1:], start=1)
            ),
        ),
        _policy(),
    )
    assert budget_report.accepted is True


def test_request_cost_authentication_and_unknown_budget_evidence_is_explicit() -> None:
    auth_failure = _attempt(
        "the_odds_api",
        0,
        outcome=CascadeOutcome.AUTH_FAILED,
        cost=RequestCostClassification.ZERO_COST_AUTHENTICATION,
        cost_units=0.0,
    )
    skipped = tuple(
        SkippedProvider(
            provider, index, "zero-cost authentication failed before odds request"
        )
        for index, provider in enumerate(CASCADE_PROVIDER_ORDER[1:], start=1)
    )
    auth_report = validate_cascade_evidence(
        _evidence(
            (auth_failure,),
            selected_provider=None,
            prediction_input_allowed=False,
            skipped=skipped,
        ),
        _policy(),
    )
    assert auth_report.accepted is True

    auth_success = _attempt(
        "the_odds_api",
        0,
        cost=RequestCostClassification.ZERO_COST_AUTHENTICATION,
        cost_units=0.0,
        readiness=READY,
    )
    auth_success_report = validate_cascade_evidence(
        _evidence(
            (auth_success,),
            selected_provider="the_odds_api",
            prediction_input_allowed=True,
        ),
        _policy(ready=True),
    )
    assert ValidationCode.INVALID_SUCCESS in auth_success_report.errors

    unknown_count = replace(_attempt("the_odds_api", 0), network_request_count=None)
    unknown_count_report = validate_cascade_evidence(
        _evidence(
            (unknown_count,),
            selected_provider="the_odds_api",
            prediction_input_allowed=True,
        ),
        _policy(ready=True),
    )
    assert ValidationCode.UNKNOWN_REQUEST_COUNT in unknown_count_report.errors

    missing_credentials = replace(
        _attempt("the_odds_api", 0), credentials_available=False
    )
    credentials_report = validate_cascade_evidence(
        _evidence(
            (missing_credentials,),
            selected_provider="the_odds_api",
            prediction_input_allowed=True,
        ),
        _policy(ready=True),
    )
    assert ValidationCode.CREDENTIAL_MISSING in credentials_report.errors


@pytest.mark.parametrize(
    ("outcome", "network_called"),
    [
        (CascadeOutcome.RATE_LIMITED, True),
        (CascadeOutcome.AUTH_FAILED, True),
        (CascadeOutcome.HTTP_401, True),
        (CascadeOutcome.HTTP_403, True),
        (CascadeOutcome.HTTP_404, True),
        (CascadeOutcome.HTTP_422, True),
        (CascadeOutcome.HTTP_429, True),
        (CascadeOutcome.HTTP_500, True),
        (CascadeOutcome.HTTP_502, True),
        (CascadeOutcome.HTTP_503, True),
        (CascadeOutcome.HTTP_5XX, True),
        (CascadeOutcome.TIMEOUT, True),
        (CascadeOutcome.MALFORMED, True),
        (CascadeOutcome.EMPTY_RESPONSE, False),
        (CascadeOutcome.STALE, True),
        (CascadeOutcome.UNSUPPORTED_FIXTURE, False),
        (CascadeOutcome.UNSUPPORTED_LEAGUE, False),
        (CascadeOutcome.UNSUPPORTED_MARKET, False),
        (CascadeOutcome.PARTIAL, True),
        (CascadeOutcome.QUALITY_REJECTED, True),
        (CascadeOutcome.CONFIG_DISABLED, False),
        (CascadeOutcome.CREDENTIAL_MISSING, False),
        (CascadeOutcome.BUDGET_REJECTED, False),
    ],
)
def test_failure_taxonomy_is_serializable_and_fail_closed(
    outcome: CascadeOutcome, network_called: bool
) -> None:
    attempt = _attempt(
        "the_odds_api",
        0,
        outcome=outcome,
        network_called=network_called,
        preflight_allowed=outcome not in {CascadeOutcome.BUDGET_REJECTED},
        budget_decision=(
            BudgetDecision.REJECTED
            if outcome is CascadeOutcome.BUDGET_REJECTED
            else BudgetDecision.ALLOWED
        ),
        cost_units=0.0 if not network_called else 1.0,
    )
    skipped = tuple(
        SkippedProvider(provider, index, "terminal injected failure")
        for index, provider in enumerate(CASCADE_PROVIDER_ORDER[1:], start=1)
    )
    record = _evidence(
        (attempt,),
        selected_provider=None,
        prediction_input_allowed=False,
        skipped=skipped,
    )
    report = validate_cascade_evidence(record.as_payload(), _policy())
    assert report.accepted is True
    assert report.fail_closed is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda attempt: replace(
            attempt,
            fixture_key=make_fixture_key("EPL", "Liverpool", "Arsenal", KICKOFF),
        ),
        lambda attempt: replace(
            attempt,
            league="BL1",
            fixture_key=make_fixture_key(
                "BL1", "Manchester United", "Arsenal", KICKOFF
            ),
        ),
        lambda attempt: replace(
            attempt,
            home_team="Liverpool",
            fixture_key=make_fixture_key("EPL", "Liverpool", "Arsenal", KICKOFF),
        ),
        lambda attempt: replace(
            attempt,
            home_team=EXPECTED.away_team,
            away_team=EXPECTED.home_team,
            fixture_key=make_fixture_key(
                "EPL", EXPECTED.away_team, EXPECTED.home_team, KICKOFF
            ),
        ),
        lambda attempt: replace(
            attempt,
            kickoff=KICKOFF + timedelta(minutes=6),
            fixture_key=make_fixture_key(
                "EPL",
                EXPECTED.home_team,
                EXPECTED.away_team,
                KICKOFF + timedelta(minutes=6),
            ),
        ),
        lambda attempt: replace(attempt, market_phase=MarketPhase.IN_PLAY),
        lambda attempt: replace(attempt, market_phase=MarketPhase.CLOSING),
        lambda attempt: replace(attempt, draw_odds=None),
        lambda attempt: replace(attempt, away_odds=None),
        lambda attempt: replace(
            attempt, source_timestamp=CAPTURED - timedelta(hours=2)
        ),
        lambda attempt: replace(
            attempt, source_timestamp=CAPTURED + timedelta(seconds=2)
        ),
        lambda attempt: replace(attempt, home_odds=1.1, draw_odds=1.1, away_odds=20.0),
        lambda attempt: replace(attempt, market_type="totals"),
        lambda attempt: replace(attempt, bookmaker_identity=None),
        lambda attempt: replace(attempt, source_identity=None),
    ],
)
def test_exact_fixture_and_quality_gates_reject_bad_success(mutator) -> None:
    attempt = mutator(_attempt("the_odds_api", 0, readiness=READY))
    report = validate_cascade_evidence(
        _evidence(
            (attempt,), selected_provider="the_odds_api", prediction_input_allowed=True
        ),
        _policy(ready=True),
    )
    assert report.accepted is False
    assert report.prediction_input_allowed is False


def test_missing_readiness_never_escalates_from_contract_support() -> None:
    attempt = _attempt(
        "odds_api_io",
        0,
        readiness=ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
    )
    report = validate_cascade_evidence(
        _evidence(
            (attempt,), selected_provider="odds_api_io", prediction_input_allowed=True
        ),
        _policy(),
    )
    assert ValidationCode.READINESS_ESCALATION in report.errors
    assert ValidationCode.PROVIDER_NOT_READY in report.errors


def test_malformed_unknown_enum_and_nan_payload_fail_as_invalid_contract() -> None:
    attempt = _attempt("the_odds_api", 0, readiness=READY)
    malformed = replace(
        attempt,
        outcome="NOT_A_REAL_OUTCOME",
        failure_classification="NOT_A_REAL_OUTCOME",
    )
    report = validate_cascade_evidence(
        _evidence((malformed,), selected_provider=None, prediction_input_allowed=False),
        _policy(ready=True),
    )
    assert report.errors == (ValidationCode.INVALID_SOURCE_CONTRACT,)

    nan_report = validate_cascade_evidence(
        _evidence(
            (replace(attempt, home_odds=nan),),
            selected_provider="the_odds_api",
            prediction_input_allowed=True,
        ),
        _policy(ready=True),
    )
    assert nan_report.errors == (ValidationCode.INVALID_SOURCE_CONTRACT,)


def test_bridge_to_builder_one_v1_remains_no_bet_and_does_not_create_prediction() -> (
    None
):
    evidence = _evidence(
        (_attempt("the_odds_api", 0, readiness=READY),),
        selected_provider="the_odds_api",
        prediction_input_allowed=True,
    )
    observation = cascade_to_shadow_observation_evidence(evidence, _policy(ready=True))
    assert observation.no_bet is True
    assert observation.publication_enabled is False
    assert observation.prediction_id is None
    assert observation.eligible is True
    assert observation.provider_covered is True

    bundle = cascade_to_shadow_evidence_bundle(evidence, _policy(ready=True))
    assert bundle.safety.no_bet is True
    assert bundle.safety.publication_enabled is False
    assert bundle.safety.real_bet_created is False
    assert bundle.safety.production_activation is False


def test_all_failed_bridge_is_validation_only_and_fail_closed() -> None:
    observation = cascade_to_shadow_observation_evidence(_all_failed(), _policy())
    assert observation.eligible is False
    assert observation.rejected is True
    assert observation.no_bet is True
    assert observation.prediction_id is None


def test_module_has_no_provider_client_or_execution_side_effect() -> None:
    source = inspect.getsource(cascade_module)
    assert "requests" not in source
    assert "httpx" not in source
    assert "urllib" not in source
    assert "import src.football.builder4" not in source
