"""Backtest-Proxies für Hebel 2, 4, 5 aus XLSX-Rohdaten.

Da wir für den historischen Backtest keine TA-Live-Serve-Stats, keine
Historic-Opening-Odds und keine In-Play-Frames haben, bauen wir Proxies aus
dem was tennis-data.co.uk XLSX liefert:

  Hebel 2 (Style)   ← rolling avg games-won-per-set aus W1-W5/L1-L5
                      + std → Klassifikator {aggressor|baseliner|counter}
  Hebel 4 (Sharp)   ← (Avg - B365) / Avg   als Proxy für Line-Movement
                      (B365 tighter als Avg ⇒ Markt hat Odds gedrückt ⇒ Sharp)
  Hebel 5 (Momentum-Carry) ← rolling comeback-rate (won after losing set 1)
                              + finish-rate (won after winning set 1)

Alle drei walk-forward: nur Matches VOR aktuellem Match verwenden.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Hebel 2: Style-Proxy aus Games-per-Set-Statistik
# ---------------------------------------------------------------------------

STYLE_AGGRESSOR = "aggressor"       # dominanter Aufschläger (avg_games hoch)
STYLE_BASELINER = "baseliner"       # ausgeglichen
STYLE_COUNTER = "counter"           # gewinnt enge Matches (avg_diff niedrig, aber wins)
STYLE_UNKNOWN = "unknown"


def _classify_from_games(avg_games_won: float, avg_diff: float, n: int) -> str:
    if n < 10:
        return STYLE_UNKNOWN
    if avg_games_won >= 5.2 and avg_diff >= 1.5:
        return STYLE_AGGRESSOR
    if avg_diff <= 0.8:
        return STYLE_COUNTER
    return STYLE_BASELINER


_MATCHUP: dict[tuple[str, str], float] = {
    (STYLE_AGGRESSOR, STYLE_COUNTER): +0.025,
    (STYLE_COUNTER, STYLE_AGGRESSOR): -0.025,
    (STYLE_AGGRESSOR, STYLE_BASELINER): +0.015,
    (STYLE_BASELINER, STYLE_AGGRESSOR): -0.015,
    (STYLE_BASELINER, STYLE_COUNTER): +0.015,
    (STYLE_COUNTER, STYLE_BASELINER): -0.015,
}


def style_matchup_bias(style_a: str, style_b: str) -> float:
    if style_a == STYLE_UNKNOWN or style_b == STYLE_UNKNOWN:
        return 0.0
    return _MATCHUP.get((style_a, style_b), 0.0)


# ---------------------------------------------------------------------------
# Hebel 4: Sharp-Money-Proxy (nutzt Odds direkt beim Bet-Filter)
# ---------------------------------------------------------------------------

def sharp_signal(b365: float, avg: float) -> int:
    """+1 = Markt backt Player (B365 < Avg, Odds gefallen)
       -1 = Markt gegen Player (B365 > Avg)
        0 = neutral (< 1% Bewegung)
    """
    if not b365 or not avg or b365 <= 1.01 or avg <= 1.01:
        return 0
    move_pct = (avg - b365) / avg
    if move_pct >= 0.01:
        return 1
    if move_pct <= -0.01:
        return -1
    return 0


def sharp_confirms_bet(our_p: float, market_p: float, sharp_dir: int) -> bool:
    """True wenn unsere Edge in dieselbe Richtung wie das Sharp-Signal zeigt."""
    edge = our_p - market_p
    if abs(edge) < 0.02:
        return False
    if edge > 0 and sharp_dir >= 0:
        return True
    if edge < 0 and sharp_dir <= 0:
        return True
    return False


# ---------------------------------------------------------------------------
# Hebel 5: Momentum-Carry (Comeback- + Finish-Rate)
# ---------------------------------------------------------------------------

@dataclass
class PlayerHebelState:
    """Rolling State pro Player für Style, Momentum-Carry (walk-forward)."""
    # Style-Proxy: (games_won, games_played) je Match
    games_hist: deque = field(default_factory=lambda: deque(maxlen=20))
    # Momentum: (won_set1: bool, won_match: bool)
    set1_hist: deque = field(default_factory=lambda: deque(maxlen=30))

    def add_match(
        self,
        my_games: int,
        opp_games: int,
        won_set1: Optional[bool],
        won_match: bool,
    ) -> None:
        if my_games + opp_games > 0:
            self.games_hist.append((my_games, opp_games))
        if won_set1 is not None:
            self.set1_hist.append((won_set1, won_match))

    def style(self) -> str:
        if not self.games_hist:
            return STYLE_UNKNOWN
        n = len(self.games_hist)
        # avg games won per set: sum(my) / sum(sets played)
        total_my = sum(g[0] for g in self.games_hist)
        total_op = sum(g[1] for g in self.games_hist)
        total_sets = 0
        # heuristik: sets played = ceil((games/6)) — schätzung, egal solange konsistent
        # besser: nutzen wir avg_games_per_match / assumed_sets_per_match ~ 2.5
        # aber wir haben games pro MATCH, nicht pro SET aggregiert
        # → avg_games_diff pro match reicht als style-signal
        avg_diff = (total_my - total_op) / n
        avg_games_per_match = total_my / n
        # normiere avg_games pro match auf ~pro-set-basis
        # avg BO3-match hat ~24-26 games total; pro side ~12-13
        # pro set ~ half of that / 2.5 = ~5
        avg_games_won_per_set = avg_games_per_match / 2.5
        return _classify_from_games(avg_games_won_per_set, abs(avg_diff) / 2.5, n)

    def comeback_rate(self) -> float:
        """Won match after losing set 1 (letzten 30 Matches). 0.5 = neutral prior."""
        losses1 = [w_m for (w_s1, w_m) in self.set1_hist if not w_s1]
        if len(losses1) < 3:
            return 0.5
        return sum(1 for w in losses1 if w) / len(losses1)

    def finish_rate(self) -> float:
        """Won match after winning set 1. 0.75 = neutral prior."""
        wins1 = [w_m for (w_s1, w_m) in self.set1_hist if w_s1]
        if len(wins1) < 3:
            return 0.75
        return sum(1 for w in wins1 if w) / len(wins1)


def momentum_carry_bias(
    state_a: PlayerHebelState,
    state_b: PlayerHebelState,
    max_shift: float = 0.02,
) -> float:
    """Bias auf Player A basierend auf mental-toughness Differenz.

    Diff = (finish_a - finish_b) + 0.5 * (comeback_a - comeback_b)
    Return: bias in [-max_shift, +max_shift].
    """
    diff = (state_a.finish_rate() - state_b.finish_rate()) \
         + 0.5 * (state_a.comeback_rate() - state_b.comeback_rate())
    # Diff typischerweise in [-0.5, +0.5] → skaliere auf max_shift
    return max(-max_shift, min(max_shift, diff * max_shift * 2))


# ---------------------------------------------------------------------------
# Aggregator + Public API
# ---------------------------------------------------------------------------

def apply_all_hebels(
    p_a: float,
    *,
    state_a: PlayerHebelState,
    state_b: PlayerHebelState,
    b365_a: Optional[float] = None,
    avg_a: Optional[float] = None,
    max_shift_total: float = 0.05,
) -> tuple[float, dict]:
    """Wendet Hebel 2 + 5 auf Prediction an. Hebel 4 wird SEPARAT als Filter
    im Bet-Placement genutzt (siehe sharp_confirms_bet).

    Return: (adjusted_p_a, debug_dict)
    """
    style_a = state_a.style(); style_b = state_b.style()
    b_style = style_matchup_bias(style_a, style_b)
    b_mom = momentum_carry_bias(state_a, state_b)
    total = b_style + b_mom
    total = max(-max_shift_total, min(max_shift_total, total))
    return (
        max(0.02, min(0.98, p_a + total)),
        {
            "style_a": style_a, "style_b": style_b,
            "bias_style": b_style, "bias_momentum": b_mom, "bias_total": total,
        },
    )
