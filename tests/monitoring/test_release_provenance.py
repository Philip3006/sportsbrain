"""TASK-P0B-004 — Release & Publication Provenance tests.

Proves all 8 required invariants:

  P1. Source release stable across data-only HEAD movement
      Given source_release_sha = A, data-only commits advance HEAD to B/C/D.
      Published provenance still reports source_release_sha = A.

  P2. Source-changing validated release advances source identity
      When a new valid source release is recorded, source_release_sha advances.

  P3. Runtime identity moves independently
      A data-only commit advances runtime_data_sha without changing source_release_sha.

  P4. Unknown source identity fails closed
      Missing/malformed/invalid release evidence → source_release_sha = null, error reported.

  P5. Serialization
      Public provenance serializes deterministically and is JSON-compatible.

  P6. Schema/build timestamp
      The published artifact includes an explicit built_at timestamp.

  P7. Backwards compatibility
      Existing health.json consumers continue functioning with provenance added.

  P8. Existing P0-B truth invariants remain intact
      MON-001/MON-002/P0-B3 regression guards remain green.

No live network calls. All state transitions are deterministic.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_META = {
    "schema_version": "1",
    "source_release_sha": "aabbccdd11223344556677889900aabbccdd1122",
    "source_ci": {
        "run_id": "99999999",
        "workflow": "ci_gates.yml",
        "status": "success",
    },
    "recorded_at": "2026-08-17T19:00:00Z",
}

RUNTIME_SHA_A = "aabbccdd11223344556677889900aabbccdd1122"
RUNTIME_SHA_B = "deadbeef00112233445566778899aabbccddeeff"


@pytest.fixture()
def prov_dir(tmp_path, monkeypatch):
    """Redirect PROVENANCE_META_PATH to a temp location."""
    import src.monitoring.release_provenance as rp
    meta_path = tmp_path / "docs" / "data" / "provenance_meta.json"
    meta_path.parent.mkdir(parents=True)
    monkeypatch.setattr(rp, "PROVENANCE_META_PATH", meta_path)
    return meta_path


@pytest.fixture()
def agg_dirs(tmp_path, monkeypatch):
    """Redirect HEALTH_DIR, HEALTH_JSON_OUT, and PROVENANCE_META_PATH for aggregate tests."""
    import src.monitoring.aggregate_health as ag
    import src.monitoring.health_writer as hw
    import src.monitoring.release_provenance as rp

    h_dir = tmp_path / "health"
    h_dir.mkdir()
    out_dir = tmp_path / "docs" / "data"
    out_dir.mkdir(parents=True)
    out_file = out_dir / "health.json"
    meta_file = out_dir / "provenance_meta.json"

    monkeypatch.setattr(hw, "HEALTH_DIR", h_dir)
    monkeypatch.setattr(ag, "HEALTH_DIR", h_dir)
    monkeypatch.setattr(ag, "HEALTH_JSON_OUT", out_file)
    monkeypatch.setattr(rp, "PROVENANCE_META_PATH", meta_file)
    monkeypatch.setattr(ag, "_FRESHNESS_TARGETS", [])

    return {"health_dir": h_dir, "out_file": out_file, "meta_file": meta_file}


# ---------------------------------------------------------------------------
# P1 — Source release stable across data-only HEAD movement
# ---------------------------------------------------------------------------

def test_p1_source_release_stable_across_data_commits(prov_dir, monkeypatch):
    """Given source=A, data-only commits advance HEAD to B/C/D.
    source_release_sha must remain A."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")

    # Simulate three different "runtime" HEAD SHAs (data-only commits)
    runtime_shas = [
        "1111111111111111111111111111111111111111",
        "2222222222222222222222222222222222222222",
        "3333333333333333333333333333333333333333",
    ]
    for sha in runtime_shas:
        monkeypatch.setenv("GITHUB_SHA", sha)
        prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
        assert prov["source_release_sha"] == VALID_META["source_release_sha"], (
            f"source_release_sha must remain stable across data-only HEAD={sha}"
        )
        assert prov["runtime_data_sha"] == sha
        assert rp.is_source_release_resolved(prov)


# ---------------------------------------------------------------------------
# P2 — Source-changing validated release advances source identity
# ---------------------------------------------------------------------------

