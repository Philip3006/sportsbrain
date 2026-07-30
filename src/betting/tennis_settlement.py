"""Tennis-Settlement (Roadmap TENNIS P1.1).

Deckt alle vom Detector erzeugten Tennis-Maerkte ab:
  home / away                       Match Winner (player_a / player_b)
  ah-1.5_a / ah+1.5_b               Set-Handicap +/-1.5
  first_set_a / first_set_b         First-Set Winner
  o/u_sets_{line}_over/under        Total Sets
  o/u_games_{line}_over/under       Total Games (Summe ueber alle Saetze)
  score_{scoreline}                 Exakter Satzstand ("2-1", "3-0", ...)

Retirement/Walkover-Handling:
  status == "walkover" oder "cancelled" -> alle Maerkte VOID (push)
  status == "retired" mit < 1 abgeschlossenem Satz -> VOID (push)
  status == "retired" mit >= 1 abgeschlossenem Satz:
    - Match Winner honoriert (Gegner des Retiring-Spielers gewinnt)
    - Alle strukturellen Maerkte (AH, O/U-Sets, O/U-Games, Set-Betting) VOID
    - First Set honoriert wenn Satz 1 vollstaendig gespielt

Rueckgabewerte:
  "won" | "lost" | "push" | "pending" | None
  None = Markt unbekannt (Aufrufer skipped)
  "pending" = Match noch nicht beendet
"""
from __future__ import annotations

from typing import Any


TENNIS_MARKET_PREFIXES = (
    "ah-1.5_", "ah+1.5_", "first_set_", "o/u_sets_", "o/u_games_", "score_",
)


def is_tennis_market(market: str) -> bool:
    """True wenn Market-String eindeutig Tennis ist (nicht Football).

    Achtung: 'home' und 'away' sind mehrdeutig -> nicht ueber diese Funktion
    dispatched. Verwenden Sie stattdessen match_id-Lookup gegen Tennis-Discovery.
    """
    return any(market.startswith(p) for p in TENNIS_MARKET_PREFIXES)


def _total_games(sets: list[tuple[int, int]]) -> int:
    return sum(a + b for a, b in sets)


def _first_set_winner(sets: list[tuple[int, int]]) -> str | None:
    """'a' wenn Spieler A den 1. Satz gewann, 'b' bei Spieler B, None wenn noch offen."""
    if not sets:
        return None
    a, b = sets[0]
    if a == b:
        return None  # Set noch nicht abgeschlossen
    return "a" if a > b else "b"


def _sets_won(sets: list[tuple[int, int]]) -> tuple[int, int]:
    """Anzahl gewonnener Saetze je Spieler."""
    a_sets = sum(1 for a, b in sets if a > b)
    b_sets = sum(1 for a, b in sets if b > a)
    return a_sets, b_sets


def _completed_sets(sets: list[tuple[int, int]]) -> int:
    """Anzahl Saetze mit klarer Entscheidung (Tennis: 6-x/x-6/7-x/x-7 etc.)."""
    return sum(1 for a, b in sets if a != b and max(a, b) >= 6)


def settle_tennis_market(market: str, match_result: dict[str, Any]) -> str | None:
    """Settled einen Tennis-Bet gegen ein Match-Ergebnis.

    match_result-Schema:
      {
        "status": "completed" | "retired" | "walkover" | "cancelled"
                  | "scheduled" | "in_progress",
        "sets":   [(a_games, b_games), ...],   # e.g. [(6,3), (4,6), (7,5)]
        "winner": "a" | "b" | None,            # required wenn status == completed
        "retired_by": "a" | "b" | None,        # nur bei status == retired
        "best_of": 3 | 5,
      }
    """
    status = match_result.get("status", "")
    sets = match_result.get("sets") or []
    best_of = match_result.get("best_of", 3)

    # --- Not-yet-decidable states -------------------------------------------
    if status in ("scheduled", "in_progress", ""):
        return "pending"

    # --- Walkover / Cancelled = alle Maerkte void ---------------------------
    if status in ("walkover", "cancelled"):
        return "push"

    # --- Retirement Handling ------------------------------------------------
    is_retired = status == "retired"
    completed = _completed_sets(sets)

    if is_retired and completed < 1:
        # Retirement vor Abschluss von Satz 1 -> alle Maerkte void
        return "push"

    # --- Structural markets void bei Retirement -----------------------------
    is_structural = (
        market.startswith("ah")
        or market.startswith("o/u_sets_")
        or market.startswith("o/u_games_")
        or market.startswith("score_")
    )
    if is_retired and is_structural:
        return "push"

    # --- Match Winner: home = player_a, away = player_b ---------------------
    winner = match_result.get("winner")
    if market == "home":
        if winner is None:
            return "push"
        return "won" if winner == "a" else "lost"
    if market == "away":
        if winner is None:
            return "push"
        return "won" if winner == "b" else "lost"

    # --- First Set ----------------------------------------------------------
    if market in ("first_set_a", "first_set_b"):
        fs = _first_set_winner(sets)
        if fs is None:
            # Satz 1 nicht abgeschlossen (bei retired < 1 completed schon oben behandelt)
            return "push"
        pick = market.split("_")[-1]  # 'a' oder 'b'
        return "won" if fs == pick else "lost"

    # --- Asian Handicap +/-1.5 Sets -----------------------------------------
    #   ah-1.5_a  = Spieler A gewinnt mit >=2 Saetzen Differenz
    #   ah+1.5_b  = Spieler B gewinnt ODER verliert mit <=1 Satz Differenz
    if market == "ah-1.5_a":
        a_sets, b_sets = _sets_won(sets)
        return "won" if (a_sets - b_sets) >= 2 else "lost"
    if market == "ah+1.5_b":
        a_sets, b_sets = _sets_won(sets)
        return "won" if (b_sets - a_sets) >= -1 else "lost"  # b darf max. 1 Satz Rueckstand haben

    # --- Total Sets O/U -----------------------------------------------------
    if market.startswith("o/u_sets_"):
        try:
            # format: o/u_sets_{line}_over  or  ..._under
            rest = market[len("o/u_sets_"):]
            line_str, side = rest.rsplit("_", 1)
            line = float(line_str)
        except (ValueError, IndexError):
            return None
        total_sets = _sets_won(sets)[0] + _sets_won(sets)[1]  # nur klare Saetze
        if side == "over":
            return "won" if total_sets > line else "lost"
        if side == "under":
            return "won" if total_sets < line else "lost"
        return None

    # --- Total Games O/U ----------------------------------------------------
    if market.startswith("o/u_games_"):
        try:
            rest = market[len("o/u_games_"):]
            line_str, side = rest.rsplit("_", 1)
            line = float(line_str)
        except (ValueError, IndexError):
            return None
        total_g = _total_games(sets)
        if side == "over":
            return "won" if total_g > line else "lost"
        if side == "under":
            return "won" if total_g < line else "lost"
        return None

    # --- Set Betting (exakter Satzstand) ------------------------------------
    if market.startswith("score_"):
        target = market[len("score_"):]  # z.B. "2-1", "3-0"
        a_sets, b_sets = _sets_won(sets)
        actual = f"{a_sets}-{b_sets}"
        # Nur wenn Match komplett & Ergebnis in gueltigem Bereich fuer best_of
        max_sets = 3 if best_of == 5 else 2  # sieger braucht 3 bzw. 2 saetze
        if max(a_sets, b_sets) < max_sets:
            return "pending"
        return "won" if actual == target else "lost"

    # --- Unknown market -----------------------------------------------------
    return None
