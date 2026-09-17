"""Experimental API-Football v3 Champions League odds adapter.

This module is deliberately outside the active SportsBrain provider cascade.
It reuses the canonical :class:`Fixture` and
:class:`NormalizedOddsObservation` contracts for evaluation, but it does not
register ``api_football`` in any active provider order, readiness view, signal
path, or authority decision.

The adapter has two explicit input paths:

* ``normalize_injected_*`` accepts deterministic fixtures for offline tests and
  marks every result ``INJECTED_FIXTURE``.
* ``fetch_real_odds`` and ``fetch_real_fixtures`` use the real HTTP transport
  and mark results ``REAL_NETWORK_CAPTURE`` only after a transport response is
  received and validated.

No method issues Builder-2 evidence, a qualification receipt, a prediction,
publication authority, betting authority, or production activation.  The
diagnostic caller owns a hard five-request budget and performs no retries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from math import isfinite

import requests

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
    digest_record,
)
from src.football.top5_shadow_provider_redundancy import normalize_team_name

API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_COMPETITION_ID = 2
API_FOOTBALL_COMPETITION_NAME = "UEFA Champions League"
API_FOOTBALL_MARKET_ID = 1
API_FOOTBALL_PROVIDER_IDENTITY = "api_football_experimental"
API_FOOTBALL_PROVIDER_FAMILY = "api_football"
API_FOOTBALL_ADAPTER_VERSION = "app-b4-api-football-v1"
API_FOOTBALL_PAGE_SIZE = 10


class ApiFootballExperimentalError(ProductionContractError):
    """Invalid experimental provider input or an unsafe normalization result."""


class ApiFootballFailure(str, Enum):
    NONE = "none"
    CREDENTIAL_MISSING = "credential_missing"
    AUTHENTICATION_ERROR = "authentication_error"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    MALFORMED_JSON = "malformed_json"
    MALFORMED_RESPONSE = "malformed_response"
    MISSING_BOOKMAKER = "missing_bookmaker"
    MISSING_MATCH_WINNER = "missing_match_winner"
    MISSING_FIXTURE = "missing_fixture"
    FIXTURE_MISMATCH = "fixture_mismatch"
    WRONG_COMPETITION = "wrong_competition"
    STALE_DATA = "stale_data"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    INVALID_DECIMAL_ODDS = "invalid_decimal_odds"
    SOURCE_TIMESTAMP_MISSING = "source_timestamp_missing"
    SOURCE_TIMESTAMP_INVALID = "source_timestamp_invalid"


class ApiFootballEvidenceKind(str, Enum):
    """Evidence origin; injected evidence can never be relabeled as real."""

    INJECTED_FIXTURE = "INJECTED_FIXTURE"
    REAL_NETWORK_CAPTURE = "REAL_NETWORK_CAPTURE"


class ApiFootballRequestKind(str, Enum):
    FIXTURES = "fixtures"
    ODDS = "odds"


@dataclass(frozen=True)
class ApiFootballExperimentPolicy:
    """Explicit evaluation policy; no production timing is implied."""

    maximum_odds_age_seconds: int = 900
    kickoff_tolerance_seconds: int = 90
    require_source_timestamp: bool = True

    def validate(self) -> None:
        if (
            not isinstance(self.maximum_odds_age_seconds, int)
            or isinstance(self.maximum_odds_age_seconds, bool)
            or self.maximum_odds_age_seconds <= 0
        ):
            raise ApiFootballExperimentalError(
                "maximum_odds_age_seconds must be a positive integer"
            )
        if (
            not isinstance(self.kickoff_tolerance_seconds, int)
            or isinstance(self.kickoff_tolerance_seconds, bool)
            or self.kickoff_tolerance_seconds < 0
        ):
            raise ApiFootballExperimentalError(
                "kickoff_tolerance_seconds must be a non-negative integer"
            )
        if not isinstance(self.require_source_timestamp, bool):
            raise ApiFootballExperimentalError(
                "require_source_timestamp must be boolean"
            )


@dataclass(frozen=True)
class ApiFootballRequest:
    """Secret-bearing request held in memory only; safe payload redacts it."""

    kind: ApiFootballRequestKind | str
    endpoint: str
    params: Mapping[str, str]
    request_identity: str
    headers: Mapping[str, str]

    def validate(self) -> None:
        try:
            ApiFootballRequestKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ApiFootballExperimentalError("unknown request kind") from exc
        if not self.endpoint.startswith(API_FOOTBALL_BASE_URL + "/"):
            raise ApiFootballExperimentalError(
                "request endpoint is outside API-Football"
            )
        if not self.request_identity.strip():
            raise ApiFootballExperimentalError("request identity is required")
        if any(not str(key).strip() for key in self.params):
            raise ApiFootballExperimentalError("request parameters contain a blank key")

    def safe_payload(self) -> dict[str, object]:
        self.validate()
        sensitive = {"x-apisports-key", "authorization", "api_key", "apikey"}
        return {
            "kind": ApiFootballRequestKind(self.kind).value,
            "endpoint": self.endpoint,
            "params": {str(key): str(value) for key, value in self.params.items()},
            "request_identity": self.request_identity,
            "headers": {
                str(key): "[REDACTED]" if str(key).lower() in sensitive else str(value)
                for key, value in self.headers.items()
            },
        }


@dataclass(frozen=True)
class ApiFootballResponse:
    """Transport output with no logging or secret-bearing representation."""

    status_code: int | None
    payload: object | None
    headers: Mapping[str, str]
    started_at: datetime
    completed_at: datetime
    latency_ms: int
    error_code: str | None = None
    timeout: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", _utc(self.started_at, "started_at"))
        object.__setattr__(
            self, "completed_at", _utc(self.completed_at, "completed_at")
        )
        if self.completed_at < self.started_at or self.latency_ms < 0:
            raise ApiFootballExperimentalError("response timing is invalid")


ApiFootballTransport = Callable[[ApiFootballRequest, float], ApiFootballResponse]


def requests_transport(
    request: ApiFootballRequest, timeout_seconds: float
) -> ApiFootballResponse:
    """Perform one explicit GET without retries or secret-bearing logs."""

    request.validate()
    started_at = datetime.now(timezone.utc)
    try:
        response = requests.get(
            request.endpoint,
            params=dict(request.params),
            headers=dict(request.headers),
            timeout=timeout_seconds,
        )
    except requests.Timeout:
        completed_at = datetime.now(timezone.utc)
        return ApiFootballResponse(
            None,
            None,
            {},
            started_at,
            completed_at,
            round((completed_at - started_at).total_seconds() * 1000),
            error_code="timeout",
            timeout=True,
        )
    except requests.RequestException:
        completed_at = datetime.now(timezone.utc)
        return ApiFootballResponse(
            None,
            None,
            {},
            started_at,
            completed_at,
            round((completed_at - started_at).total_seconds() * 1000),
            error_code="network_error",
        )
    completed_at = datetime.now(timezone.utc)
    try:
        payload = response.json()
        error_code = None
    except ValueError:
        payload = None
        error_code = "malformed_json"
    return ApiFootballResponse(
        response.status_code,
        payload,
        {str(key): str(value) for key, value in response.headers.items()},
        started_at,
        completed_at,
        round((completed_at - started_at).total_seconds() * 1000),
        error_code=error_code,
    )


@dataclass(frozen=True)
class ApiFootballFixtureCandidate:
    """A Champions League fixture mapped into the canonical Fixture contract."""

    fixture: Fixture
    provider_fixture_id: str
    competition_id: int
    competition_name: str
    source_timestamp: datetime | None

    def validate(self) -> None:
        self.fixture.validate()
        if not self.provider_fixture_id.strip():
            raise ApiFootballExperimentalError("provider fixture ID is required")
        if self.competition_id != API_FOOTBALL_COMPETITION_ID:
            raise ApiFootballExperimentalError("fixture is outside Champions League")
        if self.competition_name.casefold() != API_FOOTBALL_COMPETITION_NAME.casefold():
            raise ApiFootballExperimentalError(
                "Champions League identity is incomplete"
            )
        if self.source_timestamp is not None:
            _utc(self.source_timestamp, "fixture source timestamp")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider_fixture_id": self.provider_fixture_id,
            "competition_id": self.competition_id,
            "competition_name": self.competition_name,
            "fixture": {
                "fixture_key": self.fixture.fixture_key,
                "league_code": self.fixture.league_code,
                "home_team": self.fixture.home_team,
                "away_team": self.fixture.away_team,
                "kickoff": self.fixture.kickoff.isoformat(),
            },
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
        }


@dataclass(frozen=True)
class ApiFootballObservation:
    """Canonical observation plus an immutable origin boundary."""

    canonical: NormalizedOddsObservation
    evidence_kind: ApiFootballEvidenceKind | str
    provider_response_digest: str

    def __post_init__(self) -> None:
        try:
            kind = ApiFootballEvidenceKind(self.evidence_kind)
        except (TypeError, ValueError) as exc:
            raise ApiFootballExperimentalError(
                "unknown API-Football evidence kind"
            ) from exc
        object.__setattr__(self, "evidence_kind", kind)

    @property
    def is_real_network_capture(self) -> bool:
        return self.evidence_kind is ApiFootballEvidenceKind.REAL_NETWORK_CAPTURE

    @property
    def is_injected_fixture(self) -> bool:
        return self.evidence_kind is ApiFootballEvidenceKind.INJECTED_FIXTURE

    def validate(self) -> None:
        self.canonical.validate(require_fresh=False)
        if not self.provider_response_digest.strip():
            raise ApiFootballExperimentalError("provider response digest is required")
        metadata = dict(self.canonical.metadata)
        if metadata.get("evidence_kind") != self.evidence_kind.value:
            raise ApiFootballExperimentalError(
                "canonical evidence origin does not match wrapper"
            )
        if metadata.get("synthetic") is not (self.is_injected_fixture):
            raise ApiFootballExperimentalError(
                "synthetic evidence marker is inconsistent"
            )
        if not self.canonical.candidate_only:
            raise ApiFootballExperimentalError(
                "experimental API-Football observations must remain candidate-only"
            )
        if self.is_injected_fixture:
            if not self.canonical.source_provenance.startswith("injected://"):
                raise ApiFootballExperimentalError(
                    "injected evidence provenance is invalid"
                )
            if self.canonical.source_timing_provenance not in (
                TimingProvenance.SOURCE_TIMESTAMP,
                TimingProvenance.CAPTURE_TIME_ONLY,
            ):
                raise ApiFootballExperimentalError(
                    "injected timing provenance is invalid"
                )
        else:
            if not self.canonical.source_provenance.startswith(API_FOOTBALL_BASE_URL):
                raise ApiFootballExperimentalError(
                    "real evidence provenance is not API-Football"
                )
            if self.canonical.request_identity.startswith("injected:"):
                raise ApiFootballExperimentalError(
                    "real evidence cannot use injected request identity"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_kind": self.evidence_kind.value,
            "provider_response_digest": self.provider_response_digest,
            "canonical": self.canonical.as_payload(),
        }


@dataclass(frozen=True)
class ApiFootballCallResult:
    """Redacted, fail-closed result from one fixture or odds request."""

    request_kind: ApiFootballRequestKind | str
    request_identity: str
    state: ProviderState | str
    failure: ApiFootballFailure | str
    reason: str
    network_called: bool
    status_code: int | None = None
    quota: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    raw_response_digest: str = ""
    fixture_candidates: tuple[ApiFootballFixtureCandidate, ...] = ()
    observations: tuple[ApiFootballObservation, ...] = ()
    payload: object | None = field(default=None, repr=False, compare=False)
    latency_ms: int = 0
    duplicate_observations_suppressed: int = 0
    request_started_at: datetime | None = None
    request_completed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_candidates", tuple(self.fixture_candidates))
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "quota", self.quota)
        try:
            ApiFootballRequestKind(self.request_kind)
            ProviderState(self.state)
            ApiFootballFailure(self.failure)
        except (TypeError, ValueError) as exc:
            raise ApiFootballExperimentalError(
                "call result contains an unknown state"
            ) from exc
        if (
            not self.reason.strip()
            or self.latency_ms < 0
            or self.duplicate_observations_suppressed < 0
        ):
            raise ApiFootballExperimentalError("call result is missing safe metadata")
        self.quota.__post_init__()
        if self.request_started_at is not None:
            _utc(self.request_started_at, "request_started_at")
        if self.request_completed_at is not None:
            _utc(self.request_completed_at, "request_completed_at")
        if (
            self.request_started_at is not None
            and self.request_completed_at is not None
            and self.request_completed_at < self.request_started_at
        ):
            raise ApiFootballExperimentalError("call request timing is invalid")
        for candidate in self.fixture_candidates:
            candidate.validate()
        for observation in self.observations:
            observation.validate()
        if (
            self.observations
            and ProviderState(self.state) is not ProviderState.AVAILABLE
        ):
            raise ApiFootballExperimentalError(
                "failed call cannot contain observations"
            )

    @property
    def accepted_observation_count(self) -> int:
        return len(self.observations)

    def as_payload(self) -> dict[str, object]:
        return {
            "request_kind": ApiFootballRequestKind(self.request_kind).value,
            "request_identity": self.request_identity,
            "state": ProviderState(self.state).value,
            "failure": ApiFootballFailure(self.failure).value,
            "reason": self.reason,
            "network_called": self.network_called,
            "status_code": self.status_code,
            "quota": self.quota.as_payload(),
            "raw_response_digest": self.raw_response_digest,
            "fixture_candidates": [
                item.as_payload() for item in self.fixture_candidates
            ],
            "observations": [item.as_payload() for item in self.observations],
            "latency_ms": self.latency_ms,
            "duplicate_observations_suppressed": self.duplicate_observations_suppressed,
            "request_started_at": (
                self.request_started_at.isoformat() if self.request_started_at else None
            ),
            "request_completed_at": (
                self.request_completed_at.isoformat()
                if self.request_completed_at
                else None
            ),
        }


@dataclass
class ApiFootballDiagnosticBudget:
    """Hard diagnostic request budget; no caller may raise the five-call cap."""

    maximum_requests: int = 5
    requests_used: int = 0

    def __post_init__(self) -> None:
        if self.maximum_requests < 0 or self.maximum_requests > 5:
            raise ApiFootballExperimentalError(
                "diagnostic budget must be between 0 and 5"
            )
        if self.requests_used < 0 or self.requests_used > self.maximum_requests:
            raise ApiFootballExperimentalError("diagnostic requests_used is invalid")

    @property
    def remaining(self) -> int:
        return self.maximum_requests - self.requests_used

    def reserve(self) -> bool:
        if self.remaining <= 0:
            return False
        self.requests_used += 1
        return True


@dataclass(frozen=True)
class DiagnosticPageSummary:
    """Safe summary of an odds response page; raw provider data is not retained."""

    fixture_ids: tuple[str, ...]
    bookmaker_names: tuple[str, ...]
    match_winner_fixture_ids: tuple[str, ...]
    source_timestamps: tuple[datetime, ...]
    paging_total: int
    one_x_two_rows: int

    def as_payload(self) -> dict[str, object]:
        return {
            "fixture_ids": list(self.fixture_ids),
            "bookmaker_names": list(self.bookmaker_names),
            "match_winner_fixture_ids": list(self.match_winner_fixture_ids),
            "source_timestamps": [
                value.isoformat() for value in self.source_timestamps
            ],
            "paging_total": self.paging_total,
            "one_x_two_rows": self.one_x_two_rows,
        }


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiFootballExperimentalError(f"{name} is required")
    return value.strip()


def _parse_timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
    ):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ApiFootballExperimentalError(f"{name} is invalid") from exc
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise ApiFootballExperimentalError(f"{name} is invalid") from exc
    raise ApiFootballExperimentalError(f"{name} is required")


def _optional_timestamp(value: object, name: str) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_timestamp(value, name)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ApiFootballExperimentalError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ApiFootballExperimentalError(f"{name} must be an array")
    return value


def _quota_from_headers(headers: Mapping[str, object]) -> QuotaSnapshot:
    lowered = {
        str(key).casefold(): str(value).strip() for key, value in headers.items()
    }

    def integer(*names: str) -> int | None:
        for name in names:
            raw = lowered.get(name.casefold())
            if raw is None:
                continue
            try:
                value = int(raw)
            except ValueError:
                return None
            return value if value >= 0 else None
        return None

    return QuotaSnapshot(
        remaining=integer("x-ratelimit-requests-remaining"),
        rate_limit=integer("x-ratelimit-limit"),
        rate_remaining=integer("x-ratelimit-remaining"),
    )


def _error_text(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return ""
    errors = payload.get("errors")
    if isinstance(errors, Mapping):
        return " ".join(str(value).casefold() for value in errors.values())
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
        return " ".join(str(value).casefold() for value in errors)
    if isinstance(errors, str):
        return errors.casefold()
    return ""


def _state_for_failure(failure: ApiFootballFailure) -> ProviderState:
    return {
        ApiFootballFailure.NONE: ProviderState.AVAILABLE,
        ApiFootballFailure.CREDENTIAL_MISSING: ProviderState.CREDENTIAL_MISSING,
        ApiFootballFailure.AUTHENTICATION_ERROR: ProviderState.AUTH_FAILED,
        ApiFootballFailure.QUOTA_EXHAUSTED: ProviderState.QUOTA_EXHAUSTED,
        ApiFootballFailure.RATE_LIMITED: ProviderState.RATE_LIMITED,
        ApiFootballFailure.TIMEOUT: ProviderState.TEMPORARILY_UNAVAILABLE,
        ApiFootballFailure.NETWORK_ERROR: ProviderState.TEMPORARILY_UNAVAILABLE,
        ApiFootballFailure.HTTP_ERROR: ProviderState.TEMPORARILY_UNAVAILABLE,
        ApiFootballFailure.MALFORMED_JSON: ProviderState.MALFORMED,
        ApiFootballFailure.MALFORMED_RESPONSE: ProviderState.MALFORMED,
        ApiFootballFailure.MISSING_BOOKMAKER: ProviderState.PARTIAL,
        ApiFootballFailure.MISSING_MATCH_WINNER: ProviderState.UNSUPPORTED_MARKET,
        ApiFootballFailure.MISSING_FIXTURE: ProviderState.UNSUPPORTED_FIXTURE,
        ApiFootballFailure.FIXTURE_MISMATCH: ProviderState.UNSUPPORTED_FIXTURE,
        ApiFootballFailure.WRONG_COMPETITION: ProviderState.UNSUPPORTED_LEAGUE,
        ApiFootballFailure.STALE_DATA: ProviderState.STALE,
        ApiFootballFailure.DUPLICATE_OBSERVATION: ProviderState.QUALITY_REJECTED,
        ApiFootballFailure.INVALID_DECIMAL_ODDS: ProviderState.QUALITY_REJECTED,
        ApiFootballFailure.SOURCE_TIMESTAMP_MISSING: ProviderState.QUALITY_REJECTED,
        ApiFootballFailure.SOURCE_TIMESTAMP_INVALID: ProviderState.QUALITY_REJECTED,
    }[failure]


def _failure_result(
    request_kind: ApiFootballRequestKind,
    request_identity: str,
    failure: ApiFootballFailure,
    reason: str,
    *,
    network_called: bool,
    response: ApiFootballResponse | None = None,
) -> ApiFootballCallResult:
    return ApiFootballCallResult(
        request_kind=request_kind,
        request_identity=request_identity,
        state=_state_for_failure(failure),
        failure=failure,
        reason=reason,
        network_called=network_called,
        status_code=response.status_code if response else None,
        quota=_quota_from_headers(response.headers) if response else QuotaSnapshot(),
        raw_response_digest=digest_record(response.payload) if response else "",
        payload=response.payload if response else None,
        latency_ms=response.latency_ms if response else 0,
        request_started_at=response.started_at if response else None,
        request_completed_at=response.completed_at if response else None,
    )


def _classify_response(
    request_kind: ApiFootballRequestKind,
    request_identity: str,
    response: ApiFootballResponse,
) -> ApiFootballCallResult | None:
    if response.timeout or response.error_code == "timeout":
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.TIMEOUT,
            "provider_timeout",
            network_called=True,
            response=response,
        )
    if response.error_code == "network_error":
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.NETWORK_ERROR,
            "provider_network_error",
            network_called=True,
            response=response,
        )
    if response.error_code == "malformed_json":
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.MALFORMED_JSON,
            "response_is_not_json",
            network_called=True,
            response=response,
        )
    if response.status_code in (401, 403):
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.AUTHENTICATION_ERROR,
            "authentication_failed",
            network_called=True,
            response=response,
        )
    if response.status_code == 429:
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.RATE_LIMITED,
            "provider_rate_limited",
            network_called=True,
            response=response,
        )
    if (
        response.status_code is None
        or response.status_code < 200
        or response.status_code >= 300
    ):
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.HTTP_ERROR,
            "provider_http_error",
            network_called=True,
            response=response,
        )
    error_text = _error_text(response.payload)
    if error_text:
        if any(
            token in error_text
            for token in ("daily", "quota", "requests limit", "request limit reached")
        ):
            failure = ApiFootballFailure.QUOTA_EXHAUSTED
        elif any(
            token in error_text
            for token in ("auth", "key", "credential", "unauthorized")
        ):
            failure = ApiFootballFailure.AUTHENTICATION_ERROR
        elif any(token in error_text for token in ("rate", "too many")):
            failure = ApiFootballFailure.RATE_LIMITED
        else:
            failure = ApiFootballFailure.MALFORMED_RESPONSE
        return _failure_result(
            request_kind,
            request_identity,
            failure,
            "provider_body_error",
            network_called=True,
            response=response,
        )
    if not isinstance(response.payload, Mapping):
        return _failure_result(
            request_kind,
            request_identity,
            ApiFootballFailure.MALFORMED_RESPONSE,
            "response_root_is_not_object",
            network_called=True,
            response=response,
        )
    return None


def _competition_identity(league: Mapping[str, object]) -> bool:
    raw_id = league.get("id")
    if raw_id is not None:
        try:
            if int(raw_id) != API_FOOTBALL_COMPETITION_ID:
                return False
        except (TypeError, ValueError):
            return False
    name = str(league.get("name", "")).strip()
    return name.casefold() == API_FOOTBALL_COMPETITION_NAME.casefold()


def _canonical_team(value: object, name: str) -> str:
    return _text(value, name)


def _fixture_key(home: str, away: str, kickoff: datetime) -> str:
    return f"UCL|{normalize_team_name(home)}|{normalize_team_name(away)}|{kickoff.isoformat()}"


def _fixture_from_entry(
    entry: Mapping[str, object],
    *,
    capture_timestamp: datetime,
) -> ApiFootballFixtureCandidate:
    fixture_meta = _mapping(entry.get("fixture"), "fixture")
    league = _mapping(entry.get("league"), "league")
    teams = _mapping(entry.get("teams"), "teams")
    if not _competition_identity(league):
        raise ApiFootballExperimentalError("fixture belongs to another competition")
    fixture_id = str(fixture_meta.get("id", "")).strip()
    kickoff = _parse_timestamp(fixture_meta.get("date"), "fixture.date")
    home_meta = _mapping(teams.get("home"), "teams.home")
    away_meta = _mapping(teams.get("away"), "teams.away")
    home = _canonical_team(home_meta.get("name"), "teams.home.name")
    away = _canonical_team(away_meta.get("name"), "teams.away.name")
    candidate = ApiFootballFixtureCandidate(
        fixture=Fixture(_fixture_key(home, away, kickoff), "UCL", home, away, kickoff),
        provider_fixture_id=_text(fixture_id, "fixture.id"),
        competition_id=API_FOOTBALL_COMPETITION_ID,
        competition_name=API_FOOTBALL_COMPETITION_NAME,
        source_timestamp=_optional_timestamp(
            entry.get("update") or fixture_meta.get("update"), "fixture.update"
        ),
    )
    candidate.validate()
    if (
        candidate.source_timestamp is not None
        and candidate.source_timestamp > capture_timestamp
    ):
        raise ApiFootballExperimentalError("fixture source timestamp is in the future")
    return candidate


def _match_fixture(
    entry: Mapping[str, object],
    expected: ApiFootballFixtureCandidate,
    *,
    provider_fixture_id: str,
    kickoff_tolerance_seconds: int,
) -> ApiFootballFailure | None:
    try:
        fixture_meta = _mapping(entry.get("fixture"), "fixture")
        league = _mapping(entry.get("league"), "league")
        if not _competition_identity(league):
            return ApiFootballFailure.WRONG_COMPETITION
        if str(fixture_meta.get("id", "")).strip() != provider_fixture_id:
            return ApiFootballFailure.FIXTURE_MISMATCH
        kickoff = _parse_timestamp(fixture_meta.get("date"), "fixture.date")
        if (
            abs((kickoff - expected.fixture.kickoff).total_seconds())
            > kickoff_tolerance_seconds
        ):
            return ApiFootballFailure.FIXTURE_MISMATCH
        teams = entry.get("teams")
        if teams is not None:
            teams_map = _mapping(teams, "teams")
            home = _mapping(teams_map.get("home"), "teams.home")
            away = _mapping(teams_map.get("away"), "teams.away")
            if normalize_team_name(home.get("name")) != normalize_team_name(
                expected.fixture.home_team
            ):
                return ApiFootballFailure.FIXTURE_MISMATCH
            if normalize_team_name(away.get("name")) != normalize_team_name(
                expected.fixture.away_team
            ):
                return ApiFootballFailure.FIXTURE_MISMATCH
    except (ApiFootballExperimentalError, TypeError, ValueError):
        return ApiFootballFailure.FIXTURE_MISMATCH
    return None


def _source_timestamp(
    entry: Mapping[str, object],
    bookmaker: Mapping[str, object],
    bet: Mapping[str, object],
    response: Mapping[str, object],
) -> datetime | None:
    for value in (
        bookmaker.get("update"),
        bookmaker.get("last_update"),
        bet.get("update"),
        bet.get("last_update"),
        entry.get("update"),
        entry.get("last_update"),
        response.get("update"),
        response.get("last_update"),
    ):
        if value is not None and str(value).strip():
            return _parse_timestamp(value, "odds source timestamp")
    return None


def _decimal_odds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed) or parsed <= 1.0:
        return None
    return parsed


def _outcome_key(value: object, expected: ApiFootballFixtureCandidate) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = normalize_team_name(raw).casefold()
    if normalized in {
        "home",
        normalize_team_name(expected.fixture.home_team).casefold(),
    }:
        return "home"
    if normalized in {
        "away",
        normalize_team_name(expected.fixture.away_team).casefold(),
    }:
        return "away"
    if normalized in {"draw", "x", "tie"}:
        return "draw"
    return None


def _normalize_bookmaker(
    entry: Mapping[str, object],
    bookmaker: Mapping[str, object],
    expected: ApiFootballFixtureCandidate,
    *,
    capture_timestamp: datetime,
    request_identity: str,
    raw_response_digest: str,
    policy: ApiFootballExperimentPolicy,
    evidence_kind: ApiFootballEvidenceKind,
    response_root: Mapping[str, object],
    source_provenance: str,
    request_started_at: datetime,
    request_completed_at: datetime,
) -> tuple[ApiFootballObservation | None, ApiFootballFailure | None]:
    bookmaker_id = bookmaker.get("id")
    bookmaker_name = str(bookmaker.get("name") or bookmaker_id or "").strip()
    if not bookmaker_name:
        return None, ApiFootballFailure.MISSING_BOOKMAKER
    bets = bookmaker.get("bets")
    if not isinstance(bets, Sequence) or isinstance(bets, (str, bytes, bytearray)):
        return None, ApiFootballFailure.MISSING_MATCH_WINNER
    winner_bet: Mapping[str, object] | None = None
    for candidate in bets:
        if not isinstance(candidate, Mapping):
            continue
        bet_id = str(candidate.get("id", "")).strip()
        bet_name = str(candidate.get("name", "")).strip().casefold()
        if bet_id == str(API_FOOTBALL_MARKET_ID) or bet_name in {"match winner", "1x2"}:
            winner_bet = candidate
            break
    if winner_bet is None:
        return None, ApiFootballFailure.MISSING_MATCH_WINNER
    values = winner_bet.get("values")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return None, ApiFootballFailure.INVALID_DECIMAL_ODDS
    odds: dict[str, float] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        key = _outcome_key(value.get("value"), expected)
        if key is None:
            continue
        price = _decimal_odds(value.get("odd"))
        if price is None or key in odds:
            return None, ApiFootballFailure.INVALID_DECIMAL_ODDS
        odds[key] = price
    if set(odds) != {"home", "draw", "away"}:
        return None, ApiFootballFailure.INVALID_DECIMAL_ODDS
    try:
        source_timestamp = _source_timestamp(
            entry, bookmaker, winner_bet, response_root
        )
    except ApiFootballExperimentalError:
        return None, ApiFootballFailure.SOURCE_TIMESTAMP_INVALID
    if source_timestamp is None and policy.require_source_timestamp:
        return None, ApiFootballFailure.SOURCE_TIMESTAMP_MISSING
    if source_timestamp is not None:
        if source_timestamp > capture_timestamp:
            return None, ApiFootballFailure.SOURCE_TIMESTAMP_INVALID
        if (
            capture_timestamp - source_timestamp
        ).total_seconds() > policy.maximum_odds_age_seconds:
            return None, ApiFootballFailure.STALE_DATA
        timing = TimingProvenance.SOURCE_TIMESTAMP
    else:
        timing = TimingProvenance.CAPTURE_TIME_ONLY
    canonical = NormalizedOddsObservation(
        league_code="UCL",
        fixture_key=expected.fixture.fixture_key,
        provider_fixture_id=expected.provider_fixture_id,
        home_team=expected.fixture.home_team,
        away_team=expected.fixture.away_team,
        kickoff_utc=expected.fixture.kickoff,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=odds["home"],
        draw_odds=odds["draw"],
        away_odds=odds["away"],
        provider_identity=API_FOOTBALL_PROVIDER_IDENTITY,
        bookmaker_identity=bookmaker_name,
        source_timestamp=source_timestamp,
        captured_at=request_completed_at,
        request_identity=request_identity,
        request_started_at=request_started_at,
        request_completed_at=request_completed_at,
        latency_ms=max(
            0,
            round((request_completed_at - request_started_at).total_seconds() * 1000),
        ),
        provider_priority=0,
        fallback_depth=0,
        quota_state_after=QuotaSnapshot(),
        source_provenance=source_provenance,
        raw_record_digest=digest_record(entry),
        adapter_version=API_FOOTBALL_ADAPTER_VERSION,
        completeness=ObservationCompleteness.COMPLETE,
        error_classification=ProviderState.AVAILABLE,
        candidate_only=True,
        metadata={
            "provider_family": API_FOOTBALL_PROVIDER_FAMILY,
            "evidence_kind": evidence_kind.value,
            "synthetic": evidence_kind is ApiFootballEvidenceKind.INJECTED_FIXTURE,
            "competition_id": API_FOOTBALL_COMPETITION_ID,
            "market_id": API_FOOTBALL_MARKET_ID,
            "source_timestamp_available": source_timestamp is not None,
        },
        source_timing_provenance=timing,
    )
    observation = ApiFootballObservation(canonical, evidence_kind, raw_response_digest)
    observation.validate()
    return observation, None


def summarize_odds_payload(payload: Mapping[str, object]) -> DiagnosticPageSummary:
    """Extract safe coverage/freshness facts without normalizing or authorizing odds."""

    root = _mapping(payload, "odds response")
    entries = _sequence(root.get("response"), "odds response.response")
    fixture_ids: set[str] = set()
    bookmakers: set[str] = set()
    winner_fixtures: set[str] = set()
    timestamps: set[datetime] = set()
    rows = 0
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        fixture_meta = raw_entry.get("fixture")
        if not isinstance(fixture_meta, Mapping):
            continue
        fixture_id = str(fixture_meta.get("id", "")).strip()
        if fixture_id:
            fixture_ids.add(fixture_id)
        raw_bookmakers = raw_entry.get("bookmakers")
        if not isinstance(raw_bookmakers, Sequence) or isinstance(
            raw_bookmakers, (str, bytes, bytearray)
        ):
            continue
        for raw_bookmaker in raw_bookmakers:
            if not isinstance(raw_bookmaker, Mapping):
                continue
            name = str(
                raw_bookmaker.get("name") or raw_bookmaker.get("id") or ""
            ).strip()
            if name:
                bookmakers.add(name)
            bets = raw_bookmaker.get("bets")
            if not isinstance(bets, Sequence) or isinstance(
                bets, (str, bytes, bytearray)
            ):
                continue
            for raw_bet in bets:
                if not isinstance(raw_bet, Mapping):
                    continue
                if str(raw_bet.get("id", "")).strip() != str(
                    API_FOOTBALL_MARKET_ID
                ) and str(raw_bet.get("name", "")).casefold() not in {
                    "match winner",
                    "1x2",
                }:
                    continue
                rows += 1
                if fixture_id:
                    winner_fixtures.add(fixture_id)
                try:
                    timestamp = _source_timestamp(
                        raw_entry, raw_bookmaker, raw_bet, root
                    )
                except ApiFootballExperimentalError:
                    timestamp = None
                if timestamp is not None:
                    timestamps.add(timestamp)
    paging = root.get("paging")
    total = 1
    if isinstance(paging, Mapping):
        try:
            total = max(1, int(paging.get("total", 1)))
        except (TypeError, ValueError):
            total = 1
    return DiagnosticPageSummary(
        fixture_ids=tuple(sorted(fixture_ids)),
        bookmaker_names=tuple(sorted(bookmakers)),
        match_winner_fixture_ids=tuple(sorted(winner_fixtures)),
        source_timestamps=tuple(sorted(timestamps)),
        paging_total=total,
        one_x_two_rows=rows,
    )


class ApiFootballExperimentalAdapter:
    """Opt-in experimental adapter; never registered in active SportsBrain paths."""

    provider_identity = API_FOOTBALL_PROVIDER_IDENTITY
    transport_capability = "NETWORK_CAPABLE_EXPERIMENTAL_ONLY"

    def __init__(
        self,
        *,
        transport: ApiFootballTransport = requests_transport,
        policy: ApiFootballExperimentPolicy | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0 or not isfinite(timeout_seconds):
            raise ApiFootballExperimentalError(
                "timeout_seconds must be positive and finite"
            )
        self._transport = transport
        self.policy = policy or ApiFootballExperimentPolicy()
        self.policy.validate()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _require_api_key(api_key: str | None) -> str | None:
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        return api_key.strip()

    def _call(
        self,
        *,
        kind: ApiFootballRequestKind,
        request_identity: str,
        params: Mapping[str, str],
        api_key: str | None,
    ) -> ApiFootballCallResult:
        key = self._require_api_key(api_key)
        if key is None:
            return _failure_result(
                kind,
                request_identity,
                ApiFootballFailure.CREDENTIAL_MISSING,
                "api_football_credential_missing",
                network_called=False,
            )
        request = ApiFootballRequest(
            kind=kind,
            endpoint=f"{API_FOOTBALL_BASE_URL}/{kind.value}",
            params={str(k): str(v) for k, v in params.items()},
            request_identity=_text(request_identity, "request_identity"),
            headers={"x-apisports-key": key, "Accept": "application/json"},
        )
        response = self._transport(request, self.timeout_seconds)
        classified = _classify_response(kind, request_identity, response)
        if classified is not None:
            return classified
        assert isinstance(response.payload, Mapping)
        return ApiFootballCallResult(
            request_kind=kind,
            request_identity=request_identity,
            state=ProviderState.AVAILABLE,
            failure=ApiFootballFailure.NONE,
            reason="provider_response_received",
            network_called=True,
            status_code=response.status_code,
            quota=_quota_from_headers(response.headers),
            raw_response_digest=digest_record(response.payload),
            payload=response.payload,
            latency_ms=response.latency_ms,
            request_started_at=response.started_at,
            request_completed_at=response.completed_at,
        )

    def fetch_real_fixtures(
        self,
        *,
        api_key: str | None,
        season: int,
        match_date: date,
        request_identity: str,
        capture_timestamp: datetime | None = None,
    ) -> ApiFootballCallResult:
        """Fetch one Champions League date slice; no retries or fallback."""

        if not isinstance(season, int) or isinstance(season, bool) or season < 2000:
            raise ApiFootballExperimentalError("season must be a valid year")
        capture = _utc(
            capture_timestamp or datetime.now(timezone.utc), "capture_timestamp"
        )
        result = self._call(
            kind=ApiFootballRequestKind.FIXTURES,
            request_identity=request_identity,
            params={
                "league": str(API_FOOTBALL_COMPETITION_ID),
                "season": str(season),
                "date": match_date.isoformat(),
            },
            api_key=api_key,
        )
        if result.state is not ProviderState.AVAILABLE:
            return result
        response_capture = result.request_completed_at or capture
        try:
            candidates = self._normalize_fixture_payload(
                result.payload, capture_timestamp=response_capture
            )
        except ApiFootballExperimentalError:
            return ApiFootballCallResult(
                request_kind=ApiFootballRequestKind.FIXTURES,
                request_identity=request_identity,
                state=ProviderState.MALFORMED,
                failure=ApiFootballFailure.MALFORMED_RESPONSE,
                reason="fixture_payload_failed_validation",
                network_called=True,
                status_code=result.status_code,
                quota=result.quota,
                raw_response_digest=result.raw_response_digest,
                payload=result.payload,
                latency_ms=result.latency_ms,
                request_started_at=result.request_started_at,
                request_completed_at=result.request_completed_at,
            )
        if not candidates:
            return ApiFootballCallResult(
                request_kind=ApiFootballRequestKind.FIXTURES,
                request_identity=request_identity,
                state=ProviderState.UNSUPPORTED_FIXTURE,
                failure=ApiFootballFailure.MISSING_FIXTURE,
                reason="no_champions_league_fixture_returned",
                network_called=True,
                status_code=result.status_code,
                quota=result.quota,
                raw_response_digest=result.raw_response_digest,
                payload=result.payload,
                latency_ms=result.latency_ms,
                request_started_at=result.request_started_at,
                request_completed_at=result.request_completed_at,
            )
        return result.__class__(
            **{
                **result.__dict__,
                "fixture_candidates": tuple(candidates),
            }
        )

    def _normalize_fixture_payload(
        self, payload: object, *, capture_timestamp: datetime
    ) -> tuple[ApiFootballFixtureCandidate, ...]:
        root = _mapping(payload, "fixtures response")
        entries = _sequence(root.get("response"), "fixtures response.response")
        candidates: list[ApiFootballFixtureCandidate] = []
        seen: set[str] = set()
        for raw_entry in entries:
            if not isinstance(raw_entry, Mapping):
                continue
            candidate = _fixture_from_entry(
                raw_entry, capture_timestamp=capture_timestamp
            )
            if candidate.provider_fixture_id in seen:
                continue
            seen.add(candidate.provider_fixture_id)
            candidates.append(candidate)
        return tuple(candidates)

    def normalize_injected_fixtures(
        self,
        payload: Mapping[str, object],
        *,
        capture_timestamp: datetime,
        request_identity: str = "injected:api-football:fixtures",
    ) -> tuple[ApiFootballFixtureCandidate, ...]:
        """Normalize a fixture fixture without network access."""

        return self._normalize_fixture_payload(
            payload, capture_timestamp=_utc(capture_timestamp, "capture_timestamp")
        )

    def _normalize_odds_payload(
        self,
        payload: Mapping[str, object],
        expected: ApiFootballFixtureCandidate,
        *,
        capture_timestamp: datetime,
        request_identity: str,
        raw_response_digest: str,
        evidence_kind: ApiFootballEvidenceKind,
        provider_fixture_id: str,
        request_started_at: datetime,
        request_completed_at: datetime,
    ) -> tuple[ApiFootballObservation, ...]:
        root = _mapping(payload, "odds response")
        entries = _sequence(root.get("response"), "odds response.response")
        all_observations: list[ApiFootballObservation] = []
        failure_seen: ApiFootballFailure | None = None
        seen: set[tuple[str, str, str, float, float, float]] = set()
        for raw_entry in entries:
            if not isinstance(raw_entry, Mapping):
                continue
            mismatch = _match_fixture(
                raw_entry,
                expected,
                provider_fixture_id=provider_fixture_id,
                kickoff_tolerance_seconds=self.policy.kickoff_tolerance_seconds,
            )
            if mismatch is not None:
                failure_seen = mismatch
                continue
            raw_bookmakers = raw_entry.get("bookmakers")
            if not isinstance(raw_bookmakers, Sequence) or isinstance(
                raw_bookmakers, (str, bytes, bytearray)
            ):
                failure_seen = ApiFootballFailure.MISSING_BOOKMAKER
                continue
            for raw_bookmaker in raw_bookmakers:
                if not isinstance(raw_bookmaker, Mapping):
                    failure_seen = ApiFootballFailure.MISSING_BOOKMAKER
                    continue
                observation, failure = _normalize_bookmaker(
                    raw_entry,
                    raw_bookmaker,
                    expected,
                    capture_timestamp=capture_timestamp,
                    request_identity=request_identity,
                    raw_response_digest=raw_response_digest,
                    policy=self.policy,
                    evidence_kind=evidence_kind,
                    response_root=root,
                    source_provenance=(
                        "injected://app-b4/api-football/odds"
                        if evidence_kind is ApiFootballEvidenceKind.INJECTED_FIXTURE
                        else f"{API_FOOTBALL_BASE_URL}/odds?fixture={provider_fixture_id}"
                    ),
                    request_started_at=request_started_at,
                    request_completed_at=request_completed_at,
                )
                if observation is None:
                    failure_seen = failure or failure_seen
                    continue
                canonical = observation.canonical
                key = (
                    canonical.provider_fixture_id,
                    canonical.bookmaker_identity,
                    canonical.source_timestamp.isoformat()
                    if canonical.source_timestamp
                    else "",
                    canonical.home_odds,
                    canonical.draw_odds,
                    canonical.away_odds,
                )
                if key in seen:
                    failure_seen = ApiFootballFailure.DUPLICATE_OBSERVATION
                    continue
                seen.add(key)
                all_observations.append(observation)
        if all_observations:
            return tuple(all_observations)
        failure = failure_seen or ApiFootballFailure.MISSING_FIXTURE
        raise ApiFootballExperimentalError(failure.value)

    def normalize_injected_odds(
        self,
        payload: Mapping[str, object],
        expected: ApiFootballFixtureCandidate,
        *,
        capture_timestamp: datetime,
        request_identity: str = "injected:api-football:odds",
        provider_fixture_id: str | None = None,
    ) -> tuple[ApiFootballObservation, ...]:
        """Normalize explicit test data; output is permanently synthetic."""

        expected.validate()
        capture = _utc(capture_timestamp, "capture_timestamp")
        return self._normalize_odds_payload(
            payload,
            expected,
            capture_timestamp=capture,
            request_identity=_text(request_identity, "request_identity"),
            raw_response_digest=digest_record(payload),
            evidence_kind=ApiFootballEvidenceKind.INJECTED_FIXTURE,
            provider_fixture_id=provider_fixture_id or expected.provider_fixture_id,
            request_started_at=capture,
            request_completed_at=capture,
        )

    def fetch_real_odds(
        self,
        expected: ApiFootballFixtureCandidate,
        *,
        api_key: str | None,
        request_identity: str,
        provider_fixture_id: str | None = None,
        capture_timestamp: datetime | None = None,
    ) -> ApiFootballCallResult:
        """Fetch one fixture's odds; successful output remains candidate-only."""

        expected.validate()
        capture = _utc(
            capture_timestamp or datetime.now(timezone.utc), "capture_timestamp"
        )
        provider_id = provider_fixture_id or expected.provider_fixture_id
        result = self._call(
            kind=ApiFootballRequestKind.ODDS,
            request_identity=request_identity,
            params={
                "fixture": provider_id,
                "bet": str(API_FOOTBALL_MARKET_ID),
                "page": "1",
            },
            api_key=api_key,
        )
        if result.state is not ProviderState.AVAILABLE:
            return result
        try:
            observations = self._normalize_odds_payload(
                result.payload,
                expected,
                capture_timestamp=result.request_completed_at or capture,
                request_identity=request_identity,
                raw_response_digest=result.raw_response_digest,
                evidence_kind=ApiFootballEvidenceKind.REAL_NETWORK_CAPTURE,
                provider_fixture_id=provider_id,
                request_started_at=result.request_started_at or capture,
                request_completed_at=result.request_completed_at or capture,
            )
        except ApiFootballExperimentalError as exc:
            failure = (
                ApiFootballFailure(str(exc))
                if str(exc) in {item.value for item in ApiFootballFailure}
                else ApiFootballFailure.MALFORMED_RESPONSE
            )
            return ApiFootballCallResult(
                request_kind=ApiFootballRequestKind.ODDS,
                request_identity=request_identity,
                state=_state_for_failure(failure),
                failure=failure,
                reason="odds_payload_failed_validation",
                network_called=True,
                status_code=result.status_code,
                quota=result.quota,
                raw_response_digest=result.raw_response_digest,
                payload=result.payload,
                latency_ms=result.latency_ms,
                request_started_at=result.request_started_at,
                request_completed_at=result.request_completed_at,
            )
        return ApiFootballCallResult(
            request_kind=ApiFootballRequestKind.ODDS,
            request_identity=request_identity,
            state=ProviderState.AVAILABLE,
            failure=ApiFootballFailure.NONE,
            reason="real_network_observations_validated",
            network_called=True,
            status_code=result.status_code,
            quota=result.quota,
            raw_response_digest=result.raw_response_digest,
            observations=observations,
            payload=result.payload,
            latency_ms=result.latency_ms,
            request_started_at=result.request_started_at,
            request_completed_at=result.request_completed_at,
        )


__all__ = [
    "API_FOOTBALL_ADAPTER_VERSION",
    "API_FOOTBALL_BASE_URL",
    "API_FOOTBALL_COMPETITION_ID",
    "API_FOOTBALL_COMPETITION_NAME",
    "API_FOOTBALL_MARKET_ID",
    "API_FOOTBALL_PAGE_SIZE",
    "API_FOOTBALL_PROVIDER_FAMILY",
    "API_FOOTBALL_PROVIDER_IDENTITY",
    "ApiFootballCallResult",
    "ApiFootballDiagnosticBudget",
    "ApiFootballEvidenceKind",
    "ApiFootballExperimentPolicy",
    "ApiFootballExperimentalAdapter",
    "ApiFootballExperimentalError",
    "ApiFootballFailure",
    "ApiFootballFixtureCandidate",
    "ApiFootballObservation",
    "ApiFootballRequest",
    "ApiFootballRequestKind",
    "ApiFootballResponse",
    "DiagnosticPageSummary",
    "requests_transport",
    "summarize_odds_payload",
]
