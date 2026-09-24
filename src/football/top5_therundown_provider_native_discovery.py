"""Fail-closed provider-native Top-5 Discovery for TheRundown.

This module is an explicitly CEO-waived alternative to the existing
independent fixture-manifest Discovery contract.  It searches only the
provider's dated event snapshots, freezes no authority, and emits no partial
run.  TheRundown remains candidate-only throughout.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from src.football.odds.therundown import (
    THERUNDOWN_BASE_URL,
    THERUNDOWN_MONEYLINE_MARKET_ID,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
    TheRundownDiscoveryEventCandidateV1,
    TheRundownExperimentalAdapter,
    _integer_field,
)
from src.football.production_contracts import Fixture
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_event_discovery import (
    B4_QUOTA_PROOF_SPORT_ID,
    DISCOVERY_LEAGUE_ORDER,
    EventDiscoveryContractError,
    EventDiscoveryExecutionBlocked,
    TheRundownB4QuotaProofV1,
    TheRundownDiscoveryAuthorizationConsumptionStore,
    TheRundownEventDiscoveryResponseV1,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownNetworkHttpRequestV1,
    TheRundownNetworkHttpResponseV1,
    TheRundownRequestsHttpClientV1,
)
from src.runtime.paths import runtime_state_path

PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION = (
    "top5-therundown-provider-native-discovery-v1"
)
PROVIDER_NATIVE_DISCOVERY_AUTHORIZATION_SCHEMA_VERSION = (
    "top5-therundown-provider-native-discovery-authorization-v1"
)
PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE = "therundown_provider_native"
PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION = "WAIVED"
PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE = 7
PROVIDER_NATIVE_MAX_REQUEST_COUNT = 35
PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST = 55
PROVIDER_NATIVE_MAX_DATAPOINTS = 1925
PROVIDER_NATIVE_MINIMUM_HEADROOM = 3850
PROVIDER_NATIVE_MINIMUM_INTERVAL_SECONDS = 1.1
PROVIDER_NATIVE_MAXIMUM_RETRIES = 0
PROVIDER_NATIVE_SPORT_ID = B4_QUOTA_PROOF_SPORT_ID
PROVIDER_NATIVE_BILLING_PROVIDER_HEADER = "provider_x_datapoints"
PROVIDER_NATIVE_BILLING_CUMULATIVE_DELTA = "cumulative_quota_delta"
TOP5_REAL_PROVIDER_EXECUTION_RESOURCE = "TOP5_REAL_PROVIDER_EXECUTION"
TOP5_REAL_PROVIDER_EXECUTION_LOCK_RELATIVE_PATH = (
    "football/top5/TOP5_REAL_PROVIDER_EXECUTION.lock"
)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EventDiscoveryContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventDiscoveryContractError(f"{name} must be non-empty")
    return value.strip()


def _sha(value: object, name: str, *, length: int = 64) -> str:
    result = _text(value, name).lower()
    if len(result) != length or any(char not in "0123456789abcdef" for char in result):
        raise EventDiscoveryContractError(f"{name} must be a SHA-256 digest")
    return result


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class _QuotaCounterBaseline:
    used: int
    remaining: int
    limit: int
    period: str
    reset_at: datetime
    tier: str


def top5_real_provider_execution_lock_path() -> Path:
    return runtime_state_path(
        TOP5_REAL_PROVIDER_EXECUTION_LOCK_RELATIVE_PATH,
        require_external=True,
    )


@contextmanager
def _exclusive_provider_execution_lock():
    path = top5_real_provider_execution_lock_path()
    if path.is_symlink():
        raise EventDiscoveryExecutionBlocked(
            "TOP5_REAL_PROVIDER_EXECUTION lock must not be a symlink"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with path.open("a+") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _header_int(headers: Mapping[str, str], name: str) -> int:
    raw = headers.get(name)
    if raw is None:
        raise EventDiscoveryExecutionBlocked(
            f"native discovery billing header missing: {name}"
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise EventDiscoveryExecutionBlocked(
            f"native discovery billing header invalid: {name}"
        ) from exc
    if value < 0:
        raise EventDiscoveryExecutionBlocked(
            f"native discovery billing header negative: {name}"
        )
    return value


def _header_reset(headers: Mapping[str, str]) -> datetime:
    raw = headers.get("x-datapoints-reset")
    if not raw:
        raise EventDiscoveryExecutionBlocked(
            "native discovery billing header missing: x-datapoints-reset"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventDiscoveryExecutionBlocked(
            "native discovery billing reset header is invalid"
        ) from exc
    return _utc(parsed, "native discovery billing reset")


def _proof_quota_baseline(proof: TheRundownB4QuotaProofV1) -> _QuotaCounterBaseline:
    if any(
        value is None
        for value in (
            proof.quota_used_datapoints,
            proof.quota_limit_datapoints,
            proof.quota_period,
            proof.quota_tier,
        )
    ):
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota-proof cumulative baseline is incomplete"
        )
    if not isinstance(proof.quota_period, str) or not isinstance(proof.quota_tier, str):
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota-proof baseline metadata is invalid"
        )
    baseline = _QuotaCounterBaseline(
        used=proof.quota_used_datapoints,
        remaining=proof.remaining_datapoints,
        limit=proof.quota_limit_datapoints,
        period=proof.quota_period,
        reset_at=_utc(proof.quota_reset_at, "quota-proof reset"),
        tier=proof.quota_tier.casefold(),
    )
    if baseline.used + baseline.remaining != baseline.limit:
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota-proof baseline does not reconcile"
        )
    if baseline.limit <= 0 or baseline.tier != "free":
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota-proof baseline is not approved"
        )
    return baseline


def _billing_for_response(
    response: TheRundownNetworkHttpResponseV1,
    previous: _QuotaCounterBaseline,
) -> tuple[int, int, str, _QuotaCounterBaseline]:
    headers = {
        str(key).casefold(): str(value).strip()
        for key, value in response.headers.items()
    }
    used = _header_int(headers, "x-datapoints-used")
    remaining = _header_int(headers, "x-datapoints-remaining")
    limit = _header_int(headers, "x-datapoints-limit")
    period = headers.get("x-datapoints-period", "")
    tier = headers.get("x-tier", "").casefold()
    reset_at = _header_reset(headers)
    if not period or tier != previous.tier or tier != "free":
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota period or tier changed"
        )
    if (
        limit != previous.limit
        or period != previous.period
        or reset_at != previous.reset_at
    ):
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota boundary changed during run"
        )
    if used < previous.used or remaining > previous.remaining:
        raise EventDiscoveryExecutionBlocked(
            "native discovery cumulative quota counters moved backwards"
        )
    if used + remaining != limit:
        raise EventDiscoveryExecutionBlocked(
            "native discovery cumulative quota counters do not reconcile"
        )
    if response.finished_at >= reset_at:
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota reset boundary occurred during run"
        )
    delta_used = used - previous.used
    delta_remaining = previous.remaining - remaining
    if delta_used < 0 or delta_remaining < 0 or delta_used != delta_remaining:
        raise EventDiscoveryExecutionBlocked(
            "native discovery quota deltas do not reconcile"
        )
    if delta_used > PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST:
        raise EventDiscoveryExecutionBlocked(
            "native discovery inferred request cost exceeds 55"
        )
    exact_raw = headers.get("x-datapoints")
    if exact_raw is None:
        mode = PROVIDER_NATIVE_BILLING_CUMULATIVE_DELTA
        datapoints = delta_used
    else:
        try:
            datapoints = int(exact_raw)
        except ValueError as exc:
            raise EventDiscoveryExecutionBlocked(
                "native discovery x-datapoints header is invalid"
            ) from exc
        if datapoints < 0 or datapoints > PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST:
            raise EventDiscoveryExecutionBlocked(
                "native discovery x-datapoints exceeds 55"
            )
        if datapoints != delta_used:
            raise EventDiscoveryExecutionBlocked(
                "native discovery x-datapoints disagrees with cumulative delta"
            )
        mode = PROVIDER_NATIVE_BILLING_PROVIDER_HEADER
    return (
        datapoints,
        remaining,
        mode,
        _QuotaCounterBaseline(
            used=used,
            remaining=remaining,
            limit=limit,
            period=period,
            reset_at=reset_at,
            tier=tier,
        ),
    )


def _validated_datapoint_total(current: int, datapoints: int, *, maximum: int) -> int:
    if datapoints < 0:
        raise EventDiscoveryExecutionBlocked(
            "native discovery datapoints cannot be negative"
        )
    if datapoints > PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST:
        raise EventDiscoveryExecutionBlocked(
            "native discovery request datapoint cap exceeded"
        )
    total = current + datapoints
    if total > maximum:
        raise EventDiscoveryExecutionBlocked(
            "native discovery cumulative datapoint budget exceeded"
        )
    return total


def _participant_sort_key(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value).casefold()
        if not unicodedata.combining(char) and char.isalnum()
    )


def _request_shape_payload(search_start_date: date) -> list[dict[str, object]]:
    return [
        {
            "method": "GET",
            "endpoint": (
                f"{THERUNDOWN_BASE_URL}/sports/{THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[league]}/events/"
                f"{(search_start_date + timedelta(days=offset)).isoformat()}"
            ),
            "query": {
                "market_ids": str(THERUNDOWN_MONEYLINE_MARKET_ID),
                "main_line": "true",
                "hide_closed": "true",
                "hide_no_markets": "true",
            },
            "league": league,
            "date_offset": offset,
        }
        for league in DISCOVERY_LEAGUE_ORDER
        for offset in range(PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE)
    ]


def provider_native_discovery_request_shape_digest(search_start_date: date) -> str:
    if not isinstance(search_start_date, date) or isinstance(
        search_start_date, datetime
    ):
        raise EventDiscoveryContractError("native discovery start date is invalid")
    return _digest(_request_shape_payload(search_start_date))


@dataclass(frozen=True)
class TheRundownProviderNativeDiscoveryAuthorizationV1:
    discovery_authorization_id: str
    ceo_discovery_authorization_identity: str
    provider: str
    search_start_date: date
    adapter_version: str
    adapter_source_sha: str
    request_shape_digest: str
    quota_proof_id: str
    quota_proof_authorization_id: str
    quota_proof_evidence_digest: str
    quota_proof_response_digest: str
    quota_proof_account_scope: str
    quota_proof_remaining_datapoints: int
    quota_proof_sport_id: int
    quota_proof_snapshot_date: date
    quota_proof_observed_at: datetime
    quota_proof_finished_at: datetime
    quota_proof_reset_at: datetime
    issued_at: datetime
    expires_at: datetime
    maximum_dates_per_league: int = PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE
    maximum_request_count: int = PROVIDER_NATIVE_MAX_REQUEST_COUNT
    maximum_datapoints_per_request: int = PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST
    maximum_datapoints: int = PROVIDER_NATIVE_MAX_DATAPOINTS
    minimum_interval_seconds: float = PROVIDER_NATIVE_MINIMUM_INTERVAL_SECONDS
    maximum_retries: int = PROVIDER_NATIVE_MAXIMUM_RETRIES
    discovery_target_source: str = PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE
    independent_fixture_source_qualification: str = (
        PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
    )
    no_bet: bool = True
    candidate_qualification: bool = False
    production_authority: bool = False
    activation: bool = False
    publication: bool = False
    ledger_mutation: bool = False
    monetary_spend_authorized: bool = False

    @property
    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": PROVIDER_NATIVE_DISCOVERY_AUTHORIZATION_SCHEMA_VERSION,
            "discovery_authorization_id": self.discovery_authorization_id,
            "ceo_discovery_authorization_identity": self.ceo_discovery_authorization_identity,
            "provider": self.provider,
            "league_order": list(DISCOVERY_LEAGUE_ORDER),
            "search_start_date": self.search_start_date.isoformat(),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "request_shape_digest": self.request_shape_digest,
            "quota_proof_id": self.quota_proof_id,
            "quota_proof_authorization_id": self.quota_proof_authorization_id,
            "quota_proof_evidence_digest": self.quota_proof_evidence_digest,
            "quota_proof_response_digest": self.quota_proof_response_digest,
            "quota_proof_account_scope": self.quota_proof_account_scope,
            "quota_proof_remaining_datapoints": self.quota_proof_remaining_datapoints,
            "quota_proof_sport_id": self.quota_proof_sport_id,
            "quota_proof_snapshot_date": self.quota_proof_snapshot_date.isoformat(),
            "quota_proof_observed_at": _utc(
                self.quota_proof_observed_at, "proof observed"
            ).isoformat(),
            "quota_proof_finished_at": _utc(
                self.quota_proof_finished_at, "proof finished"
            ).isoformat(),
            "quota_proof_reset_at": _utc(
                self.quota_proof_reset_at, "proof reset"
            ).isoformat(),
            "issued_at": _utc(self.issued_at, "issued_at").isoformat(),
            "expires_at": _utc(self.expires_at, "expires_at").isoformat(),
            "maximum_dates_per_league": self.maximum_dates_per_league,
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoints_per_request": self.maximum_datapoints_per_request,
            "maximum_datapoints": self.maximum_datapoints,
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "maximum_retries": self.maximum_retries,
            "discovery_target_source": self.discovery_target_source,
            "independent_fixture_source_qualification": self.independent_fixture_source_qualification,
            "no_bet": self.no_bet,
            "candidate_qualification": self.candidate_qualification,
            "production_authority": self.production_authority,
            "activation": self.activation,
            "publication": self.publication,
            "ledger_mutation": self.ledger_mutation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    @property
    def authorization_digest(self) -> str:
        return _digest(self._payload_without_digest)

    def validate(self, *, now: datetime | None = None) -> None:
        _text(self.discovery_authorization_id, "discovery_authorization_id")
        _text(
            self.ceo_discovery_authorization_identity,
            "ceo_discovery_authorization_identity",
        )
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError("native discovery provider is invalid")
        if tuple(DISCOVERY_LEAGUE_ORDER) != ("EPL", "BL1", "LL", "SA", "L1"):
            raise EventDiscoveryContractError(
                "native discovery league order is invalid"
            )
        if not isinstance(self.search_start_date, date) or isinstance(
            self.search_start_date, datetime
        ):
            raise EventDiscoveryContractError("native discovery date is invalid")
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha", length=40)
        _sha(self.request_shape_digest, "request_shape_digest")
        for name, value in (
            ("quota_proof_id", self.quota_proof_id),
            ("quota_proof_authorization_id", self.quota_proof_authorization_id),
            ("quota_proof_account_scope", self.quota_proof_account_scope),
        ):
            _text(value, name)
        _sha(self.quota_proof_evidence_digest, "quota_proof_evidence_digest")
        _sha(self.quota_proof_response_digest, "quota_proof_response_digest")
        if self.quota_proof_sport_id != PROVIDER_NATIVE_SPORT_ID:
            raise EventDiscoveryContractError("native proof sport binding is invalid")
        if self.quota_proof_remaining_datapoints < PROVIDER_NATIVE_MINIMUM_HEADROOM:
            raise EventDiscoveryExecutionBlocked(
                "native discovery requires 3850 datapoints of proof headroom"
            )
        if self.maximum_dates_per_league != PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE:
            raise EventDiscoveryExecutionBlocked(
                "native discovery date bound is invalid"
            )
        if self.maximum_request_count != PROVIDER_NATIVE_MAX_REQUEST_COUNT:
            raise EventDiscoveryExecutionBlocked(
                "native discovery request bound is invalid"
            )
        if (
            self.maximum_datapoints_per_request
            != PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST
        ):
            raise EventDiscoveryExecutionBlocked(
                "native discovery request cost is invalid"
            )
        if self.maximum_datapoints != PROVIDER_NATIVE_MAX_DATAPOINTS:
            raise EventDiscoveryExecutionBlocked(
                "native discovery total cost is invalid"
            )
        if self.maximum_retries != 0:
            raise EventDiscoveryExecutionBlocked(
                "native discovery retries are forbidden"
            )
        if self.minimum_interval_seconds < PROVIDER_NATIVE_MINIMUM_INTERVAL_SECONDS:
            raise EventDiscoveryExecutionBlocked("native discovery pacing is too fast")
        if self.discovery_target_source != PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE:
            raise EventDiscoveryExecutionBlocked(
                "native discovery source is not explicit"
            )
        if (
            self.independent_fixture_source_qualification
            != PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
        ):
            raise EventDiscoveryExecutionBlocked("native discovery waiver is required")
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("candidate_qualification", self.candidate_qualification, False),
            ("production_authority", self.production_authority, False),
            ("activation", self.activation, False),
            ("publication", self.publication, False),
            ("ledger_mutation", self.ledger_mutation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise EventDiscoveryExecutionBlocked(
                    f"unsafe native discovery flag: {name}"
                )
        if self.request_shape_digest != provider_native_discovery_request_shape_digest(
            self.search_start_date
        ):
            raise EventDiscoveryContractError(
                "native discovery request-shape digest mismatch"
            )
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise EventDiscoveryContractError("native discovery expiry is invalid")
        current = _utc(now or datetime.now(timezone.utc), "native discovery now")
        if current < issued or current >= expires:
            raise EventDiscoveryExecutionBlocked(
                "native discovery authorization is expired"
            )
        if self.search_start_date != current.date():
            raise EventDiscoveryExecutionBlocked(
                "native discovery must start on the current UTC date"
            )

    def validate_against_quota_proof(
        self, proof: TheRundownB4QuotaProofV1, *, now: datetime
    ) -> None:
        self.validate(now=now)
        proof.validate(now=now)
        bindings = (
            (self.quota_proof_id, proof.proof_id, "proof_id"),
            (
                self.quota_proof_authorization_id,
                proof.authorization_id,
                "authorization_id",
            ),
            (
                self.quota_proof_evidence_digest.lower(),
                proof.evidence_digest.lower(),
                "evidence_digest",
            ),
            (
                self.quota_proof_response_digest.lower(),
                proof.response_digest.lower(),
                "response_digest",
            ),
            (self.quota_proof_account_scope, proof.account_scope, "account_scope"),
            (
                self.quota_proof_remaining_datapoints,
                proof.remaining_datapoints,
                "remaining_datapoints",
            ),
            (self.quota_proof_sport_id, proof.sport_id, "sport_id"),
            (self.quota_proof_snapshot_date, proof.snapshot_date, "snapshot_date"),
            (
                _utc(self.quota_proof_observed_at, "proof observed"),
                _utc(proof.response_started_at, "proof started"),
                "observed_at",
            ),
            (
                _utc(self.quota_proof_finished_at, "proof finished"),
                _utc(proof.response_finished_at, "proof finished"),
                "finished_at",
            ),
            (
                _utc(self.quota_proof_reset_at, "proof reset"),
                _utc(proof.quota_reset_at, "proof reset"),
                "reset_at",
            ),
        )
        for actual, expected, name in bindings:
            if actual != expected:
                raise EventDiscoveryExecutionBlocked(
                    f"native quota-proof binding mismatch: {name}"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        return {
            **self._payload_without_digest,
            "authorization_digest": self.authorization_digest,
        }


@dataclass(frozen=True)
class TheRundownProviderNativeDiscoveryRequestV1:
    authorization: TheRundownProviderNativeDiscoveryAuthorizationV1
    league: str
    snapshot_date: date
    sequence: int
    date_offset: int
    league_index: int

    @property
    def request_identity(self) -> str:
        return _digest(
            {
                "authorization": self.authorization.discovery_authorization_id,
                "league": self.league,
                "snapshot_date": self.snapshot_date.isoformat(),
                "sequence": self.sequence,
            }
        )

    @property
    def endpoint(self) -> str:
        sport_id = THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[self.league]
        return f"{THERUNDOWN_BASE_URL}/sports/{sport_id}/events/{self.snapshot_date.isoformat()}"

    @property
    def query(self) -> dict[str, str]:
        return {
            "market_ids": str(THERUNDOWN_MONEYLINE_MARKET_ID),
            "main_line": "true",
            "hide_closed": "true",
            "hide_no_markets": "true",
        }

    def validate(self, *, now: datetime | None = None) -> None:
        self.authorization.validate(now=now or self.authorization.issued_at)
        if self.league_index < 0 or self.league_index >= len(DISCOVERY_LEAGUE_ORDER):
            raise EventDiscoveryExecutionBlocked(
                "native discovery league index is invalid"
            )
        if self.league != DISCOVERY_LEAGUE_ORDER[self.league_index]:
            raise EventDiscoveryExecutionBlocked("native discovery order is invalid")
        if (
            self.date_offset < 0
            or self.date_offset >= PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE
        ):
            raise EventDiscoveryExecutionBlocked(
                "native discovery date offset is invalid"
            )
        if self.snapshot_date != self.authorization.search_start_date + timedelta(
            days=self.date_offset
        ):
            raise EventDiscoveryContractError(
                "native discovery date binding is invalid"
            )
        if self.sequence < 0 or self.sequence >= PROVIDER_NATIVE_MAX_REQUEST_COUNT:
            raise EventDiscoveryExecutionBlocked(
                "native discovery sequence exceeds bound"
            )

    def as_http_request(
        self, *, api_key: str, timeout_seconds: float = 30.0
    ) -> TheRundownNetworkHttpRequestV1:
        _text(api_key, "TheRundown credential")
        self.validate()
        return TheRundownNetworkHttpRequestV1(
            method="GET",
            endpoint=self.endpoint,
            query=self.query,
            headers={"X-TheRundown-Key": api_key},
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class TheRundownProviderNativeDiscoveryCaptureV1:
    discovery_authorization_id: str
    discovery_authorization_digest: str
    discovery_target_source: str
    independent_fixture_source_qualification: str
    provider: str
    league: str
    snapshot_date: date
    fixture_key: str
    home_team: str
    away_team: str
    home_participant_id: str
    away_participant_id: str
    kickoff: datetime
    provider_event_id: str
    request_identity: str
    request_shape_digest: str
    request_started_at: datetime
    response_completed_at: datetime
    raw_response_digest: str
    datapoints: int
    remaining_datapoints: int
    billing_evidence_mode: str = PROVIDER_NATIVE_BILLING_PROVIDER_HEADER
    retry_count: int = 0
    network_execution: bool = True
    qualification_eligible: bool = False
    receipt_eligible: bool = False
    provider_authority: bool = False
    activation_authorized: bool = False
    publication_authorized: bool = False
    ledger_mutated: bool = False
    monetary_spend_authorized: bool = False

    @property
    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION,
            "discovery_authorization_id": self.discovery_authorization_id,
            "discovery_authorization_digest": self.discovery_authorization_digest,
            "discovery_target_source": self.discovery_target_source,
            "independent_fixture_source_qualification": self.independent_fixture_source_qualification,
            "provider": self.provider,
            "league": self.league,
            "snapshot_date": self.snapshot_date.isoformat(),
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
            "kickoff": _utc(self.kickoff, "kickoff").isoformat(),
            "provider_event_id": self.provider_event_id,
            "request_identity": self.request_identity,
            "request_shape_digest": self.request_shape_digest,
            "request_started_at": _utc(
                self.request_started_at, "request started"
            ).isoformat(),
            "response_completed_at": _utc(
                self.response_completed_at, "response completed"
            ).isoformat(),
            "raw_response_digest": self.raw_response_digest,
            "datapoints": self.datapoints,
            "remaining_datapoints": self.remaining_datapoints,
            "billing_evidence_mode": self.billing_evidence_mode,
            "retry_count": self.retry_count,
            "network_execution": self.network_execution,
            "qualification_eligible": self.qualification_eligible,
            "receipt_eligible": self.receipt_eligible,
            "provider_authority": self.provider_authority,
            "activation_authorized": self.activation_authorized,
            "publication_authorized": self.publication_authorized,
            "ledger_mutated": self.ledger_mutated,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    @property
    def evidence_digest(self) -> str:
        return _digest(self._payload_without_digest)

    def validate(self) -> None:
        if self.discovery_target_source != PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE:
            raise EventDiscoveryContractError("native capture source is invalid")
        if (
            self.independent_fixture_source_qualification
            != PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
        ):
            raise EventDiscoveryContractError("native capture waiver is invalid")
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError("native capture provider is invalid")
        if self.league not in DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError("native capture league is invalid")
        for name, value in (
            ("provider_event_id", self.provider_event_id),
            ("request_identity", self.request_identity),
            ("home_participant_id", self.home_participant_id),
            ("away_participant_id", self.away_participant_id),
            ("raw_response_digest", self.raw_response_digest),
        ):
            _text(value, name)
        _sha(self.discovery_authorization_digest, "discovery authorization digest")
        _sha(self.request_shape_digest, "request shape digest")
        _sha(self.raw_response_digest, "raw response digest")
        fixture = Fixture(
            fixture_key=self.fixture_key,
            league_code=self.league,
            home_team=self.home_team,
            away_team=self.away_team,
            kickoff=self.kickoff,
        )
        fixture.validate()
        if fixture.fixture_key != make_fixture_key(
            self.league, self.home_team, self.away_team, self.kickoff
        ):
            raise EventDiscoveryContractError("native capture fixture key mismatch")
        if self.home_participant_id == self.away_participant_id:
            raise EventDiscoveryContractError("native participant IDs must be distinct")
        if (
            self.retry_count != 0
            or self.datapoints < 0
            or self.datapoints > PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST
        ):
            raise EventDiscoveryExecutionBlocked("native capture billing is invalid")
        if self.billing_evidence_mode not in {
            PROVIDER_NATIVE_BILLING_PROVIDER_HEADER,
            PROVIDER_NATIVE_BILLING_CUMULATIVE_DELTA,
        }:
            raise EventDiscoveryExecutionBlocked(
                "native capture billing evidence mode is invalid"
            )
        if self.network_execution is not True:
            raise EventDiscoveryExecutionBlocked(
                "native capture must be network evidence"
            )
        for name, value, expected in (
            ("qualification_eligible", self.qualification_eligible, False),
            ("receipt_eligible", self.receipt_eligible, False),
            ("provider_authority", self.provider_authority, False),
            ("activation_authorized", self.activation_authorized, False),
            ("publication_authorized", self.publication_authorized, False),
            ("ledger_mutated", self.ledger_mutated, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise EventDiscoveryExecutionBlocked(
                    f"unsafe native capture flag: {name}"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_digest, "evidence_digest": self.evidence_digest}


@dataclass(frozen=True)
class TheRundownProviderNativeDiscoveryRunResultV1:
    authorization: TheRundownProviderNativeDiscoveryAuthorizationV1
    captures: tuple[TheRundownProviderNativeDiscoveryCaptureV1, ...]
    request_count: int
    datapoint_total: int
    raw_response_digests: tuple[str, ...]

    def validate(self) -> None:
        self.authorization.validate(now=self.authorization.issued_at)
        if tuple(capture.league for capture in self.captures) != DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError(
                "native result does not contain canonical five leagues"
            )
        if (
            len(self.captures) != 5
            or len({capture.provider_event_id for capture in self.captures}) != 5
        ):
            raise EventDiscoveryContractError(
                "native result event identities are not unique"
            )
        participant_ids = {
            participant_id
            for capture in self.captures
            for participant_id in (
                capture.home_participant_id,
                capture.away_participant_id,
            )
        }
        if len(participant_ids) != 10:
            raise EventDiscoveryContractError(
                "native result participant identities are not unique"
            )
        if (
            self.request_count < 5
            or self.request_count > PROVIDER_NATIVE_MAX_REQUEST_COUNT
        ):
            raise EventDiscoveryExecutionBlocked(
                "native result request count is invalid"
            )
        if (
            self.datapoint_total < 0
            or self.datapoint_total > PROVIDER_NATIVE_MAX_DATAPOINTS
        ):
            raise EventDiscoveryExecutionBlocked(
                "native result datapoint total is invalid"
            )
        for capture in self.captures:
            capture.validate()

    @property
    def run_digest(self) -> str:
        self.validate()
        return _digest(
            {
                "schema_version": PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION,
                "authorization_digest": self.authorization.authorization_digest,
                "captures": [capture.as_payload() for capture in self.captures],
                "request_count": self.request_count,
                "datapoint_total": self.datapoint_total,
                "raw_response_digests": list(self.raw_response_digests),
            }
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION,
            "discovery_target_source": PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
            "independent_fixture_source_qualification": PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
            "authorization": self.authorization.as_payload(),
            "captures": [capture.as_payload() for capture in self.captures],
            "request_count": self.request_count,
            "datapoint_total": self.datapoint_total,
            "raw_response_digests": list(self.raw_response_digests),
            "run_digest": self.run_digest,
        }


class TheRundownProviderNativeDiscoveryTransport(Protocol):
    def execute(
        self, request: TheRundownProviderNativeDiscoveryRequestV1
    ) -> TheRundownNetworkHttpResponseV1: ...


def _select_candidate(
    adapter: TheRundownExperimentalAdapter,
    payload: object,
    *,
    league: str,
    now: datetime,
) -> TheRundownDiscoveryEventCandidateV1 | None:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
        raise EventDiscoveryExecutionBlocked(
            "native discovery payload has no events list"
        )
    candidates: list[TheRundownDiscoveryEventCandidateV1] = []
    seen_event_ids: set[str] = set()
    for raw_event in payload["events"]:
        if not isinstance(raw_event, Mapping):
            raise EventDiscoveryExecutionBlocked("native discovery event is malformed")
        event_id = str(raw_event.get("event_id", "")).strip()
        if not event_id or event_id in {"0", "None"}:
            raise EventDiscoveryExecutionBlocked("native discovery event ID is invalid")
        if event_id in seen_event_ids:
            raise EventDiscoveryExecutionBlocked(
                "native discovery contains duplicate event IDs"
            )
        seen_event_ids.add(event_id)
        if (
            _integer_field(raw_event.get("sport_id"))
            != THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[league]
        ):
            raise EventDiscoveryExecutionBlocked(
                "native discovery event league mismatch"
            )
        try:
            candidate = adapter.discovery_event_candidate(raw_event, league=league)
        except ValueError as exc:
            raise EventDiscoveryExecutionBlocked(
                "native discovery event failed canonical provider validation"
            ) from exc
        if candidate.kickoff <= _utc(now, "native discovery now"):
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(
        key=lambda candidate: (
            candidate.kickoff,
            _participant_sort_key(candidate.home_team),
            _participant_sort_key(candidate.away_team),
            candidate.provider_event_id,
        )
    )
    return candidates[0]


def _as_discovery_response(
    response: TheRundownNetworkHttpResponseV1,
) -> TheRundownEventDiscoveryResponseV1:
    if response.status_code is None:
        raise EventDiscoveryExecutionBlocked(
            "native discovery transport did not return HTTP status"
        )
    return TheRundownEventDiscoveryResponseV1(
        status_code=response.status_code,
        payload=response.payload,
        headers=response.headers,
        started_at=response.started_at,
        finished_at=response.finished_at,
        retry_count=0,
        network_execution=True,
    )


def _discover_five_league_events_provider_native_unlocked(
    authorization: TheRundownProviderNativeDiscoveryAuthorizationV1,
    *,
    proof: TheRundownB4QuotaProofV1,
    api_key: str | None = None,
    credential_loader: Callable[[], str] | None = None,
    transport: TheRundownProviderNativeDiscoveryTransport | None = None,
    adapter: TheRundownExperimentalAdapter | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    pacer: Callable[[float], None] | None = None,
) -> TheRundownProviderNativeDiscoveryRunResultV1:
    """Run bounded provider-native Discovery and return only a complete run."""

    current = _utc(now or datetime.now(timezone.utc), "native discovery now")
    authorization.validate_against_quota_proof(proof, now=current)
    previous_quota = _proof_quota_baseline(proof)
    if transport is None and credential_loader is None:
        raise EventDiscoveryExecutionBlocked(
            "native network discovery requires a post-consumption credential loader"
        )
    canonical_adapter = adapter or TheRundownExperimentalAdapter()
    sleep = pacer or time.sleep
    captures: list[TheRundownProviderNativeDiscoveryCaptureV1] = []
    raw_digests: list[str] = []
    total_datapoints = 0
    request_count = 0
    last_request_finished: datetime | None = None
    TheRundownDiscoveryAuthorizationConsumptionStore().consume(
        authorization, now=current
    )
    if credential_loader is not None:
        api_key = credential_loader()
    if transport is None:
        client = _RequestsNativeDiscoveryTransport(
            api_key=_text(api_key, "TheRundown credential")
        )
    else:
        client = transport
    for league_index, league in enumerate(DISCOVERY_LEAGUE_ORDER):
        found: TheRundownProviderNativeDiscoveryCaptureV1 | None = None
        for date_offset in range(PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE):
            if last_request_finished is not None:
                elapsed = (
                    _utc(current, "native discovery now") - last_request_finished
                ).total_seconds()
                if elapsed < authorization.minimum_interval_seconds:
                    sleep(authorization.minimum_interval_seconds - elapsed)
            request = TheRundownProviderNativeDiscoveryRequestV1(
                authorization=authorization,
                league=league,
                snapshot_date=authorization.search_start_date
                + timedelta(days=date_offset),
                sequence=request_count,
                date_offset=date_offset,
                league_index=league_index,
            )
            request.validate(now=authorization.issued_at)
            response = client.execute(request)
            request_count += 1
            if request_count > authorization.maximum_request_count:
                raise EventDiscoveryExecutionBlocked(
                    "native discovery request budget exceeded"
                )
            discovery_response = _as_discovery_response(response)
            discovery_response.validate()
            datapoints, remaining, billing_mode, previous_quota = _billing_for_response(
                response, previous_quota
            )
            if datapoints > authorization.maximum_datapoints_per_request:
                raise EventDiscoveryExecutionBlocked(
                    "native discovery request datapoint cap exceeded"
                )
            total_datapoints = _validated_datapoint_total(
                total_datapoints,
                datapoints,
                maximum=authorization.maximum_datapoints,
            )
            raw_digest = response.body_digest or _digest(response.payload)
            raw_digests.append(_sha(raw_digest, "native raw response digest"))
            candidate = _select_candidate(
                canonical_adapter,
                response.payload,
                league=league,
                now=_utc(response.finished_at, "native response finished"),
            )
            last_request_finished = _utc(
                response.finished_at, "native response finished"
            )
            if candidate is None:
                current = _utc(
                    clock() if clock is not None else datetime.now(timezone.utc),
                    "native discovery clock",
                )
                continue
            fixture_key = make_fixture_key(
                league, candidate.home_team, candidate.away_team, candidate.kickoff
            )
            capture = TheRundownProviderNativeDiscoveryCaptureV1(
                discovery_authorization_id=authorization.discovery_authorization_id,
                discovery_authorization_digest=authorization.authorization_digest,
                discovery_target_source=PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
                independent_fixture_source_qualification=PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
                provider=THERUNDOWN_PROVIDER_NAME,
                league=league,
                snapshot_date=request.snapshot_date,
                fixture_key=fixture_key,
                home_team=candidate.home_team,
                away_team=candidate.away_team,
                home_participant_id=candidate.home_participant_id,
                away_participant_id=candidate.away_participant_id,
                kickoff=candidate.kickoff,
                provider_event_id=candidate.provider_event_id,
                request_identity=request.request_identity,
                request_shape_digest=provider_native_discovery_request_shape_digest(
                    authorization.search_start_date
                ),
                request_started_at=response.started_at,
                response_completed_at=response.finished_at,
                raw_response_digest=raw_digests[-1],
                datapoints=datapoints,
                remaining_datapoints=remaining,
                billing_evidence_mode=billing_mode,
            )
            capture.validate()
            found = capture
            break
        if found is None:
            raise EventDiscoveryExecutionBlocked(
                f"native discovery found no valid future event for {league}"
            )
        captures.append(found)
    result = TheRundownProviderNativeDiscoveryRunResultV1(
        authorization=authorization,
        captures=tuple(captures),
        request_count=request_count,
        datapoint_total=total_datapoints,
        raw_response_digests=tuple(raw_digests),
    )
    result.validate()
    return result


def discover_five_league_events_provider_native(
    authorization: TheRundownProviderNativeDiscoveryAuthorizationV1,
    *,
    proof: TheRundownB4QuotaProofV1,
    api_key: str | None = None,
    credential_loader: Callable[[], str] | None = None,
    transport: TheRundownProviderNativeDiscoveryTransport | None = None,
    adapter: TheRundownExperimentalAdapter | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    pacer: Callable[[float], None] | None = None,
) -> TheRundownProviderNativeDiscoveryRunResultV1:
    """Run native Discovery while holding the exclusive real-provider lock."""

    with _exclusive_provider_execution_lock():
        return _discover_five_league_events_provider_native_unlocked(
            authorization,
            proof=proof,
            api_key=api_key,
            credential_loader=credential_loader,
            transport=transport,
            adapter=adapter,
            now=now,
            clock=clock,
            pacer=pacer,
        )


class _RequestsNativeDiscoveryTransport:
    def __init__(self, *, api_key: str) -> None:
        self._api_key = _text(api_key, "TheRundown credential")
        self._client = TheRundownRequestsHttpClientV1()

    def execute(
        self, request: TheRundownProviderNativeDiscoveryRequestV1
    ) -> TheRundownNetworkHttpResponseV1:
        return self._client.execute(request.as_http_request(api_key=self._api_key))


__all__ = [
    "PROVIDER_NATIVE_BILLING_CUMULATIVE_DELTA",
    "PROVIDER_NATIVE_BILLING_PROVIDER_HEADER",
    "PROVIDER_NATIVE_DISCOVERY_AUTHORIZATION_SCHEMA_VERSION",
    "PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION",
    "PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE",
    "PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION",
    "PROVIDER_NATIVE_MAX_DATAPOINTS",
    "PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST",
    "PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE",
    "PROVIDER_NATIVE_MAX_REQUEST_COUNT",
    "PROVIDER_NATIVE_MINIMUM_HEADROOM",
    "PROVIDER_NATIVE_MINIMUM_INTERVAL_SECONDS",
    "TOP5_REAL_PROVIDER_EXECUTION_RESOURCE",
    "TheRundownProviderNativeDiscoveryAuthorizationV1",
    "TheRundownProviderNativeDiscoveryCaptureV1",
    "TheRundownProviderNativeDiscoveryRequestV1",
    "TheRundownProviderNativeDiscoveryRunResultV1",
    "discover_five_league_events_provider_native",
    "provider_native_discovery_request_shape_digest",
    "top5_real_provider_execution_lock_path",
]
