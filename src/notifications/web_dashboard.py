"""
Writes docs/data/signals.json for the GitHub Pages web dashboard.
Called at the end of daily_scan.py and tennis_scan.py.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.betting.value_detector import BetSignal
from src.config import DEFAULT_USER as _DEFAULT_USER, ODDS_MOVE_WARN_PCT
from src.utils.atomic_io import atomic_write_json
from src.signals.signal_status import (
    load_odds_state,
    make_signal_id,
    merge_odds_state_into_signal,
    seed_initial_odds,
)


def _build_info() -> dict:
    """Return {sha, date} for footer pill. SHA from env (CI) or git, never raises."""
    sha = os.environ.get("GITHUB_SHA") or os.environ.get("GIT_SHA") or ""
    if not sha:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[2],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()
        except Exception:
            sha = ""
    return {
        "sha": sha[:7],
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

ROOT = Path(__file__).parent.parent.parent
_JSON_PATH = ROOT / "docs" / "data" / "signals.json"
# Multi-User-Schema (D4): default user's ledger is the legacy single-user
# input. `build()` accepts an explicit `user` to write `signals_{user}.json`.
from src.config import ledger_path_for as _ledger_path_for, DEFAULT_USER as _DEFAULT_USER_CFG
_LEDGER_PATH = _ledger_path_for(_DEFAULT_USER_CFG)


def list_known_users() -> list[str]:
    """Returns sorted user-slugs discovered from `results/ledger_{user}.csv` files.
    The default user is always included even if no ledger has been created yet.
    Backup/intermediate files (e.g. `ledger_pre_clv_backfill_*.csv`) are excluded."""
    users: set[str] = {_DEFAULT_USER_CFG}
    for p in (ROOT / "results").glob("ledger_*.csv"):
        slug = p.stem[len("ledger_"):]
        if not slug or len(slug) > 32:
            continue
        # exclude generated backup/intermediate files
        if any(k in slug for k in ("backup", "backfill", "snapshot")):
            continue
        # only [a-z0-9_-]
        cleaned = "".join(c for c in slug.lower() if c.isalnum() or c in "_-")
        if cleaned != slug.lower():
            continue
        users.add(slug)
    return sorted(users)


def write_signals_json_all_users(**kwargs) -> list[str]:
    """D4: Calls `write_signals_json` once per known user (auto-discovery via
    `list_known_users`). Each call uses the user's own ledger for per-user
    bankroll/open/settled state but identical scored signals.

    Returns list of users whose cloud upload FAILED. Callers must check this
    and fail loud — silent cloud-upload failures were the root cause of the
    2026-07-06 PWA staleness incident.
    """
    kwargs.pop("user", None)
    failed: list[str] = []
    for u in list_known_users():
        ok = write_signals_json(user=u, **kwargs)
        if ok is False:
            failed.append(u)
    return failed

# FIFA-Konföderation-Mapping (WM 2026 Teams + WM-Qualifikations-Backtest)
CONFEDERATION_MAP: dict[str, str] = {
    # UEFA
    "Germany": "UEFA", "Netherlands": "UEFA", "France": "UEFA", "Spain": "UEFA",
    "England": "UEFA", "Belgium": "UEFA", "Portugal": "UEFA", "Switzerland": "UEFA",
    "Croatia": "UEFA", "Austria": "UEFA", "Czechia": "UEFA", "Czech Republic": "UEFA",
    "Norway": "UEFA", "Sweden": "UEFA", "Scotland": "UEFA", "Turkey": "UEFA",
    "Italy": "UEFA", "Denmark": "UEFA", "Poland": "UEFA", "Serbia": "UEFA",
    "Greece": "UEFA", "Romania": "UEFA", "Ukraine": "UEFA", "Hungary": "UEFA",
    "Wales": "UEFA", "Iceland": "UEFA", "Slovakia": "UEFA", "Albania": "UEFA",
    "Russia": "UEFA", "Finland": "UEFA", "Ireland": "UEFA", "Bulgaria": "UEFA",
    "Bosnia and Herzegovina": "UEFA", "Bosnia & Herzegovina": "UEFA",
    "Slovenia": "UEFA", "Belarus": "UEFA", "North Macedonia": "UEFA",
    "Cyprus": "UEFA", "Estonia": "UEFA", "Latvia": "UEFA", "Lithuania": "UEFA",
    "Luxembourg": "UEFA", "Moldova": "UEFA", "Montenegro": "UEFA",
    "Northern Ireland": "UEFA", "Kazakhstan": "UEFA", "Kosovo": "UEFA",
    "Andorra": "UEFA", "Liechtenstein": "UEFA", "San Marino": "UEFA",
    "Gibraltar": "UEFA", "Faroe Islands": "UEFA", "Malta": "UEFA",
    "Armenia": "UEFA", "Azerbaijan": "UEFA", "Georgia": "UEFA", "Israel": "UEFA",
    # CONMEBOL
    "Brazil": "CONMEBOL", "Argentina": "CONMEBOL", "Paraguay": "CONMEBOL",
    "Uruguay": "CONMEBOL", "Colombia": "CONMEBOL", "Ecuador": "CONMEBOL",
    "Chile": "CONMEBOL", "Peru": "CONMEBOL", "Venezuela": "CONMEBOL",
    "Bolivia": "CONMEBOL",
    # CONCACAF
    "Mexico": "CONCACAF", "United States": "CONCACAF", "USA": "CONCACAF",
    "Canada": "CONCACAF", "Haiti": "CONCACAF", "Panama": "CONCACAF",
    "Curacao": "CONCACAF", "Curaçao": "CONCACAF", "Honduras": "CONCACAF",
    "Costa Rica": "CONCACAF", "Jamaica": "CONCACAF", "Trinidad and Tobago": "CONCACAF",
    "El Salvador": "CONCACAF", "Guatemala": "CONCACAF",
    # AFC
    "South Korea": "AFC", "Qatar": "AFC", "Australia": "AFC", "Japan": "AFC",
    "Iran": "AFC", "Saudi Arabia": "AFC", "Iraq": "AFC", "Uzbekistan": "AFC",
    "Jordan": "AFC", "China": "AFC", "India": "AFC", "Vietnam": "AFC",
    "UAE": "AFC", "United Arab Emirates": "AFC", "Oman": "AFC", "Bahrain": "AFC",
    "Kuwait": "AFC", "Thailand": "AFC", "Philippines": "AFC", "Indonesia": "AFC",
    "Malaysia": "AFC", "Lebanon": "AFC", "Syria": "AFC", "Palestine": "AFC",
    "Yemen": "AFC", "Hong Kong": "AFC", "Singapore": "AFC", "Tajikistan": "AFC",
    "Kyrgyzstan": "AFC", "Turkmenistan": "AFC", "North Korea": "AFC",
    "Myanmar": "AFC", "Cambodia": "AFC", "Laos": "AFC", "Bangladesh": "AFC",
    "Sri Lanka": "AFC", "Pakistan": "AFC", "Maldives": "AFC", "Bhutan": "AFC",
    "Nepal": "AFC", "Mongolia": "AFC", "Macau": "AFC", "Brunei": "AFC",
    "Chinese Taipei": "AFC", "East Timor": "AFC", "Guam": "AFC",
    "Northern Mariana Islands": "AFC",
    # CAF
    "South Africa": "CAF", "Morocco": "CAF", "Cote d'Ivoire": "CAF",
    "Ivory Coast": "CAF", "Tunisia": "CAF", "Egypt": "CAF", "Cape Verde": "CAF",
    "Senegal": "CAF", "Algeria": "CAF", "DR Congo": "CAF", "Ghana": "CAF",
    "Nigeria": "CAF", "Cameroon": "CAF", "Mali": "CAF", "Burkina Faso": "CAF",
    "Guinea": "CAF", "Zambia": "CAF", "Angola": "CAF", "Kenya": "CAF",
    "Uganda": "CAF", "Tanzania": "CAF", "Ethiopia": "CAF", "Sudan": "CAF",
    "Zimbabwe": "CAF", "Mozambique": "CAF", "Madagascar": "CAF", "Gabon": "CAF",
    "Congo": "CAF", "Equatorial Guinea": "CAF", "Central African Republic": "CAF",
    "Botswana": "CAF", "Namibia": "CAF", "Malawi": "CAF", "Rwanda": "CAF",
    "Burundi": "CAF", "Sierra Leone": "CAF", "Liberia": "CAF", "Togo": "CAF",
    "Benin": "CAF", "Niger": "CAF", "Chad": "CAF", "Mauritania": "CAF",
    "Gambia": "CAF", "Guinea-Bissau": "CAF", "Comoros": "CAF",
    "Lesotho": "CAF", "Eswatini": "CAF", "Mauritius": "CAF",
    "Sao Tome and Principe": "CAF", "Seychelles": "CAF", "Djibouti": "CAF",
    "Somalia": "CAF", "Eritrea": "CAF", "South Sudan": "CAF", "Libya": "CAF",
    # OFC
    "New Zealand": "OFC", "Fiji": "OFC", "Papua New Guinea": "OFC",
    "Solomon Islands": "OFC", "Vanuatu": "OFC", "Tahiti": "OFC", "Samoa": "OFC",
    "Tonga": "OFC", "Cook Islands": "OFC", "American Samoa": "OFC",
}


def _confederation(team: str) -> str:
    return CONFEDERATION_MAP.get(team, "Other")


def _market_group(mkt: str) -> str:
    if mkt in ("home", "draw", "away"):
        return "1X2"
    if mkt.startswith("o/u") and "_over" in mkt:
        return "Over"
    if mkt.startswith("o/u") and "_under" in mkt:
        return "Under"
    if mkt.startswith("ah"):
        return "AH"
    if mkt.startswith("btts"):
        return "BTTS"
    if "goals_2_4" in mkt:
        return "2-4 Tore"
    if mkt.startswith("dc_"):
        return "Double Chance"
    if mkt.startswith("first_set"):
        return "1. Satz"
    return "Sonstige"


def _build_history(n_days: int = 30, ledger_path: Path | None = None) -> list[dict]:
    """Read ledger CSV and return daily P&L history (most recent first)."""
    lp = ledger_path or _LEDGER_PATH
    if not lp.exists():
        return []
    try:
        daily: dict[str, dict] = defaultdict(lambda: {"n_bets": 0, "staked": 0.0, "pnl": 0.0})
        with open(lp, newline="") as f:
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


def _build_player_form_cache() -> dict[str, list[str]]:
    """Last-5 W/L per tennis player from signal_history.jsonl."""
    hist_path = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "signal_history.jsonl"
    if not hist_path.exists():
        return {}
    from collections import defaultdict
    player_results: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with hist_path.open() as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line:
                continue
            try:
                row = json.loads(_line)
            except json.JSONDecodeError:
                continue
            if row.get("sport") != "tennis":
                continue
            if row.get("outcome") not in ("won", "lost"):
                continue
            ts = row.get("outcome_ts") or row.get("scan_ts") or ""
            market = row.get("market", "")
            home, away = row.get("home", ""), row.get("away", "")
            player = home if market in ("home", "ah-1.5_a") else away
            if player:
                player_results[player].append((ts, row["outcome"]))
    return {
        p: ["W" if o == "won" else "L" for _, o in sorted(rs, key=lambda x: x[0])[-5:]]
        for p, rs in player_results.items()
    }


def _signal_to_dict(
    s: BetSignal,
    sport: str = "football",
    tour: str = "",
    kickoff: str = "",
    tournament_meta: dict | None = None,
    generated_at: str = "",
    form_a: list[str] | None = None,
    form_b: list[str] | None = None,
) -> dict:
    from datetime import datetime, timezone
    _match_str = f"{s.home} vs {s.away}"
    # Wave 3B.1: tennis signals use the stable fixture registry for signal_id
    # so that cross-midnight reschedules preserve identity.
    _sport_key = tournament_meta.get("sport_key", "") if tournament_meta else ""
    if sport == "tennis" and _sport_key:
        try:
            from src.tennis.fixture_registry import get_or_register as _get_stable_id
            _signal_id = _get_stable_id(_sport_key, s.home, s.away, s.market, kickoff)
        except (ImportError, OSError, ValueError):
            _signal_id = make_signal_id(sport, _match_str, s.market, kickoff)
    else:
        _signal_id = make_signal_id(sport, _match_str, s.market, kickoff)
    d = {
        "signal_id":       _signal_id,
        "sport":           sport,
        "match":           _match_str,
        "market":          s.market,
        "odds":            round(s.decimal_odds, 2),
        "model_prob":      round(s.model_prob * 100, 1),
        "fair_prob":       round(s.fair_prob * 100, 1),
        "ev_pct":          round(s.ev * 100, 1),
        "stake_eur":       round(s.stake_eur, 2),
        "stake_pct":       round(s.stake_pct * 100, 1),
        "theoretical_stake_eur": round(getattr(s, "theoretical_stake_eur", s.stake_eur), 2),
        "cap_applied":     bool(getattr(s, "cap_applied", False)),
        "confidence":      s.confidence,
        "n_models_agree":  s.n_models_agree,
        "generated_at":    generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if form_a:
        d["form_a"] = form_a
    if form_b:
        d["form_b"] = form_b
    if getattr(s, "stake_reason", ""):
        d["correlation_note"] = s.stake_reason
    if getattr(s, "no_bet_flag", False):
        d["no_bet_flag"] = True
    if getattr(s, "conflict_reason", ""):
        d["conflict_reason"] = s.conflict_reason
    if tour:
        d["tour"] = tour
    if kickoff:
        d["kickoff"] = kickoff
    if getattr(s, "league", ""):
        # 2.BL/WM/... Registry-Short → PWA-Gruppierung + Filter-Chips
        d["league"] = s.league
    if tournament_meta:
        # J2-E: Tennis-Tournament-Meta für PWA-Gruppierung
        d["tournament"] = tournament_meta.get("name", "")
        d["category"]   = tournament_meta.get("category", "")
        d["surface"]    = tournament_meta.get("surface", "")
        d["best_of"]    = tournament_meta.get("best_of", 0)
    return d


def _build_wm_stats(ledger_path: Path | None = None) -> dict:
    """Aggregiert WM-Performance-Stats aus dem Ledger.

    Liefert: stats (per Markt), series (Bankroll-Verlauf täglich),
    drawdown (current/max + Peak), clv_dist + edge_dist (Histogramme),
    summary (Lifetime ROI/Yield/Mean-CLV/Mean-Edge).
    """
    lp = ledger_path or _LEDGER_PATH
    if not lp.exists():
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
        # Histogram bins (Prozentpunkte) — Edges sind (links, rechts) halboffen [l, r)
        # CLV-Bins: <-5 / [-5,-2) / [-2,0) / [0,+2) / [+2,+5) / >=+5  (in % vom Opening)
        clv_edges = [-100, -5, -2, 0, 2, 5, 1000]
        clv_labels = ["≤-5%", "-5/-2%", "-2/0%", "0/+2%", "+2/+5%", "≥+5%"]
        clv_bins = [0] * len(clv_labels)
        # Edge-Bins: pp = model_prob - implied_market_prob (in pp)
        edge_edges = [-100, 0, 3, 6, 10, 15, 1000]
        edge_labels = ["≤0pp", "0-3pp", "3-6pp", "6-10pp", "10-15pp", "≥15pp"]
        edge_bins = [0] * len(edge_labels)
        clv_values: list[float] = []
        clv_values_30d: list[float] = []
        edge_values: list[float] = []
        # 30-day rolling window threshold (placed_date)
        from datetime import timedelta as _td
        _today_dt = datetime.now(timezone.utc).date()
        _cutoff_30d = (_today_dt - _td(days=30)).isoformat()
        total_staked = 0.0
        total_pnl = 0.0
        total_n = 0
        # Per-Team-Markt-Buckets (für Bet-History im Detail-View)
        per_team_market: dict[str, dict[str, dict]] = {}
        # Per-Konföderation-Aggregat (für Journal)
        by_confed: dict[str, dict] = {}
        with open(lp, newline="") as f:
            for row in sorted(csv.DictReader(f), key=lambda r: r.get("match_date", "")):
                status = row.get("status", "")
                if status not in ("won", "lost", "push", "void"):
                    continue
                # Void: only contribute to CLV aggregation (closing-line value still meaningful),
                # not to hit-rate / market-stats (staked is returned, no edge realized).
                _is_void = status == "void"
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
                if not _is_void:
                    stats[grp]["n"] += 1
                    stats[grp]["won"] += 1 if status == "won" else 0
                    stats[grp]["staked"] += stake
                    stats[grp]["pnl"] += pnl
                    balance += pnl
                    if date:
                        daily_balance[date] = round(balance, 2)
                    total_staked += stake
                    total_pnl += pnl
                    total_n += 1
                # Per-Team-Markt-Aggregate (für beide Teams im Match) — void aus Bias-Korrektur ausgeschlossen
                home_team = (row.get("home", "") or "").strip()
                away_team = (row.get("away", "") or "").strip()
                if not _is_void:
                    mg = _market_group(mkt)
                    for team in (home_team, away_team):
                        if not team:
                            continue
                        per_team_market.setdefault(team, {}).setdefault(mg, {
                            "n": 0, "won": 0, "staked": 0.0, "pnl": 0.0,
                        })
                        bucket = per_team_market[team][mg]
                        bucket["n"] += 1
                        bucket["won"] += 1 if status == "won" else 0
                        bucket["staked"] += stake
                        bucket["pnl"] += pnl
                    # Per-Konföderation (klassifiziere via Home-Team)
                    confed = _confederation(home_team) if home_team else "Other"
                    by_confed.setdefault(confed, {
                        "n": 0, "won": 0, "staked": 0.0, "pnl": 0.0,
                    })
                    by_confed[confed]["n"] += 1
                    by_confed[confed]["won"] += 1 if status == "won" else 0
                    by_confed[confed]["staked"] += stake
                    by_confed[confed]["pnl"] += pnl
                # CLV in % (placed_odds / closing_odds - 1) * 100  — positiv = wir hatten bessere Quote als Markt am Schluss
                # Void wird HIER mitgezählt: Closing-Line-Value bleibt aussagekräftig auch wenn Wette annulliert.
                try:
                    placed_odds = float(row.get("decimal_odds") or 0)
                    closing_odds = float(row.get("closing_odds") or 0)
                    if placed_odds > 1.0 and closing_odds > 1.0:
                        clv_pct = (placed_odds / closing_odds - 1.0) * 100.0
                        clv_values.append(clv_pct)
                        placed_date = (row.get("placed_date", "") or "")[:10]
                        if placed_date and placed_date >= _cutoff_30d:
                            clv_values_30d.append(clv_pct)
                        for i in range(len(clv_labels)):
                            if clv_edges[i] <= clv_pct < clv_edges[i + 1]:
                                clv_bins[i] += 1
                                break
                except (TypeError, ValueError):
                    pass
                # Edge in pp (Modell-% − Markt-implied-%) — nur für non-void
                if _is_void:
                    continue
                try:
                    model_prob = float(row.get("model_prob") or 0)
                    placed_odds = float(row.get("decimal_odds") or 0)
                    if 0 < model_prob < 1 and placed_odds > 1.0:
                        edge_pp = (model_prob - 1.0 / placed_odds) * 100.0
                        edge_values.append(edge_pp)
                        for i in range(len(edge_labels)):
                            if edge_edges[i] <= edge_pp < edge_edges[i + 1]:
                                edge_bins[i] += 1
                                break
                except (TypeError, ValueError):
                    pass
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
        # Drawdown auf bankroll_series
        peak = 100.0
        max_dd = 0.0
        max_dd_pct = 0.0
        for pt in bankroll_series:
            b = pt["balance"]
            if b > peak:
                peak = b
            dd = peak - b
            dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        current = bankroll_series[-1]["balance"] if bankroll_series else 100.0
        current_dd = max(0.0, peak - current)
        current_dd_pct = (current_dd / peak * 100.0) if peak > 0 else 0.0
        drawdown = {
            "peak":            round(peak, 2),
            "current":         round(current, 2),
            "current_dd":      round(current_dd, 2),
            "current_dd_pct":  round(current_dd_pct, 2),
            "max_dd":          round(max_dd, 2),
            "max_dd_pct":      round(max_dd_pct, 2),
        }
        # Summary
        mean_clv = round(sum(clv_values) / len(clv_values), 2) if clv_values else None
        mean_clv_30d = round(sum(clv_values_30d) / len(clv_values_30d), 2) if clv_values_30d else None
        mean_edge = round(sum(edge_values) / len(edge_values), 2) if edge_values else None
        yield_pct = round(total_pnl / total_staked * 100.0, 2) if total_staked > 0 else None
        summary = {
            "n_settled":   total_n,
            "staked":      round(total_staked, 2),
            "pnl":         round(total_pnl, 2),
            "yield_pct":   yield_pct,
            "mean_clv":    mean_clv,
            "mean_clv_30d": mean_clv_30d,
            "mean_edge":   mean_edge,
            "n_clv":       len(clv_values),
            "n_clv_30d":   len(clv_values_30d),
            "n_edge":      len(edge_values),
        }
        # Per-Team-Markt: Hit-Rate + ROI berechnen
        ptm_out: dict[str, dict[str, dict]] = {}
        for team, by_m in per_team_market.items():
            ptm_out[team] = {}
            for mg, b in by_m.items():
                ptm_out[team][mg] = {
                    "n":        b["n"],
                    "won":      b["won"],
                    "staked":   round(b["staked"], 2),
                    "pnl":      round(b["pnl"], 2),
                    "hit_rate": round(b["won"] / b["n"] * 100, 1) if b["n"] > 0 else None,
                    "roi":      round(b["pnl"] / b["staked"] * 100, 1) if b["staked"] > 0 else None,
                }
        # Per-Konföderation
        confed_out: dict[str, dict] = {}
        for c, b in by_confed.items():
            confed_out[c] = {
                "n":        b["n"],
                "won":      b["won"],
                "staked":   round(b["staked"], 2),
                "pnl":      round(b["pnl"], 2),
                "hit_rate": round(b["won"] / b["n"] * 100, 1) if b["n"] > 0 else None,
                "roi":      round(b["pnl"] / b["staked"] * 100, 1) if b["staked"] > 0 else None,
            }
        return {
            "stats":           stats,
            "series":          bankroll_series,
            "drawdown":        drawdown,
            "clv_dist":        {"labels": clv_labels, "bins": clv_bins},
            "edge_dist":       {"labels": edge_labels, "bins": edge_bins},
            "summary":         summary,
            "per_team_market": ptm_out,
            "by_confederation": confed_out,
        }
    except Exception:
        return {}


def _build_tennis_stats(ledger_path: Path | None = None) -> dict:
    """Tennis-Performance-Stats aus dem Ledger (Roadmap TENNIS P1.5).

    Aggregiert Tennis-Bets nach Kategorie/Surface/Markt/Tour + liefert
    aktive Turniere und Live-Gate-Status. Leerer Return {} nur bei
    komplettem Fehler - normales Off-Season-Empty ist Zero-Aggregates.
    """
    from src.betting.tennis_settlement import is_tennis_market
    lp = ledger_path or _LEDGER_PATH
    if not lp.exists():
        return {}

    # ── Aktive Turniere holen (Discovery) ─────────────────────────────────
    active_tournaments: list[dict] = []
    try:
        from src.tennis.discovery import discover_active_tournaments
        for t in discover_active_tournaments():
            active_tournaments.append({
                "slug": t.slug,
                "name": t.name,
                "category": t.category,
                "surface": t.surface,
                "best_of": t.best_of,
            })
    except Exception:
        pass

    # ── Live-Gate-Status ──────────────────────────────────────────────────
    try:
        from src.config import TENNIS_CATEGORY_MODE, TENNIS_CATEGORY_SURFACE_MODE
        gate_status = {
            "category_mode": dict(TENNIS_CATEGORY_MODE),
            "surface_overrides": [
                {"category": k[0], "surface": k[1], "mode": v}
                for k, v in TENNIS_CATEGORY_SURFACE_MODE.items()
            ],
        }
    except Exception:
        gate_status = {}

    # ── Ledger-Aggregation ────────────────────────────────────────────────
    stats = {
        "n_bets_all": 0, "n_bets_settled": 0, "n_open": 0,
        "n_won": 0, "n_lost": 0, "n_push": 0,
        "total_staked": 0.0, "total_pnl": 0.0,
        "roi_pct": None, "clv_mean": None,
        "by_market": {},
        "by_tour": {"atp": {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0},
                    "wta": {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0}},
        "by_surface": {},
    }

    try:
        with open(lp, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        rows = []

    tennis_rows = []
    for r in rows:
        market = r.get("market", "")
        src = (r.get("source") or "").lower()
        reason = (r.get("stake_reason") or "").lower()
        if is_tennis_market(market) or "tennis" in src or "tennis" in reason:
            tennis_rows.append(r)

    stats["n_bets_all"] = len(tennis_rows)
    clv_values: list[float] = []

    for r in tennis_rows:
        status = r.get("status", "").lower()
        market = r.get("market", "")
        row_src = (r.get("source") or "").lower()
        row_reason = (r.get("stake_reason") or "").lower()
        try:
            stake = float(r.get("stake_amount", 0) or 0)
            pnl = float(r.get("pnl", 0) or 0)
        except (ValueError, TypeError):
            stake, pnl = 0.0, 0.0

        if status == "open":
            stats["n_open"] += 1
            continue
        if status in ("won", "lost", "push"):
            stats["n_bets_settled"] += 1
            stats[f"n_{status}"] += 1
            stats["total_staked"] += stake
            stats["total_pnl"] += pnl

            # by_market
            bm = stats["by_market"].setdefault(market, {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0})
            bm["n"] += 1
            bm["staked"] += stake
            bm["pnl"] += pnl
            if status == "won":
                bm["won"] += 1

            # by_tour - heuristic: source/stake_reason/match_id contains 'wta' or 'atp'
            tour = None
            for hint in (row_src, row_reason, str(r.get("match_id", "")).lower()):
                if "wta" in hint:
                    tour = "wta"; break
                if "atp" in hint:
                    tour = "atp"; break
            if tour:
                bt = stats["by_tour"][tour]
                bt["n"] += 1
                bt["staked"] += stake
                bt["pnl"] += pnl
                if status == "won":
                    bt["won"] += 1

            # by_surface — aus match_id heuristic (enthält oft surface-code) oder stake_reason
            surf_hint = " ".join([row_src, row_reason, str(r.get("match_id", ""))]).lower()
            surf = None
            for s_key in ("hard", "clay", "grass", "carpet"):
                if s_key in surf_hint:
                    surf = s_key; break
            if surf:
                bs = stats["by_surface"].setdefault(surf, {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0})
                bs["n"] += 1
                bs["staked"] += stake
                bs["pnl"] += pnl
                if status == "won":
                    bs["won"] += 1

            # CLV
            try:
                clv = float(r.get("clv") or "nan")
                if -1 < clv < 3:
                    clv_values.append(clv)
            except (ValueError, TypeError):
                pass

    if stats["total_staked"] > 0:
        stats["roi_pct"] = round(100 * stats["total_pnl"] / stats["total_staked"], 3)
    if clv_values:
        stats["clv_mean"] = round(sum(clv_values) / len(clv_values), 4)

    # Round monetary sums
    stats["total_staked"] = round(stats["total_staked"], 2)
    stats["total_pnl"] = round(stats["total_pnl"], 2)
    for bm in stats["by_market"].values():
        bm["pnl"] = round(bm["pnl"], 2)
        bm["staked"] = round(bm["staked"], 2)
    for bt in stats["by_tour"].values():
        bt["pnl"] = round(bt["pnl"], 2)
        bt["staked"] = round(bt["staked"], 2)
    for bs in stats["by_surface"].values():
        bs["pnl"] = round(bs["pnl"], 2)
        bs["staked"] = round(bs["staked"], 2)
        if bs["staked"] > 0:
            bs["roi_pct"] = round(100 * bs["pnl"] / bs["staked"], 1)

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_tournaments": active_tournaments,
        "live_gate_status": gate_status,
        "stats": stats,
    }


def _get_closed_bets(ledger_path: Path | None = None) -> list[dict]:
    lp = ledger_path or _LEDGER_PATH
    if not lp.exists():
        return []
    try:
        with open(lp, newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("status") in ("won", "lost", "push")]
    except Exception:
        return []


def _get_settled_bets_for_dashboard(ledger_path: Path | None = None) -> list[dict]:
    lp = ledger_path or _LEDGER_PATH
    if not lp.exists():
        return []
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = []
        with open(lp, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") not in ("won", "lost", "push", "void"):
                    continue
                # Skip future-dated won/lost/push only (genuine data anomalies — match
                # not played yet but marked settled). Void with future date is
                # legitimate: bet was killed early (lineup change, market close).
                if r.get("status") in ("won", "lost", "push") and r.get("match_date", "") > today_str:
                    continue
                clv_val: float | None = None
                closing_val: float | None = None
                try:
                    co = float(r.get("closing_odds") or 0)
                    if co > 1.0:
                        closing_val = co
                except (TypeError, ValueError):
                    pass
                try:
                    cv = (r.get("clv") or "").strip()
                    if cv:
                        clv_val = float(cv)
                except (TypeError, ValueError):
                    pass
                rows.append({
                    "match":        f"{r['home']} vs {r['away']}",
                    "home":         r["home"],
                    "away":         r["away"],
                    "market":       r["market"],
                    "entry_odds":   float(r["decimal_odds"]),
                    "stake":        float(r["stake_amount"]),
                    "pnl":          float(r.get("pnl") or 0),
                    "match_date":   r.get("match_date", ""),
                    "status":       r["status"],
                    "clv":          clv_val,
                    "closing_odds": closing_val,
                })
        rows.sort(key=lambda x: x["match_date"], reverse=True)
        return rows
    except Exception:
        return []


def _market_to_odds_key(market: str) -> str | None:
    """Map a ledger market string to the key used in all_odds dicts."""
    m = market.lower().strip()
    if m == "home":   return "home"
    if m == "away":   return "away"
    if m == "draw":   return "draw"
    if m == "btts_yes": return "btts_yes"
    if m == "btts_no":  return "btts_no"
    if m in ("dc_1x", "dc_x2", "dc_12"): return m
    # Tennis first-set markets
    if m in ("first_set_a", "first_set_b"): return m
    # Tennis AH sets (ah+1.5_a / ah+1.5_b / ah-1.5_home etc.)
    ah = re.match(r"ah[+\-]?(\d+\.?\d*)_(home|away|a|b)$", m)
    if ah:
        side = "a" if ah.group(2) in ("home", "a") else "b"
        return f"ah{ah.group(1)}_{side}"
    # Tennis / football totals: o/u_games_22.5_over → over22.5_games
    ou_games = re.match(r"o/u_games_([\d.]+)_(over|under)$", m)
    if ou_games:
        return f"{ou_games.group(2)}{ou_games.group(1)}_games"
    # Football o/u sets/totals (legacy: anything with _over/_under left)
    if "_over"  in m: return "over25"
    if "_under" in m: return "under25"
    return None


def _get_open_bets_from_ledger(all_odds: dict | None = None, ledger_path: Path | None = None) -> list[dict]:
    """Read open bets directly from ledger — always authoritative, never stale.

    If all_odds is provided, enrich each bet with current_odds, drift_pct,
    and clv_signal using the current market prices.
    """
    lp = ledger_path or _LEDGER_PATH
    if not lp.exists():
        return []
    try:
        rows = []
        with open(lp, newline="") as f:
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


# Wave 3A: per-record freshness threshold for Tennis LIVE gate.
# Pipeline: GH Actions every 15 min → 30 min = 2× cadence, tolerates 1 missed push.
TENNIS_LIVE_RECORD_STALE_SEC = 30 * 60


def _tennis_bet_is_live(record: dict | None, now_utc: datetime) -> bool:
    """True only when the tennis live cache has fresh authoritative in_progress evidence.

    Fail-closed on every ambiguity:
    - no record → False
    - in_progress with no per-record timestamp → False
    - in_progress with stale per-record timestamp (>30 min) → False
    - in_progress with unparseable timestamp → False
    Suspended/postponed records are exempted from per-record freshness because
    they represent persistent states rather than transient match activity.
    """
    if not record:
        return False
    status = record.get("status", "")
    if status == "in_progress":
        rec_ts = record.get("updated", "")
        if not rec_ts:
            return False
        try:
            age = (now_utc - datetime.fromisoformat(str(rec_ts).replace("Z", "+00:00"))).total_seconds()
            return age <= TENNIS_LIVE_RECORD_STALE_SEC
        except (ValueError, TypeError):
            return False
    return status in ("suspended", "postponed")


def _drop_finished_signals(signals: list[dict]) -> list[dict]:
    """Remove signals whose match kicked off more than 100 minutes ago.

    Wave 3C: AWAITING_START / DELAYED matches are never dropped on kickoff alone —
    they haven't started even though the scheduled time has passed.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=100)
    result = []
    for s in signals:
        ev_status = s.get("event_status", "")
        # Authoritative terminal → drop
        if ev_status in ("COMPLETED", "CANCELLED"):
            continue
        # Waiting for start or explicitly delayed → keep regardless of kickoff
        if ev_status in ("AWAITING_START", "DELAYED"):
            result.append(s)
            continue
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


