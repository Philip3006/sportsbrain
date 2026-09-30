"""Deterministic, read-only observability for the Top-5 canary.

This module evaluates evidence that a future canary runner already produced.
It deliberately has no provider, credential, scheduler, publisher, storage, or
runtime-writer dependency.  A public health/readiness consumer can therefore
call it without creating a hidden provider request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from src.football.production_contracts import ProductionContractError, _utc
from src.football.top5_b4_provider_neutral_evidence import canonical_evidence_digest

CANARY_OBSERVABILITY_SCHEMA = "top5-canary-observability-v1"
CANARY_EVIDENCE_BUNDLE_SCHEMA = "top5-canary-evidence-bundle-v1"
TOP5_CANARY_LEAGUES: tuple[str, ...] = ("EPL", "BL1", "LL", "SA", "L1")
TOP5_PRODUCTION_PROVIDER = "the_odds_api"
TOP5_CANDIDATE_PROVIDER = "therundown_experimental"
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class CanaryHealthState(str, Enum):
    NOT_READY = "NOT_READY"
    READY_FOR_CANARY = "READY_FOR_CANARY"
    CANARY_RUNNING = "CANARY_RUNNING"
    CANARY_PASSED = "CANARY_PASSED"
    CANARY_FAILED = "CANARY_FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class CanaryDomainState(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


class CanaryObservabilityError(ProductionContractError):
    """Malformed evidence supplied to the read-only evaluator."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanaryObservabilityError(f"{name} must be non-empty text")
    return value.strip()


def _digest(value: object, name: str) -> str:
    result = _text(value, name)
    if _DIGEST_RE.fullmatch(result) is None:
        raise CanaryObservabilityError(f"{name} must be a SHA-256 digest")
    return result.lower()


