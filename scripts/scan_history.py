#!/usr/bin/env python3
"""
Aggregates daily scan reports from results/scans/ into a weekly/tournament summary.

Usage:
  python scripts/scan_history.py                    # all scans
  python scripts/scan_history.py --since 2026-06-11 # since WM start
  python scripts/scan_history.py --days 7           # last N days
"""
import argparse
import re
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta


# Regex for a signal table row: must have | Match | Market | Model% | Odds | EV | ... | Confidence |
# Tolerates optional trailing columns (Agree, etc.) and emoji in stake cells.
_ROW_RE = re.compile(
    r"^\|\s*"
    r"(?P<match>[^|]+?)\s*\|\s*"          # Match
    r"(?P<market>[^|]+?)\s*\|\s*"         # Market
    r"(?P<model_pct>[^|]+?)\s*\|\s*"      # Model%  (may contain secondary Elo value)
    r"(?P<odds>[0-9]+(?:\.[0-9]+)?)\s*\|\s*"  # Odds
    r"(?P<ev>[+-][0-9]+(?:\.[0-9]+)?%)\s*(?:⚠️)?\s*\|\s*"  # EV  (optional warning emoji)
    r"[^|]*?\|\s*"                         # Kelly  (skip)
    r"[^|]*?\|\s*"                         # Stake  (skip)
    r"(?P<confidence>HIGH|MEDIUM|LOW)\s*\|"  # Confidence
)

# Header / separator rows to skip
_SKIP_RE = re.compile(r"^\|[-| ]+\|")

# Date extracted from filename: scan_YYYY-MM-DD.md
_DATE_RE = re.compile(r"scan_(\d{4}-\d{2}-\d{2})\.md$")

# Classify market string into a canonical bucket
def _market_bucket(market: str) -> str:
    m = market.strip()
    if re.match(r"O/U", m, re.IGNORECASE):
        return "O/U"
    if re.match(r"AH", m, re.IGNORECASE):
        return "AH"
    if m.upper() in ("HOME", "DRAW", "AWAY"):
        return m.upper()
    return m.upper()