def _enrich_odds_freshness(signals: list[dict], odds_history: list) -> list[dict]:
    """N3: Compare current signal odds vs. most recent prior snapshot. Tags odds_move_pct."""
    if not odds_history:
        return signals
    # Find the most recent snapshot entry (sorted by ts or date)
    try:
        prev_entry = sorted(odds_history, key=lambda e: e.get("ts", e.get("date", "")))[-1]
    except Exception:
        return signals
    prev_odds_map = prev_entry.get("odds", {})
    side_map = {"home": "home", "away": "away", "draw": "draw"}
    for s in signals:
        side = side_map.get(s.get("market", ""))
        if not side:
            continue
        match_key = s.get("match", "")
        prev_match = prev_odds_map.get(match_key)
        if not prev_match:
            mk_lower = match_key.lower()
            prev_match = next(
                (v for k, v in prev_odds_map.items() if k.lower() == mk_lower), None
            )
        if not prev_match:
            continue
        prev_o = float(prev_match.get(side, 0) or 0)
        curr_o = float(s.get("odds", 0) or 0)
        if prev_o > 1.0 and curr_o > 1.0:
            move = (curr_o - prev_o) / prev_o
            s["odds_move_pct"] = round(move * 100, 1)
            s["odds_moved_against"] = move < -ODDS_MOVE_WARN_PCT
    return signals


