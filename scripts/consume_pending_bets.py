"""
Consume pending bets from the Cloudflare Worker KV and append them to
per-user ledger files as 'open' bets.

P0-A Durable ACK Invariant (CEO Final Gate):
    An accepted pending bet must NEVER be deleted from Worker KV before its
    ledger row is durably persisted in the canonical remote repository.

    Canonical lifecycle for accepted bets:
        1. GET  pending_bets from Worker KV
        2. Validate each bet (bankroll cap, signal_id, model_prob, …)
        3. Immediately ACK/delete REJECTED bets (they are not being persisted)
        4. Append ACCEPTED rows to per-user ledger (local write, dup-safe)
        5. Stage + commit + push to origin/main — FATAL if this fails
        6. ACK/delete ACCEPTED pending entries ONLY after push succeeds
        7. Refresh KV/public state (heartbeat, always runs)

    Failure semantics:
        - git commit failure   → return 1 → accepted KV entries remain for retry
        - git push failure     → return 1 → accepted KV entries remain for retry
        - KV ACK failure       → log + continue → next run retries (idempotent)

    Idempotency / duplicate protection:
        - _append_rows() skips rows already in ledger (match_id + market key)
        - If all rows were dups (push was already done, only KV delete failed):
            git add finds nothing staged → no new commit → push is a no-op
            → return True → ACK proceeds safely

    Persistence owner:
        The consumer Python script owns durable ledger persistence.
        The GHA workflow commit step covers health artifacts only (NOT ledger_*.csv).

Env vars:
    SIGNALS_CLOUD_URL   — e.g. https://sportsbrain-signals.<sub>.workers.dev/signals.json
    SIGNALS_API_TOKEN   — Bearer token matching Worker's API_TOKEN secret

Run manually:
    python -m scripts.consume_pending_bets
"""
from __future__ import annotations

import hashlib
import math
import os
import random as _random
import subprocess
import sys
import time as _time
from pathlib import Path

import pandas as pd
import requests

from scripts._http_retry import retry_request

# Repo-root on sys.path so `src` imports work even when run as a script.
_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.betting.ledger import (
    _FIELDS,
    _file_lock,
    _load,
    _resolve_ledger_path,
    _save,
    count_open_bets,
    ledger_summary,
)
from src.betting.signal_contract import validate_stake_cap
from src.config import BANKROLL_START, DEFAULT_USER, MAX_ACTIVE_BETS


def _worker_base() -> str:
    url = os.getenv("SIGNALS_CLOUD_URL", "").strip()
    if not url:
        raise SystemExit("SIGNALS_CLOUD_URL not set")
    url = url.removesuffix("/signals.json")
    return url.rstrip("/")


def _token() -> str:
    tok = os.getenv("SIGNALS_API_TOKEN", "").strip()
    if not tok:
        raise SystemExit("SIGNALS_API_TOKEN not set")
    return tok


def _match_id(home: str, away: str, kickoff: str) -> str:
    """Deterministic id — used for idempotency in _append_rows duplicate guard."""
    key = f"pwa|{home.strip().lower()}|{away.strip().lower()}|{(kickoff or '')[:10]}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _get_live_bankroll(user: str = DEFAULT_USER) -> float | None:
    """Compute live bankroll from ledger P&L. Returns None on failure."""
    try:
        summary = ledger_summary(user=user)
        return round(BANKROLL_START + summary["total_pnl"], 2)
    except Exception as exc:  # noqa: BLE001
        print(f"[consume] bankroll lookup failed: {exc}", file=sys.stderr)
        return None