def _extract_teams(match: str) -> tuple[str, str]:
    """Split 'Team A vs Team B' into (home, away)."""
    parts = re.split(r"\s+vs\s+", match.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return match.strip(), ""


def parse_signal_rows(filepath: Path) -> list[dict]:
    """Parse signal table rows from a scan markdown report.

    Returns a list of dicts with keys:
      match, home_team, away_team, market, market_bucket,
      model_pct_str, odds, ev, confidence
    """
    signals: list[dict] = []
    text = filepath.read_text(encoding="utf-8")

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if _SKIP_RE.match(line):
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue

        match_str = m.group("match").strip()
        market_str = m.group("market").strip()
        ev_str = m.group("ev").strip()
        confidence = m.group("confidence").strip()
        odds_str = m.group("odds").strip()

        # Skip the header row itself (match cell would be "Match")
        if match_str.lower() == "match":
            continue

        # Parse EV: "+28.6%" -> 0.286
        try:
            ev_val = float(ev_str.replace("%", "")) / 100.0
        except ValueError:
            continue

        try:
            odds_val = float(odds_str)
        except ValueError:
            continue

        home, away = _extract_teams(match_str)

        signals.append({
            "match": match_str,
            "home_team": home,
            "away_team": away,
            "market": market_str,
            "market_bucket": _market_bucket(market_str),
            "ev": ev_val,
            "odds": odds_val,
            "confidence": confidence,
        })

    return signals


def find_scan_files(scans_dir: Path, since: datetime | None, days: int | None) -> list[tuple[datetime, Path]]:
    """Return (date, path) pairs sorted chronologically, filtered by date constraints."""
    result: list[tuple[datetime, Path]] = []

    for f in scans_dir.glob("scan_*.md"):
        dm = _DATE_RE.search(f.name)
        if not dm:
            continue
        try:
            d = datetime.strptime(dm.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        result.append((d, f))

    result.sort(key=lambda x: x[0])

    if since is not None:
        result = [(d, f) for d, f in result if d >= since]

    if days is not None:
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        result = [(d, f) for d, f in result if d >= cutoff]

    return result


def print_summary(scan_files: list[tuple[datetime, Path]]) -> None:
    """Compute and print aggregated statistics."""
    if not scan_files:
        print("Keine Scan-Dateien gefunden.")
        return

    all_signals: list[dict] = []
    signals_per_day: dict[str, int] = {}
    teams_per_day: dict[str, set[str]] = defaultdict(set)

    for date, filepath in scan_files:
        date_str = date.strftime("%Y-%m-%d")
        rows = parse_signal_rows(filepath)
        all_signals.extend(rows)
        signals_per_day[date_str] = len(rows)
        for s in rows:
            for team in (s["home_team"], s["away_team"]):
                if team:
                    teams_per_day[team].add(date_str)

    total = len(all_signals)
    first_date = scan_files[0][0].strftime("%Y-%m-%d")
    last_date = scan_files[-1][0].strftime("%Y-%m-%d")
    n_days = len(scan_files)

    # Confidence distribution
    conf_counter: Counter = Counter(s["confidence"] for s in all_signals)
    conf_order = ["HIGH", "MEDIUM", "LOW"]

    # Team frequencies: count appearances across all signals
    team_signal_count: Counter = Counter()
    team_markets: dict[str, list[str]] = defaultdict(list)
    for s in all_signals:
        for team, role in ((s["home_team"], s["market_bucket"]), (s["away_team"], s["market_bucket"])):
            if team:
                team_signal_count[team] += 1
                team_markets[team].append(role)

    # Market frequencies
    market_counter: Counter = Counter(s["market_bucket"] for s in all_signals)

    # EV & Odds averages
    avg_ev = sum(s["ev"] for s in all_signals) / total if total else 0.0
    avg_odds = sum(s["odds"] for s in all_signals) / total if total else 0.0

    # Teams appearing on 2+ days
    recurring = {team: len(days_set) for team, days_set in teams_per_day.items() if len(days_set) >= 2}
    recurring_sorted = sorted(recurring.items(), key=lambda x: -x[1])

    # --- Output ---
    print("=== SportsBrain Scan History ===")
    print(f"Zeitraum: {first_date} bis {last_date} ({n_days} {'Tag' if n_days == 1 else 'Tage'})")
    print(f"Gesamt Signale: {total}")

    print("\nConfidence-Verteilung:")
    for conf in conf_order:
        count = conf_counter.get(conf, 0)
        pct = (count / total * 100) if total else 0.0
        print(f"  {conf:<7} {count} ({pct:.0f}%)")

    print("\nTop Teams (häufigste Signale):")
    top_teams = team_signal_count.most_common(10)
    for rank, (team, count) in enumerate(top_teams, 1):
        # Summarise market roles for this team
        roles = Counter(team_markets[team])
        roles_str = ", ".join(f"{r} ×{c}" for r, c in roles.most_common())
        print(f"  {rank:2}. {team:<20} — {count}x ({roles_str})")

    print("\nTop Märkte:")
    market_order = market_counter.most_common()
    for market, count in market_order:
        pct = (count / total * 100) if total else 0.0
        print(f"  {market:<12} {count} ({pct:.1f}%)")

    print(f"\nDurchschnittlicher EV: {avg_ev:+.1%}")
    print(f"Durchschnittliche Odds: {avg_odds:.2f}")

    print("\nSignale pro Tag:")
    for date_str, count in signals_per_day.items():
        label = "Signal" if count == 1 else "Signale"
        print(f"  {date_str}: {count} {label}")

    if recurring_sorted:
        print("\nWiederkehrende Teams (erscheinen an 2+ Tagen):")
        for team, n in sorted(recurring_sorted, key=lambda x: -x[1]):
            print(f"  {team}: {n} {'Tag' if n == 1 else 'Tage'}")
    else:
        print("\nKeine wiederkehrenden Teams (kein Team erscheint an 2+ Tagen).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregiert tägliche Scan-Reports zu einer Turnier-/Wochen-Zusammenfassung."
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Nur Scans ab diesem Datum einbeziehen (inklusiv).",
    )
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="Nur die letzten N Tage einbeziehen.",
    )
    args = parser.parse_args()

    since_dt: datetime | None = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error(f"Ungültiges Datumsformat für --since: '{args.since}'. Erwartet: YYYY-MM-DD")

    scans_dir = Path(__file__).resolve().parent.parent / "results" / "scans"
    if not scans_dir.is_dir():
        print(f"Fehler: Scan-Verzeichnis nicht gefunden: {scans_dir}")
        raise SystemExit(1)

    scan_files = find_scan_files(scans_dir, since=since_dt, days=args.days)

    if not scan_files:
        filters = []
        if since_dt:
            filters.append(f"--since {args.since}")
        if args.days:
            filters.append(f"--days {args.days}")
        filter_msg = f" (Filter: {', '.join(filters)})" if filters else ""
        print(f"Keine Scan-Dateien gefunden{filter_msg} in: {scans_dir}")
        raise SystemExit(0)

    print_summary(scan_files)


if __name__ == "__main__":
    main()
