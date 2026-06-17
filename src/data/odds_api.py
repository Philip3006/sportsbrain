"""
TheOddsAPI wrapper. Free tier: 500 requests/month.
Caches responses for 1 hour to preserve quota.
Set ODDS_API_KEY in .env or pass directly.
"""
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.config import DATA_CACHE, ODDS_API_URL
from src.data.cache import disk_cache

load_dotenv()

_USAGE_LOG = DATA_CACHE / "api_usage.json"


def _log_usage(requests_used: int, requests_remaining: int) -> None:
    usage = {"requests_used": requests_used, "requests_remaining": requests_remaining}
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(_USAGE_LOG, "w") as f:
        json.dump(usage, f)


def get_api_key(api_key: str | None = None) -> str:
    key = api_key or os.getenv("ODDS_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "ODDS_API_KEY not set. Add it to .env or pass api_key= directly."
        )
    return key


@disk_cache("odds_api_upcoming_wide", max_age_hours=1.0)
def fetch_upcoming_matches(
    sport: str = "soccer_fifa_world_cup",
    regions: str | None = None,
    markets: str = "h2h,totals,spreads",
    api_key: str | None = None,
    force: bool = False,
) -> list[dict]:
    """
    Fetches upcoming matches with odds from TheOddsAPI.
    Returns list of parsed match dicts with bookmaker odds.
    """
    from src.config import LINE_SHOPPING_REGIONS
    if regions is None:
        regions = ",".join(LINE_SHOPPING_REGIONS)
    key = get_api_key(api_key)
    url = f"{ODDS_API_URL}/sports/{sport}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            optional = [m for m in ("double_chance", "btts") if m in markets]
            if optional:
                print(f"  WARN: market(s) unavailable — retrying without {', '.join(optional)}.")
                params["markets"] = ",".join(m for m in markets.split(",") if m not in optional)
                resp = requests.get(url, params=params, timeout=15)
                resp.raise_for_status()
            else:
                raise
        else:
            raise

    # Log usage from response headers
    used = int(resp.headers.get("x-requests-used", 0))
    remaining = int(resp.headers.get("x-requests-remaining", 0))
    _log_usage(used, remaining)

    if remaining < 20:
        print(f"WARNING: Only {remaining} API requests remaining this month.")
        try:
            from src.notifications.web_push import send_quota_alert
            send_quota_alert(remaining)
        except Exception:
            pass

    data = resp.json()
    return _parse_matches(data)


_PREFERRED_BM = "pinnacle"


def _round_quarter(x: float) -> float:
    """Round to nearest 0.25."""
    return round(x * 4) / 4


def _parse_markets(bm: dict, home: str, away: str, store: dict, dynamic: dict) -> None:
    """Extracts h2h/totals/spreads/btts odds from one bookmaker.
    store: flat dict for h2h and btts.
    dynamic: nested dict with keys 'spreads' and 'totals' for all lines.
    """
    for market in bm.get("markets", []):
        mkt = market.get("key")
        if mkt == "h2h":
            for o in market.get("outcomes", []):
                name, price = o["name"], o["price"]
                if price > store.get(name, 0):
                    store[name] = price
        elif mkt in ("totals", "alternate_totals"):
            for o in market.get("outcomes", []):
                pt = _round_quarter(o.get("point", 0))
                side = o["name"].lower()  # "over" or "under"
                if pt not in dynamic["totals"]:
                    dynamic["totals"][pt] = {}
                if o["price"] > dynamic["totals"][pt].get(side, 0):
                    dynamic["totals"][pt][side] = o["price"]
        elif mkt in ("totals_h1", "alternate_totals_h1", "totals_h2", "alternate_totals_h2"):
            period_key = "totals_h1" if mkt.endswith("_h1") else "totals_h2"
            if period_key not in dynamic:
                dynamic[period_key] = {}
            for o in market.get("outcomes", []):
                pt = _round_quarter(o.get("point", 0))
                side = o["name"].lower()  # "over" or "under"
                if pt not in dynamic[period_key]:
                    dynamic[period_key][pt] = {}
                if o["price"] > dynamic[period_key][pt].get(side, 0):
                    dynamic[period_key][pt][side] = o["price"]
        elif mkt in ("spreads", "alternate_spreads"):
            for o in market.get("outcomes", []):
                pt = _round_quarter(o.get("point", 0))
                # Normalize to home's line (negative when home favored)
                team = o["name"]
                if team == home:
                    home_line = pt
                else:
                    home_line = -pt  # away's line flipped to home perspective
                home_line = _round_quarter(home_line)
                side = "home" if team == home else "away"
                if home_line not in dynamic["spreads"]:
                    dynamic["spreads"][home_line] = {}
                if o["price"] > dynamic["spreads"][home_line].get(side, 0):
                    dynamic["spreads"][home_line][side] = o["price"]
        elif mkt == "btts":
            for o in market.get("outcomes", []):
                key = f"btts_{o['name'].lower()}"  # "btts_yes" or "btts_no"
                if o["price"] > store.get(key, 0):
                    store[key] = o["price"]
        elif mkt == "double_chance":
            for o in market.get("outcomes", []):
                name, price = o["name"], o["price"]
                if "Home" in name and "Draw" in name:
                    key = "dc_1x"
                elif "Draw" in name and "Away" in name:
                    key = "dc_x2"
                else:
                    key = "dc_12"
                if price > store.get(key, 0):
                    store[key] = o["price"]


