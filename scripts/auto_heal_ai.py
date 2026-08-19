"""
Layer 2 AI healer — runs every 15 min via launchd, no Claude Code needed.
Reads health.json, calls Anthropic API to DIAGNOSE code issues in scripts/.
Requires ANTHROPIC_API_KEY in .env

P0D-003 BOUNDARY:
  AI is DIAGNOSIS-ONLY. It classifies failures and escalates to human.
  It MUST NOT modify source files, git add/commit/push, create branches or PRs,
  or self-modify.

Handled:
  - Code bugs → classify CODE_ISSUE → VAPID human escalation only
  - Transient errors (network, quota) → log only
  - Fallback-active but OK (espn, stale_cache) → silent
  - Unclear errors → log + VAPID push notification

Financial jobs (consume_pending_bets, settle, closing_odds, etc.):
  → BLOCKED from external AI analysis entirely
  → log local safe summary + VAPID human escalation
  → raw financial log content never sent to Anthropic

NOT handled (scope limit):
  - Source code mutation (removed — diagnosis-only)
  - Financial subprocess execution (removed — CEO Tier 3)
  - Anything outside observation and classification
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
# Ensure `from src...` works when launched by launchd (no PYTHONPATH from shell).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HEALTH_JSON = ROOT / "docs" / "data" / "health.json"
HEAL_LOG = ROOT / "results" / "auto_heal.log"
COOLDOWN_STATE = ROOT / "results" / "health" / "auto_heal_cooldown.json"

# Jobs handled by Layer 1 bash (2-min auto-retry) — skip here
_SKIP_JOBS = {"consume_pending_bets", "live_score_push", "aggregate_health"}

# Financial jobs — logs MUST NOT be sent to external AI (P0D-003 F3).
# Also covers closing_odds since update_closing_odds.py loads/saves LEDGER_PATH.
_FINANCIAL_JOBS = frozenset({
    "consume_pending_bets",
    "settle",
    "closing_odds",
    "bundesliga2_settle",
    "tennis_settle",
    "tennis_closing_odds",
    "bundesliga2_closing_odds",
})

# Global AI-call rate limit: at most 1 Claude diagnosis per 30 min across ALL jobs.
# Prevents API spam when multiple jobs fail simultaneously.
_GLOBAL_AI_COOLDOWN_MINS = 30
_GLOBAL_AI_KEY = "__global_ai_diagnose__"

# Basic secret patterns to redact from log tails before any external API call.
_SECRET_PATTERNS = [
    re.compile(r'(sk-ant-api\S+)', re.IGNORECASE),
    re.compile(r'(Bearer\s+)\S+', re.IGNORECASE),
    re.compile(r'(token[=:\s]+)\S+', re.IGNORECASE),
    re.compile(r'(key[=:\s]+)[A-Za-z0-9_\-]{16,}', re.IGNORECASE),
    re.compile(r'(ODDS_API_KEY[=:\s]+)\S+', re.IGNORECASE),
    re.compile(r'(ANTHROPIC_API_KEY[=:\s]+)\S+', re.IGNORECASE),
    re.compile(r'(VAPID[_A-Z]*[=:\s]+)\S+', re.IGNORECASE),
]


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


def _redact_secrets(text: str) -> str:
    """Apply basic redaction of secret patterns before external API calls."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(0)[:m.start(1) - m.start(0) + len(m.group(1))] + '[REDACTED]', text)
    return text


_HAIKU = "claude-haiku-4-5-20251001"

