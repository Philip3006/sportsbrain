"""TASK-P0B-001 — MON-001 / MON-011 / OPS-006 deterministic tests.

Proves that execution health derives from execution evidence:
  - exit_code != 0 can never serialize status="ok" (MON-001)
  - malformed/unknown execution evidence fails closed (MON-001)
  - health check failure becomes visible, not silently empty-good (MON-011)
  - execution-plane provenance (launchd vs GH Actions) is not collapsed (OPS-006)
  - valid ok/degraded cases remain deterministic
  - no live network calls
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.monitoring.health_writer import _coerce_exit_code, write_health

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def health_dir(tmp_path, monkeypatch):
    """Redirect HEALTH_DIR to a temp directory for isolation."""
    import src.monitoring.health_writer as hw
    monkeypatch.setattr(hw, "HEALTH_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def agg_dirs(tmp_path, monkeypatch):
    """Redirect both HEALTH_DIR and HEALTH_JSON_OUT for aggregate tests."""
    import src.monitoring.aggregate_health as ag
    import src.monitoring.health_writer as hw

    h_dir = tmp_path / "health"
    h_dir.mkdir()
    out_dir = tmp_path / "docs" / "data"
    out_dir.mkdir(parents=True)
    out_file = out_dir / "health.json"

    monkeypatch.setattr(hw, "HEALTH_DIR", h_dir)
    monkeypatch.setattr(ag, "HEALTH_DIR", h_dir)
    monkeypatch.setattr(ag, "HEALTH_JSON_OUT", out_file)

    # Suppress cloud upload for all aggregate tests
    monkeypatch.setattr(ag, "_push_to_cloud", lambda _: False)

    return {"health_dir": h_dir, "out": out_file}


# ---------------------------------------------------------------------------
# A. Writer fail-closed: exit_code=1 with status="ok" must be coerced to error
# ---------------------------------------------------------------------------

class TestWriterMON001:
    def test_ok_with_nonzero_exit_coerced_to_error(self, health_dir):
        """MON-001: status=ok + exit_code=1 → serialized as error."""
        path = write_health("tennis_scan", "ok", exit_code=1)
        data = json.loads(path.read_text())
        assert data["status"] == "error", f"Expected error, got {data['status']}"
        assert data["exit_code"] == 1
        assert "MON-001" in (data["error"] or "")

    def test_ok_with_nonzero_exit_large_code(self, health_dir):
        """Any non-zero exit code (e.g. 127, 255) is coerced."""
        path = write_health("daily_scan", "ok", exit_code=127)
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] == 127

    # B. Normal success remains ok
    def test_normal_success_preserved(self, health_dir):
        """exit_code=0 + status=ok → serialized unchanged as ok."""
        path = write_health("tennis_scan", "ok", exit_code=0)
        data = json.loads(path.read_text())
        assert data["status"] == "ok"
        assert data["exit_code"] == 0
        assert data["error"] is None

    # C. Malformed exit evidence cannot produce success
    def test_malformed_exit_string_fails_closed(self, health_dir):
        """Non-integer exit_code string is coerced to 1 → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code="abc")  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] == 1

    def test_malformed_exit_none_with_ok_status_preserved(self, health_dir):
        """exit_code=None → _coerce_exit_code returns 1; but write_health
        defaults exit_code=0, so None is never passed in the normal API path.
        Verify the internal helper is fail-closed."""
        assert _coerce_exit_code(None) == 1
        assert _coerce_exit_code("") == 1
        assert _coerce_exit_code("xyz") == 1
        assert _coerce_exit_code([]) == 1  # type: ignore[arg-type]

    def test_malformed_exit_zero_string_parsed_correctly(self, health_dir):
        """String '0' parses as 0 — not a failure."""
        assert _coerce_exit_code("0") == 0
        assert _coerce_exit_code(0) == 0

    # F. Valid degraded behaviour remains deterministic
    def test_degraded_with_zero_exit_preserved(self, health_dir):
        """degraded + exit_code=0 must not be altered (legitimate fallback)."""
        path = write_health("settle", "degraded", exit_code=0, fallback_used="espn")
        data = json.loads(path.read_text())
        assert data["status"] == "degraded"
        assert data["exit_code"] == 0
        assert data["fallback_used"] == "espn"

    def test_error_with_nonzero_exit_preserved(self, health_dir):
        """error + exit_code=1 stays error — no double-coerce confusion."""
        path = write_health("daily_scan", "error", exit_code=1, error="something failed")
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] == 1
        assert "something failed" in (data["error"] or "")

    def test_coercion_preserves_original_error_message(self, health_dir):
        """When coercing ok→error, any original caller error message is retained."""
        path = write_health(
            "tennis_scan", "ok", exit_code=1, error="jq parse error"
        )
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert "jq parse error" in (data["error"] or "")

    # G. No live network calls — confirmed by monkeypatching and no requests import.


