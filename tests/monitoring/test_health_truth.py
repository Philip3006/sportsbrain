"""TASK-P0B-001 — MON-001 / MON-011 / OPS-006 deterministic tests.

Proves that execution health derives from execution evidence:
  - exit_code != 0 can never serialize status="ok" or "degraded" (MON-001)
  - invalid/unknown exit evidence cannot produce success-like status (MON-001)
  - bool is rejected even though bool is a Python int subclass (strict typing)
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