def _websearch_odds_fallback(home: str, away: str) -> dict | None:
    """Search for h2h odds when TheOddsAPI has no bookmakers for a game.

    Uses DuckDuckGo text search → fetches first result → extracts decimal odds
    via JSON-LD or regex. Returns {home, draw, away} or None.
    """
    try:
        from ddgs import DDGS
        import re

        query = f'{home} vs {away} 2026 FIFA World Cup odds'
        results = DDGS().text(query, max_results=4)
        if not results:
            return None

        headers = {"User-Agent": "Mozilla/5.0 (compatible; SportsBrainBot/1.0)"}
        for result in results:
            url = result.get("href", "")
            if not url or "twitter" in url or "youtube" in url:
                continue
            try:
                resp = requests.get(url, headers=headers, timeout=8)
                if resp.status_code != 200:
                    continue
                html = resp.text

                # Try JSON-LD first (structured sports data)
                ld_blocks = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                                       html, re.DOTALL)
                for block in ld_blocks:
                    try:
                        ld = json.loads(block)
                        items = ld if isinstance(ld, list) else [ld]
                        for item in items:
                            if item.get("@type") in ("SportsEvent", "Event"):
                                offers = item.get("offers", [])
                                if isinstance(offers, list) and len(offers) >= 3:
                                    prices = [float(o.get("price", 0)) for o in offers if o.get("price")]
                                    if len(prices) >= 3 and all(1.01 < p < 50 for p in prices[:3]):
                                        return {"home": prices[0], "draw": prices[1], "away": prices[2]}
                    except Exception:
                        continue

                # Regex fallback: look for 3 consecutive decimal odds in context
                # e.g. "Netherlands 2.05, Draw 3.40, Japan 3.60"
                pattern = r'(?:home|win|1)\D{0,20}?(\d\.\d{2})\D{0,30}(?:draw|x|tie)\D{0,20}?(\d\.\d{2})\D{0,30}(?:away|win|2)\D{0,20}?(\d\.\d{2})'
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    h, d, a = float(m.group(1)), float(m.group(2)), float(m.group(3))
                    if all(1.01 < x < 50 for x in (h, d, a)):
                        implied = 1/h + 1/d + 1/a
                        if 0.90 <= implied <= 1.20:
                            return {"home": h, "draw": d, "away": a}
            except Exception:
                continue
    except Exception:
        pass
    return None