def _enrich_best_bookie(signals: list[dict], all_odds: dict) -> list[dict]:
    """N4: Add best_bookie field to each signal using bookmakers_h2h data."""
    _SIDE_MAP = {"home": "home", "away": "away", "draw": "draw"}
    for s in signals:
        mkt = s.get("market", "")
        side = _SIDE_MAP.get(mkt)
        if not side:
            continue
        match_key = s.get("match", "")
        bkm_list = all_odds.get(match_key, {}).get("bookmakers_h2h", [])
        if not bkm_list:
            # try case-insensitive lookup
            mk_lower = match_key.lower()
            bkm_list = next(
                (v.get("bookmakers_h2h", []) for k, v in all_odds.items() if k.lower() == mk_lower),
                [],
            )
        if bkm_list:
            best = max(bkm_list, key=lambda b: float(b.get(side) or 0))
            odds_val = best.get(side)
            if odds_val and float(odds_val) > 1.0:
                s["best_bookie"] = {"name": best.get("title", ""), "odds": round(float(odds_val), 2)}
    return signals


def _tag_display_priority(signals: list[dict], top_n: int) -> list[dict]:
    """N1: Sort by ev_pct desc, tag top_n as 'top', rest as 'extra'."""
    sorted_sigs = sorted(signals, key=lambda s: float(s.get("ev_pct", 0)), reverse=True)
    for i, s in enumerate(sorted_sigs):
        s["display_priority"] = "top" if i < top_n else "extra"
    return sorted_sigs


