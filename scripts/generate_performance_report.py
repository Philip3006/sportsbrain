"""
Generate canonical performance report for SportsBrain.

Reads from two immutable sources:
  A) results/ledger_philip.csv     — real-money bets (football + tennis)
  B) data/cache/signal_history.jsonl — all archived signals with outcomes

Source A is authoritative for betting performance (real stakes, real P&L).
Source B provides a larger prediction quality sample.

Produces: results/performance_report.json

Usage:
  python3 scripts/generate_performance_report.py
  python3 scripts/generate_performance_report.py --output /path/to/out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.metrics import compute_segment, market_group

LEDGER_CSV = ROOT / "results" / "ledger_philip.csv"
SIGNAL_HISTORY = ROOT / "data" / "cache" / "signal_history.jsonl"
DEFAULT_OUTPUT = ROOT / "results" / "performance_report.json"

# Fixed stake used for signal_history records without a real ledger entry.
# update_betting_journal.py uses 10.0 for the same purpose.
_UNIT_STAKE = 10.0

# Leagues known to be tennis tours
_TENNIS_LEAGUES = frozenset({"atp", "wta"})


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_ledger(path: Path = LEDGER_CSV) -> list[dict]:
    """Load ledger CSV and normalize to canonical record format.

    Skips void/cancelled records for performance metrics but preserves them
    (with status) so callers can filter themselves.
    """
    if not path.exists():
        return []
    df = pd.read_csv(path)
    # Coerce numeric columns that may be stored as strings
    for col in ("decimal_odds", "stake_amount", "pnl", "closing_odds", "clv", "model_prob"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    records = []
    for _, row in df.iterrows():
        league = str(row.get("league", "wm2026") or "wm2026")
        sport = "tennis" if league in _TENNIS_LEAGUES else "football"
        # closing_odds=0 means not available (legacy encoding)
        closing = row.get("closing_odds")
        if pd.isna(closing) or closing == 0:
            closing = None
        clv = row.get("clv")
        if pd.isna(clv) or clv == 0:
            clv = None
        model_prob = row.get("model_prob")
        if pd.isna(model_prob):
            model_prob = None
        records.append({
            "match_id": str(row.get("match_id", "")),
            "match_date": str(row.get("match_date", "")),
            "sport": sport,
            "league": league,
            "home": str(row.get("home", "")),
            "away": str(row.get("away", "")),
            "market": str(row.get("market", "")),
            "model_prob": model_prob,
            "decimal_odds": float(row.get("decimal_odds") or 0.0),
            "stake_amount": float(row.get("stake_amount") or 0.0),
            "status": str(row.get("status", "")),
            "pnl": float(row.get("pnl") or 0.0),
            "closing_odds": float(closing) if closing is not None else None,
            "clv": float(clv) if clv is not None else None,
            "source": "ledger",
            "surface": None,
            "tour": None,
        })
    return records


def _load_signal_history(path: Path = SIGNAL_HISTORY) -> list[dict]:
    """Load signal_history.jsonl. Returns only placed signals with outcomes.

    These records lack real stake/P&L. stake_amount uses the unit stake
    (€10) and pnl is computed from outcome+odds. They are excluded from
    CLV metrics (closing_odds not tracked in signal_history).
    """
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("placed"):
            continue
        outcome = r.get("outcome")
        if not outcome:
            continue
        # Normalize outcome to ledger status vocabulary
        status_map = {"won": "won", "lost": "lost", "push": "push"}
        status = status_map.get(outcome)
        if status is None:
            continue
        odds = float(r.get("decimal_odds") or 0.0)
        stake = float(r.get("stake_eur") or _UNIT_STAKE)
        if status == "won":
            pnl = round(stake * (odds - 1), 2)
        elif status == "lost":
            pnl = -stake
        else:
            pnl = 0.0
        tour = r.get("tour")  # atp | wta
        sport = r.get("sport", "tennis")
        league = tour if tour in _TENNIS_LEAGUES else (
            "wm2026" if sport == "football" else sport
        )
        model_prob = r.get("model_prob")
        if model_prob is not None and not (0.0 < model_prob < 1.0):
            model_prob = None
        records.append({
            "match_id": str(r.get("match_id", "")),
            "match_date": str(r.get("scan_date", "")),
            "sport": sport,
            "league": league,
            "home": str(r.get("home", "")),
            "away": str(r.get("away", "")),
            "market": str(r.get("market", "")),
            "model_prob": model_prob,
            "decimal_odds": odds,
            "stake_amount": stake,
            "status": status,
            "pnl": pnl,
            "closing_odds": None,
            "clv": None,
            "source": "signal_history",
            "surface": r.get("surface"),
            "tour": tour,
        })
    return records


def _dedup_by_ledger(
    ledger: list[dict], signals: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Return (ledger_records, signal_only_records).

    Signal records whose (match_id, market) appear in the ledger are
    excluded from signal_only to avoid double-counting.
    """
    ledger_keys = {(r["match_id"], r["market"]) for r in ledger}
    signal_only = [
        r for r in signals
        if (r["match_id"], r["market"]) not in ledger_keys
    ]
    return ledger, signal_only


# ---------------------------------------------------------------------------
# Odds provenance audit
# ---------------------------------------------------------------------------

