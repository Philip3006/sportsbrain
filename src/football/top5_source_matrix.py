"""Explicit provider/source semantics for the five disabled Top-5 adapters.

This is a decision record, not a provider client.  The values mirror the
repository's current configuration and routing code; unresolved live policy
is represented as a recommendation for later approval.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS, Top5LeagueAdapter


@dataclass(frozen=True)
class SourceSemantics:
    league_code: str
    competition_id: str
    sport_key: str
    fixture_source: str
    odds_source: str
    result_source: str
    fallback_sources: tuple[str, ...]
    shadow_authority: str
    live_authority: str
    freshness_policy: str
    failure_classes: tuple[str, ...]
    retry_policy: str
    recommendation: str

    def as_row(self) -> dict[str, object]:
        return {
            "league": self.league_code,
            "competition_id": self.competition_id,
            "sport_key": self.sport_key,
            "fixture_source": self.fixture_source,
            "odds_source": self.odds_source,
            "result_source": self.result_source,
            "fallback_sources": list(self.fallback_sources),
            "shadow_authority": self.shadow_authority,
            "live_authority": self.live_authority,
            "freshness_policy": self.freshness_policy,
            "failure_classes": list(self.failure_classes),
            "retry_policy": self.retry_policy,
            "recommendation": self.recommendation,
        }


def _semantics(adapter: Top5LeagueAdapter, result_code: str) -> SourceSemantics:
    mapping = adapter.config.provider_mapping
    assert mapping is not None
    if result_code in {"D1", "E0"}:
        fallbacks = ("the_odds_api:scores (1 request/match, only if CSV empty)",)
    else:
        fallbacks = ("none in current results_router for this football-data code",)
    return SourceSemantics(
        league_code=adapter.league_code,
        competition_id=mapping.competition_id,
        sport_key=mapping.sport_key,
        fixture_source="the_odds_api:/sports discovery + bulk event/odds request",
        odds_source="the_odds_api:bulk odds; configured markets=h2h,totals,spreads; region=eu",
        result_source=f"football_data:{result_code} CSV via results_router",
        fallback_sources=fallbacks,
        shadow_authority="injected static fixture/odds/result source only; no live authority",
        live_authority="undecided; requires explicit CEO/provider validation and rollout evidence",
        freshness_policy="signal-time contract controls lead window and maximum odds age; closing excluded",
        failure_classes=(
            "unknown or inactive competition",
            "provider timeout/rate limit/empty payload",
            "fixture identity or league mismatch",
            "missing or stale signal-time snapshot",
            "result source empty or unavailable",
        ),
        retry_policy="bounded, explicit retry reason; retry schedule and quota remain configurable",
        recommendation=(
            "Keep disabled. Validate fixture, odds, and result authority separately in shadow before any live decision."
        ),
    )


TOP5_SOURCE_MATRIX: Mapping[str, SourceSemantics] = MappingProxyType(
    {
        adapter.league_code: _semantics(adapter, adapter.config.result_source.rsplit(":", 1)[-1])
        for adapter in TOP5_LEAGUE_ADAPTERS.values()
    }
)