def write_signals_json(
    football: list[BetSignal] | None = None,
    tennis: list[BetSignal] | None = None,
    portfolio: dict | None = None,
    top_elo: list[tuple[str, float]] | None = None,
    tennis_tour_map: dict[str, str] | None = None,
    tennis_tournament_map: dict[str, dict] | None = None,  # J2-E: {match_id: {name, category, surface, best_of}}
    kickoff_map: dict[str, str] | None = None,
    schedule: list[dict] | None = None,
    all_odds: dict[str, dict] | None = None,
    model_tips: dict[str, dict] | None = None,
    model_evals: dict[str, dict] | None = None,
    open_bets: list[dict] | None = None,
    odds_history: dict | None = None,  # {match_key: [{date, home, draw, away}]}
    wm_results: list[dict] | None = None,  # [{home, away, home_score, away_score, commence_time}]
    user: str = _DEFAULT_USER,  # D4: writes signals_{user}.json; default user mirrors signals.json
) -> bool:
    """
    Writes (or merges into) docs/data/signals_{user}.json (and `signals.json`
    for the default user, for backward compat).
    Merges football and tennis so each scanner can call independently.

    Returns True iff the local write succeeded AND the cloud upload succeeded
    (or was skipped because env vars are unset — that's not treated as a
    failure). Returns False when the cloud POST actually failed, so callers
    can fail loud.

    schedule: optional list of all upcoming matches (not just value bets) —
              each dict: {sport, home, away, kickoff, tour?}
    tennis_tour_map: optional {match_id: "atp"|"wta"} — adds tour field to tennis signals
    kickoff_map: optional {match_id: "ISO-8601"} — adds kickoff time to all signals
    """
    # Per-user routing (D4)
    json_path = ROOT / "docs" / "data" / f"signals_{user}.json"
    ledger_path = _ledger_path_for(user)
    football = football or []
    tennis = tennis or []
    portfolio = portfolio or {}
    top_elo = top_elo or []
    tennis_tour_map = tennis_tour_map or {}
    tennis_tournament_map = tennis_tournament_map or {}
    kickoff_map = kickoff_map or {}

    # Load existing JSON to merge sport sections.
    # HART FAIL bei beschädigter Existing-Datei: ein stilles `existing = {}`
    # hat 2026-06-26 dazu geführt, dass tennis_scan einen leeren Payload
    # geschrieben und an die Cloud gepusht hat (PWA zeigte 0 Spiele). Lieber
    # crashen als Daten still überschreiben.
    existing: dict = {}
    if json_path.exists():
        raw = json_path.read_text()
        if "<<<<<<< " in raw or "\n=======\n" in raw or ">>>>>>> " in raw:
            raise RuntimeError(
                f"{json_path} enthält Git-Konflikt-Marker — Schreiben abgebrochen, "
                "um stilles Daten-Wipe zu verhindern. Konflikt manuell auflösen."
            )
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"{json_path} ist kein gültiges JSON ({e}). Schreiben abgebrochen, "
                "um stilles Daten-Wipe zu verhindern."
            ) from e

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    football_data = [
        _signal_to_dict(s, "football", kickoff=kickoff_map.get(s.match_id, ""), generated_at=updated)
        for s in football
    ] if football else existing.get("football", [])

    if tennis:
        _player_form = _build_player_form_cache()
        tennis_data = [
            _signal_to_dict(
                s, "tennis",
                tour=tennis_tour_map.get(s.match_id, ""),
                kickoff=kickoff_map.get(s.match_id, ""),
                tournament_meta=tennis_tournament_map.get(s.match_id),
                generated_at=updated,
                form_a=_player_form.get(s.home) or [],
                form_b=_player_form.get(s.away) or [],
            )
            for s in tennis
        ]
    else:
        tennis_data = existing.get("tennis", [])

    # Wave 3C: enrich tennis signals with canonical event state from schedule.
    # Publishes event_status, scheduled_start_current, scheduled_start_initial.
    # Also updates kickoff to scheduled_start_current (safe for tennis — signal_id
    # is stable via fixture_registry regardless of kickoff changes).
    if tennis_data:
        _sched_lookup: dict[tuple[str, str], dict] = {}
        for _g in (schedule or []):
            if _g.get("sport") == "tennis" and _g.get("scheduled_start_current"):
                _sched_lookup[(_g.get("home", ""), _g.get("away", ""))] = _g
        if _sched_lookup:
            for _tsig in tennis_data:
                _parts = _tsig.get("match", "").split(" vs ", 1)
                if len(_parts) != 2:
                    continue
                _ent = _sched_lookup.get((_parts[0].strip(), _parts[1].strip()))
                if not _ent:
                    continue
                _cur = _ent.get("scheduled_start_current")
                if _cur:
                    _tsig["scheduled_start_current"] = _cur
                    _tsig["kickoff"] = _cur  # keep kickoff in sync for legacy consumers
                _ini = _ent.get("scheduled_start_initial")
                if _ini:
                    _tsig["scheduled_start_initial"] = _ini
                _evs = _ent.get("event_status")
                if _evs:
                    _tsig["event_status"] = _evs
                _upd = _ent.get("schedule_updated_at")
                if _upd:
                    _tsig["schedule_updated_at"] = _upd

    # Remove signals for matches that ended > 100 minutes ago
    football_data = _drop_finished_signals(football_data)
    tennis_data   = _drop_finished_signals(tennis_data)

    # N1: Tag top-N signals per sport with display_priority for PWA collapsing
    from src.config import MAX_SIGNALS_DISPLAY as _MAX_DISPLAY
    football_data = _tag_display_priority(football_data, _MAX_DISPLAY)
    tennis_data   = _tag_display_priority(tennis_data, _MAX_DISPLAY)

    # O1-7: Seed initial odds for new signals, then merge refreshed odds state.
    # Backfill signal_id for pre-O1-7 signals that went through the else-branch.
    for _sig in football_data + tennis_data:
        _sid = _sig.get("signal_id")
        if not _sid:
            _sid = make_signal_id(
                _sig.get("sport", "football"),
                _sig.get("match", ""),
                _sig.get("market", ""),
                _sig.get("kickoff", ""),
            )
            _sig["signal_id"] = _sid
        seed_initial_odds(_sid, _sig)
    _odds_state = load_odds_state()
    football_data = [merge_odds_state_into_signal(sig, _odds_state) for sig in football_data]
    tennis_data   = [merge_odds_state_into_signal(sig, _odds_state) for sig in tennis_data]

    if schedule is not None:
        # F7-Fix: Sport-getrennter Merge. Wenn Caller Schedule mit einem Sport-Fokus
        # übergibt (typisch tennis_scan → nur tennis; daily_scan → nur football),
        # bewahren wir Einträge der anderen Sportarten aus dem bestehenden Schedule.
        # Verhindert Race-Condition: tennis_scan überschrieb sonst kompletten Football-
        # Schedule mit [] (bis zum nächsten Football-Scan Anzeige-Blackout).
        _incoming_sports = {(g.get("sport") or "football") for g in schedule}
        _existing = existing.get("schedule", []) or []
        _preserved = [g for g in _existing if (g.get("sport") or "football") not in _incoming_sports]
        schedule_data = schedule + _preserved
    else:
        schedule_data = existing.get("schedule", [])

    if all_odds is not None:
        # Merge: incoming keys override, existing keys not in incoming are preserved
        all_odds_data = {**existing.get("all_odds", {}), **all_odds}
    else:
        all_odds_data = existing.get("all_odds", {})

    if model_tips is not None:
        model_tips_data = model_tips
    else:
        model_tips_data = existing.get("model_tips", {})

    if model_evals is not None:
        # Neue Einträge überschreiben alte (Runden-Fortschritt: alte Paarungen raus).
        # Bestehende Einträge bleiben nur wenn NICHT in neuen → verhindert stale Vorrundenmatches.
        model_evals_data = {**existing.get("model_evals", {}), **model_evals}
        # Stale-Pruning: Einträge entfernen deren Spieler-Paar komplett durch neue ersetzt wurde.
        # Erkennung: Ein Spieler taucht in einem neuen Eintrag auf, aber mit anderem Gegner.
        new_players: set[str] = set()
        for key in model_evals:
            parts = key.split(" vs ", 1)
            if len(parts) == 2:
                new_players.add(parts[0])
                new_players.add(parts[1])
        pruned: dict = {}
        for key, val in model_evals_data.items():
            parts = key.split(" vs ", 1)
            if len(parts) == 2 and key not in model_evals:
                # Alter Eintrag: behalten falls KEINER der Spieler in einem neuen Eintrag vorkommt.
                if parts[0] in new_players or parts[1] in new_players:
                    continue  # Spieler hat neue Paarung → alten Eintrag verwerfen
            pruned[key] = val
        model_evals_data = pruned
    else:
        model_evals_data = existing.get("model_evals", {})

    # N3: Odds-freshness comparison vs. previous snapshot
    _oh_list = odds_history if isinstance(odds_history, list) else existing.get("odds_history", [])
    if _oh_list:
        football_data = _enrich_odds_freshness(football_data, _oh_list)
        tennis_data   = _enrich_odds_freshness(tennis_data, _oh_list)

    # N4: Enrich football signals with best bookie from all_odds bookmakers_h2h
    if all_odds_data:
        football_data = _enrich_best_bookie(football_data, all_odds_data)

    # Compute bankroll state from ledger — always read from ledger when not explicitly passed
    # (avoids stale phantom bets persisting in KV from old JSON)
    _resolved_open_bets = open_bets if open_bets is not None else _get_open_bets_from_ledger(
        all_odds=all_odds_data if all_odds_data else None,
        ledger_path=ledger_path,
    )

    # Enrich football open bets with is_live flag ([-5, 115] min window, same as live_score_push.py).
    # Tennis schedule entries are intentionally excluded: elapsed time is never authoritative
    # evidence of LIVE for Tennis. Tennis LIVE status comes only from the authoritative live
    # cache below (Wave 3A invariant).
    _now_utc = datetime.now(timezone.utc)
    def _name_key(s: str) -> str:
        return (s or "").lower().strip().replace(" & ", " and ")
    _ko_lookup = {
        (_name_key(g.get("home", "")), _name_key(g.get("away", ""))): g.get("kickoff", "")
        for g in (schedule_data or [])
        if g.get("sport", "football") != "tennis"
    }
    for _bet in (_resolved_open_bets or []):
        _ko = _ko_lookup.get((_name_key(_bet["home"]), _name_key(_bet["away"])), "")
        _bet["is_live"] = False
        if _ko:
            try:
                _ko_dt = datetime.fromisoformat(_ko.replace("Z", "+00:00"))
                _elapsed = (_now_utc - _ko_dt).total_seconds() / 60
                _bet["is_live"] = -5 <= _elapsed <= 115
            except ValueError:
                pass

    # Enrich open bets with tennis live status (in_progress → is_live, suspended → is_suspended).
    # Priority: tennis_live_scores.json (every 15 min) → tennis_suspended.json (every 2h fallback)
    #
    # Enrich with Tennis authoritative live status. Wave 3A: per-record freshness via
    # _tennis_bet_is_live() — stale in_progress cannot mark a bet as LIVE.
    try:
        import json as _json
        from src.data.tennis_scores import canonical_match_key as _cmk
        _tennis_live: dict = {}

        _live_cache = ROOT / "data" / "cache" / "tennis_live_scores.json"
        if _live_cache.exists():
            _lcd = _json.loads(_live_cache.read_text())
            _lcd_beat = _lcd.get("_meta", {}).get("heartbeat_at", "2000-01-01T00:00:00Z")
            _lcd_age = (_now_utc - datetime.fromisoformat(_lcd_beat.replace("Z", "+00:00"))).total_seconds()
            if _lcd_age < 3600:  # max 1h alt
                _tennis_live = {k: v for k, v in _lcd.items() if not k.startswith("_")}

        if not _tennis_live:  # Fallback: suspended-only cache vom Settle-Cron
            _susp_cache = ROOT / "data" / "cache" / "tennis_suspended.json"
            if _susp_cache.exists():
                _scd = _json.loads(_susp_cache.read_text())
                _scd_age = (_now_utc - datetime.fromisoformat(_scd.get("updated", "2000-01-01T00:00:00Z").replace("Z", "+00:00"))).total_seconds()
                if _scd_age < 7200:
                    _tennis_live = {m["match_key"]: {**m, "status": m.get("status", "suspended")}
                                    for m in _scd.get("matches", [])}

        if _tennis_live:
            for _bet in (_resolved_open_bets or []):
                _ck = _cmk(_bet.get("home", ""), _bet.get("away", ""))
                _ts = _tennis_live.get(_ck)
                if not _tennis_bet_is_live(_ts, _now_utc):
                    continue
                _ts_status = (_ts or {}).get("status", "")
                _bet["is_live"] = True  # in_progress und suspended beide im Live-Tab
                _bet["tennis_sets"] = (_ts or {}).get("sets", [])
                _bet["tennis_sets_won"] = (_ts or {}).get("sets_won", [])
                if _ts_status in ("suspended", "postponed"):
                    _bet["is_suspended"] = True
                    _bet["resume_time"] = (_ts or {}).get("resume_time")
                    _bet["suspend_status"] = _ts_status
                    _bet["suspend_sets"] = (_ts or {}).get("sets", [])
    except Exception:
        pass

    _staked = sum(float(b.get("stake", 0)) for b in (_resolved_open_bets or []))
    _max_win = sum(
        float(b.get("stake", 0)) * (float(b.get("current_odds") or b.get("entry_odds", 0)) - 1)
        for b in (_resolved_open_bets or [])
        if b.get("current_odds") or b.get("entry_odds")
    )
    _bankroll_start = 100.0
    _pnl_closed = sum(float(row.get("pnl", 0)) for row in _get_closed_bets(ledger_path=ledger_path))
    _free = round(_bankroll_start + _pnl_closed - _staked, 2)
    _exposure_pct = round(_staked / _bankroll_start * 100, 1)

    # Surface operational flags so the dashboard can show stale-data banners.
    # USED_STALE_CACHE is a module flag set by fetch_upcoming_matches() when
    # the live API failed and we fell back to the on-disk pickle.
    try:
        from src.data.odds_api import USED_STALE_CACHE as _stale_odds_flag
    except Exception:
        _stale_odds_flag = False

    payload = {
        "updated":        updated,
        "build_info":     _build_info(),
        "meta": {
            "stale_odds": bool(_stale_odds_flag),
            "default_user": _DEFAULT_USER,
            "user": user,
        },
        "schedule":       schedule_data,
        "all_odds":       all_odds_data,
        "model_tips":     model_tips_data,
        "model_evals":    model_evals_data,
        "football":       football_data,
        "tennis":         tennis_data,
        "portfolio":      portfolio if portfolio else existing.get("portfolio", {}),
        "top_elo":        [{"name": n, "rating": round(r)} for n, r in top_elo] if top_elo else existing.get("top_elo", []),
        "history":        _build_history(ledger_path=ledger_path),
        "open_bets":      _resolved_open_bets,
        "settled_bets":   _get_settled_bets_for_dashboard(ledger_path=ledger_path),
        "bankroll_state": {
            "start":        _bankroll_start,
            "free":         round(_free, 2),
            "staked":       round(_staked, 2),
            "exposure_pct": _exposure_pct,
            "max_win":      round(_max_win, 2),
            "pnl_closed":   round(_pnl_closed, 2),
            # P0-A: backend-authored timestamp — Worker uses this for freshness enforcement.
            # Threshold: 2 hours (AUTH_STATE_MAX_AGE_MS in contract.js).
            "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "wm_stats": _build_wm_stats(ledger_path=ledger_path),
        "tennis_stats": _build_tennis_stats(ledger_path=ledger_path),
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

    # P0C-001: static Pages artifacts must contain only public product data.
    # The full private payload is still uploaded to Cloudflare KV (below) so
    # the Worker POST /pending-bet validation retains bankroll_state and open_bets.
    from src.notifications.public_serializer import serialize_public_product as _spp
    public_payload = _spp(payload)
    atomic_write_json(json_path, public_payload, indent=2)
    # Backward-compat: default user also writes the legacy `signals.json`.
    # Use ROOT-relative path (not module-level _JSON_PATH) so tests that
    # monkey-patch ROOT don't accidentally stomp on the real repo file.
    if user == _DEFAULT_USER:
        legacy_path = ROOT / "docs" / "data" / "signals.json"
        atomic_write_json(legacy_path, public_payload, indent=2)
    return upload_signals_to_cloud(path=None, payload=payload, user=user)


def upload_signals_to_cloud(
    path: Path | None = None,
    payload: dict | None = None,
    user: str = _DEFAULT_USER,
) -> bool:
    """Upload signals snapshot to Cloudflare Worker KV.

    P0C-001: accepts an explicit `payload` dict (the FULL private snapshot) so
    callers no longer need to read back from the sanitized on-disk file.
    The full private payload preserves bankroll_state / open_bets for the
    Worker POST /pending-bet validation. Only the static Pages files are
    sanitized (via public_serializer.serialize_public_product).

    Returns True on success OR when cloud env vars are unset (no-op is not
    treated as failure). Returns False only when a real POST attempt failed —
    that's the signal for callers to fail loud (see incident 2026-07-06).
    All failure paths log the reason (previously all silent).
    """
    import json as _json

    try:
        import requests as _req
    except ImportError:
        print("[cloud-upload] requests not installed — skipping", flush=True)
        return True

    url = os.getenv("SIGNALS_CLOUD_URL")
    token = os.getenv("SIGNALS_API_TOKEN")
    if not url or not token:
        print("[cloud-upload] SIGNALS_CLOUD_URL/SIGNALS_API_TOKEN unset — skipping", flush=True)
        return True

    # Worker POST endpoint is /signals, GET is /signals.json — strip suffix for write
    post_url = url[: -len("/signals.json")] + "/signals" if url.endswith("/signals.json") else url
    # D4: master-token can target a specific user slot via ?user=
    if user and user != _DEFAULT_USER:
        sep = "&" if "?" in post_url else "?"
        post_url = f"{post_url}{sep}user={user}"

    if payload is not None:
        data = _json.dumps(payload, ensure_ascii=False).encode()
    else:
        target = path or _JSON_PATH
        if not target.exists():
            print(f"[cloud-upload] target {target} missing — skipping", flush=True)
            return False
        data = target.read_bytes()

    try:
        r = _req.post(
            post_url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=15,
        )
        if r.status_code == 200:
            print(f"[cloud-upload] ok user={user} ({len(data)} bytes)", flush=True)
            return True
        print(
            f"[cloud-upload] FAILED user={user} status={r.status_code} "
            f"body={r.text[:200]!r}",
            flush=True,
        )
        return False
    except Exception as e:
        print(f"[cloud-upload] EXCEPTION user={user}: {type(e).__name__}: {e}", flush=True)
        return False