def _parse_time(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise CanaryObservabilityError(
                f"{name} must be an ISO-8601 timestamp"
            ) from exc
    raise CanaryObservabilityError(f"{name} must be an ISO-8601 timestamp")


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CanaryObservabilityError(f"{name} must be a non-negative integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise CanaryObservabilityError(f"{name} must be boolean")
    return value


def _failure(name: str, reason: str) -> dict[str, str]:
    return {"domain": name, "reason": reason}


@dataclass(frozen=True)
class CanaryFixtureEvidence:
    """One redacted fixture-level canary observation."""

    fixture_id: str
    league: str
    lifecycle_stage: str
    lead_seconds: int
    provider_response_at: datetime
    signal_decision: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CanaryFixtureEvidence:
        if not isinstance(value, Mapping):
            raise CanaryObservabilityError("fixture evidence must be an object")
        return cls(
            fixture_id=_text(value.get("fixture_id"), "fixture_id"),
            league=_text(value.get("league"), "league"),
            lifecycle_stage=_text(value.get("lifecycle_stage"), "lifecycle_stage"),
            lead_seconds=_nonnegative_int(value.get("lead_seconds"), "lead_seconds"),
            provider_response_at=_parse_time(
                value.get("provider_response_at"), "provider_response_at"
            ),
            signal_decision=_text(value.get("signal_decision"), "signal_decision"),
        )

    def validate(self, *, now: datetime, response_max_age_seconds: int) -> None:
        if self.lifecycle_stage == "INITIAL":
            if not 22 * 3600 <= self.lead_seconds <= 26 * 3600:
                raise CanaryObservabilityError(
                    f"{self.league}: INITIAL lead window is outside 22-26 hours"
                )
        elif self.lifecycle_stage == "REFINEMENT":
            if not 60 * 60 <= self.lead_seconds <= 120 * 60:
                raise CanaryObservabilityError(
                    f"{self.league}: REFINEMENT lead window is outside 60-120 minutes"
                )
        else:
            raise CanaryObservabilityError(
                f"{self.league}: lifecycle stage is unsupported"
            )
        if self.signal_decision not in {"SIGNAL", "NO_SIGNAL"}:
            raise CanaryObservabilityError(f"{self.league}: signal decision is invalid")
        age = (now - self.provider_response_at).total_seconds()
        if age < 0 or age > response_max_age_seconds:
            raise CanaryObservabilityError(
                f"{self.league}: provider response is stale or future-dated"
            )

    def as_payload(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "league": self.league,
            "lifecycle_stage": self.lifecycle_stage,
            "lead_seconds": self.lead_seconds,
            "provider_response_at": self.provider_response_at.isoformat(),
            "signal_decision": self.signal_decision,
        }


@dataclass(frozen=True)
class CanaryEvaluation:
    """A deterministic health decision and its redacted evidence bundle."""

    state: CanaryHealthState
    passed: bool
    reasons: tuple[dict[str, str], ...]
    health_artifact: dict[str, object]
    evidence_bundle: dict[str, object]

    @property
    def evidence_digest(self) -> str:
        return str(self.evidence_bundle["evidence_digest"])


def _domain_payload(
    state: CanaryDomainState, reasons: Sequence[str] = ()
) -> dict[str, object]:
    return {"state": state.value, "reasons": list(reasons)}


def _base_domains() -> dict[str, dict[str, object]]:
    return {
        "runtime": _domain_payload(CanaryDomainState.UNKNOWN),
        "provider_quota": _domain_payload(CanaryDomainState.UNKNOWN),
        "fixture_binding": _domain_payload(CanaryDomainState.UNKNOWN),
        "lifecycle": _domain_payload(CanaryDomainState.UNKNOWN),
        "batch_storage": _domain_payload(CanaryDomainState.UNKNOWN),
        "public_route": _domain_payload(CanaryDomainState.UNKNOWN),
        "observability": _domain_payload(CanaryDomainState.UNKNOWN),
        "rollback": _domain_payload(CanaryDomainState.UNKNOWN),
    }


def _failure_evaluation(
    *,
    now: datetime,
    execution_id: str,
    reasons: Sequence[dict[str, str]],
    state: CanaryHealthState = CanaryHealthState.CANARY_FAILED,
    domains: Mapping[str, Mapping[str, object]] | None = None,
) -> CanaryEvaluation:
    reason_values = tuple(reasons)
    domain_payload = {
        name: dict(value) for name, value in (domains or _base_domains()).items()
    }
    health = {
        "schema_version": CANARY_OBSERVABILITY_SCHEMA,
        "generated_at": now.isoformat(),
        "state": state.value,
        "canary_execution_id": execution_id,
        "pass": False,
        "domains": domain_payload,
        "failure_reasons": list(reason_values),
        "provider_requests_from_observer": 0,
        "credentials_accessed_by_observer": False,
        "production_provider": TOP5_PRODUCTION_PROVIDER,
        "candidate_provider": TOP5_CANDIDATE_PROVIDER,
        "publication": False,
        "production_activation": False,
        "provider_authority_granted": False,
        "no_bet": True,
    }
    bundle_body = {
        "schema_version": CANARY_EVIDENCE_BUNDLE_SCHEMA,
        "generated_at": now.isoformat(),
        "state": state.value,
        "canary_execution_id": execution_id,
        "failure_reasons": list(reason_values),
        "health_state": health,
    }
    bundle = {**bundle_body, "evidence_digest": canonical_evidence_digest(bundle_body)}
    return CanaryEvaluation(
        state=state,
        passed=False,
        reasons=reason_values,
        health_artifact=health,
        evidence_bundle=bundle,
    )


def evaluate_canary_evidence(
    evidence: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> CanaryEvaluation:
    """Evaluate a future canary result without side effects.

    The function returns a structured FAIL result for malformed or incomplete
    input.  It never calls a provider and never reads credentials or storage.
    """

    current = _utc(now or datetime.now(timezone.utc), "now")
    try:
        execution_id = _text(evidence.get("canary_execution_id"), "canary_execution_id")
        authorized_execution_id = _text(
            evidence.get("authorized_canary_execution_id"),
            "authorized_canary_execution_id",
        )
        if execution_id != authorized_execution_id:
            raise CanaryObservabilityError(
                "canary execution ID is not bound to authorization"
            )
        authorization_id = _text(evidence.get("authorization_id"), "authorization_id")
        authorization_valid = _boolean(
            evidence.get("authorization_valid"), "authorization_valid"
        )
        authorization_expires_at = _parse_time(
            evidence.get("authorization_expires_at"), "authorization_expires_at"
        )
        if not authorization_valid or authorization_expires_at <= current:
            raise CanaryObservabilityError("canary authorization is missing or expired")

        quota_observed_at = _parse_time(
            evidence.get("quota_observed_at"), "quota_observed_at"
        )
        quota_max_age = _nonnegative_int(
            evidence.get("quota_max_age_seconds"), "quota_max_age_seconds"
        )
        quota_age = (current - quota_observed_at).total_seconds()
        if quota_age < 0 or quota_age > quota_max_age:
            raise CanaryObservabilityError("quota evidence is stale or future-dated")

        response_max_age = _nonnegative_int(
            evidence.get("provider_response_max_age_seconds"),
            "provider_response_max_age_seconds",
        )
        fixtures_raw = evidence.get("fixtures")
        if isinstance(fixtures_raw, (str, bytes)) or not isinstance(
            fixtures_raw, Sequence
        ):
            raise CanaryObservabilityError("fixtures must be a sequence")
        fixtures = tuple(
            CanaryFixtureEvidence.from_mapping(item) for item in fixtures_raw
        )
        if len(fixtures) != len(TOP5_CANARY_LEAGUES):
            raise CanaryObservabilityError("canary must contain exactly five fixtures")
        fixture_ids = tuple(item.fixture_id for item in fixtures)
        if len(set(fixture_ids)) != len(fixture_ids):
            raise CanaryObservabilityError("fixture IDs must be unique")
        leagues = tuple(item.league for item in fixtures)
        if set(leagues) != set(TOP5_CANARY_LEAGUES) or len(set(leagues)) != len(
            leagues
        ):
            raise CanaryObservabilityError(
                "canary must contain one fixture per canonical Top-5 league"
            )
        authorized_fixture_ids = evidence.get("authorized_fixture_ids")
        if not isinstance(authorized_fixture_ids, Mapping):
            raise CanaryObservabilityError("authorized fixture IDs are missing")
        if set(authorized_fixture_ids) != set(TOP5_CANARY_LEAGUES):
            raise CanaryObservabilityError("authorized fixture ID scope is invalid")
        if any(
            authorized_fixture_ids[league] != fixture_id
            for league, fixture_id in (
                (item.league, item.fixture_id) for item in fixtures
            )
        ):
            raise CanaryObservabilityError("fixture IDs are not bound to authorization")
        for fixture in fixtures:
            fixture.validate(now=current, response_max_age_seconds=response_max_age)

        if not _boolean(evidence.get("model_completion"), "model_completion"):
            raise CanaryObservabilityError("model completion evidence is missing")
        _text(evidence.get("batch_id"), "batch_id")
        _text(evidence.get("generation_id"), "generation_id")
        if evidence.get("batch_state") == "ROLLED_BACK":
            return _failure_evaluation(
                now=current,
                execution_id=execution_id,
                state=CanaryHealthState.ROLLED_BACK,
                reasons=(_failure("batch_storage", "batch was rolled back"),),
            )
        if evidence.get("batch_state") != "COMPLETE":
            raise CanaryObservabilityError("batch state is not COMPLETE")
        private_digest = _digest(
            evidence.get("private_storage_digest"), "private_storage_digest"
        )
        public_digest = _digest(
            evidence.get("public_storage_digest"), "public_storage_digest"
        )
        worker_digest = _digest(
            evidence.get("worker_route_digest"), "worker_route_digest"
        )
        if private_digest != public_digest or public_digest != worker_digest:
            raise CanaryObservabilityError(
                "private, public, and Worker digests do not match"
            )
        if not _boolean(
            evidence.get("signals_json_compatible"), "signals_json_compatible"
        ):
            raise CanaryObservabilityError(
                "signals.json compatibility evidence is missing"
            )
        rollback_digest = _digest(
            evidence.get("rollback_snapshot_digest"), "rollback_snapshot_digest"
        )
        if not _boolean(evidence.get("rollback_capable"), "rollback_capable"):
            raise CanaryObservabilityError("rollback capability is missing")
        if (
            _nonnegative_int(
                evidence.get("public_read_provider_calls"), "public_read_provider_calls"
            )
            != 0
        ):
            raise CanaryObservabilityError("public reads must make zero provider calls")

        for name, expected in (
            ("no_bet", True),
            ("publication", False),
            ("production_activation", False),
            ("provider_authority_granted", False),
        ):
            if evidence.get(name) is not expected:
                raise CanaryObservabilityError(f"{name} safety boundary is invalid")
        if evidence.get("production_provider") != TOP5_PRODUCTION_PROVIDER:
            raise CanaryObservabilityError(
                "production provider authority is not the_odds_api"
            )
        if evidence.get("candidate_provider") != TOP5_CANDIDATE_PROVIDER:
            raise CanaryObservabilityError("candidate provider identity is invalid")

        domains = {
            "runtime": _domain_payload(CanaryDomainState.READY),
            "provider_quota": _domain_payload(CanaryDomainState.READY),
            "fixture_binding": _domain_payload(CanaryDomainState.READY),
            "lifecycle": _domain_payload(CanaryDomainState.READY),
            "batch_storage": _domain_payload(CanaryDomainState.READY),
            "public_route": _domain_payload(CanaryDomainState.READY),
            "observability": _domain_payload(CanaryDomainState.READY),
            "rollback": _domain_payload(CanaryDomainState.READY),
        }
        fixture_payload = [fixture.as_payload() for fixture in fixtures]
        body: dict[str, object] = {
            "schema_version": CANARY_EVIDENCE_BUNDLE_SCHEMA,
            "generated_at": current.isoformat(),
            "state": CanaryHealthState.CANARY_PASSED.value,
            "canary_execution_id": execution_id,
            "authorization_id": authorization_id,
            "authorized_fixture_ids": {
                league: authorized_fixture_ids[league] for league in TOP5_CANARY_LEAGUES
            },
            "fixtures": fixture_payload,
            "batch_id": evidence["batch_id"],
            "generation_id": evidence["generation_id"],
            "batch_state": "COMPLETE",
            "private_storage_digest": private_digest,
            "public_storage_digest": public_digest,
            "worker_route_digest": worker_digest,
            "rollback_snapshot_digest": rollback_digest,
            "decision_states": {
                fixture.league: fixture.signal_decision for fixture in fixtures
            },
            "domains": domains,
            "production_provider": TOP5_PRODUCTION_PROVIDER,
            "candidate_provider": TOP5_CANDIDATE_PROVIDER,
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "provider_authority_granted": False,
            "public_read_provider_calls": 0,
            "signals_json_compatible": True,
            "rollback_capable": True,
        }
        bundle = {**body, "evidence_digest": canonical_evidence_digest(body)}
        health = {
            "schema_version": CANARY_OBSERVABILITY_SCHEMA,
            "generated_at": current.isoformat(),
            "state": CanaryHealthState.CANARY_PASSED.value,
            "canary_execution_id": execution_id,
            "fixture_ids": list(fixture_ids),
            "leagues": list(TOP5_CANARY_LEAGUES),
            "pass": True,
            "domains": domains,
            "failure_reasons": [],
            "provider_requests_from_observer": 0,
            "credentials_accessed_by_observer": False,
            "production_provider": TOP5_PRODUCTION_PROVIDER,
            "candidate_provider": TOP5_CANDIDATE_PROVIDER,
            "publication": False,
            "production_activation": False,
            "provider_authority_granted": False,
            "no_bet": True,
            "public_read_provider_calls": 0,
            "signals_json_compatible": True,
            "evidence_digest": bundle["evidence_digest"],
        }
        return CanaryEvaluation(
            state=CanaryHealthState.CANARY_PASSED,
            passed=True,
            reasons=(),
            health_artifact=health,
            evidence_bundle=bundle,
        )
    except (
        CanaryObservabilityError,
        ProductionContractError,
        TypeError,
        KeyError,
    ) as exc:
        execution_id = evidence.get("canary_execution_id")
        safe_execution_id = (
            execution_id.strip() if isinstance(execution_id, str) else "unknown"
        )
        message = str(exc).lower()
        if "quota" in message:
            safe_reason = "quota evidence is missing, stale, or invalid"
        elif "fixture" in message:
            safe_reason = "fixture binding evidence is missing or invalid"
        elif "batch state" in message:
            safe_reason = "batch state is incomplete or failed"
        elif "digest" in message:
            safe_reason = "private, public, or Worker digest evidence is invalid"
        elif "batch" in message or "rollback" in message:
            safe_reason = "batch, storage, or rollback evidence is missing or invalid"
        elif "lifecycle" in message or "lead window" in message:
            safe_reason = "lifecycle evidence is missing or invalid"
        elif "provider calls" in message:
            safe_reason = "public reads made an unexpected provider call"
        elif "provider" in message:
            safe_reason = "provider authority evidence is invalid"
        elif "authorization" in message:
            safe_reason = "authorization evidence is missing, expired, or invalid"
        elif "safety" in message or "bet" in message or "publication" in message:
            safe_reason = "canary safety boundary evidence is invalid"
        else:
            safe_reason = "required canary evidence is missing or invalid"
        return _failure_evaluation(
            now=current,
            execution_id=safe_execution_id,
            reasons=(_failure("observability", safe_reason),),
        )


__all__ = [
    "CANARY_EVIDENCE_BUNDLE_SCHEMA",
    "CANARY_OBSERVABILITY_SCHEMA",
    "TOP5_CANARY_LEAGUES",
    "TOP5_CANDIDATE_PROVIDER",
    "TOP5_PRODUCTION_PROVIDER",
    "CanaryDomainState",
    "CanaryEvaluation",
    "CanaryFixtureEvidence",
    "CanaryHealthState",
    "CanaryObservabilityError",
    "evaluate_canary_evidence",
]
