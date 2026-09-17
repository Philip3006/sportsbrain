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
PR88_REAL_EVIDENCE_SUMMARY_SCHEMA_VERSION = (
    "top5-therundown-pr88-real-evidence-summary-v1"
)
PR88_REAL_EVIDENCE_HEAD = "279654a2aefe71fc4b7db9d58cd985adb9fbdd94"
PR88_REAL_EVIDENCE_REVIEW_ID = "5241854809"
PR88_REAL_EVIDENCE_DOCUMENT = "docs/top5_therundown_experimental_evaluation.md"
PR88_REAL_EVIDENCE_DOCUMENT_BLOB = "c6fec62ee16976d8cc2ed7dd6bce7d75fc32d8b8"
PR91_SHADOW_COMPATIBILITY_HEAD = "a29030571940c73f51066856f589e29b06627523"
PR91_SHADOW_COMPATIBILITY_REVIEW_ID = "5241881123"

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


class TheRundownEvidenceSummaryStatus(str, Enum):
    """Status for redacted PR-88 run summaries, never canonical authority."""

    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
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
    CANONICAL_OBSERVATION_REQUIRED = "CANONICAL_OBSERVATION_REQUIRED"
    EXACT_PRICE_PROVENANCE_REQUIRED = "EXACT_PRICE_PROVENANCE_REQUIRED"


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


