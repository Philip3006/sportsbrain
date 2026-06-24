"""Generates results/session_report.md — a 24h health + bet briefing shown
at the start of every Claude-Code session via the SessionStart hook.

Inputs (best-effort, all optional):
  - results/health/*.json           (current per-job snapshots)
  - results/health/push_state.json  (which jobs sent failure pushes)
  - results/audits/publish_failure_latest.json
  - results/ledger.csv              (settled bets in last 24h)
  - results/*.log + results/*_cron.log (errored runs in last 24h)

Output: results/session_report.md (overwritten on each call).

CLI: python3 scripts/generate_session_report.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HEALTH_DIR = ROOT / "results" / "health"
AUDITS_DIR = ROOT / "results" / "audits"
from src.config import ledger_path_for, DEFAULT_USER
LEDGER = ledger_path_for(DEFAULT_USER)
LOG_DIR = ROOT / "results"
REPORT_OUT = ROOT / "results" / "session_report.md"

CUTOFF = datetime.now(timezone.utc) - timedelta(hours=24)


# -------- helpers --------

def _read_json(p: Path) -> dict | list | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# -------- sections --------

def collect_health() -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (failures, degraded, ok) job dicts."""
    failures, degraded, ok = [], [], []

    def _classify(entries: list[dict]) -> None:
        for data in entries:
            if not isinstance(data, dict):
                continue
            st = data.get("status")
            if st == "error":
                failures.append(data)
            elif st in ("degraded", "stale"):
                degraded.append(data)
            else:
                ok.append(data)

    per_job_files = sorted(HEALTH_DIR.glob("*.json")) if HEALTH_DIR.exists() else []
    per_job_files = [p for p in per_job_files if p.name != "push_state.json"]

    if per_job_files:
        _classify([_read_json(p) for p in per_job_files])
    else:
        # Fallback: read docs/data/health.json committed by GH Actions
        cloud = ROOT / "docs" / "data" / "health.json"
        if cloud.exists():
            data = _read_json(cloud)
            if isinstance(data, dict):
                _classify(data.get("jobs", []))

    return failures, degraded, ok


_LOG_TS_PAT = re.compile(
    r"\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}| UTC| CEST| CET| \w+)?)\]?"
)
_LOG_MAX_AGE_H = 4  # only surface errors from the last 4 hours


def _log_line_recent(line: str, cutoff: datetime) -> bool:
    """Return True if the log line carries a timestamp newer than cutoff."""
    m = _LOG_TS_PAT.search(line)
    if not m:
        return False  # no timestamp → can't confirm recency, exclude
    raw = m.group(1).strip()
    # Normalise common non-standard suffixes
    for tz_str, offset in ((" UTC", "+00:00"), (" CEST", "+02:00"), (" CET", "+01:00")):
        if raw.endswith(tz_str):
            raw = raw[: -len(tz_str)] + offset
            break
    raw = raw.replace(" ", "T", 1) if "T" not in raw else raw
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except Exception:
        return False


def collect_recent_log_errors() -> dict[str, list[str]]:
    """Greps cron logs for error keywords in the last _LOG_MAX_AGE_H hours.

    Only lines with a parseable timestamp within the window are included so that
    old resolved errors don't show up as active issues in the session report.
    """
    job_files = {
        "scan":                  "scan_cron.log",
        "closing_odds":          "closing_odds_cron.log",
        "prematch_scan":         "prematch_scan_cron.log",
        "consume_pending_bets":  "consume_pending_bets.log",
        "settle":                "settle.log",
        "auto_retrain":          "launchd_auto_retrain.log",
        "live_score_push":       "launchd_live_score_push.log",
    }
    pat = re.compile(r"error|exception|traceback|timeout|rejected|401|403|500|502|504",
                     re.IGNORECASE)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_LOG_MAX_AGE_H)
    out: dict[str, list[str]] = {}
    for job, fname in job_files.items():
        path = LOG_DIR / fname
        if not path.exists():
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        # Only scan recent tail (last 500 lines) then time-filter
        recent_lines = [ln.strip() for ln in lines[-500:] if _log_line_recent(ln, cutoff)]
        matches = [ln for ln in recent_lines if pat.search(ln)]
        if matches:
            # Last 3 distinct messages so the report stays readable.
            seen, kept = set(), []
            for ln in reversed(matches):
                key = ln[:80]
                if key in seen:
                    continue
                seen.add(key)
                kept.append(ln[:200])
                if len(kept) >= 3:
                    break
            out[job] = list(reversed(kept))
    return out


def collect_audit_blocker() -> dict | None:
    return _read_json(AUDITS_DIR / "publish_failure_latest.json")


def collect_outcome_symptoms() -> list[dict]:
    """Run outcome-checks und liefere serialisierte Symptome."""
    try:
        from src.monitoring.outcome_checks import run_all_checks, to_dicts
    except Exception:
        return []
    try:
        return to_dicts(run_all_checks())
    except Exception:
        return []


