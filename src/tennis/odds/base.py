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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol


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


def sanity_ok(a: float, b: float,
              lo: float = 0.95, hi: float = 1.15,
              min_odd: float = 1.01, max_odd: float = 50.0) -> bool:
    """H2H-Sanity: implied Marginale muss zwischen [lo, hi] liegen."""
    if not (min_odd < a < max_odd) or not (min_odd < b < max_odd):
        return False
    implied = (1.0 / a) + (1.0 / b)
    return lo <= implied <= hi
