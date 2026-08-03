"""N7: Multi-Market Konflikt-Check — markiert semantisch widersprüchliche Signale."""
from __future__ import annotations

from src.betting.value_detector import BetSignal

# Pairs where the first market conflicts with the second.
# Format: (market_a, market_b) — if both are signalled for the same match,
# the lower-EV leg gets no_bet_flag=True and a conflict_reason.
_CONFLICT_PAIRS: list[tuple[str, str]] = [
    # Home win + Under: home wins often involve more goals (home team dominating)
    ("home", "o/u2.5_under"),
    ("home", "o/u3.0_under"),
    # Away win + BTTS No: away teams scoring away while home scores 0 is rare
    ("away", "btts_no"),
    # Draw + Under is generally fine (draw often low-scoring) — not a conflict
    # Over + Under on the same game is an obvious conflict
    ("o/u2.5_over", "o/u2.5_under"),
    ("o/u3.0_over", "o/u3.0_under"),
    ("o/u1.5_over", "o/u1.5_under"),
]


def check_conflicts(signals: list[BetSignal]) -> list[BetSignal]:
    """Mark the weaker leg of conflicting signal pairs with no_bet_flag=True."""
    by_match: dict[str, list[BetSignal]] = {}
    for s in signals:
        key = f"{s.home.lower()}|{s.away.lower()}"
        by_match.setdefault(key, []).append(s)

    for match_sigs in by_match.values():
        if len(match_sigs) < 2:
            continue
        mkt_map: dict[str, BetSignal] = {s.market: s for s in match_sigs}
        for mkt_a, mkt_b in _CONFLICT_PAIRS:
            sig_a = mkt_map.get(mkt_a)
            sig_b = mkt_map.get(mkt_b)
            if sig_a is None or sig_b is None:
                continue
            # Keep higher-EV leg; block the weaker one
            if sig_a.ev >= sig_b.ev:
                sig_b.no_bet_flag = True
                sig_b.conflict_reason = f"conflicts with {mkt_a}"
            else:
                sig_a.no_bet_flag = True
                sig_a.conflict_reason = f"conflicts with {mkt_b}"

    return signals
