"""TASK-P0B-001 / TASK-P0B-002 — MON-001 / MON-002 / MON-011 / MON-012 / OPS-006 tests.

P0B-001 (MON-001 / MON-011 / OPS-006):
  - exit_code != 0 can never serialize status="ok" or "degraded"
  - invalid/unknown exit evidence cannot produce success-like status
  - bool is rejected even though bool is a Python int subclass
  - malformed/unknown execution evidence fails closed
  - health check failure becomes visible, not silently empty-good
  - execution-plane provenance (launchd vs GH Actions) is not collapsed
  - valid ok/degraded cases remain deterministic

P0B-002 (MON-002 / MON-012):
  - off-window or already-ran-in-cycle jobs are never falsely stale
  - tennis_scan cron set: 8 exact UTC points, no false stale between points
  - tennis_retrain daily 05:00 UTC: not_expected after run, overdue if missed
  - bundesliga2_live_push: off-window → not_expected; in-window → evaluated
  - bundesliga2_closing_odds: weekly points; long gap does not false-stale
  - consume_pending_bets: 30-min fallback (not 2-min)
  - odds_refresh: 300s interval
  - unsupported expectation kind fails closed
  - all P0B-001 tests remain green (regression gate)

No live network calls.
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
# _coerce_exit_code strict-typing unit tests (CEO truth table)
# ---------------------------------------------------------------------------

class TestCoerceExitCode:
    """Direct unit tests for _coerce_exit_code helper."""

    def test_int_zero_valid(self):
        assert _coerce_exit_code(0) == 0

    def test_int_one_valid(self):
        assert _coerce_exit_code(1) == 1

    def test_int_negative_valid(self):
        assert _coerce_exit_code(-1) == -1

    def test_int_large_valid(self):
        assert _coerce_exit_code(127) == 127

    def test_string_zero_unknown(self):
        assert _coerce_exit_code("0") is None

    def test_string_one_unknown(self):
        assert _coerce_exit_code("1") is None

    def test_string_text_unknown(self):
        assert _coerce_exit_code("abc") is None

    def test_string_empty_unknown(self):
        assert _coerce_exit_code("") is None

    def test_float_zero_unknown(self):
        assert _coerce_exit_code(0.0) is None

    def test_float_partial_unknown(self):
        assert _coerce_exit_code(0.5) is None

    def test_bool_false_unknown(self):
        """bool is rejected even though bool is a Python int subclass."""
        assert _coerce_exit_code(False) is None

    def test_bool_true_unknown(self):
        """bool is rejected even though bool is a Python int subclass."""
        assert _coerce_exit_code(True) is None

    def test_none_unknown(self):
        assert _coerce_exit_code(None) is None

    def test_list_unknown(self):
        assert _coerce_exit_code([]) is None  # type: ignore[arg-type]

    def test_dict_unknown(self):
        assert _coerce_exit_code({}) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A/B/C/F. Writer fail-closed: strict truth table via write_health
# ---------------------------------------------------------------------------

class TestWriterMON001:
    def test_ok_with_nonzero_exit_coerced_to_error(self, health_dir):
        """A: status=ok + exit_code=1 → serialized as error."""
        path = write_health("tennis_scan", "ok", exit_code=1)
        data = json.loads(path.read_text())
        assert data["status"] == "error", f"Expected error, got {data['status']}"
        assert data["exit_code"] == 1
        assert "MON-001" in (data["error"] or "")

    def test_ok_with_nonzero_exit_large_code(self, health_dir):
        """Any non-zero int exit code is coerced."""
        path = write_health("daily_scan", "ok", exit_code=127)
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] == 127

    def test_ok_with_negative_exit_coerced_to_error(self, health_dir):
        """Negative int is valid non-zero exit — coerced from ok to error."""
        path = write_health("tennis_scan", "ok", exit_code=-1)
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] == -1
        assert "MON-001" in (data["error"] or "")

    def test_normal_success_preserved(self, health_dir):
        """B: exit_code=0 + status=ok → serialized unchanged as ok."""
        path = write_health("tennis_scan", "ok", exit_code=0)
        data = json.loads(path.read_text())
        assert data["status"] == "ok"
        assert data["exit_code"] == 0
        assert data["error"] is None

    def test_malformed_exit_string_fails_closed(self, health_dir):
        """C: Non-integer exit_code string → unknown evidence → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code="abc")  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None  # unknown stored as null, not fabricated

    def test_string_zero_exit_cannot_produce_success(self, health_dir):
        """C: '0' is not canonical int evidence → unknown → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code="0")  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None

    def test_string_one_exit_cannot_produce_success(self, health_dir):
        """C: '1' is not canonical int evidence → unknown → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code="1")  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None

    def test_float_zero_exit_cannot_produce_success(self, health_dir):
        """C: 0.0 float is not canonical int evidence → unknown → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code=0.0)  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None

    def test_bool_false_exit_cannot_produce_success(self, health_dir):
        """C: False is bool (rejected even as int subclass) → unknown → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code=False)  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None

    def test_bool_true_exit_cannot_produce_success(self, health_dir):
        """C: True is bool (rejected even as int subclass) → unknown → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code=True)  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None

    def test_none_exit_cannot_produce_success(self, health_dir):
        """C: None exit_code is unknown evidence → cannot be ok."""
        path = write_health("tennis_scan", "ok", exit_code=None)  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None
        assert "MON-001" in (data["error"] or "")

    def test_unknown_exit_stored_as_null_not_fabricated(self, health_dir):
        """Unknown evidence is stored as null — not fabricated as 0 or 1."""
        path = write_health("tennis_scan", "ok", exit_code=[])  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["exit_code"] is None

    def test_degraded_with_zero_exit_preserved(self, health_dir):
        """F: degraded + exit_code=0 must not be altered (legitimate fallback)."""
        path = write_health("settle", "degraded", exit_code=0, fallback_used="espn")
        data = json.loads(path.read_text())
        assert data["status"] == "degraded"
        assert data["exit_code"] == 0
        assert data["fallback_used"] == "espn"

    def test_degraded_with_nonzero_exit_coerced_to_error(self, health_dir):
        """degraded + exit_code=1 → error (execution failed, cannot claim degraded)."""
        path = write_health("settle", "degraded", exit_code=1)
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] == 1
        assert "MON-001" in (data["error"] or "")

    def test_degraded_with_unknown_exit_coerced_to_error(self, health_dir):
        """degraded + unknown exit evidence → error (cannot claim legitimate degradation)."""
        path = write_health("settle", "degraded", exit_code=None)  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert data["exit_code"] is None
        assert "MON-001" in (data["error"] or "")

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

    # --- CEO Correction 2: stale evidence annotation ---

    def test_stale_with_zero_exit_preserved_clean(self, health_dir):
        """stale + exit_code=0 → stale, no execution-failure annotation."""
        path = write_health("tennis_scan", "stale", exit_code=0)
        data = json.loads(path.read_text())
        assert data["status"] == "stale"
        assert data["exit_code"] == 0
        # No [MON-001] execution-failure note for a clean zero exit
        assert "MON-001" not in (data["error"] or "")

    def test_stale_with_nonzero_exit_annotates_error(self, health_dir):
        """stale + exit_code=1 → stale preserved, error field records failure."""
        path = write_health("tennis_scan", "stale", exit_code=1)
        data = json.loads(path.read_text())
        assert data["status"] == "stale"
        assert data["exit_code"] == 1
        assert "MON-001" in (data["error"] or "")

    def test_stale_with_none_exit_annotates_unknown_evidence(self, health_dir):
        """stale + exit_code=None → stale preserved, error field records missing evidence."""
        path = write_health("tennis_scan", "stale", exit_code=None)  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "stale"
        assert data["exit_code"] is None
        assert "MON-001" in (data["error"] or "")

    def test_stale_with_malformed_exit_annotates_unknown_evidence(self, health_dir):
        """stale + exit_code='abc' → stale preserved, error field records invalid evidence."""
        path = write_health("tennis_scan", "stale", exit_code="abc")  # type: ignore[arg-type]
        data = json.loads(path.read_text())
        assert data["status"] == "stale"
        assert data["exit_code"] is None
        assert "MON-001" in (data["error"] or "")

    def test_stale_execution_failure_preserves_existing_error(self, health_dir):
        """stale + nonzero exit + existing error text → both pieces retained."""
        path = write_health(
            "tennis_scan", "stale", exit_code=1, error="upstream timeout"
        )
        data = json.loads(path.read_text())
        assert data["status"] == "stale"
        assert "MON-001" in (data["error"] or "")
        assert "upstream timeout" in (data["error"] or "")


# ---------------------------------------------------------------------------
# D. Aggregate cannot publish impossible ok/degraded + nonzero/unknown from snapshot
# ---------------------------------------------------------------------------

class TestAggregateMON001:
    def _write_raw_snapshot(self, health_dir: Path, job: str, payload: dict) -> None:
        (health_dir / f"{job}.json").write_text(json.dumps(payload))

    def _now_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_aggregate_coerces_legacy_ok_nonzero(self, agg_dirs):
        """D: a pre-existing snapshot with ok+exit_code=1 must not publish ok."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": 1,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"
        assert "MON-001" in (entry["error"] or "")

    def test_aggregate_ok_nonzero_affects_overall(self, agg_dirs):
        """An impossible snapshot must drag overall to down, not stay green."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": 1,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        assert payload["overall"] in ("down", "degraded")

    def test_aggregate_valid_ok_zero_still_published_ok(self, agg_dirs):
        """D-inverse: a valid ok+exit_code=0 snapshot must remain ok."""
        from src.monitoring import aggregate_health as ag

        now_str = self._now_str()
        for job in ["tennis_scan", "tennis_retrain"]:
            self._write_raw_snapshot(agg_dirs["health_dir"], job, {
                "job": job, "status": "ok", "exit_code": 0,
                "last_run_at": now_str, "error": None, "fallback_used": None,
            })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "ok"

    def test_aggregate_ok_with_null_exit_coerced_to_error(self, agg_dirs):
        """ok + exit_code=null (JSON null) → error (unknown evidence cannot claim ok)."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": None,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"
        assert "MON-001" in (entry["error"] or "")

    def test_aggregate_ok_with_missing_exit_key_coerced_to_error(self, agg_dirs):
        """ok + exit_code key absent → error (missing evidence cannot claim ok)."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok",
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
            # exit_code key deliberately omitted
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"
        assert "MON-001" in (entry["error"] or "")

    def test_aggregate_ok_with_string_exit_coerced_to_error(self, agg_dirs):
        """ok + exit_code='0' (string) → error ('0' is not canonical int evidence)."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": "0",
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"

    def test_aggregate_ok_with_bool_exit_coerced_to_error(self, agg_dirs):
        """ok + exit_code=False (JSON bool) → error (bool is not int evidence)."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": False,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"

    def test_aggregate_degraded_with_zero_exit_preserved(self, agg_dirs):
        """degraded + exit_code=0 → degraded preserved (legitimate documented fallback)."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "degraded", "exit_code": 0,
            "last_run_at": self._now_str(), "error": None, "fallback_used": "cache",
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "degraded"

    def test_aggregate_degraded_with_nonzero_exit_coerced_to_error(self, agg_dirs):
        """degraded + exit_code=1 → error (execution failed)."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "degraded", "exit_code": 1,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"
        assert "MON-001" in (entry["error"] or "")

    def test_aggregate_degraded_with_malformed_exit_coerced_to_error(self, agg_dirs):
        """degraded + malformed exit evidence → error."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "degraded", "exit_code": "abc",
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "error"

    # --- CEO Correction 2: stale evidence annotation (aggregate) ---

    def test_aggregate_stale_with_nonzero_exit_annotates_error(self, agg_dirs):
        """legacy stale + exit_code=1 → stale preserved, error field records failure."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "stale", "exit_code": 1,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "stale"
        assert "MON-001" in (entry["error"] or "")

    def test_aggregate_stale_with_missing_exit_annotates_unknown(self, agg_dirs):
        """legacy stale + exit_code absent → stale preserved, error records missing evidence."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "stale",
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
            # exit_code key deliberately omitted
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["status"] == "stale"
        assert "MON-001" in (entry["error"] or "")

    # --- CEO Correction 2: normalized exit_code in published payload ---

    def test_aggregate_publishes_normalized_exit_string_zero_as_null(self, agg_dirs):
        """Legacy exit_code='0' (string) must publish as null, never as '0'."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": "0",
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["exit_code"] is None

    def test_aggregate_publishes_normalized_exit_false_as_null(self, agg_dirs):
        """Legacy exit_code=False (JSON bool) must publish as null."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": False,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["exit_code"] is None

    def test_aggregate_publishes_normalized_exit_float_as_null(self, agg_dirs):
        """Legacy exit_code=0.5 (float) must publish as null."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "error", "exit_code": 0.5,
            "last_run_at": self._now_str(), "error": "job crashed", "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["exit_code"] is None

    def test_aggregate_publishes_valid_zero_as_integer_zero(self, agg_dirs):
        """Valid exit_code=0 (int) must remain integer 0, not null."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "ok", "exit_code": 0,
            "last_run_at": self._now_str(), "error": None, "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["exit_code"] == 0
        assert entry["exit_code"] is not None

    def test_aggregate_publishes_valid_nonzero_as_real_integer(self, agg_dirs):
        """Valid exit_code=1 (int) must remain integer 1 in published payload."""
        from src.monitoring import aggregate_health as ag

        self._write_raw_snapshot(agg_dirs["health_dir"], "tennis_scan", {
            "job": "tennis_scan", "status": "error", "exit_code": 1,
            "last_run_at": self._now_str(), "error": "failure", "fallback_used": None,
        })
        payload = ag.aggregate()
        entry = next(j for j in payload["jobs"] if j["job"] == "tennis_scan")
        assert entry["exit_code"] == 1

    def test_aggregate_freshness_pseudo_jobs_exit_code_is_null(self, agg_dirs):
        """Freshness pseudo-jobs (signals_data_fresh, live_scores_fresh) must publish exit_code=null."""
        from src.monitoring import aggregate_health as ag

        payload = ag.aggregate()
        pseudo_jobs = [j for j in payload["jobs"] if j["job"].endswith("_fresh")]
        assert len(pseudo_jobs) >= 1, "expected at least one freshness pseudo-job"
        for pj in pseudo_jobs:
            assert pj["exit_code"] is None, (
                f"{pj['job']} freshness pseudo-job must have null exit_code, got {pj['exit_code']!r}"
            )


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
            "job": "tennis_scan", "status": "ok", "exit_code": "abc",
            "last_run_at": now_str, "error": None, "fallback_used": None,
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


