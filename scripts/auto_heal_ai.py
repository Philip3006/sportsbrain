"""
Layer 2 AI healer — runs every 15 min via launchd, no Claude Code needed.
Reads health.json, calls Anthropic API to diagnose and fix code bugs in scripts/.
Requires ANTHROPIC_API_KEY in .env

Handled:
  - Code bugs in scripts/ (FIX → apply → pytest → commit → push)
  - Transient errors (network, quota) → log only
  - Fallback-active but OK (espn, stale_cache) → silent
  - Unclear errors → log + VAPID push notification

NOT handled (scope limit):
  - Anything outside scripts/ (src/, models/, data/, tests/)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
# Ensure `from src...` works when launched by launchd (no PYTHONPATH from shell).
# Previously auto_heal_ai spammed "No module named 'src'" for hours and skipped
# every outcome check silently (incident 2026-07-06).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HEALTH_JSON = ROOT / "docs" / "data" / "health.json"
HEAL_LOG = ROOT / "results" / "auto_heal.log"
COOLDOWN_STATE = ROOT / "results" / "health" / "auto_heal_cooldown.json"

# Jobs handled by Layer 1 bash (2-min auto-retry) — skip here
_SKIP_JOBS = {"consume_pending_bets", "live_score_push", "aggregate_health"}

# Global AI-call rate limit: at most 1 Claude diagnosis per 30 min across ALL jobs.
# Prevents API spam when multiple jobs fail simultaneously.
_GLOBAL_AI_COOLDOWN_MINS = 30
_GLOBAL_AI_KEY = "__global_ai_diagnose__"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [auto_heal_ai] {msg}"
    print(line)
    with HEAL_LOG.open("a") as f:
        f.write(line + "\n")


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY"):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return key


def _log_tail(job: str, n: int = 80) -> str:
    log_path = ROOT / "results" / f"{job}.log"
    if not log_path.exists():
        return "(no log file)"
    lines = log_path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


_HAIKU = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"

_DIAGNOSIS_PROMPT = """\
SportsBrain cron job '{job}' has health status '{status}' (fallback_used: {fallback}).

Log tail (last 80 lines):
<log>
{log_tail}
</log>

Analyze the root cause. Reply with EXACTLY ONE of these formats:

If it's a fixable code bug in a file under scripts/:
FIX
FILE: scripts/filename.py
OLD: <exact string to replace — single line only>
NEW: <replacement string — single line only>

If it's transient (network timeout, DNS, API quota 429, rate limit):
TRANSIENT: <brief reason>

If fallback is active but job is otherwise working (espn fallback, stale cache):
DEGRADED_OK: <brief reason>

If unclear or requires human review:
UNCLEAR: <brief reason>

Be concise. Output only the structured response above, nothing else."""

_FIX_PROMPT = """\
SportsBrain cron job '{job}' needs a code fix. Initial diagnosis:
{diagnosis}

Log tail (last 80 lines):
<log>
{log_tail}
</log>

Produce the corrected fix block (same format):
FIX
FILE: scripts/filename.py
OLD: <exact string to replace — single line only>
NEW: <replacement string — single line only>

