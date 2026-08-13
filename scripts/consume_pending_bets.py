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

from scripts._http_retry import retry_request

# Repo-root on sys.path so `src` imports work even when run as a script.
_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.betting.ledger import _FIELDS, _load, _save, _file_lock, _resolve_ledger_path, count_open_bets, ledger_summary
from src.betting.signal_contract import compute_safe_stake
from src.config import DEFAULT_USER, MAX_ACTIVE_BETS, BANKROLL_START


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


def _get_live_bankroll(user: str = DEFAULT_USER) -> float | None:
    """Compute live bankroll from ledger P&L. Returns None on failure."""
    try:
        summary = ledger_summary(user=user)
        return round(BANKROLL_START + summary["total_pnl"], 2)
    except Exception as exc:
        print(f"[consume] bankroll lookup failed: {exc}", file=sys.stderr)
        return None


def _row_from_bet(bet: dict, today: str, bankroll: float | None = None) -> dict | tuple[None, str]:
    """Build a ledger row from a pending bet payload.

    Returns the row dict, or (None, reason) if the bet cannot be written.
    When bankroll is provided the 5% cap is enforced and stake_pct is truthful.
    """
    match = (bet.get("match") or "").strip()
    if " vs " not in match:
        return None, "invalid match format"
    home, away = [s.strip() for s in match.split(" vs ", 1)]
    market = (bet.get("market") or "").strip()
    odds = float(bet.get("odds") or 0)
    stake_raw = float(bet.get("stake_eur") or 0)
    if odds < 1.01 or stake_raw <= 0:
        return None, f"invalid odds ({odds}) or stake ({stake_raw})"
    kickoff = bet.get("kickoff") or ""
    match_date = kickoff[:10] if kickoff else ""

    # Source validation: reclassify to manual if signal_id missing for value bets
    source = (bet.get("source") or "value").strip().lower()
    if source not in ("value", "manual"):
        source = "value"
    signal_id = (bet.get("signal_id") or "").strip()
    if source == "value" and not signal_id:
        source = "manual"  # reclassify — no canonical signal identity

    # 5% bankroll cap revalidation
    cap_applied = False
    if bankroll is not None and bankroll > 0:
        stake, cap_applied = compute_safe_stake(bankroll, stake_raw)
        stake_pct_val = round(stake / bankroll * 100, 4)
    elif source == "value" and bankroll is None:
        # Fail closed for value bets when bankroll is unknown
        return None, "bankroll unknown — cannot revalidate 5% cap for value bet"
    else:
        stake = stake_raw
        stake_pct_val = 0.0  # manual bets without bankroll context

    model_prob_raw = bet.get("model_prob")
    if model_prob_raw is None or model_prob_raw == "":
        model_prob_str = ""
    else:
        try:
            model_prob_str = f"{float(model_prob_raw):.6f}"
        except (TypeError, ValueError):
            model_prob_str = ""

    row = {
        "match_id":         _match_id(home, away, kickoff),
        "match_date":       match_date,
        "home":             home,
        "away":             away,
        "market":           market,
        "decimal_odds":     f"{odds:.4f}",
        "stake_pct":        f"{stake_pct_val:.4f}",
        "stake_amount":     f"{stake:.2f}",
        "placed_date":      today,
        "status":           "open",
        "pnl":              "0.0",
        "closing_odds":     "0.0",
        "clv":              "",
        "pinnacle_ref_odds": "",
        "source":           source,
        "model_prob":       model_prob_str,
        # Extra identity fields (stored as stake_reason for backward-compat CSV schema)
        # Actual new fields are written via extra_fields dict passed to _append_rows
    }
    extra = {
        "signal_id":          signal_id,
        "fixture_key":        (bet.get("fixture_key") or "").strip(),
        "sport":              (bet.get("sport") or "").strip(),
        "league":             (bet.get("league") or "").strip(),
        "bankroll_at_placement": f"{bankroll:.2f}" if bankroll is not None else "",
        "cap_applied":        str(cap_applied).lower(),
    }
    return row, extra


def _append_rows(rows: list[dict], user: str = DEFAULT_USER) -> int:
    if not rows:
        return 0
    ledger_path = _resolve_ledger_path(None, user)
    with _file_lock(ledger_path):
        df = _load(ledger_path)
        existing = set(zip(
            df.get("match_id", pd.Series([], dtype=str)),
            df.get("market", pd.Series([], dtype=str)),
        ))
        new_rows = [r for r in rows if (r["match_id"], r["market"]) not in existing]
        if new_rows:
            # Only include canonical _FIELDS columns — extra identity fields are
            # piggy-backed onto stake_reason where schema allows, otherwise dropped
            # gracefully to keep backward-compat with existing CSV schema.
            safe_rows = []
            for r in new_rows:
                # stake_reason: encode signal_id + fixture_key for traceability
                sig_id = r.pop("signal_id", "")
                fix_key = r.pop("fixture_key", "")
                r.pop("bankroll_at_placement", None)
                r.pop("cap_applied", None)
                r.pop("sport", None)
                # league is a canonical _FIELDS column — keep it
                if sig_id or fix_key:
                    r["stake_reason"] = f"sig={sig_id} fix={fix_key}".strip()
                safe_rows.append(r)
            new_df = pd.DataFrame(safe_rows, columns=_FIELDS)
            df = pd.concat([df, new_df], ignore_index=True)
            _save(df, ledger_path)
    return len(new_rows)


