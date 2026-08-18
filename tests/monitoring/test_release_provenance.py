"""TASK-P0B-004 — Release & Publication Provenance tests (CEO correction pass).

Proves all 8 required invariants plus correction-pass additions:

  P1. Source release stable across data-only HEAD movement
  P2. Source-changing validated release advances source identity
  P3. Runtime identity moves independently
  P4. Unknown source identity fails closed
  P5. Serialization — deterministic, JSON-compatible
  P6. Schema/build timestamp present
  P7. Backwards compatibility — legacy health consumers unaffected
  P8. Existing P0-B truth invariants intact (regression guard)

  C1. CI SHA mismatch guard — source_ci.head_sha != source_release_sha → fail closed
  C4a. Source A + data-only descendants → consistent = True
  C4b. Source A + source-changing descendant without release record → consistent = False
  C4c. Unable to determine ancestry → consistent = None → fail closed
  C4d. Source-changing validated release B → consistent True after advance

  C3-static. ci_gates.yml provenance push does NOT contain a silent-success path
             (grep for '|| true' in the push step — must be absent).
  C5-static. Every active workflow invoking aggregate_health uses fetch-depth: 0
             so _check_source_runtime_consistency() can classify SOURCE..RUNTIME.

No live network calls. All state transitions are deterministic.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SOURCE_SHA_A = "aabbccdd11223344556677889900aabbccdd1122"
SOURCE_SHA_B = "feedface" * 5 + "feed"  # 40 chars: new source release
RUNTIME_SHA_DATA_1 = "1111111111111111111111111111111111111111"
RUNTIME_SHA_DATA_2 = "2222222222222222222222222222222222222222"
RUNTIME_SHA_SOURCE_CHANGING = "deadbeef" * 5  # would trigger CI if landed

VALID_META = {
    "schema_version": "1",
    "source_release_sha": SOURCE_SHA_A,
    "source_ci": {
        "run_id": "99999999",
        "workflow": "ci_gates.yml",
        "status": "success",
        "head_sha": SOURCE_SHA_A,  # C1: must match source_release_sha
    },
    "recorded_at": "2026-08-17T19:00:00Z",
}


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
    """Redirect HEALTH_DIR, HEALTH_JSON_OUT, PROVENANCE_META_PATH for aggregate tests."""
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


def _make_git_mock(returncode=0, stdout="", stderr=""):
    """Create a subprocess.run mock returning a CompletedProcess-like object."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# C1 — CI SHA mismatch guard
# ---------------------------------------------------------------------------

