from __future__ import annotations

from datetime import datetime, timezone

from src.football.top5_canary_observability import (
    TOP5_CANARY_LEAGUES,
    CanaryHealthState,
    evaluate_canary_evidence,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
FIXTURE_IDS = {
    "EPL": "fixture-epl-001",
    "BL1": "fixture-bl1-001",
    "LL": "fixture-ll-001",
    "SA": "fixture-sa-001",
    "L1": "fixture-l1-001",
}


def _digest(char: str) -> str:
    return char * 64


def _evidence(**overrides: object) -> dict[str, object]:
    fixtures = [
        {
            "fixture_id": FIXTURE_IDS[league],
            "league": league,
            "lifecycle_stage": "INITIAL",
            "lead_seconds": 24 * 3600,
            "provider_response_at": "2026-09-30T11:55:00+00:00",
            "signal_decision": "SIGNAL" if league in {"EPL", "LL"} else "NO_SIGNAL",
        }
        for league in TOP5_CANARY_LEAGUES
    ]
    result: dict[str, object] = {
        "canary_execution_id": "canary-20260930-001",
        "authorized_canary_execution_id": "canary-20260930-001",
        "authorization_id": "auth-20260930-001",
        "authorized_fixture_ids": dict(FIXTURE_IDS),
        "authorization_valid": True,
        "authorization_expires_at": "2026-09-30T13:00:00+00:00",
        "quota_observed_at": "2026-09-30T11:50:00+00:00",
        "quota_max_age_seconds": 3600,
        "provider_response_max_age_seconds": 900,
        "fixtures": fixtures,
        "model_completion": True,
        "batch_id": "batch-20260930-001",
        "generation_id": "generation-20260930-001",
        "batch_state": "COMPLETE",
        "private_storage_digest": _digest("a"),
        "public_storage_digest": _digest("a"),
        "worker_route_digest": _digest("a"),
        "signals_json_compatible": True,
        "rollback_snapshot_digest": _digest("b"),
        "rollback_capable": True,
        "public_read_provider_calls": 0,
        "production_provider": "the_odds_api",
        "candidate_provider": "therundown_experimental",
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "provider_authority_granted": False,
    }
    result.update(overrides)
    return result


def test_complete_bundle_is_deterministic_and_passes() -> None:
    first = evaluate_canary_evidence(_evidence(), now=NOW)
    second = evaluate_canary_evidence(_evidence(), now=NOW)

    assert first.passed is True
    assert first.state is CanaryHealthState.CANARY_PASSED
    assert first.evidence_digest == second.evidence_digest
    assert set(first.health_artifact["domains"]) == {
        "runtime",
        "provider_quota",
        "fixture_binding",
        "lifecycle",
        "batch_storage",
        "public_route",
        "observability",
        "rollback",
    }
    assert first.health_artifact["provider_requests_from_observer"] == 0
    assert first.health_artifact["credentials_accessed_by_observer"] is False


def test_missing_fixture_fails_closed() -> None:
    payload = _evidence(fixtures=_evidence()["fixtures"][:-1])
    result = evaluate_canary_evidence(payload, now=NOW)
    assert result.passed is False
    assert result.state is CanaryHealthState.CANARY_FAILED
    assert "fixture binding" in result.reasons[0]["reason"]


def test_stale_quota_fails_closed() -> None:
    result = evaluate_canary_evidence(
        _evidence(quota_observed_at="2026-09-30T10:00:00+00:00"), now=NOW
    )
    assert result.passed is False
    assert "quota evidence" in result.reasons[0]["reason"]


def test_incomplete_batch_fails_closed() -> None:
    result = evaluate_canary_evidence(_evidence(batch_state="RUNNING"), now=NOW)
    assert result.passed is False
    assert "batch state" in result.reasons[0]["reason"]


def test_storage_or_route_digest_mismatch_fails_closed() -> None:
    result = evaluate_canary_evidence(
        _evidence(worker_route_digest=_digest("c")), now=NOW
    )
    assert result.passed is False
    assert "digest evidence" in result.reasons[0]["reason"]


def test_missing_rollback_snapshot_fails_closed() -> None:
    result = evaluate_canary_evidence(_evidence(rollback_snapshot_digest=None), now=NOW)
    assert result.passed is False
    assert "digest evidence" in result.reasons[0]["reason"]


def test_failed_batch_and_public_reads_are_observable_failures() -> None:
    failed_batch = evaluate_canary_evidence(_evidence(batch_state="FAILED"), now=NOW)
    hidden_call = evaluate_canary_evidence(
        _evidence(public_read_provider_calls=1), now=NOW
    )
    assert failed_batch.state is CanaryHealthState.CANARY_FAILED
    assert hidden_call.state is CanaryHealthState.CANARY_FAILED
    assert "provider call" in hidden_call.reasons[0]["reason"]


def test_rolled_back_state_is_not_reported_as_pass() -> None:
    result = evaluate_canary_evidence(_evidence(batch_state="ROLLED_BACK"), now=NOW)
    assert result.passed is False
    assert result.state is CanaryHealthState.ROLLED_BACK


def test_invalid_authorization_and_safety_boundary_fail_closed() -> None:
    invalid_auth = evaluate_canary_evidence(
        _evidence(authorization_valid=False), now=NOW
    )
    unsafe = evaluate_canary_evidence(_evidence(publication=True), now=NOW)
    assert invalid_auth.passed is False
    assert unsafe.passed is False


def test_lifecycle_window_and_provider_identity_are_required() -> None:
    outside_window = _evidence()
    outside_window["fixtures"] = [
        {**item, "lead_seconds": 20 * 3600}
        for item in outside_window["fixtures"]  # type: ignore[index]
    ]
    wrong_provider = evaluate_canary_evidence(
        _evidence(candidate_provider="the_odds_api"), now=NOW
    )
    assert evaluate_canary_evidence(outside_window, now=NOW).passed is False
    assert wrong_provider.passed is False
