"""Reads all results/health/{job}.json files and writes a consolidated
docs/data/health.json (also pushed to the Cloudflare Worker KV alongside
the existing signals.json).

Decides per-job status with a freshness check:
    last_run_at older than (interval_s + grace_s) → "stale"
    otherwise: keep the status that the job itself wrote

Overall status:
    any "error"          → "down"
    any "stale|degraded" → "degraded"
    otherwise             → "ok"

CLI: python3 -m src.monitoring.aggregate_health [--no-upload]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
HEALTH_DIR = ROOT / "results" / "health"
HEALTH_JSON_OUT = ROOT / "docs" / "data" / "health.json"

from src.monitoring.health_writer import JOB_SCHEDULE  # noqa: E402


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_stale(last_run_at: str | None, interval_s: int, grace_s: int) -> bool:
    """Returns True if last_run_at is older than (interval + grace) ago."""
    if not last_run_at:
        return True
    dt = _parse_iso(last_run_at)
    if dt is None:
        return True
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age > (interval_s + grace_s)


def _load_one(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _load_baseline() -> dict[str, dict]:
    """Reads docs/data/health.json and returns {job: entry} as fallback baseline.

    Used by --merge-from-committed so GH Actions can preserve Mac-side job
    statuses for jobs it doesn't run itself (e.g. live_score_push).
    """
    if not HEALTH_JSON_OUT.exists():
        return {}
    try:
        data = json.loads(HEALTH_JSON_OUT.read_text())
        return {e["job"]: e for e in data.get("jobs", []) if "job" in e}
    except Exception:
        return {}


def _job_entry(job: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Builds the public dashboard entry for one job, including freshness."""
    sched = JOB_SCHEDULE.get(job, {})
    interval_s = int(sched.get("interval_s", 3600))
    grace_s = int(sched.get("grace_s", 600))
    cadence = sched.get("cadence", "")

    if raw is None:
        return {
            "job":               job,
            "status":            "stale",
            "last_run_at":       None,
            "duration_s":        None,
            "exit_code":         None,
            "error":             "no health snapshot yet — job has never reported",
            "fallback_used":     None,
            "next_expected_in_s": interval_s,
            "cadence":           cadence,
        }

    status_written = raw.get("status", "stale")
    last = raw.get("last_run_at")
    stale = _is_stale(last, interval_s, grace_s)
    final_status = "stale" if stale else status_written

    # If the job wrote "ok" but it's overdue, surface that explicitly in the error.
    err = raw.get("error")
    if stale and not err:
        err = f"last run was at {last} — overdue (>{interval_s + grace_s}s)"

    return {
        "job":               job,
        "status":            final_status,
        "last_run_at":       last,
        "duration_s":        raw.get("duration_s"),
        "exit_code":         raw.get("exit_code"),
        "error":             err,
        "fallback_used":     raw.get("fallback_used"),
        "next_expected_in_s": interval_s,
        "cadence":           cadence,
    }


def _overall(jobs: list[dict[str, Any]]) -> str:
    statuses = {j["status"] for j in jobs}
    if "error" in statuses:
        return "down"
    if "stale" in statuses or "degraded" in statuses:
        return "degraded"
    return "ok"


def aggregate(merge_from_committed: bool = False) -> dict[str, Any]:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    baseline = _load_baseline() if merge_from_committed else {}
    jobs: list[dict[str, Any]] = []
    for job in JOB_SCHEDULE:
        path = HEALTH_DIR / f"{job}.json"
        if path.exists():
            raw = _load_one(path)
        elif merge_from_committed and job in baseline:
            raw = baseline[job]
        else:
            raw = None
        jobs.append(_job_entry(job, raw))

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall":      _overall(jobs),
        "jobs":         jobs,
    }
    HEALTH_JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEALTH_JSON_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, HEALTH_JSON_OUT)
    return payload


def _push_to_cloud(payload: dict[str, Any]) -> bool:
    """Adds health to the cloud signals.json so the dashboard polls one
    URL only. Worker accepts JSON via POST /signals."""
    try:
        import requests
    except ImportError:
        return False
    url = os.getenv("SIGNALS_CLOUD_URL")
    token = os.getenv("SIGNALS_API_TOKEN")
    if not url or not token:
        return False

    post_url = url[: -len("/signals.json")] + "/signals" if url.endswith("/signals.json") else url

    # Fetch current cloud signals and inject the health key, then re-post.
    # CRITICAL: If GET fails or returns a payload without core keys, do NOT
    # re-post — a bare {"health": ...} would wipe schedule/odds/model_tips
    # from the cloud KV (vorheriger Vorfall: PWA zeigte "Invalid Date" und 0
    # Spiele). Fallback auf lokale signals.json wenn vorhanden.
    current: dict[str, Any] | None = None
    try:
        get_resp = requests.get(url, timeout=10)
        if get_resp.status_code == 200:
            j = get_resp.json()
            if isinstance(j, dict) and ("schedule" in j or "updated" in j or "football" in j):
                current = j
    except Exception:
        current = None

    if current is None:
        # Fallback: lokale signals.json verwenden, statt Cloud zu nuken
        local_path = Path(__file__).resolve().parents[2] / "docs" / "data" / "signals.json"
        try:
            current = json.loads(local_path.read_text())
        except Exception:
            print("[health] cloud GET failed and no local signals.json — skip upload")
            return False

    current["health"] = payload

    try:
        post_resp = requests.post(
            post_url,
            data=json.dumps(current).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        return post_resp.status_code in (200, 201, 204)
    except Exception:
        return False


def _cli() -> int:
    p = argparse.ArgumentParser(description="Aggregate health snapshots into docs/data/health.json")
    p.add_argument("--no-upload", action="store_true",
                   help="skip Cloudflare KV upload (useful in tests)")
    p.add_argument("--merge-from-committed", action="store_true",
                   help="use docs/data/health.json as baseline for jobs without fresh snapshots "
                        "(for GitHub Actions, where results/health/ is gitignored)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    payload = aggregate(merge_from_committed=args.merge_from_committed)

    if not args.quiet:
        n_ok = sum(1 for j in payload["jobs"] if j["status"] == "ok")
        n_total = len(payload["jobs"])
        print(f"[health] overall={payload['overall']} — {n_ok}/{n_total} ok")

    if not args.no_upload:
        ok = _push_to_cloud(payload)
        if not args.quiet:
            print(f"[health] cloud upload: {'ok' if ok else 'skipped/failed'}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
