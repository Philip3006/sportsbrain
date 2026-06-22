"""
Squad merger: suspension overlay, source priority logic, and squad impact features.
Combines Covers.com → Transfermarkt → Wikipedia → default fallback chain.
"""
from __future__ import annotations

import json

import pandas as pd

from .squad_models import (
    PlayerStatus, SquadReport, _SUSPENSIONS_FILE, default_report,
)
from .squad_covers import _fetch_covers_squad, _overlay_sofascore_values
from .squad_transfermarkt import fetch_transfermarkt_squad
from .squad_wikipedia import _fetch_wc_squads_page, _fetch_wikipedia_squad


# ---------------------------------------------------------------------------
# Suspension overlay — manual JSON-based tracking
# ---------------------------------------------------------------------------

def load_suspensions() -> dict[str, list[str]]:
    """Load manually maintained suspension list from data/suspensions.json."""
    if not _SUSPENSIONS_FILE.exists():
        return {}
    try:
        data = json.loads(_SUSPENSIONS_FILE.read_text())
        # Filter out comment keys (starting with _)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return {}


def get_suspended_players(team: str) -> list[str]:
    """Returns list of suspended player names for a team."""
    suspensions = load_suspensions()
    # Try exact match first, then case-insensitive
    if team in suspensions:
        return suspensions[team]
    for k, v in suspensions.items():
        if k.lower() == team.lower():
            return v
    return []


def _apply_suspension_overlay(
    players: list[PlayerStatus],
    team: str,
) -> tuple[list[PlayerStatus], int]:
    """
    Applies manual suspension overlay to player list.
    Returns (updated_players, suspended_count).
    Matches are case-insensitive and support partial name matching
    (e.g. "Rodrygo" matches "Rodrygo Goes").
    """
    suspended = get_suspended_players(team)
    if not suspended:
        return players, 0

    count = 0
    for player in players:
        if any(
            player["name"].lower() in s.lower() or s.lower() in player["name"].lower()
            if isinstance(player, dict)
            else player.name.lower() in s.lower() or s.lower() in player.name.lower()
            for s in suspended
        ):
            if isinstance(player, dict):
                player["available"] = False
                player["status"] = "suspended"
            else:
                player.status = "suspended"
                player.availability = 0.0
            count += 1
    return players, count


def _apply_suspension_overlay_to_statuses(
    players: list[PlayerStatus],
    team: str,
) -> tuple[list[PlayerStatus], int]:
    """
    Applies manual suspension overlay to a list[PlayerStatus].
    Returns (updated_players, suspended_count).
    """
    suspended = get_suspended_players(team)
    if not suspended:
        return players, 0

    count = 0
    for player in players:
        if any(
            player.name.lower() in s.lower() or s.lower() in player.name.lower()
            for s in suspended
        ):
            player.status = "suspended"
            player.availability = 0.0
            count += 1
    return players, count


def squad_report(
    team: str,
    match_date: pd.Timestamp,
    force: bool = False,
) -> SquadReport:
    """
    Returns SquadReport for team at match_date.
    Priority: Covers.com injuries → Transfermarkt → Wikipedia → default_report.
    Suspension overlay (data/suspensions.json) is applied on top of all sources.
    Sofascore market values are overlayed onto the chosen source's player list.
    """
    # Covers.com: structured injury data, scrapable without bot protection
    covers_players = _fetch_covers_squad(team, match_date)
    if covers_players:
        covers_players, susp_count = _apply_suspension_overlay_to_statuses(covers_players, team)
        # For covers: we only have injured/doubtful players, not the full squad.
        # Pad with a proxy full-squad so availability_score reflects reality.
        squad_size = 26
        n_injured = len(covers_players)
        fit_players = [
            PlayerStatus(name=f"fit_{i}", position="unknown", availability=1.0, status="fit")
            for i in range(max(0, squad_size - n_injured))
        ]
        all_players = covers_players + fit_players
        _overlay_sofascore_values(team, all_players)
        return SquadReport(
            team=team,
            report_date=match_date,
            players=all_players,
            data_source="covers",
            suspended_count=susp_count,
        )

    players = fetch_transfermarkt_squad(team, match_date, force=force)
    if players:
        players, susp_count = _apply_suspension_overlay_to_statuses(players, team)
        _overlay_sofascore_values(team, players)
        return SquadReport(
            team=team,
            report_date=match_date,
            players=players,
            data_source="transfermarkt",
            suspended_count=susp_count,
        )

    # TM blocked — try consolidated WC squads page (parse API, no bot blocking)
    wiki_players = _fetch_wc_squads_page(team, match_date)
    if not wiki_players:
        wiki_players = _fetch_wikipedia_squad(team, match_date)
    if wiki_players:
        wiki_players, susp_count = _apply_suspension_overlay_to_statuses(wiki_players, team)
        _overlay_sofascore_values(team, wiki_players)
        return SquadReport(
            team=team,
            report_date=match_date,
            players=wiki_players,
            data_source="wikipedia",
            suspended_count=susp_count,
        )

    # Even for default reports, note any known suspensions
    susp_count = len(get_suspended_players(team))
    report = default_report(team, match_date)
    report.suspended_count = susp_count
    return report