def test_c1_head_sha_matches_source_release_sha(prov_dir, monkeypatch):
    """Valid record: source_ci.head_sha == source_release_sha → accepted."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setattr(
        rp, "_check_source_runtime_consistency", lambda s, r: (True, None)
    )
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] == SOURCE_SHA_A


def test_c1_head_sha_mismatch_fails_closed(prov_dir, monkeypatch):
    """C1: source_ci.head_sha != source_release_sha → fail closed.

    Run 32058083032 ran on e48cf436d (PR head), not on 71d952d85 (merge SHA).
    A record pairing these two must be rejected.
    """
    import src.monitoring.release_provenance as rp
    bad_meta = {
        **VALID_META,
        "source_release_sha": SOURCE_SHA_A,
        "source_ci": {
            **VALID_META["source_ci"],
            "head_sha": "e48cf436d19c3ba1941fa1eb867db08317619b86",  # different SHA
        },
    }
    prov_dir.write_text(json.dumps(bad_meta), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] is None
    assert "errors" in prov
    assert any("C1 mismatch" in e or "head_sha" in e for e in prov["errors"])


def test_c1_head_sha_absent_does_not_fail(prov_dir, monkeypatch):
    """Legacy records without head_sha are not rejected (field is optional for compatibility)."""
    import src.monitoring.release_provenance as rp
    legacy_meta = {
        **VALID_META,
        "source_ci": {
            "run_id": "99999999",
            "workflow": "ci_gates.yml",
            "status": "success",
            # head_sha deliberately omitted (legacy record)
        },
    }
    prov_dir.write_text(json.dumps(legacy_meta), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setattr(
        rp, "_check_source_runtime_consistency", lambda s, r: (True, None)
    )
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] == SOURCE_SHA_A


# C6 (test-only): historical P0B-004 seed frozen as fixture, so the semantic
# invariants below can be regression-tested even after main advances past that
# release. Production code (release_provenance / record_source_release) is
# untouched.
_P0B004_HISTORICAL_SEED = {
    "schema_version": "1",
    "source_release_sha": "7cd6c6793419ab1f89c4b3c8e446f7764e84387a",
    "source_ci": {
        "run_id": "32075583343",
        "workflow": "ci_gates.yml",
        "status": "success",
        "head_sha": "7cd6c6793419ab1f89c4b3c8e446f7764e84387a",
    },
    "recorded_at": "2026-08-17T22:20:43Z",
}


def _assert_provenance_seed_semantics(meta: dict) -> None:
    """Semantic invariants for any well-formed provenance_meta.json seed.

    Enforces the same C1 contract as record_source_release without hard-coding
    the current SHA/run_id (which changes with every source release merge).
    """
    import re
    sha = meta.get("source_release_sha")
    assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), (
        f"source_release_sha must be a 40-char hex string, got {sha!r}"
    )
    ci = meta.get("source_ci")
    assert isinstance(ci, dict), "source_ci must be an object"
    run_id = ci.get("run_id")
    assert isinstance(run_id, (str, int)) and str(run_id).isdigit() and str(run_id), (
        f"source_ci.run_id must be a numeric identifier, got {run_id!r}"
    )
    assert ci.get("status") == "success", (
        f"source_ci.status must be 'success', got {ci.get('status')!r}"
    )
    head_sha = ci.get("head_sha")
    assert head_sha == sha, (
        f"C1 self-consistency: source_ci.head_sha ({head_sha!r}) "
        f"must equal source_release_sha ({sha!r})"
    )
    recorded_at = meta.get("recorded_at")
    assert isinstance(recorded_at, str) and recorded_at, "recorded_at must be a non-empty string"
    # Accept trailing Z (Zulu) as +00:00
    datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))


def test_c1_seed_uses_correct_run_id():
    """Regression: provenance_meta.json seed satisfies the C1 semantic contract.

    C6 correction: uses SEMANTIC INVARIANTS (SHA format, run_id numeric,
    status=success, head_sha == source_release_sha, valid ISO recorded_at)
    rather than hard-coded values that break after every source release merge.
    """
    from src.monitoring import release_provenance as rp
    meta = json.loads(rp.PROVENANCE_META_PATH.read_text(encoding="utf-8"))
    _assert_provenance_seed_semantics(meta)


def test_c1_historical_p0b004_seed_regression_fixture():
    """C6: the historical P0B-004 seed still satisfies the semantic contract.

    Guards against a future refactor that would silently accept a seed the
    original P0B-004 seed would have failed. Runs against a static fixture, NOT
    the live provenance_meta.json (which has already advanced).
    """
    _assert_provenance_seed_semantics(_P0B004_HISTORICAL_SEED)
    # Explicit historical values remain verifiable via fixture:
    assert _P0B004_HISTORICAL_SEED["source_release_sha"] == (
        "7cd6c6793419ab1f89c4b3c8e446f7764e84387a"
    )
    assert _P0B004_HISTORICAL_SEED["source_ci"]["run_id"] == "32075583343"


# ---------------------------------------------------------------------------
# P1 — Source release stable across data-only HEAD movement
# ---------------------------------------------------------------------------

def test_p1_source_release_stable_across_data_commits(prov_dir, monkeypatch):
    """Given source=A, data-only commits advance HEAD. source_release_sha must remain A."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")

    for sha in [RUNTIME_SHA_DATA_1, RUNTIME_SHA_DATA_2]:
        monkeypatch.setenv("GITHUB_SHA", sha)
        # Patch consistency to True (data-only confirmed)
        monkeypatch.setattr(
            rp, "_check_source_runtime_consistency", lambda s, r: (True, None)
        )
        prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
        assert prov["source_release_sha"] == SOURCE_SHA_A
        assert prov["runtime_data_sha"] == sha
        assert prov["source_runtime_consistent"] is True
        assert rp.is_source_release_resolved(prov)


# ---------------------------------------------------------------------------
# P2 — Source-changing validated release advances source identity
# ---------------------------------------------------------------------------