def _consume_user(base: str, headers: dict, user: str) -> int:
    """Consume pending_bets for `user` and append to per-user ledger.
    Master-token uses ?user= query param to target a specific slot.
    Returns rows added."""
    suffix = "" if user == DEFAULT_USER else f"?user={user}"
    try:
        r = retry_request(
            "GET",
            f"{base}/pending_bets{suffix}",
            headers=headers,
            timeout=15,
            log_prefix=f"[consume:{user}]",
        )
    except requests.RequestException as e:
        print(f"[consume:{user}] fetch failed: {e}", file=sys.stderr)
        return 0
    if r.status_code != 200:
        print(f"[consume:{user}] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 0

    bets = (r.json() or {}).get("bets") or []
    if not bets:
        return 0

    today = pd.Timestamp.now().strftime("%Y-%m-%d")

    # Fetch bankroll once for this user (used for 5% cap revalidation)
    bankroll = _get_live_bankroll(user)

    rows = []
    ids_for_row: list[tuple[str, str]] = []
    for b in bets:
        # Enforce MAX_ACTIVE_BETS before adding each new bet
        current_open = count_open_bets(user=user) + len(rows)
        source = (b.get("source") or "value").strip().lower()
        if source not in ("value", "manual"):
            source = "value"
        signal_id = (b.get("signal_id") or "").strip()
        if source == "value" and not signal_id:
            source = "manual"
        if current_open >= MAX_ACTIVE_BETS:
            print(
                f"[consume:{user}] REJECT bet — max active bets ({MAX_ACTIVE_BETS}) reached",
                file=sys.stderr,
            )
            ids_for_row.append((b.get("id", ""), "max_bets"))
            continue

        result = _row_from_bet(b, today, bankroll)
        if isinstance(result, tuple):
            row, extra = result
        else:
            row, extra = result, {}
        if row is None:
            reason = extra if isinstance(extra, str) else "invalid"
            print(f"[consume:{user}] SKIP bet: {reason}", file=sys.stderr)
            ids_for_row.append((b.get("id", ""), "invalid"))
            continue
        # Merge extra identity fields into the row
        if isinstance(extra, dict):
            row.update({k: v for k, v in extra.items() if k not in row})
        rows.append(row)
        ids_for_row.append((b.get("id", ""), "ok"))

    added = _append_rows(rows, user=user)
    print(f"[consume:{user}] received={len(bets)} appended={added} (dup-skip={len(rows) - added})")

    for bid, _status in ids_for_row:
        if not bid:
            continue
        try:
            d = retry_request(
                "DELETE",
                f"{base}/pending_bets/{bid}{suffix}",
                headers=headers,
                timeout=15,
                log_prefix=f"[consume:{user}]",
            )
            if d.status_code != 200:
                print(f"[consume:{user}] DELETE {bid} → HTTP {d.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"[consume:{user}] DELETE {bid} failed: {e}", file=sys.stderr)
    return added


def _process_cancel_requests(base: str, headers: dict, user: str) -> int:
    """Read cancel_requests from Worker KV, apply to ledger, clear processed."""
    from src.betting.ledger import cancel_bet
    suffix = "" if user == DEFAULT_USER else f"?user={user}"
    try:
        # O1-1: cancel_requests is non-critical — single attempt, short timeout.
        # Previously 3 retries×15s burned ~65s per user on Worker timeouts.
        r = retry_request("GET", f"{base}/cancel_requests{suffix}", headers=headers,
                          timeout=8, retries=1, log_prefix=f"[cancel:{user}]")
    except requests.RequestException as e:
        print(f"[cancel:{user}] fetch failed: {e}", file=sys.stderr)
        return 0
    if r.status_code != 200:
        return 0
    reqs = (r.json() or {}).get("requests") or []
    if not reqs:
        return 0
    cancelled = 0
    for req in reqs:
        home, away, market = req.get("home",""), req.get("away",""), req.get("market","")
        if not (home and away and market):
            continue
        result = cancel_bet(home, away, market, user=user)
        print(f"[cancel:{user}] {home} vs {away} {market} → {result}")
        if result in ("ok", "already_cancelled"):
            cancelled += 1
    if cancelled:
        try:
            retry_request("DELETE", f"{base}/cancel_requests{suffix}", headers=headers,
                          timeout=8, retries=1, log_prefix=f"[cancel:{user}]")
        except Exception:
            pass
    return cancelled


def main() -> int:
    base = _worker_base()
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}

    # D4: consume per known user. Master-token routes via ?user= query.
    from src.notifications.web_dashboard import list_known_users
    added = 0
    for u in list_known_users():
        added += _consume_user(base, headers, u)
        _process_cancel_requests(base, headers, u)

    if added == 0:
        print("[consume] no pending bets (across all users)")
        return 0

    # Refresh KV immediately so app shows updated open_bets + bankroll_state
    if added > 0:
        try:
            from src.notifications.web_dashboard import write_signals_json_all_users
            write_signals_json_all_users()
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
            # Only the ledger files — no other files (avoid pushing local-only state).
            # Add all per-user ledger files that may have changed.
            _g("add", "results/ledger_*.csv")
            staged = _g("diff", "--cached", "--quiet", "--", "results")
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
