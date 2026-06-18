import json

import pytest

from src.models.lifecycle import activate, mark_validated, register_trained
from src.notifications.publish_gate import PublishBlocked, payload_digest, publish_payload


def _active_model(model_dir):
    model_dir.mkdir()
    snapshot = model_dir / "params_20260615.pkl"
    snapshot.write_bytes(b"model")
    register_trained(model_dir, snapshot, issues=[])
    mark_validated(model_dir, snapshot, bounds=True, drift=True, backtest=True, audit=True)
    activate(model_dir, snapshot)


def _payload(**overrides):
    body = {
        "updated": "2026-06-18T12:00:00Z",
        "schedule": [],
        "all_odds": {},
        "model_tips": {"Brazil vs Argentina": {"p_home": 0.55, "p_draw": 0.25, "p_away": 0.20}},
        "football": [],
        "tennis": [],
        "bankroll_state": {},
        "open_bets": [],
    }
    body.update(overrides)
    return body


def test_valid_snapshot_atomically_replaces_productive_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')

    publish_payload(_payload(), destination, model_dir, tmp_path / "audit" / "failure.json")

    assert json.loads(destination.read_text())["updated"] == "2026-06-18T12:00:00Z"


def test_audit_flag_does_not_replace_productive_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')
    _sig = {"match": "A vs B", "market": "home", "odds": 2.0, "model_prob": 0.6,
            "ev_pct": 20.0, "min_ev_pct": 3.0, "stake_eur": 10.0,
            "safety_gates": {"positive_ev": True, "min_ev": True, "kelly": True}}
    # A MEDIUM signal for the same match makes the audit flag actionable (blocking).
    suspicious = _payload(
        model_tips={"A vs B": {"p_home": 0.99}},
        football=[{**_sig, "confidence": "MEDIUM"}],
    )

    with pytest.raises(PublishBlocked, match="audit flag"):
        publish_payload(suspicious, destination, model_dir, tmp_path / "audit" / "failure.json")

    assert json.loads(destination.read_text()) == {"old": True}
    assert json.loads((tmp_path / "audit" / "failure.json").read_text())["alert"]["code"] == "PUBLISH_BLOCKED"


def test_audit_flag_low_only_does_not_block(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')
    _sig = {"match": "A vs B", "market": "home", "odds": 2.0, "model_prob": 0.6,
            "ev_pct": 20.0, "min_ev_pct": 3.0, "stake_eur": 10.0,
            "safety_gates": {"positive_ev": True, "min_ev": True, "kelly": True}}
    # Flagged match has only a LOW signal — should not block publishing.
    suspicious = _payload(
        model_tips={"A vs B": {"p_home": 0.99}},
        football=[{**_sig, "confidence": "LOW"}],
    )

    publish_payload(suspicious, destination, model_dir, tmp_path / "audit" / "failure.json")

    assert json.loads(destination.read_text()).get("updated") == "2026-06-18T12:00:00Z"


def test_candidate_bound_manual_review_can_release_flagged_prediction(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')
    suspicious = _payload(model_tips={"A vs B": {"p_home": 0.99}})
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({
        "candidate_sha256": payload_digest(suspicious),
        "active_snapshot": "params_20260615.pkl",
        "approved_by": "phase1-reviewer",
        "approved_at": "2026-06-18T15:00:00Z",
        "reviews": {"A vs B": {
            "reasons": ["p_home=0.990 > 0.85"],
            "cause_documented": "Extreme favorite is supported by current team inputs.",
            "market_comparison": "Consensus market probability independently supports the favorite.",
            "model_components_checked": ["dixon_coles", "market"],
            "parameter_bounds_checked": True,
            "parameter_drift_checked": True,
            "decision": "approve",
        }},
    }))

    publish_payload(
        suspicious, destination, model_dir, tmp_path / "audit" / "failure.json",
        approval_path=approval,
    )

    assert json.loads(destination.read_text())["model_tips"]["A vs B"]["p_home"] == 0.99


def test_manual_review_is_invalid_after_candidate_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')
    suspicious = _payload(model_tips={"A vs B": {"p_home": 0.99}})
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({
        "candidate_sha256": "0" * 64,
        "active_snapshot": "params_20260615.pkl",
        "approved_by": "reviewer",
        "approved_at": "2026-06-18T15:00:00Z",
        "reviews": {},
    }))

    with pytest.raises(PublishBlocked, match="candidate_sha256 mismatch"):
        publish_payload(
            suspicious, destination, model_dir, tmp_path / "audit" / "failure.json",
            approval_path=approval,
        )

    assert json.loads(destination.read_text()) == {"old": True}


def test_invalid_schema_does_not_replace_productive_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')

    with pytest.raises(PublishBlocked, match="schedule must be list"):
        publish_payload(_payload(schedule={}), destination, model_dir, tmp_path / "audit" / "failure.json")

    assert json.loads(destination.read_text()) == {"old": True}


def test_unvalidated_active_model_blocks_publish(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    snapshot = model_dir / "params_legacy.pkl"
    snapshot.write_bytes(b"model")
    (model_dir / "lifecycle.json").write_text(json.dumps({
        "version": 1,
        "active": snapshot.name,
        "snapshots": {snapshot.name: {"status": "active", "legacy_bootstrap": True}},
    }))
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')

    with pytest.raises(PublishBlocked, match="lacks complete evidence"):
        publish_payload(_payload(), destination, model_dir, tmp_path / "audit" / "failure.json")

    assert json.loads(destination.read_text()) == {"old": True}


def test_non_serializable_payload_does_not_replace_productive_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')

    with pytest.raises(PublishBlocked):
        publish_payload(
            _payload(all_odds={"bad": object()}), destination, model_dir,
            tmp_path / "audit" / "failure.json",
        )

    assert json.loads(destination.read_text()) == {"old": True}


def test_upload_failure_keeps_local_productive_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNALS_CLOUD_URL", "https://worker.test/signals.json")
    monkeypatch.setenv("SIGNALS_API_TOKEN", "secret")
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')

    with pytest.raises(PublishBlocked, match="Worker upload failed"):
        publish_payload(
            _payload(), destination, model_dir, tmp_path / "audit" / "failure.json",
            upload=lambda _: False,
        )

    assert json.loads(destination.read_text()) == {"old": True}


def test_shadow_mode_writes_only_shadow_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTSBRAIN_WRITER_MODE", "shadow")
    monkeypatch.delenv("SIGNALS_CLOUD_URL", raising=False)
    monkeypatch.delenv("SIGNALS_API_TOKEN", raising=False)
    model_dir = tmp_path / "models"
    _active_model(model_dir)
    destination = tmp_path / "signals.json"
    destination.write_text('{"old": true}')
    report = tmp_path / "audit" / "failure.json"

    publish_payload(_payload(), destination, model_dir, report)

    assert json.loads(destination.read_text()) == {"old": True}
    assert json.loads((report.parent / "shadow_signals.json").read_text()) == _payload()
