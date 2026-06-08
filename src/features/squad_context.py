"""
Tournament stage context features and squad availability stubs.
Player-level data (FBref/Transfermarkt) integrates here when available.
"""
import pandas as pd


# WM 2026 schedule (host: USA/Canada/Mexico, 48 teams, 12 groups)
# Source: FIFA official schedule (Final confirmed 2026-07-19, New Jersey)
# Note: end bounds use midnight of the *next* day for single-day stages (Final, SF days)
# so that any kickoff time on that date (e.g. 19:30 UTC) is correctly detected.
# Stages are contiguous: June 26 23:59 → group; June 27 00:00 → r32.
_WM2026_STAGES = [
    ("group", pd.Timestamp("2026-06-11"), pd.Timestamp("2026-06-27")),
    ("r32",   pd.Timestamp("2026-06-27"), pd.Timestamp("2026-07-05")),
    ("r16",   pd.Timestamp("2026-07-05"), pd.Timestamp("2026-07-09")),
    ("qf",    pd.Timestamp("2026-07-09"), pd.Timestamp("2026-07-14")),
    ("sf",    pd.Timestamp("2026-07-14"), pd.Timestamp("2026-07-19")),
    ("final", pd.Timestamp("2026-07-19"), pd.Timestamp("2026-07-20")),
]

# UEFA Nations League / EURO stages (approximate)
_EURO2028_STAGES = [
    ("group", pd.Timestamp("2028-06-01"), pd.Timestamp("2028-06-29")),
    ("r16",   pd.Timestamp("2028-06-29"), pd.Timestamp("2028-07-07")),
    ("qf",    pd.Timestamp("2028-07-07"), pd.Timestamp("2028-07-11")),
    ("sf",    pd.Timestamp("2028-07-11"), pd.Timestamp("2028-07-15")),
    ("final", pd.Timestamp("2028-07-18"), pd.Timestamp("2028-07-19")),
]


def tournament_stage_features(
    match_date: pd.Timestamp,
    tournament: str | None = None,
) -> dict[str, float]:
    """
    Encodes tournament stage context.
    In the group stage, draws have asymmetric strategic value;
    knockout rounds eliminate draw-as-safe-result optionality.
    """
    stage = _detect_stage(match_date, tournament)

    is_group = float(stage == "group")
    is_knockout = float(stage in ("r32", "r16", "qf", "sf", "final"))
    is_final = float(stage == "final")

    # In group stage, a draw is more tactically acceptable — slightly increases
    # true draw probability vs. model's pre-match estimate.
    draw_incentive = 0.05 if stage == "group" else 0.0

    return {
        "is_group_stage": is_group,
        "is_knockout": is_knockout,
        "is_final": is_final,
        "draw_incentive": draw_incentive,
    }


def _detect_stage(match_date: pd.Timestamp, tournament: str | None) -> str:
    """Detects WM 2026 stage from date. Falls back to 'unknown'."""
    if tournament and "World Cup" in str(tournament) and "qualif" not in str(tournament).lower():
        for stage, start, end in _WM2026_STAGES:
            if start <= match_date < end:
                return stage
    # For non-WM or unknown dates, try generic detection
    for stage, start, end in _WM2026_STAGES:
        if start <= match_date < end:
            return stage
    return "unknown"


def competition_weight(tournament: str | None) -> float:
    """
    Importance weight of the tournament type.
    Used to scale how much a match influences form lookbacks.
    """
    if not tournament:
        return 0.2
    t = str(tournament).lower()
    if "world cup" in t and "qualif" not in t:
        return 1.0
    if "euro" in t and "qualif" not in t:
        return 0.95
    if "copa" in t and "qualif" not in t:
        return 0.95
    if "nations league" in t or "gold cup" in t or "asian cup" in t or "african cup" in t:
        return 0.85
    if "qualif" in t or "qualification" in t:
        return 0.7
    if "friendly" in t:
        return 0.2
    return 0.65