# P0D-003: Diagnosis prompt — no FIX/replacement block format.
# AI classifies the failure and provides human-readable diagnosis.
# Code fixes are HUMAN-ONLY — AI must not produce autonomously-applicable changes.
_DIAGNOSIS_PROMPT = """\
SportsBrain cron job '{job}' has health status '{status}' (fallback_used: {fallback}).

Log tail (last 80 lines):
<log>
{log_tail}
</log>

Analyze the root cause. Reply with EXACTLY ONE of these formats:

If it's a code bug requiring a source code change (human must fix):
CODE_ISSUE: <brief diagnosis of the root cause — describe what is wrong and where, \
do NOT produce code snippets, replacement strings, or patch blocks>

If it's transient (network timeout, DNS, API quota 429, rate limit):
TRANSIENT: <brief reason>

If fallback is active but job is otherwise working (espn fallback, stale cache):
DEGRADED_OK: <brief reason>

If unclear or requires human review:
UNCLEAR: <brief reason>

Be concise. Output only the structured response above, nothing else.
Do NOT produce code blocks, diff patches, or any text that looks like a file edit."""


def _call(model: str, prompt: str, max_tokens: int = 400) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key())
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _apply_fix(response: str, job: str) -> bool:
    """P0D-003: FAIL-CLOSED STUB — code fixes are DIAGNOSIS-ONLY.

    This function previously applied and pushed source code changes autonomously.
    That capability is REMOVED (P0D-003). The healer may diagnose but MUST NOT
    mutate source files, stage/commit/push, or create branches/PRs.

    Any call with a FIX block is treated as a CODE_ISSUE and logged for human review.
    Always returns False — no code is applied.
    """
    _log(
        f"{job}: _apply_fix called — code mutation is BLOCKED (P0D-003 diagnosis-only). "
        "Human review required."
    )
    return False


def _ask_claude(job: str, status: str, fallback: str | None, log_tail: str) -> str:
    """Haiku diagnoses the failure and returns a structured classification."""
    redacted_tail = _redact_secrets(log_tail)
    prompt = _DIAGNOSIS_PROMPT.format(
        job=job, status=status, fallback=fallback, log_tail=redacted_tail
    )
    return _call(_HAIKU, prompt, max_tokens=400)


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


# P0D-003: _ACTION_MAP contains only non-financial, non-settlement actions.
# Financial actions (re-consume, re-run-settle) are REMOVED — Tier 3 require
# explicit CEO authorization and run through GH Actions financial writers only.
_ACTION_MAP = {
    # force-refresh-signals: daily_scan.py --force (non-financial, idempotent)
    "force-refresh-signals": ["python3", "scripts/daily_scan.py", "--force"],
    "re-test-vapid": ["python3", "-m", "src.notifications.health_push", "auto_heal_ai", "vapid-test"],
    "prompt-resubscribe": None,  # non-automatable — direct escalation only
    "none": None,
}


def _run_outcome_action(
    action: str, sym_id: str, *, extra_env: dict[str, str] | None = None,
) -> tuple[bool, str, int | None]:
    """Run a non-financial healer action.

    Returns (success, stdout_tail, exit_code).
    exit_code is None when the action could not be executed (no command, exception).
    """
    cmd = _ACTION_MAP.get(action)
    if cmd is None:
        return False, f"action {action!r} has no executable map (escalated)", None
    run_env = {**os.environ, **extra_env} if extra_env else None
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=600, env=run_env,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout (>600s)", None
    except Exception as e:
        return False, f"exception: {e}", None
    tail = (proc.stdout or "")[-200:].strip()
    return proc.returncode == 0, tail, proc.returncode


