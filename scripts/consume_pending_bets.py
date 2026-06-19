"""
Consume pending bets from the Cloudflare Worker KV and append them to
results/ledger.csv as 'open' bets.

Flow:
    1. GET  {WORKER_BASE}/pending_bets       → list of bets placed via PWA
    2. For each bet: append row to ledger.csv (locked write, skip duplicates)
    3. DELETE {WORKER_BASE}/pending_bets/{id} after successful append

Env vars (re-using existing conventions from src/notifications/web_dashboard.py):
    SIGNALS_CLOUD_URL   — e.g. https://sportsbrain-signals.<sub>.workers.dev/signals.json
                          (the /signals.json suffix is stripped)
    SIGNALS_API_TOKEN   — Bearer token, must match Worker's API_TOKEN secret

Run manually:
    python -m scripts.consume_pending_bets

Cron via launchd: add a plist entry, every 5 minutes.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pandas as pd
import requests

# Repo-root on sys.path so `src` imports work even when run as a script.
_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.betting.ledger import LEDGER_PATH, _FIELDS, _load, _save, _file_lock


def _worker_base() -> str:
    url = os.getenv("SIGNALS_CLOUD_URL", "").strip()
    if not url:
        raise SystemExit("SIGNALS_CLOUD_URL not set")
    if url.endswith("/signals.json"):
        url = url[: -len("/signals.json")]
    return url.rstrip("/")


def _token() -> str:
    tok = os.getenv("SIGNALS_API_TOKEN", "").strip()
    if not tok:
        raise SystemExit("SIGNALS_API_TOKEN not set")
    return tok


def _match_id(home: str, away: str, kickoff: str) -> str:
    """Deterministic id so re-running the consumer hits the duplicate guard."""
    key = f"pwa|{home.strip().lower()}|{away.strip().lower()}|{(kickoff or '')[:10]}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _row_from_bet(bet: dict, today: str) -> dict | None:
    match = (bet.get("match") or "").strip()
    if " vs " not in match:
        return None
    home, away = [s.strip() for s in match.split(" vs ", 1)]
    market = (bet.get("market") or "").strip()
    odds = float(bet.get("odds") or 0)
    stake = float(bet.get("stake_eur") or 0)
    if odds < 1.01 or stake <= 0:
        return None
    kickoff = bet.get("kickoff") or ""
    match_date = kickoff[:10] if kickoff else ""
    source = (bet.get("source") or "value").strip().lower()
    if source not in ("value", "manual"):
        source = "value"
    model_prob_raw = bet.get("model_prob")
    if model_prob_raw is None or model_prob_raw == "":
        model_prob_str = ""
    else:
        try:
            model_prob_str = f"{float(model_prob_raw):.6f}"
        except (TypeError, ValueError):
            model_prob_str = ""
    return {
        "match_id":     _match_id(home, away, kickoff),
        "match_date":   match_date,
        "home":         home,
        "away":         away,
        "market":       market,
        "decimal_odds": f"{odds:.4f}",
        "stake_pct":    "0.0",  # PWA bets bypass Kelly; ledger keeps it for compat
        "stake_amount": f"{stake:.2f}",
        "placed_date":  today,
        "status":       "open",
        "pnl":          "0.0",
        "closing_odds": "0.0",
        "clv":          "",
        "pinnacle_ref_odds": "",
        "source":       source,
        "model_prob":   model_prob_str,
    }


def _append_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _file_lock(LEDGER_PATH):
        df = _load(LEDGER_PATH)
        existing = set(zip(
            df.get("match_id", pd.Series([], dtype=str)),
            df.get("market", pd.Series([], dtype=str)),
        ))
        new_rows = [r for r in rows if (r["match_id"], r["market"]) not in existing]
        if new_rows:
            new_df = pd.DataFrame(new_rows, columns=_FIELDS)
            df = pd.concat([df, new_df], ignore_index=True)
            _save(df, LEDGER_PATH)
    return len(new_rows)


def main() -> int:
    base = _worker_base()
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}

    import time as _time
    r = None
    for _attempt in range(3):
        try:
            r = requests.get(f"{base}/pending_bets", headers=headers, timeout=15)
            break
        except requests.RequestException as e:
            if _attempt == 2:
                print(f"[consume] fetch failed: {e}", file=sys.stderr)
                return 1
            _time.sleep(5)
    if r.status_code != 200:
        print(f"[consume] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 1

    bets = (r.json() or {}).get("bets") or []
    if not bets:
        print("[consume] no pending bets")
        return 0

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    rows = []
    ids_for_row: list[tuple[str, str]] = []  # (id, "ok"|"skip"|"invalid")
    for b in bets:
        row = _row_from_bet(b, today)
        if row is None:
            ids_for_row.append((b.get("id", ""), "invalid"))
            continue
        rows.append(row)
        ids_for_row.append((b.get("id", ""), "ok"))

    added = _append_rows(rows)
    print(f"[consume] received={len(bets)} appended={added} (duplicates skipped: {len(rows) - added})")

    # Delete consumed entries (also delete invalid ones so they don't pile up)
    for bid, status in ids_for_row:
        if not bid:
            continue
        try:
            d = requests.delete(f"{base}/pending_bets/{bid}", headers=headers, timeout=15)
            if d.status_code != 200:
                print(f"[consume] DELETE {bid} → HTTP {d.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[consume] DELETE {bid} failed: {e}", file=sys.stderr)

    # Refresh KV immediately so app shows updated open_bets + bankroll_state
    if added > 0:
        try:
            from src.notifications.web_dashboard import write_signals_json
            write_signals_json()
            print("[consume] KV state refreshed")
        except Exception as e:
            print(f"[consume] KV refresh failed (non-fatal): {e}", file=sys.stderr)

        # Push ledger to GitHub so the CI watchdog sees the new bets and
        # doesn't overwrite the KV with a stale repo state.
        import subprocess
        try:
            ROOT = Path(__file__).resolve().parent.parent
            def _g(*args):
                return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                      text=True, timeout=30)
            # Only the ledger — no other files (avoid pushing local-only state).
            _g("add", "results/ledger.csv")
            staged = _g("diff", "--cached", "--quiet", "results/ledger.csv")
            if staged.returncode != 0:  # there are staged changes
                commit_msg = f"auto: ledger sync {added} bet(s)"
                _g("commit", "-m", commit_msg,
                   "--author=SportsBrain Bot <bot@sportsbrain>")
                for attempt in range(1, 6):
                    _g("pull", "--rebase", "origin", "main")
                    push = _g("push", "origin", "main")
                    if push.returncode == 0:
                        print(f"[consume] ledger pushed to GitHub ({commit_msg})")
                        break
                    print(f"[consume] push attempt {attempt} failed: "
                          f"{push.stderr.strip()[:120]}", file=sys.stderr)
                    import time as _t, random as _r
                    _t.sleep(_r.randint(2, 6))
                else:
                    print("[consume] ledger push gave up after 5 attempts",
                          file=sys.stderr)
        except Exception as e:
            print(f"[consume] ledger push failed (non-fatal): {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
