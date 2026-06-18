"""Build public model tips from match contexts (all matches, incl. divergence-filtered)."""
from __future__ import annotations

from src.config import canonical_name


def _scorers(rows: list[dict] | None, top_n: int = 2) -> list[dict]:
    result = []
    for row in rows or []:
        name = row.get("name") or row.get("player")
        probability = row.get("p", row.get("p_score"))
        if name and probability is not None:
            result.append({"name": name, "p": round(float(probability), 3)})
    return result[:top_n]


def build_published_model_tips(
    schedule: list[dict],
    match_contexts: dict[str, dict],
) -> dict[str, dict]:
    """Map schedule display names to prediction data for ALL matches.

    match_contexts should be all_match_contexts (all API matches, including
    divergence-filtered ones) so every upcoming match gets a prediction card.
    """
    ctx_by_teams = {
        (canonical_name(ctx.get("home", "")), canonical_name(ctx.get("away", ""))): ctx
        for ctx in match_contexts.values()
        if ctx.get("home") and ctx.get("away")
    }
    tips: dict[str, dict] = {}
    for game in schedule:
        home_raw, away_raw = game.get("home", ""), game.get("away", "")
        ctx = ctx_by_teams.get((canonical_name(home_raw), canonical_name(away_raw)))
        if ctx is None:
            continue
        tip: dict = {
            "p_home": round(float(ctx["p_home"]), 3),
            "p_draw": round(float(ctx["p_draw"]), 3),
            "p_away": round(float(ctx["p_away"]), 3),
            "top_scorers_home": _scorers(ctx.get("home_scorers")),
            "top_scorers_away": _scorers(ctx.get("away_scorers")),
        }
        optional = {
            "xg_home":       ("lambda_home", 2),
            "xg_away":       ("lambda_away", 2),
            "p_btts_yes":    ("p_btts_yes", 3),
            "p_over15":      ("p_over15", 3),
            "p_over25":      ("p_over25", 3),
            "p_over35":      ("p_over35", 3),
            "p_under25":     ("p_under25", 3),
        }
        for output_key, (context_key, digits) in optional.items():
            value = ctx.get(context_key)
            if value is not None:
                tip[output_key] = round(float(value), digits)
        if "p_btts_yes" in tip:
            tip["p_btts_no"] = round(1.0 - tip["p_btts_yes"], 3)
        # Most likely scoreline
        top_scores = ctx.get("top_scorelines", [])
        if top_scores:
            sl = top_scores[0]
            tip["top_scoreline"] = f"{sl[0]}-{sl[1]}"
            tip["top_scoreline_prob"] = round(float(sl[2]), 3)
            if len(top_scores) >= 2:
                sl2 = top_scores[1]
                tip["second_scoreline"] = f"{sl2[0]}-{sl2[1]}"
                tip["second_scoreline_prob"] = round(float(sl2[2]), 3)
            if len(top_scores) >= 3:
                sl3 = top_scores[2]
                tip["third_scoreline"] = f"{sl3[0]}-{sl3[1]}"
                tip["third_scoreline_prob"] = round(float(sl3[2]), 3)
        tips[f"{home_raw} vs {away_raw}"] = tip
    return tips