def _parse_matches(raw: list[dict]) -> list[dict]:
    """
    Returns one entry per match with:
    - Best odds across ALL bookmakers (used for EV calculation)
    - Pinnacle-specific odds (sharp reference line)
    - `spreads`: {home_line: {"home": odds, "away": odds}} for all AH lines
    - `totals_lines`: {line: {"over": odds, "under": odds}} for all O/U lines
    """
    matches = []
    for event in raw:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        commence = event.get("commence_time", "")
        match_id = event.get("id", f"{home}_vs_{away}")

        bookmakers = event.get("bookmakers", [])
        # WebSearch fallback: 0 bookmakers OR fewer than 3 (sparse early market)
        sparse = len(bookmakers) < 3
        if sparse:
            ws_odds = _websearch_odds_fallback(home, away)
            if ws_odds:
                ws_bm = {"key": "websearch", "title": "WebSearch", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": home,   "price": ws_odds["home"]},
                        {"name": "Draw", "price": ws_odds["draw"]},
                        {"name": away,   "price": ws_odds["away"]},
                    ]}
                ]}
                if not bookmakers:
                    bookmakers = [ws_bm]
                    print(f"  INFO: {home} vs {away} — odds via WebSearch (0 bookmakers) "
                          f"({ws_odds['home']}/{ws_odds['draw']}/{ws_odds['away']})")
                else:
                    bookmakers = bookmakers + [ws_bm]
                    print(f"  INFO: {home} vs {away} — WebSearch enriched sparse market "
                          f"({len(bookmakers)-1} → +WebSearch)")
            elif not bookmakers:
                print(f"  WARN: {home} vs {away} — no bookmakers, WebSearch found nothing.")
                continue

        best: dict[str, float] = {}
        pin: dict[str, float] = {}
        pin_dynamic: dict = {"spreads": {}, "totals": {}, "totals_h1": {}, "totals_h2": {}}
        best_dynamic: dict = {"spreads": {}, "totals": {}, "totals_h1": {}, "totals_h2": {}}
        best_bm: dict[str, str] = {}
        bm_h2h: list[tuple[str, float, float, float]] = []  # (key, home, draw, away)
        # Per-Bookmaker-Quoten für h2h (Bookie-Matrix im Frontend)
        per_bm_h2h: list[dict] = []

        for bm in bookmakers:
            bm_key = bm["key"]
            bm_title = bm.get("title", bm_key)
            prev = {k: v for k, v in best.items()}
            _parse_markets(bm, home, away, best, best_dynamic)
            for k in best:
                if best[k] != prev.get(k, 0):
                    best_bm[k] = bm_key
            if bm_key == _PREFERRED_BM:
                _parse_markets(bm, home, away, pin, pin_dynamic)
            # Capture per-bookmaker h2h for coherence fallback + matrix
            bm_store: dict[str, float] = {}
            _parse_markets(
                bm, home, away, bm_store,
                {"spreads": {}, "totals": {}, "totals_h1": {}, "totals_h2": {}},
            )
            bh = bm_store.get(home, 0.0)
            bd = bm_store.get("Draw", 0.0)
            ba = bm_store.get(away, 0.0)
            if bh > 0 and bd > 0 and ba > 0:
                bm_h2h.append((bm_key, bh, bd, ba))
                per_bm_h2h.append({
                    "key":   bm_key,
                    "title": bm_title,
                    "home":  round(bh, 2),
                    "draw":  round(bd, 2),
                    "away":  round(ba, 2),
                })

        if not best.get(home):
            continue

        def _implied(h: float, d: float, a: float) -> float:
            return 1/h + 1/d + 1/a if h > 0 and d > 0 and a > 0 else 0.0

        h_odds = best.get(home, 0.0)
        d_odds = best.get("Draw", 0.0)
        a_odds = best.get(away, 0.0)

        # For h2h: if best-of-all is incoherent (implied < 90%), fall back to a single
        # coherent bookmaker. Pinnacle first, then highest-implied single bookmaker.
        if _implied(h_odds, d_odds, a_odds) < 0.90:
            pin_h = pin.get(home, 0.0)
            pin_d = pin.get("Draw", 0.0)
            pin_a = pin.get(away, 0.0)
            if _implied(pin_h, pin_d, pin_a) >= 0.90:
                h_odds, d_odds, a_odds = pin_h, pin_d, pin_a
            else:
                candidates = [(1/bh + 1/bd + 1/ba, bh, bd, ba, bk)
                              for bk, bh, bd, ba in bm_h2h
                              if 1/bh + 1/bd + 1/ba >= 0.90]
                if candidates:
                    _, h_odds, d_odds, a_odds, chosen_bm = max(candidates)
                    print(f"  INFO: {home} vs {away} — using {chosen_bm} h2h "
                          f"(best-of implied was {_implied(best.get(home,0), best.get('Draw',0), best.get(away,0)):.0%})")
                else:
                    print(f"  WARN: {home} vs {away} — no coherent h2h found, skipping.")
                    continue

        # Build backward-compat h2h flat keys from best dict
        match_dict: dict = {
            "match_id":       match_id,
            "commence_time":  commence,
            "home_team":      home,
            "away_team":      away,
            "tournament":     "FIFA World Cup",
            # h2h odds: coherent single-bookmaker when best-of-all is incoherent
            "home_odds":      h_odds,
            "draw_odds":      d_odds,
            "away_odds":      a_odds,
            # Backward compat O/U 2.5 (best across bookmakers)
            "over_odds":      best_dynamic["totals"].get(2.5, {}).get("over", 0.0),
            "under_odds":     best_dynamic["totals"].get(2.5, {}).get("under", 0.0),
            "over15_odds":    best_dynamic["totals"].get(1.5, {}).get("over", 0.0),
            "under15_odds":   best_dynamic["totals"].get(1.5, {}).get("under", 0.0),
            "over35_odds":    best_dynamic["totals"].get(3.5, {}).get("over", 0.0),
            "under35_odds":   best_dynamic["totals"].get(3.5, {}).get("under", 0.0),
            # Backward compat AH (best across bookmakers)
            "ah_home_odds":   best_dynamic["spreads"].get(-0.5, {}).get("home", 0.0),
            "ah_away_odds":   best_dynamic["spreads"].get(-0.5, {}).get("away", 0.0),
            "ah1_home_odds":  best_dynamic["spreads"].get(-1.0, {}).get("home", 0.0),
            "ah1_away_odds":  best_dynamic["spreads"].get(-1.0, {}).get("away", 0.0),
            "ah15_home_odds": best_dynamic["spreads"].get(-1.5, {}).get("home", 0.0),
            "ah15_away_odds": best_dynamic["spreads"].get(-1.5, {}).get("away", 0.0),
            "ah2_home_odds":  best_dynamic["spreads"].get(-2.0, {}).get("home", 0.0),
            "ah2_away_odds":  best_dynamic["spreads"].get(-2.0, {}).get("away", 0.0),
            "ah25_home_odds": best_dynamic["spreads"].get(-2.5, {}).get("home", 0.0),
            "ah25_away_odds": best_dynamic["spreads"].get(-2.5, {}).get("away", 0.0),
            "btts_yes_odds":  best.get("btts_yes", 0.0),
            "btts_no_odds":   best.get("btts_no", 0.0),
            "ftts_home_odds": 0.0,
            "ftts_away_odds": 0.0,
            "best_home_bm":   best_bm.get(home, ""),
            "best_draw_bm":   best_bm.get("Draw", ""),
            "best_away_bm":   best_bm.get(away, ""),
            # Pinnacle odds (sharp reference line — used for display and CLV)
            "pin_home":      pin.get(home, 0.0),
            "pin_draw":      pin.get("Draw", 0.0),
            "pin_away":      pin.get(away, 0.0),
            "pin_over":      pin_dynamic["totals"].get(2.5, {}).get("over", 0.0),
            "pin_under":     pin_dynamic["totals"].get(2.5, {}).get("under", 0.0),
            "pin_ah_home":   pin_dynamic["spreads"].get(-0.5, {}).get("home", 0.0),
            "pin_ah_away":   pin_dynamic["spreads"].get(-0.5, {}).get("away", 0.0),
            "pin_ah1_home":  pin_dynamic["spreads"].get(-1.0, {}).get("home", 0.0),
            "pin_ah1_away":  pin_dynamic["spreads"].get(-1.0, {}).get("away", 0.0),
            "pin_ah15_home": pin_dynamic["spreads"].get(-1.5, {}).get("home", 0.0),
            "pin_ah15_away": pin_dynamic["spreads"].get(-1.5, {}).get("away", 0.0),
            "pin_btts_yes":  pin.get("btts_yes", 0.0),
            "pin_btts_no":   pin.get("btts_no", 0.0),
            # Double Chance (best across bookmakers)
            "dc_1x_odds":    best.get("dc_1x", 0.0),   # Home or Draw
            "dc_x2_odds":    best.get("dc_x2", 0.0),   # Draw or Away
            "dc_12_odds":    best.get("dc_12", 0.0),   # Home or Away
            "pin_dc_1x":     pin.get("dc_1x", 0.0),
            "pin_dc_x2":     pin.get("dc_x2", 0.0),
            "pin_dc_12":     pin.get("dc_12", 0.0),
            # Dynamic all-lines dicts — used by scanner for comprehensive coverage
            "spreads":        best_dynamic["spreads"],   # {home_line: {"home": odds, "away": odds}}
            "totals_lines":   best_dynamic["totals"],    # {line: {"over": odds, "under": odds}}
            "totals_h1_lines": best_dynamic["totals_h1"], # 1st-half O/U lines only
            "totals_h2_lines": best_dynamic["totals_h2"], # 2nd-half O/U lines only
            # Per-Bookmaker-h2h-Quoten (für Bookie-Matrix im Frontend)
            "bookmakers_h2h": per_bm_h2h,
        }
        matches.append(match_dict)

    return matches


