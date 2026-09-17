"""No-network APP-B1 evidence gate for the TheRundown candidate."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite

from src.football.production_contracts import ProductionContractError
from src.football.provider_cascade.contracts import MARKET_PREMATCH_1X2
from src.football.top5_controlled_shadow_provider_qualification import TOP5_LEAGUES

THERUNDOWN_PROVIDER_IDENTITY = "therundown_experimental"
THERUNDOWN_QUALIFICATION_SCHEMA_VERSION = "top5-therundown-qualification-evidence-v1"
EVIDENCE_SCHEMA_VERSION = THERUNDOWN_QUALIFICATION_SCHEMA_VERSION

QUALIFICATION_CRITERIA = (
    "provider_league_identity_verified",
    "real_fixture_observed",
    "home_away_identity_verified",
    "pre_match_regulation_1x2",
    "complete_home_draw_away_prices",
    "real_bookmaker_observed",
    "source_timestamp_freshness_observable",
    "request_provider_provenance_retained",
    "quota_rate_limit_behavior_recorded",
)

_EVIDENCE_FIELDS = frozenset(
    """schema_version evidence_id provider_identity evidence_kind
    canonical_league provider_league_code provider_league_identity_verified
    fixture_observed fixture_key provider_event_id home_team away_team
    home_away_identity_verified kickoff market_type market_phase odds
    bookmaker_observed bookmaker_identity source_timestamp
    source_timing_provenance captured_at provider_request_id observation_id
    source_provenance raw_record_digest normalized_record_digest adapter_version
    quota_state_before quota_state_after rate_limit_state quota_cost_units
    network_request_count synthetic_reconstruction provider_status failure_codes""".split()  # noqa: SIM905
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REAL_EVIDENCE = "REAL_OBSERVED"
_PRE_MATCH = "PRE_MATCH"
_SOURCE_TIMESTAMP = "SOURCE_TIMESTAMP"


TheRundownQualificationError = ProductionContractError


class TheRundownQualificationStatus(str, Enum):
    QUALIFIED_EVIDENCE_READY = "QUALIFIED_EVIDENCE_READY"
    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
    FAILED = "FAILED"
    UNOBSERVED = "UNOBSERVED"


class TheRundownQualificationCode(str, Enum):
    INVALID_SCHEMA = "INVALID_SCHEMA"
    PROVIDER_IDENTITY_MISMATCH = "PROVIDER_IDENTITY_MISMATCH"
    EVIDENCE_NOT_REAL = "EVIDENCE_NOT_REAL"
    SYNTHETIC_EVIDENCE = "SYNTHETIC_EVIDENCE"
    LEAGUE_OUT_OF_SCOPE = "LEAGUE_OUT_OF_SCOPE"
    PROVIDER_LEAGUE_UNVERIFIED = "PROVIDER_LEAGUE_UNVERIFIED"
    FIXTURE_NOT_OBSERVED = "FIXTURE_NOT_OBSERVED"
    HOME_AWAY_UNVERIFIED = "HOME_AWAY_UNVERIFIED"
    PREMATCH_MARKET_REQUIRED = "PREMATCH_MARKET_REQUIRED"
    COMPLETE_PRICES_REQUIRED = "COMPLETE_PRICES_REQUIRED"
    BOOKMAKER_NOT_OBSERVED = "BOOKMAKER_NOT_OBSERVED"
    FRESHNESS_NOT_OBSERVABLE = "FRESHNESS_NOT_OBSERVABLE"
    ODDS_STALE = "ODDS_STALE"
    REQUEST_PROVENANCE_INCOMPLETE = "REQUEST_PROVENANCE_INCOMPLETE"
    QUOTA_RATE_LIMIT_UNRECORDED = "QUOTA_RATE_LIMIT_UNRECORDED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    UNSAFE_NETWORK_COUNT = "UNSAFE_NETWORK_COUNT"


_HARD_FAILURES = frozenset(
    {
        TheRundownQualificationCode.INVALID_SCHEMA.value,
        TheRundownQualificationCode.PROVIDER_IDENTITY_MISMATCH.value,
        TheRundownQualificationCode.EVIDENCE_NOT_REAL.value,
        TheRundownQualificationCode.SYNTHETIC_EVIDENCE.value,
        TheRundownQualificationCode.LEAGUE_OUT_OF_SCOPE.value,
        TheRundownQualificationCode.PROVIDER_FAILURE.value,
        TheRundownQualificationCode.UNSAFE_NETWORK_COUNT.value,
    }
)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_price(
    value: object, *, minimum: float = 1.0, inclusive: bool = False
) -> bool:
    if isinstance(value, bool):
        return False
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(price) and (price >= minimum if inclusive else price > minimum)


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _recorded_state(value: object, fields: tuple[str, ...]) -> bool:
    if not isinstance(value, Mapping):
        return False
    present = False
    for field in fields:
        item = value.get(field)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return False
        present = True
    return present


@dataclass(frozen=True)
class TheRundownEvidenceResult:
    evidence_id: str
    league: str | None
    status: TheRundownQualificationStatus
    criteria: Mapping[str, bool]
    failure_codes: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY


@dataclass(frozen=True)
class TheRundownLeagueQualification:
    league: str
    status: TheRundownQualificationStatus
    evidence_count: int
    qualified_evidence_ids: tuple[str, ...]
    criteria_satisfied: tuple[str, ...]
    failure_codes: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "league": self.league,
            "status": self.status.value,
            "evidence_count": self.evidence_count,
            "qualified_evidence_ids": list(self.qualified_evidence_ids),
            "criteria_satisfied": list(self.criteria_satisfied),
            "failure_codes": list(self.failure_codes),
        }


@dataclass(frozen=True)
class TheRundownQualificationReport:
    schema_version: str
    provider_identity: str
    status: TheRundownQualificationStatus
    leagues: tuple[TheRundownLeagueQualification, ...]
    invalid_evidence_count: int
    global_failure_codes: tuple[str, ...]
    maximum_odds_age_seconds: int

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "status": self.status.value,
            "leagues": [item.as_payload() for item in self.leagues],
            "invalid_evidence_count": self.invalid_evidence_count,
            "global_failure_codes": list(self.global_failure_codes),
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "production_authority_changed": False,
            "provider_registered": False,
        }


def _validate_policy(maximum_odds_age_seconds: int) -> None:
    if (
        not isinstance(maximum_odds_age_seconds, int)
        or isinstance(maximum_odds_age_seconds, bool)
        or maximum_odds_age_seconds <= 0
    ):
        raise TheRundownQualificationError(
            "maximum_odds_age_seconds must be a positive integer"
        )


def _evaluate_record(
    raw: object, *, maximum_odds_age_seconds: int
) -> TheRundownEvidenceResult:
    if not isinstance(raw, Mapping):
        return TheRundownEvidenceResult(
            evidence_id="invalid",
            league=None,
            status=TheRundownQualificationStatus.FAILED,
            criteria={criterion: False for criterion in QUALIFICATION_CRITERIA},
            failure_codes=(TheRundownQualificationCode.INVALID_SCHEMA.value,),
        )

    failures: list[str] = []
    criteria = {criterion: False for criterion in QUALIFICATION_CRITERIA}
    unknown = set(raw) - _EVIDENCE_FIELDS
    if raw.get("schema_version") != EVIDENCE_SCHEMA_VERSION or unknown:
        failures.append(TheRundownQualificationCode.INVALID_SCHEMA.value)

    evidence_id = raw.get("evidence_id")
    evidence_id_text = evidence_id if _text(evidence_id) else "invalid"
    provider_ok = raw.get("provider_identity") == THERUNDOWN_PROVIDER_IDENTITY
    if not provider_ok:
        failures.append(TheRundownQualificationCode.PROVIDER_IDENTITY_MISMATCH.value)

    evidence_kind = raw.get("evidence_kind")
    if evidence_kind != _REAL_EVIDENCE:
        failures.append(TheRundownQualificationCode.EVIDENCE_NOT_REAL.value)
    if raw.get("synthetic_reconstruction") is True:
        failures.append(TheRundownQualificationCode.SYNTHETIC_EVIDENCE.value)
    if raw.get("network_request_count") != 1:
        failures.append(TheRundownQualificationCode.UNSAFE_NETWORK_COUNT.value)

    league = raw.get("canonical_league")
    league_text = league if isinstance(league, str) else None
    league_in_scope = league_text in TOP5_LEAGUES
    if not league_in_scope:
        failures.append(TheRundownQualificationCode.LEAGUE_OUT_OF_SCOPE.value)

    provider_league_ok = (
        raw.get("provider_league_identity_verified") is True
        and _text(raw.get("provider_league_code"))
        and provider_ok
        and league_in_scope
    )
    criteria[QUALIFICATION_CRITERIA[0]] = provider_league_ok
    if not provider_league_ok:
        failures.append(TheRundownQualificationCode.PROVIDER_LEAGUE_UNVERIFIED.value)

    kickoff = _aware_datetime(raw.get("kickoff"))
    fixture_ok = (
        raw.get("fixture_observed") is True
        and _text(raw.get("fixture_key"))
        and _text(raw.get("provider_event_id"))
        and kickoff is not None
    )
    criteria[QUALIFICATION_CRITERIA[1]] = fixture_ok
    if not fixture_ok:
        failures.append(TheRundownQualificationCode.FIXTURE_NOT_OBSERVED.value)

    teams_ok = (
        raw.get("home_away_identity_verified") is True
        and _text(raw.get("home_team"))
        and _text(raw.get("away_team"))
        and raw.get("home_team") != raw.get("away_team")
    )
    criteria[QUALIFICATION_CRITERIA[2]] = teams_ok
    if not teams_ok:
        failures.append(TheRundownQualificationCode.HOME_AWAY_UNVERIFIED.value)

    source_timestamp = _aware_datetime(raw.get("source_timestamp"))
    captured_at = _aware_datetime(raw.get("captured_at"))
    freshness_ok = (
        source_timestamp is not None
        and captured_at is not None
        and raw.get("source_timing_provenance") == _SOURCE_TIMESTAMP
        and 0
        <= (captured_at - source_timestamp).total_seconds()
        <= maximum_odds_age_seconds
    )
    if source_timestamp is not None and captured_at is not None:
        source_age = (captured_at - source_timestamp).total_seconds()
        if source_age < 0 or source_age > maximum_odds_age_seconds:
            failures.append(TheRundownQualificationCode.ODDS_STALE.value)
    if raw.get("source_timing_provenance") != _SOURCE_TIMESTAMP:
        failures.append(TheRundownQualificationCode.FRESHNESS_NOT_OBSERVABLE.value)
    criteria[QUALIFICATION_CRITERIA[6]] = freshness_ok
    if (
        not freshness_ok
        and TheRundownQualificationCode.ODDS_STALE.value not in failures
    ):
        failures.append(TheRundownQualificationCode.FRESHNESS_NOT_OBSERVABLE.value)

    pre_match_ok = (
        raw.get("market_type") == MARKET_PREMATCH_1X2
        and raw.get("market_phase") == _PRE_MATCH
        and kickoff is not None
        and captured_at is not None
        and captured_at < kickoff
    )
    criteria[QUALIFICATION_CRITERIA[3]] = pre_match_ok
    if not pre_match_ok:
        failures.append(TheRundownQualificationCode.PREMATCH_MARKET_REQUIRED.value)

    odds = raw.get("odds")
    prices_ok = isinstance(odds, Mapping) and all(
        _finite_price(odds.get(outcome)) for outcome in ("home", "draw", "away")
    )
    criteria[QUALIFICATION_CRITERIA[4]] = prices_ok
    if not prices_ok:
        failures.append(TheRundownQualificationCode.COMPLETE_PRICES_REQUIRED.value)

    bookmaker_ok = raw.get("bookmaker_observed") is True and _text(
        raw.get("bookmaker_identity")
    )
    criteria[QUALIFICATION_CRITERIA[5]] = bookmaker_ok
    if not bookmaker_ok:
        failures.append(TheRundownQualificationCode.BOOKMAKER_NOT_OBSERVED.value)

    provenance_ok = all(
        (
            _text(raw.get("provider_request_id")),
            _text(raw.get("observation_id")),
            _text(raw.get("source_provenance")),
            _digest(raw.get("raw_record_digest")),
            _digest(raw.get("normalized_record_digest")),
            _text(raw.get("adapter_version")),
        )
    )
    criteria[QUALIFICATION_CRITERIA[7]] = provenance_ok
    if not provenance_ok:
        failures.append(TheRundownQualificationCode.REQUEST_PROVENANCE_INCOMPLETE.value)

    quota_ok = (
        _recorded_state(raw.get("quota_state_before"), ("used", "remaining"))
        and _recorded_state(raw.get("quota_state_after"), ("used", "remaining"))
        and _recorded_state(
            raw.get("rate_limit_state"), ("rate_limit", "rate_remaining")
        )
        and not isinstance(raw.get("quota_cost_units"), bool)
        and _finite_price(raw.get("quota_cost_units"), minimum=0.0, inclusive=True)
    )
    criteria[QUALIFICATION_CRITERIA[8]] = quota_ok
    if not quota_ok:
        failures.append(TheRundownQualificationCode.QUOTA_RATE_LIMIT_UNRECORDED.value)

    if raw.get("provider_status") != "AVAILABLE" or raw.get("failure_codes") not in (
        (),
        [],
    ):
        failures.append(TheRundownQualificationCode.PROVIDER_FAILURE.value)

    failures = list(dict.fromkeys(failures))
    accepted = not failures and all(criteria.values())
    if accepted:
        status = TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    elif set(failures) & _HARD_FAILURES:
        status = TheRundownQualificationStatus.FAILED
    else:
        status = TheRundownQualificationStatus.PARTIAL_EVIDENCE
    return TheRundownEvidenceResult(
        evidence_id=str(evidence_id_text),
        league=league_text if league_in_scope else None,
        status=status,
        criteria=criteria,
        failure_codes=tuple(failures),
    )


def _overall_status(
    leagues: Sequence[TheRundownLeagueQualification],
    global_failure_codes: Sequence[str],
) -> TheRundownQualificationStatus:
    if global_failure_codes:
        return TheRundownQualificationStatus.FAILED
    statuses = tuple(item.status for item in leagues)
    if all(
        status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
        for status in statuses
    ):
        return TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    if any(status is TheRundownQualificationStatus.FAILED for status in statuses):
        return TheRundownQualificationStatus.FAILED
    if any(
        status
        in {
            TheRundownQualificationStatus.PARTIAL_EVIDENCE,
            TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY,
        }
        for status in statuses
    ):
        return TheRundownQualificationStatus.PARTIAL_EVIDENCE
    return TheRundownQualificationStatus.UNOBSERVED


def evaluate_therundown_qualification(
    payload: Mapping[str, object], *, maximum_odds_age_seconds: int
) -> TheRundownQualificationReport:
    """Evaluate a B2-produced evidence envelope without side effects."""

    _validate_policy(maximum_odds_age_seconds)
    if not isinstance(payload, Mapping):
        raise TheRundownQualificationError("qualification envelope must be a mapping")
    unknown = set(payload) - {"schema_version", "provider_identity", "evidence"}
    if payload.get("schema_version") != THERUNDOWN_QUALIFICATION_SCHEMA_VERSION:
        raise TheRundownQualificationError("unsupported evidence schema")
    if payload.get("provider_identity") != THERUNDOWN_PROVIDER_IDENTITY:
        raise TheRundownQualificationError("qualification provider identity mismatch")
    evidence = payload.get("evidence")
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise TheRundownQualificationError("evidence must be a sequence")
    global_failures = (
        [TheRundownQualificationCode.INVALID_SCHEMA.value] if unknown else []
    )
    evaluated = tuple(
        _evaluate_record(item, maximum_odds_age_seconds=maximum_odds_age_seconds)
        for item in evidence
    )
    grouped: dict[str, list[TheRundownEvidenceResult]] = {
        league: [] for league in TOP5_LEAGUES
    }
    invalid_count = 0
    for result in evaluated:
        if result.league in grouped:
            grouped[result.league].append(result)
        else:
            invalid_count += 1
            global_failures.extend(result.failure_codes)

    league_results: list[TheRundownLeagueQualification] = []
    for league in TOP5_LEAGUES:
        items = grouped[league]
        if not items:
            league_results.append(
                TheRundownLeagueQualification(
                    league,
                    TheRundownQualificationStatus.UNOBSERVED,
                    0,
                    (),
                    (),
                    (),
                )
            )
            continue
        qualified = tuple(item.evidence_id for item in items if item.accepted)
        satisfied = tuple(
            criterion
            for criterion in QUALIFICATION_CRITERIA
            if any(item.criteria.get(criterion) for item in items)
        )
        failures = tuple(
            dict.fromkeys(code for item in items for code in item.failure_codes)
        )
        if qualified:
            status = TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
        elif any(item.status is TheRundownQualificationStatus.FAILED for item in items):
            status = TheRundownQualificationStatus.FAILED
        else:
            status = TheRundownQualificationStatus.PARTIAL_EVIDENCE
        league_results.append(
            TheRundownLeagueQualification(
                league,
                status,
                len(items),
                qualified,
                satisfied,
                failures,
            )
        )

    global_failures = list(dict.fromkeys(global_failures))
    report = TheRundownQualificationReport(
        schema_version=THERUNDOWN_QUALIFICATION_SCHEMA_VERSION,
        provider_identity=THERUNDOWN_PROVIDER_IDENTITY,
        status=_overall_status(league_results, global_failures),
        leagues=tuple(league_results),
        invalid_evidence_count=invalid_count,
        global_failure_codes=tuple(global_failures),
        maximum_odds_age_seconds=maximum_odds_age_seconds,
    )
    return report


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "QUALIFICATION_CRITERIA",
    "THERUNDOWN_PROVIDER_IDENTITY",
    "THERUNDOWN_QUALIFICATION_SCHEMA_VERSION",
    "TheRundownEvidenceResult",
    "TheRundownLeagueQualification",
    "TheRundownQualificationCode",
    "TheRundownQualificationError",
    "TheRundownQualificationReport",
    "TheRundownQualificationStatus",
    "evaluate_therundown_qualification",
]