def test_p2_new_validated_release_advances_source_sha(prov_dir, monkeypatch):
    """When a new valid source release is recorded, source_release_sha advances."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")

    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    first = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert first["source_release_sha"] == SOURCE_SHA_A

    new_meta = {
        **VALID_META,
        "source_release_sha": SOURCE_SHA_B,
        "source_ci": {
            "run_id": "12345678",
            "workflow": "ci_gates.yml",
            "status": "success",
            "head_sha": SOURCE_SHA_B,
        },
    }
    prov_dir.write_text(json.dumps(new_meta), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_B)
    second = rp.build_provenance(built_at="2026-08-18T10:05:00Z")
    assert second["source_release_sha"] == SOURCE_SHA_B
    assert second["source_release_sha"] != first["source_release_sha"]


# ---------------------------------------------------------------------------
# P3 — Runtime identity moves independently
# ---------------------------------------------------------------------------

def test_p3_runtime_sha_moves_independently(prov_dir, monkeypatch):
    """Data-only commit advances runtime_data_sha without changing source_release_sha."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    prov_a = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_2)
    prov_b = rp.build_provenance(built_at="2026-08-17T21:05:00Z")

    assert prov_a["source_release_sha"] == prov_b["source_release_sha"]
    assert prov_a["runtime_data_sha"] != prov_b["runtime_data_sha"]
    assert prov_b["runtime_data_sha"] == RUNTIME_SHA_DATA_2


# ---------------------------------------------------------------------------
# P4 — Unknown source identity fails closed
# ---------------------------------------------------------------------------

def test_p4_missing_meta_fails_closed(prov_dir, monkeypatch):
    """Missing provenance_meta.json → source_release_sha = null."""
    import src.monitoring.release_provenance as rp
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] is None
    assert not rp.is_source_release_resolved(prov)
    assert "errors" in prov


def test_p4_malformed_meta_fails_closed(prov_dir, monkeypatch):
    """Malformed provenance_meta.json → source_release_sha = null."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text("{ not valid json !!!", encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] is None


def test_p4_empty_sha_fails_closed(prov_dir, monkeypatch):
    """Empty source_release_sha in meta → fail closed."""
    import src.monitoring.release_provenance as rp
    bad = {**VALID_META, "source_release_sha": ""}
    prov_dir.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] is None


def test_p4_invalid_ci_status_fails_closed(prov_dir, monkeypatch):
    """source_ci.status='failure' → fail closed."""
    import src.monitoring.release_provenance as rp
    bad = {
        **VALID_META,
        "source_ci": {**VALID_META["source_ci"], "status": "failure"},
    }
    prov_dir.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["source_release_sha"] is None


# ---------------------------------------------------------------------------
# C4a — Source A + data-only descendants → consistent = True
# ---------------------------------------------------------------------------

def test_c4a_data_only_descendants_consistent(prov_dir, monkeypatch):
    """C4a: git log between source and runtime shows no source-changing files → True."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)

    def mock_git(args, **kw):
        if "log" in args:
            mock = _make_git_mock(returncode=0, stdout="")  # empty = no source changes
            return mock
        return _make_git_mock(returncode=0, stdout=RUNTIME_SHA_DATA_1)

    monkeypatch.setattr(subprocess, "run", mock_git)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_release_sha"] == SOURCE_SHA_A
    assert prov["runtime_data_sha"] == RUNTIME_SHA_DATA_1
    assert prov["source_runtime_consistent"] is True
    assert rp.is_source_release_resolved(prov)


def test_c4a_same_sha_is_trivially_consistent(prov_dir, monkeypatch):
    """C4a: runtime == source_release → consistent = True without git call."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)

    git_called = []

    def mock_git(args, **kw):
        if "log" in args:
            git_called.append(True)
        return _make_git_mock(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", mock_git)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_runtime_consistent"] is True
    assert not git_called, "git log must not be called when SHAs are equal"


# ---------------------------------------------------------------------------
# C4b — Source A + source-changing descendant B without release record → False
# ---------------------------------------------------------------------------

def test_c4b_source_changing_commits_detected(prov_dir, monkeypatch):
    """C4b: git log finds source-changing commits between source and runtime → consistent = False."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_SOURCE_CHANGING)

    def mock_git(args, **kw):
        if "log" in args:
            # Simulate finding a source-changing commit
            return _make_git_mock(returncode=0, stdout="abc1234 feat: new model added to src/")
        return _make_git_mock(returncode=0, stdout=RUNTIME_SHA_SOURCE_CHANGING)

    monkeypatch.setattr(subprocess, "run", mock_git)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_runtime_consistent"] is False
    assert not rp.is_source_release_resolved(prov)
    assert "errors" in prov or prov.get("source_runtime_consistent") is False


