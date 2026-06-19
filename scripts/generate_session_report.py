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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
HEALTH_DIR = ROOT / "results" / "health"
AUDITS_DIR = ROOT / "results" / "audits"
LEDGER = ROOT / "results" / "ledger.csv"
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


def collect_recent_log_errors() -> dict[str, list[str]]:
    """Greps cron logs for error keywords in the last ~24h.

    Returns {job: [line, ...]} truncated. We can't perfectly time-filter the
    log file content, so we just take the last N matching lines per file
    as a proxy.
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
    out: dict[str, list[str]] = {}
    for job, fname in job_files.items():
        path = LOG_DIR / fname
        if not path.exists():
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        matches = [ln.strip() for ln in lines[-300:] if pat.search(ln)]
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
        parts.append("- " + ", ".join(j["job"] for j in ok))
    else:
        parts.append("- noch keine ok-Snapshots vorhanden")
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
        suggestions.append(f"- {j['job']}: prüfen warum exit={j.get('exit_code')} — `{(j.get('error') or '')[:120]}`")
    for j in deg_with_fb:
        suggestions.append(f"- {j['job']}: läuft auf Fallback `{j.get('fallback_used')}`, primäre Quelle prüfen")
    if isinstance(audit, dict) and audit.get("status") == "blocked":
        suggestions.append("- Publish-Audit: Blocker-Match prüfen, ggf. DC-Retrain erzwingen")
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
