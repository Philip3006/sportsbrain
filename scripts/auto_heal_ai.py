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
HEALTH_JSON = ROOT / "docs" / "data" / "health.json"
HEAL_LOG = ROOT / "results" / "auto_heal.log"
COOLDOWN_STATE = ROOT / "results" / "health" / "auto_heal_cooldown.json"

# Jobs handled by Layer 1 bash (2-min auto-retry) — skip here
_SKIP_JOBS = {"consume_pending_bets", "live_score_push", "aggregate_health"}


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
    "re-run-settle": ["python3", "scripts/settle_bets.py"],
    "re-consume": ["python3", "scripts/consume_pending_bets.py"],
    "force-refresh-signals": ["python3", "scripts/daily_scan.py", "--force"],
    "re-test-vapid": ["python3", "-m", "src.notifications.health_push", "auto_heal_ai", "vapid-test"],
    "prompt-resubscribe": None,  # nicht autom. heilbar — direkt eskalieren
    "none": None,
}


def _run_outcome_action(action: str, sym_id: str) -> tuple[bool, str]:
    """Führt eine deterministische Heil-Action aus.

    Returns (success, stdout_tail).
    """
    cmd = _ACTION_MAP.get(action)
    if cmd is None:
        return False, f"action {action!r} hat keine ausführbare Map (eskaliert)"
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout (>600s)"
    except Exception as e:
        return False, f"exception: {e}"
    tail = (proc.stdout or "")[-200:].strip()
    return proc.returncode == 0, tail


def _handle_outcome_symptoms() -> None:
    """Outcome-Checks → deterministische Action → optional Push.

    Läuft immer (auch bei overall=ok), weil Outcome-Probleme jobspezifische
    Health-Status nicht spiegeln müssen.
    """
    try:
        from src.monitoring.outcome_checks import run_all_checks, Symptom
    except Exception as e:
        _log(f"outcome_checks import failed: {e}")
        return

    symptoms: list = run_all_checks()
    if not symptoms:
        return

    for sym in symptoms:
        _log(f"outcome-symptom: {sym.id} [{sym.severity}] → {sym.summary}")
        action = sym.suggested_action

        if action in (None, "none"):
            # Direkt eskalieren
            if not _recently_pushed(sym.id, hours=24):
                _log(f"{sym.id}: no action available — pushing")
                _vapid_push(f"{sym.id}: {sym.summary}")
                _mark_pushed(sym.id)
            continue

        if action == "prompt-resubscribe":
            # Browser-Resubscribe braucht User-Eingriff — eskalieren mit 24h cooldown
            if not _recently_pushed(sym.id, hours=24):
                _log(f"{sym.id}: needs human (resubscribe) — pushing")
                _vapid_push(f"{sym.id}: {sym.summary}")
                _mark_pushed(sym.id)
            continue

        ok, tail = _run_outcome_action(action, sym.id)
        if ok:
            _log(f"auto-action: {sym.id} → ok ({action})")
        else:
            _log(f"auto-action: {sym.id} → failed ({action}): {tail[:120]}")

        # Re-check: ist Symptom weg?
        try:
            still = [s for s in run_all_checks() if s.id == sym.id]
        except Exception:
            still = []
        if not still:
            _log(f"{sym.id}: resolved nach Auto-Action ✅")
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
                continue
            # Debounce: same job not more than once per 6h.
            if _recently_pushed(job, hours=6):
                _log(f"{job}: unclear — within 6h cooldown, skipping push")
                continue
            _log(f"{job}: unclear — pushing notification")
            _vapid_push(f"{job}: {response}")
            _mark_pushed(job)
        else:
            _log(f"{job}: unexpected response format — {response[:80]}")


if __name__ == "__main__":
    main()
