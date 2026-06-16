"""
Writes docs/data/signals.json for the GitHub Pages web dashboard.
Called at the end of daily_scan.py and tennis_scan.py.
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.betting.value_detector import BetSignal

ROOT = Path(__file__).parent.parent.parent
_JSON_PATH = ROOT / "docs" / "data" / "signals.json"
_LEDGER_PATH = ROOT / "results" / "ledger.csv"


def _build_history(n_days: int = 30) -> list[dict]:
    """Read ledger CSV and return daily P&L history (most recent first)."""
    if not _LEDGER_PATH.exists():
        return []
    try:
        daily: dict[str, dict] = defaultdict(lambda: {"n_bets": 0, "staked": 0.0, "pnl": 0.0})
        with open(_LEDGER_PATH, newline="") as f:
            for row in csv.DictReader(f):
                date = (row.get("placed_date") or row.get("match_date") or "")[:10].strip()
                if not date:
                    continue
                daily[date]["n_bets"] += 1
                daily[date]["staked"] += float(row.get("stake_amount") or 0)
                daily[date]["pnl"]    += float(row.get("pnl") or 0)
        result = []
        for date in sorted(daily.keys(), reverse=True)[:n_days]:
            d = daily[date]
            roi = (d["pnl"] / d["staked"] * 100) if d["staked"] > 0 else 0.0
            result.append({
                "date":    date,
                "n_bets":  d["n_bets"],
                "pnl":     round(d["pnl"], 2),
                "roi_pct": round(roi, 1),
            })
        return result
    except Exception:
        return []


def _signal_to_dict(
    s: BetSignal,
    sport: str = "football",
    tour: str = "",
    kickoff: str = "",
) -> dict:
    d = {
        "sport":           sport,
        "match":           f"{s.home} vs {s.away}",
        "market":          s.market,
        "odds":            round(s.decimal_odds, 2),
        "model_prob":      round(s.model_prob * 100, 1),
        "fair_prob":       round(s.fair_prob * 100, 1),
        "ev_pct":          round(s.ev * 100, 1),
        "stake_eur":       round(s.stake_eur, 2),
        "stake_pct":       round(s.stake_pct * 100, 1),
        "confidence":      s.confidence,
        "n_models_agree":  s.n_models_agree,
    }
    if tour:
        d["tour"] = tour
    if kickoff:
        d["kickoff"] = kickoff
    return d


def _build_wm_stats() -> dict:
    """Aggregiert WM-Performance-Stats aus dem Ledger."""
    if not _LEDGER_PATH.exists():
        return {}
    try:
        stats = {
            "1x2":   {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
            "ou25":  {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
            "btts":  {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
            "other": {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
        }
        daily_balance: dict[str, float] = {}
        balance = 100.0
        with open(_LEDGER_PATH, newline="") as f:
            for row in sorted(csv.DictReader(f), key=lambda r: r.get("match_date", "")):
                status = row.get("status", "")
                if status not in ("won", "lost", "push"):
                    continue
                mkt = row.get("market", "")
                stake = float(row.get("stake_amount", 0))
                pnl = float(row.get("pnl", 0))
                date = row.get("match_date", "")[:10]
                # Marktgruppe
                if mkt in ("home", "draw", "away"):
                    grp = "1x2"
                elif "o/u2.5" in mkt or "o/u1.5" in mkt or "o/u3.5" in mkt:
                    grp = "ou25"
                elif "btts" in mkt:
                    grp = "btts"
                else:
                    grp = "other"
                stats[grp]["n"] += 1
                stats[grp]["won"] += 1 if status == "won" else 0
                stats[grp]["staked"] += stake
                stats[grp]["pnl"] += pnl
                balance += pnl
                if date:
                    daily_balance[date] = round(balance, 2)
        bankroll_series = [{"date": "2026-06-11", "balance": 100.0}] + [
            {"date": d, "balance": b}
            for d, b in sorted(daily_balance.items())
            if d > "2026-06-11"
        ]
        # Compute hit-rates
        for grp in stats:
            d = stats[grp]
            d["hit_rate"] = round(d["won"] / d["n"] * 100, 1) if d["n"] > 0 else None
            d["roi"] = round(d["pnl"] / d["staked"] * 100, 1) if d["staked"] > 0 else None
            d["staked"] = round(d["staked"], 2)
            d["pnl"] = round(d["pnl"], 2)
        return {"stats": stats, "series": bankroll_series}
    except Exception:
        return {}


def _get_closed_bets() -> list[dict]:
    if not _LEDGER_PATH.exists():
        return []
    try:
        with open(_LEDGER_PATH, newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("status") in ("won", "lost", "push")]
    except Exception:
        return []


def _market_to_odds_key(market: str) -> str | None:
    """Map a ledger market string to the key used in all_odds dicts."""
    m = market.lower().strip()
    if m == "home":   return "home"
    if m == "away":   return "away"
    if m == "draw":   return "draw"
    if "_over"  in m: return "over25"
    if "_under" in m: return "under25"
    if m == "btts_yes": return "btts_yes"
    if m == "btts_no":  return "btts_no"
    if m in ("dc_1x", "dc_x2", "dc_12"): return m
    ah = re.match(r"ah[+\-]?(\d+\.?\d*)_(home|away)", m)
    if ah:
        return f"ah{ah.group(1)}_{ah.group(2)}"
    return None


def _get_open_bets_from_ledger(all_odds: dict | None = None) -> list[dict]:
    """Read open bets directly from ledger — always authoritative, never stale.

    If all_odds is provided, enrich each bet with current_odds, drift_pct,
    and clv_signal using the current market prices.
    """
    if not _LEDGER_PATH.exists():
        return []
    try:
        rows = []
        with open(_LEDGER_PATH, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") != "open":
                    continue
                home = r["home"]
                away = r["away"]
                market = r["market"]
                entry_odds = float(r["decimal_odds"])

                current_odds = None
                drift_pct = None
                clv_signal = None

                if all_odds:
                    try:
                        mk_lower = f"{home.lower()} vs {away.lower()}"
                        odds_block = next(
                            (v for k, v in all_odds.items() if k.lower() == mk_lower), None
                        )
                        odds_key = _market_to_odds_key(market)
                        if odds_key and odds_block is not None:
                            raw = odds_block.get(odds_key)
                            if raw is not None:
                                current_odds = float(raw)
                                if entry_odds and entry_odds > 0:
                                    drift_pct = round((current_odds - entry_odds) / entry_odds * 100, 1)
                                    if drift_pct > 2:
                                        clv_signal = "good"
                                    elif drift_pct < -2:
                                        clv_signal = "bad"
                    except Exception:
                        pass

                rows.append({
                    "match": f"{home} vs {away}",
                    "home": home,
                    "away": away,
                    "market": market,
                    "entry_odds": entry_odds,
                    "current_odds": current_odds,
                    "drift_pct": drift_pct,
                    "clv_signal": clv_signal,
                    "stake": float(r["stake_amount"]),
                    "match_date": r.get("match_date", ""),
                    "model_edge_pct": None,
                })
        return rows
    except Exception:
        return []


def _drop_finished_signals(signals: list[dict]) -> list[dict]:
    """Remove signals whose match kicked off more than 100 minutes ago."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=100)
    result = []
    for s in signals:
        ko = s.get("kickoff", "")
        if not ko:
            result.append(s)
            continue
        try:
            ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
            if ko_dt > cutoff:
                result.append(s)
        except ValueError:
            result.append(s)
    return result


