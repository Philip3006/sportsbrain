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


def main() -> None:
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
            _log(f"{job}: unclear — pushing notification")
            _vapid_push(f"{job}: {response}")
        else:
            _log(f"{job}: unexpected response format — {response[:80]}")


if __name__ == "__main__":
    main()