# ---------------------------------------------------------------------------
# D. Aggregate cannot publish impossible ok + nonzero exit from legacy snapshot
# ---------------------------------------------------------------------------

class TestAggregateMON001:
    def _write_raw_snapshot(self, health_dir: Path, job: str, payload: dict) -> None:
        (health_dir / f"{job}.json").write_text(json.dumps(payload))

    def test_aggregate_coerces_legacy_ok_nonzero(self, agg_dirs):
        """D: a pre-existing snapshot with ok+exit_code=1 must not publish ok."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan",
            "status": "ok",
            "exit_code": 1,
            "last_run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": None,
            "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] != "ok", f"Impossible ok published: {entry}"
        assert entry["status"] == "error"
        assert "MON-001" in (entry["error"] or "")

    def test_aggregate_ok_nonzero_affects_overall(self, agg_dirs):
        """An impossible snapshot must drag overall to down, not stay green."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan",
            "status": "ok",
            "exit_code": 1,
            "last_run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": None,
            "fallback_used": None,
        })
        payload = ag.aggregate()
        assert payload["overall"] in ("down", "degraded")

    def test_aggregate_valid_ok_zero_still_published_ok(self, agg_dirs):
        """D-inverse: a valid ok+exit_code=0 snapshot must remain ok."""
        from src.monitoring import aggregate_health as ag

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for job in ["tennis_scan", "tennis_retrain"]:
            self._write_raw_snapshot(agg_dirs["health_dir"], job, {
                "job": job,
                "status": "ok",
                "exit_code": 0,
                "last_run_at": now_str,
                "error": None,
                "fallback_used": None,
            })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "ok"

    def test_aggregate_legacy_exit_code_none_treated_as_zero(self, agg_dirs):
        """Legacy snapshots without exit_code field (None) + ok → remains ok.
        None means 'no execution evidence' which is distinct from nonzero."""
        from src.monitoring import aggregate_health as ag

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan",
            "status": "ok",
            "exit_code": None,
            "last_run_at": now_str,
            "error": None,
            "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        # None exit_code treated as 0 (absent evidence, not contradiction)
        assert entry["status"] == "ok"


# ---------------------------------------------------------------------------
# E. Malformed/unreadable health evidence is visible as non-green
# ---------------------------------------------------------------------------

class TestMON011Visibility:
    def test_unreadable_snapshot_surfaced_as_stale(self, agg_dirs):
        """E: a corrupt JSON file must not be silently treated as ok."""
        from src.monitoring import aggregate_health as ag

        (agg_dirs["health_dir"] / "tennis_scan.json").write_text("{{not valid json")
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        # Unreadable → _load_one returns None → _job_entry returns stale
        assert entry["status"] != "ok"
        assert entry["status"] == "stale"

    def test_missing_snapshot_surfaced_as_stale(self, agg_dirs):
        """E: a job with no snapshot at all must appear as stale, not ok."""
        from src.monitoring import aggregate_health as ag

        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "stale"
        assert entry["last_run_at"] is None

    def test_malformed_exit_string_in_snapshot_fails_closed(self, agg_dirs):
        """C+E: snapshot with exit_code='abc' + status=ok → coerced to error."""
        from src.monitoring import aggregate_health as ag

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = {
            "job": "tennis_scan",
            "status": "ok",
            "exit_code": "abc",
            "last_run_at": now_str,
            "error": None,
            "fallback_used": None,
        }
        (agg_dirs["health_dir"] / "tennis_scan.json").write_text(json.dumps(raw))
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"


# ---------------------------------------------------------------------------
# OPS-006 preservation: execution-plane provenance not collapsed
# ---------------------------------------------------------------------------

class TestOPS006Preservation:
    def test_run_id_field_preserved_in_snapshot(self, health_dir):
        """OPS-006: run_id carries plane provenance; it must survive write_health."""
        path = write_health(
            "tennis_scan", "ok", exit_code=0,
            run_id="tennis_scan-20260816T070000Z-gh-actions-12345",
        )
        data = json.loads(path.read_text())
        assert data["run_id"] == "tennis_scan-20260816T070000Z-gh-actions-12345"

    def test_local_run_id_not_confused_with_gha_run_id(self, health_dir):
        """OPS-006: launchd run_id pattern is distinct from GH Actions pattern."""
        launchd_run_id = "tennis_scan-20260816T070000Z-launchd-9812"
        path = write_health("tennis_scan", "ok", exit_code=0, run_id=launchd_run_id)
        data = json.loads(path.read_text())
        assert "launchd" in data["run_id"]
        assert data["status"] == "ok"