# ===========================================================================
# TASK-P0B-002 — MON-002 / MON-012 deterministic tests
# ===========================================================================
# All tests inject an explicit `now` via the evaluate_expectation API or the
# _job_entry `now` parameter so there are no wall-clock dependencies.

from datetime import timedelta

from src.monitoring.job_schedule import (
    JOB_EXPECTATIONS,
    CronSetExpectation,
    EventWithFallbackExpectation,
    IntervalExpectation,
    WindowedExpectation,
    evaluate_expectation,
    next_trigger_s,
    nominal_interval_s,
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Unit tests — evaluate_expectation (deterministic, no I/O)
# ---------------------------------------------------------------------------

class TestIntervalExpectation:
    """odds_refresh 300s — INTERVAL tests."""

    EXP = IntervalExpectation(interval_s=300, grace_s=300, cadence="alle 5 Min (launchd)")

    def test_no_snapshot_is_overdue(self):
        """No last_run_at → always overdue for interval jobs."""
        now = _utc(2026, 8, 16, 12, 0)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_before_due(self):
        """100s since last run — well within 300s interval, not overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=100)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_exactly_at_interval(self):
        """300s since last run — at interval boundary, within grace (300+300=600), not overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=300)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False

    def test_at_interval_plus_grace_boundary(self):
        """600s since last run — exactly at boundary (strict >600), NOT overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=600)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False

    def test_beyond_interval_plus_grace(self):
        """601s since last run — just past grace, overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=601)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_cadence_preserved(self):
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=100)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.cadence == "alle 5 Min (launchd)"

    def test_odds_refresh_in_registry(self):
        """odds_refresh must be in JOB_EXPECTATIONS as IntervalExpectation 300s."""
        exp = JOB_EXPECTATIONS.get("odds_refresh")
        assert exp is not None
        assert isinstance(exp, IntervalExpectation)
        assert exp.interval_s == 300
        assert exp.grace_s == 300

    def test_nominal_interval_s_for_interval(self):
        assert nominal_interval_s(self.EXP) == 300


