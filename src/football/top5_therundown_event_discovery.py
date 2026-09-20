"""Non-authorizing TheRundown provider-event discovery gate.

This module solves only the bootstrap problem for the later strict
five-league network authorization.  It resolves provider event IDs from the
same dated league snapshot that is explicitly authorized for discovery.  It
does not issue the real-run authorization, create a receipt, grant provider
authority, or activate any production path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import Protocol

from src.football.odds.therundown import (
    THERUNDOWN_BASE_URL,
    THERUNDOWN_MONEYLINE_MARKET_ID,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
    TheRundownExperimentalAdapter,
)
from src.football.production_contracts import Fixture, _utc
from src.football.provider_cascade.contracts import (
    TOP5_LEAGUE_CODES,
    CascadeTimingPolicy,
    digest_record,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
    TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
    TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
    TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkParticipantScopeV1,
    TheRundownNetworkRequestScopeV1,
)
from src.football.top5_therundown_shadow_canary import (
    TheRundownCanaryTargetV1,
)

DISCOVERY_SCHEMA_VERSION = "top5-therundown-provider-event-discovery-v1"
DISCOVERY_AUTHORIZATION_SCHEMA_VERSION = (
    "top5-therundown-provider-event-discovery-authorization-v1"
)
DISCOVERY_EVIDENCE_SCHEMA_VERSION = (
    "top5-therundown-provider-event-discovery-evidence-v1"
)
DISCOVERY_LEAGUE_ORDER = ("EPL", "BL1", "LL", "SA", "L1")
DISCOVERY_KICKOFF_TOLERANCE_SECONDS = 60
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_PREMATCH_STATUSES = frozenset(
    {
        "scheduled",
        "statusscheduled",
        "notstarted",
        "statusnotstarted",
        "prematch",
        "upcoming",
        "statusupcoming",
    }
)
_STATE_REJECT_FLAGS = (
    "closed",
    "completed",
    "in_play",
    "inplay",
    "live",
    "is_closed",
    "is_completed",
    "is_in_play",
    "is_live",
)


class EventDiscoveryContractError(ValueError):
    """Malformed discovery input or non-authorizing evidence."""


class EventDiscoveryExecutionBlocked(EventDiscoveryContractError):
    """Fail-closed refusal during the bounded discovery stage."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventDiscoveryContractError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise EventDiscoveryContractError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _utc_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        try:
            return _utc(value, name)
        except Exception as exc:
            raise EventDiscoveryContractError(f"{name} must be timezone-aware") from exc
    if not isinstance(value, str) or not value.strip():
        raise EventDiscoveryContractError(f"{name} must be an ISO timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except (TypeError, ValueError) as exc:
        raise EventDiscoveryContractError(f"{name} must be an ISO timestamp") from exc


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return _utc_datetime(value, "timestamp").isoformat()
    return value


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _team_key(value: object) -> str:
    import unicodedata

    raw = str(value or "").strip()
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", raw).casefold()
        if not unicodedata.combining(char) and char.isalnum()
    )