# ---------------------------------------------------------------------------
# C4c — Unable to determine ancestry → consistent = None → fail closed
# ---------------------------------------------------------------------------

def test_c4c_git_error_fails_closed(prov_dir, monkeypatch):
    """C4c: git log fails (shallow clone, SHA not found) → consistent = None → not resolved."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)

    def mock_git(args, **kw):
        if "log" in args:
            return _make_git_mock(returncode=128, stderr="fatal: bad object abc123")
        return _make_git_mock(returncode=0, stdout=RUNTIME_SHA_DATA_1)

    monkeypatch.setattr(subprocess, "run", mock_git)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_runtime_consistent"] is None
    assert not rp.is_source_release_resolved(prov), (
        "C4c: unknown consistency must fail closed — SHA alone is insufficient"
    )


def test_c4c_git_timeout_fails_closed(prov_dir, monkeypatch):
    """C4c: git subprocess timeout → consistent = None → not resolved."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)

    def mock_git(args, **kw):
        if "log" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=10)
        return _make_git_mock(returncode=0, stdout=RUNTIME_SHA_DATA_1)

    monkeypatch.setattr(subprocess, "run", mock_git)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")

    assert prov["source_runtime_consistent"] is None
    assert not rp.is_source_release_resolved(prov)


# ---------------------------------------------------------------------------
# C4d — Source-changing validated release B with matching metadata → True
# ---------------------------------------------------------------------------