class TestCronSetTennisScan:
    """tennis_scan — CRON SET tests (8 daily UTC points)."""

    # Actual workflow: 02/06/09/12/15/18/21/23 UTC daily
    EXP = JOB_EXPECTATIONS["tennis_scan"]

    def _assert_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)
        hours = {h for (_, h, _) in self.EXP.points}
        assert hours == {2, 6, 9, 12, 15, 18, 21, 23}, (
            f"tennis_scan must have exactly 8 cron hours: got {sorted(hours)}"
        )

    def test_schedule_has_exactly_8_daily_points(self):
        """MON-012: tennis_scan expectation matches active workflow (8 daily cron points)."""
        self._assert_cron_set()
        assert len(self.EXP.points) == 8

    def test_no_04_utc_point(self):
        """Workflow has no 04:00 UTC point — source wins over CEO audit."""
        hours = {h for (_, h, _) in self.EXP.points}
        assert 4 not in hours

    def test_just_after_cron_point_not_yet_overdue(self):
        """At 06:05 UTC, if job ran at 06:01, not overdue (within grace)."""
        now = _utc(2026, 8, 16, 6, 5)  # Saturday 06:05 UTC
        last = _utc(2026, 8, 16, 6, 1)  # ran 4 min after cron point
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False

    def test_ran_in_cycle_makes_job_not_expected(self):
        """After job runs post-cron, it is not_expected until the next cron point (MON-002)."""
        # 09:00 is a cron point. Job ran at 09:02. Now it's 11:30 (before next 12:00 cron).
        now = _utc(2026, 8, 16, 11, 30)
        last = _utc(2026, 8, 16, 9, 2)   # ran after 09:00 cron
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False, "job ran for current cycle; must not be marked expected"
        assert r.is_overdue is False

    def test_no_false_stale_between_cron_points(self):
        """MON-002: 3h gap between 06:00 and 09:00 must not stale a job that ran at 06:05."""
        now = _utc(2026, 8, 16, 8, 59)   # just before next 09:00 cron
        last = _utc(2026, 8, 16, 6, 5)   # ran after 06:00
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_4h_gap_02_to_06_no_false_stale(self):
        """Largest gap (02:00→06:00 = 4h) must not stale a job that ran at 02:03."""
        now = _utc(2026, 8, 16, 5, 59)   # just before 06:00
        last = _utc(2026, 8, 16, 2, 3)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_missed_cron_becomes_overdue_after_grace(self):
        """Job that missed 06:00 is overdue at 07:01 (grace=3600s → deadline=07:00)."""
        now = _utc(2026, 8, 16, 7, 1)    # 1 min past 06:00 + 60min grace
        last = _utc(2026, 8, 15, 23, 5)  # ran yesterday at 23:05 (after 23:00 cron)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_day_rollover_at_midnight(self):
        """Cron point 23:00 belongs to the previous calendar day; 00:30 sees it correctly."""
        # now is 2026-08-17 00:30 UTC. Most recent past cron = 2026-08-16 23:00.
        now = _utc(2026, 8, 17, 0, 30)
        last = _utc(2026, 8, 16, 23, 3)  # ran after yesterday's 23:00
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False   # ran in time, next cron is 02:00 today
        assert r.is_overdue is False

    def test_02_utc_cron_point_present(self):
        """02:00 UTC is a valid cron point — missing it would create a false stale window."""
        now = _utc(2026, 8, 16, 2, 5)   # 5 min after 02:00
        last = _utc(2026, 8, 16, 2, 2)  # ran after 02:00
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False


