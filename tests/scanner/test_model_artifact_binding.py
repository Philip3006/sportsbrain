import json

from src.scanner import daily_scan


def _active_model(models_dir):
    dc_dir = models_dir / "dixon_coles"
    dc_dir.mkdir(parents=True)
    (dc_dir / "params_active.pkl").write_bytes(b"model")
    (dc_dir / "lifecycle.json").write_text(json.dumps({
        "version": 1,
        "active": "params_active.pkl",
        "snapshots": {"params_active.pkl": {
            "status": "active",
            "evidence": {"backtest": True, "bounds": True, "drift": True, "audit": True},
        }},
    }))


def test_lgbm_gate_passes_regardless_of_dc_snapshot(tmp_path, monkeypatch):
    _active_model(tmp_path)
    lgbm = tmp_path / "lgbm"
    lgbm.mkdir()
    monkeypatch.setattr(daily_scan, "MODELS_DIR", tmp_path)
    (lgbm / "gate.json").write_text(json.dumps({
        "passed": True, "dc_snapshot": "params_other.pkl", "dc_weight": 0.4,
    }))

    gate = daily_scan._load_lgbm_gate()

    assert gate["passed"] is True


def test_lgbm_gate_accepts_active_dc_snapshot(tmp_path, monkeypatch):
    _active_model(tmp_path)
    lgbm = tmp_path / "lgbm"
    lgbm.mkdir()
    monkeypatch.setattr(daily_scan, "MODELS_DIR", tmp_path)
    (lgbm / "gate.json").write_text(json.dumps({
        "passed": True, "dc_snapshot": "params_active.pkl", "dc_weight": 0.4,
    }))

    assert daily_scan._load_lgbm_gate()["passed"] is True


def test_stacker_without_active_snapshot_binding_is_disabled(tmp_path, monkeypatch):
    _active_model(tmp_path)
    lgbm = tmp_path / "lgbm"
    lgbm.mkdir()
    monkeypatch.setattr(daily_scan, "MODELS_DIR", tmp_path)
    (lgbm / "stacker.pkl").write_bytes(b"not loaded when metadata mismatches")
    (lgbm / "stacker_features.json").write_text(json.dumps({"dc_snapshot": "params_other.pkl"}))

    assert daily_scan._load_stacker() is None
