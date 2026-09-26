"""Explicit candidate-provider eligibility for non-authoritative shadows.

This contract deliberately separates candidate qualification eligibility from
the active Football provider repertoire.  It binds one independently captured
observation to the exact adapter, authorization/request scope, fixture,
participants, bookmaker, market, timing, provenance, digests, and quota
evidence required before it may enter the pure qualification gate.

It never registers a provider, changes routing order, enables publication or
betting, or issues a qualification receipt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite

from src.football.provider_cascade.contracts import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
    MARKET_PREMATCH_1X2,
    TOP5_LEAGUE_CODES,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key

CANDIDATE_PROVIDER_IDENTITIES = CANDIDATE_ONLY_PROVIDER_IDENTITIES
CANDIDATE_PROVIDER_REPERTOIRE = tuple(sorted(CANDIDATE_PROVIDER_IDENTITIES))
THERUNDOWN_RPS_PROVIDER_IDENTITIES = frozenset(
    {"therundown", "therundown_experimental"}
)
PREMATCH_MARKET_PHASE = "PRE_MATCH"
REAL_OBSERVED = "REAL_OBSERVED"
CAPTURE_TIME_ONLY = "CAPTURE_TIME_ONLY"
UNKNOWN_TIMESTAMP = "UNKNOWN"
PROVIDER_SOURCE_TIMESTAMP = "PROVIDER_SOURCE_TIMESTAMP"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


class CandidateEligibilityError(ValueError):
    """Malformed or insufficient candidate-provider evidence."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateEligibilityError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise CandidateEligibilityError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CandidateEligibilityError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _odds(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise CandidateEligibilityError(f"{name} must be a decimal price")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CandidateEligibilityError(f"{name} must be a decimal price") from exc
    if not isfinite(number) or number <= 1.0:
        raise CandidateEligibilityError(f"{name} must be a decimal price")
    return number


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CandidateEligibilityError(f"{name} must be non-negative or null")
    return value


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