def _status_values(record: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("status", "state", "event_status", "phase"):
        value = record.get(key)
        if value is not None and str(value).strip():
            values.append(_team_key(value))
    score = record.get("score")
    if isinstance(score, Mapping):
        for key in ("status", "state", "event_status", "phase"):
            value = score.get(key)
            if value is not None and str(value).strip():
                values.append(_team_key(value))
    return tuple(values)


def _is_true(value: object) -> bool:
    return value is True or (
        isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"}
    )


def _prematch(event: Mapping[str, object]) -> bool:
    statuses = _status_values(event)
    if not statuses or any(status not in _PREMATCH_STATUSES for status in statuses):
        return False
    return not any(_is_true(event.get(key)) for key in _STATE_REJECT_FLAGS)


def _provider_shape(target: TheRundownEventDiscoveryTargetV1) -> dict[str, object]:
    sport_id = THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[target.league]
    return {
        "method": "GET",
        "endpoint": f"{THERUNDOWN_BASE_URL}/sports/{sport_id}/events/{target.kickoff.date().isoformat()}",
        "params": {
            "market_ids": str(THERUNDOWN_MONEYLINE_MARKET_ID),
            "main_line": "true",
            "hide_closed": "true",
            "hide_no_markets": "true",
        },
        "league": target.league,
        "fixture_key": target.fixture_key,
        "request_identity": target.request_identity,
    }


@dataclass(frozen=True)
class TheRundownEventDiscoveryTargetV1:
    """Stable fixture scope; deliberately contains no provider event ID."""

    provider: str
    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime
    home_participant_id: str
    away_participant_id: str
    request_identity: str

    def validate(self) -> None:
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError("discovery provider identity is invalid")
        if self.league not in TOP5_LEAGUE_CODES:
            raise EventDiscoveryContractError("discovery league is outside Top-5")
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("home_participant_id", self.home_participant_id),
            ("away_participant_id", self.away_participant_id),
            ("request_identity", self.request_identity),
        ):
            _text(value, name)
        if self.home_participant_id == self.away_participant_id:
            raise EventDiscoveryContractError("participant IDs must be distinct")
        kickoff = _utc_datetime(self.kickoff, "kickoff")
        try:
            expected = make_fixture_key(
                self.league, self.home_team, self.away_team, kickoff
            )
        except Exception as exc:
            raise EventDiscoveryContractError("fixture scope is malformed") from exc
        if self.fixture_key != expected:
            raise EventDiscoveryContractError(
                "fixture key does not match fixture scope"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _utc_datetime(self.kickoff, "kickoff").isoformat(),
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
            "request_identity": self.request_identity,
        }


