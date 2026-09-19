"""Offline Champions League provider-cascade shadow diagnostics.

The normal provider-cascade contracts are intentionally strict: malformed or
partial observations stop at the quality boundary.  This module is the
diagnostic boundary that runs before that decision.  It accepts source-shaped
local replay records, compares them with a canonical event, and retains the
reason a source would be rejected.

The module is deliberately pure.  It does not import a transport, read a
cache, write an artifact, select a provider, or use the wall clock.  Callers
must provide the replay ``as_of`` time, which makes the report deterministic
and suitable for controlled-shadow engineering work only.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from types import MappingProxyType

CHAMPIONS_LEAGUE_CODE = "ucl"
CHAMPIONS_LEAGUE_SCHEMA_VERSION = "champions-league-provider-shadow-v1"
DEFAULT_REPLAY_AS_OF = datetime(2026, 9, 19, 18, tzinfo=timezone.utc)
DEFAULT_MAX_FRESHNESS_SECONDS = 900
DEFAULT_REQUIRED_BOOKMAKERS: tuple[str, ...] = ()
_OUTCOMES = ("home", "draw", "away")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_LEAGUE_ALIASES = frozenset(
    {
        "ucl",
        "champions league",
        "champions_league",
        "uefa champions league",
        "uefa_champs_league",
        "soccer uefa champs league",
        "soccer_uefa_champs_league",
    }
)


class ShadowDiagnosticError(ValueError):
    """Raised for an invalid diagnostic policy or canonical replay fixture."""


class DiagnosticCode(str, Enum):
    """Bounded issue vocabulary emitted by the offline diagnostic."""

    MALFORMED_INPUT = "malformed_input"
    SOURCE_LIMIT_EXCEEDED = "source_limit_exceeded"
    EVENT_ID_MISSING = "event_id_missing"
    EVENT_ID_MISMATCH = "event_id_mismatch"
    FIXTURE_KEY_MISSING = "fixture_key_missing"
    FIXTURE_KEY_MISMATCH = "fixture_key_mismatch"
    COMPETITION_MISMATCH = "competition_mismatch"
    PARTICIPANT_MAPPING_MISSING = "participant_mapping_missing"
    PARTICIPANT_MAPPING_PARTIAL = "participant_mapping_partial"
    PARTICIPANT_MAPPING_MISMATCH = "participant_mapping_mismatch"
    KICKOFF_MISSING = "kickoff_missing"
    KICKOFF_INVALID = "kickoff_invalid"
    KICKOFF_MISMATCH = "kickoff_mismatch"
    REGULATION_1X2_MISSING = "regulation_1x2_missing"
    REGULATION_1X2_INCOMPLETE = "regulation_1x2_incomplete"
    REGULATION_1X2_INVALID = "regulation_1x2_invalid"
    BOOKMAKER_MISSING = "bookmaker_missing"
    BOOKMAKER_COVERAGE_GAP = "bookmaker_coverage_gap"
    SOURCE_TIMESTAMP_MISSING = "source_timestamp_missing"
    CAPTURE_TIMESTAMP_MISSING = "capture_timestamp_missing"
    TIMESTAMP_INVALID = "timestamp_invalid"
    TIMESTAMP_ORDER_INVALID = "timestamp_order_invalid"
    TIMESTAMP_AFTER_REPLAY = "timestamp_after_replay"
    POST_KICKOFF_ODDS = "post_kickoff_odds"
    FRESHNESS_MISSING = "freshness_missing"
    STALE_SOURCE = "stale_source"
    PROVENANCE_MISSING = "provenance_missing"
    PROVENANCE_INVALID = "provenance_invalid"
    SOURCE_DISAGREEMENT = "source_disagreement"


class QualityStatus(str, Enum):
    MATCH = "MATCH"
    PARTIAL = "PARTIAL"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    INVALID = "INVALID"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_text(value: object) -> str:
    return " ".join(_text(value).casefold().split())


def _canonical_competition(value: object) -> str:
    return _normalized_text(value).replace("-", "_")


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _field(row: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _participant(row: Mapping[str, object], side: str) -> tuple[str, str]:
    participant_group = row.get("participants")
    grouped = participant_group.get(side) if isinstance(participant_group, Mapping) else None
    nested = _field(row, f"{side}_participant", side) or grouped
    nested_map = nested if isinstance(nested, Mapping) else {}
    participant_id = _field(
        row,
        f"{side}_participant_id",
        f"{side}_team_id",
        f"{side}_id",
        f"{side}_entity_id",
    )
    name = _field(
        row,
        f"{side}_participant_name",
        f"{side}_team_name",
        f"{side}_name",
    )
    if not participant_id:
        participant_id = _field(nested_map, "id", "participant_id", "team_id")
    if not name:
        name = _field(nested_map, "name", "participant_name", "team_name")
    return _text(participant_id), _text(name)


def _odds_mapping(row: Mapping[str, object]) -> Mapping[str, object] | None:
    value = _field(
        row,
        "regulation_1x2",
        "h2h_1x2",
        "h2h",
        "odds",
        "market",
    )
    if isinstance(value, Mapping):
        nested = value.get("odds")
        if isinstance(nested, Mapping):
            return nested
        for market_name in ("1x2", "1X2", "regulation_1x2", "h2h"):
            nested = value.get(market_name)
            if isinstance(nested, Mapping):
                return nested
        return value
    return None


def _bookmakers(row: Mapping[str, object]) -> tuple[str, ...]:
    value = _field(
        row,
        "bookmakers",
        "bookmaker",
        "bookmaker_identity",
        "bookmaker_key",
        "sportsbook",
        "bookmaker_coverage",
    )
    values: list[str] = []
    if isinstance(value, Mapping):
        named_value = _field(value, "name", "key", "title")
        if named_value is not None:
            value = named_value
        else:
            values.extend(str(key) for key in value)
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = []
        for item in value:
            if isinstance(item, Mapping):
                item_value = _field(item, "name", "key", "title", "bookmaker")
            else:
                item_value = item
            if _text(item_value):
                values.append(_text(item_value))
    return tuple(sorted({_normalized_text(item) for item in values if _text(item)}))


def _provenance(row: Mapping[str, object]) -> Mapping[str, object]:
    nested = row.get("provenance")
    result = dict(nested) if isinstance(nested, Mapping) else {}
    aliases = {
        "source": ("source", "provider", "source_identity"),
        "adapter_version": ("adapter_version", "adapter", "version"),
        "request_identity": ("request_identity", "request_id", "request"),
        "raw_payload_digest": (
            "raw_payload_digest",
            "raw_response_digest",
            "raw_record_digest",
            "payload_digest",
        ),
        "source_provenance": ("source_provenance", "provenance_identity"),
    }
    for target, names in aliases.items():
        if target not in result:
            value = _field(row, *names)
            if value is not None:
                result[target] = value
    return MappingProxyType(result)


def _canonical_participant_key(participant_id: str, name: str) -> str:
    if participant_id:
        return f"id:{_normalized_text(participant_id)}"
    if name:
        return f"name:{_normalized_text(name)}"
    return ""


def _outcome_value(odds: Mapping[str, object], outcome: str) -> object | None:
    aliases = {
        "home": ("home", "home_odds", "1"),
        "draw": ("draw", "draw_odds", "x"),
        "away": ("away", "away_odds", "2"),
    }
    return _field(odds, *aliases[outcome])


def _finite_decimal(value: object) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(float(value)) and float(value) > 1.0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CanonicalEvent:
    """Expected UCL event identity used for source comparison."""

    fixture_key: str
    event_id: str
    home_participant_id: str
    home_name: str
    away_participant_id: str
    away_name: str
    kickoff: datetime
    competition: str = CHAMPIONS_LEAGUE_CODE

    def __post_init__(self) -> None:
        kickoff = _parse_datetime(self.kickoff)
        if kickoff is None:
            raise ShadowDiagnosticError("canonical kickoff must be timezone-aware")
        object.__setattr__(self, "kickoff", kickoff)

    def validate(self) -> None:
        required = (
            self.fixture_key,
            self.event_id,
            self.home_participant_id,
            self.home_name,
            self.away_participant_id,
            self.away_name,
        )
        if any(not _text(value) for value in required):
            raise ShadowDiagnosticError("canonical event identity is incomplete")
        if _canonical_competition(self.competition) not in _LEAGUE_ALIASES:
            raise ShadowDiagnosticError("canonical event is outside Champions League")
        if _canonical_participant_key(self.home_participant_id, self.home_name) == _canonical_participant_key(
            self.away_participant_id, self.away_name
        ):
            raise ShadowDiagnosticError("canonical participants must be distinct")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "fixture_key": self.fixture_key,
            "event_id": self.event_id,
            "competition": self.competition,
            "home_participant_id": self.home_participant_id,
            "home_name": self.home_name,
            "away_participant_id": self.away_participant_id,
            "away_name": self.away_name,
            "kickoff": self.kickoff.isoformat(),
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> CanonicalEvent:
        if not isinstance(row, Mapping):
            raise ShadowDiagnosticError("canonical event must be a mapping")
        home_id, home_name = _participant(row, "home")
        away_id, away_name = _participant(row, "away")
        kickoff = _parse_datetime(_field(row, "kickoff", "kickoff_utc", "commence_time"))
        if kickoff is None:
            raise ShadowDiagnosticError("canonical kickoff is missing or invalid")
        event = cls(
            fixture_key=_text(_field(row, "fixture_key", "match_key", "canonical_fixture_key")),
            event_id=_text(_field(row, "event_id", "canonical_event_id", "match_id")),
            home_participant_id=home_id,
            home_name=home_name,
            away_participant_id=away_id,
            away_name=away_name,
            kickoff=kickoff,
            competition=_text(_field(row, "competition", "league", "league_code"))
            or CHAMPIONS_LEAGUE_CODE,
        )
        event.validate()
        return event


@dataclass(frozen=True)
class SourceFixture:
    """One source-shaped raw fixture; fields remain nullable for diagnostics."""

    source: str
    fixture_key: str = ""
    source_event_id: str = ""
    competition: str = ""
    home_participant_id: str = ""
    home_name: str = ""
    away_participant_id: str = ""
    away_name: str = ""
    kickoff: object | None = None
    regulation_1x2: Mapping[str, object] | None = None
    bookmakers: tuple[str, ...] = ()
    source_timestamp: object | None = None
    captured_at: object | None = None
    adapter_version: str = ""
    request_identity: str = ""
    raw_payload_digest: str = ""
    source_provenance: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def as_payload(self) -> dict[str, object]:
        def timestamp(value: object | None) -> str | None:
            parsed = _parse_datetime(value)
            return parsed.isoformat() if parsed else (_text(value) or None)

        return {
            "source": self.source,
            "fixture_key": self.fixture_key,
            "source_event_id": self.source_event_id,
            "competition": self.competition,
            "home_participant_id": self.home_participant_id,
            "home_name": self.home_name,
            "away_participant_id": self.away_participant_id,
            "away_name": self.away_name,
            "kickoff": timestamp(self.kickoff),
            "regulation_1x2": dict(self.regulation_1x2 or {}),
            "bookmakers": list(self.bookmakers),
            "source_timestamp": timestamp(self.source_timestamp),
            "captured_at": timestamp(self.captured_at),
            "adapter_version": self.adapter_version,
            "request_identity": self.request_identity,
            "raw_payload_digest": self.raw_payload_digest,
            "source_provenance": self.source_provenance,
        }

    @classmethod
    def from_mapping(
        cls, row: Mapping[str, object], *, source: str | None = None
    ) -> SourceFixture:
        if not isinstance(row, Mapping):
            raise ShadowDiagnosticError("source fixture must be a mapping")
        home_id, home_name = _participant(row, "home")
        away_id, away_name = _participant(row, "away")
        provenance = _provenance(row)
        return cls(
            source=_text(source or _field(row, "source", "provider", "source_identity")),
            fixture_key=_text(_field(row, "fixture_key", "match_key", "canonical_fixture_key")),
            source_event_id=_text(
                _field(row, "source_event_id", "provider_event_id", "event_id", "match_id")
            ),
            competition=_text(_field(row, "competition", "league", "league_code")),
            home_participant_id=home_id,
            home_name=home_name,
            away_participant_id=away_id,
            away_name=away_name,
            kickoff=_field(row, "kickoff", "kickoff_utc", "commence_time", "scheduled_at"),
            regulation_1x2=_odds_mapping(row),
            bookmakers=_bookmakers(row),
            source_timestamp=_field(
                row, "source_timestamp", "last_update", "odds_timestamp", "updated_at"
            ),
            captured_at=_field(row, "captured_at", "observed_at", "retrieved_at"),
            adapter_version=_text(provenance.get("adapter_version")),
            request_identity=_text(provenance.get("request_identity")),
            raw_payload_digest=_text(provenance.get("raw_payload_digest")),
            source_provenance=_text(provenance.get("source_provenance")),
            metadata=MappingProxyType(dict(row)),
        )


@dataclass(frozen=True)
class DiagnosticPolicy:
    """Explicit bounds for one deterministic replay comparison."""

    as_of: datetime
    max_freshness_seconds: int = DEFAULT_MAX_FRESHNESS_SECONDS
    kickoff_tolerance_seconds: int = 60
    required_bookmakers: tuple[str, ...] = DEFAULT_REQUIRED_BOOKMAKERS
    max_events: int = 100
    max_sources_per_event: int = 8
    max_issue_samples: int = 40

    def __post_init__(self) -> None:
        parsed = _parse_datetime(self.as_of)
        if parsed is None:
            raise ShadowDiagnosticError("policy as_of must be timezone-aware")
        object.__setattr__(self, "as_of", parsed)
        if (
            not isinstance(self.max_freshness_seconds, int)
            or isinstance(self.max_freshness_seconds, bool)
            or self.max_freshness_seconds <= 0
            or not isinstance(self.kickoff_tolerance_seconds, int)
            or isinstance(self.kickoff_tolerance_seconds, bool)
            or self.kickoff_tolerance_seconds < 0
            or not isinstance(self.max_events, int)
            or isinstance(self.max_events, bool)
            or self.max_events <= 0
            or not isinstance(self.max_sources_per_event, int)
            or isinstance(self.max_sources_per_event, bool)
            or self.max_sources_per_event <= 0
            or not isinstance(self.max_issue_samples, int)
            or isinstance(self.max_issue_samples, bool)
            or self.max_issue_samples <= 0
        ):
            raise ShadowDiagnosticError("diagnostic policy bounds are invalid")
        bookmakers = tuple(
            sorted({_normalized_text(value) for value in self.required_bookmakers if _text(value)})
        )
        object.__setattr__(self, "required_bookmakers", bookmakers)

    def as_payload(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "max_freshness_seconds": self.max_freshness_seconds,
            "kickoff_tolerance_seconds": self.kickoff_tolerance_seconds,
            "required_bookmakers": list(self.required_bookmakers),
            "max_events": self.max_events,
            "max_sources_per_event": self.max_sources_per_event,
            "max_issue_samples": self.max_issue_samples,
        }


@dataclass(frozen=True)
class SourceQuality:
    """Quality result for one source/event pair."""

    source: str
    fixture_key: str
    source_event_id: str
    event_identity: QualityStatus
    participant_mapping: QualityStatus
    regulation_1x2_complete: bool
    bookmakers: tuple[str, ...]
    required_bookmakers: tuple[str, ...]
    missing_bookmakers: tuple[str, ...]
    bookmaker_coverage_ratio: float
    kickoff: datetime | None
    source_timestamp: datetime | None
    captured_at: datetime | None
    source_age_seconds: int | None
    timestamp_status: QualityStatus
    fresh: bool
    provenance_complete: bool
    issues: tuple[DiagnosticCode, ...]
    eligible: bool

    @property
    def regulation_1x2_completeness(self) -> str:
        return "complete" if self.regulation_1x2_complete else "incomplete"

    @property
    def freshness_status(self) -> str:
        if self.source_age_seconds is None:
            return "unknown"
        return "fresh" if self.fresh else "stale"

    def as_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "fixture_key": self.fixture_key,
            "source_event_id": self.source_event_id,
            "event_identity": self.event_identity.value,
            "participant_mapping": self.participant_mapping.value,
            "regulation_1x2_complete": self.regulation_1x2_complete,
            "regulation_1x2_completeness": self.regulation_1x2_completeness,
            "bookmakers": list(self.bookmakers),
            "required_bookmakers": list(self.required_bookmakers),
            "missing_bookmakers": list(self.missing_bookmakers),
            "bookmaker_coverage_ratio": self.bookmaker_coverage_ratio,
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "source_age_seconds": self.source_age_seconds,
            "timestamp_status": self.timestamp_status.value,
            "fresh": self.fresh,
            "freshness_status": self.freshness_status,
            "provenance_complete": self.provenance_complete,
            "issues": [issue.value for issue in self.issues],
            "eligible": self.eligible,
        }


@dataclass(frozen=True)
class SourceDisagreement:
    """One field whose normalized value differs across local sources."""

    fixture_key: str
    field: str
    sources: tuple[str, ...]
    values: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "fixture_key": self.fixture_key,
            "field": self.field,
            "sources": list(self.sources),
            "values": list(self.values),
        }


@dataclass(frozen=True)
class ShadowDiagnosticReport:
    """Non-authoritative, bounded report for one offline shadow comparison."""

    as_of: datetime
    policy: DiagnosticPolicy
    event_count: int
    source_input_count: int
    source_evaluated_count: int
    source_quality: tuple[SourceQuality, ...]
    disagreements: tuple[SourceDisagreement, ...]
    issue_counts: Mapping[str, int]
    issue_samples: tuple[dict[str, object], ...]
    truncated: bool

    @property
    def quality_issue_count(self) -> int:
        return sum(self.issue_counts.values())

    @property
    def eligible_source_count(self) -> int:
        return sum(item.eligible for item in self.source_quality)

    @property
    def status(self) -> str:
        if not self.source_quality:
            return "blocked"
        if self.quality_issue_count or self.disagreements:
            return "diagnostic_findings"
        return "ready"

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": CHAMPIONS_LEAGUE_SCHEMA_VERSION,
            "league": CHAMPIONS_LEAGUE_CODE,
            "status": self.status,
            "as_of": self.as_of.isoformat(),
            "event_count": self.event_count,
            "source_input_count": self.source_input_count,
            "source_evaluated_count": self.source_evaluated_count,
            "eligible_source_count": self.eligible_source_count,
            "source_quality": [item.as_payload() for item in self.source_quality],
            "metrics": [item.as_payload() for item in self.source_quality],
            "disagreements": [item.as_payload() for item in self.disagreements],
            "source_disagreements": [item.as_payload() for item in self.disagreements],
            "disagreement_count": len(self.disagreements),
            "issue_counts": dict(self.issue_counts),
            "issue_samples": list(self.issue_samples),
            "truncated": self.truncated,
            "offline_replay": True,
            "source_kind": "OFFLINE_REPLAY",
            "controlled_shadow": True,
            "network_called": False,
            "routing_authority": "none",
            "selected_source": None,
            "counts_as_real": False,
            "no_bet": True,
            "publication_enabled": False,
            "activation_state": "disabled",
        }


def _issue(
    issues: list[tuple[DiagnosticCode, str]],
    code: DiagnosticCode,
    detail: str,
) -> None:
    issues.append((code, detail))


def _participant_status(
    expected: tuple[str, str], observed: tuple[str, str]
) -> QualityStatus:
    expected_id, expected_name = expected
    observed_id, observed_name = observed
    if not observed_id and not observed_name:
        return QualityStatus.MISSING
    if observed_id and expected_id:
        # A stable participant ID is authoritative across source-specific name
        # aliases (for example, a source appending "FC" to a club name).
        return (
            QualityStatus.MATCH
            if _normalized_text(observed_id) == _normalized_text(expected_id)
            else QualityStatus.MISMATCH
        )
    if observed_name and expected_name:
        return (
            QualityStatus.MATCH
            if _normalized_text(observed_name) == _normalized_text(expected_name)
            else QualityStatus.MISMATCH
        )
    return QualityStatus.PARTIAL


def _source_quality(
    event: CanonicalEvent,
    source_fixture: SourceFixture,
    policy: DiagnosticPolicy,
) -> tuple[SourceQuality, list[tuple[DiagnosticCode, str]]]:
    issues: list[tuple[DiagnosticCode, str]] = []
    source = _text(source_fixture.source) or "<unknown>"
    fixture_key = _text(source_fixture.fixture_key)
    source_event_id = _text(source_fixture.source_event_id)
    event_identity = QualityStatus.MATCH
    if not fixture_key:
        event_identity = QualityStatus.MISSING
        _issue(issues, DiagnosticCode.FIXTURE_KEY_MISSING, "fixture key is missing")
    elif fixture_key != event.fixture_key:
        event_identity = QualityStatus.MISMATCH
        _issue(issues, DiagnosticCode.FIXTURE_KEY_MISMATCH, "fixture key differs from canonical event")
    if not source_event_id:
        _issue(issues, DiagnosticCode.EVENT_ID_MISSING, "source event id is missing")

    competition = _canonical_competition(source_fixture.competition)
    if competition and competition not in _LEAGUE_ALIASES:
        event_identity = QualityStatus.MISMATCH
        _issue(issues, DiagnosticCode.COMPETITION_MISMATCH, "source is outside Champions League")
    elif not competition:
        _issue(issues, DiagnosticCode.COMPETITION_MISMATCH, "source competition is missing")

    participants = (
        _participant_status(
            (event.home_participant_id, event.home_name),
            (source_fixture.home_participant_id, source_fixture.home_name),
        ),
        _participant_status(
            (event.away_participant_id, event.away_name),
            (source_fixture.away_participant_id, source_fixture.away_name),
        ),
    )
    if any(status is QualityStatus.MISSING for status in participants):
        participant_mapping = QualityStatus.MISSING
        _issue(issues, DiagnosticCode.PARTICIPANT_MAPPING_MISSING, "participant mapping is missing")
    elif any(status is QualityStatus.MISMATCH for status in participants):
        participant_mapping = QualityStatus.MISMATCH
        _issue(issues, DiagnosticCode.PARTICIPANT_MAPPING_MISMATCH, "participant mapping differs from canonical event")
    elif any(status is QualityStatus.PARTIAL for status in participants):
        participant_mapping = QualityStatus.PARTIAL
        _issue(issues, DiagnosticCode.PARTICIPANT_MAPPING_PARTIAL, "participant mapping relies on incomplete identity")
    else:
        participant_mapping = QualityStatus.MATCH

    kickoff = _parse_datetime(source_fixture.kickoff)
    if source_fixture.kickoff is None or source_fixture.kickoff == "":
        kickoff_status = QualityStatus.MISSING
        _issue(issues, DiagnosticCode.KICKOFF_MISSING, "kickoff is missing")
    elif kickoff is None:
        kickoff_status = QualityStatus.INVALID
        _issue(issues, DiagnosticCode.KICKOFF_INVALID, "kickoff is invalid or timezone-naive")
    elif abs((kickoff - event.kickoff).total_seconds()) > policy.kickoff_tolerance_seconds:
        kickoff_status = QualityStatus.MISMATCH
        _issue(issues, DiagnosticCode.KICKOFF_MISMATCH, "kickoff differs from canonical event")
    else:
        kickoff_status = QualityStatus.MATCH

    odds = source_fixture.regulation_1x2
    regulation_complete = True
    if not odds:
        regulation_complete = False
        _issue(issues, DiagnosticCode.REGULATION_1X2_MISSING, "regulation 1X2 market is missing")
    else:
        for outcome in _OUTCOMES:
            value = _outcome_value(odds, outcome)
            if value is None:
                regulation_complete = False
                _issue(issues, DiagnosticCode.REGULATION_1X2_INCOMPLETE, f"{outcome} outcome is missing")
            elif not _finite_decimal(value):
                regulation_complete = False
                _issue(issues, DiagnosticCode.REGULATION_1X2_INVALID, f"{outcome} outcome is invalid")

    bookmakers = tuple(sorted({_normalized_text(value) for value in source_fixture.bookmakers if _text(value)}))
    if not bookmakers:
        _issue(issues, DiagnosticCode.BOOKMAKER_MISSING, "bookmaker coverage is missing")
    required = tuple(policy.required_bookmakers)
    missing_bookmakers = tuple(sorted(set(required) - set(bookmakers)))
    if missing_bookmakers:
        _issue(issues, DiagnosticCode.BOOKMAKER_COVERAGE_GAP, "required bookmaker coverage is incomplete")
    bookmaker_ratio = (
        len(set(required) - set(missing_bookmakers)) / len(required) if required else float(bool(bookmakers))
    )

    source_timestamp = _parse_datetime(source_fixture.source_timestamp)
    captured_at = _parse_datetime(source_fixture.captured_at)
    timestamp_status = QualityStatus.MATCH
    if source_fixture.source_timestamp is None or source_fixture.source_timestamp == "":
        timestamp_status = QualityStatus.MISSING
        _issue(issues, DiagnosticCode.SOURCE_TIMESTAMP_MISSING, "source timestamp is missing")
    elif source_timestamp is None:
        timestamp_status = QualityStatus.INVALID
        _issue(issues, DiagnosticCode.TIMESTAMP_INVALID, "source timestamp is invalid or timezone-naive")
    if source_fixture.captured_at is None or source_fixture.captured_at == "":
        timestamp_status = QualityStatus.MISSING
        _issue(issues, DiagnosticCode.CAPTURE_TIMESTAMP_MISSING, "capture timestamp is missing")
    elif captured_at is None:
        timestamp_status = QualityStatus.INVALID
        _issue(issues, DiagnosticCode.TIMESTAMP_INVALID, "capture timestamp is invalid or timezone-naive")
    if source_timestamp and captured_at and source_timestamp > captured_at:
        timestamp_status = QualityStatus.INVALID
        _issue(issues, DiagnosticCode.TIMESTAMP_ORDER_INVALID, "source timestamp is after capture timestamp")
    if source_timestamp and source_timestamp > policy.as_of:
        timestamp_status = QualityStatus.INVALID
        _issue(issues, DiagnosticCode.TIMESTAMP_AFTER_REPLAY, "source timestamp is after replay as_of")
    if captured_at and captured_at > policy.as_of:
        timestamp_status = QualityStatus.INVALID
        _issue(issues, DiagnosticCode.TIMESTAMP_AFTER_REPLAY, "capture timestamp is after replay as_of")
    if source_timestamp and kickoff and source_timestamp >= kickoff:
        timestamp_status = QualityStatus.MISMATCH
        _issue(issues, DiagnosticCode.POST_KICKOFF_ODDS, "odds timestamp is not pre-match")

    source_age: int | None = None
    fresh = False
    if source_timestamp is None:
        _issue(issues, DiagnosticCode.FRESHNESS_MISSING, "freshness cannot be measured without source timestamp")
    else:
        age = (policy.as_of - source_timestamp).total_seconds()
        if age >= 0:
            source_age = round(age)
            fresh = age <= policy.max_freshness_seconds
            if not fresh:
                _issue(issues, DiagnosticCode.STALE_SOURCE, "source timestamp exceeds freshness bound")

    provenance_values = (
        source,
        _text(source_fixture.adapter_version),
        _text(source_fixture.request_identity),
        _text(source_fixture.raw_payload_digest),
        _text(source_fixture.source_provenance),
    )
    provenance_complete = all(provenance_values)
    if not provenance_complete:
        _issue(issues, DiagnosticCode.PROVENANCE_MISSING, "source provenance is incomplete")
    elif not _DIGEST_RE.fullmatch(source_fixture.raw_payload_digest.lower()):
        provenance_complete = False
        _issue(issues, DiagnosticCode.PROVENANCE_INVALID, "raw payload digest is not a SHA-256 value")

    unique_issue_codes = tuple(sorted({code for code, _ in issues}, key=lambda code: code.value))
    eligible = not unique_issue_codes and event_identity is QualityStatus.MATCH and kickoff_status is QualityStatus.MATCH
    return (
        SourceQuality(
            source=source,
            fixture_key=fixture_key,
            source_event_id=source_event_id,
            event_identity=event_identity,
            participant_mapping=participant_mapping,
            regulation_1x2_complete=regulation_complete,
            bookmakers=bookmakers,
            required_bookmakers=required,
            missing_bookmakers=missing_bookmakers,
            bookmaker_coverage_ratio=bookmaker_ratio,
            kickoff=kickoff,
            source_timestamp=source_timestamp,
            captured_at=captured_at,
            source_age_seconds=source_age,
            timestamp_status=timestamp_status,
            fresh=fresh,
            provenance_complete=provenance_complete,
            issues=unique_issue_codes,
            eligible=eligible,
        ),
        issues,
    )


def _comparison_value(event: CanonicalEvent, item: SourceQuality, fixture: SourceFixture, field: str) -> str:
    if field == "fixture_key":
        return fixture.fixture_key
    if field == "participants":
        return "|".join(
            (
                _canonical_participant_key(fixture.home_participant_id, fixture.home_name),
                _canonical_participant_key(fixture.away_participant_id, fixture.away_name),
            )
        )
    if field == "kickoff":
        kickoff = _parse_datetime(fixture.kickoff)
        return kickoff.isoformat() if kickoff else "missing"
    if field == "regulation_1x2":
        odds = fixture.regulation_1x2 or {}
        return "|".join(str(_outcome_value(odds, outcome)) for outcome in _OUTCOMES)
    if field == "bookmaker_coverage":
        return ",".join(item.bookmakers)
    if field == "freshness":
        return "fresh" if item.fresh else "not_fresh"
    return ""


def _find_disagreements(
    event: CanonicalEvent,
    records: Sequence[tuple[SourceFixture, SourceQuality]],
    policy: DiagnosticPolicy,
) -> tuple[SourceDisagreement, ...]:
    if len(records) < 2:
        return ()
    disagreements: list[SourceDisagreement] = []
    for field_name in (
        "fixture_key",
        "participants",
        "kickoff",
        "regulation_1x2",
        "bookmaker_coverage",
        "freshness",
    ):
        values_by_source = [
            (quality.source, _comparison_value(event, quality, fixture, field_name))
            for fixture, quality in records
        ]
        normalized_values = {value for _, value in values_by_source}
        if field_name == "kickoff":
            timestamps = [_parse_datetime(fixture.kickoff) for fixture, _ in records]
            valid = [timestamp for timestamp in timestamps if timestamp is not None]
            differs = bool(valid) and max(valid) - min(valid) > timedelta(seconds=policy.kickoff_tolerance_seconds)
        else:
            differs = len(normalized_values) > 1
        if differs:
            disagreements.append(
                SourceDisagreement(
                    fixture_key=event.fixture_key,
                    field=field_name,
                    sources=tuple(source for source, _ in values_by_source),
                    values=tuple(value for _, value in values_by_source),
                )
            )
    return tuple(disagreements)


def _coerce_event(value: CanonicalEvent | Mapping[str, object]) -> CanonicalEvent:
    return value if isinstance(value, CanonicalEvent) else CanonicalEvent.from_mapping(value)


def _coerce_source(value: SourceFixture | Mapping[str, object]) -> SourceFixture:
    return value if isinstance(value, SourceFixture) else SourceFixture.from_mapping(value)


def compare_champions_league_sources(
    events: Sequence[CanonicalEvent | Mapping[str, object]],
    source_fixtures: Sequence[SourceFixture | Mapping[str, object]],
    *,
    policy: DiagnosticPolicy,
) -> ShadowDiagnosticReport:
    """Compare source-shaped local fixtures without provider or runtime I/O."""

    if not isinstance(policy, DiagnosticPolicy):
        raise ShadowDiagnosticError("a DiagnosticPolicy is required")
    canonical_events = tuple(sorted((_coerce_event(event) for event in events), key=lambda item: item.fixture_key))
    if len(canonical_events) > policy.max_events:
        canonical_events = canonical_events[: policy.max_events]
    event_map: dict[str, CanonicalEvent] = {}
    for event in canonical_events:
        event.validate()
        if event.fixture_key in event_map:
            raise ShadowDiagnosticError("canonical event fixture keys must be unique")
        event_map[event.fixture_key] = event

    records = tuple(_coerce_source(value) for value in source_fixtures)
    grouped: dict[str, list[SourceFixture]] = {key: [] for key in event_map}
    unbound: list[SourceFixture] = []
    for record in records:
        if record.fixture_key in grouped:
            grouped[record.fixture_key].append(record)
        else:
            unbound.append(record)

    issue_counts: Counter[str] = Counter()
    issue_samples: list[dict[str, object]] = []
    quality: list[SourceQuality] = []
    disagreements: list[SourceDisagreement] = []
    evaluated = 0

    def add_issue(source: str, fixture_key: str, code: DiagnosticCode, detail: str) -> None:
        issue_counts[code.value] += 1
        if len(issue_samples) < policy.max_issue_samples:
            issue_samples.append(
                {"source": source, "fixture_key": fixture_key, "code": code.value, "detail": detail}
            )

    for event in canonical_events:
        event_records = sorted(grouped[event.fixture_key], key=lambda item: (_text(item.source), item.source_event_id))
        if len(event_records) > policy.max_sources_per_event:
            add_issue(
                "<replay>",
                event.fixture_key,
                DiagnosticCode.SOURCE_LIMIT_EXCEEDED,
                "source count exceeded the bounded per-event limit",
            )
            event_records = event_records[: policy.max_sources_per_event]
        event_quality: list[tuple[SourceFixture, SourceQuality]] = []
        for record in event_records:
            item, raw_issues = _source_quality(event, record, policy)
            quality.append(item)
            event_quality.append((record, item))
            evaluated += 1
            for code, detail in raw_issues:
                add_issue(item.source, event.fixture_key, code, detail)
        event_disagreements = _find_disagreements(event, event_quality, policy)
        disagreements.extend(event_disagreements)
        for finding in event_disagreements:
            add_issue(
                "<comparison>",
                finding.fixture_key,
                DiagnosticCode.SOURCE_DISAGREEMENT,
                f"source values disagree for {finding.field}",
            )

    for record in sorted(unbound, key=lambda item: (_text(item.source), item.fixture_key, item.source_event_id)):
        add_issue(
            _text(record.source) or "<unknown>",
            _text(record.fixture_key),
            DiagnosticCode.FIXTURE_KEY_MISMATCH,
            "source fixture does not match a canonical event",
        )

    return ShadowDiagnosticReport(
        as_of=policy.as_of,
        policy=policy,
        event_count=len(canonical_events),
        source_input_count=len(records),
        source_evaluated_count=evaluated,
        source_quality=tuple(sorted(quality, key=lambda item: (item.fixture_key, item.source, item.source_event_id))),
        disagreements=tuple(sorted(disagreements, key=lambda item: (item.fixture_key, item.field))),
        issue_counts=MappingProxyType(dict(sorted(issue_counts.items()))),
        issue_samples=tuple(issue_samples),
        truncated=(
            len(events) > len(canonical_events)
            or any(
                len(grouped[event.fixture_key]) > policy.max_sources_per_event
                for event in canonical_events
            )
        ),
    )


def run_offline_champions_league_shadow(
    events: Sequence[CanonicalEvent | Mapping[str, object]],
    source_fixtures: Sequence[SourceFixture | Mapping[str, object]],
    *,
    as_of: datetime,
    max_freshness_seconds: int = DEFAULT_MAX_FRESHNESS_SECONDS,
    kickoff_tolerance_seconds: int = 60,
    required_bookmakers: Sequence[str] = DEFAULT_REQUIRED_BOOKMAKERS,
    max_events: int = 100,
    max_sources_per_event: int = 8,
    max_issue_samples: int = 40,
) -> ShadowDiagnosticReport:
    """Convenience wrapper requiring explicit deterministic replay time."""

    policy = DiagnosticPolicy(
        as_of=as_of,
        max_freshness_seconds=max_freshness_seconds,
        kickoff_tolerance_seconds=kickoff_tolerance_seconds,
        required_bookmakers=tuple(required_bookmakers),
        max_events=max_events,
        max_sources_per_event=max_sources_per_event,
        max_issue_samples=max_issue_samples,
    )
    return compare_champions_league_sources(events, source_fixtures, policy=policy)


def _fixture_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_champions_league_replay() -> tuple[tuple[CanonicalEvent, ...], tuple[SourceFixture, ...]]:
    """Return fixed local replay inputs covering pass and disagreement cases."""

    first = CanonicalEvent(
        fixture_key="ucl-replay-001",
        event_id="canonical-event-001",
        home_participant_id="club-alpha",
        home_name="Club Alpha",
        away_participant_id="club-beta",
        away_name="Club Beta",
        kickoff=datetime(2026, 9, 19, 20, tzinfo=timezone.utc),
    )
    second = CanonicalEvent(
        fixture_key="ucl-replay-002",
        event_id="canonical-event-002",
        home_participant_id="club-gamma",
        home_name="Club Gamma",
        away_participant_id="club-delta",
        away_name="Club Delta",
        kickoff=datetime(2026, 9, 19, 21, tzinfo=timezone.utc),
    )
    first_good = SourceFixture(
        source="source_alpha",
        fixture_key=first.fixture_key,
        source_event_id="alpha-event-001",
        competition="UEFA Champions League",
        home_participant_id="club-alpha",
        home_name="Club Alpha",
        away_participant_id="club-beta",
        away_name="Club Beta",
        kickoff=first.kickoff,
        regulation_1x2={"home": 2.1, "draw": 3.4, "away": 3.7},
        bookmakers=("Bet365", "Pinnacle"),
        source_timestamp=datetime(2026, 9, 19, 17, 55, tzinfo=timezone.utc),
        captured_at=DEFAULT_REPLAY_AS_OF,
        adapter_version="source-alpha-adapter-v1",
        request_identity="local-replay:alpha:001",
        raw_payload_digest=_fixture_digest("ucl-replay-001:source-alpha"),
        source_provenance="local-replay:ucl:source-alpha",
    )
    first_alias = SourceFixture(
        source="source_beta",
        fixture_key=first.fixture_key,
        source_event_id="beta-event-001",
        competition="ucl",
        home_participant_id="club-alpha",
        home_name="Club Alpha FC",
        away_participant_id="club-beta",
        away_name="Club Beta",
        kickoff="2026-09-19T20:00:30Z",
        regulation_1x2={"1": 2.11, "x": 3.39, "2": 3.71},
        bookmakers=("Bet365",),
        source_timestamp="2026-09-19T17:50:00Z",
        captured_at="2026-09-19T18:00:00Z",
        adapter_version="source-beta-adapter-v2",
        request_identity="local-replay:beta:001",
        raw_payload_digest=_fixture_digest("ucl-replay-001:source-beta"),
        source_provenance="local-replay:ucl:source-beta",
    )
    first_bad = SourceFixture(
        source="source_gamma",
        fixture_key=first.fixture_key,
        source_event_id="gamma-event-001",
        competition="ucl",
        home_participant_id="club-alpha",
        home_name="Club Alpha",
        away_participant_id="club-unknown",
        away_name="Club Beta",
        kickoff="2026-09-19T20:15:00Z",
        regulation_1x2={"home": 2.2, "away": 3.6},
        bookmakers=(),
        source_timestamp="2026-09-19T10:00:00Z",
        captured_at=DEFAULT_REPLAY_AS_OF,
        adapter_version="source-gamma-adapter-v1",
        request_identity="local-replay:gamma:001",
        raw_payload_digest="not-a-digest",
        source_provenance="local-replay:ucl:source-gamma",
    )
    second_mismatch = SourceFixture(
        source="source_delta",
        fixture_key=second.fixture_key,
        source_event_id="delta-event-002",
        competition="champions_league",
        home_participant_id="club-delta",
        home_name="Club Delta",
        away_participant_id="club-gamma",
        away_name="Club Gamma",
        kickoff="2026-09-19T21:00:00Z",
        regulation_1x2={"home": 1.9, "draw": 3.8, "away": 4.2},
        bookmakers=("Pinnacle",),
        source_timestamp="2026-09-19T17:45:00Z",
        captured_at=DEFAULT_REPLAY_AS_OF,
        adapter_version="source-delta-adapter-v1",
        request_identity="local-replay:delta:002",
        raw_payload_digest=_fixture_digest("ucl-replay-002:source-delta"),
        source_provenance="local-replay:ucl:source-delta",
    )
    return (first, second), (first_good, first_alias, first_bad, second_mismatch)


def run_deterministic_champions_league_shadow() -> ShadowDiagnosticReport:
    """Run the fixed replay sample with no filesystem or network access."""

    events, source_fixtures = deterministic_champions_league_replay()
    return run_offline_champions_league_shadow(
        events,
        source_fixtures,
        as_of=DEFAULT_REPLAY_AS_OF,
        required_bookmakers=("bet365", "pinnacle"),
    )


# Explicit aliases keep the seam discoverable to callers using either the
# provider-cascade or Champions League terminology.
ChampionsLeagueShadowDiagnosticReport = ShadowDiagnosticReport
ChampionsLeagueSourceFixture = SourceFixture
ChampionsLeagueCanonicalEvent = CanonicalEvent
ChampionsLeagueDiagnosticPolicy = DiagnosticPolicy


__all__ = [
    "CHAMPIONS_LEAGUE_CODE",
    "CHAMPIONS_LEAGUE_SCHEMA_VERSION",
    "DEFAULT_MAX_FRESHNESS_SECONDS",
    "DEFAULT_REPLAY_AS_OF",
    "CanonicalEvent",
    "ChampionsLeagueCanonicalEvent",
    "ChampionsLeagueDiagnosticPolicy",
    "ChampionsLeagueShadowDiagnosticReport",
    "ChampionsLeagueSourceFixture",
    "DiagnosticCode",
    "DiagnosticPolicy",
    "QualityStatus",
    "ShadowDiagnosticError",
    "ShadowDiagnosticReport",
    "SourceDisagreement",
    "SourceFixture",
    "SourceQuality",
    "compare_champions_league_sources",
    "deterministic_champions_league_replay",
    "run_deterministic_champions_league_shadow",
    "run_offline_champions_league_shadow",
]