@dataclass(frozen=True)
class CandidateProviderEligibilityV1:
    """One explicit candidate/shadow/qualification eligibility binding.

    The capability flags are intentionally asymmetric: candidate, shadow, and
    qualification may be true; every production-side capability is permanently
    false in this contract.
    """

    provider_identity: str
    candidate_capability: bool
    shadow_capability: bool
    qualification_capability: bool
    active_production_capability: bool
    publication_capability: bool
    scheduler_capability: bool
    betting_capability: bool
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    ceo_authorization_identity: str
    authorization_expires_at: datetime
    adapter_version: str
    adapter_source_sha: str
    configuration_digest: str
    league_code: str
    fixture_key: str
    provider_event_id: str
    home_participant_id: str
    away_participant_id: str
    request_identity: str
    home_team: str
    away_team: str
    kickoff: datetime
    bookmaker_identity: str
    market_type: str
    market_phase: str
    home_odds: float
    draw_odds: float
    away_odds: float
    source_timestamp: datetime
    captured_at: datetime
    request_started_at: datetime
    request_finished_at: datetime
    maximum_source_age_seconds: int
    source_provenance: str
    provider_timestamp_provenance: str
    raw_response_digest: str
    provider_record_digest: str
    normalized_record_digest: str
    cascade_evidence_digest: str
    quota_before: int | None
    quota_after: int | None
    quota_cost_units: float
    rate_limit_remaining: int | None
    rate_limit_reset_at: datetime | None
    account_tier: str
    provider_delay_seconds: float
    evidence_kind: str = REAL_OBSERVED
    network_execution: bool = True
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    rate_limit_limit: int | None = None
    schema_version: str | None = None

    @classmethod
    def from_network_capture(
        cls,
        capture: object,
        *,
        now: datetime | None = None,
        maximum_source_age_seconds: int = 300,
    ) -> CandidateProviderEligibilityV1:
        """Build eligibility only from a completed PR #103 capture envelope."""

        target = getattr(capture, "target", None)
        request = getattr(capture, "request", None)
        response = getattr(capture, "response", None)
        if target is None or request is None or response is None:
            raise CandidateEligibilityError("network capture envelope is required")
        provider_billing = response.raw_metadata.get("provider_billing")
        rate_limit_limit = None
        if isinstance(provider_billing, Mapping):
            rate_limit_limit = provider_billing.get("x-rate-limit")
            if rate_limit_limit is None:
                rate_limit_limit = provider_billing.get("x-ratelimit-limit")
        candidate = cls(
            provider_identity=target.provider,
            candidate_capability=True,
            shadow_capability=True,
            qualification_capability=True,
            active_production_capability=False,
            publication_capability=False,
            scheduler_capability=False,
            betting_capability=False,
            controlled_shadow_run_id=request.controlled_shadow_run_id,
            qualification_session_id=request.qualification_session_id,
            authorization_id=request.authorization_id,
            ceo_authorization_identity=request.ceo_authorization_identity,
            authorization_expires_at=request.authorization_expires_at,
            adapter_version=response.adapter_version,
            adapter_source_sha=response.adapter_source_sha,
            configuration_digest=request.configuration_digest,
            league_code=target.league,
            fixture_key=target.fixture_key,
            provider_event_id=response.provider_event_id,
            home_participant_id=request.home_participant_id,
            away_participant_id=request.away_participant_id,
            request_identity=response.provider_request_id,
            home_team=target.home_team,
            away_team=target.away_team,
            kickoff=target.kickoff,
            bookmaker_identity=response.bookmaker_identity,
            market_type=MARKET_PREMATCH_1X2,
            market_phase=PREMATCH_MARKET_PHASE,
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            source_timestamp=response.source_timestamp,
            captured_at=response.captured_at,
            request_started_at=response.request_started_at,
            request_finished_at=response.request_finished_at,
            maximum_source_age_seconds=maximum_source_age_seconds,
            source_provenance=response.source_identity,
            provider_timestamp_provenance=response.provider_timestamp_provenance,
            raw_response_digest=response.raw_response_digest,
            provider_record_digest=response.provider_record_digest,
            normalized_record_digest=response.normalized_record_digest,
            cascade_evidence_digest=response.cascade_evidence_digest,
            quota_before=response.quota_before,
            quota_after=response.quota_after,
            quota_cost_units=response.quota_cost_units,
            rate_limit_remaining=None,
            rate_limit_reset_at=None,
            account_tier=response.account_tier,
            provider_delay_seconds=response.provider_delay_seconds,
            evidence_kind=capture.evidence_kind,
            network_execution=capture.network_execution,
            no_bet=response.no_bet,
            publication=response.publication,
            production_activation=response.production_activation,
            monetary_spend_authorized=response.monetary_spend_authorized,
            rate_limit_limit=rate_limit_limit,
            schema_version=(
                "top5-candidate-provider-eligibility-v2"
                if target.provider in THERUNDOWN_RPS_PROVIDER_IDENTITIES
                else "top5-candidate-provider-eligibility-v1"
            ),
        )
        candidate.validate(now=now)
        if getattr(capture, "candidate_only", None) is not True:
            raise CandidateEligibilityError(
                "candidate capture must remain candidate-only"
            )
        if getattr(capture, "receipt_eligible", None) is not False:
            raise CandidateEligibilityError(
                "candidate capture cannot be receipt-eligible"
            )
        return candidate

    def validate(self, *, now: datetime | None = None) -> None:
        if self.provider_identity not in CANDIDATE_PROVIDER_IDENTITIES:
            raise CandidateEligibilityError(
                "provider is not explicitly candidate-eligible"
            )
        schema_version = self.schema_version or (
            "top5-candidate-provider-eligibility-v2"
            if self.provider_identity in THERUNDOWN_RPS_PROVIDER_IDENTITIES
            else "top5-candidate-provider-eligibility-v1"
        )
        if self.provider_identity in THERUNDOWN_RPS_PROVIDER_IDENTITIES:
            if schema_version not in {
                "top5-candidate-provider-eligibility-v1",
                "top5-candidate-provider-eligibility-v2",
            }:
                raise CandidateEligibilityError(
                    "TheRundown candidate eligibility schema is unsupported"
                )
        elif schema_version != "top5-candidate-provider-eligibility-v1":
            raise CandidateEligibilityError(
                "candidate eligibility schema is unsupported"
            )
        for name, value, expected in (
            ("candidate_capability", self.candidate_capability, True),
            ("shadow_capability", self.shadow_capability, True),
            ("qualification_capability", self.qualification_capability, True),
            ("active_production_capability", self.active_production_capability, False),
            ("publication_capability", self.publication_capability, False),
            ("scheduler_capability", self.scheduler_capability, False),
            ("betting_capability", self.betting_capability, False),
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise CandidateEligibilityError(f"unsafe candidate flag: {name}")
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("authorization_id", self.authorization_id),
            ("ceo_authorization_identity", self.ceo_authorization_identity),
            ("adapter_version", self.adapter_version),
            ("configuration_digest", self.configuration_digest),
            ("provider_event_id", self.provider_event_id),
            ("request_identity", self.request_identity),
            ("home_participant_id", self.home_participant_id),
            ("away_participant_id", self.away_participant_id),
            ("bookmaker_identity", self.bookmaker_identity),
            ("source_provenance", self.source_provenance),
            ("account_tier", self.account_tier),
        ):
            _text(value, name)
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _sha(self.configuration_digest, "configuration_digest")
        if self.league_code not in TOP5_LEAGUE_CODES:
            raise CandidateEligibilityError("unsupported canonical Top-5 league")
        _text(self.fixture_key, "fixture_key")
        kickoff = _utc(self.kickoff, "kickoff")
        authorization_expires_at = _utc(
            self.authorization_expires_at, "authorization_expires_at"
        )
        if self.fixture_key != make_fixture_key(
            self.league_code, self.home_team, self.away_team, kickoff
        ):
            raise CandidateEligibilityError("fixture identity binding mismatch")
        if self.home_participant_id == self.away_participant_id:
            raise CandidateEligibilityError("participant binding is not distinct")
        if self.market_type != MARKET_PREMATCH_1X2:
            raise CandidateEligibilityError("candidate market is not pre-match 1X2")
        if self.market_phase != PREMATCH_MARKET_PHASE:
            raise CandidateEligibilityError(
                "in-play/post-kickoff evidence is forbidden"
            )
        _odds(self.home_odds, "home_odds")
        _odds(self.draw_odds, "draw_odds")
        _odds(self.away_odds, "away_odds")
        source = _utc(self.source_timestamp, "source_timestamp")
        captured = _utc(self.captured_at, "captured_at")
        started = _utc(self.request_started_at, "request_started_at")
        finished = _utc(self.request_finished_at, "request_finished_at")
        if source > captured or finished < started or finished > captured:
            raise CandidateEligibilityError("candidate timestamps are not ordered")
        if kickoff <= captured:
            raise CandidateEligibilityError("post-kickoff evidence is forbidden")
        if captured >= authorization_expires_at:
            raise CandidateEligibilityError("candidate authorization is expired")
        age = (captured - source).total_seconds()
        if age < 0 or age > self.maximum_source_age_seconds:
            raise CandidateEligibilityError("candidate observation is stale")
        if _enum_value(self.provider_timestamp_provenance) in {
            CAPTURE_TIME_ONLY,
            UNKNOWN_TIMESTAMP,
        }:
            raise CandidateEligibilityError(
                "source timestamp provenance is insufficient"
            )
        for name, value in (
            ("raw_response_digest", self.raw_response_digest),
            ("provider_record_digest", self.provider_record_digest),
            ("normalized_record_digest", self.normalized_record_digest),
            ("cascade_evidence_digest", self.cascade_evidence_digest),
        ):
            _sha(value, name)
        _optional_nonnegative_int(self.quota_before, "quota_before")
        _optional_nonnegative_int(self.quota_after, "quota_after")
        if (
            self.quota_before is not None
            and self.quota_after is not None
            and self.quota_after > self.quota_before
        ):
            raise CandidateEligibilityError("quota-after exceeds quota-before")
        if not isfinite(float(self.quota_cost_units)) or self.quota_cost_units < 0:
            raise CandidateEligibilityError("quota cost evidence is invalid")
        if self.provider_identity in THERUNDOWN_RPS_PROVIDER_IDENTITIES:
            if (
                self.rate_limit_remaining is not None
                or self.rate_limit_reset_at is not None
            ):
                raise CandidateEligibilityError(
                    "TheRundown exposes an RPS ceiling, not remaining/reset state"
                )
            if self.rate_limit_limit is None:
                raise CandidateEligibilityError("rate-limit evidence is missing")
            _optional_nonnegative_int(self.rate_limit_limit, "rate_limit_limit")
            if self.rate_limit_limit == 0:
                raise CandidateEligibilityError("rate_limit_limit must be positive")
        else:
            _optional_nonnegative_int(self.rate_limit_remaining, "rate_limit_remaining")
            if self.rate_limit_reset_at is None:
                if self.rate_limit_limit is None:
                    raise CandidateEligibilityError("rate-limit evidence is missing")
                _optional_nonnegative_int(self.rate_limit_limit, "rate_limit_limit")
                if self.rate_limit_limit == 0:
                    raise CandidateEligibilityError("rate_limit_limit must be positive")
            else:
                _utc(self.rate_limit_reset_at, "rate_limit_reset_at")
                if self.rate_limit_remaining is None:
                    raise CandidateEligibilityError(
                        "rate_limit_reset_at requires rate_limit_remaining"
                    )
            if (
                self.rate_limit_limit is not None
                and self.rate_limit_remaining is not None
                and self.rate_limit_remaining > self.rate_limit_limit
            ):
                raise CandidateEligibilityError("rate-limit counters do not reconcile")
        if (
            not isfinite(float(self.provider_delay_seconds))
            or self.provider_delay_seconds < 0
        ):
            raise CandidateEligibilityError("provider delay evidence is invalid")
        try:
            evidence_kind = _enum_value(self.evidence_kind)
        except (TypeError, ValueError) as exc:
            raise CandidateEligibilityError(
                "candidate evidence kind is invalid"
            ) from exc
        if evidence_kind != REAL_OBSERVED:
            raise CandidateEligibilityError(
                "candidate qualification requires real evidence"
            )
        if self.network_execution is not True:
            raise CandidateEligibilityError(
                "candidate real evidence requires network execution"
            )
        if now is not None and captured > _utc(now, "eligibility now"):
            raise CandidateEligibilityError("capture timestamp is in the future")

    @classmethod
    def from_payload(cls, payload: object) -> CandidateProviderEligibilityV1:
        """Parse a serialized eligibility binding without relaxing validation."""

        if not isinstance(payload, Mapping):
            raise CandidateEligibilityError("candidate eligibility must be a mapping")

        def required(name: str) -> object:
            if name not in payload:
                raise CandidateEligibilityError(f"candidate eligibility missing {name}")
            return payload[name]

        def timestamp(name: str) -> datetime:
            value = required(name)
            if isinstance(value, datetime):
                return value
            if not isinstance(value, str):
                raise CandidateEligibilityError(f"{name} must be an ISO timestamp")
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise CandidateEligibilityError(
                    f"{name} must be an ISO timestamp"
                ) from exc

        provider_identity = required("provider_identity")
        uses_therundown_rps_contract = (
            provider_identity in THERUNDOWN_RPS_PROVIDER_IDENTITIES
        )
        return cls(
            provider_identity=provider_identity,
            candidate_capability=required("candidate_capability"),
            shadow_capability=required("shadow_capability"),
            qualification_capability=required("qualification_capability"),
            active_production_capability=required("active_production_capability"),
            publication_capability=required("publication_capability"),
            scheduler_capability=required("scheduler_capability"),
            betting_capability=required("betting_capability"),
            controlled_shadow_run_id=required("controlled_shadow_run_id"),
            qualification_session_id=required("qualification_session_id"),
            authorization_id=required("authorization_id"),
            ceo_authorization_identity=required("ceo_authorization_identity"),
            authorization_expires_at=timestamp("authorization_expires_at"),
            adapter_version=required("adapter_version"),
            adapter_source_sha=required("adapter_source_sha"),
            configuration_digest=required("configuration_digest"),
            league_code=required("league_code"),
            fixture_key=required("fixture_key"),
            provider_event_id=required("provider_event_id"),
            home_participant_id=required("home_participant_id"),
            away_participant_id=required("away_participant_id"),
            request_identity=required("request_identity"),
            home_team=required("home_team"),
            away_team=required("away_team"),
            kickoff=timestamp("kickoff"),
            bookmaker_identity=required("bookmaker_identity"),
            market_type=required("market_type"),
            market_phase=required("market_phase"),
            home_odds=required("home_odds"),
            draw_odds=required("draw_odds"),
            away_odds=required("away_odds"),
            source_timestamp=timestamp("source_timestamp"),
            captured_at=timestamp("captured_at"),
            request_started_at=timestamp("request_started_at"),
            request_finished_at=timestamp("request_finished_at"),
            maximum_source_age_seconds=required("maximum_source_age_seconds"),
            source_provenance=required("source_provenance"),
            provider_timestamp_provenance=required("provider_timestamp_provenance"),
            raw_response_digest=required("raw_response_digest"),
            provider_record_digest=required("provider_record_digest"),
            normalized_record_digest=required("normalized_record_digest"),
            cascade_evidence_digest=required("cascade_evidence_digest"),
            quota_before=payload.get("quota_before"),
            quota_after=payload.get("quota_after"),
            quota_cost_units=required("quota_cost_units"),
            rate_limit_remaining=(
                None
                if uses_therundown_rps_contract
                else payload.get("rate_limit_remaining")
            ),
            rate_limit_reset_at=(
                timestamp("rate_limit_reset_at")
                if not uses_therundown_rps_contract
                and payload.get("rate_limit_reset_at") is not None
                else None
            ),
            account_tier=required("account_tier"),
            provider_delay_seconds=required("provider_delay_seconds"),
            evidence_kind=payload.get("evidence_kind", REAL_OBSERVED),
            network_execution=payload.get("network_execution", True),
            no_bet=payload.get("no_bet", True),
            publication=payload.get("publication", False),
            production_activation=payload.get("production_activation", False),
            monetary_spend_authorized=payload.get("monetary_spend_authorized", False),
            rate_limit_limit=payload.get("rate_limit_limit"),
            schema_version=payload.get(
                "schema_version", "top5-candidate-provider-eligibility-v1"
            ),
        )

    def matches_observation(self, observation: object) -> None:
        """Bind a canonical qualification observation to this exact eligibility."""

        self.validate(now=getattr(observation, "captured_at", None))
        for name in (
            "provider_identity",
            "league",
            "fixture_key",
            "provider_event_id",
            "provider_request_id",
            "home_team",
            "away_team",
            "bookmaker_identity",
            "adapter_version",
            "adapter_source_sha",
            "raw_response_digest",
            "normalized_record_digest",
            "source_timestamp",
            "captured_at",
            "request_started_at",
            "request_finished_at",
            "home_odds",
            "draw_odds",
            "away_odds",
            "no_bet",
            "publication_enabled",
            "production_activation",
            "monetary_spend_authorized",
        ):
            expected_name = {
                "league": "league_code",
                "provider_request_id": "request_identity",
                "publication_enabled": "publication",
            }.get(name, name)
            expected = getattr(self, expected_name)
            actual = getattr(observation, name)
            if actual != expected:
                raise CandidateEligibilityError(
                    f"candidate eligibility binding mismatch: {name}"
                )
        if observation.qualification_session_id != self.qualification_session_id:
            raise CandidateEligibilityError(
                "candidate eligibility binding mismatch: qualification_session_id"
            )
        attestation = getattr(observation, "capture_attestation", None)
        if attestation is None:
            raise CandidateEligibilityError(
                "candidate observation attestation is missing"
            )
        for name, expected in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("ceo_authorization_id", self.authorization_id),
            ("qualification_session_id", self.qualification_session_id),
        ):
            if getattr(attestation, name, None) != expected:
                raise CandidateEligibilityError(
                    f"candidate eligibility binding mismatch: {name}"
                )
        if _enum_value(observation.evidence_kind) != REAL_OBSERVED:
            raise CandidateEligibilityError(
                "candidate observation is not REAL_OBSERVED"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = dict(self.__dict__)
        uses_therundown_rps_contract = (
            self.provider_identity in THERUNDOWN_RPS_PROVIDER_IDENTITIES
        )
        schema_version = self.schema_version or (
            "top5-candidate-provider-eligibility-v2"
            if uses_therundown_rps_contract
            else "top5-candidate-provider-eligibility-v1"
        )
        if uses_therundown_rps_contract and schema_version.endswith("-v2"):
            payload.pop("rate_limit_remaining")
            payload.pop("rate_limit_reset_at")
        for name in (
            "authorization_expires_at",
            "kickoff",
            "source_timestamp",
            "captured_at",
            "request_started_at",
            "request_finished_at",
        ):
            payload[name] = _utc(payload[name], name).isoformat()
        if payload.get("rate_limit_reset_at") is not None:
            payload["rate_limit_reset_at"] = _utc(
                payload["rate_limit_reset_at"], "rate_limit_reset_at"
            ).isoformat()
        payload["provider_timestamp_provenance"] = _enum_value(
            self.provider_timestamp_provenance
        )
        payload["evidence_kind"] = _enum_value(self.evidence_kind)
        payload["schema_version"] = schema_version
        return payload


__all__ = [
    "CANDIDATE_PROVIDER_IDENTITIES",
    "CANDIDATE_PROVIDER_REPERTOIRE",
    "CandidateEligibilityError",
    "CandidateProviderEligibilityV1",
]