@dataclass(frozen=True)
class TheRundownLeagueEvidenceSummary:
    """Redacted, real-run summary for one league from the reviewed PR-88 run."""

    league: str
    provider_league_id: str | None
    provider_league_code: str | None
    access: str
    real_fixture: Mapping[str, object] | None
    complete_1x2: bool
    complete_1x2_count: int
    bookmakers: tuple[str, ...]
    source_update_timestamp_range: tuple[str, ...]
    delay_seconds: int | None
    event_datapoints: int
    result: str
    status: TheRundownEvidenceSummaryStatus
    blockers: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "league": self.league,
            "provider_league_id": self.provider_league_id,
            "provider_league_code": self.provider_league_code,
            "access": self.access,
            "real_fixture": dict(self.real_fixture) if self.real_fixture else None,
            "complete_1x2": self.complete_1x2,
            "complete_1x2_count": self.complete_1x2_count,
            "bookmakers": list(self.bookmakers),
            "source_update_timestamp_range": list(self.source_update_timestamp_range),
            "delay_seconds": self.delay_seconds,
            "event_datapoints": self.event_datapoints,
            "result": self.result,
            "status": self.status.value,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class TheRundownPR88EvidenceSummaryReport:
    """Read-only projection of PR-88's real diagnostic evidence.

    This is deliberately a summary contract. It cannot satisfy the detailed
    evidence gate and cannot be projected into a Builder-2 receipt or a
    production-authoritative provider observation.
    """

    schema_version: str
    provider_identity: str
    source: Mapping[str, object]
    run: Mapping[str, object]
    status: TheRundownEvidenceSummaryStatus
    leagues: tuple[TheRundownLeagueEvidenceSummary, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "source": dict(self.source),
            "run": dict(self.run),
            "status": self.status.value,
            "leagues": [item.as_payload() for item in self.leagues],
            "summary_only": True,
            "canonical_gate_eligible": False,
            "builder2_receipt_eligible": False,
            "provider_registered": False,
            "production_authority_changed": False,
            "publication_authorized": False,
            "betting_authorized": False,
            "production_activation_authorized": False,
            "evaluator_network_accessed": False,
            "shadow_compatibility": {
                "pr": 91,
                "head": PR91_SHADOW_COMPATIBILITY_HEAD,
                "review_id": PR91_SHADOW_COMPATIBILITY_REVIEW_ID,
            },
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


def _summary_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TheRundownQualificationError(f"{name} must be non-empty text")
    return value.strip()


def _summary_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TheRundownQualificationError(f"{name} must be a non-negative integer")
    return value


def _summary_string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TheRundownQualificationError(f"{name} must be a list")
    result = tuple(_summary_text(item, name) for item in value)
    if len(set(result)) != len(result):
        raise TheRundownQualificationError(f"{name} must not contain duplicates")
    return result


def _summary_fixture(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TheRundownQualificationError("real_fixture must be an object or null")
    allowed = {"home", "away", "kickoff"}
    if set(value) != allowed:
        raise TheRundownQualificationError(
            "real_fixture must contain only home, away, and kickoff"
        )
    home = _summary_text(value.get("home"), "real_fixture.home")
    away = _summary_text(value.get("away"), "real_fixture.away")
    kickoff = _aware_datetime(value.get("kickoff"))
    if home == away or kickoff is None:
        raise TheRundownQualificationError("real_fixture identity is invalid")
    return {"home": home, "away": away, "kickoff": kickoff.isoformat()}


def _summary_source(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TheRundownQualificationError("source must be an object")
    allowed = {
        "provider_identity",
        "source_pr",
        "source_head",
        "source_review_id",
        "source_document",
        "source_document_blob",
    }
    if set(payload) != allowed:
        raise TheRundownQualificationError("source schema is invalid")
    if payload.get("provider_identity") != THERUNDOWN_PROVIDER_IDENTITY:
        raise TheRundownQualificationError("source provider identity mismatch")
    if payload.get("source_pr") != 88:
        raise TheRundownQualificationError("source PR must be 88")
    if payload.get("source_head") != PR88_REAL_EVIDENCE_HEAD:
        raise TheRundownQualificationError("source PR-88 head mismatch")
    if payload.get("source_review_id") != PR88_REAL_EVIDENCE_REVIEW_ID:
        raise TheRundownQualificationError("source PR-88 review mismatch")
    if payload.get("source_document") != PR88_REAL_EVIDENCE_DOCUMENT:
        raise TheRundownQualificationError("source document mismatch")
    if payload.get("source_document_blob") != PR88_REAL_EVIDENCE_DOCUMENT_BLOB:
        raise TheRundownQualificationError("source document digest mismatch")
    return dict(payload)


def _summary_run(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TheRundownQualificationError("run must be an object")
    allowed = {
        "real_requests",
        "datapoints_before",
        "datapoints_after",
        "datapoints_consumed",
        "account_tier",
        "delay_seconds",
        "rate_limit_per_second",
        "bookmaker_entitlement",
        "history_access",
        "live_odds_access",
        "websocket_access",
    }
    if set(payload) != allowed:
        raise TheRundownQualificationError("run schema is invalid")
    for name in (
        "real_requests",
        "datapoints_before",
        "datapoints_after",
        "datapoints_consumed",
        "delay_seconds",
        "rate_limit_per_second",
    ):
        _summary_nonnegative_int(payload.get(name), f"run.{name}")
    if (
        payload["datapoints_after"] - payload["datapoints_before"]
        != payload["datapoints_consumed"]
    ):
        raise TheRundownQualificationError("run datapoint accounting is inconsistent")
    _summary_text(payload.get("account_tier"), "run.account_tier")
    _summary_string_list(
        payload.get("bookmaker_entitlement"), "run.bookmaker_entitlement"
    )
    for name in ("history_access", "live_odds_access", "websocket_access"):
        if not isinstance(payload.get(name), bool):
            raise TheRundownQualificationError(f"run.{name} must be boolean")
    return dict(payload)


def _summary_league(payload: object) -> TheRundownLeagueEvidenceSummary:
    if not isinstance(payload, Mapping):
        raise TheRundownQualificationError("league summary must be an object")
    allowed = {
        "canonical_league",
        "provider_league_id",
        "provider_league_code",
        "access",
        "real_fixture",
        "complete_1x2",
        "complete_1x2_count",
        "bookmakers",
        "source_update_timestamp_range",
        "delay_seconds",
        "event_datapoints",
        "result",
    }
    if set(payload) != allowed:
        raise TheRundownQualificationError("league summary schema is invalid")
    league = _summary_text(payload.get("canonical_league"), "canonical_league")
    if league not in TOP5_LEAGUES:
        raise TheRundownQualificationError("summary league is outside Top-5 scope")
    provider_id = _summary_text(payload.get("provider_league_id"), "provider_league_id")
    provider_code = _summary_text(
        payload.get("provider_league_code"), "provider_league_code"
    )
    access = _summary_text(payload.get("access"), "access")
    if access not in {"YES", "NO", "PARTIAL"}:
        raise TheRundownQualificationError("summary access state is invalid")
    fixture = _summary_fixture(payload.get("real_fixture"))
    complete = payload.get("complete_1x2")
    if not isinstance(complete, bool):
        raise TheRundownQualificationError("complete_1x2 must be boolean")
    complete_count = _summary_nonnegative_int(
        payload.get("complete_1x2_count"), "complete_1x2_count"
    )
    bookmakers = _summary_string_list(payload.get("bookmakers"), "bookmakers")
    if complete and complete_count != len(bookmakers):
        raise TheRundownQualificationError(
            "complete_1x2_count must match the bookmaker summary"
        )
    timestamps = _summary_string_list(
        payload.get("source_update_timestamp_range"),
        "source_update_timestamp_range",
    )
    for timestamp in timestamps:
        if _aware_datetime(timestamp) is None:
            raise TheRundownQualificationError(
                "source_update_timestamp_range must be timezone-aware"
            )
    delay = payload.get("delay_seconds")
    if delay is not None:
        delay = _summary_nonnegative_int(delay, "delay_seconds")
    event_datapoints = _summary_nonnegative_int(
        payload.get("event_datapoints"), "event_datapoints"
    )
    result = _summary_text(payload.get("result"), "result")
    if result not in {"PASS_EVIDENCE", "PARTIAL", "UNOBSERVED"}:
        raise TheRundownQualificationError("summary result is invalid")
    observed = fixture is not None
    complete_observation = complete and complete_count > 0 and bool(bookmakers)
    if not observed and not complete_observation:
        status = TheRundownEvidenceSummaryStatus.UNOBSERVED
    else:
        status = TheRundownEvidenceSummaryStatus.PARTIAL_EVIDENCE
    blockers = [
        TheRundownQualificationCode.CANONICAL_OBSERVATION_REQUIRED.value,
        TheRundownQualificationCode.EXACT_PRICE_PROVENANCE_REQUIRED.value,
        TheRundownQualificationCode.FRESHNESS_NOT_OBSERVABLE.value,
    ]
    if not observed:
        blockers.append(TheRundownQualificationCode.FIXTURE_NOT_OBSERVED.value)
    if not complete_observation:
        blockers.append(TheRundownQualificationCode.COMPLETE_PRICES_REQUIRED.value)
    return TheRundownLeagueEvidenceSummary(
        league=league,
        provider_league_id=provider_id,
        provider_league_code=provider_code,
        access=access,
        real_fixture=fixture,
        complete_1x2=complete,
        complete_1x2_count=complete_count,
        bookmakers=bookmakers,
        source_update_timestamp_range=timestamps,
        delay_seconds=delay,
        event_datapoints=event_datapoints,
        result=result,
        status=status,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def evaluate_therundown_pr88_evidence_summary(
    payload: Mapping[str, object],
) -> TheRundownPR88EvidenceSummaryReport:
    """Consume PR-88's redacted real-run summary without qualifying it."""

    if not isinstance(payload, Mapping):
        raise TheRundownQualificationError("evidence summary must be an object")
    allowed = {"schema_version", "provider_identity", "source", "run", "leagues"}
    if set(payload) != allowed:
        raise TheRundownQualificationError("evidence summary schema is invalid")
    if payload.get("schema_version") != PR88_REAL_EVIDENCE_SUMMARY_SCHEMA_VERSION:
        raise TheRundownQualificationError("unsupported PR-88 summary schema")
    if payload.get("provider_identity") != THERUNDOWN_PROVIDER_IDENTITY:
        raise TheRundownQualificationError("summary provider identity mismatch")
    source = _summary_source(payload.get("source"))
    run = _summary_run(payload.get("run"))
    raw_leagues = payload.get("leagues")
    if not isinstance(raw_leagues, list):
        raise TheRundownQualificationError("leagues must be a list")
    leagues = tuple(_summary_league(item) for item in raw_leagues)
    if {item.league for item in leagues} != set(TOP5_LEAGUES) or len(leagues) != len(
        TOP5_LEAGUES
    ):
        raise TheRundownQualificationError(
            "summary must contain exactly one item for every Top-5 league"
        )
    leagues = tuple(sorted(leagues, key=lambda item: TOP5_LEAGUES.index(item.league)))
    return TheRundownPR88EvidenceSummaryReport(
        schema_version=PR88_REAL_EVIDENCE_SUMMARY_SCHEMA_VERSION,
        provider_identity=THERUNDOWN_PROVIDER_IDENTITY,
        source=source,
        run=run,
        status=TheRundownEvidenceSummaryStatus.PARTIAL_EVIDENCE,
        leagues=leagues,
    )


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "PR88_REAL_EVIDENCE_DOCUMENT",
    "PR88_REAL_EVIDENCE_DOCUMENT_BLOB",
    "PR88_REAL_EVIDENCE_HEAD",
    "PR88_REAL_EVIDENCE_REVIEW_ID",
    "PR88_REAL_EVIDENCE_SUMMARY_SCHEMA_VERSION",
    "PR91_SHADOW_COMPATIBILITY_HEAD",
    "PR91_SHADOW_COMPATIBILITY_REVIEW_ID",
    "QUALIFICATION_CRITERIA",
    "THERUNDOWN_PROVIDER_IDENTITY",
    "THERUNDOWN_QUALIFICATION_SCHEMA_VERSION",
    "TheRundownEvidenceResult",
    "TheRundownEvidenceSummaryStatus",
    "TheRundownLeagueEvidenceSummary",
    "TheRundownLeagueQualification",
    "TheRundownPR88EvidenceSummaryReport",
    "TheRundownQualificationCode",
    "TheRundownQualificationError",
    "TheRundownQualificationReport",
    "TheRundownQualificationStatus",
    "evaluate_therundown_pr88_evidence_summary",
    "evaluate_therundown_qualification",
]
