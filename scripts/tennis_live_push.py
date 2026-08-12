"""Tennis Live-Score-Push: Satz-Abschluss + Suspension Notifications.

Logik:
  1. Lies offene Tennis-Wetten aus allen Ledgern
  2. Fetch ESPN live tennis scores (ATP + WTA)
  3. Vergleiche gegen Cache in data/cache/tennis_live_scores.json
  4. Neuer abgeschlossener Satz → Push "🎾 Satz X: 6-Y | Stand"
  5. Spiel unterbrochen (suspended/postponed) → Push "⏸ Unterbrochen"
  6. Cache + docs/data/tennis_live_scores.json schreiben
  7. data/cache/tennis_suspended.json als Nebeneffekt aktualisieren

Wird via tennis_live_scan.yml alle 15 Min ausgeloest (10-22 UTC).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.tennis_settlement import is_tennis_market
from src.config import DEFAULT_USER, ledger_path_for
from src.data.tennis_scores import canonical_match_key, fetch_tennis_scores_espn

CACHE_PATH = ROOT / "data" / "cache" / "tennis_live_scores.json"
DOCS_PATH  = ROOT / "docs" / "data" / "tennis_live_scores.json"
SUSP_PATH  = ROOT / "data" / "cache" / "tennis_suspended.json"


_STALE_LIVE_HOURS = 8  # tennis BO5 rarely exceeds ~5h; 8h is safely past any real match


def _prune_stale(cache: dict) -> dict:
    """Drop live-score entries whose `updated` is older than _STALE_LIVE_HOURS.

    Prevents false LIVE rendering in the PWA when a match entry remains
    `status=in_progress` after the actual match ended (bet closed, ESPN stopped
    reporting, or update loop skipped it because it is no longer in open bets).
    Preserves the `_meta` heartbeat key.
    """
    now = datetime.now(timezone.utc)
    pruned: dict = {}
    for key, entry in cache.items():
        if key == "_meta" or not isinstance(entry, dict):
            pruned[key] = entry
            continue
        ts_raw = entry.get("updated")
        if not ts_raw:
            pruned[key] = entry
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (now - ts).total_seconds() / 3600
            if age_hours <= _STALE_LIVE_HOURS:
                pruned[key] = entry
        except (ValueError, TypeError):
            # unparseable timestamp — keep entry rather than dropping silently
            pruned[key] = entry
    return pruned


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    cache = _prune_stale(cache)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cache, indent=2, ensure_ascii=False)
    CACHE_PATH.write_text(payload)
    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(payload)


def _touch_heartbeat(reason: str) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = _load_cache()
    cache["_meta"] = {"heartbeat_at": now_iso, "reason": reason}
    _save_cache(cache)


def _completed_sets(sets: list) -> list:
    return [(a, b) for a, b in sets if a != b and max(a, b) >= 6]


def _sets_str(sets: list, mark_last: bool = False) -> str:
    if not sets:
        return ""
    parts = []
    for i, (a, b) in enumerate(sets):
        s = f"{a}-{b}"
        if mark_last and i == len(sets) - 1:
            s += "*"
        parts.append(s)
    return ", ".join(parts)


def _sets_won(sets: list) -> tuple[int, int]:
    a = sum(1 for a, b in sets if a > b)
    b = sum(1 for a, b in sets if b > a)
    return a, b


def _open_tennis_bets() -> list[dict]:
    bets: list[dict] = []
    ledger_dir = ledger_path_for(DEFAULT_USER).parent
    for ledger in ledger_dir.glob("ledger_*.csv"):
        if "backfill" in ledger.stem or "backup" in ledger.stem:
            continue
        try:
            with open(ledger, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("status") != "open":
                        continue
                    market = r.get("market", "")
                    src = (r.get("source") or "").lower()
                    reason = (r.get("stake_reason") or "").lower()
                    if is_tennis_market(market) or "tennis" in src or "tennis" in reason:
                        bets.append(r)
        except Exception:
            continue
    return bets


def main() -> int:
    bets = _open_tennis_bets()
    if not bets:
        print("[tennis_live_push] Keine offenen Tennis-Wetten — Skip.")
        _touch_heartbeat("no open tennis bets")
        return 0

    # Dedupliziere Matches (ein Match kann mehrere offene Wetten haben)
    match_pairs: dict[str, tuple[str, str]] = {}
    for b in bets:
        home, away = b.get("home", ""), b.get("away", "")
        if not home or not away:
            continue
        ck = canonical_match_key(home, away)
        if ck not in match_pairs:
            match_pairs[ck] = (home, away)

    if not match_pairs:
        _touch_heartbeat("no valid match pairs")
        return 0

    print(f"[tennis_live_push] {len(match_pairs)} Tennis-Matches mit offenen Wetten")

    espn_all = {**fetch_tennis_scores_espn("atp"), **fetch_tennis_scores_espn("wta")}

    cache = _load_cache()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pushes_sent = 0
    live_count = 0
    suspended_matches: list[dict] = []

    try:
        from src.notifications.web_push import _send_notification
    except Exception:
        _send_notification = None  # type: ignore[assignment]

    settle_reminders: list[str] = []  # match keys für Summary-Log

    for ck, (home, away) in match_pairs.items():
        sc = (espn_all.get(ck)
              or espn_all.get(f"{home} vs {away}")
              or espn_all.get(f"{away} vs {home}"))

        prev = cache.get(ck, {})

        # ── Settle-Reminder: Match beendet via ESPN ─────────────────────────
        if sc:
            status = sc.get("status", "")
            if status in ("completed", "retired", "walkover") and not prev.get("settle_push_sent"):
                sets = sc.get("sets") or []
                score_str = _sets_str(sets)
                winner = sc.get("winner", "")
                body = f"Match beendet: {home} vs {away}"
                if winner:
                    body += f" — Gewinner: {winner}"
                if score_str:
                    body += f" · {score_str}"
                if _send_notification:
                    try:
                        _send_notification("✅ Settle jetzt!", body)
                        pushes_sent += 1
                        settle_reminders.append(ck)
                        print(f"  ✅ Settle-Reminder pushed: {home} vs {away}")
                    except Exception:
                        pass
                cache[ck] = {**prev, "settle_push_sent": True, "updated": now_iso}
                continue  # kein weiteres Processing für abgeschlossene Matches

        # ── Timeout-Guard: Bet-Kickoff + 3h überschritten, kein Score ────────
        # Schützt vor ESPN-Ausfällen bei Matches die schon enden sollten.
        for b in bets:
            bck = canonical_match_key(b.get("home", ""), b.get("away", ""))
            if bck != ck:
                continue
            match_date = b.get("match_date") or b.get("date") or ""
            if not match_date or prev.get("timeout_push_sent"):
                break
            try:
                from datetime import datetime as _dt, timezone as _tz
                ko_dt = _dt.fromisoformat(match_date.replace("Z", "+00:00"))
                if ko_dt.tzinfo is None:
                    ko_dt = ko_dt.replace(tzinfo=timezone.utc)
                hours_elapsed = (datetime.now(timezone.utc) - ko_dt).total_seconds() / 3600
                if hours_elapsed > 5:  # 5h nach geplantem Kickoff
                    if _send_notification and not sc:
                        try:
                            _send_notification("⏰ Prüfe Settlement", f"{home} vs {away} — kein Score-Update nach {hours_elapsed:.0f}h, Settlement prüfen!")
                            pushes_sent += 1
                            print(f"  ⏰ Timeout-Guard pushed: {home} vs {away}")
                        except Exception:
                            pass
                    cache[ck] = {**prev, "timeout_push_sent": True, "updated": now_iso}
            except Exception:
                pass
            break

        if not sc:
            continue

        status = sc.get("status", "")
        sets = sc.get("sets") or []

        if status not in ("in_progress", "suspended", "postponed"):
            continue

        live_count += 1
        curr_done = _completed_sets(sets)
        prev_done = _completed_sets(prev.get("sets", []))

        pa = sc.get("player_a", home)
        pb = sc.get("player_b", away)
        cache[ck] = {
            "home":        pa,
            "away":        pb,
            "sets":        sets,
            "sets_won":    list(_sets_won(curr_done)),
            "status":      status,
            "resume_time": sc.get("resume_time"),
            "updated":     now_iso,
            "match_start_push_sent": prev.get("match_start_push_sent", False),
            "suspended_push_sent": prev.get("suspended_push_sent", False),
        }

        # ── Suspension Push (einmal pro Unterbrechung) ─────────────────────
        if status in ("suspended", "postponed"):
            suspended_matches.append({
                "match_key": ck,
                "home": pa, "away": pb,
                "status": status,
                "sets": sets,
                "resume_time": sc.get("resume_time"),
            })
            if not prev.get("suspended_push_sent") and _send_notification:
                label = "📅 Verschoben" if status == "postponed" else "⏸ Unterbrochen"
                score_str = _sets_str(sets)
                body = f"{pa} vs {pb}"
                if score_str:
                    body += f" · Stand: {score_str}"
                if _send_notification(
                    title=f"{label} — Tennis",
                    body=body,
                    url="/sportsbrain/#bets",
                    kind="suspension",
                    tag=f"susp-{ck}",
                    require=False,
                ):
                    pushes_sent += 1
                    cache[ck]["suspended_push_sent"] = True
                    print(f"  ⏸ Suspension pushed: {pa} vs {pb}")
            continue

        # ── Match-Start Push (einmal wenn erstes Mal in_progress gesehen) ──
        prev_was_not_live = prev.get("status", "") not in ("in_progress",)
        if prev_was_not_live and not prev.get("match_start_push_sent") and _send_notification:
            score_str = _sets_str(sets, mark_last=True) if sets else ""
            body = f"{pa} vs {pb}"
            if score_str:
                body += f" · Stand: {score_str}"
            if _send_notification(
                title=f"🎾 Match gestartet!",
                body=body,
                url="/sportsbrain/#bets",
                kind="match_start",
                tag=f"start-{ck}",
                require=False,
            ):
                pushes_sent += 1
                cache[ck]["match_start_push_sent"] = True
                print(f"  🎾 Start pushed: {pa} vs {pb}")

        # ── Neu abgeschlossene Sätze → Push ────────────────────────────────
        new_sets = curr_done[len(prev_done):]
        for i, (sa, sb) in enumerate(new_sets):
            set_num = len(prev_done) + i + 1
            aw, bw = _sets_won(curr_done)
            sets_str = _sets_str(curr_done)
            winner_name = pa if sa > sb else pb
            if _send_notification and _send_notification(
                title=f"🎾 Satz {set_num}: {sa}-{sb} — {winner_name}",
                body=f"{pa} vs {pb} · {sets_str} ({aw}:{bw} Sätze)",
                url="/sportsbrain/#bets",
                kind="tennis_set",
                tag=f"tset-{ck}-s{set_num}",
                require=False,
            ):
                pushes_sent += 1
                print(f"  🎾 Satz {set_num} pushed: {pa} {sa}-{sb} {pb}")

    cache["_meta"] = {
        "heartbeat_at": now_iso,
        "reason": f"{live_count} live matches, {pushes_sent} pushes",
    }
    _save_cache(cache)

    # tennis_suspended.json als Nebeneffekt aktualisieren
    SUSP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUSP_PATH.write_text(json.dumps({"updated": now_iso, "matches": suspended_matches}, indent=2))

    settle_str = f", {len(settle_reminders)} settle-reminder(s)" if settle_reminders else ""
    print(f"[tennis_live_push] {live_count} live, {pushes_sent} push(es) gesendet{settle_str}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
