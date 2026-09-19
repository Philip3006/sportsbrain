from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_production_verification import (
    ACTIVE_FOOTBALL_AUTHORITY,
    CANDIDATE_PROVIDER,
    ActivationIdentity,
    MatchObservation,
    PreActivationBaseline,
    PublicationGateResult,
    ResourceEvidence,
    RollbackTrigger,
    RoutingEvidence,
    RuntimeEvidence,
    VerificationStatus,
    evaluate_publication_gate,
    verify_production,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
IDENTITY = ActivationIdentity("activation-1", "config-digest-1", "source-1", "research-1", "model-1", "candidate-1")


def _baseline(**overrides):
    values = {
        "captured_at": NOW,
        "activation": IDENTITY,
        "worker_health": "ok",
        "pwa_health": "ok",
        "football_scheduler_state": "active",
        "active_provider_order": (ACTIVE_FOOTBALL_AUTHORITY,),
        "launchd_expectations": {"aggregate_health": "active", "odds_refresh": "active"},
        "github_workflow_expectations": {"football_scan": "fallback-only"},
        "runtime_writer_state": {"runtime": "active"},
        "ledger_writer_state": {"financial": "active"},
        "football_health_artifacts": {"football": "ok", "top5": "ok"},
        "request_counts": {ACTIVE_FOOTBALL_AUTHORITY: 0},
        "quota_remaining": {ACTIVE_FOOTBALL_AUTHORITY: 100},
        "spend_units": {ACTIVE_FOOTBALL_AUTHORITY: 0.0},
        "expected_fixture_identities": {"fixture-1": ("event-1", "Home", "Away")},
    }
    values.update(overrides)
    return PreActivationBaseline(**values)


def _routing(**overrides):
    values = {
        "expected_provider": ACTIVE_FOOTBALL_AUTHORITY,
        "selected_provider": ACTIVE_FOOTBALL_AUTHORITY,
        "active_provider_order": (ACTIVE_FOOTBALL_AUTHORITY,),
        "observed_providers": (ACTIVE_FOOTBALL_AUTHORITY,),
    }
    values.update(overrides)
    return RoutingEvidence(**values)


def _observation(**overrides):
    values = {
        "fixture_key": "fixture-1",
        "event_identity": "event-1",
        "home_team": "Home",
        "away_team": "Away",
        "kickoff_at": NOW + timedelta(minutes=30),
        "observed_at": NOW - timedelta(seconds=30),
        "source_sha": IDENTITY.source_sha,
        "config_digest": IDENTITY.activation_digest,
        "market_kind": "signal_time",
        "odds": {"home": 2.1, "draw": 3.2, "away": 3.4},
    }
    values.update(overrides)
    return MatchObservation(**values)


def _runtime(**overrides):
    values = {
        "scan_refresh_exit_code": 0,
        "worker_health": "ok",
        "pwa_health": "ok",
        "football_scheduler_state": "active",
        "runtime_writer_state": {"runtime": "active"},
        "ledger_writer_state": {"financial": "active"},
        "football_health_artifacts": {"football": "ok", "top5": "ok"},
        "retry_budget": 1,
    }
    values.update(overrides)
    return RuntimeEvidence(**values)


def _resources(**overrides):
    values = {
        "request_counts": {ACTIVE_FOOTBALL_AUTHORITY: 1},
        "request_budgets": {ACTIVE_FOOTBALL_AUTHORITY: 2},
        "quota_before": {ACTIVE_FOOTBALL_AUTHORITY: 100},
        "quota_after": {ACTIVE_FOOTBALL_AUTHORITY: 99},
        "spend_delta": {ACTIVE_FOOTBALL_AUTHORITY: 1.0},
        "spend_budgets": {ACTIVE_FOOTBALL_AUTHORITY: 2.0},
    }
    values.update(overrides)
    return ResourceEvidence(**values)


def _verify(**overrides):
    values = {
        "baseline": _baseline(), "observed_activation": IDENTITY, "routing": _routing(),
        "observations": (_observation(),), "runtime": _runtime(), "resources": _resources(), "checked_at": NOW,
    }
    values.update(overrides)
    return verify_production(**values)


def test_success_is_exactly_production_verified():
    report = _verify()
    assert report.status is VerificationStatus.PRODUCTION_VERIFIED
    assert report.triggers == ()
    assert report.as_payload()["status"] == "PRODUCTION_VERIFIED"


@pytest.mark.parametrize(
    "kwargs, trigger",
    [
        ({"routing": _routing(selected_provider="wrong")}, RollbackTrigger.ROUTING_MISMATCH),
        ({"observations": (_observation(odds={"home": 2.0, "draw": 3.0}),)}, RollbackTrigger.INCOMPLETE_1X2),
        ({"observations": (_observation(observed_at=NOW - timedelta(hours=2)),)}, RollbackTrigger.STALE_OR_POST_KICKOFF_DATA),
        ({"runtime": _runtime(unexpected_scheduler_runs=1)}, RollbackTrigger.UNEXPECTED_SCHEDULER_ACTIVITY),
        ({"runtime": _runtime(retry_count=2)}, RollbackTrigger.UNCONTROLLED_RETRIES),
        ({"runtime": _runtime(publication_enabled=True)}, RollbackTrigger.PUBLICATION_LEAKAGE),
        ({"runtime": _runtime(ledger_mutations=1)}, RollbackTrigger.FINANCIAL_LEDGER_MUTATION),
        ({"runtime": _runtime(sealed_partition_mutations=1)}, RollbackTrigger.SEALED_PARTITION_MUTATION),
        ({"resources": _resources(quota_after={ACTIVE_FOOTBALL_AUTHORITY: 98})}, RollbackTrigger.UNEXPECTED_QUOTA_CONSUMPTION),
    ],
)
def test_hard_safety_findings_require_rollback(kwargs, trigger):
    report = _verify(**kwargs)
    assert report.status is VerificationStatus.ROLLBACK_REQUIRED
    assert trigger in report.triggers


def test_missing_evidence_is_blocked_without_claiming_verified():
    report = _verify(observations=())
    assert report.status is VerificationStatus.VERIFICATION_BLOCKED
    assert report.triggers == ()


def test_activation_identity_mismatch_requires_rollback():
    observed = ActivationIdentity(**{**IDENTITY.as_payload(), "activation_digest": "other"})
    report = _verify(observed_activation=observed)
    assert report.status is VerificationStatus.ROLLBACK_REQUIRED
    assert RollbackTrigger.ACTIVATION_IDENTITY_MISMATCH in report.triggers


def test_candidate_provider_cannot_enter_active_routing():
    report = _verify(routing=_routing(active_provider_order=(CANDIDATE_PROVIDER, ACTIVE_FOOTBALL_AUTHORITY)))
    assert report.status is VerificationStatus.ROLLBACK_REQUIRED
    assert RollbackTrigger.ROUTING_MISMATCH in report.triggers


def test_unexpected_active_provider_order_is_not_accepted():
    report = _verify(routing=_routing(active_provider_order=(ACTIVE_FOOTBALL_AUTHORITY, "other")))
    assert report.status is VerificationStatus.ROLLBACK_REQUIRED
    assert RollbackTrigger.ROUTING_MISMATCH in report.triggers


def test_runtime_writer_drift_requires_health_rollback():
    report = _verify(runtime=_runtime(runtime_writer_state={"runtime": "read_only"}))
    assert report.status is VerificationStatus.ROLLBACK_REQUIRED
    assert RollbackTrigger.HEALTH_FAILURE in report.triggers


def test_publication_requires_verified_identity_and_separate_ceo_authorization():
    report = _verify()
    blocked = evaluate_publication_gate(report, IDENTITY, ceo_publication_authorization=None)
    assert isinstance(blocked, PublicationGateResult)
    assert blocked.eligible is False
    assert "separate CEO publication authorization is required" in blocked.failures

    eligible = evaluate_publication_gate(report, IDENTITY, ceo_publication_authorization="ceo-publication-1")
    assert eligible.eligible is True
    assert eligible.as_payload()["publication_enabled"] is False


def test_baseline_requires_the_odds_api_and_forbids_candidate():
    with pytest.raises(ValueError, match="The Odds API"):
        _baseline(active_provider_order=("therundown",)).validate()


def test_no_observation_can_use_closing_market_or_post_kickoff_data():
    report = _verify(observations=(_observation(market_kind="closing"),))
    assert report.status is VerificationStatus.ROLLBACK_REQUIRED
    assert RollbackTrigger.STALE_OR_POST_KICKOFF_DATA in report.triggers
