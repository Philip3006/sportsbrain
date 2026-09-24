"""Independent, deterministic qualification rules for a Champions League dataset.

The objects in this module are contracts, not an ingestion or modelling
pipeline.  They deliberately have no provider, network, scheduler, publisher,
financial, or Builder 1 dependency.  A dataset is accepted only when its
competition/match identity, historical format, temporal partitions, source
availability, model metadata, and shadow evidence can all be checked from the
supplied values.

All temporal intervals use UTC and are half-open: ``[start_at, end_at)``.
Closing-market snapshots and any source timestamp after prediction time are
hard failures, including when they are represented only in provenance.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Any

from src.football.production_contracts import ProductionContractError


CHAMPIONS_LEAGUE_COMPETITION_ID = "uefa_champions_league"
CHAMPIONS_LEAGUE_COMPETITION_NAME = "UEFA Champions League"
CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION = "champions-league-qualification-v1"


class ChampionsLeagueQualificationError(ProductionContractError):
    """Raised when a Champions League dataset or artifact is not admissible."""


class HistoricalFormatEra(str, Enum):
    EUROPEAN_CUP_PRE_1992 = "european_cup_pre_1992"
    CHAMPIONS_LEAGUE_GROUP_STAGE_1992_2003 = "champions_league_group_stage_1992_2003"
    CHAMPIONS_LEAGUE_TWO_GROUP_STAGES_2003_2004 = (
        "champions_league_two_group_stages_2003_2004"
    )
    CHAMPIONS_LEAGUE_GROUP_STAGE_2004_2024 = "champions_league_group_stage_2004_2024"
    CHAMPIONS_LEAGUE_LEAGUE_PHASE_2024_PLUS = "champions_league_league_phase_2024_plus"


class VenueSemantics(str, Enum):
    HOME_AWAY = "home_away"
    NEUTRAL = "neutral"


class AggregateMode(str, Enum):
    NONE = "none"
    TWO_LEG = "two_leg"
    SINGLE_LEG = "single_leg"


class ResultStatus(str, Enum):
    FINAL = "final"
    SCHEDULED = "scheduled"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class DatasetPartition(str, Enum):
    DEVELOPMENT = "development"
    CALIBRATION = "calibration"
    HOLDOUT = "holdout"


PARTITION_ORDER = (
    DatasetPartition.DEVELOPMENT.value,
    DatasetPartition.CALIBRATION.value,
    DatasetPartition.HOLDOUT.value,
)
_PARTITION_ALIASES = {
    "dev": DatasetPartition.DEVELOPMENT.value,
    "development": DatasetPartition.DEVELOPMENT.value,
    "train": DatasetPartition.DEVELOPMENT.value,
    "cal": DatasetPartition.CALIBRATION.value,
    "calibration": DatasetPartition.CALIBRATION.value,
    "validation": DatasetPartition.CALIBRATION.value,
    "holdout": DatasetPartition.HOLDOUT.value,
    "test": DatasetPartition.HOLDOUT.value,
}

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_SEASON_RE = re.compile(r"^(\d{4})[-/](\d{2})$")
_MATCHDAY_RE = re.compile(r"^matchday[_ -]?([1-8])$")
_FORBIDDEN_MODEL_INPUT_TOKENS = (
    "closing",
    "final_odds",
    "post_match",
    "post_kickoff",
    "after_kickoff",
    "future",
)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChampionsLeagueQualificationError(f"{field_name} is required")
    return value.strip()


def _require_identity(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChampionsLeagueQualificationError(f"{field_name} is required")
    text = value.strip()
    if value != text or _IDENTITY_RE.fullmatch(text) is None:
        raise ChampionsLeagueQualificationError(f"{field_name} is malformed")
    return text


def _require_sha(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if _SHA_RE.fullmatch(text) is None:
        raise ChampionsLeagueQualificationError(
            f"{field_name} must be a 40-64 character hexadecimal SHA"
        )
    return text.lower()


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ChampionsLeagueQualificationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _optional_utc(value: object, field_name: str) -> datetime | None:
    return None if value is None else _utc(value, field_name)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field_name)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field_name)
        except ValueError as exc:
            raise ChampionsLeagueQualificationError(
                f"{field_name} must be an ISO-8601 timestamp"
            ) from exc
    raise ChampionsLeagueQualificationError(f"{field_name} must be a timestamp")


def _optional_parse_datetime(value: object, field_name: str) -> datetime | None:
    return None if value is None else _parse_datetime(value, field_name)


def _normalize_token(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _normalize_id_token(value: object, field_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalize_token(value, field_name)).strip("_")


def _enum_value(enum_type: type[Enum], value: object, field_name: str) -> str:
    try:
        return enum_type(value).value
    except (TypeError, ValueError) as exc:
        raise ChampionsLeagueQualificationError(f"{field_name} is invalid") from exc


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ChampionsLeagueQualificationError(f"{field_name} must be boolean")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChampionsLeagueQualificationError(f"{field_name} must be a non-negative integer")
    return value


def _stable_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, Mapping):
        return {str(key): _stable_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    return value


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _stable_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChampionsLeagueQualificationError(f"{field_name} must be an object")
    return value


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ChampionsLeagueQualificationError(f"{field_name} must be a sequence")
    return tuple(value)


def _reject_forbidden_model_input_name(value: object, field_name: str) -> str:
    name = _require_text(value, field_name)
    token = _normalize_id_token(name, field_name)
    if not token:
        raise ChampionsLeagueQualificationError(f"{field_name} is malformed")
    if any(
        token == forbidden or token.startswith(f"{forbidden}_")
        for forbidden in _FORBIDDEN_MODEL_INPUT_TOKENS
    ):
        raise ChampionsLeagueQualificationError(
            f"{field_name} declares future or closing-market information"
        )
    return name


def normalize_competition_id(value: object) -> str:
    """Resolve accepted historical/provider labels to one canonical ID."""

    if isinstance(value, Mapping):
        value = value.get("canonical_competition_id", value.get("canonical_id", value.get("competition_id")))
    token = _normalize_id_token(value, "competition_id")
    aliases = {
        "ucl": CHAMPIONS_LEAGUE_COMPETITION_ID,
        "champions_league": CHAMPIONS_LEAGUE_COMPETITION_ID,
        "uefa_champions_league": CHAMPIONS_LEAGUE_COMPETITION_ID,
        "uefa_cl": CHAMPIONS_LEAGUE_COMPETITION_ID,
        "european_cup": CHAMPIONS_LEAGUE_COMPETITION_ID,
        "european_champions_cup": CHAMPIONS_LEAGUE_COMPETITION_ID,
    }
    try:
        return aliases[token]
    except KeyError as exc:
        raise ChampionsLeagueQualificationError(
            "competition identity is not the canonical UEFA Champions League"
        ) from exc


def normalize_season(value: object) -> str:
    text = _require_text(value, "season")
    match = _SEASON_RE.fullmatch(text)
    if match is None:
        raise ChampionsLeagueQualificationError("season must use YYYY-YY")
    start_year, end_suffix = int(match.group(1)), int(match.group(2))
    if start_year < 1955 or end_suffix != (start_year + 1) % 100:
        raise ChampionsLeagueQualificationError("season is not a valid Champions League season")
    return f"{start_year:04d}-{end_suffix:02d}"


def season_start_year(season: str) -> int:
    return int(normalize_season(season)[:4])


def normalize_format_era(value: object) -> str:
    token = _normalize_id_token(value, "format_era")
    aliases = {
        "european_cup": HistoricalFormatEra.EUROPEAN_CUP_PRE_1992.value,
        "european_cup_pre_1992": HistoricalFormatEra.EUROPEAN_CUP_PRE_1992.value,
        "ucl_1992_2003": HistoricalFormatEra.CHAMPIONS_LEAGUE_GROUP_STAGE_1992_2003.value,
        "champions_league_group_stage_1992_2003": HistoricalFormatEra.CHAMPIONS_LEAGUE_GROUP_STAGE_1992_2003.value,
        "ucl_two_group_stages_2003_2004": HistoricalFormatEra.CHAMPIONS_LEAGUE_TWO_GROUP_STAGES_2003_2004.value,
        "champions_league_two_group_stages_2003_2004": HistoricalFormatEra.CHAMPIONS_LEAGUE_TWO_GROUP_STAGES_2003_2004.value,
        "ucl_group_stage_2004_2024": HistoricalFormatEra.CHAMPIONS_LEAGUE_GROUP_STAGE_2004_2024.value,
        "champions_league_group_stage_2004_2024": HistoricalFormatEra.CHAMPIONS_LEAGUE_GROUP_STAGE_2004_2024.value,
        "ucl_league_phase_2024_plus": HistoricalFormatEra.CHAMPIONS_LEAGUE_LEAGUE_PHASE_2024_PLUS.value,
        "champions_league_league_phase_2024_plus": HistoricalFormatEra.CHAMPIONS_LEAGUE_LEAGUE_PHASE_2024_PLUS.value,
    }
    try:
        return aliases[token]
    except KeyError as exc:
        raise ChampionsLeagueQualificationError("historical format era is invalid") from exc


def expected_format_era(season: str) -> str:
    start = season_start_year(season)
    if start < 1992:
        return HistoricalFormatEra.EUROPEAN_CUP_PRE_1992.value
    if start < 2003:
        return HistoricalFormatEra.CHAMPIONS_LEAGUE_GROUP_STAGE_1992_2003.value
    if start == 2003:
        return HistoricalFormatEra.CHAMPIONS_LEAGUE_TWO_GROUP_STAGES_2003_2004.value
    if start < 2024:
        return HistoricalFormatEra.CHAMPIONS_LEAGUE_GROUP_STAGE_2004_2024.value
    return HistoricalFormatEra.CHAMPIONS_LEAGUE_LEAGUE_PHASE_2024_PLUS.value


def normalize_partition(value: object, field_name: str = "partition") -> str:
    token = _normalize_id_token(value, field_name)
    try:
        return _PARTITION_ALIASES[token]
    except KeyError as exc:
        raise ChampionsLeagueQualificationError(
            f"{field_name} must be development, calibration, or holdout"
        ) from exc


class TeamAliasRegistry:
    """Deterministic alias-to-canonical-team resolver.

    Keys and values are team IDs or names supplied by the dataset owner.  The
    registry never guesses across unrelated teams; absent aliases are resolved
    by a Unicode/punctuation-stable canonical token.
    """

    def __init__(self, aliases: Mapping[str, str] | None = None):
        if aliases is None:
            aliases = {}
        if not isinstance(aliases, Mapping):
            raise ChampionsLeagueQualificationError("team aliases must be an object")
        self.aliases = MappingProxyType(dict(aliases))
        normalized: dict[str, str] = {}
        for alias, target in aliases.items():
            alias_key = _normalize_token(alias, "team alias")
            target_key = _normalize_id_token(target, "canonical team")
            old = normalized.get(alias_key)
            if old is not None and old != target_key:
                raise ChampionsLeagueQualificationError(
                    "team alias resolves ambiguously to multiple canonical teams"
                )
            normalized[alias_key] = target_key
        self._normalized_aliases = MappingProxyType(normalized)
        self.validate()

    @classmethod
    def from_mapping(cls, aliases: Mapping[str, str]) -> TeamAliasRegistry:
        return cls(aliases)

    def validate(self) -> None:
        seen: dict[str, str] = {}
        for alias, target in self.aliases.items():
            alias_key = _normalize_token(alias, "team alias")
            target_key = _normalize_id_token(target, "canonical team")
            if not alias_key or not target_key:
                raise ChampionsLeagueQualificationError("team aliases cannot be blank")
            old = seen.get(alias_key)
            if old is not None and old != target_key:
                raise ChampionsLeagueQualificationError(
                    "team alias resolves ambiguously to multiple canonical teams"
                )
            seen[alias_key] = target_key
        if dict(seen) != dict(self._normalized_aliases):
            raise ChampionsLeagueQualificationError("team alias index is inconsistent")

    def resolve(self, value: object, field_name: str = "team") -> str:
        text = _require_text(value, field_name)
        key = _normalize_token(text, field_name)
        target = self._normalized_aliases.get(key)
        resolved = target if target is not None else _normalize_id_token(text, field_name)
        if not resolved:
            raise ChampionsLeagueQualificationError(f"{field_name} has no canonical identity")
        return resolved

    def as_payload(self) -> dict[str, str]:
        self.validate()
        return dict(sorted(self.aliases.items(), key=lambda pair: str(pair[0])))


@dataclass(frozen=True)
class TemporalPartitionWindow:
    partition: str
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_at", _utc(self.start_at, "partition start_at"))
        object.__setattr__(self, "end_at", _utc(self.end_at, "partition end_at"))

    def validate(self) -> None:
        expected = normalize_partition(self.partition)
        if expected != self.partition:
            raise ChampionsLeagueQualificationError("partition window name is not canonical")
        if self.start_at >= self.end_at:
            raise ChampionsLeagueQualificationError("partition window must have positive duration")

    def contains(self, value: datetime) -> bool:
        self.validate()
        timestamp = _utc(value, "partition timestamp")
        return self.start_at <= timestamp < self.end_at

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "partition": self.partition,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
        }


def _coerce_partition_windows(value: Mapping[str, object] | Mapping[str, TemporalPartitionWindow]) -> dict[str, TemporalPartitionWindow]:
    if not isinstance(value, Mapping):
        raise ChampionsLeagueQualificationError("partitions must be an object")
    result: dict[str, TemporalPartitionWindow] = {}
    for key, raw in value.items():
        partition = normalize_partition(key, "partition name")
        if isinstance(raw, TemporalPartitionWindow):
            window = raw
        else:
            item = _mapping(raw, f"{partition} partition")
            window = TemporalPartitionWindow(
                partition=partition,
                start_at=_parse_datetime(item.get("start_at"), f"{partition} start_at"),
                end_at=_parse_datetime(item.get("end_at"), f"{partition} end_at"),
            )
        if window.partition != partition:
            raise ChampionsLeagueQualificationError("partition key and window identity differ")
        if partition in result:
            raise ChampionsLeagueQualificationError("duplicate temporal partition")
        result[partition] = window
    if set(result) != set(PARTITION_ORDER):
        raise ChampionsLeagueQualificationError("all three temporal partitions are required")
    return result


def validate_temporal_partitions(
    partitions: Mapping[str, object],
    rows: Sequence[ChampionsLeagueMatch] | None = None,
) -> dict[str, TemporalPartitionWindow]:
    """Validate ordered, non-overlapping development/calibration/holdout windows."""

    windows = _coerce_partition_windows(partitions)
    for partition in PARTITION_ORDER:
        windows[partition].validate()
    for earlier, later in zip(PARTITION_ORDER, PARTITION_ORDER[1:]):
        if (
            windows[earlier].start_at >= windows[later].start_at
            or windows[earlier].end_at > windows[later].start_at
        ):
            raise ChampionsLeagueQualificationError(
                f"temporal partitions are not chronological: {earlier} and {later}"
            )
    if rows is not None:
        for row in _sequence(rows, "dataset rows"):
            partition = normalize_partition(row.partition or "", "row partition")
            if not windows[partition].contains(row.kickoff_at):
                raise ChampionsLeagueQualificationError(
                    f"row {row.fixture_id} is assigned to the wrong temporal partition"
                )
    return windows


@dataclass(frozen=True)
class ChampionsLeagueMatch:
    """One canonical match row, including the result only when chronologically available."""

    fixture_id: str
    competition_id: str
    season: str
    format_era: str
    stage: str
    round: str
    home_team: str
    away_team: str
    kickoff_at: datetime
    observation_id: str = ""
    home_team_id: str | None = None
    away_team_id: str | None = None
    leg: int | None = None
    aggregate_tie_id: str | None = None
    aggregate_mode: str = AggregateMode.NONE.value
    aggregate_home_score_before: int | None = None
    aggregate_away_score_before: int | None = None
    aggregate_context_available_at: datetime | None = None
    venue_semantics: str = VenueSemantics.HOME_AWAY.value
    neutral_venue: bool | None = None
    venue_id: str | None = None
    result_status: str = ResultStatus.FINAL.value
    home_score: int | None = None
    away_score: int | None = None
    result_available_at: datetime | None = None
    source_available_at: datetime | None = None
    partition: str | None = None
    source_record_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff_at", _utc(self.kickoff_at, "kickoff_at"))
        for field_name in (
            "aggregate_context_available_at",
            "result_available_at",
            "source_available_at",
        ):
            object.__setattr__(self, field_name, _optional_utc(getattr(self, field_name), field_name))
        if not self.observation_id:
            object.__setattr__(self, "observation_id", self.fixture_id)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ChampionsLeagueMatch:
        item = _mapping(raw, "match row")
        competition = item.get("competition_id", item.get("competition"))
        aggregate = item.get("aggregate_context")
        aggregate_map = _mapping(aggregate, "aggregate_context") if aggregate is not None else {}
        result = item.get("result")
        result_map = _mapping(result, "result") if result is not None else {}
        neutral = item.get("neutral_venue")
        venue = item.get("venue_semantics", item.get("home_away_neutral"))
        if venue is None and neutral is not None:
            venue = VenueSemantics.NEUTRAL.value if neutral else VenueSemantics.HOME_AWAY.value
        if venue is None:
            venue = VenueSemantics.HOME_AWAY.value
        return cls(
            fixture_id=item.get("fixture_id", item.get("match_id", "")),
            competition_id=competition,
            season=item.get("season", ""),
            format_era=item.get("format_era", item.get("historical_format_era", "")),
            stage=item.get("stage", ""),
            round=item.get("round", item.get("round_name", "")),
            home_team=item.get("home_team", ""),
            away_team=item.get("away_team", ""),
            kickoff_at=_parse_datetime(
                item.get("kickoff_at", item.get("kickoff", item.get("scheduled_at"))),
                "kickoff_at",
            ),
            observation_id=item.get("observation_id", item.get("row_id", "")),
            home_team_id=item.get("home_team_id"),
            away_team_id=item.get("away_team_id"),
            leg=item.get("leg", aggregate_map.get("leg")),
            aggregate_tie_id=item.get("aggregate_tie_id", aggregate_map.get("tie_id")),
            aggregate_mode=item.get("aggregate_mode", aggregate_map.get("mode", AggregateMode.NONE.value)),
            aggregate_home_score_before=item.get(
                "aggregate_home_score_before", aggregate_map.get("home_score_before")
            ),
            aggregate_away_score_before=item.get(
                "aggregate_away_score_before", aggregate_map.get("away_score_before")
            ),
            aggregate_context_available_at=_optional_parse_datetime(
                item.get("aggregate_context_available_at", aggregate_map.get("available_at")),
                "aggregate_context_available_at",
            ),
            venue_semantics=venue,
            neutral_venue=neutral,
            venue_id=item.get("venue_id"),
            result_status=item.get("result_status", result_map.get("status", ResultStatus.FINAL.value)),
            home_score=item.get("home_score", result_map.get("home_score")),
            away_score=item.get("away_score", result_map.get("away_score")),
            result_available_at=_optional_parse_datetime(
                item.get("result_available_at", result_map.get("available_at")),
                "result_available_at",
            ),
            source_available_at=_optional_parse_datetime(
                item.get("source_available_at", item.get("available_at")),
                "source_available_at",
            ),
            partition=item.get("partition"),
            source_record_id=item.get("source_record_id", item.get("source_id")),
        )

    def resolved_team_ids(self, aliases: TeamAliasRegistry | None = None) -> tuple[str, str]:
        registry = aliases or TeamAliasRegistry()
        home = registry.resolve(self.home_team, "home_team")
        away = registry.resolve(self.away_team, "away_team")
        for name, declared, resolved in (
            ("home_team_id", self.home_team_id, home),
            ("away_team_id", self.away_team_id, away),
        ):
            if declared is not None and resolved != _normalize_id_token(declared, name):
                raise ChampionsLeagueQualificationError(
                    f"{name} does not match its team alias"
                )
        return (
            _normalize_id_token(self.home_team_id, "home_team_id") if self.home_team_id is not None else home,
            _normalize_id_token(self.away_team_id, "away_team_id") if self.away_team_id is not None else away,
        )

    def canonical_fixture_key_for(self, aliases: TeamAliasRegistry | None = None) -> str:
        home, away = self.resolved_team_ids(aliases)
        return "|".join(
            (
                CHAMPIONS_LEAGUE_COMPETITION_ID,
                normalize_season(self.season),
                _normalize_id_token(self.stage, "stage"),
                _normalize_id_token(self.round, "round"),
                _normalize_id_token(self.aggregate_tie_id, "aggregate_tie_id")
                if self.aggregate_tie_id is not None
                else "none",
                str(self.leg) if self.leg is not None else "none",
                home,
                away,
                self.kickoff_at.isoformat(),
            )
        )

    @property
    def canonical_fixture_key(self) -> str:
        return self.canonical_fixture_key_for()

    def _identity_payload(self, aliases: TeamAliasRegistry | None = None) -> dict[str, object]:
        home, away = self.resolved_team_ids(aliases)
        return {
            "fixture_id": self.fixture_id,
            "competition_id": CHAMPIONS_LEAGUE_COMPETITION_ID,
            "season": normalize_season(self.season),
            "format_era": normalize_format_era(self.format_era),
            "stage": _normalize_id_token(self.stage, "stage"),
            "round": _normalize_id_token(self.round, "round"),
            "home_team_id": home,
            "away_team_id": away,
            "kickoff_at": self.kickoff_at.isoformat(),
            "leg": self.leg,
            "aggregate_tie_id": self.aggregate_tie_id,
            "venue_semantics": self.venue_semantics,
        }

    def validate(self, aliases: TeamAliasRegistry | None = None) -> None:
        aliases = aliases or TeamAliasRegistry()
        aliases.validate()
        fixture_id = _require_identity(self.fixture_id, "fixture_id")
        _require_identity(self.observation_id, "observation_id")
        normalize_competition_id(self.competition_id)
        season = normalize_season(self.season)
        era = normalize_format_era(self.format_era)
        if era != expected_format_era(season):
            raise ChampionsLeagueQualificationError(
                f"historical format era does not match season {season}"
            )
        stage = _normalize_id_token(self.stage, "stage")
        round_name = _normalize_id_token(self.round, "round")
        if stage not in {"qualifying", "playoff", "group_stage", "knockout", "league_phase", "final"}:
            raise ChampionsLeagueQualificationError("stage is not a supported Champions League stage")
        if not round_name:
            raise ChampionsLeagueQualificationError("round is required")
        if stage == "league_phase":
            if era != HistoricalFormatEra.CHAMPIONS_LEAGUE_LEAGUE_PHASE_2024_PLUS.value:
                raise ChampionsLeagueQualificationError("league phase is not valid before 2024-25")
            if _MATCHDAY_RE.fullmatch(round_name) is None:
                raise ChampionsLeagueQualificationError("league phase round must be matchday_1 through matchday_8")
        elif stage == "group_stage":
            if era in {
                HistoricalFormatEra.EUROPEAN_CUP_PRE_1992.value,
                HistoricalFormatEra.CHAMPIONS_LEAGUE_LEAGUE_PHASE_2024_PLUS.value,
            }:
                raise ChampionsLeagueQualificationError("group stage is inconsistent with historical format era")
            if not (round_name == "group" or round_name.startswith("group_") or _MATCHDAY_RE.fullmatch(round_name)):
                raise ChampionsLeagueQualificationError("group-stage round is malformed")
        elif stage == "final":
            if round_name != "final":
                raise ChampionsLeagueQualificationError("final stage must use round=final")
        elif stage == "knockout":
            if round_name not in {"round_of_16", "quarter_final", "semi_final", "final"}:
                raise ChampionsLeagueQualificationError("knockout round is invalid")
        elif stage == "qualifying":
            if round_name not in {
                "preliminary", "first_qualifying", "second_qualifying", "third_qualifying", "qualifying_round",
            }:
                raise ChampionsLeagueQualificationError("qualifying round is invalid")
        elif round_name != "playoff":
            raise ChampionsLeagueQualificationError("playoff stage must use round=playoff")

        home, away = self.resolved_team_ids(aliases)
        if home == away:
            raise ChampionsLeagueQualificationError("home and away teams must be distinct after alias resolution")
        if self.home_team_id is not None:
            _require_identity(self.home_team_id, "home_team_id")
        if self.away_team_id is not None:
            _require_identity(self.away_team_id, "away_team_id")
        if self.leg is not None:
            if isinstance(self.leg, bool) or not isinstance(self.leg, int) or self.leg not in (1, 2):
                raise ChampionsLeagueQualificationError("leg must be 1 or 2")
        mode = _enum_value(AggregateMode, self.aggregate_mode, "aggregate_mode")
        for field_name, value in (
            ("aggregate_home_score_before", self.aggregate_home_score_before),
            ("aggregate_away_score_before", self.aggregate_away_score_before),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ChampionsLeagueQualificationError(
                    f"{field_name} must be a non-negative integer"
                )
        if mode == AggregateMode.NONE.value and any(
            value is not None
            for value in (
                self.leg,
                self.aggregate_tie_id,
                self.aggregate_home_score_before,
                self.aggregate_away_score_before,
                self.aggregate_context_available_at,
            )
        ):
            raise ChampionsLeagueQualificationError("aggregate fields require an aggregate mode")
        if mode == AggregateMode.TWO_LEG.value:
            if self.leg not in (1, 2) or not self.aggregate_tie_id:
                raise ChampionsLeagueQualificationError("two-leg context requires tie identity and leg 1 or 2")
            _require_identity(self.aggregate_tie_id, "aggregate_tie_id")
            if self.leg == 1:
                if any(value not in (None, 0) for value in (self.aggregate_home_score_before, self.aggregate_away_score_before)):
                    raise ChampionsLeagueQualificationError("leg 1 cannot have a non-zero prior aggregate")
            elif self.aggregate_home_score_before is None or self.aggregate_away_score_before is None:
                raise ChampionsLeagueQualificationError("leg 2 requires prior aggregate scores")
            if (self.aggregate_home_score_before is None) != (self.aggregate_away_score_before is None):
                raise ChampionsLeagueQualificationError("aggregate scores must be supplied as a pair")
            if self.leg == 2 and self.aggregate_context_available_at is None:
                raise ChampionsLeagueQualificationError(
                    "leg 2 requires aggregate context availability"
                )
        if mode == AggregateMode.SINGLE_LEG.value:
            if self.leg is not None:
                raise ChampionsLeagueQualificationError("single-leg context cannot declare a leg number")
            if self.aggregate_tie_id is None:
                raise ChampionsLeagueQualificationError("single-leg context requires tie identity")
            _require_identity(self.aggregate_tie_id, "aggregate_tie_id")
            if any(value is not None for value in (self.aggregate_home_score_before, self.aggregate_away_score_before)):
                raise ChampionsLeagueQualificationError("single-leg context cannot contain prior aggregate scores")
        if self.aggregate_context_available_at is not None:
            if mode == AggregateMode.NONE.value or self.aggregate_context_available_at > self.kickoff_at:
                raise ChampionsLeagueQualificationError("aggregate context timestamp is inconsistent")
        venue = _enum_value(VenueSemantics, self.venue_semantics, "venue_semantics")
        if self.neutral_venue is not None and _strict_bool(self.neutral_venue, "neutral_venue") != (venue == VenueSemantics.NEUTRAL.value):
            raise ChampionsLeagueQualificationError("neutral_venue disagrees with venue_semantics")
        if round_name == "final" and venue != VenueSemantics.NEUTRAL.value:
            raise ChampionsLeagueQualificationError("Champions League final must preserve neutral-venue semantics")
        if self.venue_id is not None:
            _require_identity(self.venue_id, "venue_id")
        status = _enum_value(ResultStatus, self.result_status, "result_status")
        scores = (self.home_score, self.away_score)
        if status == ResultStatus.FINAL.value:
            if any(isinstance(score, bool) or not isinstance(score, int) or score < 0 for score in scores):
                raise ChampionsLeagueQualificationError("final result requires non-negative integer scores")
            if self.result_available_at is None or self.result_available_at < self.kickoff_at:
                raise ChampionsLeagueQualificationError("final result availability must be at or after kickoff")
            if self.source_available_at is None or self.source_available_at < self.result_available_at:
                raise ChampionsLeagueQualificationError("source availability precedes final result availability")
        elif any(score is not None for score in scores) or self.result_available_at is not None:
            raise ChampionsLeagueQualificationError("non-final result cannot contain scores or result availability")
        if self.source_available_at is None:
            raise ChampionsLeagueQualificationError("source_available_at is required")
        _utc(self.source_available_at, "source_available_at")
        if self.source_record_id is not None:
            _require_identity(self.source_record_id, "source_record_id")
        if self.partition is None:
            raise ChampionsLeagueQualificationError(f"match {fixture_id} has no temporal partition")
        normalize_partition(self.partition, "row partition")

    def result_outcome(self) -> str | None:
        self.validate()
        if self.result_status != ResultStatus.FINAL.value:
            return None
        if self.home_score == self.away_score:
            return "draw"
        return "home" if self.home_score > self.away_score else "away"

    def as_payload(self, aliases: TeamAliasRegistry | None = None) -> dict[str, object]:
        self.validate(aliases)
        return {
            "fixture_id": self.fixture_id,
            "observation_id": self.observation_id,
            "competition_id": CHAMPIONS_LEAGUE_COMPETITION_ID,
            "season": normalize_season(self.season),
            "format_era": normalize_format_era(self.format_era),
            "stage": _normalize_id_token(self.stage, "stage"),
            "round": _normalize_id_token(self.round, "round"),
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_team_id": self.resolved_team_ids(aliases)[0],
            "away_team_id": self.resolved_team_ids(aliases)[1],
            "kickoff_at": self.kickoff_at.isoformat(),
            "leg": self.leg,
            "aggregate_tie_id": self.aggregate_tie_id,
            "aggregate_mode": _enum_value(AggregateMode, self.aggregate_mode, "aggregate_mode"),
            "aggregate_home_score_before": self.aggregate_home_score_before,
            "aggregate_away_score_before": self.aggregate_away_score_before,
            "aggregate_context_available_at": self.aggregate_context_available_at.isoformat() if self.aggregate_context_available_at else None,
            "venue_semantics": _enum_value(VenueSemantics, self.venue_semantics, "venue_semantics"),
            "neutral_venue": self.neutral_venue,
            "venue_id": self.venue_id,
            "result_status": _enum_value(ResultStatus, self.result_status, "result_status"),
            "home_score": self.home_score,
            "away_score": self.away_score,
            "result_available_at": self.result_available_at.isoformat() if self.result_available_at else None,
            "source_available_at": self.source_available_at.isoformat() if self.source_available_at else None,
            "partition": normalize_partition(self.partition or "", "row partition"),
            "source_record_id": self.source_record_id,
        }


# Names used by callers that describe rows as fixtures or observations.
ChampionsLeagueFixture = ChampionsLeagueMatch
ChampionsLeagueRow = ChampionsLeagueMatch


def _validate_aggregate_context(
    rows: Sequence[ChampionsLeagueMatch], aliases: TeamAliasRegistry
) -> None:
    """Validate tie-level context, including the leg-2 historical snapshot."""

    ties: dict[tuple[str, str], list[ChampionsLeagueMatch]] = {}
    for row in rows:
        mode = _enum_value(AggregateMode, row.aggregate_mode, "aggregate_mode")
        if mode == AggregateMode.NONE.value:
            continue
        if row.aggregate_tie_id is None:
            raise ChampionsLeagueQualificationError("aggregate tie identity is required")
        key = (mode, _normalize_id_token(row.aggregate_tie_id, "aggregate_tie_id"))
        ties.setdefault(key, []).append(row)

    for (mode, tie_id), tie_rows in ties.items():
        if mode == AggregateMode.SINGLE_LEG.value:
            if len(tie_rows) != 1:
                raise ChampionsLeagueQualificationError(
                    f"single-leg aggregate tie is duplicated: {tie_id}"
                )
            continue

        if len(tie_rows) != 2:
            raise ChampionsLeagueQualificationError(
                f"two-leg aggregate tie must contain exactly two legs: {tie_id}"
            )
        by_leg = {row.leg: row for row in tie_rows}
        if set(by_leg) != {1, 2}:
            raise ChampionsLeagueQualificationError(
                f"two-leg aggregate tie must contain legs 1 and 2: {tie_id}"
            )
        first, second = by_leg[1], by_leg[2]
        if first.kickoff_at >= second.kickoff_at:
            raise ChampionsLeagueQualificationError(
                f"aggregate legs are not chronological: {tie_id}"
            )
        if (
            _normalize_id_token(first.stage, "stage")
            != _normalize_id_token(second.stage, "stage")
            or _normalize_id_token(first.round, "round")
            != _normalize_id_token(second.round, "round")
        ):
            raise ChampionsLeagueQualificationError(
                f"aggregate legs disagree on stage or round: {tie_id}"
            )
        first_home, first_away = first.resolved_team_ids(aliases)
        second_home, second_away = second.resolved_team_ids(aliases)
        if {first_home, first_away} != {second_home, second_away}:
            raise ChampionsLeagueQualificationError(
                f"aggregate legs contain different teams: {tie_id}"
            )
        if first.result_status != ResultStatus.FINAL.value:
            raise ChampionsLeagueQualificationError(
                f"leg 1 result is unavailable for aggregate tie: {tie_id}"
            )
        if first.result_available_at is None or second.aggregate_context_available_at is None:
            raise ChampionsLeagueQualificationError(
                f"aggregate tie chronology is incomplete: {tie_id}"
            )
        if first.result_available_at > second.aggregate_context_available_at:
            raise ChampionsLeagueQualificationError(
                f"leg 2 aggregate context predates leg 1 result: {tie_id}"
            )
        expected_home, expected_away = (
            (first.home_score, first.away_score)
            if first_home == second_home
            else (first.away_score, first.home_score)
        )
        if (
            second.aggregate_home_score_before != expected_home
            or second.aggregate_away_score_before != expected_away
        ):
            raise ChampionsLeagueQualificationError(
                f"leg 2 aggregate scores conflict with leg 1 result: {tie_id}"
            )


@dataclass(frozen=True)
class ChampionsLeagueDatasetManifest:
    manifest_id: str
    dataset_id: str
    competition_id: str
    schema_version: str
    row_count: int
    seasons: tuple[str, ...]
    partition_counts: Mapping[str, int]
    format_eras: tuple[str, ...]
    source_digests: Mapping[str, str]
    as_of: datetime
    generated_at: datetime
    dataset_sha: str
    manifest_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of, "manifest as_of"))
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "manifest generated_at"))
        object.__setattr__(self, "seasons", _sequence(self.seasons, "manifest seasons"))
        object.__setattr__(self, "format_eras", _sequence(self.format_eras, "manifest format_eras"))
        object.__setattr__(
            self,
            "partition_counts",
            MappingProxyType(dict(_mapping(self.partition_counts, "partition_counts"))),
        )
        object.__setattr__(
            self,
            "source_digests",
            MappingProxyType(dict(_mapping(self.source_digests, "source_digests"))),
        )

    @classmethod
    def build(
        cls,
        *,
        manifest_id: str,
        dataset_id: str,
        rows: Sequence[ChampionsLeagueMatch],
        partitions: Mapping[str, object],
        as_of: datetime,
        generated_at: datetime,
        source_digests: Mapping[str, str] | None = None,
        aliases: TeamAliasRegistry | None = None,
        schema_version: str = CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION,
    ) -> ChampionsLeagueDatasetManifest:
        registry = aliases or TeamAliasRegistry()
        rows = _sequence(rows, "dataset rows")
        windows = validate_temporal_partitions(partitions)
        row_payloads = [row.as_payload(registry) for row in rows]
        partition_counts = {partition: sum(normalize_partition(row.partition or "") == partition for row in rows) for partition in PARTITION_ORDER}
        seasons = tuple(sorted({normalize_season(row.season) for row in rows}))
        eras = tuple(sorted({normalize_format_era(row.format_era) for row in rows}))
        digest = _digest({"rows": sorted(row_payloads, key=lambda item: (str(item["fixture_id"]), str(item["observation_id"])))})
        sources = {"canonical_rows": digest, **dict(source_digests or {})}
        provisional = cls(
            manifest_id=manifest_id,
            dataset_id=dataset_id,
            competition_id=CHAMPIONS_LEAGUE_COMPETITION_ID,
            schema_version=schema_version,
            row_count=len(rows),
            seasons=seasons,
            partition_counts=partition_counts,
            format_eras=eras,
            source_digests=sources,
            as_of=as_of,
            generated_at=generated_at,
            dataset_sha=digest,
            manifest_sha="0" * 64,
        )
        return cls(**{**provisional.__dict__, "manifest_sha": _digest(provisional._payload_without_sha())})

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ChampionsLeagueDatasetManifest:
        item = _mapping(raw, "dataset manifest")
        return cls(
            manifest_id=item.get("manifest_id", ""),
            dataset_id=item.get("dataset_id", ""),
            competition_id=item.get("competition_id", item.get("competition", "")),
            schema_version=item.get("schema_version", ""),
            row_count=item.get("row_count", -1),
            seasons=_sequence(item.get("seasons", ()), "manifest seasons"),
            partition_counts=_mapping(item.get("partition_counts", {}), "partition_counts"),
            format_eras=_sequence(item.get("format_eras", ()), "manifest format_eras"),
            source_digests=_mapping(item.get("source_digests", {}), "source_digests"),
            as_of=_parse_datetime(item.get("as_of"), "manifest as_of"),
            generated_at=_parse_datetime(item.get("generated_at"), "manifest generated_at"),
            dataset_sha=item.get("dataset_sha", ""),
            manifest_sha=item.get("manifest_sha", ""),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "dataset_id": self.dataset_id,
            "competition_id": CHAMPIONS_LEAGUE_COMPETITION_ID,
            "schema_version": self.schema_version,
            "row_count": self.row_count,
            "seasons": list(self.seasons),
            "partition_counts": dict(self.partition_counts),
            "format_eras": list(self.format_eras),
            "source_digests": dict(self.source_digests),
            "as_of": self.as_of.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "dataset_sha": self.dataset_sha.lower(),
        }

    def validate(
        self,
        rows: Sequence[ChampionsLeagueMatch] | None = None,
        aliases: TeamAliasRegistry | None = None,
    ) -> None:
        _require_identity(self.manifest_id, "manifest_id")
        _require_identity(self.dataset_id, "dataset_id")
        normalize_competition_id(self.competition_id)
        _require_text(self.schema_version, "schema_version")
        if self.schema_version != CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION:
            raise ChampionsLeagueQualificationError("unsupported dataset manifest schema version")
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int) or self.row_count < 0:
            raise ChampionsLeagueQualificationError("manifest row_count is invalid")
        if tuple(sorted(self.seasons)) != self.seasons or len(set(self.seasons)) != len(self.seasons):
            raise ChampionsLeagueQualificationError("manifest seasons must be sorted and unique")
        for season in self.seasons:
            normalize_season(season)
        if tuple(sorted(self.format_eras)) != self.format_eras or len(set(self.format_eras)) != len(self.format_eras):
            raise ChampionsLeagueQualificationError("manifest format_eras must be sorted and unique")
        for era in self.format_eras:
            normalize_format_era(era)
        for partition in PARTITION_ORDER:
            count = self.partition_counts.get(partition)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ChampionsLeagueQualificationError("manifest partition counts are invalid")
        if set(self.partition_counts) != set(PARTITION_ORDER):
            raise ChampionsLeagueQualificationError("manifest partition counts are incomplete")
        if not self.source_digests:
            raise ChampionsLeagueQualificationError("manifest requires source digests")
        canonical_rows_digest = self.source_digests.get("canonical_rows")
        if canonical_rows_digest is None:
            raise ChampionsLeagueQualificationError(
                "manifest requires the canonical_rows source digest"
            )
        for name, digest in self.source_digests.items():
            _require_text(name, "source digest name")
            _require_sha(digest, f"source digest {name}")
        _require_sha(self.dataset_sha, "dataset_sha")
        _require_sha(self.manifest_sha, "manifest_sha")
        if self.generated_at < self.as_of:
            raise ChampionsLeagueQualificationError("manifest generated_at precedes as_of")
        if _digest(self._payload_without_sha()) != self.manifest_sha.lower():
            raise ChampionsLeagueQualificationError("dataset manifest digest changed")
        if rows is None:
            return
        registry = aliases or TeamAliasRegistry()
        if self.row_count != len(rows):
            raise ChampionsLeagueQualificationError("manifest row_count does not match dataset")
        payloads = [row.as_payload(registry) for row in rows]
        digest = _digest({"rows": sorted(payloads, key=lambda item: (str(item["fixture_id"]), str(item["observation_id"])))})
        if digest != self.dataset_sha.lower():
            raise ChampionsLeagueQualificationError("dataset digest does not match manifest")
        if canonical_rows_digest.lower() != digest:
            raise ChampionsLeagueQualificationError(
                "manifest canonical_rows digest does not match dataset"
            )
        seasons = tuple(sorted({normalize_season(row.season) for row in rows}))
        eras = tuple(sorted({normalize_format_era(row.format_era) for row in rows}))
        counts = {partition: sum(normalize_partition(row.partition or "") == partition for row in rows) for partition in PARTITION_ORDER}
        if seasons != self.seasons or eras != self.format_eras or counts != dict(self.partition_counts):
            raise ChampionsLeagueQualificationError("manifest metadata does not match dataset rows")
        for row in rows:
            if row.source_available_at is None or row.source_available_at > self.as_of:
                raise ChampionsLeagueQualificationError("dataset contains information after manifest as_of")
            if row.result_status == ResultStatus.FINAL.value and row.result_available_at > self.as_of:
                raise ChampionsLeagueQualificationError("manifest as_of precedes a final result")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "dataset_sha": self.dataset_sha.lower(), "manifest_sha": self.manifest_sha.lower()}


DatasetManifest = ChampionsLeagueDatasetManifest


@dataclass(frozen=True)
class ChampionsLeagueDataset:
    dataset_id: str
    rows: tuple[ChampionsLeagueMatch, ...]
    partitions: Mapping[str, object]
    manifest: ChampionsLeagueDatasetManifest
    aliases: TeamAliasRegistry = field(default_factory=TeamAliasRegistry)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", _sequence(self.rows, "dataset rows"))

    @classmethod
    def from_rows(
        cls,
        *,
        dataset_id: str,
        rows: Sequence[ChampionsLeagueMatch | Mapping[str, object]],
        partitions: Mapping[str, object],
        as_of: datetime,
        generated_at: datetime,
        aliases: TeamAliasRegistry | None = None,
        manifest: ChampionsLeagueDatasetManifest | Mapping[str, object] | None = None,
    ) -> ChampionsLeagueDataset:
        registry = aliases or TeamAliasRegistry()
        raw_rows = _sequence(rows, "dataset rows")
        matches = tuple(
            row if isinstance(row, ChampionsLeagueMatch) else ChampionsLeagueMatch.from_mapping(row)
            for row in raw_rows
        )
        built_manifest = (
            ChampionsLeagueDatasetManifest.build(
                manifest_id=f"{dataset_id}-manifest",
                dataset_id=dataset_id,
                rows=matches,
                partitions=partitions,
                as_of=as_of,
                generated_at=generated_at,
                aliases=registry,
            )
            if manifest is None
            else manifest if isinstance(manifest, ChampionsLeagueDatasetManifest) else ChampionsLeagueDatasetManifest.from_mapping(manifest)
        )
        dataset = cls(dataset_id, matches, partitions, built_manifest, registry)
        dataset.validate()
        return dataset

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], aliases: TeamAliasRegistry | None = None) -> ChampionsLeagueDataset:
        item = _mapping(raw, "dataset")
        registry = aliases or TeamAliasRegistry.from_mapping(item.get("team_aliases", {}))
        rows = tuple(ChampionsLeagueMatch.from_mapping(row) for row in _sequence(item.get("rows", ()), "dataset rows"))
        manifest = ChampionsLeagueDatasetManifest.from_mapping(item.get("manifest", {}))
        dataset = cls(
            dataset_id=item.get("dataset_id", manifest.dataset_id),
            rows=rows,
            partitions=_mapping(item.get("partitions", {}), "partitions"),
            manifest=manifest,
            aliases=registry,
        )
        dataset.validate()
        return dataset

    def validate(self) -> None:
        _require_identity(self.dataset_id, "dataset_id")
        if self.manifest.dataset_id != self.dataset_id:
            raise ChampionsLeagueQualificationError("dataset and manifest identities differ")
        self.aliases.validate()
        for row in self.rows:
            if not isinstance(row, ChampionsLeagueMatch):
                raise ChampionsLeagueQualificationError("dataset rows must be Champions League match objects")
            row.validate(self.aliases)
        validate_temporal_partitions(self.partitions, self.rows)
        _validate_aggregate_context(self.rows, self.aliases)
        seen_observations: dict[str, ChampionsLeagueMatch] = {}
        seen_fixture_ids: dict[str, ChampionsLeagueMatch] = {}
        seen_canonical: dict[str, ChampionsLeagueMatch] = {}
        for row in self.rows:
            for label, seen, key in (
                ("observation", seen_observations, row.observation_id),
                ("fixture", seen_fixture_ids, row.fixture_id),
                ("canonical fixture", seen_canonical, row.canonical_fixture_key_for(self.aliases)),
            ):
                previous = seen.get(key)
                if previous is not None:
                    if label == "fixture" and previous.as_payload(self.aliases) != row.as_payload(self.aliases):
                        raise ChampionsLeagueQualificationError("conflicting observations share a fixture identity")
                    raise ChampionsLeagueQualificationError(f"duplicate {label} observation: {key}")
                seen[key] = row
        self.manifest.validate(self.rows, self.aliases)

    def row_for_fixture(self, fixture_id: str) -> ChampionsLeagueMatch:
        self.validate()
        matches = [row for row in self.rows if row.fixture_id == fixture_id]
        if len(matches) != 1:
            raise ChampionsLeagueQualificationError("fixture identity is missing or ambiguous")
        return matches[0]

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "dataset_id": self.dataset_id,
            "partitions": {key: self.partitions[key].as_payload() if isinstance(self.partitions[key], TemporalPartitionWindow) else dict(self.partitions[key]) for key in sorted(self.partitions)},
            "team_aliases": self.aliases.as_payload(),
            "rows": [row.as_payload(self.aliases) for row in self.rows],
            "manifest": self.manifest.as_payload(),
        }


def validate_dataset_manifest(
    manifest: ChampionsLeagueDatasetManifest | Mapping[str, object],
    rows: Sequence[ChampionsLeagueMatch] | None = None,
    aliases: TeamAliasRegistry | None = None,
) -> None:
    value = manifest if isinstance(manifest, ChampionsLeagueDatasetManifest) else ChampionsLeagueDatasetManifest.from_mapping(manifest)
    value.validate(rows, aliases)


@dataclass(frozen=True)
class FeatureProvenance:
    """Proof that one model feature existed before its prediction timestamp."""

    feature_name: str
    fixture_id: str
    partition: str
    prediction_at: datetime
    source_available_at: datetime
    source_kind: str
    source_digest: str
    generated_at: datetime | None = None
    source_event_at: datetime | None = None
    source_fixture_ids: tuple[str, ...] = ()
    source_partitions: tuple[str, ...] = ()
    market_snapshot_kind: str | None = None
    uses_future_information: bool = False
    is_closing_market: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "prediction_at", _utc(self.prediction_at, "prediction_at"))
        object.__setattr__(self, "source_available_at", _utc(self.source_available_at, "source_available_at"))
        object.__setattr__(self, "generated_at", _optional_utc(self.generated_at, "feature generated_at"))
        object.__setattr__(self, "source_event_at", _optional_utc(self.source_event_at, "source_event_at"))
        object.__setattr__(self, "source_fixture_ids", _sequence(self.source_fixture_ids, "source_fixture_ids"))
        object.__setattr__(self, "source_partitions", _sequence(self.source_partitions, "source_partitions"))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> FeatureProvenance:
        item = _mapping(raw, "feature provenance")
        return cls(
            feature_name=item.get("feature_name", item.get("name", "")),
            fixture_id=item.get("fixture_id", ""),
            partition=item.get("partition", ""),
            prediction_at=_parse_datetime(item.get("prediction_at"), "prediction_at"),
            source_available_at=_parse_datetime(item.get("source_available_at", item.get("available_at")), "source_available_at"),
            source_kind=item.get("source_kind", ""),
            source_digest=item.get("source_digest", item.get("source_sha", "")),
            generated_at=_optional_parse_datetime(item.get("generated_at"), "feature generated_at"),
            source_event_at=_optional_parse_datetime(item.get("source_event_at", item.get("event_at")), "source_event_at"),
            source_fixture_ids=_sequence(
                item.get("source_fixture_ids", item.get("source_fixtures", ())),
                "source_fixture_ids",
            ),
            source_partitions=_sequence(item.get("source_partitions", ()), "source_partitions"),
            market_snapshot_kind=item.get("market_snapshot_kind"),
            uses_future_information=item.get("uses_future_information", False),
            is_closing_market=item.get("is_closing_market", False),
        )

    def validate(self, dataset: ChampionsLeagueDataset, target: ChampionsLeagueMatch | None = None) -> None:
        _reject_forbidden_model_input_name(self.feature_name, "feature_name")
        _require_identity(self.fixture_id, "feature fixture_id")
        target = target or dataset.row_for_fixture(self.fixture_id)
        if target.fixture_id != self.fixture_id:
            raise ChampionsLeagueQualificationError("feature provenance target fixture is ambiguous")
        partition = normalize_partition(self.partition)
        if partition != normalize_partition(target.partition or ""):
            raise ChampionsLeagueQualificationError("feature provenance partition differs from target partition")
        if self.prediction_at >= target.kickoff_at:
            raise ChampionsLeagueQualificationError("prediction timestamp is not before kickoff")
        if self.source_available_at > self.prediction_at:
            raise ChampionsLeagueQualificationError("feature source was not available at prediction time")
        if self.generated_at is not None and self.generated_at > self.prediction_at:
            raise ChampionsLeagueQualificationError("feature was generated after prediction time")
        if self.source_event_at is not None and self.source_event_at > self.prediction_at:
            raise ChampionsLeagueQualificationError("feature source event is in the future")
        if (
            self.source_event_at is not None
            and self.source_event_at > self.source_available_at
        ):
            raise ChampionsLeagueQualificationError(
                "feature source availability predates its source event"
            )
        if not isinstance(self.uses_future_information, bool) or self.uses_future_information:
            raise ChampionsLeagueQualificationError("future information is forbidden in feature provenance")
        if not isinstance(self.is_closing_market, bool) or self.is_closing_market:
            raise ChampionsLeagueQualificationError("closing market information is forbidden in model inputs")
        if self.market_snapshot_kind is not None:
            snapshot_kind = _normalize_id_token(self.market_snapshot_kind, "market_snapshot_kind")
            if snapshot_kind in {"closing", "closing_market", "market_closing"}:
                raise ChampionsLeagueQualificationError("historical closing market information cannot be a model input")
            if snapshot_kind not in {"signal_time", "pre_match", "opening", "none"}:
                raise ChampionsLeagueQualificationError("market snapshot kind is invalid")
        source_kind = _reject_forbidden_model_input_name(self.source_kind, "source_kind")
        if "closing" in _normalize_token(source_kind, "source_kind"):
            raise ChampionsLeagueQualificationError("feature source kind exposes closing market information")
        _require_sha(self.source_digest, "feature source_digest")
        source_ids = tuple(_require_identity(value, "source fixture_id") for value in self.source_fixture_ids)
        source_partitions = tuple(normalize_partition(value, "source partition") for value in self.source_partitions)
        if len(set(source_ids)) != len(source_ids):
            raise ChampionsLeagueQualificationError("feature provenance contains duplicate source fixtures")
        if len(set(source_partitions)) != len(source_partitions):
            raise ChampionsLeagueQualificationError("feature provenance contains duplicate source partitions")
        target_rank = PARTITION_ORDER.index(partition)
        for source_partition in source_partitions:
            if PARTITION_ORDER.index(source_partition) > target_rank:
                raise ChampionsLeagueQualificationError("feature references a later temporal partition")
        for source_id in source_ids:
            source = dataset.row_for_fixture(source_id)
            if source.fixture_id == target.fixture_id or source.kickoff_at >= self.prediction_at:
                raise ChampionsLeagueQualificationError("feature references a future or target fixture")
            if (
                source.source_available_at is not None
                and source.source_available_at > self.source_available_at
            ):
                raise ChampionsLeagueQualificationError(
                    "feature availability predates an underlying source observation"
                )
            source_partition = normalize_partition(source.partition or "")
            if PARTITION_ORDER.index(source_partition) > target_rank:
                raise ChampionsLeagueQualificationError("feature references a later temporal partition")
            if source_partitions and source_partition not in source_partitions:
                raise ChampionsLeagueQualificationError("feature source partition declaration is inconsistent")

    def as_payload(self) -> dict[str, object]:
        self.validate  # keep the object serializable before dataset-bound validation
        return {
            "feature_name": self.feature_name,
            "fixture_id": self.fixture_id,
            "partition": normalize_partition(self.partition),
            "prediction_at": self.prediction_at.isoformat(),
            "source_available_at": self.source_available_at.isoformat(),
            "source_kind": self.source_kind,
            "source_digest": self.source_digest.lower(),
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "source_event_at": self.source_event_at.isoformat() if self.source_event_at else None,
            "source_fixture_ids": list(self.source_fixture_ids),
            "source_partitions": list(self.source_partitions),
            "market_snapshot_kind": self.market_snapshot_kind,
            "uses_future_information": self.uses_future_information,
            "is_closing_market": self.is_closing_market,
        }


PredictionFeatureProvenance = FeatureProvenance


@dataclass(frozen=True)
class ChampionsLeagueModelMetadata:
    model_id: str
    model_version: str
    artifact_sha: str
    dataset_manifest_sha: str
    feature_schema_hash: str
    feature_names: tuple[str, ...]
    training_partition: str
    calibration_partition: str
    holdout_partition: str
    trained_at: datetime
    metadata_sha: str
    input_market_snapshot_kinds: tuple[str, ...] = ()
    uses_future_information: bool = False
    uses_closing_market: bool = False
    metadata_version: str = CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "trained_at", _utc(self.trained_at, "model trained_at"))
        object.__setattr__(self, "feature_names", _sequence(self.feature_names, "feature_names"))
        object.__setattr__(
            self,
            "input_market_snapshot_kinds",
            _sequence(self.input_market_snapshot_kinds, "input_market_snapshot_kinds"),
        )

    @classmethod
    def build(
        cls,
        *,
        model_id: str,
        model_version: str,
        artifact_sha: str,
        dataset_manifest_sha: str,
        feature_names: Sequence[str],
        trained_at: datetime,
        training_partition: str = DatasetPartition.DEVELOPMENT.value,
        calibration_partition: str = DatasetPartition.CALIBRATION.value,
        holdout_partition: str = DatasetPartition.HOLDOUT.value,
        input_market_snapshot_kinds: Sequence[str] = (),
        feature_schema_hash: str | None = None,
    ) -> ChampionsLeagueModelMetadata:
        names = _sequence(feature_names, "feature_names")
        schema = feature_schema_hash or _digest({"feature_names": list(names)})
        provisional = cls(
            model_id=model_id,
            model_version=model_version,
            artifact_sha=artifact_sha,
            dataset_manifest_sha=dataset_manifest_sha,
            feature_schema_hash=schema,
            feature_names=names,
            training_partition=training_partition,
            calibration_partition=calibration_partition,
            holdout_partition=holdout_partition,
            trained_at=trained_at,
            metadata_sha="0" * 64,
            input_market_snapshot_kinds=_sequence(
                input_market_snapshot_kinds, "input_market_snapshot_kinds"
            ),
        )
        return cls(**{**provisional.__dict__, "metadata_sha": _digest(provisional._payload_without_sha())})

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ChampionsLeagueModelMetadata:
        item = _mapping(raw, "model metadata")
        return cls(
            model_id=item.get("model_id", item.get("model_identity", "")),
            model_version=item.get("model_version", ""),
            artifact_sha=item.get("artifact_sha", item.get("model_artifact_sha", "")),
            dataset_manifest_sha=item.get("dataset_manifest_sha", item.get("manifest_sha", "")),
            feature_schema_hash=item.get("feature_schema_hash", ""),
            feature_names=_sequence(item.get("feature_names", ()), "feature_names"),
            training_partition=item.get("training_partition", ""),
            calibration_partition=item.get("calibration_partition", ""),
            holdout_partition=item.get("holdout_partition", ""),
            trained_at=_parse_datetime(item.get("trained_at"), "model trained_at"),
            metadata_sha=item.get("metadata_sha", ""),
            input_market_snapshot_kinds=_sequence(
                item.get("input_market_snapshot_kinds", ()),
                "input_market_snapshot_kinds",
            ),
            uses_future_information=item.get("uses_future_information", False),
            uses_closing_market=item.get("uses_closing_market", False),
            metadata_version=item.get("metadata_version", CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "artifact_sha": self.artifact_sha.lower(),
            "dataset_manifest_sha": self.dataset_manifest_sha.lower(),
            "feature_schema_hash": self.feature_schema_hash.lower(),
            "feature_names": list(self.feature_names),
            "training_partition": self.training_partition,
            "calibration_partition": self.calibration_partition,
            "holdout_partition": self.holdout_partition,
            "trained_at": self.trained_at.isoformat(),
            "input_market_snapshot_kinds": list(self.input_market_snapshot_kinds),
            "uses_future_information": self.uses_future_information,
            "uses_closing_market": self.uses_closing_market,
            "metadata_version": self.metadata_version,
        }

    def validate(self, manifest: ChampionsLeagueDatasetManifest | None = None) -> None:
        _require_identity(self.model_id, "model_id")
        _require_identity(self.model_version, "model_version")
        _require_sha(self.artifact_sha, "model artifact_sha")
        _require_sha(self.dataset_manifest_sha, "dataset_manifest_sha")
        _require_sha(self.feature_schema_hash, "feature_schema_hash")
        if self.metadata_version != CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION:
            raise ChampionsLeagueQualificationError("unsupported model metadata version")
        names = tuple(
            _reject_forbidden_model_input_name(name, "feature name")
            for name in self.feature_names
        )
        if not names:
            raise ChampionsLeagueQualificationError("model metadata requires at least one feature")
        if len(set(names)) != len(names):
            raise ChampionsLeagueQualificationError("model feature names must be unique")
        expected_schema = _digest({"feature_names": list(names)})
        if expected_schema != self.feature_schema_hash.lower():
            raise ChampionsLeagueQualificationError("feature schema hash does not match feature names")
        if (
            normalize_partition(self.training_partition, "training_partition") != DatasetPartition.DEVELOPMENT.value
            or normalize_partition(self.calibration_partition, "calibration_partition") != DatasetPartition.CALIBRATION.value
            or normalize_partition(self.holdout_partition, "holdout_partition") != DatasetPartition.HOLDOUT.value
        ):
            raise ChampionsLeagueQualificationError("model metadata partitions must be development, calibration, holdout")
        if not isinstance(self.uses_future_information, bool) or self.uses_future_information:
            raise ChampionsLeagueQualificationError("model metadata declares future information")
        if not isinstance(self.uses_closing_market, bool) or self.uses_closing_market:
            raise ChampionsLeagueQualificationError("model metadata declares closing market input")
        normalized_kinds = []
        for kind in self.input_market_snapshot_kinds:
            normalized = _normalize_id_token(kind, "input market snapshot kind")
            if normalized in {"closing", "closing_market", "market_closing"}:
                raise ChampionsLeagueQualificationError("model metadata contains a closing market input")
            if normalized not in {"signal_time", "pre_match", "opening", "none"}:
                raise ChampionsLeagueQualificationError("model metadata contains an invalid market input kind")
            if normalized in normalized_kinds:
                raise ChampionsLeagueQualificationError(
                    "model metadata repeats a market input kind"
                )
            normalized_kinds.append(normalized)
        if _digest(self._payload_without_sha()) != self.metadata_sha.lower():
            raise ChampionsLeagueQualificationError("model metadata digest changed")
        if manifest is not None and self.dataset_manifest_sha.lower() != manifest.manifest_sha.lower():
            raise ChampionsLeagueQualificationError("model metadata references a different dataset manifest")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "metadata_sha": self.metadata_sha.lower()}


ModelArtifactMetadata = ChampionsLeagueModelMetadata


def validate_model_metadata(
    metadata: ChampionsLeagueModelMetadata | Mapping[str, object],
    manifest: ChampionsLeagueDatasetManifest | Mapping[str, object] | None = None,
) -> None:
    value = metadata if isinstance(metadata, ChampionsLeagueModelMetadata) else ChampionsLeagueModelMetadata.from_mapping(metadata)
    manifest_value = None
    if manifest is not None:
        manifest_value = manifest if isinstance(manifest, ChampionsLeagueDatasetManifest) else ChampionsLeagueDatasetManifest.from_mapping(manifest)
        manifest_value.validate()
    value.validate(manifest_value)


@dataclass(frozen=True)
class ChampionsLeagueShadowEvidence:
    evidence_id: str
    prediction_id: str
    fixture_id: str
    partition: str
    prediction_at: datetime
    generated_at: datetime
    model_id: str
    model_artifact_sha: str
    dataset_manifest_sha: str
    feature_schema_hash: str
    features: tuple[FeatureProvenance, ...]
    evidence_sha: str
    input_market_snapshot_kind: str | None = None
    no_bet: bool = True
    publication_enabled: bool = False
    real_bet_created: bool = False
    ledger_mutated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "prediction_at", _utc(self.prediction_at, "evidence prediction_at"))
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "evidence generated_at"))
        object.__setattr__(self, "features", _sequence(self.features, "evidence features"))

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        prediction_id: str,
        fixture_id: str,
        partition: str,
        prediction_at: datetime,
        generated_at: datetime,
        model_id: str,
        model_artifact_sha: str,
        dataset_manifest_sha: str,
        feature_schema_hash: str,
        features: Sequence[FeatureProvenance],
        input_market_snapshot_kind: str | None = None,
    ) -> ChampionsLeagueShadowEvidence:
        provisional = cls(
            evidence_id=evidence_id,
            prediction_id=prediction_id,
            fixture_id=fixture_id,
            partition=partition,
            prediction_at=prediction_at,
            generated_at=generated_at,
            model_id=model_id,
            model_artifact_sha=model_artifact_sha,
            dataset_manifest_sha=dataset_manifest_sha,
            feature_schema_hash=feature_schema_hash,
            features=_sequence(features, "evidence features"),
            evidence_sha="0" * 64,
            input_market_snapshot_kind=input_market_snapshot_kind,
        )
        return cls(**{**provisional.__dict__, "evidence_sha": _digest(provisional._payload_without_sha())})

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ChampionsLeagueShadowEvidence:
        item = _mapping(raw, "shadow evidence")
        return cls(
            evidence_id=item.get("evidence_id", ""),
            prediction_id=item.get("prediction_id", ""),
            fixture_id=item.get("fixture_id", item.get("fixture_key", "")),
            partition=item.get("partition", ""),
            prediction_at=_parse_datetime(item.get("prediction_at"), "evidence prediction_at"),
            generated_at=_parse_datetime(item.get("generated_at"), "evidence generated_at"),
            model_id=item.get("model_id", item.get("model_identity", "")),
            model_artifact_sha=item.get("model_artifact_sha", item.get("artifact_sha", "")),
            dataset_manifest_sha=item.get("dataset_manifest_sha", item.get("manifest_sha", "")),
            feature_schema_hash=item.get("feature_schema_hash", ""),
            features=tuple(FeatureProvenance.from_mapping(value) for value in _sequence(item.get("features", ()), "evidence features")),
            evidence_sha=item.get("evidence_sha", ""),
            input_market_snapshot_kind=item.get("input_market_snapshot_kind"),
            no_bet=item.get("no_bet", False),
            publication_enabled=item.get("publication_enabled", True),
            real_bet_created=item.get("real_bet_created", False),
            ledger_mutated=item.get("ledger_mutated", False),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "prediction_id": self.prediction_id,
            "fixture_id": self.fixture_id,
            "partition": self.partition,
            "prediction_at": self.prediction_at.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "model_id": self.model_id,
            "model_artifact_sha": self.model_artifact_sha.lower(),
            "dataset_manifest_sha": self.dataset_manifest_sha.lower(),
            "feature_schema_hash": self.feature_schema_hash.lower(),
            "features": [feature.as_payload() for feature in self.features],
            "input_market_snapshot_kind": self.input_market_snapshot_kind,
            "no_bet": self.no_bet,
            "publication_enabled": self.publication_enabled,
            "real_bet_created": self.real_bet_created,
            "ledger_mutated": self.ledger_mutated,
        }

    def validate(
        self,
        dataset: ChampionsLeagueDataset,
        metadata: ChampionsLeagueModelMetadata,
    ) -> None:
        dataset.validate()
        metadata.validate(dataset.manifest)
        _require_identity(self.evidence_id, "evidence_id")
        _require_identity(self.prediction_id, "prediction_id")
        _require_identity(self.fixture_id, "evidence fixture_id")
        row = dataset.row_for_fixture(self.fixture_id)
        partition = normalize_partition(self.partition)
        if partition != normalize_partition(row.partition or ""):
            raise ChampionsLeagueQualificationError("shadow evidence partition differs from fixture")
        if self.prediction_at >= row.kickoff_at or self.generated_at >= row.kickoff_at:
            raise ChampionsLeagueQualificationError("shadow evidence is not pre-kickoff")
        if self.generated_at < self.prediction_at:
            raise ChampionsLeagueQualificationError("evidence generated_at precedes prediction_at")
        if self.model_id != metadata.model_id:
            raise ChampionsLeagueQualificationError("shadow evidence model identity differs from metadata")
        if self.model_artifact_sha.lower() != metadata.artifact_sha.lower():
            raise ChampionsLeagueQualificationError("shadow evidence model artifact differs from metadata")
        if self.dataset_manifest_sha.lower() != dataset.manifest.manifest_sha.lower():
            raise ChampionsLeagueQualificationError("shadow evidence references a different dataset manifest")
        if self.feature_schema_hash.lower() != metadata.feature_schema_hash.lower():
            raise ChampionsLeagueQualificationError("shadow evidence feature schema differs from metadata")
        if tuple(sorted(feature.feature_name for feature in self.features)) != tuple(sorted(metadata.feature_names)):
            raise ChampionsLeagueQualificationError("shadow evidence feature set differs from model metadata")
        if not isinstance(self.no_bet, bool) or self.no_bet is not True:
            raise ChampionsLeagueQualificationError("shadow evidence requires no_bet=true")
        for name, value in (
            ("publication_enabled", self.publication_enabled),
            ("real_bet_created", self.real_bet_created),
            ("ledger_mutated", self.ledger_mutated),
        ):
            if not isinstance(value, bool) or value is not False:
                raise ChampionsLeagueQualificationError(f"shadow evidence violates {name}=false")
        if self.input_market_snapshot_kind is not None:
            snapshot_kind = _normalize_id_token(
                self.input_market_snapshot_kind, "input_market_snapshot_kind"
            )
            if snapshot_kind in {"closing", "closing_market", "market_closing"}:
                raise ChampionsLeagueQualificationError(
                    "shadow evidence contains closing market input"
                )
            if snapshot_kind not in {"signal_time", "pre_match", "opening", "none"}:
                raise ChampionsLeagueQualificationError(
                    "shadow evidence contains an invalid market input kind"
                )
            metadata_kinds = {
                _normalize_id_token(kind, "input market snapshot kind")
                for kind in metadata.input_market_snapshot_kinds
            }
            if metadata_kinds and snapshot_kind not in metadata_kinds:
                raise ChampionsLeagueQualificationError(
                    "shadow evidence market input kind differs from metadata"
                )
        for feature in self.features:
            if feature.prediction_at != self.prediction_at:
                raise ChampionsLeagueQualificationError("feature and evidence prediction timestamps differ")
            feature.validate(dataset, row)
        if _digest(self._payload_without_sha()) != self.evidence_sha.lower():
            raise ChampionsLeagueQualificationError("shadow evidence digest changed")

    def as_payload(self) -> dict[str, object]:
        return {**self._payload_without_sha(), "evidence_sha": self.evidence_sha.lower()}


ShadowPredictionEvidence = ChampionsLeagueShadowEvidence


def validate_shadow_evidence(
    evidence: Sequence[ChampionsLeagueShadowEvidence | Mapping[str, object]],
    dataset: ChampionsLeagueDataset,
    metadata: ChampionsLeagueModelMetadata,
) -> tuple[ChampionsLeagueShadowEvidence, ...]:
    """Validate independent shadow evidence and reject duplicate observations."""

    raw_evidence = _sequence(evidence, "shadow evidence")
    values = tuple(
        item
        if isinstance(item, ChampionsLeagueShadowEvidence)
        else ChampionsLeagueShadowEvidence.from_mapping(item)
        for item in raw_evidence
    )
    seen_ids: set[str] = set()
    seen_prediction_ids: set[str] = set()
    seen_predictions: set[tuple[str, str]] = set()
    for item in values:
        item.validate(dataset, metadata)
        if item.evidence_id in seen_ids:
            raise ChampionsLeagueQualificationError("duplicate shadow evidence identity")
        if item.prediction_id in seen_prediction_ids:
            raise ChampionsLeagueQualificationError("duplicate shadow prediction identity")
        key = (item.fixture_id, normalize_partition(item.partition))
        if key in seen_predictions:
            raise ChampionsLeagueQualificationError("duplicate shadow observation for fixture partition")
        seen_ids.add(item.evidence_id)
        seen_prediction_ids.add(item.prediction_id)
        seen_predictions.add(key)
    return values


@dataclass(frozen=True)
class QualificationReport:
    accepted: bool
    errors: tuple[str, ...]
    dataset_rows: int = 0
    shadow_evidence_rows: int = 0

    def raise_if_rejected(self) -> None:
        if not self.accepted:
            raise ChampionsLeagueQualificationError("; ".join(self.errors) or "qualification rejected")


def qualify_champions_league(
    dataset: ChampionsLeagueDataset,
    metadata: ChampionsLeagueModelMetadata,
    evidence: Sequence[ChampionsLeagueShadowEvidence | Mapping[str, object]] = (),
) -> QualificationReport:
    """Return a deterministic report; any contract violation is a rejection."""

    errors: list[str] = []
    evidence_count = 0
    try:
        dataset.validate()
        metadata.validate(dataset.manifest)
        validated = validate_shadow_evidence(evidence, dataset, metadata)
        evidence_count = len(validated)
    except ChampionsLeagueQualificationError as exc:
        errors.append(str(exc))
    return QualificationReport(
        accepted=not errors,
        errors=tuple(errors),
        dataset_rows=len(dataset.rows),
        shadow_evidence_rows=evidence_count,
    )


def validate_champions_league_qualification(
    dataset: ChampionsLeagueDataset,
    metadata: ChampionsLeagueModelMetadata,
    evidence: Sequence[ChampionsLeagueShadowEvidence | Mapping[str, object]] = (),
) -> QualificationReport:
    report = qualify_champions_league(dataset, metadata, evidence)
    report.raise_if_rejected()
    return report


validate_dataset = ChampionsLeagueDataset.validate
validate_artifacts = validate_champions_league_qualification


# ---------------------------------------------------------------------------
# Offline candidate qualification
# ---------------------------------------------------------------------------
#
# The historical match/dataset contract above answers whether an input data
# set is coherent.  The following contract answers a different question:
# whether a particular offline candidate is admissible for review.  It is
# deliberately evidence-only.  No field below is inferred from a model file,
# a runtime, or a provider; every identity is supplied and hash-bound by the
# caller.

OFFLINE_QUALIFICATION_CONTRACT_VERSION = (
    "champions-league-offline-qualification-v1"
)
_OFFLINE_ROLES = {"training", "calibration", "final_evaluation"}
_OFFLINE_FORBIDDEN_INPUT_TOKENS = {
    "closing",
    "closing_odds",
    "final_odds",
    "post_match",
    "post_kickoff",
    "future",
}


def _oq_text(value: object, name: str) -> str:
    return _require_text(value, name)


def _oq_identity(value: object, name: str) -> str:
    return _require_identity(value, name)


def _oq_sha(value: object, name: str) -> str:
    return _require_sha(value, name).lower()


def _oq_time(value: object, name: str) -> datetime:
    return _utc(value, name)


def _oq_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ChampionsLeagueQualificationError(f"{name} must be boolean")
    return value


def _oq_nonnegative(value: object, name: str) -> int:
    return _nonnegative_int(value, name)


def _oq_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ChampionsLeagueQualificationError(f"{name} must be numeric") from exc
    if not isfinite(result):
        raise ChampionsLeagueQualificationError(f"{name} must be finite")
    return result


def _oq_sequence(value: object, name: str) -> tuple[object, ...]:
    return _sequence(value, name)


def _oq_component_payload(value: object) -> object:
    if value is None:
        return None
    if hasattr(value, "as_payload"):
        return value.as_payload()
    if isinstance(value, Mapping):
        return dict(value)
    raise ChampionsLeagueQualificationError("qualification component is malformed")


def _oq_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChampionsLeagueQualificationError(f"{name} must be an object")
    return value


def _oq_input_name(value: object, name: str) -> str:
    text = _oq_text(value, name)
    token = _normalize_id_token(text, name)
    if not token or any(
        token == forbidden or token.startswith(f"{forbidden}_")
        for forbidden in _OFFLINE_FORBIDDEN_INPUT_TOKENS
    ):
        raise ChampionsLeagueQualificationError(
            f"{name} contains future or closing-market information"
        )
    return text


@dataclass(frozen=True)
class CandidateArtifactIdentity:
    """Exact identity of the candidate artifact under review."""

    candidate_id: str
    artifact_id: str
    artifact_sha: str
    version: str
    artifact_kind: str
    identity_sha: str

    @classmethod
    def build(
        cls,
        *,
        candidate_id: str,
        artifact_id: str,
        artifact_sha: str,
        version: str,
        artifact_kind: str = "model",
    ) -> "CandidateArtifactIdentity":
        provisional = cls(
            candidate_id=candidate_id,
            artifact_id=artifact_id,
            artifact_sha=artifact_sha,
            version=version,
            artifact_kind=artifact_kind,
            identity_sha="0" * 64,
        )
        return replace(provisional, identity_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "CandidateArtifactIdentity":
        item = _oq_mapping(raw, "candidate artifact identity")
        return cls(
            candidate_id=item.get("candidate_id", ""),
            artifact_id=item.get("artifact_id", item.get("model_artifact_id", "")),
            artifact_sha=item.get("artifact_sha", item.get("model_artifact_sha", "")),
            version=item.get("version", item.get("artifact_version", "")),
            artifact_kind=item.get("artifact_kind", "model"),
            identity_sha=item.get("identity_sha", item.get("candidate_artifact_identity_sha", "")),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "artifact_id": self.artifact_id,
            "artifact_sha": str(self.artifact_sha).lower(),
            "version": self.version,
            "artifact_kind": self.artifact_kind,
        }

    def validate(self) -> None:
        _oq_identity(self.candidate_id, "candidate_id")
        _oq_identity(self.artifact_id, "artifact_id")
        _oq_sha(self.artifact_sha, "candidate artifact_sha")
        _oq_identity(self.version, "candidate artifact version")
        _oq_identity(self.artifact_kind, "candidate artifact kind")
        _oq_sha(self.identity_sha, "candidate artifact identity_sha")
        if _digest(self._payload_without_sha()) != self.identity_sha.lower():
            raise ChampionsLeagueQualificationError(
                "candidate artifact identity digest changed"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "identity_sha": self.identity_sha.lower()}


@dataclass(frozen=True)
class FeatureSchemaIdentity:
    """Hash-bound feature names and schema version used by the candidate."""

    schema_id: str
    schema_version: str
    feature_names: tuple[str, ...]
    schema_sha: str

    @classmethod
    def build(
        cls,
        *,
        schema_id: str,
        schema_version: str,
        feature_names: Sequence[str],
    ) -> "FeatureSchemaIdentity":
        provisional = cls(
            schema_id=schema_id,
            schema_version=schema_version,
            feature_names=tuple(feature_names),
            schema_sha="0" * 64,
        )
        return replace(provisional, schema_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "FeatureSchemaIdentity":
        item = _oq_mapping(raw, "feature schema identity")
        return cls(
            schema_id=item.get("schema_id", item.get("feature_schema_id", "")),
            schema_version=item.get("schema_version", ""),
            feature_names=tuple(
                str(value)
                for value in _oq_sequence(
                    item.get("feature_names", item.get("features", ())),
                    "feature_names",
                )
            ),
            schema_sha=item.get("schema_sha", item.get("feature_schema_hash", "")),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "feature_names": list(self.feature_names),
        }

    def validate(self) -> None:
        _oq_identity(self.schema_id, "feature schema_id")
        _oq_identity(self.schema_version, "feature schema_version")
        if not self.feature_names:
            raise ChampionsLeagueQualificationError("feature schema must declare features")
        names = tuple(_oq_input_name(value, "feature name") for value in self.feature_names)
        if len(set(names)) != len(names):
            raise ChampionsLeagueQualificationError("feature schema contains duplicate features")
        _oq_sha(self.schema_sha, "feature schema_sha")
        if _digest(self._payload_without_sha()) != self.schema_sha.lower():
            raise ChampionsLeagueQualificationError("feature schema identity digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "schema_sha": self.schema_sha.lower()}


@dataclass(frozen=True)
class ApprovedInformationTimeBoundary:
    """The approved as-of boundary against which every input is checked."""

    boundary_id: str
    cutoff_at: datetime
    approved: bool
    approved_by: str
    approved_at: datetime
    policy: str
    boundary_sha: str

    @classmethod
    def build(
        cls,
        *,
        boundary_id: str,
        cutoff_at: datetime,
        approved_by: str,
        approved_at: datetime,
        policy: str,
        approved: bool = True,
    ) -> "ApprovedInformationTimeBoundary":
        provisional = cls(
            boundary_id=boundary_id,
            cutoff_at=cutoff_at,
            approved=approved,
            approved_by=approved_by,
            approved_at=approved_at,
            policy=policy,
            boundary_sha="0" * 64,
        )
        return replace(provisional, boundary_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ApprovedInformationTimeBoundary":
        item = _oq_mapping(raw, "information-time boundary")
        return cls(
            boundary_id=item.get("boundary_id", item.get("information_time_boundary_id", "")),
            cutoff_at=_parse_datetime(
                item.get("cutoff_at", item.get("information_time_cutoff_at")),
                "boundary cutoff_at",
            ),
            approved=item.get("approved", False),
            approved_by=item.get("approved_by", ""),
            approved_at=_parse_datetime(item.get("approved_at"), "boundary approved_at"),
            policy=item.get("policy", item.get("boundary_policy", "")),
            boundary_sha=item.get("boundary_sha", ""),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "boundary_id": self.boundary_id,
            "cutoff_at": self.cutoff_at.isoformat(),
            "approved": self.approved,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat(),
            "policy": self.policy,
        }

    def validate(self) -> None:
        _oq_identity(self.boundary_id, "boundary_id")
        cutoff = _oq_time(self.cutoff_at, "boundary cutoff_at")
        _oq_bool(self.approved, "boundary approved")
        if not self.approved:
            raise ChampionsLeagueQualificationError("information-time boundary is not approved")
        _oq_identity(self.approved_by, "boundary approved_by")
        _oq_time(self.approved_at, "boundary approved_at")
        _oq_text(self.policy, "boundary policy")
        if cutoff.tzinfo is None:
            raise ChampionsLeagueQualificationError("boundary cutoff_at must be timezone-aware")
        _oq_sha(self.boundary_sha, "boundary_sha")
        if _digest(self._payload_without_sha()) != self.boundary_sha.lower():
            raise ChampionsLeagueQualificationError("information-time boundary identity changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "boundary_sha": self.boundary_sha.lower()}


@dataclass(frozen=True)
class TrainingPartitionIdentity:
    """Exact training partition identity; names such as ``train`` are not enough."""

    partition_id: str
    dataset_id: str
    partition_role: str
    partition_sha: str
    start_at: datetime
    end_at: datetime
    row_count: int
    boundary_id: str

    @classmethod
    def build(
        cls,
        *,
        partition_id: str,
        dataset_id: str,
        start_at: datetime,
        end_at: datetime,
        row_count: int,
        boundary_id: str,
        partition_role: str = "training",
    ) -> "TrainingPartitionIdentity":
        provisional = cls(
            partition_id=partition_id,
            dataset_id=dataset_id,
            partition_role=partition_role,
            partition_sha="0" * 64,
            start_at=start_at,
            end_at=end_at,
            row_count=row_count,
            boundary_id=boundary_id,
        )
        return replace(provisional, partition_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "TrainingPartitionIdentity":
        item = _oq_mapping(raw, "training partition identity")
        return cls(
            partition_id=item.get("partition_id", item.get("training_partition_id", "")),
            dataset_id=item.get("dataset_id", ""),
            partition_role=item.get("partition_role", item.get("role", "")),
            partition_sha=item.get("partition_sha", item.get("training_partition_sha", "")),
            start_at=_parse_datetime(item.get("start_at"), "training partition start_at"),
            end_at=_parse_datetime(item.get("end_at"), "training partition end_at"),
            row_count=item.get("row_count", -1),
            boundary_id=item.get("boundary_id", ""),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "partition_id": self.partition_id,
            "dataset_id": self.dataset_id,
            "partition_role": self.partition_role,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "row_count": self.row_count,
            "boundary_id": self.boundary_id,
        }

    def validate(self, boundary: ApprovedInformationTimeBoundary | None = None) -> None:
        _oq_identity(self.partition_id, "training partition_id")
        _oq_identity(self.dataset_id, "training dataset_id")
        role = _normalize_id_token(self.partition_role, "training partition_role")
        if role not in {"training", "development"}:
            raise ChampionsLeagueQualificationError(
                "training partition identity must declare the training role"
            )
        start = _oq_time(self.start_at, "training partition start_at")
        end = _oq_time(self.end_at, "training partition end_at")
        if end <= start:
            raise ChampionsLeagueQualificationError("training partition window is empty")
        _oq_sha(self.partition_sha, "training partition_sha")
        _oq_nonnegative(self.row_count, "training partition row_count")
        if self.row_count == 0:
            raise ChampionsLeagueQualificationError("training partition is empty")
        _oq_identity(self.boundary_id, "training partition boundary_id")
        if boundary is not None and self.boundary_id != boundary.boundary_id:
            raise ChampionsLeagueQualificationError(
                "training partition is bound to a different information-time boundary"
            )
        if boundary is not None and end > boundary.cutoff_at:
            raise ChampionsLeagueQualificationError(
                "training partition extends beyond the approved information-time boundary"
            )
        if _digest(self._payload_without_sha()) != self.partition_sha.lower():
            raise ChampionsLeagueQualificationError("training partition identity changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "partition_sha": self.partition_sha.lower()}


@dataclass(frozen=True)
class CalibrationEvidence:
    """Independent calibration evidence bound to the exact candidate/schema."""

    evidence_id: str
    partition_id: str
    partition_sha: str
    candidate_artifact_id: str
    artifact_sha: str
    feature_schema_sha: str
    boundary_id: str
    provenance_id: str
    metric_name: str
    metric_value: float
    sample_count: int
    passed: bool
    evidence_sha: str
    partition_role: str = "calibration"

    @classmethod
    def build(
        cls,
        *,
        evidence_id: str,
        partition_id: str,
        partition_sha: str,
        candidate_artifact_id: str,
        artifact_sha: str,
        feature_schema_sha: str,
        boundary_id: str,
        provenance_id: str,
        metric_name: str,
        metric_value: float,
        sample_count: int,
        passed: bool = True,
        partition_role: str = "calibration",
    ) -> "CalibrationEvidence":
        provisional = cls(
            evidence_id=evidence_id,
            partition_id=partition_id,
            partition_sha=partition_sha,
            candidate_artifact_id=candidate_artifact_id,
            artifact_sha=artifact_sha,
            feature_schema_sha=feature_schema_sha,
            boundary_id=boundary_id,
            provenance_id=provenance_id,
            metric_name=metric_name,
            metric_value=metric_value,
            sample_count=sample_count,
            passed=passed,
            evidence_sha="0" * 64,
            partition_role=partition_role,
        )
        return replace(provisional, evidence_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "CalibrationEvidence":
        item = _oq_mapping(raw, "calibration evidence")
        return cls(
            evidence_id=item.get("evidence_id", ""),
            partition_id=item.get("partition_id", item.get("calibration_partition_id", "")),
            partition_sha=item.get("partition_sha", item.get("calibration_partition_sha", "")),
            candidate_artifact_id=item.get("candidate_artifact_id", item.get("artifact_id", "")),
            artifact_sha=item.get("artifact_sha", item.get("candidate_artifact_sha", "")),
            feature_schema_sha=item.get("feature_schema_sha", item.get("feature_schema_hash", "")),
            boundary_id=item.get("boundary_id", ""),
            provenance_id=item.get("provenance_id", ""),
            metric_name=item.get("metric_name", ""),
            metric_value=item.get("metric_value", float("nan")),
            sample_count=item.get("sample_count", -1),
            passed=item.get("passed", item.get("calibration_passed", False)),
            evidence_sha=item.get("evidence_sha", ""),
            partition_role=item.get("partition_role", "calibration"),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "partition_id": self.partition_id,
            "partition_sha": str(self.partition_sha).lower(),
            "candidate_artifact_id": self.candidate_artifact_id,
            "artifact_sha": str(self.artifact_sha).lower(),
            "feature_schema_sha": str(self.feature_schema_sha).lower(),
            "boundary_id": self.boundary_id,
            "provenance_id": self.provenance_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "sample_count": self.sample_count,
            "passed": self.passed,
            "partition_role": self.partition_role,
        }

    def validate(
        self,
        candidate: CandidateArtifactIdentity | None = None,
        schema: FeatureSchemaIdentity | None = None,
        boundary: ApprovedInformationTimeBoundary | None = None,
    ) -> None:
        _oq_identity(self.evidence_id, "calibration evidence_id")
        _oq_identity(self.partition_id, "calibration partition_id")
        _oq_sha(self.partition_sha, "calibration partition_sha")
        if _normalize_id_token(self.partition_role, "calibration partition_role") != "calibration":
            raise ChampionsLeagueQualificationError("evidence is not calibration evidence")
        _oq_identity(self.candidate_artifact_id, "calibration candidate_artifact_id")
        _oq_sha(self.artifact_sha, "calibration artifact_sha")
        _oq_sha(self.feature_schema_sha, "calibration feature_schema_sha")
        _oq_identity(self.boundary_id, "calibration boundary_id")
        _oq_identity(self.provenance_id, "calibration provenance_id")
        _oq_text(self.metric_name, "calibration metric_name")
        _oq_float(self.metric_value, "calibration metric_value")
        _oq_nonnegative(self.sample_count, "calibration sample_count")
        if self.sample_count == 0:
            raise ChampionsLeagueQualificationError("calibration evidence has no samples")
        if not _oq_bool(self.passed, "calibration passed"):
            raise ChampionsLeagueQualificationError("calibration evidence did not pass")
        _oq_sha(self.evidence_sha, "calibration evidence_sha")
        if candidate is not None and self.candidate_artifact_id != candidate.artifact_id:
            raise ChampionsLeagueQualificationError("calibration evidence uses a different artifact")
        if candidate is not None and self.artifact_sha.lower() != candidate.artifact_sha.lower():
            raise ChampionsLeagueQualificationError("calibration artifact digest differs from candidate")
        if schema is not None and self.feature_schema_sha.lower() != schema.schema_sha.lower():
            raise ChampionsLeagueQualificationError("calibration feature schema differs from candidate")
        if boundary is not None and self.boundary_id != boundary.boundary_id:
            raise ChampionsLeagueQualificationError("calibration evidence uses a different time boundary")
        if _digest(self._payload_without_sha()) != self.evidence_sha.lower():
            raise ChampionsLeagueQualificationError("calibration evidence digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "evidence_sha": self.evidence_sha.lower()}


@dataclass(frozen=True)
class FinalEvaluationEvidence:
    """Legitimate out-of-sample final evaluation evidence."""

    evidence_id: str
    evaluation_id: str
    partition_id: str
    partition_sha: str
    candidate_artifact_id: str
    artifact_sha: str
    feature_schema_sha: str
    boundary_id: str
    provenance_id: str
    metric_name: str
    metric_value: float
    sample_count: int
    legitimate: bool
    out_of_sample: bool
    held_out: bool
    results_resolved: bool
    result_source_id: str
    evaluated_at: datetime
    evidence_sha: str
    partition_role: str = "final_evaluation"

    @classmethod
    def build(
        cls,
        *,
        evidence_id: str,
        evaluation_id: str,
        partition_id: str,
        partition_sha: str,
        candidate_artifact_id: str,
        artifact_sha: str,
        feature_schema_sha: str,
        boundary_id: str,
        provenance_id: str,
        metric_name: str,
        metric_value: float,
        sample_count: int,
        result_source_id: str,
        evaluated_at: datetime,
        legitimate: bool = True,
        out_of_sample: bool = True,
        held_out: bool = True,
        results_resolved: bool = True,
        partition_role: str = "final_evaluation",
    ) -> "FinalEvaluationEvidence":
        provisional = cls(
            evidence_id=evidence_id,
            evaluation_id=evaluation_id,
            partition_id=partition_id,
            partition_sha=partition_sha,
            candidate_artifact_id=candidate_artifact_id,
            artifact_sha=artifact_sha,
            feature_schema_sha=feature_schema_sha,
            boundary_id=boundary_id,
            provenance_id=provenance_id,
            metric_name=metric_name,
            metric_value=metric_value,
            sample_count=sample_count,
            legitimate=legitimate,
            out_of_sample=out_of_sample,
            held_out=held_out,
            results_resolved=results_resolved,
            result_source_id=result_source_id,
            evaluated_at=evaluated_at,
            evidence_sha="0" * 64,
            partition_role=partition_role,
        )
        return replace(provisional, evidence_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "FinalEvaluationEvidence":
        item = _oq_mapping(raw, "final evaluation evidence")
        return cls(
            evidence_id=item.get("evidence_id", ""),
            evaluation_id=item.get("evaluation_id", ""),
            partition_id=item.get("partition_id", item.get("final_evaluation_partition_id", "")),
            partition_sha=item.get("partition_sha", item.get("final_evaluation_partition_sha", "")),
            candidate_artifact_id=item.get("candidate_artifact_id", item.get("artifact_id", "")),
            artifact_sha=item.get("artifact_sha", item.get("candidate_artifact_sha", "")),
            feature_schema_sha=item.get("feature_schema_sha", item.get("feature_schema_hash", "")),
            boundary_id=item.get("boundary_id", ""),
            provenance_id=item.get("provenance_id", ""),
            metric_name=item.get("metric_name", ""),
            metric_value=item.get("metric_value", float("nan")),
            sample_count=item.get("sample_count", -1),
            legitimate=item.get("legitimate", False),
            out_of_sample=item.get("out_of_sample", False),
            held_out=item.get("held_out", False),
            results_resolved=item.get("results_resolved", False),
            result_source_id=item.get("result_source_id", ""),
            evaluated_at=_parse_datetime(item.get("evaluated_at"), "final evaluation evaluated_at"),
            evidence_sha=item.get("evidence_sha", ""),
            partition_role=item.get("partition_role", "final_evaluation"),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "evaluation_id": self.evaluation_id,
            "partition_id": self.partition_id,
            "partition_sha": str(self.partition_sha).lower(),
            "candidate_artifact_id": self.candidate_artifact_id,
            "artifact_sha": str(self.artifact_sha).lower(),
            "feature_schema_sha": str(self.feature_schema_sha).lower(),
            "boundary_id": self.boundary_id,
            "provenance_id": self.provenance_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "sample_count": self.sample_count,
            "legitimate": self.legitimate,
            "out_of_sample": self.out_of_sample,
            "held_out": self.held_out,
            "results_resolved": self.results_resolved,
            "result_source_id": self.result_source_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "partition_role": self.partition_role,
        }

    def validate(
        self,
        candidate: CandidateArtifactIdentity | None = None,
        schema: FeatureSchemaIdentity | None = None,
        boundary: ApprovedInformationTimeBoundary | None = None,
    ) -> None:
        for value, name in (
            (self.evidence_id, "final evaluation evidence_id"),
            (self.evaluation_id, "evaluation_id"),
            (self.partition_id, "final evaluation partition_id"),
            (self.candidate_artifact_id, "final evaluation candidate_artifact_id"),
            (self.boundary_id, "final evaluation boundary_id"),
            (self.provenance_id, "final evaluation provenance_id"),
            (self.result_source_id, "final evaluation result_source_id"),
        ):
            _oq_identity(value, name)
        _oq_sha(self.partition_sha, "final evaluation partition_sha")
        _oq_sha(self.artifact_sha, "final evaluation artifact_sha")
        _oq_sha(self.feature_schema_sha, "final evaluation feature_schema_sha")
        role = _normalize_id_token(self.partition_role, "final evaluation partition_role")
        if role not in {"final_evaluation", "holdout", "test"}:
            raise ChampionsLeagueQualificationError("evidence is not final-evaluation evidence")
        _oq_text(self.metric_name, "final evaluation metric_name")
        _oq_float(self.metric_value, "final evaluation metric_value")
        _oq_nonnegative(self.sample_count, "final evaluation sample_count")
        if self.sample_count == 0:
            raise ChampionsLeagueQualificationError("final evaluation has no samples")
        for value, name in (
            (self.legitimate, "final evaluation legitimate"),
            (self.out_of_sample, "final evaluation out_of_sample"),
            (self.held_out, "final evaluation held_out"),
            (self.results_resolved, "final evaluation results_resolved"),
        ):
            if _oq_bool(value, name) is not True:
                raise ChampionsLeagueQualificationError(
                    "final evaluation evidence is not legitimate"
                )
        _oq_time(self.evaluated_at, "final evaluation evaluated_at")
        if candidate is not None and self.candidate_artifact_id != candidate.artifact_id:
            raise ChampionsLeagueQualificationError("final evaluation uses a different artifact")
        if candidate is not None and self.artifact_sha.lower() != candidate.artifact_sha.lower():
            raise ChampionsLeagueQualificationError("final evaluation artifact digest differs from candidate")
        if schema is not None and self.feature_schema_sha.lower() != schema.schema_sha.lower():
            raise ChampionsLeagueQualificationError("final evaluation feature schema differs from candidate")
        if boundary is not None and self.boundary_id != boundary.boundary_id:
            raise ChampionsLeagueQualificationError("final evaluation uses a different time boundary")
        _oq_sha(self.evidence_sha, "final evaluation evidence_sha")
        if _digest(self._payload_without_sha()) != self.evidence_sha.lower():
            raise ChampionsLeagueQualificationError("final evaluation evidence digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "evidence_sha": self.evidence_sha.lower()}


@dataclass(frozen=True)
class SourceProvenance:
    """Non-secret provenance for every evidence family."""

    provenance_id: str
    source_name: str
    source_sha: str
    dataset_id: str
    available_at: datetime
    retrieved_at: datetime
    row_count: int
    boundary_id: str
    immutable: bool
    authoritative: bool
    provenance_sha: str
    role: str = "dataset"

    @classmethod
    def build(
        cls,
        *,
        provenance_id: str,
        source_name: str,
        source_sha: str,
        dataset_id: str,
        available_at: datetime,
        retrieved_at: datetime,
        row_count: int,
        boundary_id: str,
        immutable: bool = True,
        authoritative: bool = True,
        role: str = "dataset",
    ) -> "SourceProvenance":
        provisional = cls(
            provenance_id=provenance_id,
            source_name=source_name,
            source_sha=source_sha,
            dataset_id=dataset_id,
            available_at=available_at,
            retrieved_at=retrieved_at,
            row_count=row_count,
            boundary_id=boundary_id,
            immutable=immutable,
            authoritative=authoritative,
            provenance_sha="0" * 64,
            role=role,
        )
        return replace(provisional, provenance_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "SourceProvenance":
        item = _oq_mapping(raw, "source provenance")
        return cls(
            provenance_id=item.get("provenance_id", item.get("source_provenance_id", "")),
            source_name=item.get("source_name", item.get("source", "")),
            source_sha=item.get("source_sha", item.get("dataset_sha", "")),
            dataset_id=item.get("dataset_id", ""),
            available_at=_parse_datetime(item.get("available_at", item.get("as_of")), "source available_at"),
            retrieved_at=_parse_datetime(item.get("retrieved_at"), "source retrieved_at"),
            row_count=item.get("row_count", -1),
            boundary_id=item.get("boundary_id", ""),
            immutable=item.get("immutable", False),
            authoritative=item.get("authoritative", False),
            provenance_sha=item.get("provenance_sha", ""),
            role=item.get("role", "dataset"),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "provenance_id": self.provenance_id,
            "source_name": self.source_name,
            "source_sha": str(self.source_sha).lower(),
            "dataset_id": self.dataset_id,
            "available_at": self.available_at.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "row_count": self.row_count,
            "boundary_id": self.boundary_id,
            "immutable": self.immutable,
            "authoritative": self.authoritative,
            "role": self.role,
        }

    def validate(self, boundary: ApprovedInformationTimeBoundary | None = None) -> None:
        for value, name in (
            (self.provenance_id, "provenance_id"),
            (self.source_name, "source_name"),
            (self.dataset_id, "source dataset_id"),
            (self.boundary_id, "source boundary_id"),
        ):
            _oq_identity(value, name)
        _oq_sha(self.source_sha, "source_sha")
        available = _oq_time(self.available_at, "source available_at")
        retrieved = _oq_time(self.retrieved_at, "source retrieved_at")
        if retrieved < available:
            raise ChampionsLeagueQualificationError("source retrieval predates source availability")
        _oq_nonnegative(self.row_count, "source row_count")
        if self.row_count == 0:
            raise ChampionsLeagueQualificationError("source provenance has no rows")
        _oq_bool(self.immutable, "source immutable")
        _oq_bool(self.authoritative, "source authoritative")
        if not self.immutable or not self.authoritative:
            raise ChampionsLeagueQualificationError("source provenance is not authoritative and immutable")
        _oq_text(self.role, "source role")
        _oq_sha(self.provenance_sha, "provenance_sha")
        if boundary is not None:
            if self.boundary_id != boundary.boundary_id:
                raise ChampionsLeagueQualificationError("source uses a different time boundary")
            if available > boundary.cutoff_at:
                raise ChampionsLeagueQualificationError("source was unavailable at the approved boundary")
        if _digest(self._payload_without_sha()) != self.provenance_sha.lower():
            raise ChampionsLeagueQualificationError("source provenance digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "provenance_sha": self.provenance_sha.lower()}


@dataclass(frozen=True)
class ContaminationPass:
    """Explicit zero-overlap/zero-leakage result for the partition audit."""

    check_id: str
    passed: bool
    checked_at: datetime
    method: str
    training_source_sha: str
    calibration_source_sha: str
    final_evaluation_source_sha: str
    overlap_count: int
    leakage_count: int
    check_sha: str

    @classmethod
    def build(
        cls,
        *,
        check_id: str,
        checked_at: datetime,
        method: str,
        training_source_sha: str,
        calibration_source_sha: str,
        final_evaluation_source_sha: str,
        overlap_count: int = 0,
        leakage_count: int = 0,
        passed: bool = True,
    ) -> "ContaminationPass":
        provisional = cls(
            check_id=check_id,
            passed=passed,
            checked_at=checked_at,
            method=method,
            training_source_sha=training_source_sha,
            calibration_source_sha=calibration_source_sha,
            final_evaluation_source_sha=final_evaluation_source_sha,
            overlap_count=overlap_count,
            leakage_count=leakage_count,
            check_sha="0" * 64,
        )
        return replace(provisional, check_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ContaminationPass":
        item = _oq_mapping(raw, "contamination pass")
        return cls(
            check_id=item.get("check_id", ""),
            passed=item.get("passed", False),
            checked_at=_parse_datetime(item.get("checked_at"), "contamination checked_at"),
            method=item.get("method", ""),
            training_source_sha=item.get("training_source_sha", ""),
            calibration_source_sha=item.get("calibration_source_sha", ""),
            final_evaluation_source_sha=item.get("final_evaluation_source_sha", ""),
            overlap_count=item.get("overlap_count", -1),
            leakage_count=item.get("leakage_count", -1),
            check_sha=item.get("check_sha", ""),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "checked_at": self.checked_at.isoformat(),
            "method": self.method,
            "training_source_sha": str(self.training_source_sha).lower(),
            "calibration_source_sha": str(self.calibration_source_sha).lower(),
            "final_evaluation_source_sha": str(self.final_evaluation_source_sha).lower(),
            "overlap_count": self.overlap_count,
            "leakage_count": self.leakage_count,
        }

    def validate(self) -> None:
        _oq_identity(self.check_id, "contamination check_id")
        if _oq_bool(self.passed, "contamination passed") is not True:
            raise ChampionsLeagueQualificationError("contamination check did not pass")
        _oq_time(self.checked_at, "contamination checked_at")
        _oq_text(self.method, "contamination method")
        for value, name in (
            (self.training_source_sha, "contamination training_source_sha"),
            (self.calibration_source_sha, "contamination calibration_source_sha"),
            (self.final_evaluation_source_sha, "contamination final_evaluation_source_sha"),
        ):
            _oq_sha(value, name)
        if _oq_nonnegative(self.overlap_count, "contamination overlap_count") != 0:
            raise ChampionsLeagueQualificationError("contamination overlap is non-zero")
        if _oq_nonnegative(self.leakage_count, "contamination leakage_count") != 0:
            raise ChampionsLeagueQualificationError("contamination leakage is non-zero")
        _oq_sha(self.check_sha, "contamination check_sha")
        if _digest(self._payload_without_sha()) != self.check_sha.lower():
            raise ChampionsLeagueQualificationError("contamination pass digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "check_sha": self.check_sha.lower()}


@dataclass(frozen=True)
class RuntimeInputs:
    """Exact runtime input contract used by the candidate during replay."""

    runtime_id: str
    runtime_version: str
    candidate_artifact_id: str
    artifact_sha: str
    feature_schema_sha: str
    boundary_id: str
    input_names: tuple[str, ...]
    input_sha: str
    config_sha: str
    provenance_id: str
    runtime_sha: str

    @classmethod
    def build(
        cls,
        *,
        runtime_id: str,
        runtime_version: str,
        candidate_artifact_id: str,
        artifact_sha: str,
        feature_schema_sha: str,
        boundary_id: str,
        input_names: Sequence[str],
        config_sha: str,
        provenance_id: str,
    ) -> "RuntimeInputs":
        names = tuple(input_names)
        input_sha = _digest({"input_names": list(names)})
        provisional = cls(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            candidate_artifact_id=candidate_artifact_id,
            artifact_sha=artifact_sha,
            feature_schema_sha=feature_schema_sha,
            boundary_id=boundary_id,
            input_names=names,
            input_sha=input_sha,
            config_sha=config_sha,
            provenance_id=provenance_id,
            runtime_sha="0" * 64,
        )
        return replace(provisional, runtime_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RuntimeInputs":
        item = _oq_mapping(raw, "runtime inputs")
        return cls(
            runtime_id=item.get("runtime_id", ""),
            runtime_version=item.get("runtime_version", ""),
            candidate_artifact_id=item.get("candidate_artifact_id", item.get("artifact_id", "")),
            artifact_sha=item.get("artifact_sha", ""),
            feature_schema_sha=item.get("feature_schema_sha", item.get("feature_schema_hash", "")),
            boundary_id=item.get("boundary_id", ""),
            input_names=tuple(str(value) for value in _oq_sequence(item.get("input_names", item.get("runtime_input_names", ())), "runtime input_names")),
            input_sha=item.get("input_sha", ""),
            config_sha=item.get("config_sha", ""),
            provenance_id=item.get("provenance_id", item.get("source_provenance_id", "")),
            runtime_sha=item.get("runtime_sha", ""),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "candidate_artifact_id": self.candidate_artifact_id,
            "artifact_sha": str(self.artifact_sha).lower(),
            "feature_schema_sha": str(self.feature_schema_sha).lower(),
            "boundary_id": self.boundary_id,
            "input_names": list(self.input_names),
            "input_sha": str(self.input_sha).lower(),
            "config_sha": str(self.config_sha).lower(),
            "provenance_id": self.provenance_id,
        }

    def validate(
        self,
        candidate: CandidateArtifactIdentity | None = None,
        schema: FeatureSchemaIdentity | None = None,
        boundary: ApprovedInformationTimeBoundary | None = None,
    ) -> None:
        for value, name in (
            (self.runtime_id, "runtime_id"),
            (self.runtime_version, "runtime_version"),
            (self.candidate_artifact_id, "runtime candidate_artifact_id"),
            (self.boundary_id, "runtime boundary_id"),
            (self.provenance_id, "runtime provenance_id"),
        ):
            _oq_identity(value, name)
        _oq_sha(self.artifact_sha, "runtime artifact_sha")
        _oq_sha(self.feature_schema_sha, "runtime feature_schema_sha")
        _oq_sha(self.config_sha, "runtime config_sha")
        names = tuple(_oq_input_name(value, "runtime input name") for value in self.input_names)
        if not names or len(set(names)) != len(names):
            raise ChampionsLeagueQualificationError("runtime inputs must be non-empty and unique")
        _oq_sha(self.input_sha, "runtime input_sha")
        if _digest({"input_names": list(names)}) != self.input_sha.lower():
            raise ChampionsLeagueQualificationError("runtime input identity changed")
        _oq_sha(self.runtime_sha, "runtime_sha")
        if candidate is not None:
            if self.candidate_artifact_id != candidate.artifact_id:
                raise ChampionsLeagueQualificationError("runtime uses a different candidate artifact")
            if self.artifact_sha.lower() != candidate.artifact_sha.lower():
                raise ChampionsLeagueQualificationError("runtime artifact digest differs from candidate")
        if schema is not None:
            if self.feature_schema_sha.lower() != schema.schema_sha.lower():
                raise ChampionsLeagueQualificationError("runtime feature schema differs from candidate")
            if set(names) != set(schema.feature_names):
                raise ChampionsLeagueQualificationError("runtime inputs differ from feature schema")
        if boundary is not None and self.boundary_id != boundary.boundary_id:
            raise ChampionsLeagueQualificationError("runtime uses a different time boundary")
        if _digest(self._payload_without_sha()) != self.runtime_sha.lower():
            raise ChampionsLeagueQualificationError("runtime input identity digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "runtime_sha": self.runtime_sha.lower()}


@dataclass(frozen=True)
class RollbackIdentity:
    """Tested rollback target identity; rollback is part of qualification."""

    rollback_id: str
    target_artifact_id: str
    target_artifact_sha: str
    target_version: str
    config_sha: str
    tested: bool
    tested_at: datetime
    rollback_sha: str

    @classmethod
    def build(
        cls,
        *,
        rollback_id: str,
        target_artifact_id: str,
        target_artifact_sha: str,
        target_version: str,
        config_sha: str,
        tested_at: datetime,
        tested: bool = True,
    ) -> "RollbackIdentity":
        provisional = cls(
            rollback_id=rollback_id,
            target_artifact_id=target_artifact_id,
            target_artifact_sha=target_artifact_sha,
            target_version=target_version,
            config_sha=config_sha,
            tested=tested,
            tested_at=tested_at,
            rollback_sha="0" * 64,
        )
        return replace(provisional, rollback_sha=_digest(provisional._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RollbackIdentity":
        item = _oq_mapping(raw, "rollback identity")
        return cls(
            rollback_id=item.get("rollback_id", ""),
            target_artifact_id=item.get("target_artifact_id", item.get("rollback_target_artifact_id", "")),
            target_artifact_sha=item.get("target_artifact_sha", item.get("rollback_target_artifact_sha", "")),
            target_version=item.get("target_version", item.get("rollback_target_version", "")),
            config_sha=item.get("config_sha", ""),
            tested=item.get("tested", False),
            tested_at=_parse_datetime(item.get("tested_at"), "rollback tested_at"),
            rollback_sha=item.get("rollback_sha", ""),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "rollback_id": self.rollback_id,
            "target_artifact_id": self.target_artifact_id,
            "target_artifact_sha": str(self.target_artifact_sha).lower(),
            "target_version": self.target_version,
            "config_sha": str(self.config_sha).lower(),
            "tested": self.tested,
            "tested_at": self.tested_at.isoformat(),
        }

    def validate(self) -> None:
        for value, name in (
            (self.rollback_id, "rollback_id"),
            (self.target_artifact_id, "rollback target_artifact_id"),
            (self.target_version, "rollback target_version"),
        ):
            _oq_identity(value, name)
        _oq_sha(self.target_artifact_sha, "rollback target_artifact_sha")
        _oq_sha(self.config_sha, "rollback config_sha")
        if _oq_bool(self.tested, "rollback tested") is not True:
            raise ChampionsLeagueQualificationError("rollback identity was not tested")
        _oq_time(self.tested_at, "rollback tested_at")
        _oq_sha(self.rollback_sha, "rollback_sha")
        if _digest(self._payload_without_sha()) != self.rollback_sha.lower():
            raise ChampionsLeagueQualificationError("rollback identity digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "rollback_sha": self.rollback_sha.lower()}


def _oq_coerce(value: object, cls: type[Any]) -> object:
    if value is None or isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        return cls.from_mapping(value)
    return value


def _oq_coerce_many(value: object, cls: type[Any]) -> tuple[object, ...]:
    if value is None:
        return ()
    values = _oq_sequence(value, "qualification evidence")
    return tuple(_oq_coerce(item, cls) for item in values)


@dataclass(frozen=True)
class OfflineQualificationReport:
    """Fail-closed qualification report for one offline Champions League candidate."""

    report_id: str
    competition_id: str
    candidate_artifact_identity: CandidateArtifactIdentity | object | None
    feature_schema_identity: FeatureSchemaIdentity | object | None
    approved_information_time_boundary: ApprovedInformationTimeBoundary | object | None
    training_partition_identity: TrainingPartitionIdentity | object | None
    calibration_evidence: tuple[CalibrationEvidence | object, ...]
    final_evaluation_evidence: tuple[FinalEvaluationEvidence | object, ...]
    source_provenance: tuple[SourceProvenance | object, ...]
    contamination_pass: ContaminationPass | object | None
    runtime_inputs: RuntimeInputs | object | None
    rollback_identity: RollbackIdentity | object | None
    accepted: bool
    errors: tuple[str, ...]
    report_sha: str
    contract_version: str = OFFLINE_QUALIFICATION_CONTRACT_VERSION

    @classmethod
    def create(
        cls,
        *,
        report_id: str,
        candidate_artifact_identity: CandidateArtifactIdentity,
        feature_schema_identity: FeatureSchemaIdentity,
        approved_information_time_boundary: ApprovedInformationTimeBoundary,
        training_partition_identity: TrainingPartitionIdentity,
        calibration_evidence: Sequence[CalibrationEvidence],
        final_evaluation_evidence: Sequence[FinalEvaluationEvidence],
        source_provenance: Sequence[SourceProvenance],
        contamination_pass: ContaminationPass,
        runtime_inputs: RuntimeInputs,
        rollback_identity: RollbackIdentity,
    ) -> "OfflineQualificationReport":
        report = cls(
            report_id=report_id,
            competition_id=CHAMPIONS_LEAGUE_COMPETITION_ID,
            candidate_artifact_identity=candidate_artifact_identity,
            feature_schema_identity=feature_schema_identity,
            approved_information_time_boundary=approved_information_time_boundary,
            training_partition_identity=training_partition_identity,
            calibration_evidence=tuple(calibration_evidence),
            final_evaluation_evidence=tuple(final_evaluation_evidence),
            source_provenance=tuple(source_provenance),
            contamination_pass=contamination_pass,
            runtime_inputs=runtime_inputs,
            rollback_identity=rollback_identity,
            accepted=True,
            errors=(),
            report_sha="0" * 64,
        )
        report._validate_header()
        report._validate_components()
        return replace(report, report_sha=_digest(report._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "OfflineQualificationReport":
        item = _oq_mapping(raw, "offline qualification report")
        return cls(
            report_id=item.get("report_id", ""),
            competition_id=item.get("competition_id", item.get("competition", "")),
            candidate_artifact_identity=_oq_coerce(item.get("candidate_artifact_identity", item.get("candidate_artifact")), CandidateArtifactIdentity),
            feature_schema_identity=_oq_coerce(item.get("feature_schema_identity", item.get("feature_schema")), FeatureSchemaIdentity),
            approved_information_time_boundary=_oq_coerce(item.get("approved_information_time_boundary", item.get("information_time_boundary")), ApprovedInformationTimeBoundary),
            training_partition_identity=_oq_coerce(item.get("training_partition_identity", item.get("training_partition")), TrainingPartitionIdentity),
            calibration_evidence=_oq_coerce_many(item.get("calibration_evidence", ()), CalibrationEvidence),
            final_evaluation_evidence=_oq_coerce_many(item.get("final_evaluation_evidence", item.get("final_evaluation", ())), FinalEvaluationEvidence),
            source_provenance=_oq_coerce_many(item.get("source_provenance", ()), SourceProvenance),
            contamination_pass=_oq_coerce(item.get("contamination_pass"), ContaminationPass),
            runtime_inputs=_oq_coerce(item.get("runtime_inputs"), RuntimeInputs),
            rollback_identity=_oq_coerce(item.get("rollback_identity"), RollbackIdentity),
            accepted=item.get("accepted", False),
            errors=tuple(str(value) for value in _oq_sequence(item.get("errors", ()), "report errors")),
            report_sha=item.get("report_sha", ""),
            contract_version=item.get("contract_version", OFFLINE_QUALIFICATION_CONTRACT_VERSION),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "competition_id": self.competition_id,
            "contract_version": self.contract_version,
            "candidate_artifact_identity": _oq_component_payload(self.candidate_artifact_identity),
            "feature_schema_identity": _oq_component_payload(self.feature_schema_identity),
            "approved_information_time_boundary": _oq_component_payload(self.approved_information_time_boundary),
            "training_partition_identity": _oq_component_payload(self.training_partition_identity),
            "calibration_evidence": [_oq_component_payload(value) for value in self.calibration_evidence],
            "final_evaluation_evidence": [_oq_component_payload(value) for value in self.final_evaluation_evidence],
            "source_provenance": [_oq_component_payload(value) for value in self.source_provenance],
            "contamination_pass": _oq_component_payload(self.contamination_pass),
            "runtime_inputs": _oq_component_payload(self.runtime_inputs),
            "rollback_identity": _oq_component_payload(self.rollback_identity),
            "accepted": self.accepted,
            "errors": list(self.errors),
        }

    def _validate_components(self) -> None:
        candidate = self.candidate_artifact_identity
        schema = self.feature_schema_identity
        boundary = self.approved_information_time_boundary
        training = self.training_partition_identity
        contamination = self.contamination_pass
        runtime = self.runtime_inputs
        rollback = self.rollback_identity
        if not isinstance(candidate, CandidateArtifactIdentity):
            raise ChampionsLeagueQualificationError("candidate artifact identity is required")
        if not isinstance(schema, FeatureSchemaIdentity):
            raise ChampionsLeagueQualificationError("feature schema identity is required")
        if not isinstance(boundary, ApprovedInformationTimeBoundary):
            raise ChampionsLeagueQualificationError("approved information-time boundary is required")
        if not isinstance(training, TrainingPartitionIdentity):
            raise ChampionsLeagueQualificationError("training partition identity is required")
        if not isinstance(contamination, ContaminationPass):
            raise ChampionsLeagueQualificationError("contamination pass is required")
        if not isinstance(runtime, RuntimeInputs):
            raise ChampionsLeagueQualificationError("runtime inputs are required")
        if not isinstance(rollback, RollbackIdentity):
            raise ChampionsLeagueQualificationError("rollback identity is required")
        candidate.validate()
        schema.validate()
        boundary.validate()
        training.validate(boundary)
        if not self.calibration_evidence:
            raise ChampionsLeagueQualificationError("calibration evidence is required")
        if not self.final_evaluation_evidence:
            raise ChampionsLeagueQualificationError("legitimate final-evaluation evidence is required")
        if not self.source_provenance:
            raise ChampionsLeagueQualificationError("source provenance is required")
        provenance = {}
        for item in self.source_provenance:
            if not isinstance(item, SourceProvenance):
                raise ChampionsLeagueQualificationError("source provenance is malformed")
            item.validate(boundary)
            if item.provenance_id in provenance:
                raise ChampionsLeagueQualificationError("duplicate source provenance identity")
            provenance[item.provenance_id] = item
        calibration_ids: set[str] = set()
        for item in self.calibration_evidence:
            if not isinstance(item, CalibrationEvidence):
                raise ChampionsLeagueQualificationError("calibration evidence is malformed")
            item.validate(candidate, schema, boundary)
            if item.evidence_id in calibration_ids:
                raise ChampionsLeagueQualificationError("duplicate calibration evidence identity")
            calibration_ids.add(item.evidence_id)
            if item.provenance_id not in provenance:
                raise ChampionsLeagueQualificationError("calibration evidence lacks source provenance")
        evaluation_ids: set[str] = set()
        calibration_partition_ids = {
            item.partition_id for item in self.calibration_evidence
            if isinstance(item, CalibrationEvidence)
        }
        for item in self.final_evaluation_evidence:
            if not isinstance(item, FinalEvaluationEvidence):
                raise ChampionsLeagueQualificationError("final evaluation evidence is malformed")
            item.validate(candidate, schema, boundary)
            if item.evidence_id in evaluation_ids:
                raise ChampionsLeagueQualificationError("duplicate final evaluation evidence identity")
            evaluation_ids.add(item.evidence_id)
            if item.provenance_id not in provenance:
                raise ChampionsLeagueQualificationError("final evaluation evidence lacks source provenance")
            if item.partition_id == training.partition_id:
                raise ChampionsLeagueQualificationError("final evaluation reuses the training partition")
            if item.partition_id in calibration_partition_ids:
                raise ChampionsLeagueQualificationError("final evaluation reuses the calibration partition")
        contamination.validate()
        known_source_shas = {item.source_sha.lower() for item in provenance.values()}
        for value in (
            contamination.training_source_sha,
            contamination.calibration_source_sha,
            contamination.final_evaluation_source_sha,
        ):
            if value.lower() not in known_source_shas:
                raise ChampionsLeagueQualificationError("contamination pass references unknown source provenance")
        runtime.validate(candidate, schema, boundary)
        if runtime.provenance_id not in provenance:
            raise ChampionsLeagueQualificationError("runtime inputs lack source provenance")
        rollback.validate()
        if rollback.target_artifact_id == candidate.artifact_id:
            raise ChampionsLeagueQualificationError("rollback target must differ from candidate artifact")
        if rollback.target_artifact_sha.lower() == candidate.artifact_sha.lower():
            raise ChampionsLeagueQualificationError("rollback target digest must differ from candidate")

    def _validate_header(self) -> None:
        _oq_identity(self.report_id, "qualification report_id")
        if normalize_competition_id(self.competition_id) != CHAMPIONS_LEAGUE_COMPETITION_ID:
            raise ChampionsLeagueQualificationError("qualification report is for another competition")
        if self.contract_version != OFFLINE_QUALIFICATION_CONTRACT_VERSION:
            raise ChampionsLeagueQualificationError("unsupported offline qualification contract version")

    def validate(self) -> None:
        self._validate_header()
        if not isinstance(self.accepted, bool):
            raise ChampionsLeagueQualificationError("qualification report accepted must be boolean")
        if not isinstance(self.errors, tuple):
            raise ChampionsLeagueQualificationError("qualification report errors must be a tuple")
        _oq_sha(self.report_sha, "qualification report_sha")
        if _digest(self._payload_without_sha()) != self.report_sha.lower():
            raise ChampionsLeagueQualificationError("qualification report digest changed")
        if self.accepted:
            if self.errors:
                raise ChampionsLeagueQualificationError("accepted qualification report contains errors")
            self._validate_components()
        elif not self.errors:
            raise ChampionsLeagueQualificationError("rejected qualification report must contain errors")

    def raise_if_rejected(self) -> None:
        self.validate()
        if not self.accepted:
            raise ChampionsLeagueQualificationError("; ".join(self.errors))

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "report_sha": self.report_sha.lower()}


def _oq_rejected_report(
    *,
    report_id: str,
    error: str,
    candidate_artifact_identity: object = None,
    feature_schema_identity: object = None,
    approved_information_time_boundary: object = None,
    training_partition_identity: object = None,
    calibration_evidence: tuple[object, ...] = (),
    final_evaluation_evidence: tuple[object, ...] = (),
    source_provenance: tuple[object, ...] = (),
    contamination_pass: object = None,
    runtime_inputs: object = None,
    rollback_identity: object = None,
) -> OfflineQualificationReport:
    report = OfflineQualificationReport(
        report_id=report_id,
        competition_id=CHAMPIONS_LEAGUE_COMPETITION_ID,
        candidate_artifact_identity=candidate_artifact_identity,
        feature_schema_identity=feature_schema_identity,
        approved_information_time_boundary=approved_information_time_boundary,
        training_partition_identity=training_partition_identity,
        calibration_evidence=calibration_evidence,
        final_evaluation_evidence=final_evaluation_evidence,
        source_provenance=source_provenance,
        contamination_pass=contamination_pass,
        runtime_inputs=runtime_inputs,
        rollback_identity=rollback_identity,
        accepted=False,
        errors=(error,),
        report_sha="0" * 64,
    )
    return replace(report, report_sha=_digest(report._payload_without_sha()))


def qualify_offline_candidate(
    bundle: Mapping[str, object] | None = None,
    *,
    report_id: str = "champions-league-offline-qualification",
    candidate_artifact_identity: CandidateArtifactIdentity | Mapping[str, object] | None = None,
    feature_schema_identity: FeatureSchemaIdentity | Mapping[str, object] | None = None,
    approved_information_time_boundary: ApprovedInformationTimeBoundary | Mapping[str, object] | None = None,
    training_partition_identity: TrainingPartitionIdentity | Mapping[str, object] | None = None,
    calibration_evidence: Sequence[CalibrationEvidence | Mapping[str, object]] = (),
    final_evaluation_evidence: Sequence[FinalEvaluationEvidence | Mapping[str, object]] = (),
    source_provenance: Sequence[SourceProvenance | Mapping[str, object]] = (),
    contamination_pass: ContaminationPass | Mapping[str, object] | None = None,
    runtime_inputs: RuntimeInputs | Mapping[str, object] | None = None,
    rollback_identity: RollbackIdentity | Mapping[str, object] | None = None,
) -> OfflineQualificationReport:
    """Return a deterministic report; missing or conflicting evidence rejects."""

    try:
        if bundle is not None:
            item = _oq_mapping(bundle, "offline qualification input")
            report_id = item.get("report_id", report_id)
            candidate_artifact_identity = item.get(
                "candidate_artifact_identity",
                item.get("candidate_artifact", candidate_artifact_identity),
            )
            feature_schema_identity = item.get(
                "feature_schema_identity",
                item.get("feature_schema", feature_schema_identity),
            )
            approved_information_time_boundary = item.get(
                "approved_information_time_boundary",
                item.get("information_time_boundary", approved_information_time_boundary),
            )
            training_partition_identity = item.get(
                "training_partition_identity",
                item.get("training_partition", training_partition_identity),
            )
            calibration_evidence = item.get("calibration_evidence", calibration_evidence)
            final_evaluation_evidence = item.get(
                "final_evaluation_evidence",
                item.get("final_evaluation", final_evaluation_evidence),
            )
            source_provenance = item.get("source_provenance", source_provenance)
            contamination_pass = item.get("contamination_pass", contamination_pass)
            runtime_inputs = item.get("runtime_inputs", runtime_inputs)
            rollback_identity = item.get("rollback_identity", rollback_identity)
        report = OfflineQualificationReport(
            report_id=report_id,
            competition_id=CHAMPIONS_LEAGUE_COMPETITION_ID,
            candidate_artifact_identity=_oq_coerce(candidate_artifact_identity, CandidateArtifactIdentity),
            feature_schema_identity=_oq_coerce(feature_schema_identity, FeatureSchemaIdentity),
            approved_information_time_boundary=_oq_coerce(approved_information_time_boundary, ApprovedInformationTimeBoundary),
            training_partition_identity=_oq_coerce(training_partition_identity, TrainingPartitionIdentity),
            calibration_evidence=tuple(_oq_coerce_many(calibration_evidence, CalibrationEvidence)),
            final_evaluation_evidence=tuple(_oq_coerce_many(final_evaluation_evidence, FinalEvaluationEvidence)),
            source_provenance=tuple(_oq_coerce_many(source_provenance, SourceProvenance)),
            contamination_pass=_oq_coerce(contamination_pass, ContaminationPass),
            runtime_inputs=_oq_coerce(runtime_inputs, RuntimeInputs),
            rollback_identity=_oq_coerce(rollback_identity, RollbackIdentity),
            accepted=True,
            errors=(),
            report_sha="0" * 64,
        )
        report._validate_header()
        report._validate_components()
        return replace(report, report_sha=_digest(report._payload_without_sha()))
    except (ChampionsLeagueQualificationError, TypeError, ValueError) as exc:
        return _oq_rejected_report(report_id=report_id, error=str(exc))


def validate_offline_qualification(
    report: OfflineQualificationReport | Mapping[str, object],
) -> OfflineQualificationReport:
    """Validate and require acceptance of a serialized offline report."""

    value = report if isinstance(report, OfflineQualificationReport) else OfflineQualificationReport.from_mapping(report)
    value.raise_if_rejected()
    return value


# Naming aliases make the boundary discoverable to callers that use the
# competition name rather than the generic offline-candidate wording.
ChampionsLeagueOfflineQualificationReport = OfflineQualificationReport
ChampionsLeagueQualificationReport = OfflineQualificationReport
ModelArtifactIdentity = CandidateArtifactIdentity
InformationTimeBoundary = ApprovedInformationTimeBoundary
CalibrationReport = CalibrationEvidence
FinalEvaluationReport = FinalEvaluationEvidence
ProvenanceRecord = SourceProvenance
ContaminationEvidence = ContaminationPass
RuntimeInputIdentity = RuntimeInputs
RollbackArtifactIdentity = RollbackIdentity
qualify_champions_league_offline = qualify_offline_candidate
validate_champions_league_offline_qualification = validate_offline_qualification


__all__ = [
    "ApprovedInformationTimeBoundary",
    "AggregateMode",
    "CalibrationEvidence",
    "CalibrationReport",
    "CHAMPIONS_LEAGUE_COMPETITION_ID",
    "CHAMPIONS_LEAGUE_COMPETITION_NAME",
    "CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION",
    "CandidateArtifactIdentity",
    "ChampionsLeagueDataset",
    "ChampionsLeagueDatasetManifest",
    "ChampionsLeagueFixture",
    "ChampionsLeagueMatch",
    "ChampionsLeagueModelMetadata",
    "ChampionsLeagueOfflineQualificationReport",
    "ChampionsLeagueQualificationReport",
    "ChampionsLeagueQualificationError",
    "ChampionsLeagueRow",
    "ChampionsLeagueShadowEvidence",
    "ContaminationPass",
    "ContaminationEvidence",
    "DatasetManifest",
    "DatasetPartition",
    "FeatureSchemaIdentity",
    "FeatureProvenance",
    "FinalEvaluationEvidence",
    "FinalEvaluationReport",
    "HistoricalFormatEra",
    "InformationTimeBoundary",
    "ModelArtifactIdentity",
    "ModelArtifactMetadata",
    "OFFLINE_QUALIFICATION_CONTRACT_VERSION",
    "OfflineQualificationReport",
    "PARTITION_ORDER",
    "PredictionFeatureProvenance",
    "QualificationReport",
    "ResultStatus",
    "RollbackIdentity",
    "RollbackArtifactIdentity",
    "RuntimeInputs",
    "RuntimeInputIdentity",
    "ProvenanceRecord",
    "SourceProvenance",
    "ShadowPredictionEvidence",
    "TeamAliasRegistry",
    "TemporalPartitionWindow",
    "TrainingPartitionIdentity",
    "VenueSemantics",
    "expected_format_era",
    "normalize_competition_id",
    "normalize_format_era",
    "normalize_partition",
    "normalize_season",
    "qualify_champions_league",
    "qualify_champions_league_offline",
    "qualify_offline_candidate",
    "season_start_year",
    "validate_artifacts",
    "validate_champions_league_qualification",
    "validate_champions_league_offline_qualification",
    "validate_dataset",
    "validate_dataset_manifest",
    "validate_model_metadata",
    "validate_offline_qualification",
    "validate_shadow_evidence",
    "validate_temporal_partitions",
]
