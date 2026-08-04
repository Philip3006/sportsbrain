"""J8-M8: CLV-Alarm für Tennis-Bets.

Prüft rollierendes 14d-Mittel der CLV auf allen bekannten User-Ledgers.
Wenn Tennis-Bets in dieser Periode:
  - mean_clv_14d < 0  → WARN + optional Push
  - mean_clv_14d < -0.02 mit n ≥ 10  → CRITICAL

Non-Tennis-Bets bleiben aus der Aggregation (Football hat eigene Metriken).

Usage:
    python3 scripts/monitor_clv.py
    python3 scripts/monitor_clv.py --user philip --push
    python3 scripts/monitor_clv.py --window-days 30
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.tennis_settlement import is_tennis_market
from src.config import DEFAULT_USER, ledger_path_for


_WARN_THRESH = 0.0
_CRIT_THRESH = -0.02
_CRIT_MIN_N = 10


def _parse_placed_date(row: dict) -> datetime | None:
    v = row.get("placed_date") or row.get("timestamp") or ""
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _tennis_clv_window(ledger_path: Path, window_days: int) -> tuple[list[float], int]:
    """Returns (clv_values, n_settled) für Tennis-Bets in den letzten window_days."""
    if not ledger_path.exists():
        return [], 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    clvs: list[float] = []
    n_settled = 0
    with ledger_path.open() as f:
        for row in csv.DictReader(f):
            market = row.get("market", "")
            source = (row.get("source") or "").lower()
            if not (is_tennis_market(market) or "tennis" in source):
                continue
            if row.get("status", "").lower() not in ("won", "lost"):
                continue
            pd_ = _parse_placed_date(row)
            if pd_ is None or pd_ < cutoff:
                continue
            n_settled += 1
            try:
                clv = float(row.get("clv") or "")
                clvs.append(clv)
            except (ValueError, TypeError):
                continue
    return clvs, n_settled


def _tennis_clv_by_market(ledger_path: Path, window_days: int, min_consecutive: int = 3) -> list[dict]:
    """Per-Markt CLV: gibt Märkte zurück wo letzte min_consecutive Bets alle CLV < -0.05 hatten."""
    if not ledger_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    # market → list of (placed_date, clv) chronologisch
    by_market: dict[str, list[tuple[datetime, float]]] = {}
    with ledger_path.open() as f:
        for row in csv.DictReader(f):
            market = row.get("market", "")
            source = (row.get("source") or "").lower()
            if not (is_tennis_market(market) or "tennis" in source):
                continue
            if row.get("status", "").lower() not in ("won", "lost"):
                continue
            pd_ = _parse_placed_date(row)
            if pd_ is None or pd_ < cutoff:
                continue
            try:
                clv = float(row.get("clv") or "")
            except (ValueError, TypeError):
                continue
            by_market.setdefault(market, []).append((pd_, clv))

    alerts = []
    for market, entries in by_market.items():
        entries.sort(key=lambda x: x[0])
        last_n = [c for _, c in entries[-min_consecutive:]]
        if len(last_n) >= min_consecutive and all(c < -0.05 for c in last_n):
            alerts.append({
                "market": market,
                "n_consecutive": len(last_n),
                "mean_clv": round(sum(last_n) / len(last_n), 4),
                "last_clv": last_n[-1],
            })
    return alerts


def _classify(mean_clv: float, n: int) -> tuple[str, str]:
    if n == 0:
        return "OK", "keine Tennis-Bets in der Periode"
    if n >= _CRIT_MIN_N and mean_clv < _CRIT_THRESH:
        return "CRITICAL", f"mean_clv_14d={mean_clv:+.4f} < {_CRIT_THRESH:+.2f} bei n={n}"
    if mean_clv < _WARN_THRESH:
        return "WARN", f"mean_clv_14d={mean_clv:+.4f} < 0 (n={n})"
    return "OK", f"mean_clv_14d={mean_clv:+.4f} bei n={n}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None, help="nur diesen User prüfen")
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--push", action="store_true", help="bei WARN/CRITICAL Web-Push senden")
    args = ap.parse_args()

    ledger_dir = ledger_path_for(DEFAULT_USER).parent
    if args.user:
        users = [args.user]
    else:
        users = sorted({
            p.stem.replace("ledger_", "")
            for p in ledger_dir.glob("ledger_*.csv")
            if "backfill" not in p.stem and "backup" not in p.stem
        }) or [DEFAULT_USER]

    exit_code = 0
    for u in users:
        lp = ledger_path_for(u)
        clvs, n = _tennis_clv_window(lp, args.window_days)
        mean = sum(clvs) / len(clvs) if clvs else 0.0
        level, msg = _classify(mean, n)
        print(f"[{u}] {level}: {msg}")
        if level in ("WARN", "CRITICAL"):
            exit_code = max(exit_code, 1 if level == "WARN" else 2)
            if args.push:
                try:
                    from src.notifications.web_push import send_generic_alert
                    send_generic_alert(
                        title=f"Tennis CLV {level}",
                        body=f"{u}: {msg}",
                        url="/#journal",
                    )
                except Exception as e:
                    print(f"  [push] failed: {e}")

        # Per-Markt-Trend: letzte 3 Bets auf selben Markt alle CLV < -5% → Alarm
        market_alerts = _tennis_clv_by_market(lp, args.window_days)
        for alert in market_alerts:
            mkt_msg = (f"Markt '{alert['market']}': letzte {alert['n_consecutive']} Bets "
                       f"mean_clv={alert['mean_clv']:+.3f} (< −5%)")
            print(f"[{u}] WARN market-trend: {mkt_msg}")
            exit_code = max(exit_code, 1)
            if args.push:
                try:
                    from src.notifications.web_push import send_generic_alert
                    send_generic_alert(
                        title="Tennis Markt-CLV ⚠",
                        body=f"{u}: {mkt_msg}",
                        url="/#journal",
                    )
                except Exception as e:
                    print(f"  [push] market-alert failed: {e}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