def collect_self_heal_activity_24h() -> list[dict]:
    """Parsed results/auto_heal.log für die letzten 24h.

    Liefert sortierte Liste von Ereignissen: outcome-symptom, auto-action, resolved.
    """
    log = ROOT / "results" / "auto_heal.log"
    if not log.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    pat_ts = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\] \[auto_heal_ai\] (.+)")
    out: list[dict] = []
    try:
        lines = log.read_text(errors="ignore").splitlines()[-2000:]
    except Exception:
        return []
    for ln in lines:
        m = pat_ts.match(ln)
        if not m:
            continue
        try:
            dt = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < cutoff:
            continue
        msg = m.group(2)
        kind = None
        for key in ("auto-action:", "outcome-symptom:", "resolved nach", "needs human", "persistiert"):
            if key in msg:
                kind = key.rstrip(":")
                break
        if kind is None:
            continue
        out.append({"ts": m.group(1), "kind": kind, "msg": msg[:200]})
    return out


def collect_push_delivery_24h() -> dict | None:
    p = ROOT / "results" / "health" / "push_delivery.json"
    state = _read_json(p)
    if not isinstance(state, dict):
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = state.get(today) or {}
    return {
        "today": bucket,
        "last_send_at": state.get("last_send_at"),
    }


def collect_ledger_24h() -> dict:
    """Counts wins/losses/voids in last 24h and sums P&L."""
    if not LEDGER.exists():
        return {"settled": 0, "won": 0, "lost": 0, "void": 0,
                "pnl": 0.0, "rows": []}
    won = lost = void = 0
    pnl = 0.0
    rows: list[dict] = []
    try:
        with LEDGER.open() as f:
            for r in csv.DictReader(f):
                status = (r.get("status") or "").strip().lower()
                if status not in ("won", "lost", "void"):
                    continue
                date_str = (r.get("match_date") or "").strip()
                # cheap 24h filter: keep "yesterday or today" by date prefix
                # (precise enough for a daily briefing — UTC vs local within tolerance).
                if not date_str:
                    continue
                try:
                    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if dt < CUTOFF - timedelta(hours=12):
                    continue  # > 36h old → ignore
                rows.append(r)
                try:
                    pnl += float(r.get("pnl") or 0)
                except ValueError:
                    pass
                if status == "won":
                    won += 1
                elif status == "lost":
                    lost += 1
                elif status == "void":
                    void += 1
    except Exception:
        pass
    return {"settled": won + lost + void, "won": won, "lost": lost, "void": void,
            "pnl": round(pnl, 2), "rows": rows}


# -------- markdown rendering --------

def _fmt_rel(iso: str | None) -> str:
    dt = _parse_iso(iso)
    if not dt:
        return "—"
    diff_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if diff_min < 1:
        return "gerade eben"
    if diff_min < 60:
        return f"vor {int(diff_min)} Min"
    if diff_min < 60 * 24:
        return f"vor {int(diff_min/60)} h"
    return f"vor {int(diff_min/(60*24))} Tag(en)"