def _row_from_bet(
    bet: dict,
    today: str,
    bankroll: float | None = None,
) -> tuple[dict, dict] | tuple[None, str]:
    """Build a ledger row from a pending bet payload.

    Returns (row_dict, extra_fields) or (None, reason_str).

    P0-A security rules applied at this boundary:
    - ALL bets (value and manual) require an authoritative bankroll.
    - stake > 5% of bankroll → REJECT (not silently cap).
    - source=value without a valid signal_id → REJECT.
    - source=value model_prob must satisfy 0 < mp < 1 (strict; calibration-eligible).
      model_prob=0.0 and model_prob=1.0 are REJECTED — Brier/ECE/LogLoss require (0,1).
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

    source = (bet.get("source") or "value").strip().lower()
    if source not in ("value", "manual"):
        return None, f"invalid source {source!r}"

    signal_id = (bet.get("signal_id") or "").strip()

    # P0-A: source=value requires a valid canonical signal_id — REJECT, do NOT reclassify
    if source == "value" and not signal_id:
        return None, "source=value requires signal_id — REJECT (use explicit source=manual for manual bets)"

    # P0-A: ALL bets require an authoritative bankroll
    if bankroll is None or bankroll <= 0:
        return None, "authoritative bankroll unavailable — cannot enforce 5% cap, bet rejected"

    # P0-A: REJECT if stake exceeds 5% cap (do not silently alter confirmed payload)
    cap_ok, cap_reason = validate_stake_cap(bankroll, stake_raw)
    if not cap_ok:
        return None, f"5% cap violation: {cap_reason}"
    cap_applied = False  # stake passed validation without capping

    stake_pct_val = round(stake_raw / bankroll * 100, 4)

    model_prob_raw = bet.get("model_prob")
    if source == "value":
        # P0-A: source=value requires valid canonical model_prob in strict open interval (0, 1).
        # Worker normalizes published percent (e.g. 52.0) → fraction (0.52) before storing.
        # null/missing/0.0/1.0/out-of-range fails closed — Brier/ECE/LogLoss require 0 < p < 1.
        if model_prob_raw is None or model_prob_raw == "":
            return None, "source=value requires canonical model_prob — null/missing fails closed"
        try:
            mp = float(model_prob_raw)
        except (TypeError, ValueError):
            return None, f"source=value model_prob {model_prob_raw!r} not numeric — reject"
        if not math.isfinite(mp) or not (0.0 < mp < 1.0):
            return None, f"source=value model_prob {mp} out of calibration range (0, 1) exclusive — reject"
        model_prob_str = f"{mp:.6f}"
    else:
        if model_prob_raw is None or model_prob_raw == "":
            model_prob_str = ""
        else:
            try:
                model_prob_str = f"{float(model_prob_raw):.6f}"
            except (TypeError, ValueError):
                model_prob_str = ""

    row = {
        "match_id":              _match_id(home, away, kickoff),
        "match_date":            match_date,
        "home":                  home,
        "away":                  away,
        "market":                market,
        "decimal_odds":          f"{odds:.4f}",
        "stake_pct":             f"{stake_pct_val:.4f}",
        "stake_amount":          f"{stake_raw:.2f}",
        "placed_date":           today,
        "status":                "open",
        "pnl":                   "0.0",
        "closing_odds":          "0.0",
        "clv":                   "",
        "pinnacle_ref_odds":     "",
        "source":                source,
        "model_prob":            model_prob_str,
        "stake_reason":          "",
        "league":                (bet.get("league") or "").strip(),
        # P0-A: canonical identity + risk provenance as explicit ledger fields
        "signal_id":             signal_id,
        "fixture_key":           (bet.get("fixture_key") or "").strip(),
        "sport":                 (bet.get("sport") or "").strip(),
        "bankroll_at_placement": f"{bankroll:.2f}",
        "cap_applied":           str(cap_applied).lower(),
    }
    return row, {}


def _append_rows(rows: list[dict], user: str = DEFAULT_USER) -> int:
    """Append rows to per-user ledger. Skips duplicates by (match_id, market)."""
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
            new_df = pd.DataFrame(new_rows, columns=_FIELDS)
            df = pd.concat([df, new_df], ignore_index=True)
            _save(df, ledger_path)
    return len(new_rows)


def _kv_delete_one(base: str, headers: dict, bid: str, user: str) -> None:
    """Delete a single pending bet from Worker KV. Logs errors but does not raise.

    KV ACK failure is non-fatal: the next run will see the bet already in the ledger
    (duplicate), skip it, and retry the ACK safely.
    """
    if not bid:
        return
    suffix = "" if user == DEFAULT_USER else f"?user={user}"
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


def _durable_push(added: int) -> bool:
    """Stage, commit (if anything staged), and push all per-user ledger files to origin.

    Returns True on success (including no-op when ledger already matches remote).
    Returns False on failure — caller must treat this as FATAL and NOT ACK pending KV entries.

    Idempotent:
    - If all accepted bets were dups already in the ledger (pushed in a previous run):
        git add finds nothing staged → no commit → push is a no-op → return True.
    - If local ledger has uncommitted changes (crash between append and commit in prev run):
        git add stages the file → commit → push → ACK on next run.
    """
    def _g(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=_ROOT, capture_output=True, text=True, timeout=30, check=False
        )

    _g("add", "results/ledger_*.csv")
    staged = _g("diff", "--cached", "--quiet")
    if staged.returncode == 0:
        # Nothing staged: ledger matches HEAD (and therefore remote from previous push).
        # This covers the dup-only retry path — safe to ACK.
        print("[consume] ledger in sync with HEAD — no new commit needed")
        return True

    commit_msg = f"auto: ledger sync {added} bet(s)"
    commit = _g("commit", "-m", commit_msg, "--author=SportsBrain Bot <bot@sportsbrain>")
    if commit.returncode != 0:
        print(f"[consume] ledger commit failed: {commit.stderr.strip()[:200]}", file=sys.stderr)
        return False

    for attempt in range(1, 6):
        _g("pull", "--rebase", "origin", "main")
        push = _g("push", "origin", "main")
        if push.returncode == 0:
            print(f"[consume] ledger pushed ({commit_msg})")
            return True
        print(
            f"[consume] push attempt {attempt} failed: {push.stderr.strip()[:120]}",
            file=sys.stderr,
        )
        _time.sleep(_random.randint(2, 6))

    print("[consume] ledger push gave up after 5 attempts", file=sys.stderr)
    return False


def _fetch_validate_user(
    base: str,
    headers: dict,
    user: str,
) -> tuple[list[dict], list[str], list[str]]:
    """Fetch and validate pending bets for `user` from Worker KV.

    Returns:
        rows:         validated ledger row dicts (passed to _append_rows)
        accepted_ids: IDs of valid bets — ACK ONLY after durable push succeeds
        rejected_ids: IDs of invalid/rejected bets — safe to ACK immediately

    Does NOT write to ledger. Does NOT delete from KV.
    """
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
        return [], [], []
    if r.status_code != 200:
        print(f"[consume:{user}] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return [], [], []

    bets = (r.json() or {}).get("bets") or []
    if not bets:
        return [], [], []

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    bankroll = _get_live_bankroll(user)

    rows: list[dict] = []
    accepted_ids: list[str] = []
    rejected_ids: list[str] = []

    for b in bets:
        # Enforce MAX_ACTIVE_BETS using current ledger + already-accepted rows this run.
        current_open = count_open_bets(user=user) + len(rows)
        if current_open >= MAX_ACTIVE_BETS:
            print(
                f"[consume:{user}] REJECT bet — max active bets ({MAX_ACTIVE_BETS}) reached",
                file=sys.stderr,
            )
            bid = b.get("id", "")
            if bid:
                rejected_ids.append(bid)
            continue

        result = _row_from_bet(b, today, bankroll)
        if isinstance(result, tuple) and len(result) == 2:
            row, _extra = result
        else:
            row, _extra = None, "unexpected result"

        bid = b.get("id", "")
        if row is None:
            reason = _extra if isinstance(_extra, str) else "invalid"
            print(f"[consume:{user}] SKIP bet: {reason}", file=sys.stderr)
            if bid:
                rejected_ids.append(bid)
        else:
            rows.append(row)
            if bid:
                accepted_ids.append(bid)

    print(
        f"[consume:{user}] received={len(bets)} accepted={len(accepted_ids)} "
        f"rejected={len(rejected_ids)}"
    )
    return rows, accepted_ids, rejected_ids


def _process_cancel_requests(base: str, headers: dict, user: str) -> int:
    """Read cancel_requests from Worker KV, apply to ledger, clear processed."""
    from src.betting.ledger import cancel_bet
    suffix = "" if user == DEFAULT_USER else f"?user={user}"
    try:
        # Non-critical — single attempt, short timeout.
        r = retry_request(
            "GET", f"{base}/cancel_requests{suffix}", headers=headers,
            timeout=8, retries=1, log_prefix=f"[cancel:{user}]",
        )
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
        home, away, market = req.get("home", ""), req.get("away", ""), req.get("market", "")
        if not (home and away and market):
            continue
        result = cancel_bet(home, away, market, user=user)
        print(f"[cancel:{user}] {home} vs {away} {market} → {result}")
        if result in ("ok", "already_cancelled"):
            cancelled += 1
    if cancelled:
        try:
            retry_request(
                "DELETE", f"{base}/cancel_requests{suffix}", headers=headers,
                timeout=8, retries=1, log_prefix=f"[cancel:{user}]",
            )
        except Exception:  # noqa: BLE001, S110
            pass
    return cancelled


def main() -> int:
    base = _worker_base()
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}

    from src.notifications.web_dashboard import list_known_users

    # ── Phase 1: Fetch and validate all users ────────────────────────────────
    rows_by_user: dict[str, list[dict]] = {}
    # (bet_id, user) pairs — user needed for per-user KV suffix
    all_accepted: list[tuple[str, str]] = []   # ACK only after durable push
    all_rejected: list[tuple[str, str]] = []   # ACK immediately (not persisted)

    for u in list_known_users():
        rows, accepted_ids, rejected_ids = _fetch_validate_user(base, headers, u)
        if rows:
            rows_by_user[u] = rows
        all_accepted.extend((bid, u) for bid in accepted_ids)
        all_rejected.extend((bid, u) for bid in rejected_ids)
        _process_cancel_requests(base, headers, u)

    # ── Phase 2: Immediately ACK rejected bets ───────────────────────────────
    # Rejected bets are not being persisted — safe to remove from KV now.
    for bid, user in all_rejected:
        _kv_delete_one(base, headers, bid, user)

    # ── Phase 3: Durable persistence lifecycle for accepted bets ────────────
    total_added = 0
    if all_accepted:
        # 3a. Append accepted rows to per-user ledger (local write, dup-safe).
        for u, rows in rows_by_user.items():
            total_added += _append_rows(rows, user=u)

        # 3b. Durable push: stage → commit (if staged) → push to origin.
        #     FATAL: if push fails, accepted KV entries remain for retry — return 1.
        push_ok = _durable_push(total_added)
        if not push_ok:
            print(
                "[consume] FATAL: durable push failed — "
                "accepted bets remain in KV for retry on next run",
                file=sys.stderr,
            )
            return 1

        # 3c. Push succeeded → ACK accepted bets from KV.
        for bid, user in all_accepted:
            _kv_delete_one(base, headers, bid, user)

        print(
            f"[consume] {total_added} new row(s) persisted, "
            f"{len(all_accepted)} bet(s) ACK'd from KV"
        )
    else:
        print("[consume] no pending bets (across all users)")

    # ── Phase 4: Refresh KV state (heartbeat — always runs) ─────────────────
    # Updates bankroll_state.published_at so Worker AUTH_STATE_MAX_AGE_MS check
    # never fails solely due to a scan gap (≤30 min refresh cadence via cron).
    try:
        from src.notifications.web_dashboard import write_signals_json_all_users
        write_signals_json_all_users()
        print(f"[consume] KV state refreshed (added={total_added})")
    except Exception as e:  # noqa: BLE001
        print(f"[consume] KV refresh failed (non-fatal): {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
