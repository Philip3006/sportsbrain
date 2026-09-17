"""Core contract for the Football odds quote.

The active football source is The Odds API. A missing or invalid quote remains
absent; no alternate source or model-implied quote is promoted.

match_hint-Schema:
    home_team, away_team, sport_key, commence_time, match_id
    bookmakers: bereits geparstes TheOddsAPI-Bookmakers-Dict (optional, spart Quota)
    model_probs: {p_home, p_draw, p_away} is retained only for callers that
    need to describe model context; it is never an odds source.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

_T = TypeVar("_T")


@dataclass
class FootballOddsQuote:
    """Einheitliche Odds-Repräsentation für Football über alle Quellen."""
    home_team: str
    away_team: str
    source: str
    source_tier: int

    # 1X2 (Haupt-Markt — Coverage-Gate hängt daran)
    h2h_home: float = 0.0
    h2h_draw: float = 0.0
    h2h_away: float = 0.0

    # Asian Handicap (halbe Tore — The Odds API: "spreads")
    ah_line: float = 0.0
    ah_home: float = 0.0
    ah_away: float = 0.0

    # Totals O/U (Standard-Line 2.5 + weitere gängige Linien)
    ou_line: float = 2.5
    ou_over: float = 0.0
    ou_under: float = 0.0
    ou15_over: float = 0.0
    ou15_under: float = 0.0
    ou35_over: float = 0.0
    ou35_under: float = 0.0
    # Double Chance
    dc_1x: float = 0.0
    dc_x2: float = 0.0
    dc_12: float = 0.0

    # Both Teams to Score
    btts_yes: float = 0.0
    btts_no: float = 0.0

    bookmaker: str = "consensus"
    bookies_count_1x2: int = 0
    confidence: float = 1.0
    no_bet_flag: bool = False
    ts: datetime = field(default_factory=datetime.utcnow)

    def has_1x2(self) -> bool:
        return self.h2h_home > 1.0 and self.h2h_draw > 1.0 and self.h2h_away > 1.0

    def sane_1x2(self) -> bool:
        if not self.has_1x2():
            return False
        return sanity_1x2(self.h2h_home, self.h2h_draw, self.h2h_away)


def sanity_1x2(h: float, d: float, a: float,
               lo: float = 1.0, hi: float = 1.20) -> bool:
    """3-Weg-Sanity: implied Marginalensumme in [lo, hi]."""
    if not all(1.01 < x < 50.0 for x in (h, d, a)):
        return False
    implied = 1.0 / h + 1.0 / d + 1.0 / a
    return lo < implied <= hi


def sanity_2way(h: float, a: float,
                lo: float = 0.95, hi: float = 1.15) -> bool:
    """2-Weg-Sanity (DC/BTTS — kein Draw)."""
    if not all(1.01 < x < 50.0 for x in (h, a)):
        return False
    return lo <= (1.0 / h + 1.0 / a) <= hi


def canonical_team(name: str) -> str:
    """Leichte Normierung für Team-Name-Matching (Groß/Klein, Satzzeichen)."""
    return name.lower().strip().replace(".", "").replace("-", " ")


class ThreadSafeCache(Generic[_T]):
    """Thread-sicherer Einzelwert-Cache mit TTL."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._value: _T | None = None
        self._ts: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> _T | None:
        with self._lock:
            if self._value is not None and (time.monotonic() - self._ts) < self._ttl:
                return self._value
            return None

    def set(self, value: _T) -> None:
        with self._lock:
            self._value = value
            self._ts = time.monotonic()

    def invalidate(self) -> None:
        with self._lock:
            self._value = None
            self._ts = 0.0


class ThreadSafeDictCache(Generic[_T]):
    """Thread-sicherer Key→Value-Cache mit einheitlichem TTL."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._data: dict[Any, _T] = {}
        self._ts: dict[Any, float] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> _T | None:
        with self._lock:
            if key in self._data and (time.monotonic() - self._ts.get(key, 0.0)) < self._ttl:
                return self._data[key]
            return None

    def set(self, key: Any, value: _T) -> None:
        with self._lock:
            self._data[key] = value
            self._ts[key] = time.monotonic()
