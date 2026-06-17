from __future__ import annotations

import math
import re
import unicodedata


def _normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", value)


def _scorer_market_name(market: str) -> str | None:
    if not market.startswith("scorer_"):
        return None
    return _normalize_name(market[len("scorer_"):].replace("_", " "))


def _totals_legs(line: float) -> list[float]:
    frac = round(line - math.floor(line), 2)
    base = math.floor(line)
    if frac == 0.25:
        return [float(base), float(base) + 0.5]
    if frac == 0.75:
        return [float(base) + 0.5, float(base) + 1.0]
    return [line]


def _settle_totals_leg(total: int, line: float, side: str) -> str:
    if side == "over":
        if total > line:
            return "won"
        if float(line).is_integer() and total == int(line):
            return "push"
        return "lost"
    if total < line:
        return "won"
    if float(line).is_integer() and total == int(line):
        return "push"
    return "lost"


def _early_totals_leg(total: int, line: float, side: str) -> str | None:
    if side == "over":
        return "won" if total > line else None
    return "lost" if total > line else None


def _combine_leg_results(results: list[str]) -> dict | None:
    if not results:
        return None
    if all(r == "won" for r in results):
        return {"status": "won", "pnl_mode": "full_win"}
    if all(r == "lost" for r in results):
        return {"status": "lost", "pnl_mode": "full_loss"}
    if results.count("won") == 1 and results.count("push") == 1:
        return {"status": "won", "pnl_mode": "half_win"}
    if results.count("lost") == 1 and results.count("push") == 1:
        return {"status": "lost", "pnl_mode": "half_loss"}
    if all(r == "push" for r in results):
        return {"status": "void", "pnl_mode": "push"}
    return None


def _resolve_totals_market(market: str, total: int, completed: bool) -> dict | None:
    match = re.fullmatch(r"o/u([0-9]+(?:\.[0-9]+)?)_(over|under)", market)
    if not match:
        return None
    line = float(match.group(1))
    side = match.group(2)
    legs = _totals_legs(line)
    if completed:
        return _combine_leg_results([_settle_totals_leg(total, leg, side) for leg in legs])
    early = [_early_totals_leg(total, leg, side) for leg in legs]
    if all(r == "won" for r in early):
        return {"status": "won", "pnl_mode": "full_win"}
    if all(r == "lost" for r in early):
        return {"status": "lost", "pnl_mode": "full_loss"}
    return None


def _resolve_scorer_market(
    market: str,
    completed: bool,
    scorer_names: set[str] | None,
) -> dict | None:
    scorer = _scorer_market_name(market)
    if not scorer or not scorer_names:
        return None
    normalized_scorers = {_normalize_name(name) for name in scorer_names if name}
    if scorer in normalized_scorers:
        return {"status": "won", "pnl_mode": "full_win"}
    if completed:
        return {"status": "lost", "pnl_mode": "full_loss"}
    return None


def resolve_market_state(
    market: str,
    home_score: int,
    away_score: int,
    *,
    completed: bool = False,
    scorer_names: set[str] | None = None,
) -> dict | None:
    total = home_score + away_score
    diff = home_score - away_score

    scorer = _resolve_scorer_market(market, completed, scorer_names)
    if scorer is not None:
        return scorer

    totals = _resolve_totals_market(market, total, completed)
    if totals is not None:
        return totals

    if market == "btts_yes":
        if home_score >= 1 and away_score >= 1:
            return {"status": "won", "pnl_mode": "full_win"}
        if completed:
            return {"status": "lost", "pnl_mode": "full_loss"}
        return None

    if market == "btts_no":
        if home_score >= 1 and away_score >= 1:
            return {"status": "lost", "pnl_mode": "full_loss"}
        if completed:
            return {"status": "won", "pnl_mode": "full_win"}
        return None

    if market == "goals_2_4":
        if total > 4:
            return {"status": "lost", "pnl_mode": "full_loss"}
        if completed:
            return {
                "status": "won" if 2 <= total <= 4 else "lost",
                "pnl_mode": "full_win" if 2 <= total <= 4 else "full_loss",
            }
        return None

    if market == "goals_2_4_no":
        if total > 4:
            return {"status": "won", "pnl_mode": "full_win"}
        if completed:
            return {
                "status": "won" if not (2 <= total <= 4) else "lost",
                "pnl_mode": "full_win" if not (2 <= total <= 4) else "full_loss",
            }
        return None

    if not completed:
        return None

    if market == "home":
        return {"status": "won" if diff > 0 else "lost", "pnl_mode": "full_win" if diff > 0 else "full_loss"}
    if market == "away":
        return {"status": "won" if diff < 0 else "lost", "pnl_mode": "full_win" if diff < 0 else "full_loss"}
    if market == "draw":
        return {"status": "won" if diff == 0 else "lost", "pnl_mode": "full_win" if diff == 0 else "full_loss"}
    if market == "dc_1x":
        return {"status": "won" if home_score >= away_score else "lost", "pnl_mode": "full_win" if home_score >= away_score else "full_loss"}
    if market == "dc_x2":
        return {"status": "won" if away_score >= home_score else "lost", "pnl_mode": "full_win" if away_score >= home_score else "full_loss"}
    if market == "dc_12":
        return {"status": "won" if home_score != away_score else "lost", "pnl_mode": "full_win" if home_score != away_score else "full_loss"}

    return None


def pnl_from_mode(pnl_mode: str, decimal_odds: float, stake: float) -> float:
    if pnl_mode == "full_win":
        return stake * (decimal_odds - 1.0)
    if pnl_mode == "half_win":
        return stake * (decimal_odds - 1.0) / 2.0
    if pnl_mode == "half_loss":
        return -stake / 2.0
    if pnl_mode == "full_loss":
        return -stake
    return 0.0