def _audit_odds_provenance(ledger: list[dict]) -> dict:
    """Report on odds provenance fields available in the ledger."""
    n = len(ledger)
    settled = [r for r in ledger if r["status"] in ("won", "lost", "push")]
    has_close = sum(1 for r in settled if r.get("closing_odds") is not None)
    has_clv = sum(1 for r in settled if r.get("clv") is not None)
    has_model_prob = sum(1 for r in settled if r.get("model_prob") is not None)
    return {
        "n_total": n,
        "n_settled": len(settled),
        "closing_odds_coverage": round(has_close / len(settled), 4) if settled else None,
        "clv_coverage": round(has_clv / len(settled), 4) if settled else None,
        "model_prob_coverage": round(has_model_prob / len(settled), 4) if settled else None,
        "gaps": {
            "odds_fetch_tier": (
                "metric unavailable: FETCH_ODDS_SOURCE (added O1-2) is not yet"
                " persisted to ledger rows. Adding odds_fetch_tier column to"
                " append_bets() would close this gap."
            ),
            "odds_timestamp": (
                "metric unavailable: entry odds timestamp not stored."
                " TheOddsAPI response timestamp could be added to ledger."
            ),
            "odds_provider": (
                "metric unavailable: specific bookmaker used for entry odds"
                " not stored. 'source' column in ledger is 'value'/'manual',"
                " not bookie-level."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(
    ledger: list[dict],
    signals: list[dict],
) -> dict:
    """Assemble the canonical performance report.

    Betting performance (ROI/P&L/drawdown/CLV) uses ledger only.
    Prediction quality (Brier/LogLoss/calibration) uses ledger + signal_history
    combined (larger sample).
    """
    # De-duplicate: signal records already in ledger are excluded
    ledger_recs, signal_only = _dedup_by_ledger(ledger, signals)

    # Combined pool for prediction quality
    combined = ledger_recs + signal_only

    now = datetime.now(timezone.utc).isoformat()
    dates = [r["match_date"] for r in combined if r["match_date"]]
    data_through = max(dates) if dates else None

    report: dict = {
        "generated_at": now,
        "data_through": data_through,
        "sources": {
            "ledger": {
                "path": str(LEDGER_CSV),
                "n": len(ledger_recs),
                "note": "real-money bets; authoritative for P&L metrics",
            },
            "signal_history": {
                "path": str(SIGNAL_HISTORY),
                "n": len(signal_only),
                "unit_stake_eur": _UNIT_STAKE,
                "note": (
                    "placed signals with tracked outcomes (paper-trade stakes);"
                    " used for prediction quality only"
                ),
            },
        },
        "odds_provenance": _audit_odds_provenance(ledger_recs),
    }

    # -- Overall: prediction quality uses combined, betting uses ledger only --
    report["overall"] = {
        "prediction_quality": compute_segment(combined),
        "betting_performance": compute_segment(ledger_recs),
    }

    # -- By sport --
    sports: dict[str, list] = defaultdict(list)
    for r in combined:
        sports[r["sport"]].append(r)
    ledger_by_sport: dict[str, list] = defaultdict(list)
    for r in ledger_recs:
        ledger_by_sport[r["sport"]].append(r)

    report["sports"] = {}
    for sp, recs in sports.items():
        report["sports"][sp] = {
            "prediction_quality": compute_segment(recs),
            "betting_performance": compute_segment(ledger_by_sport.get(sp, [])),
        }

    # -- By league --
    leagues: dict[str, list] = defaultdict(list)
    for r in combined:
        leagues[r["league"]].append(r)
    ledger_by_league: dict[str, list] = defaultdict(list)
    for r in ledger_recs:
        ledger_by_league[r["league"]].append(r)

    report["leagues"] = {}
    for lg, recs in leagues.items():
        report["leagues"][lg] = {
            "prediction_quality": compute_segment(recs),
            "betting_performance": compute_segment(ledger_by_league.get(lg, [])),
        }

    # -- By market group --
    mkt_groups: dict[str, list] = defaultdict(list)
    for r in combined:
        mkt_groups[market_group(r["market"])].append(r)
    ledger_by_mkt: dict[str, list] = defaultdict(list)
    for r in ledger_recs:
        ledger_by_mkt[market_group(r["market"])].append(r)

    report["markets"] = {}
    for mg, recs in mkt_groups.items():
        if len(recs) < 3:
            continue  # skip tiny segments
        report["markets"][mg] = {
            "prediction_quality": compute_segment(recs),
            "betting_performance": compute_segment(ledger_by_mkt.get(mg, [])),
        }

    # -- Tennis surface (only where surface known) --
    surfaces: dict[str, list] = defaultdict(list)
    for r in combined:
        if r.get("surface"):
            surfaces[r["surface"]].append(r)
    if surfaces:
        report["tennis_surfaces"] = {
            surf: compute_segment(recs)
            for surf, recs in surfaces.items()
            if len(recs) >= 3
        }

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SportsBrain performance report")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--ledger", default=str(LEDGER_CSV),
        help="Override ledger CSV path",
    )
    parser.add_argument(
        "--signals", default=str(SIGNAL_HISTORY),
        help="Override signal_history.jsonl path",
    )
    args = parser.parse_args()

    ledger = _load_ledger(Path(args.ledger))
    signals = _load_signal_history(Path(args.signals))
    report = build_report(ledger, signals)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"  ✓ Performance report written: {out}")

    # Print summary to stdout
    ov = report["overall"]
    bp = ov["betting_performance"]
    pq = ov["prediction_quality"]
    print(f"  Ledger bets: {bp['n']} total, {bp['n_binary']} binary settled")
    roi_v = bp["roi_pct"]["value"]
    print(f"  ROI: {roi_v:.2f}%" if roi_v is not None else "  ROI: unavailable")
    bs = pq["brier_score"]
    print(f"  Brier Score: {bs['value']} (n={bs['n']})" if bs["value"] is not None else "  Brier: unavailable")
    ll = pq["log_loss"]
    print(f"  Log Loss:    {ll['value']} (n={ll['n']})" if ll["value"] is not None else "  Log Loss: unavailable")


if __name__ == "__main__":
    main()