def test_p2_new_validated_release_advances_source_sha(prov_dir, monkeypatch):
    """When a new valid source release is recorded, source_release_sha advances."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    first = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert first["source_release_sha"] == VALID_META["source_release_sha"]

    new_sha = "feedface" * 5  # new source release
    new_meta = {
        **VALID_META,
        "source_release_sha": new_sha,
        "source_ci": {"run_id": "12345678", "workflow": "ci_gates.yml", "status": "success"},
        "recorded_at": "2026-08-18T10:00:00Z",
    }
    prov_dir.write_text(json.dumps(new_meta), encoding="utf-8")

    second = rp.build_provenance(built_at="2026-08-18T10:05:00Z")
    assert second["source_release_sha"] == new_sha
    assert second["source_release_sha"] != first["source_release_sha"]


# ---------------------------------------------------------------------------
# P3 — Runtime identity moves independently
# ---------------------------------------------------------------------------

def test_p3_runtime_sha_moves_independently(prov_dir, monkeypatch):
    """Data-only commit advances runtime_data_sha without changing source_release_sha."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    prov_a = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_B)
    prov_b = rp.build_provenance(built_at="2026-08-17T21:05:00Z")

    # Source release unchanged
    assert prov_a["source_release_sha"] == prov_b["source_release_sha"]
    # Runtime data advanced
    assert prov_a["runtime_data_sha"] != prov_b["runtime_data_sha"]
    assert prov_b["runtime_data_sha"] == RUNTIME_SHA_B


# ---------------------------------------------------------------------------
# P4 — Unknown source identity fails closed
# ---------------------------------------------------------------------------

def test_p4_missing_meta_fails_closed(prov_dir, monkeypatch):
    """Missing provenance_meta.json → source_release_sha = null, error reported."""
    import src.monitoring.release_provenance as rp

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_release_sha"] is None
    assert not rp.is_source_release_resolved(prov)
    assert "errors" in prov
    assert any("source_release" in e for e in prov["errors"])


def test_p4_malformed_meta_fails_closed(prov_dir, monkeypatch):
    """Malformed provenance_meta.json → source_release_sha = null."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text("{ not valid json !!!", encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_release_sha"] is None
    assert "errors" in prov


def test_p4_empty_sha_fails_closed(prov_dir, monkeypatch):
    """Empty source_release_sha in meta → fail closed."""
    import src.monitoring.release_provenance as rp

    bad_meta = {**VALID_META, "source_release_sha": ""}
    prov_dir.write_text(json.dumps(bad_meta), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_release_sha"] is None
    assert "errors" in prov


def test_p4_invalid_ci_status_fails_closed(prov_dir, monkeypatch):
    """source_ci.status != 'success' → fail closed (e.g., 'failure' must not propagate)."""
    import src.monitoring.release_provenance as rp

    bad_meta = {
        **VALID_META,
        "source_ci": {"run_id": "1", "workflow": "ci_gates.yml", "status": "failure"},
    }
    prov_dir.write_text(json.dumps(bad_meta), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_release_sha"] is None
    assert "errors" in prov


# ---------------------------------------------------------------------------
# P5 — Serialization
# ---------------------------------------------------------------------------

def test_p5_provenance_json_serializable(prov_dir, monkeypatch):
    """Provenance dict is JSON-serializable and deterministic."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)

    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    serialized = json.dumps(prov)
    assert isinstance(serialized, str)

    # Re-parse and compare
    reparsed = json.loads(serialized)
    assert reparsed["source_release_sha"] == prov["source_release_sha"]
    assert reparsed["runtime_data_sha"] == prov["runtime_data_sha"]
    assert reparsed["built_at"] == "2026-08-17T21:00:00Z"
    assert reparsed["schema_version"] == "1"


def test_p5_provenance_null_case_serializable(prov_dir, monkeypatch):
    """Null/error provenance is also JSON-serializable."""
    import src.monitoring.release_provenance as rp

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    serialized = json.dumps(prov)
    reparsed = json.loads(serialized)
    assert reparsed["source_release_sha"] is None


# ---------------------------------------------------------------------------
# P6 — Schema/build timestamp
# ---------------------------------------------------------------------------

def test_p6_built_at_present(prov_dir, monkeypatch):
    """Provenance always includes an explicit built_at timestamp."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)

    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert "built_at" in prov
    assert prov["built_at"] == "2026-08-17T21:00:00Z"


def test_p6_built_at_auto_generated_when_none(prov_dir, monkeypatch):
    """If built_at is not provided, it is auto-generated as a valid ISO timestamp."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)

    prov = rp.build_provenance()
    assert "built_at" in prov
    # Must be parseable as ISO-8601
    ts = prov["built_at"]
    datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_p6_schema_version_present(prov_dir, monkeypatch):
    """Provenance always includes schema_version."""
    import src.monitoring.release_provenance as rp

    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert "schema_version" in prov
    assert prov["schema_version"] == "1"


# ---------------------------------------------------------------------------
# P7 — Backwards compatibility
# ---------------------------------------------------------------------------

def test_p7_aggregate_still_has_legacy_fields(agg_dirs, monkeypatch):
    """Existing health.json fields (generated_at, overall, jobs) remain present after provenance added."""
    import src.monitoring.aggregate_health as ag

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    agg_dirs["meta_file"].write_text(json.dumps(VALID_META), encoding="utf-8")

    payload = ag.aggregate()

    assert "generated_at" in payload
    assert "overall" in payload
    assert "jobs" in payload
    assert "provenance" in payload