def write_signals_json(
    football: list[BetSignal] | None = None,
    tennis: list[BetSignal] | None = None,
    portfolio: dict | None = None,
    top_elo: list[tuple[str, float]] | None = None,
    tennis_tour_map: dict[str, str] | None = None,
    kickoff_map: dict[str, str] | None = None,
    schedule: list[dict] | None = None,
    all_odds: dict[str, dict] | None = None,
    model_tips: dict[str, dict] | None = None,
    open_bets: list[dict] | None = None,
    odds_history: dict | None = None,  # {match_key: [{date, home, draw, away}]}
    wm_results: list[dict] | None = None,  # [{home, away, home_score, away_score, commence_time}]
) -> None:
    """
    Writes (or merges into) docs/data/signals.json.
    Merges football and tennis so each scanner can call independently.

    schedule: optional list of all upcoming matches (not just value bets) —
              each dict: {sport, home, away, kickoff, tour?}
    tennis_tour_map: optional {match_id: "atp"|"wta"} — adds tour field to tennis signals
    kickoff_map: optional {match_id: "ISO-8601"} — adds kickoff time to all signals
    """
    football = football or []
    tennis = tennis or []
    portfolio = portfolio or {}
    top_elo = top_elo or []
    tennis_tour_map = tennis_tour_map or {}
    kickoff_map = kickoff_map or {}

    # Load existing JSON to merge sport sections
    existing: dict = {}
    if _JSON_PATH.exists():
        try:
            existing = json.loads(_JSON_PATH.read_text())
        except Exception:
            existing = {}

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    football_data = [
        _signal_to_dict(s, "football", kickoff=kickoff_map.get(s.match_id, ""))
        for s in football
    ] if football else existing.get("football", [])

    if tennis:
        tennis_data = [
            _signal_to_dict(
                s, "tennis",
                tour=tennis_tour_map.get(s.match_id, ""),
                kickoff=kickoff_map.get(s.match_id, ""),
            )
            for s in tennis
        ]
    else:
        tennis_data = existing.get("tennis", [])

    # Remove signals for matches that ended > 100 minutes ago
    football_data = _drop_finished_signals(football_data)
    tennis_data   = _drop_finished_signals(tennis_data)

    if schedule is not None:
        schedule_data = schedule
    else:
        schedule_data = existing.get("schedule", [])

    if all_odds is not None:
        all_odds_data = all_odds
    else:
        all_odds_data = existing.get("all_odds", {})

    if model_tips is not None:
        model_tips_data = model_tips
    else:
        model_tips_data = existing.get("model_tips", {})

    # Compute bankroll state from ledger — always read from ledger when not explicitly passed
    # (avoids stale phantom bets persisting in KV from old JSON)
    _resolved_open_bets = open_bets if open_bets is not None else _get_open_bets_from_ledger(
        all_odds=all_odds_data if all_odds_data else None
    )
    _staked = sum(float(b.get("stake", 0)) for b in (_resolved_open_bets or []))
    _max_win = sum(
        float(b.get("stake", 0)) * (float(b.get("current_odds") or b.get("entry_odds", 0)) - 1)
        for b in (_resolved_open_bets or [])
        if b.get("current_odds") or b.get("entry_odds")
    )
    _bankroll_start = 100.0
    _pnl_closed = sum(float(row.get("pnl", 0)) for row in _get_closed_bets())
    _free = round(_bankroll_start + _pnl_closed - _staked, 2)
    _exposure_pct = round(_staked / _bankroll_start * 100, 1)

    payload = {
        "updated":        updated,
        "schedule":       schedule_data,
        "all_odds":       all_odds_data,
        "model_tips":     model_tips_data,
        "football":       football_data,
        "tennis":         tennis_data,
        "portfolio":      portfolio if portfolio else existing.get("portfolio", {}),
        "top_elo":        [{"name": n, "rating": round(r)} for n, r in top_elo] if top_elo else existing.get("top_elo", []),
        "history":        _build_history(),
        "open_bets":      _resolved_open_bets,
        "bankroll_state": {
            "start":        _bankroll_start,
            "free":         round(_free, 2),
            "staked":       round(_staked, 2),
            "exposure_pct": _exposure_pct,
            "max_win":      round(_max_win, 2),
            "pnl_closed":   round(_pnl_closed, 2),
        },
        "wm_stats": _build_wm_stats(),
    }
    payload["odds_history"] = odds_history if odds_history is not None else existing.get("odds_history", {})

    # WM Results: merge auto-fetched scores with existing — never overwrite existing entries
    _wm_results_base: list[dict] = wm_results if wm_results is not None else existing.get("wm_results", [])
    try:
        from src.data.odds_api import fetch_wm_scores as _fetch_wm_scores
        _fetched = _fetch_wm_scores(days_from=14)
        # Build lookup: update existing entries if score was missing, add new ones
        _existing_map = {
            (e.get("home", ""), e.get("away", "")): i
            for i, e in enumerate(_wm_results_base)
        }
        for _m in _fetched:
            _key = (_m.get("home", ""), _m.get("away", ""))
            _entry = {
                "home": _m.get("home", ""),
                "away": _m.get("away", ""),
                "home_score": _m.get("home_score"),
                "away_score": _m.get("away_score"),
                "commence_time": _m.get("commence_time", ""),
            }
            if _key in _existing_map:
                # Overwrite if existing entry has no score yet
                _idx = _existing_map[_key]
                if _wm_results_base[_idx].get("home_score") is None and _entry["home_score"] is not None:
                    _wm_results_base[_idx] = _entry
            else:
                _wm_results_base.append(_entry)
                _existing_map[_key] = len(_wm_results_base) - 1
    except Exception:
        pass  # silently keep existing wm_results on any error
    payload["wm_results"] = _wm_results_base

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    upload_signals_to_cloud()


def upload_signals_to_cloud(path: Path | None = None) -> bool:
    """Upload signals.json to Cloudflare Worker KV. No-op if env vars not set."""
    try:
        import requests as _req
    except ImportError:
        return False

    url = os.getenv("SIGNALS_CLOUD_URL")
    token = os.getenv("SIGNALS_API_TOKEN")
    if not url or not token:
        return False

    # Worker POST endpoint is /signals, GET is /signals.json — strip suffix for write
    post_url = url[: -len("/signals.json")] + "/signals" if url.endswith("/signals.json") else url

    target = path or _JSON_PATH
    if not target.exists():
        return False

    try:
        data = target.read_bytes()
        r = _req.post(
            post_url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False
