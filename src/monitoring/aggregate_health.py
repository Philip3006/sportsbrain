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

from src.monitoring.health_writer import _coerce_exit_code
from src.monitoring.job_schedule import (
    JOB_EXPECTATIONS,
    IntervalExpectation,
    evaluate_expectation,
    next_trigger_s,
)
from src.monitoring.release_provenance import build_provenance
from src.utils.atomic_io import atomic_write_json


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


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


def _job_entry(
    job: str,
    raw: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    from_baseline: bool = False,
) -> dict[str, Any]:
    """Builds the public dashboard entry for one job, including freshness.

    MON-002: A job not expected to run right now must not become stale merely
    because its last successful run is old.  Expectation evaluation gates the
    staleness check so off-window / already-ran-in-cycle jobs stay non-stale.

    `from_baseline=True` signals the raw dict came from a previously aggregated
    docs/data/health.json (--merge-from-committed path) rather than a direct
    job health snapshot.  This enables two behaviours:
    - B2-generated entries (contain `reported_status`): pre-schedule execution
      truth is restored from that field for a lossless round-trip.
    - Legacy pre-B2 entries (no `reported_status`): stale+exit0 is migrated to
      "degraded" so old aggregator-promoted stale does not feed back forever.

    `now` is injectable for deterministic tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    exp = JOB_EXPECTATIONS.get(job)
    if exp is None:
        # Unknown job — conservative default: simple 1h interval.
        exp = IntervalExpectation(interval_s=3600, grace_s=600, cadence="")

    cadence = exp.cadence
    next_exp_s = next_trigger_s(exp, now)

    # --- No snapshot yet ---
    if raw is None:
        last_dt: datetime | None = None
        result = evaluate_expectation(exp, last_dt, now)
        if not result.in_window:
            # Off-window and never reported — no execution occurred.
            # Cannot be "degraded" (requires confirmed execution) or "ok" (no evidence).
            # "error" + not_expected is the smallest truthful non-success representation (MON-001).
            return {
                "job":                job,
                "status":             "error",
                "last_run_at":        None,
                "duration_s":         None,
                "exit_code":          None,
                "error":              "no health snapshot yet — job has never reported",
                "fallback_used":      None,
                "next_expected_in_s": next_exp_s,
                "cadence":            cadence,
                "expectation_state":  "not_expected",
            }
        return {
            "job":                job,
            "status":             "stale",
            "last_run_at":        None,
            "duration_s":         None,
            "exit_code":          None,
            "error":              "no health snapshot yet — job has never reported",
            "fallback_used":      None,
            "next_expected_in_s": next_exp_s,
            "cadence":            cadence,
            "expectation_state":  "overdue",
        }

    # --- Snapshot exists: MON-001 defensive coercion ---
    _VALID = {"ok", "degraded", "error", "stale"}
    raw_status = raw.get("status", "stale")
    raw_exit = raw.get("exit_code")
    coerced_exit = _coerce_exit_code(raw_exit)  # None if unknown/invalid
    err = raw.get("error")

    if not isinstance(raw_status, str) or raw_status not in _VALID:
        # Unknown/non-string/malformed status — fail closed (MON-001).
        note = (
            f"[MON-001] aggregate: unknown/malformed status {raw_status!r} "
            f"(exit_code={raw_exit!r}). Treated as error."
        )
        err = (note + f" Original error: {err}") if err else note
        status_written = "error"
    else:
        status_written = raw_status

    # --- Baseline source migration (merge-from-committed path, MON-002) ---
    # Distinguish aggregate-overlay status from execution truth so old
    # false-stale promotions do not feed back permanently.
    if from_baseline:
        reported = raw.get("reported_status")
        if isinstance(reported, str) and reported in _VALID:
            # B2-generated baseline: restore pre-schedule execution truth directly.
            status_written = reported
        elif status_written == "stale" and coerced_exit == 0:
            # Legacy pre-B2 baseline: aggregator may have promoted job to stale
            # solely because last_run was old.  Conservative migration: treat as
            # degraded (confirms execution happened without asserting full ok).
            status_written = "degraded"

    if status_written in ("ok", "degraded"):
        orig_status = status_written
        if coerced_exit is None:
            status_written = "error"
            err = (
                f"[MON-001] aggregate: {orig_status} with missing/invalid exit evidence "
                f"(raw exit_code={raw_exit!r}). "
                + (f"Original error: {err}" if err else "No error field in snapshot.")
            )
        elif coerced_exit != 0:
            status_written = "error"
            err = (
                f"[MON-001] aggregate coerced {orig_status}→error from legacy snapshot: "
                f"exit_code={coerced_exit}. "
                + (f"Original error: {err}" if err else "No error field in snapshot.")
            )
    elif status_written == "stale":
        if coerced_exit is None:
            note = (
                f"[MON-001] aggregate: stale with missing/invalid exit evidence "
                f"(raw exit_code={raw_exit!r})."
            )
            err = (note + f" Original error: {err}") if err else note
        elif coerced_exit != 0:
            note = (
                f"[MON-001] aggregate: stale with execution failure: exit_code={coerced_exit}."
            )
            err = (note + f" Original error: {err}") if err else note

    # Pre-schedule execution truth — captured after MON-001 coercions so that
    # baseline round-trips can restore this value and skip re-promotion.
    reported_status_out = status_written

    # --- Expectation evaluation (MON-002) ---
    last = raw.get("last_run_at")
    last_dt = _parse_iso(last)
    result = evaluate_expectation(exp, last_dt, now)

    if not result.in_window:
        # Job correctly not expected — skip staleness promotion.
        expectation_state = "not_expected"
        final_status = status_written
    elif result.is_overdue:
        expectation_state = "overdue"
        final_status = "stale"
        if not err:
            err = f"last run was at {last} — overdue per expectation model"
    else:
        expectation_state = "expected"
        final_status = status_written

    return {
        "job":                job,
        "status":             final_status,
        "reported_status":    reported_status_out,
        "last_run_at":        last,
        "duration_s":         raw.get("duration_s"),
        "exit_code":          coerced_exit,
        "error":              err,
        "fallback_used":      raw.get("fallback_used"),
        "next_expected_in_s": next_exp_s,
        "cadence":            cadence,
        "expectation_state":  expectation_state,
    }


def _overall(jobs: list[dict[str, Any]]) -> str:
    statuses = {j["status"] for j in jobs}
    if "error" in statuses:
        return "down"
    if "stale" in statuses or "degraded" in statuses:
        return "degraded"
    return "ok"


# Data-freshness pseudo-jobs — surface stale outputs even when the producing
# job reports "ok" (2026-07-06 incident: daily_scan grün, signals.json 11h alt).
# Format: (job_name, path, max_age_s, cadence_string)
_FRESHNESS_TARGETS = [
    ("signals_data_fresh", ROOT / "docs" / "data" / "signals.json", 90 * 60, "≤90min"),
    ("live_scores_fresh",  ROOT / "docs" / "data" / "live_scores.json", 4 * 3600, "≤4h"),
]


def _freshness_entries() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for job, path, max_age_s, cadence in _FRESHNESS_TARGETS:
        if not path.exists():
            out.append({
                "job": job, "status": "error",
                "last_run_at": None, "duration_s": None, "exit_code": None,
                "error": f"{path.name} missing",
                "fallback_used": None,
                "next_expected_in_s": max_age_s, "cadence": cadence,
            })
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_s = (now - mtime).total_seconds()
        if age_s > max_age_s:
            status = "degraded"
            err = f"{path.name} is {int(age_s / 60)}min old (limit {max_age_s // 60}min)"
        else:
            status = "ok"
            err = None
        out.append({
            "job": job, "status": status,
            "last_run_at": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": None, "exit_code": None,
            "error": err, "fallback_used": None,
            "next_expected_in_s": max_age_s, "cadence": cadence,
        })
    return out


def aggregate(merge_from_committed: bool = False) -> dict[str, Any]:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    baseline = _load_baseline() if merge_from_committed else {}
    now = datetime.now(timezone.utc)
    jobs: list[dict[str, Any]] = []
    for job in JOB_EXPECTATIONS:
        path = HEALTH_DIR / f"{job}.json"
        if path.exists():
            raw = _load_one(path)
            jobs.append(_job_entry(job, raw, now=now))
        elif merge_from_committed and job in baseline:
            jobs.append(_job_entry(job, baseline[job], now=now, from_baseline=True))
        else:
            jobs.append(_job_entry(job, None, now=now))

    jobs.extend(_freshness_entries())

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "generated_at": built_at,
        "overall":      _overall(jobs),
        "jobs":         jobs,
        "provenance":   build_provenance(built_at=built_at),
    }
    atomic_write_json(HEALTH_JSON_OUT, payload, indent=2)
    return payload


def _push_to_cloud(payload: dict[str, Any]) -> bool:
    """Merge health into the cloud signals KV via Worker ?merge_health=1 POST.

    P0C-001: Uses a targeted merge-only POST instead of the former GET→inject→POST
    cycle. The Worker merges {"health": ...} into the existing KV object server-side,
    preserving all other KV fields (including private bankroll_state / open_bets that
    the Worker POST /pending-bet validation requires). This avoids the privacy cascade
    where fetching the now-public GET endpoint and re-posting would wipe private fields
    from KV.
    """
    try:
        import requests
    except ImportError:
        return False
    url = os.getenv("SIGNALS_CLOUD_URL")
    token = os.getenv("SIGNALS_API_TOKEN")
    if not url or not token:
        return False

    post_url = url[: -len("/signals.json")] + "/signals" if url.endswith("/signals.json") else url
    sep = "&" if "?" in post_url else "?"
    merge_url = f"{post_url}{sep}merge_health=1"

    try:
        post_resp = requests.post(
            merge_url,
            data=json.dumps({"health": payload}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        ok = post_resp.status_code in (200, 201, 204)
        if ok:
            print("[health] cloud upload: ok", flush=True)
        else:
            print(f"[health] cloud upload failed: {post_resp.status_code}", flush=True)
        return ok
    except Exception as e:
        print(f"[health] cloud upload exception: {e}", flush=True)
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