def _handle_outcome_symptoms() -> None:
    """Outcome-Checks → non-financial deterministic action → optional Push.

    Läuft immer (auch bei overall=ok), weil Outcome-Probleme jobspezifische
    Health-Status nicht spiegeln müssen.

    P0D-003: Financial actions (re-consume, re-run-settle) are not in _ACTION_MAP.
    Any symptom suggesting a financial action escalates to human via VAPID only.
    """
    try:
        from src.monitoring.outcome_checks import run_all_checks
    except Exception as e:
        _log(f"outcome_checks import failed: {type(e).__name__}: {e}")
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

        # P0D-003: Financial actions must never be dispatched autonomously.
        # Log + human escalation only.
        if action in ("re-consume", "re-run-settle"):
            _log(
                f"{sym.id}: suggested action {action!r} is a financial Tier-3 action — "
                "requires human authorization. Escalating."
            )
            if not _recently_pushed(sym.id, hours=24):
                _vapid_push(
                    f"{sym.id}: {sym.summary} | financial action required — "
                    "human authorization needed"
                )
                _mark_pushed(sym.id)
            continue

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

        # Check action is in the allowed non-financial map
        if action not in _ACTION_MAP or _ACTION_MAP[action] is None:
            _log(f"{sym.id}: action {action!r} not in allowed healer map — escalating")
            if not _recently_pushed(sym.id, hours=24):
                _vapid_push(f"{sym.id}: {sym.summary} | action {action!r} requires human")
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

        action_extra_env: dict[str, str] | None = None
        if (attempt is not None
                and attempt.state == RecoveryState.DISPATCHED):
            _ab = RECOVERY_REGISTRY.get(action)
            if _ab and _ab.health_job:
                action_extra_env = {"RECOVERY_ATTEMPT_ID": attempt.attempt_id}

        ok, tail, exit_code = _run_outcome_action(action, sym.id, extra_env=action_extra_env)
        completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if ok:
            _log(f"auto-action: {sym.id} → ok ({action})")
        else:
            _log(f"auto-action: {sym.id} → failed ({action}): {tail[:120]}")

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
                        exe_ev = ExecutionEvidence(
                            run_id=None,
                            observed_at=completed_at,
                            exit_code=None,
                            source="health_snapshot_absent",
                            job=binding.health_job,
                        )
                else:
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

        checker_failed = False
        still: list = []
        try:
            all_post_checks = run_all_checks()
            still = [s for s in all_post_checks if s.id == sym.id]
            checker_errors = [s for s in all_post_checks if s.id.startswith("checker_error_")]
            if checker_errors:
                checker_failed = True
                _log(f"{sym.id}: checker_error during re-check: {[e.id for e in checker_errors]}")
        except Exception as exc:
            _log(f"{sym.id}: checker exception during verification — treated as unresolved: {exc}")
            still = [sym]
            checker_failed = True

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

        if not still:
            if (recovery_available and attempt is not None
                    and attempt.state == RecoveryState.RECOVERED):
                _log(f"{sym.id}: RECOVERED ✅ (attempt={attempt.attempt_id})")
            else:
                _log(f"{sym.id}: symptom absent after action — recovery unverified")
            continue

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

        # P0D-003 F3: Financial jobs must not send raw logs to Anthropic.
        if job in _FINANCIAL_JOBS:
            _log(
                f"{job}: financial job requires human review — "
                "raw log withheld from external AI (P0D-003)"
            )
            if not _recently_pushed(job, hours=6):
                _vapid_push(
                    f"{job}: financial job {status} — "
                    "human review required (log not sent to AI)"
                )
                _mark_pushed(job)
            _mark_pushed(_GLOBAL_AI_KEY)
            break

        tail = _log_tail(job)
        _log(f"analyzing {job} (status={status}, fallback={fallback})")

        try:
            response = _ask_claude(job, status, fallback, tail)
        except Exception as e:
            _log(f"{job}: API call failed: {e}")
            continue

        _mark_pushed(_GLOBAL_AI_KEY)
        _log(f"{job}: response → {response[:120]}")

        if response.startswith("CODE_ISSUE:"):
            # P0D-003: Diagnosis-only. No code mutation. Escalate to human.
            _log(f"{job}: CODE_ISSUE identified — human review required. {response}")
            if not _recently_pushed(job, hours=6):
                _vapid_push(f"{job}: CODE_ISSUE — {response[:200]}")
                _mark_pushed(job)
        elif response.startswith("TRANSIENT:"):
            _log(f"{job}: transient — no action. {response}")
        elif response.startswith("DEGRADED_OK:"):
            pass  # Normal fallback — silent
        elif response.startswith("UNCLEAR:"):
            if "no log file" in response.lower():
                _log(f"{job}: unclear (no log) — skipping push")
            else:
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
