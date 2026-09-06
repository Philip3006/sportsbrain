"""Kern-Kontrakt für Tennis-Odds-Quellen.

Jede Quelle implementiert `OddsSource.fetch(match_hint)` und gibt eine
OddsQuote zurück (oder None). Der Merger konsumiert diese Quotes und
priorisiert nach source_tier.

source_tier-Konvention:
    1 = Sharp/Exchange (Betfair, Pinnacle)
    2 = EU/US-Retail (TheOddsAPI, Tennis-Explorer Consensus)
    3 = Soft-Retail   (Bwin, Tipico)
    4 = Scrape/Web    (WebSearch-Ensemble, LLM-Parse)
    5 = Modell-Implied (KEIN Betting, nur Display)
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Optional, Protocol, TypeVar

_T = TypeVar("_T")


@dataclass
class OddsQuote:
    """Einheitliche Odds-Repräsentation über alle Quellen."""
    player_a: str
    player_b: str
    h2h_a: float
    h2h_b: float
    source: str                # "betfair" | "tennis_explorer" | "the_odds_api" | ...
    source_tier: int           # 1..5
    bookmaker: str = ""        # "consensus" | "exchange" | konkreter Bookie-Name
    ts: datetime = field(default_factory=datetime.utcnow)
    confidence: float = 1.0    # 0..1 (Sanity-Score, Bookie-Count etc.)
    no_bet_flag: bool = False  # True → Display-only, KEIN Ledger, KEIN Signal
    bookies_count: int = 1     # wie viele einzelne Bookies aggregiert

    def sane(self) -> bool:
        return sanity_ok(self.h2h_a, self.h2h_b)


@dataclass(frozen=True)
class ProviderOutcome:
    """Sanitized direct-provider evidence for the diagnostics-only merge path."""
    provider: str
    attempted: bool
    eligible: bool
    status_class: str
    result: str = "no_quote"
    http_status: int | None = None
    error_class: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "attempted": self.attempted,
            "eligible": self.eligible,
            "status_class": self.status_class,
            "http_status": self.http_status,
            "result": self.result,
            "error_class": self.error_class,
        }


class OddsSource(Protocol):
    """Interface für alle Odds-Quellen."""
    name: str
    tier: int

    def fetch(self, match_hint: dict) -> Optional[OddsQuote]:
        """Holt Quote für einen Match-Hint.

        match_hint: {'player_a', 'player_b', 'tournament', 'commence_time',
                     'sport_key', ...} — Best-Effort, nicht alle Felder Pflicht.
        Return None bei Fehlschlag (Timeout, kein Match gefunden, kein Sanity).
        """
        ...


class ThreadSafeCache(Generic[_T]):
    """Drop-in Ersatz für Module-Level-Globals mit TTL und Lock.

    Motivation: Betfair und Pinnacle hatten jeweils 3–9 Modul-Level-Globals
    ohne Mutex. Bei ThreadPoolExecutor-Merge konnten parallele HTTP-Threads
    denselben Token/Cache-Dict überschreiben ohne Synchronisation.

    Verwendung:
        _session: ThreadSafeCache[str] = ThreadSafeCache(ttl=4*3600)

        token = _session.get()
        if token is None:
            token = _do_login()
            _session.set(token)
    """

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
    """Thread-sicherer Key→Value-Cache mit einheitlichem TTL pro Eintrag."""

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

    def get_all(self) -> list[_T]:
        now = time.monotonic()
        with self._lock:
            return [v for k, v in self._data.items() if (now - self._ts.get(k, 0.0)) < self._ttl]


def sanity_ok(a: float, b: float,
              lo: float = 0.95, hi: float = 1.15,
              min_odd: float = 1.01, max_odd: float = 50.0) -> bool:
    """H2H-Sanity: implied Marginale muss zwischen [lo, hi] liegen."""
    if not (min_odd < a < max_odd) or not (min_odd < b < max_odd):
        return False
    implied = (1.0 / a) + (1.0 / b)
    return lo <= implied <= hi