def render() -> str:
    failures, degraded, ok = collect_health()
    log_errors = collect_recent_log_errors()
    audit = collect_audit_blocker()
    ledger = collect_ledger_24h()
    symptoms = collect_outcome_symptoms()
    heal_events = collect_self_heal_activity_24h()
    push_stats = collect_push_delivery_24h()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts: list[str] = []
    parts.append(f"# 🛡️ System-Bericht — {today} (letzte 24h)\n")

    # --- Failures ---
    parts.append("## 🔴 Failures")
    if failures:
        for j in failures:
            parts.append(
                f"- **{j['job']}** (exit {j.get('exit_code')}, {_fmt_rel(j.get('last_run_at'))})\n"
                f"  - `{(j.get('error') or '').strip()[:200]}`"
            )
    else:
        parts.append("- keine Job-Fehler in den aktuellen Health-Snapshots ✅")
    parts.append("")

    # --- Degraded / Fallback ---
    parts.append("## 🟠 Degraded / Fallback aktiv")
    deg_with_fb = [j for j in degraded if j.get("fallback_used") or j.get("status") == "degraded"]
    if deg_with_fb:
        for j in deg_with_fb:
            fb = j.get("fallback_used") or "(stale/no-data)"
            parts.append(f"- **{j['job']}**: Fallback `{fb}` — {_fmt_rel(j.get('last_run_at'))}")
    else:
        parts.append("- alle Jobs liefern aus der primären Quelle ✅")
    stale = [j for j in degraded if j.get("status") == "stale"]
    if stale:
        parts.append("")
        parts.append("**Stale (overdue):**")
        for j in stale:
            parts.append(f"- {j['job']} — letztes Update {_fmt_rel(j.get('last_run_at'))}")
    parts.append("")

    # --- Erfolgreich ---
    parts.append("## ✅ Erfolgreich (letzte Runs)")
    if ok:
        parts.append("- " + ", ".join(j["job"] for j in ok if isinstance(j, dict) and "job" in j))
    else:
        parts.append("- noch keine ok-Snapshots vorhanden")
    parts.append("")

    # --- Outcome-Symptome (End-State-Checks jenseits exit_code) ---
    parts.append("## 🟠 Stille Degradationen (Outcome-Level)")
    if symptoms:
        for s in symptoms:
            parts.append(
                f"- **[{s['severity']}] {s['id']}** — {s['summary']}"
                f"\n  - suggested action: `{s['suggested_action']}`"
            )
    else:
        parts.append("- keine — Outcomes konsistent mit erwartetem Verhalten ✅")
    parts.append("")

    # --- Self-Heal-Aktivität letzte 24h ---
    parts.append("## 🛠 Self-Heal-Aktivität (24h)")
    if heal_events:
        # Aggregieren: zähle pro (kind, sym_id) — die Liste sonst zu lang
        from collections import Counter
        cnt: Counter = Counter()
        last_seen: dict[str, str] = {}
        for ev in heal_events:
            key = ev["msg"].split(":")[0][:60] + " | " + ev["kind"]
            cnt[key] += 1
            last_seen[key] = ev["ts"]
        for key, n in cnt.most_common(15):
            parts.append(f"- {key} ×{n} (zuletzt {last_seen[key]} UTC)")
    else:
        parts.append("- keine Auto-Heal-Aktivität geloggt")
    parts.append("")

    # --- Push-Delivery ---
    parts.append("## 📲 Push-Delivery (heute)")
    if push_stats and push_stats.get("today"):
        t = push_stats["today"]
        last = push_stats.get("last_send_at", "—")
        parts.append(
            f"- attempted {t.get('attempted',0)} · sent {t.get('sent',0)} · "
            f"pruned_410 {t.get('pruned_410',0)} — letzter Send: {last}"
        )
    else:
        parts.append("- noch keine Push-Versuche heute")
    parts.append("")

    # --- Recent log errors ---
    if log_errors:
        parts.append("## 📜 Auffällige Log-Zeilen (Cron-Logs)")
        for job, lines in log_errors.items():
            parts.append(f"**{job}:**")
            for ln in lines:
                parts.append(f"- `{ln}`")
        parts.append("")

    # --- Audit blocker ---
    if isinstance(audit, dict) and audit.get("status") == "blocked":
        alert = audit.get("alert", {})
        parts.append("## 🚧 Publish-Audit Blocker")
        parts.append(f"- {alert.get('code','?')}: {alert.get('message','')}")
        parts.append(f"- generated_at: {audit.get('generated_at','?')}")
        parts.append("")

    # --- Tagesbilanz ---
    parts.append("## 📊 Tagesbilanz (Bets ≤ 36h)")
    parts.append(
        f"- {ledger['settled']} settled — W{ledger['won']} / L{ledger['lost']} / "
        f"V{ledger['void']}, P&L: {ledger['pnl']:+.2f} €"
    )
    for r in ledger["rows"][:10]:
        parts.append(
            f"  - {r.get('home','?')} vs {r.get('away','?')} "
            f"`{r.get('market','?')}` @{r.get('decimal_odds','?')} → "
            f"{r.get('status','?')} ({r.get('pnl','?')})"
        )
    parts.append("")

    # --- Empfehlungen ---
    parts.append("## 🔧 Empfohlene Fixes für die nächste Session")
    suggestions: list[str] = []
    for j in failures:
        suggestions.append(f"- **{j['job']}**: exit={j.get('exit_code')} — `{(j.get('error') or '')[:120]}`")
    for j in deg_with_fb:
        suggestions.append(f"- **{j['job']}**: läuft auf Fallback `{j.get('fallback_used')}`, primäre Quelle prüfen")
    for s in symptoms:
        suggestions.append(
            f"- **{s['id']}** ({s['severity']}): {s['summary']} — Action: `{s['suggested_action']}`"
        )
    if isinstance(audit, dict) and audit.get("status") == "blocked":
        suggestions.append("- Publish-Audit: Blocker-Match prüfen, ggf. DC-Retrain erzwingen")
    # Surface log errors (already time-filtered to last 4h) that aren't covered by health JSON
    jobs_with_health_issue = {j["job"] for j in failures + deg_with_fb}
    for job, lines in log_errors.items():
        if job not in jobs_with_health_issue:
            snippet = lines[-1][:120] if lines else ""
            suggestions.append(f"- **{job}** (Log-Fehler letzte 4h): `{snippet}`")
    if not suggestions:
        suggestions.append("- nichts kritisches — System läuft sauber ✅")
    parts += suggestions
    parts.append("")

    parts.append(f"_generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_\n")
    return "\n".join(parts)


def main() -> int:
    REPORT_OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(render())
    print(f"[session-report] wrote {REPORT_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