# ---------------------------------------------------------------------------
# Squad impact features for the model
# ---------------------------------------------------------------------------

# Position-weighted impact (Lever 3 proxy without per-player market values).
# Rationale: GK losses hurt more than outfield since they're rarely replaced 1:1.
# DEF/MID slightly above FWD because tournament defenses rely on chemistry.
_POSITION_IMPACT_WEIGHTS = {
    "GK":  1.30, "Goalkeeper": 1.30,
    "DEF": 1.05, "Defender": 1.05, "Centre-Back": 1.05, "Left-Back": 1.05,
                  "Right-Back": 1.05,
    "MID": 1.05, "Midfielder": 1.05, "Central Midfield": 1.05,
                  "Defensive Midfield": 1.05, "Attacking Midfield": 1.05,
    "FWD": 1.00, "Forward": 1.00, "Centre-Forward": 1.00, "Left Winger": 1.00,
                  "Right Winger": 1.00, "Second Striker": 1.00,
    "unknown": 1.00,
}


def _weighted_impact_lost(report: SquadReport) -> float:
    """Normalized impact lost ∈ [0,1].

    Preferred: per-player market-value share × position weight × (1 − availability).
    Fallback: position-weighted only (used when no per-player values are present —
    e.g. Wikipedia squads or pre-Lever-3 caches).

    The market-value formula reflects the intuition that Mbappé out hurts France
    far more than a backup midfielder out hurts France, even with the same
    binary "1 player missing" signal.
    """
    if not report.players:
        return 0.0

    total_value = sum(p.market_value_eur_m for p in report.players if p.market_value_eur_m > 0)
    if total_value > 0:
        # Value-weighted impact
        return float(sum(
            (p.market_value_eur_m / total_value)
            * _POSITION_IMPACT_WEIGHTS.get(p.position, 1.0)
            * (1.0 - p.availability)
            for p in report.players
            if p.market_value_eur_m > 0
        ))

    # Fallback: position-weight only
    total_w = sum(_POSITION_IMPACT_WEIGHTS.get(p.position, 1.0) for p in report.players)
    if total_w == 0:
        return 0.0
    lost = sum(
        _POSITION_IMPACT_WEIGHTS.get(p.position, 1.0) * (1.0 - p.availability)
        for p in report.players
    )
    return float(lost / total_w)


def squad_impact_features(
    home_report: SquadReport,
    away_report: SquadReport,
) -> dict[str, float]:
    """Numeric features for the model from two SquadReports."""
    impact_h = _weighted_impact_lost(home_report)
    impact_a = _weighted_impact_lost(away_report)
    return {
        "squad_availability_home": home_report.availability_score,
        "squad_availability_away": away_report.availability_score,
        "squad_availability_diff": (
            home_report.availability_score - away_report.availability_score
        ),
        "key_player_risk_home": float(len(home_report.risk_players)),
        "key_player_risk_away": float(len(away_report.risk_players)),
        # Lever 3: position-weighted impact-lost (proxy for per-player value impact)
        "weighted_impact_lost_home": impact_h,
        "weighted_impact_lost_away": impact_a,
        "weighted_impact_lost_diff": impact_h - impact_a,
    }