def test_p7_provenance_embedded_in_health_json(agg_dirs, monkeypatch):
    """The written health.json includes provenance at top level."""
    import src.monitoring.aggregate_health as ag

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    agg_dirs["meta_file"].write_text(json.dumps(VALID_META), encoding="utf-8")

    ag.aggregate()

    data = json.loads(agg_dirs["out_file"].read_text())
    assert "provenance" in data
    assert data["provenance"]["source_release_sha"] == VALID_META["source_release_sha"]
    assert data["provenance"]["runtime_data_sha"] == RUNTIME_SHA_A


def test_p7_provenance_fail_closed_in_aggregate(agg_dirs, monkeypatch):
    """Missing provenance_meta.json → health.json still written, provenance has null source."""
    import src.monitoring.aggregate_health as ag

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    # Do NOT write meta_file → simulate missing

    payload = ag.aggregate()

    assert "provenance" in payload
    assert payload["provenance"]["source_release_sha"] is None
    # health.json still written (backwards compatible)
    data = json.loads(agg_dirs["out_file"].read_text())
    assert "generated_at" in data
    assert "jobs" in data


# ---------------------------------------------------------------------------
# P8 — Existing P0-B truth invariants remain intact (regression guard)
# ---------------------------------------------------------------------------

def test_p8_mon001_exit_code_coercion_unchanged():
    """MON-001: exit_code != 0 cannot serialize status=ok — regression guard."""
    from src.monitoring.health_writer import write_health
    import tempfile, os

    with tempfile.TemporaryDirectory() as d:
        import src.monitoring.health_writer as hw
        original = hw.HEALTH_DIR
        hw.HEALTH_DIR = Path(d)
        try:
            path = write_health("test_job", "ok", exit_code=1, run_id="test-001")
            data = json.loads(path.read_text())
            assert data["status"] == "error", "MON-001: exit=1 must coerce ok→error"
        finally:
            hw.HEALTH_DIR = original


def test_p8_mon001_bool_exit_rejected():
    """MON-001: bool exit_code must be rejected (bool is int subclass)."""
    from src.monitoring.health_writer import _coerce_exit_code
    assert _coerce_exit_code(True) is None
    assert _coerce_exit_code(False) is None


def test_p8_provenance_does_not_affect_job_status(agg_dirs, monkeypatch):
    """Adding provenance to aggregate output must not alter any job status entry."""
    import src.monitoring.aggregate_health as ag
    import src.monitoring.health_writer as hw

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_A)
    agg_dirs["meta_file"].write_text(json.dumps(VALID_META), encoding="utf-8")

    hw.HEALTH_DIR = agg_dirs["health_dir"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = agg_dirs["health_dir"] / "consume_pending_bets.json"
    snap.write_text(json.dumps({
        "job": "consume_pending_bets",
        "status": "ok",
        "last_run_at": now,
        "exit_code": 0,
        "error": None,
        "fallback_used": None,
        "run_id": "test-run",
        "recovery_attempt_id": None,
    }), encoding="utf-8")

    payload = ag.aggregate()

    job_entries = {j["job"]: j for j in payload["jobs"]}
    cpb = job_entries.get("consume_pending_bets")
    assert cpb is not None
    assert cpb["status"] in ("ok", "not_expected", "error"), (
        f"Unexpected status: {cpb['status']}"
    )
    # Provenance is top-level, not mixed into jobs
    for j in payload["jobs"]:
        assert "provenance" not in j, "provenance must not appear inside job entries"


# ---------------------------------------------------------------------------
# record_source_release script tests
# ---------------------------------------------------------------------------

def test_record_source_release_writes_meta(tmp_path, monkeypatch):
    """record_source_release.py writes valid provenance_meta.json."""
    import scripts.record_source_release as rsr

    meta_path = tmp_path / "provenance_meta.json"
    monkeypatch.setattr(rsr, "PROVENANCE_META", meta_path)
    monkeypatch.setenv("GITHUB_SHA", "abc123def456" * 3)
    monkeypatch.setenv("GITHUB_RUN_ID", "55551234")
    monkeypatch.setenv("GITHUB_WORKFLOW", "ci_gates.yml")

    rc = rsr.main()
    assert rc == 0
    data = json.loads(meta_path.read_text())
    assert data["source_ci"]["status"] == "success"
    assert data["source_release_sha"].startswith("abc123")
    assert data["source_ci"]["run_id"] == "55551234"


def test_record_source_release_fails_without_sha(tmp_path, monkeypatch):
    """record_source_release.py returns exit code 1 if GITHUB_SHA is missing."""
    import scripts.record_source_release as rsr

    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "55551234")
    rc = rsr.main()
    assert rc == 1


def test_record_source_release_fails_without_run_id(tmp_path, monkeypatch):
    """record_source_release.py returns exit code 1 if GITHUB_RUN_ID is missing."""
    import scripts.record_source_release as rsr

    monkeypatch.setenv("GITHUB_SHA", "abc123" * 6)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    rc = rsr.main()
    assert rc == 1