Only fix files under scripts/. Output only the fix block, nothing else."""


def _call(model: str, prompt: str, max_tokens: int = 400) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key())
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _ask_claude(job: str, status: str, fallback: str | None, log_tail: str) -> str:
    """Haiku diagnoses; Sonnet is called only when a code fix is needed."""
    prompt = _DIAGNOSIS_PROMPT.format(
        job=job, status=status, fallback=fallback, log_tail=log_tail
    )
    diagnosis = _call(_HAIKU, prompt, max_tokens=400)

    if diagnosis.startswith("FIX"):
        _log(f"{job}: Haiku flagged FIX — escalating to Sonnet for precise fix")
        fix_prompt = _FIX_PROMPT.format(
            job=job, diagnosis=diagnosis, log_tail=log_tail
        )
        return _call(_SONNET, fix_prompt, max_tokens=600)

    return diagnosis


def _apply_fix(response: str, job: str) -> bool:
    """Parse FIX block and apply it. Returns True if committed+pushed."""
    lines = response.splitlines()
    if not lines or lines[0] != "FIX":
        return False

    def _val(prefix: str) -> str:
        line = next((l for l in lines if l.startswith(prefix)), None)
        return line.replace(prefix, "", 1).strip() if line else ""

    rel_path = _val("FILE:")
    old_text = _val("OLD:")
    new_text = _val("NEW:")

    if not (rel_path and old_text and new_text):
        _log(f"{job}: malformed FIX block — skipping")
        return False

    if not rel_path.startswith("scripts/"):
        _log(f"{job}: FIX targets '{rel_path}' outside scripts/ — refused")
        return False

    target = ROOT / rel_path
    if not target.exists():
        _log(f"{job}: target '{rel_path}' not found")
        return False

    content = target.read_text()
    if old_text not in content:
        _log(f"{job}: OLD string not found in {rel_path}")
        return False

    target.write_text(content.replace(old_text, new_text, 1))
    _log(f"{job}: applied fix to {rel_path}: {old_text!r} → {new_text!r}")

    # Verify with pytest
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        target.write_text(content)  # revert
        _log(f"{job}: pytest failed — reverted. {r.stdout.strip()[-200:]}")
        return False

    _log(f"{job}: pytest passed — committing")
    subprocess.run(["git", "add", rel_path], cwd=ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"fix: auto-heal {job} in {rel_path}"],
        cwd=ROOT, check=True,
    )
    subprocess.run(
        ["bash", str(ROOT / "scripts" / "_git_safe_push.sh")],
        cwd=ROOT,
    )
    return True


def _load_cooldown() -> dict:
    if not COOLDOWN_STATE.exists():
        return {}
    try:
        return json.loads(COOLDOWN_STATE.read_text())
    except Exception:
        return {}


def _recently_pushed(job: str, hours: int = 6) -> bool:
    state = _load_cooldown()
    iso = state.get(job, "")
    if not iso:
        return False
    try:
        last = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return False
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return age_h < hours


def _mark_pushed(job: str) -> None:
    state = _load_cooldown()
    state[job] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    COOLDOWN_STATE.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_STATE.write_text(json.dumps(state, indent=2))


def _vapid_push(msg: str) -> None:
    """Send a push notification for errors needing human review."""
    try:
        subprocess.run(
            [sys.executable, "-m", "src.notifications.health_push",
             "auto_heal_ai", msg],
            cwd=ROOT, timeout=15,
        )
    except Exception:
        pass


_ACTION_MAP = {
    # settle_cron.sh sources _health.sh → health_finish("settle", ...) — produces snapshot.
    "re-run-settle": ["bash", "scripts/settle_cron.sh"],
    # consume_pending_bets_cron.sh sources _health.sh → health_finish("consume_pending_bets", ...).
    "re-consume": ["bash", "scripts/consume_pending_bets_cron.sh"],
    # daily_scan.py --force has no health-writing wrapper that also accepts --force.
    # scan_cron.sh omits --force (different semantics); binding is health_job=None → process_exit.
    "force-refresh-signals": ["python3", "scripts/daily_scan.py", "--force"],
    "re-test-vapid": ["python3", "-m", "src.notifications.health_push", "auto_heal_ai", "vapid-test"],
    "prompt-resubscribe": None,  # nicht autom. heilbar — direkt eskalieren
    "none": None,
}


def _run_outcome_action(action: str, sym_id: str) -> tuple[bool, str, int | None]:
    """Führt eine deterministische Heil-Action aus.

    Returns (success, stdout_tail, exit_code).
    exit_code is None when the action could not be executed (no command, exception).
    """
    cmd = _ACTION_MAP.get(action)
    if cmd is None:
        return False, f"action {action!r} hat keine ausführbare Map (eskaliert)", None
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout (>600s)", None
    except Exception as e:
        return False, f"exception: {e}", None
    tail = (proc.stdout or "")[-200:].strip()
    return proc.returncode == 0, tail, proc.returncode


def _handle_outcome_symptoms() -> None:
    """Outcome-Checks → deterministische Action → optional Push.

    Läuft immer (auch bei overall=ok), weil Outcome-Probleme jobspezifische
    Health-Status nicht spiegeln müssen.

    P0-B3: every dispatchable action is tracked through the canonical recovery
    chain (REQUESTED→DISPATCHED→OBSERVED→VERIFIED→RECOVERED). Process exit=0
    alone is never sufficient — health snapshot and symptom re-check must confirm.
    Only RecoveryState.RECOVERED may emit a recovery-success claim.
    If tracking fails, operational action may continue but recovery remains unverified.
    """
    try:
        from src.monitoring.outcome_checks import run_all_checks
    except Exception as e:
        _log(f"outcome_checks import failed: {type(e).__name__}: {e} (sys.path[0]={sys.path[0]!r})")
        return

    try:
        from src.monitoring.recovery_truth import (
            RECOVERY_REGISTRY,
            ExecutionEvidence,
            RecoveryState,
            RecoveryStore,
            VerificationEvidence,
            collect_health_snapshot_evidence,
            mark_dispatched,
            observe_execution,
            request_recovery,
            verify_resolution,
        )
        store = RecoveryStore()
        recovery_available = True
    except (ImportError, AttributeError, OSError) as e:
        _log(f"recovery_truth import failed — tracking disabled: {e}")
        recovery_available = False

    symptoms: list = run_all_checks()
    if not symptoms:
        return

    for sym in symptoms:
        _log(f"outcome-symptom: {sym.id} [{sym.severity}] → {sym.summary}")
        action = sym.suggested_action

        if action in (None, "none"):
            if not _recently_pushed(sym.id, hours=24):
                _log(f"{sym.id}: no action available — pushing")
                _vapid_push(f"{sym.id}: {sym.summary}")
                _mark_pushed(sym.id)
            continue

        if action == "prompt-resubscribe":
            if not _recently_pushed(sym.id, hours=24):
                _log(f"{sym.id}: needs human (resubscribe) — pushing")
                _vapid_push(f"{sym.id}: {sym.summary}")
                _mark_pushed(sym.id)
            continue

        # ── P0-B3 recovery chain: request + pre-dispatch snapshot baseline ───
        attempt = None
        pre_dispatch_last_run_at: str | None = None
        pre_dispatch_run_id: str | None = None
        if recovery_available:
            try:
                attempt = request_recovery(sym.id, action, symptom_id=sym.id)
                store.save(attempt)
                if attempt.state == RecoveryState.RECOVERY_UNAVAILABLE:
                    _log(f"{sym.id}: RECOVERY_UNAVAILABLE for {action!r} — {attempt.terminal_reason}")
                    # Dispatch for operational continuity; recovery cannot be claimed.
                else:
                    # Capture pre-dispatch snapshot baseline for freshness check
                    binding = RECOVERY_REGISTRY.get(action)
                    if binding and binding.health_job:
                        _baseline = collect_health_snapshot_evidence(
                            binding.health_job, attempt.requested_at
                        )
                        if _baseline:
                            pre_dispatch_last_run_at = _baseline.observed_at
                            pre_dispatch_run_id = _baseline.run_id
                    attempt = mark_dispatched(attempt)
                    store.save(attempt)
            except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
                _log(f"{sym.id}: recovery tracking error at request/dispatch: {e}")
                attempt = None

        ok, tail, exit_code = _run_outcome_action(action, sym.id)
        completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if ok:
            _log(f"auto-action: {sym.id} → ok ({action})")
        else:
            _log(f"auto-action: {sym.id} → failed ({action}): {tail[:120]}")

        # ── Observe execution (P0-B3: health snapshot required for snapshot-backed) ──
        if recovery_available and attempt is not None and attempt.state == RecoveryState.DISPATCHED:
            try:
                binding = RECOVERY_REGISTRY.get(action)
                if binding and binding.health_job:
                    exe_ev = collect_health_snapshot_evidence(
                        binding.health_job, attempt.requested_at,
                        pre_dispatch_last_run_at=pre_dispatch_last_run_at,
                        pre_dispatch_run_id=pre_dispatch_run_id,
                    )
                    if exe_ev is None:
                        # Snapshot absent after action — source=health_snapshot_absent will
                        # fail source validation in observe_execution (fail closed).
                        exe_ev = ExecutionEvidence(
                            run_id=None,
                            observed_at=completed_at,
                            exit_code=None,
                            source="health_snapshot_absent",
                            job=binding.health_job,
                        )
                else:
                    # process_exit binding (e.g., re-test-vapid, force-refresh-signals) — explicit
                    exe_ev = ExecutionEvidence(
                        run_id=None,
                        observed_at=completed_at,
                        exit_code=exit_code,
                        source="process_exit",
                        job=None,
                    )
                attempt = observe_execution(attempt, exe_ev)
                store.save(attempt)
                if attempt.state != RecoveryState.OBSERVED:
                    _log(f"{sym.id}: observe_execution failed: {attempt.terminal_reason}")
            except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
                _log(f"{sym.id}: recovery tracking error at observe: {e}")
                attempt = None

        # Re-check: ist Symptom weg? Checker exceptions/errors are NOT symptom_absent (fail closed).
        checker_failed = False
        still: list = []
        try:
            all_post_checks = run_all_checks()
            still = [s for s in all_post_checks if s.id == sym.id]
            # checker_error_* symptoms mean a check may have failed to evaluate the
            # original symptom — cannot confirm absence (P0-B3 checker-error rule).
            checker_errors = [s for s in all_post_checks if s.id.startswith("checker_error_")]
            if checker_errors:
                checker_failed = True
                _log(f"{sym.id}: checker_error during re-check (verification unavailable): "
                     f"{[e.id for e in checker_errors]}")
        except Exception as exc:
            _log(f"{sym.id}: checker exception during verification — treated as unresolved: {exc}")
            still = [sym]  # cannot confirm symptom gone — fail closed
            checker_failed = True

        # ── Verify resolution (P0-B3) ──────────────────────────────────────────
        if recovery_available and attempt is not None and attempt.state == RecoveryState.OBSERVED:
            try:
                sym_absent = len(still) == 0 and not checker_failed
                ver_ev = VerificationEvidence(
                    verified_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    symptom_absent=sym_absent,
                    detail=(
                        f"re-ran outcome checks after {action!r}; "
                        f"{'checker exception/error' if checker_failed else 'symptom absent' if sym_absent else 'symptom still present'}"
                    ),
                )
                attempt = verify_resolution(attempt, ver_ev)
                store.save(attempt)
                if attempt.state != RecoveryState.RECOVERED:
                    _log(f"{sym.id}: recovery not confirmed: {attempt.state.value} — {attempt.terminal_reason}")
            except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
                _log(f"{sym.id}: recovery tracking error at verify: {e}")

        # Only RECOVERED state may emit a recovery-success claim (P0-B3).
        if not still:
            if (recovery_available and attempt is not None
                    and attempt.state == RecoveryState.RECOVERED):
                _log(f"{sym.id}: RECOVERED ✅ (attempt={attempt.attempt_id})")
            else:
                _log(f"{sym.id}: symptom absent after action — recovery unverified")
            continue

        # Symptom hartnäckig → einmal eskalieren mit 24h Cooldown
        if _recently_pushed(sym.id, hours=24):
            _log(f"{sym.id}: persistiert, aber im 24h-Cooldown — skip push")
            continue
        _log(f"{sym.id}: persistiert nach Auto-Action — pushing")
        _vapid_push(f"{sym.id}: {sym.summary}")
        _mark_pushed(sym.id)


def main() -> None:
    # Outcome-Layer läuft IMMER — unabhängig von job-status oder API-Key.
    _handle_outcome_symptoms()

    if not _api_key():
        return  # Silent — ANTHROPIC_API_KEY not configured yet

    if not HEALTH_JSON.exists():
        return

    data = json.loads(HEALTH_JSON.read_text())
    if data.get("overall") == "ok":
        return  # Silent when healthy

    affected = [j for j in data["jobs"] if j["status"] not in ("ok",)]

    if _recently_pushed(_GLOBAL_AI_KEY, hours=_GLOBAL_AI_COOLDOWN_MINS / 60):
        _log("global AI cooldown active — skipping diagnosis this cycle")
        return

    for job_info in affected:
        job = job_info["job"]
        if job in _SKIP_JOBS:
            continue

        status = job_info["status"]
        fallback = job_info.get("fallback_used")

        tail = _log_tail(job)
        _log(f"analyzing {job} (status={status}, fallback={fallback})")

        try:
            response = _ask_claude(job, status, fallback, tail)
        except Exception as e:
            _log(f"{job}: API call failed: {e}")
            continue

        _mark_pushed(_GLOBAL_AI_KEY)  # stamp global cooldown after first call
        _log(f"{job}: response → {response[:120]}")

        if response.startswith("FIX"):
            _apply_fix(response, job)
        elif response.startswith("TRANSIENT:"):
            _log(f"{job}: transient — no action. {response}")
        elif response.startswith("DEGRADED_OK:"):
            pass  # Normal fallback — silent
        elif response.startswith("UNCLEAR:"):
            # Skip non-actionable "no log file" verdicts entirely — they spam
            # whenever a stale job hasn't written a log this hour.
            if "no log file" in response.lower():
                _log(f"{job}: unclear (no log) — skipping push")
            else:
                # Debounce: same job not more than once per 6h.
                if _recently_pushed(job, hours=6):
                    _log(f"{job}: unclear — within 6h cooldown, skipping push")
                else:
                    _log(f"{job}: unclear — pushing notification")
                    _vapid_push(f"{job}: {response}")
                    _mark_pushed(job)
        else:
            _log(f"{job}: unexpected response format — {response[:80]}")

        break  # one diagnosis per run — next job waits for next cooldown window


if __name__ == "__main__":
    main()
