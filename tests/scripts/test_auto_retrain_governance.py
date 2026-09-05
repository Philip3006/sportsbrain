"""Scheduled retraining must observe model freshness without persisting models."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd


def _results(date: str) -> pd.DataFrame:
    return pd.DataFrame(
        [{"tournament": "FIFA World Cup", "date": pd.Timestamp(date), "home_score": 1}]
    )


def _configure(monkeypatch, results: pd.DataFrame):
    from scripts import auto_retrain

    monkeypatch.setattr(
        auto_retrain,
        "_load_latest_dc_params",
        lambda: SimpleNamespace(fit_date=pd.Timestamp("2026-09-05")),
    )
    monkeypatch.setattr(auto_retrain, "fetch_international_results", lambda **_kwargs: results)
    return auto_retrain


def test_observe_only_no_new_matches_exits_cleanly_without_training(monkeypatch):
    auto_retrain = _configure(monkeypatch, _results("2026-09-04"))
    calls: list[object] = []
    monkeypatch.setattr(auto_retrain, "_run_retraining", lambda *_args: calls.append("train"))

    assert auto_retrain.main(observe_only=True) == 0
    assert calls == []


def test_observe_only_blocks_all_model_training_when_retrain_is_required(monkeypatch, tmp_path):
    auto_retrain = _configure(monkeypatch, _results("2026-09-06"))
    calls: list[object] = []
    monkeypatch.setattr(auto_retrain, "_run_retraining", lambda *_args: calls.append("dc/lgbm/stacker"))
    monkeypatch.setattr(auto_retrain, "MODELS_DIR", tmp_path / "models")

    assert auto_retrain.main(observe_only=True) == auto_retrain.OBSERVE_ONLY_RETRAIN_REQUIRED_EXIT
    assert calls == []
    assert not (tmp_path / "models").exists()


def test_manual_mode_retains_the_existing_retraining_path(monkeypatch):
    auto_retrain = _configure(monkeypatch, _results("2026-09-06"))
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(auto_retrain, "_run_retraining", lambda *args: calls.append(args))

    assert auto_retrain.main() == 0
    assert len(calls) == 1


def test_scheduled_wrapper_invokes_observe_only_mode():
    from pathlib import Path

    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "auto_retrain_cron.sh"
    assert "scripts/auto_retrain.py --observe-only" in wrapper.read_text()