def _parse_period_totals_from_event(event: dict) -> dict[str, dict]:
    """Extract real first/second-half totals from one event-odds response."""
    dynamic: dict = {"spreads": {}, "totals": {}, "totals_h1": {}, "totals_h2": {}}
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    store: dict[str, float] = {}
    for bm in event.get("bookmakers", []):
        _parse_markets(bm, home, away, store, dynamic)
    return {
        "totals_h1_lines": dynamic["totals_h1"],
        "totals_h2_lines": dynamic["totals_h2"],
    }


def fetch_event_period_totals(
    sport: str,
    event_id: str,
    regions: str | None = None,
    api_key: str | None = None,
    force: bool = False,
    max_age_hours: float = 1.0,
) -> dict[str, dict]:
    """Fetch real H1/H2 O/U lines for one event from TheOddsAPI.

    Returns {"totals_h1_lines": {...}, "totals_h2_lines": {...}}. Missing or
    unsupported period markets return empty dicts.
    """
    import hashlib
    import pickle
    import time as _time

    from src.config import LINE_SHOPPING_REGIONS

    if regions is None:
        regions = ",".join(LINE_SHOPPING_REGIONS)
    markets = "totals_h1,alternate_totals_h1,totals_h2,alternate_totals_h2"
    cache_key = hashlib.md5(f"{sport}|{event_id}|{regions}|{markets}".encode()).hexdigest()
    cache_path = DATA_CACHE / f"period_totals_{cache_key}.pkl"
    if not force and cache_path.exists():
        age_hours = (_time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            with open(cache_path, "rb") as f:
                return pickle.load(f)

    key = get_api_key(api_key)
    url = f"{ODDS_API_URL}/sports/{sport}/events/{event_id}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = _parse_period_totals_from_event(resp.json())
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422:
            data = {"totals_h1_lines": {}, "totals_h2_lines": {}}
        else:
            raise

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(data, f)
    return data


def derive_goals_range_implied(
    totals_lines: dict,
    min_g: int = 2,
    max_g: int = 4,
) -> float | None:
    """Derive implied P(min_g ≤ total ≤ max_g) from available O/U lines.

    Primary: P(2-4) = P(over 1.5) − P(over 4.5).
    Fallback: invert O/U 2.5 to a Poisson mean λ, then sum PMF over [min_g, max_g].
    Returns None when no usable line is available.
    """
    from scipy.optimize import brentq
    from scipy.stats import poisson as _poisson

    low_key = float(min_g) - 0.5   # 1.5
    high_key = float(max_g) + 0.5  # 4.5
    over_low = totals_lines.get(low_key, {}).get("over")
    over_high = totals_lines.get(high_key, {}).get("over")
    if over_low and over_high and over_low > 1.0 and over_high > 1.0:
        implied = 1.0 / over_low - 1.0 / over_high
        return max(0.0, implied)

    # Fallback: use O/U 2.5 to infer Poisson mean, then compute P(min_g–max_g)
    over_25 = totals_lines.get(2.5, {}).get("over")
    under_25 = totals_lines.get(2.5, {}).get("under")
    if not over_25 or not under_25 or over_25 <= 1.0 or under_25 <= 1.0:
        return None
    # Remove margin: fair P(over 2.5) = P(total ≥ 3)
    total_inv = 1.0 / over_25 + 1.0 / under_25
    p_over = (1.0 / over_25) / total_inv
    # Solve for λ: P(total ≥ 3 | Poisson(λ)) = p_over
    try:
        lam = brentq(lambda l: 1.0 - _poisson.cdf(2, l) - p_over, 0.01, 15.0)
    except ValueError:
        return None
    implied = float(sum(_poisson.pmf(k, lam) for k in range(min_g, max_g + 1)))
    return max(0.0, implied)


def fetch_event_player_props(
    event_id: str,
    regions: str = "eu,uk",
    api_key: str | None = None,
    force: bool = False,
    max_age_hours: float = 1.0,
) -> dict[str, float]:
    """
    Fetches anytime goalscorer odds for one event from TheOddsAPI.
    Returns {player_name: best_odds_across_bookmakers}.
    Costs 1 API request per call; cached per event_id for 1h to preserve quota.
    """
    import pickle
    import time as _time

    cache_path = DATA_CACHE / f"player_props_{event_id}.pkl"
    if not force and cache_path.exists():
        if (_time.time() - cache_path.stat().st_mtime) / 3600 < max_age_hours:
            with open(cache_path, "rb") as f:
                return pickle.load(f)

    key = get_api_key(api_key)
    url = f"{ODDS_API_URL}/sports/soccer_fifa_world_cup/events/{event_id}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": "player_goal_scorer_anytime",
        "oddsFormat": "decimal",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except Exception:
        return {}

    used = int(resp.headers.get("x-requests-used", 0))
    remaining = int(resp.headers.get("x-requests-remaining", 0))
    _log_usage(used, remaining)

    best: dict[str, float] = {}
    for bm in resp.json().get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "player_goal_scorer_anytime":
                continue
            for outcome in market.get("outcomes", []):
                name = (outcome.get("description") or "").strip()
                price = float(outcome.get("price") or 0)
                if name and price > 1.0 and price > best.get(name, 0):
                    best[name] = price

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(best, f)
    return best


_ESPN_NAME_ALIASES: dict[str, str] = {
    # ESPN displayName → our canonical name (as used in ledger/signals)
    "congo dr":           "DR Congo",
    "dr congo":           "DR Congo",
    "bosnia and herzegovina": "Bosnia & Herzegovina",
    "korea republic":     "South Korea",
    "korea dpr":          "North Korea",
    "usa":                "United States",
    "united states":      "USA",
    "côte d'ivoire":      "Ivory Coast",
    "cote d'ivoire":      "Ivory Coast",
    "türkiye":            "Turkey",
}


def _espn_team_name(raw: str) -> str:
    """Normalize ESPN displayName to canonical team name."""
    return _ESPN_NAME_ALIASES.get(raw.lower().strip(), raw)


def _fetch_espn_wm_scores() -> list[dict]:
    """ESPN public scoreboard — kein API-Key, ~30-60s Lag.

    Returns same schema as fetch_wm_live_scores.
    """
    from datetime import datetime, timezone
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    now_iso = datetime.now(timezone.utc).isoformat()
    results = []
    for e in resp.json().get("events", []):
        comp = e.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue
        status = comp.get("status", {})
        state = status.get("type", {}).get("state", "pre")
        completed = bool(status.get("type", {}).get("completed", False))
        home_score = away_score = None
        if state in ("in", "post"):
            try:
                home_score = int(home_c.get("score", 0))
                away_score = int(away_c.get("score", 0))
            except (ValueError, TypeError):
                pass
        results.append({
            "match_id":      f"espn_{e.get('id', '')}",
            "home":          _espn_team_name(home_c.get("team", {}).get("displayName", "")),
            "away":          _espn_team_name(away_c.get("team", {}).get("displayName", "")),
            "home_score":    home_score,
            "away_score":    away_score,
            "commence_time": e.get("date", "").replace("Z", "+00:00"),
            "completed":     completed,
            "display_clock": status.get("displayClock", ""),
            "period":        status.get("period", 0),
            "last_update":   now_iso,
        })
    return results


def fetch_espn_goal_scorers(event_id: str) -> list[str]:
    """Returns normalized scorer display names for one ESPN event, best effort."""
    if not event_id:
        return []
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"
    resp = requests.get(url, params={"event": event_id}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    names: list[str] = []

    def _append_name(raw: str | None) -> None:
        if not raw:
            return
        val = str(raw).strip()
        if val and val not in names:
            names.append(val)

    scoring_plays = data.get("scoringPlays") or []
    for play in scoring_plays:
        athletes = play.get("participants") or play.get("athletes") or []
        if isinstance(athletes, list):
            for athlete in athletes:
                if not isinstance(athlete, dict):
                    continue
                display = athlete.get("displayName")
                short = athlete.get("shortName")
                full = (athlete.get("athlete") or {}).get("displayName") if isinstance(athlete.get("athlete"), dict) else None
                _append_name(display or full or short)
        competitor = play.get("competitor") or {}
        leader = competitor.get("leaders") if isinstance(competitor, dict) else None
        if isinstance(leader, list):
            for row in leader:
                leaders = row.get("leaders") if isinstance(row, dict) else None
                if isinstance(leaders, list):
                    for athlete in leaders:
                        if isinstance(athlete, dict):
                            _append_name(athlete.get("displayName"))
        text = play.get("text")
        if isinstance(text, str) and "goal" in text.lower():
            _append_name(text.split("goal", 1)[0].strip(" .:-"))

    if names:
        return names

    drives = data.get("drives") or {}
    for play in drives.get("plays", []) if isinstance(drives, dict) else []:
        text = play.get("text")
        if isinstance(text, str) and "goal" in text.lower():
            _append_name(text.split("goal", 1)[0].strip(" .:-"))

    return names


def fetch_wm_live_scores(
    days_from: int = 2,
    api_key: str | None = None,
) -> list[dict]:
    """WM Live-Scores — ESPN primär (kein Key, ~30-60s Lag), TheOddsAPI als Fallback.

    Returns list of {match_id, home, away, home_score, away_score,
    commence_time, completed, last_update}.
    """
    try:
        scores = _fetch_espn_wm_scores()
        if scores:
            return scores
    except Exception as e:
        print(f"  [scores] ESPN fehlgeschlagen ({e}), Fallback TheOddsAPI…")

    # Fallback: TheOddsAPI
    key = get_api_key(api_key)
    url = f"{ODDS_API_URL}/sports/soccer_fifa_world_cup/scores"
    params = {"apiKey": key, "daysFrom": days_from}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    results = []
    for m in resp.json():
        scores_raw = m.get("scores") or []
        scores: dict[str, int] = {}
        for s in scores_raw:
            if s.get("score") is not None:
                try:
                    scores[s["name"]] = int(s["score"])
                except (ValueError, TypeError):
                    pass
        home, away = m.get("home_team", ""), m.get("away_team", "")
        results.append({
            "match_id":      m.get("id", f"{home}_vs_{away}"),
            "home":          home,
            "away":          away,
            "home_score":    scores.get(home),
            "away_score":    scores.get(away),
            "commence_time": m.get("commence_time", ""),
            "completed":     bool(m.get("completed", False)),
            "last_update":   m.get("last_update", ""),
        })
    return results


def fetch_wm_scores(
    days_from: int = 3,
    api_key: str | None = None,
) -> list[dict]:
    """
    Fetches completed WM 2026 match scores from TheOddsAPI.
    Cached for 30 minutes to avoid quota burn.
    Returns list of {match_id, home, away, home_score, away_score, commence_time}.
    """
    key = get_api_key(api_key)
    url = f"{ODDS_API_URL}/sports/soccer_fifa_world_cup/scores"
    params = {"apiKey": key, "daysFrom": days_from}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = []
    for m in data:
        if not m.get("completed"):
            continue
        scores_raw = m.get("scores") or []
        scores = {}
        for s in scores_raw:
            if s.get("score") is not None:
                try:
                    scores[s["name"]] = int(s["score"])
                except (ValueError, TypeError):
                    pass
        home, away = m["home_team"], m["away_team"]
        results.append({
            "match_id": m.get("id", f"{home}_vs_{away}"),
            "home": home,
            "away": away,
            "home_score": scores.get(home),
            "away_score": scores.get(away),
            "commence_time": m.get("commence_time", ""),
        })
    return results


def mock_upcoming_matches() -> list[dict]:
    """Returns synthetic upcoming matches for dry-run testing (no API call)."""
    return [
        {
            "match_id": "mock_USA_vs_Mexico",
            "commence_time": "2026-06-15T20:00:00Z",
            "home_team": "United States",
            "away_team": "Mexico",
            "home_odds": 2.30,
            "draw_odds": 3.20,
            "away_odds": 3.00,
            "btts_yes_odds": 1.72,
            "btts_no_odds": 2.05,
            "ftts_home_odds": 1.90,
            "ftts_away_odds": 2.05,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
            # Dynamic dicts used by scanner's dynamic loops
            "totals_lines": {
                1.5: {"over": 1.35, "under": 3.10},
                2.25: {"over": 1.93, "under": 1.97},
                2.5: {"over": 1.90, "under": 1.95},
                3.5: {"over": 3.20, "under": 1.38},
            },
            "spreads": {
                -0.5: {"home": 1.85, "away": 2.00},
                -1.0: {"home": 2.05, "away": 1.80},
            },
            # Backward-compat (still used for pin_* fields in b365_map)
            "over_odds": 1.90, "under_odds": 1.95,
            "over15_odds": 1.35, "under15_odds": 3.10,
            "over35_odds": 3.20, "under35_odds": 1.38,
            "ah_home_odds": 1.85, "ah_away_odds": 2.00,
            "ah1_home_odds": 2.05, "ah1_away_odds": 1.80,
            "ah15_home_odds": 0.0, "ah15_away_odds": 0.0,
            "pin_home": 2.28, "pin_draw": 3.15, "pin_away": 2.97,
            "pin_over": 1.90, "pin_under": 1.95,
            "pin_ah_home": 1.83, "pin_ah_away": 1.98,
            "pin_ah1_home": 2.02, "pin_ah1_away": 1.78,
            "pin_ah15_home": 0.0, "pin_ah15_away": 0.0,
            "pin_btts_yes": 1.70, "pin_btts_no": 2.03,
            "dc_1x_odds": 1.35, "dc_x2_odds": 1.72, "dc_12_odds": 1.22,
            "pin_dc_1x": 1.33, "pin_dc_x2": 1.70, "pin_dc_12": 1.20,
        },
        {
            "match_id": "mock_Brazil_vs_Argentina",
            "commence_time": "2026-06-16T17:00:00Z",
            "home_team": "Brazil",
            "away_team": "Argentina",
            "home_odds": 2.10,
            "draw_odds": 3.40,
            "away_odds": 3.50,
            "btts_yes_odds": 1.72,
            "btts_no_odds": 2.05,
            "ftts_home_odds": 1.85,
            "ftts_away_odds": 2.10,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
            # Dynamic dicts
            "totals_lines": {
                1.5: {"over": 1.30, "under": 3.40},
                2.25: {"over": 1.87, "under": 2.02},
                2.5: {"over": 1.85, "under": 2.00},
                3.5: {"over": 3.40, "under": 1.35},
            },
            "spreads": {
                -0.5: {"home": 1.90, "away": 1.95},
                -0.75: {"home": 2.05, "away": 1.85},
                -1.0: {"home": 2.20, "away": 1.72},
            },
            # Backward-compat
            "over_odds": 1.85, "under_odds": 2.00,
            "over15_odds": 1.30, "under15_odds": 3.40,
            "over35_odds": 3.40, "under35_odds": 1.35,
            "ah_home_odds": 1.90, "ah_away_odds": 1.95,
            "ah1_home_odds": 2.20, "ah1_away_odds": 1.72,
            "ah15_home_odds": 0.0, "ah15_away_odds": 0.0,
            "pin_home": 2.08, "pin_draw": 3.38, "pin_away": 3.47,
            "pin_over": 1.83, "pin_under": 1.97,
            "pin_ah_home": 1.88, "pin_ah_away": 1.93,
            "pin_ah1_home": 2.18, "pin_ah1_away": 1.70,
            "pin_ah15_home": 0.0, "pin_ah15_away": 0.0,
            "pin_btts_yes": 1.70, "pin_btts_no": 2.03,
            "dc_1x_odds": 1.30, "dc_x2_odds": 1.88, "dc_12_odds": 1.18,
            "pin_dc_1x": 1.28, "pin_dc_x2": 1.86, "pin_dc_12": 1.16,
        },
    ]
