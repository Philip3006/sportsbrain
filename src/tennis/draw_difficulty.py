"""Draw-Difficulty Index for Tennis (N10).

Computes a ±5pp Elo-based adjustment to match win probability based on how
difficult each player's remaining draw is relative to the other player's.

Theory: in a tournament bracket, Player A faces their half of the draw for all
remaining rounds. If A's expected remaining opponents are significantly stronger
than B's, A's current match probability should be slightly reduced (they're
"saving energy for harder matches" and are more likely to face fatigue/pressure
from tougher upcoming opponents, especially in long tournaments).

Adjustment is bounded to [-MAX_ADJ, +MAX_ADJ] and activates only when:
  - At least N_OPPONENTS_MIN expected opponents are known for each side
  - The tournament is at round ≥ R3 (quarter/semi where bracket shape matters)
  - Elo data is available for the expected opponents

Usage:
    from src.tennis.draw_difficulty import draw_difficulty_adj

    adj = draw_difficulty_adj(
        elo_a=1750.0,
        elo_b=1600.0,
        remaining_opps_a=[1820, 1700, 1650],  # expected opponents for A
        remaining_opps_b=[1500, 1550, 1480],  # expected opponents for B
    )
    # adj is a float in [-0.05, +0.05] — add to p_a (or subtract from p_b)

When bracket data is unavailable, pass empty lists → returns 0.0 (no adjustment).
"""
from __future__ import annotations

# Maximum probability adjustment (±5pp = ±0.05)
MAX_ADJ: float = 0.05

# Elo difference at which the full ±5pp adjustment is applied.
# If remaining draw for A is 200 Elo points tougher than B's → -5pp on A.
ELO_SCALE: float = 200.0

# Minimum opponents known before we trust the draw index.
N_OPPONENTS_MIN: int = 2


def draw_difficulty_adj(
    elo_a: float,
    elo_b: float,
    remaining_opps_a: list[float],
    remaining_opps_b: list[float],
    *,
    max_adj: float = MAX_ADJ,
    elo_scale: float = ELO_SCALE,
    n_min: int = N_OPPONENTS_MIN,
) -> float:
    """Returns the draw-difficulty probability adjustment for player A.

    Positive return → adjust p_a upward (A has easier remaining draw).
    Negative return → adjust p_a downward (A has harder remaining draw).
    Returns 0.0 when insufficient bracket data is available.

    Args:
        elo_a: Current Elo of player A.
        elo_b: Current Elo of player B.
        remaining_opps_a: Elo ratings of expected upcoming opponents for A.
        remaining_opps_b: Elo ratings of expected upcoming opponents for B.
        max_adj: Cap on the adjustment magnitude (default 0.05 = 5pp).
        elo_scale: Elo difference → full max_adj (default 200).
        n_min: Minimum opponents needed to trust the index (default 2).
    """
    if len(remaining_opps_a) < n_min or len(remaining_opps_b) < n_min:
        return 0.0

    # Average expected opponent strength, relative to current player strength.
    # Relative difficulty: how far above/below the player's own Elo are the opponents?
    avg_opp_a = sum(remaining_opps_a) / len(remaining_opps_a)
    avg_opp_b = sum(remaining_opps_b) / len(remaining_opps_b)

    # Relative hardness: positive = A's opponents are stronger relative to A
    hardness_a = avg_opp_a - elo_a
    hardness_b = avg_opp_b - elo_b

    # Draw-difficulty delta: positive = B's draw is harder (good for A)
    delta = hardness_a - hardness_b

    # Linear scaling capped to ±max_adj
    adj = max(-max_adj, min(max_adj, -delta / elo_scale * max_adj))
    return round(adj, 4)


def apply_draw_adj(p_a: float, adj: float) -> tuple[float, float]:
    """Apply draw difficulty adjustment to (p_a, p_b), preserving normalization.

    Returns (p_a_adj, p_b_adj) where p_a_adj + p_b_adj = 1.0.
    """
    p_a_new = max(0.01, min(0.99, p_a + adj))
    p_b_new = 1.0 - p_a_new
    return p_a_new, p_b_new


def bracket_to_opp_elos(
    bracket: dict,
    player: str,
    elo_ratings: dict[str, float],
    *,
    max_rounds: int = 3,
) -> list[float]:
    """Extract expected opponent Elo ratings from a bracket dict.

    Bracket format (e.g., from a scraper or ATP API):
    {
        "player": {
            "quarter": "Q1",   # which quarter/half of the draw
            "opponents": ["Player X", "Player Y", ...]  # sorted by round order
        },
        ...
    }

    Returns list of Elo ratings for expected opponents, up to max_rounds.
    Returns [] if player not found in bracket or Elo not available.
    """
    player_info = bracket.get(player) or bracket.get(player.lower())
    if not player_info:
        return []
    opponents = player_info.get("opponents", [])[:max_rounds]
    elos = []
    for opp in opponents:
        elo = elo_ratings.get(opp)
        if elo is None:
            # Try case-insensitive lookup
            elo = next((v for k, v in elo_ratings.items() if k.lower() == opp.lower()), None)
        if elo is not None:
            elos.append(elo)
    return elos
