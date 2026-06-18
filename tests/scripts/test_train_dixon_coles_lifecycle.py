import json

import pandas as pd

import scripts.train_dixon_coles as train
from src.models.dixon_coles import DixonColesParams
from src.models.lifecycle import activate, mark_validated, register_trained


def test_load_active_params_returns_newest_snapshot(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    dc_dir = models_dir / "dixon_coles"
    dc_dir.mkdir(parents=True)
    older = dc_dir / "params_20260615.pkl"
    newer = dc_dir / "params_20260618.pkl"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    monkeypatch.setattr(train, "MODELS_DIR", models_dir)
    monkeypatch.setattr(train.dixon_coles, "load", lambda path: path.read_bytes())

    path, payload = train._load_active_params()

    assert path == newer
    assert payload == b"newer"


def test_candidate_paths_never_overwrite_same_day_artifact(tmp_path):
    first_params, first_elo = train._next_candidate_paths(tmp_path, pd.Timestamp("2026-06-18"))
    first_params.write_bytes(b"model")
    first_elo.write_text(json.dumps({"A": 1500}))

    second_params, second_elo = train._next_candidate_paths(tmp_path, pd.Timestamp("2026-06-18"))

    assert first_params.name == "params_20260618_candidate01.pkl"
    assert first_elo.name == "elo_20260618_candidate01.json"
    assert second_params.name == "params_20260618_candidate02.pkl"
    assert second_elo.name == "elo_20260618_candidate02.json"


def test_candidate_issues_include_drift_and_optimizer_bounds():
    prior = DixonColesParams(
        attack={"A": 0.0}, defence={"A": 0.0}, home_adv=0.2, rho=-0.1,
        fit_date=pd.Timestamp("2026-06-17"),
    )
    candidate = DixonColesParams(
        attack={"A": 2.0}, defence={"A": 0.0}, home_adv=0.2, rho=-0.50,
        fit_date=pd.Timestamp("2026-06-18"),
    )

    issues = train._candidate_issues(candidate, prior)

    assert any("A.attack drift" in issue for issue in issues)
    assert any("rho optimizer bound hit" in issue for issue in issues)


def test_retry_schedule_keeps_quarter_boost_as_last_safe_attempt():
    source = (train.Path(train.__file__)).read_text()
    assert "boost_schedule = [None, 1.0, 0.75, 0.5, 0.25]" in source