def test_c4d_new_valid_release_advance_consistent(prov_dir, monkeypatch):
    """C4d: new source release B recorded → runtime == B → consistent = True."""
    import src.monitoring.release_provenance as rp
    new_meta = {
        **VALID_META,
        "source_release_sha": SOURCE_SHA_B,
        "source_ci": {
            "run_id": "11111111",
            "workflow": "ci_gates.yml",
            "status": "success",
            "head_sha": SOURCE_SHA_B,
        },
    }
    prov_dir.write_text(json.dumps(new_meta), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_B)  # runtime == source (just merged)

    def mock_git(args, **kw):
        # Same SHA → _check_source_runtime_consistency returns True without git call
        return _make_git_mock(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", mock_git)
    prov = rp.build_provenance(built_at="2026-08-18T10:05:00Z")

    assert prov["source_release_sha"] == SOURCE_SHA_B
    assert prov["source_runtime_consistent"] is True
    assert rp.is_source_release_resolved(prov)


# ---------------------------------------------------------------------------
# P5 — Serialization
# ---------------------------------------------------------------------------

def test_p5_provenance_json_serializable(prov_dir, monkeypatch):
    """Provenance dict is JSON-serializable and deterministic."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))

    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    serialized = json.dumps(prov)
    reparsed = json.loads(serialized)
    assert reparsed["source_release_sha"] == prov["source_release_sha"]
    assert reparsed["runtime_data_sha"] == prov["runtime_data_sha"]
    assert reparsed["built_at"] == "2026-08-17T21:00:00Z"
    assert reparsed["schema_version"] == "1"
    assert reparsed["source_runtime_consistent"] is True


def test_p5_null_case_serializable(prov_dir, monkeypatch):
    """Null/error provenance is also JSON-serializable."""
    import src.monitoring.release_provenance as rp
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    reparsed = json.loads(json.dumps(prov))
    assert reparsed["source_release_sha"] is None
    assert reparsed["source_runtime_consistent"] is None


# ---------------------------------------------------------------------------
# P6 — Schema/build timestamp
# ---------------------------------------------------------------------------

def test_p6_built_at_present(prov_dir, monkeypatch):
    """Provenance always includes an explicit built_at timestamp."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["built_at"] == "2026-08-17T21:00:00Z"


def test_p6_built_at_auto_generated_when_none(prov_dir, monkeypatch):
    """If built_at is not provided, it is auto-generated as a valid ISO timestamp."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    prov = rp.build_provenance()
    datetime.fromisoformat(prov["built_at"].replace("Z", "+00:00"))


def test_p6_schema_version_present(prov_dir, monkeypatch):
    """Provenance always includes schema_version."""
    import src.monitoring.release_provenance as rp
    prov_dir.write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    prov = rp.build_provenance(built_at="2026-08-17T21:00:00Z")
    assert prov["schema_version"] == "1"


# ---------------------------------------------------------------------------
# P7 — Backwards compatibility
# ---------------------------------------------------------------------------

def test_p7_aggregate_still_has_legacy_fields(agg_dirs, monkeypatch):
    """Existing health.json fields (generated_at, overall, jobs) present after provenance."""
    import src.monitoring.aggregate_health as ag
    import src.monitoring.release_provenance as rp
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    agg_dirs["meta_file"].write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    payload = ag.aggregate()
    assert "generated_at" in payload
    assert "overall" in payload
    assert "jobs" in payload
    assert "provenance" in payload


def test_p7_provenance_embedded_in_health_json(agg_dirs, monkeypatch):
    """The written health.json includes provenance at top level with all keys."""
    import src.monitoring.aggregate_health as ag
    import src.monitoring.release_provenance as rp
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    agg_dirs["meta_file"].write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    ag.aggregate()
    data = json.loads(agg_dirs["out_file"].read_text())
    assert "provenance" in data
    prov = data["provenance"]
    assert prov["source_release_sha"] == SOURCE_SHA_A
    assert prov["runtime_data_sha"] == SOURCE_SHA_A
    assert prov["source_runtime_consistent"] is True
    assert "source_ci" in prov


def test_p7_provenance_fail_closed_in_aggregate(agg_dirs, monkeypatch):
    """Missing provenance_meta.json → health.json still written, provenance has null source."""
    import src.monitoring.aggregate_health as ag
    monkeypatch.setenv("GITHUB_SHA", RUNTIME_SHA_DATA_1)
    payload = ag.aggregate()
    assert "provenance" in payload
    assert payload["provenance"]["source_release_sha"] is None
    data = json.loads(agg_dirs["out_file"].read_text())
    assert "generated_at" in data
    assert "jobs" in data


def test_p7_provenance_fields_not_in_job_entries(agg_dirs, monkeypatch):
    """Provenance is top-level only — must not appear inside job entries."""
    import src.monitoring.aggregate_health as ag
    import src.monitoring.release_provenance as rp
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    agg_dirs["meta_file"].write_text(json.dumps(VALID_META), encoding="utf-8")
    monkeypatch.setattr(rp, "_check_source_runtime_consistency", lambda s, r: (True, None))
    payload = ag.aggregate()
    for j in payload["jobs"]:
        assert "provenance" not in j


# ---------------------------------------------------------------------------
# P8 — Existing P0-B truth invariants intact (regression guard)
# ---------------------------------------------------------------------------

def test_p8_mon001_exit_code_coercion_unchanged(tmp_path, monkeypatch):
    """MON-001: exit_code != 0 cannot serialize status=ok."""
    import src.monitoring.health_writer as hw
    monkeypatch.setattr(hw, "HEALTH_DIR", tmp_path)
    path = hw.write_health("test_job", "ok", exit_code=1, run_id="test-001")
    data = json.loads(path.read_text())
    assert data["status"] == "error"


def test_p8_mon001_bool_exit_rejected():
    """MON-001: bool exit_code rejected (bool is int subclass)."""
    from src.monitoring.health_writer import _coerce_exit_code
    assert _coerce_exit_code(True) is None
    assert _coerce_exit_code(False) is None


# ---------------------------------------------------------------------------
# C3-static — No silent-success push path in ci_gates.yml
# ---------------------------------------------------------------------------

def test_c3_static_no_silent_push_in_ci_gates():
    """C3: ci_gates.yml provenance push step must NOT contain '|| true' for the git push.

    A provenance push failure must abort CI, not be silently swallowed.
    This test verifies the workflow no longer contains the || true pattern
    on the line that pushes provenance_meta.json to origin.
    """
    from src.monitoring import release_provenance as rp
    workflow_path = rp.ROOT / ".github" / "workflows" / "ci_gates.yml"
    assert workflow_path.exists(), "ci_gates.yml not found"
    content = workflow_path.read_text(encoding="utf-8")

    # Find the provenance push block — look for lines between "Commit and push provenance"
    # and the next step delimiter. The git push in that block must not have || true.
    lines = content.splitlines()
    in_provenance_block = False
    for line in lines:
        if "Commit and push provenance" in line:
            in_provenance_block = True
            continue
        if in_provenance_block:
            # Stop at the next step
            if line.strip().startswith("- name:") or line.strip().startswith("- uses:"):
                break
            # git push line must not have || true
            if "git push" in line and "|| true" in line:
                pytest.fail(
                    f"C3 violation: provenance push has silent-success path: {line!r}\n"
                    "A provenance publication failure must NOT be converted into workflow success."
                )


# ---------------------------------------------------------------------------
# record_source_release script tests
# ---------------------------------------------------------------------------

def test_record_source_release_writes_meta_with_head_sha(tmp_path, monkeypatch):
    """record_source_release.py writes valid provenance_meta.json including head_sha (C1)."""
    import scripts.record_source_release as rsr
    meta_path = tmp_path / "provenance_meta.json"
    monkeypatch.setattr(rsr, "PROVENANCE_META", meta_path)
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.setenv("GITHUB_RUN_ID", "55551234")
    monkeypatch.setenv("GITHUB_WORKFLOW", "ci_gates.yml")
    rc = rsr.main()
    assert rc == 0
    data = json.loads(meta_path.read_text())
    assert data["source_ci"]["status"] == "success"
    assert data["source_release_sha"] == SOURCE_SHA_A
    assert data["source_ci"]["head_sha"] == SOURCE_SHA_A, (
        "C1: head_sha must be written and equal source_release_sha"
    )
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
    monkeypatch.setenv("GITHUB_SHA", SOURCE_SHA_A)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    rc = rsr.main()
    assert rc == 1


# ---------------------------------------------------------------------------
# C5 — Static regression: every active workflow that publishes/aggregates
#       provenance must use a full-history checkout (fetch-depth: 0).
# ---------------------------------------------------------------------------

def _active_workflows_invoking_aggregate_health() -> list[Path]:
    """Return paths of active (non-.disabled) workflow files that invoke the
    canonical aggregate_health Python module or build_provenance path.

    Matches only direct Python-module invocations so that workflows which only
    reference 'aggregate_health' as a string constant (e.g., cloud_healer.yml
    SKIP set) are not incorrectly flagged.
    """
    workflows_dir = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
    # Patterns that unambiguously invoke the aggregate_health module in Python
    _INVOKE_PATTERNS = (
        "src.monitoring.aggregate_health",
        "from src.monitoring.aggregate_health",
        "import aggregate_health",
    )
    matched: list[Path] = []
    for wf in sorted(workflows_dir.glob("*.yml")):
        if wf.suffix == ".disabled":
            continue
        text = wf.read_text(encoding="utf-8")
        if any(pat in text for pat in _INVOKE_PATTERNS):
            matched.append(wf)
    return matched


def test_c5_static_aggregate_health_workflows_use_full_history_checkout():
    """Every active workflow that invokes src.monitoring.aggregate_health must
    have fetch-depth: 0 on its checkout step.

    aggregate_health calls build_provenance() → _check_source_runtime_consistency()
    which runs `git log SOURCE..RUNTIME`. A shallow clone without full history
    causes git log to return a non-zero exit code, making source_runtime_consistent
    = None (fail-closed), which would routinely degrade public provenance in health.json.

    This test prevents future regressions where a new aggregate_health-invoking
    workflow is added without the required full-history checkout.
    """
    workflows = _active_workflows_invoking_aggregate_health()
    assert workflows, (
        "No active workflows found that invoke src.monitoring.aggregate_health — "
        "update _INVOKE_PATTERNS if the invocation pattern changed"
    )

    violations: list[str] = []
    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        if "fetch-depth: 0" not in text:
            violations.append(
                f"{wf.name}: invokes aggregate_health but lacks 'fetch-depth: 0' "
                f"on checkout — _check_source_runtime_consistency() will fail-closed "
                f"on shallow clones, degrading provenance in public health.json"
            )

    assert not violations, (
        "C5 VIOLATION — workflow(s) invoking aggregate_health lack full-history checkout:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
