"""Stable contracts shared by the Champions League integrity/replay seam."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


CHAMPIONS_LEAGUE_CODE = "ucl"
CHAMPIONS_LEAGUE_SCHEMA_VERSION = "champions-league-integrity-v1"
MAX_ISSUE_SAMPLES = 100
_LEAGUE_ALIASES = frozenset(
    {
        "ucl",
        "champions league",
        "champions_league",
        "uefa champions league",
        "uefa_champs_league",
        "soccer_uefa_champs_league",
    }
)


class ChampionsLeagueIntegrityError(ValueError):
    """Raised by the fail-closed assertion helper."""


class IntegrityCode(str, Enum):
    MISSING_FIXTURE = "missing_fixture"
    DUPLICATE_FIXTURE = "duplicate_fixture"
    DUPLICATE_RESULT = "duplicate_result"
    DUPLICATE_ODDS = "duplicate_odds"
    ENTITY_MISMATCH = "entity_mismatch"
    TIMEZONE_MISMATCH = "timezone_mismatch"
    KICKOFF_MISMATCH = "kickoff_mismatch"
    MISSING_RESULT = "missing_result"
    MISSING_ODDS = "missing_odds"
    STALE_ODDS = "stale_odds"
    MISSING_BOOKMAKER_PROVENANCE = "missing_bookmaker_provenance"
    MISSING_TIMESTAMP = "missing_timestamp"
    TIMESTAMP_MISMATCH = "timestamp_mismatch"
    MISSING_FEATURE = "missing_feature"
    SERIALIZER_INPUT_MISMATCH = "serializer_input_mismatch"
    STALE_CACHE = "stale_cache"
    POST_KICKOFF_CONTAMINATION = "post_kickoff_contamination"
    MALFORMED_INPUT = "malformed_input"


@dataclass(frozen=True)
class ChampionsLeagueIntegrityPolicy:
    """Explicit replay policy; no production freshness defaults are inferred."""

    as_of: datetime
    max_odds_age_seconds: int
    max_cache_age_seconds: int
    kickoff_tolerance_seconds: int = 0
    post_kickoff_tolerance_seconds: int = 0
    require_results: bool = False
    require_odds: bool = True
    require_features: bool = True
    require_serializer_inputs: bool = True
    require_bookmaker_provenance: bool = True
    max_issue_samples: int = 40

    def validate(self) -> None:
        if (
            not isinstance(self.as_of, datetime)
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
        ):
            raise ChampionsLeagueIntegrityError("policy as_of must be timezone-aware")
        values = (
            ("max_odds_age_seconds", self.max_odds_age_seconds, 1),
            ("max_cache_age_seconds", self.max_cache_age_seconds, 1),
            ("kickoff_tolerance_seconds", self.kickoff_tolerance_seconds, 0),
            ("post_kickoff_tolerance_seconds", self.post_kickoff_tolerance_seconds, 0),
            ("max_issue_samples", self.max_issue_samples, 1),
        )
        for name, value, minimum in values:
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ChampionsLeagueIntegrityError(f"{name} must be an integer >= {minimum}")
        if self.max_issue_samples > MAX_ISSUE_SAMPLES:
            raise ChampionsLeagueIntegrityError(
                f"max_issue_samples must be <= {MAX_ISSUE_SAMPLES}"
            )

    @property
    def as_of_utc(self) -> datetime:
        self.validate()
        return self.as_of.astimezone(timezone.utc)


@dataclass(frozen=True)
class IntegrityIssue:
    code: IntegrityCode
    source: str
    fixture_key: str
    field: str
    detail: str

    def as_payload(self) -> dict[str, str]:
        return {
            "code": IntegrityCode(self.code).value,
            "source": self.source,
            "fixture_key": self.fixture_key,
            "field": self.field,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ChampionsLeagueIntegrityReport:
    """Deterministic integrity result with bounded issue detail."""

    inventory: Mapping[str, int]
    fixture_keys: tuple[str, ...]
    issue_counts: Mapping[str, int]
    issues: tuple[IntegrityIssue, ...]
    max_issue_samples: int

    @property
    def valid(self) -> bool:
        return not self.issue_counts

    @property
    def observability_ready(self) -> bool:
        return self.valid

    def raise_if_invalid(self) -> None:
        if self.issues:
            first = self.issues[0]
            raise ChampionsLeagueIntegrityError(
                f"{first.code.value}: {first.source}.{first.field}: {first.detail}"
            )

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": CHAMPIONS_LEAGUE_SCHEMA_VERSION,
            "league": CHAMPIONS_LEAGUE_CODE,
            "status": "ready" if self.valid else "blocked",
            "observability_ready": self.observability_ready,
            "inventory": dict(self.inventory),
            "fixture_keys": list(self.fixture_keys),
            "issue_counts": dict(self.issue_counts),
            "issues": [issue.as_payload() for issue in self.issues],
            "issue_sample_limit": self.max_issue_samples,
            "offline_replay": True,
            "counts_as_real": False,
            "no_bet": True,
            "publication_enabled": False,
            "activation_state": "disabled",
        }
