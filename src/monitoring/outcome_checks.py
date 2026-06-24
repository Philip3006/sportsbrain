"""Outcome-Level Self-Diagnose.

Während `aggregate_health.py` nur job-Frische + exit_code prüft, fragt dieses
Modul *Outcomes*: wurde eine offene Wette nach Match-Ende abgerechnet? Liefert
signals.json frische Daten? Kommen Pushes überhaupt noch raus?

Jeder Check liefert maximal ein `Symptom`. Symptome haben eine stabile
`symptom_id`, damit `auto_heal_ai` Cooldowns sauber tracken kann.

CLI: `python3 -m src.monitoring.outcome_checks`
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

LEDGER_PATH = ROOT / "results" / "ledger_philip.csv"
SIGNALS_PATH = ROOT / "docs" / "data" / "signals.json"
PUSH_DELIVERY_PATH = ROOT / "results" / "health" / "push_delivery.json"
SETTLE_LOG = ROOT / "results" / "settle.log"

# Symptom-IDs sind stabil — auto_heal_ai nutzt sie als Cooldown-Key.
SYM_STUCK_BETS = "stuck_open_bets"
SYM_SIGNALS_STALE = "signals_stale"
SYM_PUSH_DEAD = "push_delivery_dead"
SYM_PUSH_EXPIRED = "push_subscriptions_expired"
SYM_SETTLE_SILENT = "settle_ran_but_no_progress"


@dataclass
class Symptom:
    id: str
    severity: str  # "warn" | "error"
    summary: str
    suggested_action: str  # action-id, siehe ACTION_MAP in auto_heal_ai.py
    payload: dict


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(p: Path):
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


# ── Checks ───────────────────────────────────────────────────────

def _stuck_open_bets(now: datetime, max_age_h: int = 24) -> list[dict]:
    """Bets mit status=open deren match_date > max_age_h zurückliegt."""
    if not LEDGER_PATH.exists():
        return []
    cutoff = (now - timedelta(hours=max_age_h)).date()
    stuck = []
    try:
        with LEDGER_PATH.open() as f:
            for r in csv.DictReader(f):
                if (r.get("status") or "").strip().lower() != "open":
                    continue
                date_str = (r.get("match_date") or "").strip()
                try:
                    md = datetime.fromisoformat(date_str).date()
                except (ValueError, TypeError):
                    continue
                if md <= cutoff:
                    stuck.append({
                        "match_id": r.get("match_id", ""),
                        "match": f"{r.get('home','?')} vs {r.get('away','?')}",
                        "market": r.get("market", ""),
                        "match_date": date_str,
                    })
    except Exception:
        return []
    return stuck


def check_stuck_open_bets() -> Symptom | None:
    stuck = _stuck_open_bets(_now())
    if not stuck:
        return None
    return Symptom(
        id=SYM_STUCK_BETS,
        severity="error",
        summary=f"{len(stuck)} offene Bet(s) mit Match-Datum > 24h alt — Settlement hängt.",
        suggested_action="re-run-settle",
        payload={"bets": stuck[:10], "count": len(stuck)},
    )


def check_signals_freshness(max_age_min: int = 90) -> Symptom | None:
    """signals.json wurde seit max_age_min nicht aktualisiert."""
    sig = _read_json(SIGNALS_PATH)
    if not isinstance(sig, dict):
        return None
    updated = _parse_iso(sig.get("updated"))
    if updated is None:
        return None
    age_min = (_now() - updated).total_seconds() / 60
    if age_min <= max_age_min:
        return None
    return Symptom(
        id=SYM_SIGNALS_STALE,
        severity="warn",
        summary=f"signals.json ist {int(age_min)} Min alt (Grenze {max_age_min}).",
        suggested_action="force-refresh-signals",
        payload={"updated": sig.get("updated"), "age_min": int(age_min)},
    )


def check_push_delivery_health() -> Symptom | None:
    """Liest push_delivery.json. Symptom wenn:
       - heutige attempt-Quote > 0 und pruned_410 / attempted >= 0.5, ODER
       - last_send_at > 24h her trotz settled bets im selben Zeitraum.
    """
    state = _read_json(PUSH_DELIVERY_PATH)
    if not isinstance(state, dict):
        return None

    today = state.get(_now().strftime("%Y-%m-%d"), {})
    attempted = int(today.get("attempted", 0) or 0)
    pruned = int(today.get("pruned_410", 0) or 0)
    if attempted > 0 and pruned / attempted >= 0.5:
        return Symptom(
            id=SYM_PUSH_EXPIRED,
            severity="error",
            summary=f"Heute {pruned}/{attempted} Push-Abos abgelaufen (≥50%) — Re-Subscribe nötig.",
            suggested_action="prompt-resubscribe",
            payload={"attempted": attempted, "pruned": pruned},
        )

    last_send = _parse_iso(state.get("last_send_at"))
    if last_send is not None:
        age_h = (_now() - last_send).total_seconds() / 3600
        # Nur warnen wenn auch wirklich Settlements gelaufen sind im Zeitraum.
        recent_settled = _count_settled_last_h(24)
        if age_h > 24 and recent_settled > 0:
            return Symptom(
                id=SYM_PUSH_DEAD,
                severity="error",
                summary=f"Letzter Push-Versuch vor {int(age_h)}h, aber {recent_settled} Bet(s) abgerechnet — Pipeline tot.",
                suggested_action="re-test-vapid",
                payload={"last_send_at": state.get("last_send_at"), "settled_24h": recent_settled},
            )
    return None


def _count_settled_last_h(hours: int) -> int:
    if not LEDGER_PATH.exists():
        return 0
    cutoff = _now() - timedelta(hours=hours)
    n = 0
    try:
        with LEDGER_PATH.open() as f:
            for r in csv.DictReader(f):
                st = (r.get("status") or "").strip().lower()
                if st not in ("won", "lost", "void"):
                    continue
                date_str = (r.get("match_date") or "").strip()
                try:
                    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if dt >= cutoff - timedelta(hours=12):
                    n += 1
    except Exception:
        return 0
    return n


def check_settle_silent() -> Symptom | None:
    """settle.log hatte in letzter Stunde Aktivität, aber stuck_open_bets > 0 bleibt."""
    if not SETTLE_LOG.exists():
        return None
    stuck = _stuck_open_bets(_now())
    if not stuck:
        return None
    # Wenn das letzte settle weniger als 1h her ist UND wir immer noch stuck Bets haben,
    # ist settle stumm gelaufen.
    try:
        mtime = datetime.fromtimestamp(SETTLE_LOG.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    if (_now() - mtime) > timedelta(hours=1):
        return None
    return Symptom(
        id=SYM_SETTLE_SILENT,
        severity="warn",
        summary=f"settle.log < 1h alt, aber {len(stuck)} Bet(s) noch open — Settlement-Quelle liefert keine Scores.",
        suggested_action="re-run-settle",
        payload={"stuck_count": len(stuck), "settle_log_age_min": int((_now() - mtime).total_seconds() / 60)},
    )


# ── Aggregator ───────────────────────────────────────────────────

_ALL_CHECKS = (
    check_stuck_open_bets,
    check_signals_freshness,
    check_push_delivery_health,
    check_settle_silent,
)


def run_all_checks() -> list[Symptom]:
    """Führt alle Checks aus und liefert aktive Symptome."""
    out: list[Symptom] = []
    for fn in _ALL_CHECKS:
        try:
            sym = fn()
        except Exception as e:
            # Ein Check-Fehler darf das Gesamtsystem nicht stoppen.
            out.append(Symptom(
                id=f"checker_error_{fn.__name__}",
                severity="warn",
                summary=f"Check {fn.__name__} failed: {e}",
                suggested_action="none",
                payload={"exc": str(e)},
            ))
            continue
        if sym is not None:
            out.append(sym)
    return out


def to_dicts(symptoms: list[Symptom]) -> list[dict]:
    return [asdict(s) for s in symptoms]


def _cli() -> int:
    symptoms = run_all_checks()
    if not symptoms:
        print("[outcome-checks] keine Symptome aktiv ✅")
        return 0
    print(f"[outcome-checks] {len(symptoms)} Symptom(e):")
    for s in symptoms:
        print(f"  - [{s.severity}] {s.id}: {s.summary}")
        print(f"    → action: {s.suggested_action}  payload: {json.dumps(s.payload)[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