@dataclass(frozen=True)
class TheRundownEventDiscoveryAuthorizationV1:
    discovery_authorization_id: str
    ceo_discovery_authorization_identity: str
    provider: str
    targets: tuple[TheRundownEventDiscoveryTargetV1, ...]
    adapter_version: str
    adapter_source_sha: str
    request_shape_digest: str
    issued_at: datetime
    expires_at: datetime
    maximum_request_count: int = TOP5_CONTROLLED_SHADOW_REQUEST_COUNT
    maximum_datapoints: int = TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET
    maximum_datapoints_per_request: int = THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
    minimum_interval_seconds: float = TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS
    maximum_retries: int = 0
    no_bet: bool = True
    candidate_qualification: bool = False
    production_authority: bool = False
    activation: bool = False
    publication: bool = False
    ledger_mutation: bool = False
    monetary_spend_authorized: bool = False

    @property
    def authorization_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": DISCOVERY_AUTHORIZATION_SCHEMA_VERSION,
            "discovery_authorization_id": self.discovery_authorization_id,
            "ceo_discovery_authorization_identity": self.ceo_discovery_authorization_identity,
            "provider": self.provider,
            "targets": [target.as_payload() for target in self.targets],
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "request_shape_digest": self.request_shape_digest,
            "issued_at": _utc_datetime(self.issued_at, "issued_at").isoformat(),
            "expires_at": _utc_datetime(self.expires_at, "expires_at").isoformat(),
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoints": self.maximum_datapoints,
            "maximum_datapoints_per_request": self.maximum_datapoints_per_request,
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "maximum_retries": self.maximum_retries,
            "no_bet": self.no_bet,
            "candidate_qualification": self.candidate_qualification,
            "production_authority": self.production_authority,
            "activation": self.activation,
            "publication": self.publication,
            "ledger_mutation": self.ledger_mutation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    def validate(self, *, now: datetime | None = None) -> None:
        _text(self.discovery_authorization_id, "discovery_authorization_id")
        _text(
            self.ceo_discovery_authorization_identity,
            "ceo_discovery_authorization_identity",
        )
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError(
                "discovery provider is not candidate-only"
            )
        if len(self.targets) != len(DISCOVERY_LEAGUE_ORDER):
            raise EventDiscoveryContractError("discovery requires exactly five targets")
        if tuple(target.league for target in self.targets) != DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError("discovery league order is not canonical")
        for target in self.targets:
            target.validate()
        if len({target.fixture_key for target in self.targets}) != len(self.targets):
            raise EventDiscoveryContractError(
                "discovery fixture identities must be unique"
            )
        if len({target.request_identity for target in self.targets}) != len(
            self.targets
        ):
            raise EventDiscoveryContractError(
                "discovery request identities must be unique"
            )
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _sha(self.request_shape_digest, "request_shape_digest")
        expected_shape_digest = _digest(
            [_provider_shape(target) for target in self.targets]
        )
        if expected_shape_digest != self.request_shape_digest.lower():
            raise EventDiscoveryContractError("request-shape digest mismatch")
        if self.maximum_request_count != TOP5_CONTROLLED_SHADOW_REQUEST_COUNT:
            raise EventDiscoveryExecutionBlocked(
                "discovery request budget must be five"
            )
        if self.maximum_datapoints != TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET:
            raise EventDiscoveryExecutionBlocked(
                "discovery datapoint budget must be 275"
            )
        if (
            self.maximum_datapoints_per_request
            != THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery request datapoint cap must be 55"
            )
        if self.maximum_retries != 0:
            raise EventDiscoveryExecutionBlocked("discovery retries are forbidden")
        if (
            isinstance(self.minimum_interval_seconds, bool)
            or not isinstance(self.minimum_interval_seconds, (int, float))
            or not isfinite(float(self.minimum_interval_seconds))
            or self.minimum_interval_seconds
            < TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery pacing must be at least 1.1 seconds"
            )
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
                raise EventDiscoveryExecutionBlocked(f"unsafe discovery flag: {name}")
        issued = _utc_datetime(self.issued_at, "issued_at")
        expires = _utc_datetime(self.expires_at, "expires_at")
        if expires <= issued:
            raise EventDiscoveryContractError("discovery expiry must follow issue time")
        current = _utc_datetime(now or datetime.now(timezone.utc), "discovery now")
        if current < issued or current >= expires:
            raise EventDiscoveryExecutionBlocked("discovery authorization is expired")

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        return {
            **self._payload_without_digest(),
            "authorization_digest": self.authorization_digest,
        }

    def request_shape(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        payload = [_provider_shape(target) for target in self.targets]
        return {"requests": payload}


@dataclass(frozen=True)
class TheRundownEventDiscoveryRequestV1:
    authorization: TheRundownEventDiscoveryAuthorizationV1
    target: TheRundownEventDiscoveryTargetV1
    sequence: int

    def validate(self) -> None:
        self.authorization.validate(now=self.authorization.issued_at)
        self.target.validate()
        if self.target.league not in DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError("request league is invalid")
        if self.sequence != DISCOVERY_LEAGUE_ORDER.index(self.target.league):
            raise EventDiscoveryExecutionBlocked("discovery request order is invalid")


@dataclass(frozen=True)
class TheRundownEventDiscoveryResponseV1:
    status_code: int
    payload: object
    headers: Mapping[str, str]
    started_at: datetime
    finished_at: datetime
    retry_count: int = 0
    network_execution: bool = False

    def validate(self) -> None:
        if self.status_code != 200:
            raise EventDiscoveryExecutionBlocked("discovery response is not HTTP 200")
        if self.retry_count != 0:
            raise EventDiscoveryExecutionBlocked("discovery response reports a retry")
        started = _utc_datetime(self.started_at, "response started_at")
        finished = _utc_datetime(self.finished_at, "response finished_at")
        if finished < started:
            raise EventDiscoveryContractError("response timestamps are not ordered")
        if not isinstance(self.payload, Mapping):
            raise EventDiscoveryExecutionBlocked("discovery payload is not an object")
        if not isinstance(self.payload.get("events"), list):
            raise EventDiscoveryExecutionBlocked("discovery payload has no events list")
        if not isinstance(self.network_execution, bool):
            raise EventDiscoveryContractError("network_execution must be boolean")


class TheRundownEventDiscoveryTransport(Protocol):
    def execute(
        self, request: TheRundownEventDiscoveryRequestV1
    ) -> TheRundownEventDiscoveryResponseV1: ...


@dataclass(frozen=True)
class TheRundownEventDiscoveryEvidenceV1:
    discovery_authorization_id: str
    discovery_authorization_digest: str
    provider: str
    league: str
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
    provider_event_evidence_digest: str
    datapoints: int
    remaining_datapoints: int
    retry_count: int = 0
    network_execution: bool = False
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
            "schema_version": DISCOVERY_EVIDENCE_SCHEMA_VERSION,
            "discovery_authorization_id": self.discovery_authorization_id,
            "discovery_authorization_digest": self.discovery_authorization_digest,
            "provider": self.provider,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
            "kickoff": _utc_datetime(self.kickoff, "kickoff").isoformat(),
            "provider_event_id": self.provider_event_id,
            "request_identity": self.request_identity,
            "request_shape_digest": self.request_shape_digest,
            "request_started_at": _utc_datetime(
                self.request_started_at, "request_started_at"
            ).isoformat(),
            "response_completed_at": _utc_datetime(
                self.response_completed_at, "response_completed_at"
            ).isoformat(),
            "raw_response_digest": self.raw_response_digest,
            "datapoints": self.datapoints,
            "remaining_datapoints": self.remaining_datapoints,
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
    def computed_provider_event_evidence_digest(self) -> str:
        return _digest(self._payload_without_digest)

    def validate(self) -> None:
        _text(self.discovery_authorization_id, "discovery_authorization_id")
        _sha(self.discovery_authorization_digest, "discovery_authorization_digest")
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError("evidence provider is invalid")
        if self.league not in TOP5_LEAGUE_CODES:
            raise EventDiscoveryContractError("evidence league is invalid")
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("home_participant_id", self.home_participant_id),
            ("away_participant_id", self.away_participant_id),
            ("provider_event_id", self.provider_event_id),
            ("request_identity", self.request_identity),
            ("raw_response_digest", self.raw_response_digest),
        ):
            _text(value, name)
        _sha(self.request_shape_digest, "request_shape_digest")
        _sha(self.raw_response_digest, "raw_response_digest")
        _sha(self.provider_event_evidence_digest, "provider_event_evidence_digest")
        kickoff = _utc_datetime(self.kickoff, "kickoff")
        try:
            expected_fixture_key = make_fixture_key(
                self.league, self.home_team, self.away_team, kickoff
            )
        except Exception as exc:
            raise EventDiscoveryContractError(
                "evidence fixture scope is malformed"
            ) from exc
        if self.fixture_key != expected_fixture_key:
            raise EventDiscoveryContractError("evidence fixture key mismatch")
        started = _utc_datetime(self.request_started_at, "request_started_at")
        completed = _utc_datetime(self.response_completed_at, "response_completed_at")
        if completed < started:
            raise EventDiscoveryContractError("evidence timestamps are not ordered")
        if (
            self.retry_count != 0
            or self.datapoints < 0
            or self.remaining_datapoints < 0
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery billing evidence is invalid"
            )
        if self.datapoints > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
            raise EventDiscoveryExecutionBlocked(
                "discovery request datapoint cap exceeded"
            )
        if self.network_execution is not True and self.qualification_eligible:
            raise EventDiscoveryExecutionBlocked("offline discovery cannot qualify")
        for name, value in (
            ("qualification_eligible", self.qualification_eligible),
            ("receipt_eligible", self.receipt_eligible),
            ("provider_authority", self.provider_authority),
            ("activation_authorized", self.activation_authorized),
            ("publication_authorized", self.publication_authorized),
            ("ledger_mutated", self.ledger_mutated),
            ("monetary_spend_authorized", self.monetary_spend_authorized),
        ):
            if value is not False:
                raise EventDiscoveryExecutionBlocked(
                    f"discovery evidence is authorizing: {name}"
                )
        if self.home_participant_id == self.away_participant_id:
            raise EventDiscoveryContractError(
                "evidence participant IDs must be distinct"
            )
        if (
            self.computed_provider_event_evidence_digest
            != self.provider_event_evidence_digest
        ):
            raise EventDiscoveryContractError("provider event evidence digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = dict(self._payload_without_digest)
        payload["provider_event_evidence_digest"] = self.provider_event_evidence_digest
        return payload


def discovery_request_shape_digest(
    targets: Sequence[TheRundownEventDiscoveryTargetV1],
) -> str:
    if tuple(target.league for target in targets) != DISCOVERY_LEAGUE_ORDER:
        raise EventDiscoveryContractError(
            "request shape requires canonical five-league order"
        )
    for target in targets:
        target.validate()
    return _digest([_provider_shape(target) for target in targets])


def _billing(response: TheRundownEventDiscoveryResponseV1) -> tuple[int, int]:
    lowered = {
        str(key).casefold(): str(value).strip()
        for key, value in response.headers.items()
    }

    def integer(name: str) -> int:
        try:
            value = int(lowered[name])
        except (KeyError, ValueError) as exc:
            raise EventDiscoveryExecutionBlocked(
                f"missing or invalid billing header: {name}"
            ) from exc
        if value < 0:
            raise EventDiscoveryExecutionBlocked(f"negative billing header: {name}")
        return value

    datapoints = integer("x-datapoints")
    used = integer("x-datapoints-used")
    remaining = integer("x-datapoints-remaining")
    limit = integer("x-datapoints-limit")
    if used + remaining != limit or datapoints > used:
        raise EventDiscoveryExecutionBlocked(
            "discovery quota counters do not reconcile"
        )
    if datapoints > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
        raise EventDiscoveryExecutionBlocked("discovery request datapoint cap exceeded")
    return datapoints, remaining


def _validated_datapoint_total(current: int, datapoints: int) -> int:
    """Apply both per-request and cumulative discovery billing caps."""

    if datapoints < 0:
        raise EventDiscoveryExecutionBlocked("discovery datapoints cannot be negative")
    if datapoints > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
        raise EventDiscoveryExecutionBlocked("discovery request datapoint cap exceeded")
    total = current + datapoints
    if total > TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET:
        raise EventDiscoveryExecutionBlocked(
            "discovery cumulative datapoint budget exceeded"
        )
    return total


def _resolve_event_id(
    target: TheRundownEventDiscoveryTargetV1,
    payload: Mapping[str, object],
    *,
    response_at: datetime,
) -> str:
    events = payload.get("events")
    if not isinstance(events, list):
        raise EventDiscoveryExecutionBlocked("discovery payload has no events list")
    fixture = Fixture(
        target.fixture_key,
        target.league,
        target.home_team,
        target.away_team,
        target.kickoff,
    )
    adapter = TheRundownExperimentalAdapter()
    timing = CascadeTimingPolicy(
        maximum_odds_age_seconds=300,
        kickoff_tolerance_seconds=DISCOVERY_KICKOFF_TOLERANCE_SECONDS,
    )
    event_ids: list[str] = []
    matches: list[Mapping[str, object]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_id = str(event.get("event_id", "")).strip()
        if event_id:
            event_ids.append(event_id)
        if adapter.event_identity(event, fixture, timing_policy=timing) == "match":
            matches.append(event)
    if len(event_ids) != len(set(event_ids)):
        raise EventDiscoveryExecutionBlocked(
            "discovery response has duplicate provider event IDs"
        )
    if len(matches) == 0:
        raise EventDiscoveryExecutionBlocked(
            "discovery response has zero matching events"
        )
    if len(matches) != 1:
        raise EventDiscoveryExecutionBlocked(
            "discovery response has multiple matching events"
        )
    resolved = str(matches[0].get("event_id", "")).strip()
    if not resolved:
        raise EventDiscoveryExecutionBlocked(
            "discovery match has malformed provider event ID"
        )
    teams = matches[0].get("teams")
    if (
        not isinstance(teams, list)
        or len(teams) != 2
        or any(not isinstance(team, Mapping) for team in teams)
    ):
        raise EventDiscoveryExecutionBlocked(
            "discovery match has malformed participants"
        )
    typed_teams = tuple(team for team in teams if isinstance(team, Mapping))
    home_marked = tuple(team for team in typed_teams if team.get("is_home") is True)
    away_marked = tuple(team for team in typed_teams if team.get("is_away") is True)
    if home_marked or away_marked:
        if len(home_marked) != 1 or len(away_marked) != 1:
            raise EventDiscoveryExecutionBlocked(
                "discovery participant markers are ambiguous"
            )
        home_team = home_marked[0]
        away_team = away_marked[0]
    else:
        # TheRundown's documented unmarked order is [away, home].
        away_team, home_team = typed_teams
    if (
        str(home_team.get("team_id", "")).strip() != target.home_participant_id
        or str(away_team.get("team_id", "")).strip() != target.away_participant_id
    ):
        raise EventDiscoveryExecutionBlocked("discovery participant identity mismatch")
    if target.kickoff <= response_at:
        raise EventDiscoveryExecutionBlocked(
            "discovery fixture is stale or already started"
        )
    return resolved


def discover_five_league_events(
    authorization: TheRundownEventDiscoveryAuthorizationV1,
    *,
    transport: TheRundownEventDiscoveryTransport,
    now: datetime,
    pacer: Callable[[float], None] | None = None,
) -> tuple[TheRundownEventDiscoveryEvidenceV1, ...]:
    """Resolve exactly five IDs; any failure emits no partial artifact set."""

    now = _utc_datetime(now, "discovery now")
    authorization.validate(now=now)
    wait = pacer or (lambda _seconds: None)
    artifacts: list[TheRundownEventDiscoveryEvidenceV1] = []
    datapoints_total = 0
    for index, target in enumerate(authorization.targets):
        if index:
            wait(authorization.minimum_interval_seconds)
        request = TheRundownEventDiscoveryRequestV1(authorization, target, index)
        request.validate()
        try:
            response = transport.execute(request)
        except Exception as exc:
            raise EventDiscoveryExecutionBlocked(
                f"{target.league}: discovery transport failure"
            ) from exc
        response.validate()
        if response.network_execution is not True:
            # Offline fake transport is permitted for tests, but its evidence
            # remains explicitly non-authorizing and cannot be mistaken for a run.
            network_execution = False
        else:
            network_execution = True
        if _utc_datetime(response.finished_at, "response finished_at") > now:
            raise EventDiscoveryExecutionBlocked(
                "discovery response is from the future"
            )
        datapoints, remaining = _billing(response)
        datapoints_total = _validated_datapoint_total(datapoints_total, datapoints)
        event_id = _resolve_event_id(
            target, response.payload, response_at=response.finished_at
        )
        raw_digest = digest_record(response.payload)
        evidence_payload = {
            "schema_version": DISCOVERY_EVIDENCE_SCHEMA_VERSION,
            "discovery_authorization_id": authorization.discovery_authorization_id,
            "discovery_authorization_digest": authorization.authorization_digest,
            "provider": target.provider,
            "league": target.league,
            "fixture_key": target.fixture_key,
            "home_team": target.home_team,
            "away_team": target.away_team,
            "home_participant_id": target.home_participant_id,
            "away_participant_id": target.away_participant_id,
            "kickoff": target.kickoff,
            "provider_event_id": event_id,
            "request_identity": target.request_identity,
            "request_shape_digest": authorization.request_shape_digest,
            "request_started_at": response.started_at,
            "response_completed_at": response.finished_at,
            "raw_response_digest": raw_digest,
            "datapoints": datapoints,
            "remaining_datapoints": remaining,
            "retry_count": 0,
            "network_execution": network_execution,
            "qualification_eligible": False,
            "receipt_eligible": False,
            "provider_authority": False,
            "activation_authorized": False,
            "publication_authorized": False,
            "ledger_mutated": False,
            "monetary_spend_authorized": False,
        }
        evidence = TheRundownEventDiscoveryEvidenceV1(
            discovery_authorization_id=authorization.discovery_authorization_id,
            discovery_authorization_digest=authorization.authorization_digest,
            provider=target.provider,
            league=target.league,
            fixture_key=target.fixture_key,
            home_team=target.home_team,
            away_team=target.away_team,
            home_participant_id=target.home_participant_id,
            away_participant_id=target.away_participant_id,
            kickoff=target.kickoff,
            provider_event_id=event_id,
            request_identity=target.request_identity,
            request_shape_digest=authorization.request_shape_digest,
            request_started_at=response.started_at,
            response_completed_at=response.finished_at,
            raw_response_digest=raw_digest,
            provider_event_evidence_digest=_digest(evidence_payload),
            datapoints=datapoints,
            remaining_datapoints=remaining,
            network_execution=network_execution,
        )
        evidence.validate()
        artifacts.append(evidence)
    if len(artifacts) != TOP5_CONTROLLED_SHADOW_REQUEST_COUNT:
        raise EventDiscoveryExecutionBlocked("discovery did not resolve five events")
    return tuple(artifacts)


def materialize_prebound_network_configuration(
    authorization: TheRundownEventDiscoveryAuthorizationV1,
    evidence: Sequence[TheRundownEventDiscoveryEvidenceV1],
    *,
    enabled: bool = False,
) -> TheRundownNetworkConfigurationV1:
    """Convert all five non-authorizing artifacts into strict run config."""

    if enabled is not False:
        raise EventDiscoveryExecutionBlocked(
            "discovery cannot enable controlled network execution"
        )
    authorization.validate(now=authorization.issued_at)
    if len(evidence) != len(DISCOVERY_LEAGUE_ORDER):
        raise EventDiscoveryContractError(
            "exactly five discovery artifacts are required"
        )
    if len({item.league for item in evidence}) != len(DISCOVERY_LEAGUE_ORDER):
        raise EventDiscoveryContractError(
            "discovery artifacts contain duplicate leagues"
        )
    by_league = {item.league: item for item in evidence}
    if tuple(by_league) != DISCOVERY_LEAGUE_ORDER:
        raise EventDiscoveryContractError(
            "discovery artifacts are incomplete or unordered"
        )
    targets: list[TheRundownCanaryTargetV1] = []
    for target in authorization.targets:
        item = by_league[target.league]
        item.validate()
        if item.discovery_authorization_id != authorization.discovery_authorization_id:
            raise EventDiscoveryContractError(
                "discovery authorization binding mismatch"
            )
        if item.discovery_authorization_digest != authorization.authorization_digest:
            raise EventDiscoveryContractError("discovery authorization digest mismatch")
        if (
            item.fixture_key != target.fixture_key
            or item.request_identity != target.request_identity
            or item.provider != target.provider
            or item.home_team != target.home_team
            or item.away_team != target.away_team
            or item.home_participant_id != target.home_participant_id
            or item.away_participant_id != target.away_participant_id
            or _utc_datetime(item.kickoff, "evidence kickoff")
            != _utc_datetime(target.kickoff, "target kickoff")
        ):
            raise EventDiscoveryContractError(
                "discovery fixture/request binding mismatch"
            )
        if item.provider_event_id in {
            candidate.provider_event_id
            for candidate in evidence
            if candidate is not item
        }:
            raise EventDiscoveryContractError(
                "discovery provider event IDs must be unique"
            )
        targets.append(
            TheRundownCanaryTargetV1(
                provider=target.provider,
                league=target.league,
                fixture_key=target.fixture_key,
                provider_event_id=item.provider_event_id,
                home_team=target.home_team,
                away_team=target.away_team,
                kickoff=target.kickoff,
            )
        )
    participants = tuple(
        TheRundownNetworkParticipantScopeV1(
            fixture_key=target.fixture_key,
            home_participant_id=target.home_participant_id,
            away_participant_id=target.away_participant_id,
        )
        for target in authorization.targets
    )
    requests = tuple(
        TheRundownNetworkRequestScopeV1(
            fixture_key=target.fixture_key,
            request_identity=target.request_identity,
        )
        for target in authorization.targets
    )
    draft = TheRundownNetworkConfigurationV1(
        targets=tuple(targets),
        participant_scope=participants,
        request_scope=requests,
        adapter_version=authorization.adapter_version,
        adapter_source_sha=authorization.adapter_source_sha,
        maximum_request_count=TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
        maximum_datapoints=TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
        maximum_quota_cost_units=float(TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET),
        request_quota_cost_units=float(THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST),
        maximum_source_age_seconds=300,
        minimum_interval_seconds=authorization.minimum_interval_seconds,
        maximum_retries=0,
        enabled=enabled,
        no_bet=True,
        publication=False,
        production_activation=False,
        monetary_spend_authorized=False,
        configuration_digest="0" * 64,
    )
    configuration = replace(
        draft, configuration_digest=draft.computed_configuration_digest
    )
    configuration.validate()
    return configuration


__all__ = [
    "DISCOVERY_AUTHORIZATION_SCHEMA_VERSION",
    "DISCOVERY_EVIDENCE_SCHEMA_VERSION",
    "DISCOVERY_SCHEMA_VERSION",
    "EventDiscoveryContractError",
    "EventDiscoveryExecutionBlocked",
    "TheRundownEventDiscoveryAuthorizationV1",
    "TheRundownEventDiscoveryEvidenceV1",
    "TheRundownEventDiscoveryRequestV1",
    "TheRundownEventDiscoveryResponseV1",
    "TheRundownEventDiscoveryTargetV1",
    "TheRundownEventDiscoveryTransport",
    "discover_five_league_events",
    "discovery_request_shape_digest",
    "materialize_prebound_network_configuration",
]