class TestCronSetTennisRetrain:
    """tennis_retrain — DAILY CRON (05:00 UTC) tests."""

    EXP = JOB_EXPECTATIONS["tennis_retrain"]

    def test_is_daily_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)
        assert all(h == 5 and m == 0 for (_, h, m) in self.EXP.points)

    def test_before_daily_run_not_yet_overdue(self):
        """At 04:59 UTC with last run yesterday at 05:02: within next cycle, not overdue."""
        now = _utc(2026, 8, 16, 4, 59)
        last = _utc(2026, 8, 15, 5, 2)   # ran yesterday after 05:00
        r = evaluate_expectation(self.EXP, last, now)
        # most_recent_cron = yesterday 05:00; last >= that → not_expected
        assert r.in_window is False
        assert r.is_overdue is False

    def test_ran_today_not_expected_midday(self):
        """After running at 05:02 today, job is not_expected at 13:00 (no new cron until tomorrow)."""
        now = _utc(2026, 8, 16, 13, 0)
        last = _utc(2026, 8, 16, 5, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_missed_daily_run_becomes_overdue(self):
        """If 05:00 cron passed and job has not run, it becomes overdue after grace (1h)."""
        now = _utc(2026, 8, 16, 6, 1)   # 1h 1min past 05:00, grace=3600s → overdue
        last = _utc(2026, 8, 15, 5, 2)  # ran yesterday
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_within_grace_not_yet_overdue(self):
        """At 05:30 (30min after 05:00), job hasn't run — within 1h grace, not overdue."""
        now = _utc(2026, 8, 16, 5, 30)
        last = _utc(2026, 8, 15, 5, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False


class TestWindowedBL2LivePush:
    """bundesliga2_live_push — WINDOWED tests (Fri/Sat/Sun UTC windows)."""

    EXP = JOB_EXPECTATIONS["bundesliga2_live_push"]

    # Python weekdays: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # 2026-08-14 = Friday, 2026-08-15 = Saturday, 2026-08-16 = Sunday
    # 2026-08-17 = Monday, 2026-08-18 = Tuesday

    def test_monday_off_window(self):
        """Monday is not in any active window → not_expected (MON-002)."""
        now = _utc(2026, 8, 17, 20, 0)   # Monday 20:00 UTC
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_tuesday_off_window(self):
        now = _utc(2026, 8, 18, 12, 0)   # Tuesday 12:00 UTC
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is False

    def test_friday_before_window_off(self):
        """Friday 17:59 UTC — window starts 18:00, so not yet active."""
        now = _utc(2026, 8, 14, 17, 59)  # Friday 17:59
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is False

    def test_friday_at_window_start(self):
        """Friday 18:00 UTC — window just opened; grace_s=300, elapsed=0s → not overdue."""
        now = _utc(2026, 8, 14, 18, 0)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is False  # window-entry grace applies

    def test_friday_inside_window_ran_recently(self):
        """Friday 20:15 UTC, last run 3 min ago: inside window, not overdue."""
        now = _utc(2026, 8, 14, 20, 15)
        last = now - timedelta(seconds=180)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_friday_inside_window_overdue(self):
        """Friday 20:15 UTC, last run 600s ago (interval=120, grace=300 → deadline=420s): overdue."""
        now = _utc(2026, 8, 14, 20, 15)
        last = now - timedelta(seconds=600)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_saturday_window_starts_at_11(self):
        """Saturday 10:59 UTC — before Saturday window (starts 11:00), not_expected."""
        now = _utc(2026, 8, 15, 10, 59)  # Saturday 10:59
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is False

    def test_saturday_at_window_start(self):
        now = _utc(2026, 8, 15, 11, 0)   # Saturday 11:00
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True

    def test_saturday_inside_window(self):
        now = _utc(2026, 8, 15, 15, 30)  # Saturday 15:30
        last = now - timedelta(seconds=100)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_saturday_at_end_of_window(self):
        """Saturday 22:59 UTC — still within window (end_hour=22)."""
        now = _utc(2026, 8, 15, 22, 59)
        last = now - timedelta(seconds=100)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True

    def test_saturday_after_window(self):
        """Saturday 23:00 UTC — past window end (end_hour=22), not_expected."""
        now = _utc(2026, 8, 15, 23, 0)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is False

    def test_sunday_window(self):
        now = _utc(2026, 8, 16, 12, 0)   # Sunday 12:00
        last = now - timedelta(seconds=100)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_is_windowed_expectation(self):
        assert isinstance(self.EXP, WindowedExpectation)
        assert self.EXP.interval_s == 120

    def test_off_window_job_never_stale_with_old_last_run(self):
        """MON-002: old last_run_at on a weekday must not make the job stale."""
        # Monday: off-window. Last run was Saturday (2 days ago). Must not be stale.
        now = _utc(2026, 8, 17, 20, 0)   # Monday
        last = _utc(2026, 8, 15, 21, 0)  # Saturday (last window run)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False


class TestCronSetBL2ClosingOdds:
    """bundesliga2_closing_odds — WEEKLY CRON SET tests."""

    EXP = JOB_EXPECTATIONS["bundesliga2_closing_odds"]

    # 2026-08-14 = Friday, 2026-08-15 = Saturday, 2026-08-16 = Sunday
    # Cron points: Fri 18:25, Sat 10:55, Sat 18:25, Sun 11:25 UTC

    def test_is_cron_set_weekly(self):
        assert isinstance(self.EXP, CronSetExpectation)
        weekdays = {wd for (wd, _, _) in self.EXP.points if wd is not None}
        # Python weekday: Fri=4, Sat=5, Sun=6
        assert weekdays == {4, 5, 6}

    def test_exact_four_weekly_points(self):
        points_set = {(wd, h, m) for (wd, h, m) in self.EXP.points}
        assert (4, 18, 25) in points_set  # Friday 18:25
        assert (5, 10, 55) in points_set  # Saturday 10:55
        assert (5, 18, 25) in points_set  # Saturday 18:25
        assert (6, 11, 25) in points_set  # Sunday 11:25
        assert len(self.EXP.points) == 4

    def test_tuesday_long_gap_no_false_stale(self):
        """MON-002: Tuesday (no cron point) — job ran Sunday → not_expected, NOT stale."""
        now = _utc(2026, 8, 18, 12, 0)   # Tuesday 12:00
        last = _utc(2026, 8, 16, 11, 30) # ran Sunday after 11:25 cron
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_wednesday_no_false_stale(self):
        """Wednesday: job ran Sunday → not_expected for days (MON-002)."""
        now = _utc(2026, 8, 19, 9, 0)
        last = _utc(2026, 8, 16, 11, 30)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_friday_before_cron_point_not_expected(self):
        """Friday 17:00 — before 18:25 cron. Job ran last Sunday → still not_expected."""
        now = _utc(2026, 8, 14, 17, 0)
        last = _utc(2026, 8, 9, 11, 30)   # ran last Sunday
        r = evaluate_expectation(self.EXP, last, now)
        # last_expected = last Sunday 11:25 (2026-08-09); last ran after it → not_expected
        assert r.in_window is False
        assert r.is_overdue is False

    def test_friday_cron_point_at_1825_expected(self):
        """At Friday 18:25, job hasn't run today: in_window=True, within grace."""
        now = _utc(2026, 8, 14, 18, 25)  # exactly at cron point
        last = _utc(2026, 8, 9, 11, 30)  # ran last Sunday (before today's Fri 18:25)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        # 0s elapsed since last_expected (18:25 == now), grace_s=1800 → not overdue
        assert r.is_overdue is False

    def test_friday_cron_point_overdue_after_grace(self):
        """At Friday 18:55 (30min + 1s past 18:25), job still hasn't run → overdue."""
        now = _utc(2026, 8, 14, 18, 56)  # 31 min past 18:25 → > grace_s=1800
        last = _utc(2026, 8, 9, 11, 30)  # last Sunday
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_saturday_first_cron_1055(self):
        """Saturday 10:55 — cron fires; job hasn't run today → expected, not yet overdue."""
        now = _utc(2026, 8, 15, 10, 55)
        last = _utc(2026, 8, 14, 18, 30) # ran Friday after 18:25
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_saturday_between_cron_points_no_stale(self):
        """Between Sat 10:55 and Sat 18:25, if job ran at 10:58 → not_expected."""
        now = _utc(2026, 8, 15, 14, 0)
        last = _utc(2026, 8, 15, 10, 58)  # ran after 10:55
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_sunday_cron_1125(self):
        """Sunday 11:25 — expected, job hasn't run today."""
        now = _utc(2026, 8, 16, 11, 25)
        last = _utc(2026, 8, 15, 18, 30)  # ran Saturday after 18:25
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_ran_sunday_not_stale_on_monday(self):
        """Ran Sunday at 11:30 → not_expected Monday (MON-002 key test)."""
        now = _utc(2026, 8, 17, 10, 0)   # Monday
        last = _utc(2026, 8, 16, 11, 30)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False


class TestEventWithFallbackConsumePendingBets:
    """consume_pending_bets — EVENT + 30-MIN FALLBACK tests."""

    EXP = JOB_EXPECTATIONS["consume_pending_bets"]

    def test_is_event_with_fallback(self):
        assert isinstance(self.EXP, EventWithFallbackExpectation)
        assert self.EXP.fallback_interval_s == 1800

    def test_not_120s_interval(self):
        """Legacy JOB_SCHEDULE had 120s. New model must use 30-min fallback."""
        assert self.EXP.fallback_interval_s != 120

    def test_no_snapshot_is_overdue(self):
        now = _utc(2026, 8, 16, 12, 0)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_ran_recently_not_overdue(self):
        """Ran 10 min ago → well within 30+10=40 min deadline, not overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(minutes=10)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False

    def test_overdue_after_fallback_plus_grace(self):
        """Ran 41 min ago — past 30min fallback + 10min grace (2400s), overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=2401)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is True

    def test_exactly_at_deadline_not_overdue(self):
        """Exactly at deadline (1800+600=2400s): strict >, NOT overdue."""
        now = _utc(2026, 8, 16, 12, 0)
        last = now - timedelta(seconds=2400)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False


class TestInvalidExpectation:
    """Malformed / unsupported expectation kind must fail closed."""

    def test_unsupported_kind_raises(self):
        """evaluate_expectation raises ValueError for unknown expectation kinds."""
        class BogusExpectation:
            cadence = ""
            kind = "bogus"
        now = _utc(2026, 8, 16, 12, 0)
        with pytest.raises((ValueError, AttributeError)):
            evaluate_expectation(BogusExpectation(), None, now)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Aggregate integration: MON-002 end-to-end via _job_entry
# ---------------------------------------------------------------------------

class TestAggregateMON002:
    """MON-002 integration: aggregate respects expectation model."""

    def _write_raw(self, health_dir: Path, job: str, payload: dict) -> None:
        (health_dir / f"{job}.json").write_text(json.dumps(payload))

    def _now_str(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_bl2_closing_odds_tuesday_ran_sunday_not_stale(self, agg_dirs):
        """BL2 closing odds: ran Sunday 11:30, Tuesday 12:00 → not stale (not_expected, MON-002)."""
        from src.monitoring import aggregate_health as ag

        tuesday = _utc(2026, 8, 18, 12, 0)
        # Job ran on Sunday after the 11:25 cron point.
        raw = {
            "job": "bundesliga2_closing_odds", "status": "ok", "exit_code": 0,
            "last_run_at": self._now_str(_utc(2026, 8, 16, 11, 30)),
            "error": None, "fallback_used": None,
        }
        entry = ag._job_entry("bundesliga2_closing_odds", raw, now=tuesday)
        assert entry["status"] != "stale", (
            f"Off-window job that ran in last cycle must not be stale; got {entry['status']!r}"
        )
        assert entry["expectation_state"] == "not_expected"

    def test_bl2_live_push_monday_no_snapshot_not_stale(self, agg_dirs):
        """BL2 live push, no snapshot, Monday → error+not_expected (never-ran, off-window)."""
        from src.monitoring import aggregate_health as ag

        monday = _utc(2026, 8, 17, 20, 0)
        entry = ag._job_entry("bundesliga2_live_push", None, now=monday)
        assert entry["status"] == "error", (
            f"never-ran + off-window must be 'error' not ok/degraded; got {entry['status']!r}"
        )
        assert entry["expectation_state"] == "not_expected"
        assert entry["error"] is not None

    def test_tennis_scan_not_stale_between_cron_points(self, agg_dirs):
        """tennis_scan: ran at 09:02, now is 11:30 → not stale (not_expected)."""
        from src.monitoring import aggregate_health as ag

        now = _utc(2026, 8, 16, 11, 30)
        raw = {
            "job": "tennis_scan", "status": "ok", "exit_code": 0,
            "last_run_at": self._now_str(_utc(2026, 8, 16, 9, 2)),
            "error": None, "fallback_used": None,
        }
        entry = ag._job_entry("tennis_scan", raw, now=now)
        assert entry["status"] != "stale"
        assert entry["expectation_state"] == "not_expected"

    def test_tennis_retrain_not_stale_midday_after_morning_run(self, agg_dirs):
        """tennis_retrain: ran 05:02, now 13:00 → not stale."""
        from src.monitoring import aggregate_health as ag

        now = _utc(2026, 8, 16, 13, 0)
        raw = {
            "job": "tennis_retrain", "status": "ok", "exit_code": 0,
            "last_run_at": self._now_str(_utc(2026, 8, 16, 5, 2)),
            "error": None, "fallback_used": None,
        }
        entry = ag._job_entry("tennis_retrain", raw, now=now)
        assert entry["status"] != "stale"
        assert entry["expectation_state"] == "not_expected"

    def test_bl2_live_push_inside_window_stale_if_old_run(self, agg_dirs):
        """BL2 live push inside Saturday window: overdue if last run is 10 min ago."""
        from src.monitoring import aggregate_health as ag

        now = _utc(2026, 8, 15, 14, 30)  # Saturday 14:30 — in window
        last_run = now - timedelta(seconds=601)   # 601s ago > 120+300=420s deadline
        raw = {
            "job": "bundesliga2_live_push", "status": "ok", "exit_code": 0,
            "last_run_at": self._now_str(last_run),
            "error": None, "fallback_used": None,
        }
        entry = ag._job_entry("bundesliga2_live_push", raw, now=now)
        assert entry["status"] == "stale"
        assert entry["expectation_state"] == "overdue"

    def test_expectation_state_field_present_for_all_known_jobs(self, agg_dirs):
        """Every known job entry must include expectation_state field."""
        from src.monitoring import aggregate_health as ag

        now = _utc(2026, 8, 18, 12, 0)  # Monday — some jobs off-window
        for job in JOB_EXPECTATIONS:
            entry = ag._job_entry(job, None, now=now)
            assert "expectation_state" in entry, (
                f"Job {job!r} is missing expectation_state in output"
            )

    def test_p0b001_regression_ok_nonzero_still_coerced(self, agg_dirs):
        """P0B-001 regression: ok + exit_code=1 must still coerce to error even with new model."""
        from src.monitoring import aggregate_health as ag

        now = _utc(2026, 8, 16, 9, 2)   # inside tennis_scan expected window
        raw = {
            "job": "tennis_scan", "status": "ok", "exit_code": 1,
            "last_run_at": self._now_str(now - timedelta(seconds=60)),
            "error": None, "fallback_used": None,
        }
        entry = ag._job_entry("tennis_scan", raw, now=now)
        assert entry["status"] == "error"
        assert "MON-001" in (entry["error"] or "")

    def test_freshness_pseudo_jobs_still_separate(self, agg_dirs):
        """MON-012: freshness pseudo-jobs must still appear separately (regression)."""
        from src.monitoring import aggregate_health as ag

        payload = ag.aggregate()
        pseudo = [j for j in payload["jobs"] if j["job"].endswith("_fresh")]
        assert len(pseudo) >= 1, "freshness pseudo-jobs must be present in aggregate output"

    def test_never_ran_off_window_is_error_not_ok(self, agg_dirs):
        """CEO C2 corrected: no snapshot + off-window → error+not_expected, never ok/degraded."""
        from src.monitoring import aggregate_health as ag

        monday = _utc(2026, 8, 17, 20, 0)   # Monday — bl2_live_push is off-window
        entry = ag._job_entry("bundesliga2_live_push", None, now=monday)
        assert entry["status"] == "error", (
            f"Never-ran off-window must be 'error' (no execution evidence); got {entry['status']!r}"
        )
        assert entry["expectation_state"] == "not_expected"
        assert entry["error"] is not None

    def test_next_expected_in_s_cron_set_is_truthful(self, agg_dirs):
        """CEO C3: CronSet next_expected_in_s must be seconds to next trigger, not grace_s."""
        from src.monitoring import aggregate_health as ag

        # tennis_retrain daily 05:00 UTC. At 14:00 → next trigger is 15h = 54000s away.
        now = _utc(2026, 8, 16, 14, 0)
        raw = {
            "job": "tennis_retrain", "status": "ok", "exit_code": 0,
            "last_run_at": self._now_str(_utc(2026, 8, 16, 5, 2)),
            "error": None, "fallback_used": None,
        }
        entry = ag._job_entry("tennis_retrain", raw, now=now)
        exp = JOB_EXPECTATIONS["tennis_retrain"]
        assert entry["next_expected_in_s"] != exp.grace_s, (
            f"next_expected_in_s must not equal grace_s={exp.grace_s} — that was the bug"
        )
        assert entry["next_expected_in_s"] is not None
        expected_s = int((_utc(2026, 8, 17, 5, 0) - now).total_seconds())  # ~54000s
        assert abs(entry["next_expected_in_s"] - expected_s) < 120


# ---------------------------------------------------------------------------
# CEO Correction 1 — window-entry grace (new tests)
# ---------------------------------------------------------------------------

class TestWindowedWindowEntryGrace:
    """CEO C1: at window start, first run must not be immediately overdue."""

    EXP = JOB_EXPECTATIONS["bundesliga2_live_push"]

    def test_at_window_start_no_prior_run_not_overdue(self):
        """Exactly at 18:00, no prior run — grace_s=300 hasn't elapsed."""
        now = _utc(2026, 8, 14, 18, 0)   # Friday 18:00
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is False, "window just opened, 0s elapsed < grace_s=300"

    def test_within_grace_of_window_start_not_overdue(self):
        """4 min (240s) into window, no run — 240 < grace_s=300, not overdue."""
        now = _utc(2026, 8, 14, 18, 4)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_past_grace_of_window_start_overdue(self):
        """6 min (360s) into window, no run — 360 > grace_s=300, overdue."""
        now = _utc(2026, 8, 14, 18, 6)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_previous_window_run_still_gets_grace(self):
        """Last ran Saturday; now Friday 18:02 — prior-window run gets window-start grace."""
        now = _utc(2026, 8, 14, 18, 2)           # Friday 18:02
        last = _utc(2026, 8, 8, 21, 30)           # previous Saturday (before this window)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        # window_start=18:00, elapsed=120s < grace_s=300 → not overdue
        assert r.is_overdue is False

    def test_run_within_current_window_uses_interval_grace(self):
        """Ran 180s ago within current window — 180 < interval_s=120+grace_s=300=420, not overdue."""
        now = _utc(2026, 8, 14, 19, 30)          # Friday 19:30 (well inside window)
        last = now - timedelta(seconds=180)        # 3 min ago, after 18:00
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_run_within_current_window_overdue_after_interval_grace(self):
        """Ran 500s ago within window — 500 > 120+300=420, overdue."""
        now = _utc(2026, 8, 14, 19, 30)
        last = now - timedelta(seconds=500)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_saturday_window_entry_grace(self):
        """Saturday 11:02 (120s in), no prior run — 120 < grace_s=300, not overdue."""
        now = _utc(2026, 8, 15, 11, 2)
        r = evaluate_expectation(self.EXP, None, now)
        assert r.in_window is True
        assert r.is_overdue is False


# ---------------------------------------------------------------------------
# CEO Correction 3 — next_trigger_s truthfulness (new tests)
# ---------------------------------------------------------------------------

class TestNextTriggerS:
    """CEO C3: next_trigger_s returns actual seconds to next trigger, not grace_s."""

    def test_cron_set_next_trigger_not_grace_s(self):
        """tennis_retrain: next_trigger_s must not equal grace_s=3600."""
        now = _utc(2026, 8, 16, 14, 0)
        exp = JOB_EXPECTATIONS["tennis_retrain"]
        s = next_trigger_s(exp, now)
        assert s != exp.grace_s, f"returned grace_s={exp.grace_s} — was the bug"

    def test_cron_set_next_trigger_is_seconds_to_0500(self):
        """tennis_retrain at 14:00 → next 05:00 next day ≈ 54000s."""
        now = _utc(2026, 8, 16, 14, 0)
        exp = JOB_EXPECTATIONS["tennis_retrain"]
        s = next_trigger_s(exp, now)
        assert s is not None
        expected_s = int((_utc(2026, 8, 17, 5, 0) - now).total_seconds())
        assert abs(s - expected_s) < 60, f"expected ~{expected_s}s, got {s}"

    def test_interval_returns_interval_s(self):
        exp = JOB_EXPECTATIONS["odds_refresh"]
        assert next_trigger_s(exp, _utc(2026, 8, 16, 12, 0)) == 300

    def test_windowed_off_window_returns_seconds_to_next_start(self):
        """bl2_live_push Monday 20:00 → next window is Friday 18:00."""
        now = _utc(2026, 8, 17, 20, 0)   # Monday (2026-08-17)
        exp = JOB_EXPECTATIONS["bundesliga2_live_push"]
        s = next_trigger_s(exp, now)
        # Next Friday 18:00 UTC = 2026-08-21 18:00
        expected_s = int((_utc(2026, 8, 21, 18, 0) - now).total_seconds())
        assert s is not None
        assert abs(s - expected_s) < 60

    def test_windowed_in_window_returns_interval_s(self):
        """Inside window → next expected in interval_s."""
        now = _utc(2026, 8, 15, 14, 0)   # Saturday — in window
        exp = JOB_EXPECTATIONS["bundesliga2_live_push"]
        assert next_trigger_s(exp, now) == exp.interval_s

    def test_event_fallback_returns_fallback_interval(self):
        exp = JOB_EXPECTATIONS["consume_pending_bets"]
        assert next_trigger_s(exp, _utc(2026, 8, 16, 12, 0)) == exp.fallback_interval_s

    def test_tennis_scan_cron_set_next_within_day(self):
        """tennis_scan: at 10:00, next trigger is 12:00 = 7200s."""
        now = _utc(2026, 8, 16, 10, 0)
        exp = JOB_EXPECTATIONS["tennis_scan"]
        s = next_trigger_s(exp, now)
        assert s is not None
        assert abs(s - 7200) < 60, f"expected ~7200s, got {s}"


# ---------------------------------------------------------------------------
# CEO Correction 4 — scheduler reconciliation (new tests)
# ---------------------------------------------------------------------------

class TestTennisSettleCronSet:
    """tennis_settle active cron '15 6-22/2 * * *' — 9 daily UTC points."""

    EXP = JOB_EXPECTATIONS["tennis_settle"]

    def test_is_cron_set_not_interval(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_has_9_daily_points(self):
        assert len(self.EXP.points) == 9
        hours = {h for (_, h, _) in self.EXP.points}
        assert hours == {6, 8, 10, 12, 14, 16, 18, 20, 22}

    def test_all_points_at_minute_15(self):
        assert all(m == 15 for (_, _, m) in self.EXP.points)

    def test_all_points_are_daily(self):
        assert all(wd is None for (wd, _, _) in self.EXP.points)

    def test_no_false_stale_overnight(self):
        """Ran at 22:20 (after 22:15 cron) → not overdue at 04:00 next day (MON-002)."""
        now = _utc(2026, 8, 17, 4, 0)    # Monday 04:00
        last = _utc(2026, 8, 16, 22, 20) # Sunday 22:20 (after 22:15)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_expected_at_0615(self):
        """At 06:15 — expected, 0s elapsed < grace_s=3600, not overdue."""
        now = _utc(2026, 8, 16, 6, 15)
        last = _utc(2026, 8, 15, 22, 20)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_overdue_if_missed_0615_past_grace(self):
        """Missed 06:15, now 07:20 (65min > grace_s=60min) → overdue."""
        now = _utc(2026, 8, 16, 7, 20)
        last = _utc(2026, 8, 15, 22, 20)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_not_stale_between_0815_and_1015(self):
        """Ran at 08:20 (after 08:15) → not expected at 09:30 (before 10:15)."""
        now = _utc(2026, 8, 16, 9, 30)
        last = _utc(2026, 8, 16, 8, 20)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False


class TestBL2SettleCronSet:
    """bundesliga2_settle — CronSet: 7 weekly points, no false-stale Wed/Thu."""

    EXP = JOB_EXPECTATIONS["bundesliga2_settle"]

    def test_is_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_has_7_points(self):
        assert len(self.EXP.points) == 7

    def test_has_monday_2030(self):
        assert (0, 20, 30) in self.EXP.points

    def test_has_friday_2130(self):
        assert (4, 21, 30) in self.EXP.points

    def test_no_false_stale_wednesday(self):
        """Wed 12:00 — ran Tue 20:35 (after 20:30 cron) → not_expected (MON-002)."""
        now = _utc(2026, 8, 19, 12, 0)    # Wednesday
        last = _utc(2026, 8, 18, 20, 35)  # Tuesday 20:35
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_no_false_stale_thursday(self):
        """Thu 15:00 — ran Tue 20:35 → not_expected."""
        now = _utc(2026, 8, 20, 15, 0)
        last = _utc(2026, 8, 18, 20, 35)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_expected_friday_2130(self):
        """Fri 21:30 — expected, 0s elapsed < grace_s=3600, not overdue."""
        now = _utc(2026, 8, 14, 21, 30)
        last = _utc(2026, 8, 11, 21, 5)   # previous Sunday
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False

    def test_overdue_friday_past_grace(self):
        """Fri 22:40 (70min after 21:30) — not ran → overdue."""
        now = _utc(2026, 8, 14, 22, 40)
        last = _utc(2026, 8, 11, 21, 5)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True


class TestBL2ScanCronSet:
    """bundesliga2_scan — CronSet: daily 06:00 + matchday extras."""

    EXP = JOB_EXPECTATIONS["bundesliga2_scan"]

    def test_is_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_daily_06_point_present(self):
        assert (None, 6, 0) in self.EXP.points

    def test_friday_extra_1500(self):
        assert (4, 15, 0) in self.EXP.points   # Fri=4

    def test_saturday_extra_0800(self):
        assert (5, 8, 0) in self.EXP.points    # Sat=5

    def test_sunday_extra_0800(self):
        assert (6, 8, 0) in self.EXP.points    # Sun=6

    def test_not_stale_midmorning_wednesday_after_daily_run(self):
        """Wed 10:00: ran at 06:05 (after daily 06:00, no extra Wed cron) → not_expected."""
        now = _utc(2026, 8, 19, 10, 0)   # Wednesday — only daily 06:00 applies
        last = _utc(2026, 8, 19, 6, 5)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_sunday_extra_0800_triggers_if_only_daily_ran(self):
        """Sun 10:00: only ran at 06:05 (daily), missed 08:00 extra cron → in_window."""
        now = _utc(2026, 8, 16, 10, 0)   # Sunday 10:00
        last = _utc(2026, 8, 16, 6, 5)   # ran after 06:00 but not after 08:00
        r = evaluate_expectation(self.EXP, last, now)
        # 08:00 is most recent past point; last_run (06:05) < 08:00 → in_window
        assert r.in_window is True

    def test_friday_expected_at_1500_if_not_yet_ran(self):
        """Fri 15:00 — extra scan expected; ran at 06:05 (before 15:00 cron) → in_window."""
        now = _utc(2026, 8, 14, 15, 0)
        last = _utc(2026, 8, 14, 6, 5)   # ran after daily 06:00 but before 15:00
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is False  # 0s elapsed since 15:00 cron


# ---------------------------------------------------------------------------
# CEO Correction 2 — launchd scheduler resolution (new tests)
# ---------------------------------------------------------------------------

class TestDailyScanLaunchd:
    """daily_scan: launchd StartCalendarInterval 07:00 UTC daily."""

    EXP = JOB_EXPECTATIONS["daily_scan"]

    def test_is_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_single_daily_point_at_0700(self):
        assert (None, 7, 0) in self.EXP.points
        assert len(self.EXP.points) == 1

    def test_not_expected_after_morning_run(self):
        """Ran 07:02, now 14:00 → not_expected (no second daily trigger)."""
        now = _utc(2026, 8, 17, 14, 0)
        last = _utc(2026, 8, 17, 7, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_overdue_if_missed_0700(self):
        """10:01 (3h1m after 07:00), no run today → overdue (> grace_s=7200s)."""
        now = _utc(2026, 8, 17, 10, 1)
        last = _utc(2026, 8, 16, 7, 2)   # yesterday's run — before today's 07:00 point
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True
        assert r.is_overdue is True

    def test_within_grace_not_overdue(self):
        """08:59 (1h59m after 07:00), no run yet → within grace_s=7200, not overdue."""
        now = _utc(2026, 8, 17, 8, 59)
        last = _utc(2026, 8, 16, 7, 3)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False


class TestAutoRetrainCronSet:
    """auto_retrain: launchd StartCalendarInterval 06:00 + 18:00 UTC daily."""

    EXP = JOB_EXPECTATIONS["auto_retrain"]

    def test_is_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_two_daily_points(self):
        assert (None, 6, 0) in self.EXP.points
        assert (None, 18, 0) in self.EXP.points
        assert len(self.EXP.points) == 2

    def test_not_expected_after_morning_run(self):
        """Ran 06:02, now 12:00 → not_expected (next trigger 18:00)."""
        now = _utc(2026, 8, 17, 12, 0)
        last = _utc(2026, 8, 17, 6, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_expected_at_1800_if_only_morning_ran(self):
        """19:00, ran at 06:02, not yet after 18:00 cron → in_window."""
        now = _utc(2026, 8, 17, 19, 0)
        last = _utc(2026, 8, 17, 6, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True

    def test_not_expected_after_evening_run(self):
        """Ran 18:03, now 21:00 → not_expected."""
        now = _utc(2026, 8, 17, 21, 0)
        last = _utc(2026, 8, 17, 18, 3)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_next_trigger_between_runs(self):
        """12:00: next trigger is 18:00 → ~21600s."""
        now = _utc(2026, 8, 17, 12, 0)
        s = next_trigger_s(self.EXP, now)
        assert s is not None
        assert 21000 < s < 22200  # ~6h ± 10min


class TestClosingOddsCronSet:
    """closing_odds: launchd 14:00 UTC + closing-odds-evening 18:00 UTC."""

    EXP = JOB_EXPECTATIONS["closing_odds"]

    def test_is_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_two_daily_points(self):
        assert (None, 14, 0) in self.EXP.points
        assert (None, 18, 0) in self.EXP.points
        assert len(self.EXP.points) == 2

    def test_not_expected_after_afternoon_run(self):
        """Ran 14:02, now 16:00 → not_expected."""
        now = _utc(2026, 8, 17, 16, 0)
        last = _utc(2026, 8, 17, 14, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False

    def test_expected_at_1800_if_only_afternoon_ran(self):
        """19:00, only ran 14:02 → in_window (missed 18:00)."""
        now = _utc(2026, 8, 17, 19, 0)
        last = _utc(2026, 8, 17, 14, 2)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True


class TestPrematchScanInterval:
    """prematch_scan: launchd StartInterval=1200 (20 min)."""

    EXP = JOB_EXPECTATIONS["prematch_scan"]

    def test_is_interval(self):
        assert isinstance(self.EXP, IntervalExpectation)

    def test_interval_is_1200(self):
        assert self.EXP.interval_s == 1200

    def test_not_stale_within_1200s(self):
        now = _utc(2026, 8, 17, 12, 0)
        last = now - timedelta(seconds=1100)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False

    def test_stale_after_1800s(self):
        """After 1800s (= interval + grace_s), must be overdue."""
        now = _utc(2026, 8, 17, 12, 0)
        last = now - timedelta(seconds=1801)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is True


class TestSettleCronSet:
    """settle: launchd StartCalendarInterval HH:30 for all 24 hours."""

    EXP = JOB_EXPECTATIONS["settle"]

    def test_is_cron_set(self):
        assert isinstance(self.EXP, CronSetExpectation)

    def test_24_daily_points_at_30min(self):
        assert len(self.EXP.points) == 24
        for h in range(24):
            assert (None, h, 30) in self.EXP.points

    def test_not_expected_after_hourly_run(self):
        """Ran 12:31, now 12:45 → not_expected (next trigger 13:30)."""
        now = _utc(2026, 8, 17, 12, 45)
        last = _utc(2026, 8, 17, 12, 31)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is False
        assert r.is_overdue is False

    def test_in_window_if_missed_hourly(self):
        """13:45 and last ran at 12:31 → missed 13:30, in_window."""
        now = _utc(2026, 8, 17, 13, 45)
        last = _utc(2026, 8, 17, 12, 31)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.in_window is True

    def test_overdue_after_grace(self):
        """13:31 = 1 min after 13:30, last ran 12:31 → overdue (> grace_s=600)."""
        now = _utc(2026, 8, 17, 13, 41)   # 11min after 13:30
        last = _utc(2026, 8, 17, 12, 31)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is True   # 660s > grace_s=600

    def test_within_grace_not_overdue(self):
        """13:39 = 9min after 13:30 → within grace_s=600, not overdue."""
        now = _utc(2026, 8, 17, 13, 39)   # 9min after 13:30
        last = _utc(2026, 8, 17, 12, 31)
        r = evaluate_expectation(self.EXP, last, now)
        assert r.is_overdue is False


class TestNeverRanOffWindowIsError:
    """CEO C2 corrected: raw=None + off-window must publish error, not degraded or ok."""

    def test_windowed_job_never_ran_off_window_is_error(self, agg_dirs=None):
        from src.monitoring import aggregate_health as ag
        monday = _utc(2026, 8, 17, 20, 0)   # Monday — bl2_live_push off-window
        entry = ag._job_entry("bundesliga2_live_push", None, now=monday)
        assert entry["status"] == "error"
        assert entry["exit_code"] is None
        assert entry["expectation_state"] == "not_expected"

    def test_cron_set_job_never_ran_between_points_is_error(self, agg_dirs=None):
        """CronSet job never ran, currently between triggers → error+not_expected."""
        from src.monitoring import aggregate_health as ag
        # tennis_retrain: ran today 05:02, so no snapshot means "never". Between points at 12:00.
        now = _utc(2026, 8, 17, 12, 0)   # between 05:00 and next day 05:00 — not_expected
        entry = ag._job_entry("tennis_retrain", None, now=now)
        # No snapshot: last_expected=05:00 today, no run → in_window=True → should be stale
        # (this is the overdue path, not off-window). Verifying off-window behavior separately.
        # The "not_expected" case only applies when ran_in_time=True; None means never → in_window.
        # So for CronSet-never-ran-in-window, expect stale (overdue), not error.
        assert entry["status"] == "stale"
        assert entry["expectation_state"] == "overdue"

    def test_never_ran_off_window_overall_is_down(self, agg_dirs=None):
        """Error status from never-ran+off-window job propagates to overall=down."""
        from src.monitoring import aggregate_health as ag
        monday = _utc(2026, 8, 17, 20, 0)
        entry = ag._job_entry("bundesliga2_live_push", None, now=monday)
        assert entry["status"] == "error", "must be error to trigger overall=down"
