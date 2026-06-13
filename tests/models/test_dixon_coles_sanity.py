"""Sanity-acceptance gate for DC params snapshots (docs/audit_2026-06-12.md, A).

Prevents a future retrain from silently shipping a corrupted snapshot like
params_20260612.pkl (Mexico.defence=-4.04, Botswana.attack=-5.68).
"""
import pickle
from copy import deepcopy

import pandas as pd
import pytest

from src.models import dixon_coles
from src.models.dixon_coles import DixonColesParams, validate_params, save


def _make_clean_params() -> DixonColesParams:
    return DixonColesParams(
        attack={"A": 0.5, "B": -0.2, "C": 1.1},
        defence={"A": -0.4, "B": 0.1, "C": -1.0},
        home_adv=0.25,
        rho=-0.13,
        fit_date=pd.Timestamp("2026-06-13"),
    )


class TestValidateParamsRange:
    def test_clean_params_no_issues(self):
        assert validate_params(_make_clean_params()) == []

    def test_attack_outlier_detected(self):
        p = _make_clean_params()
        p.attack["B"] = -5.68  # Botswana-style outlier
        issues = validate_params(p)
        assert any("B.attack" in i and "out of range" in i for i in issues)

    def test_defence_outlier_detected(self):
        p = _make_clean_params()
        p.defence["A"] = -4.04  # Mexico-style outlier
        issues = validate_params(p)
        assert any("A.defence" in i and "out of range" in i for i in issues)
        assert any("-4.040" in i for i in issues)

    def test_home_adv_outlier(self):
        p = _make_clean_params()
        p.home_adv = 1.5
        issues = validate_params(p)
        assert any("home_adv" in i for i in issues)

    def test_rho_outlier(self):
        p = _make_clean_params()
        p.rho = -0.7
        issues = validate_params(p)
        assert any("rho" in i for i in issues)


class TestValidateParamsDrift:
    def test_no_prior_no_drift_check(self):
        p = _make_clean_params()
        p.attack["A"] = 2.4  # still in range, no prior
        assert validate_params(p, prior=None) == []

    def test_drift_within_threshold_ok(self):
        prior = _make_clean_params()
        curr = deepcopy(prior)
        curr.attack["A"] = prior.attack["A"] + 1.0  # under 1.5
        assert validate_params(curr, prior=prior) == []

    def test_drift_exceeds_threshold(self):
        prior = _make_clean_params()
        curr = deepcopy(prior)
        curr.attack["A"] = prior.attack["A"] + 2.0  # over 1.5
        issues = validate_params(curr, prior=prior)
        assert any("A.attack drift" in i for i in issues)

    def test_new_team_no_drift_issue(self):
        # A team that only appears in the new snapshot must not raise a drift
        # issue (no prior value to compare against).
        prior = _make_clean_params()
        curr = deepcopy(prior)
        curr.attack["D_new"] = 0.3
        curr.defence["D_new"] = -0.1
        assert validate_params(curr, prior=prior) == []


class TestSaveGate:
    def test_save_clean_ok(self, tmp_path):
        out = tmp_path / "snap.pkl"
        save(_make_clean_params(), out)
        assert out.exists()
        with open(out, "rb") as f:
            loaded = pickle.load(f)
        assert isinstance(loaded, DixonColesParams)

    def test_save_poisoned_raises(self, tmp_path):
        p = _make_clean_params()
        p.defence["A"] = -4.04
        out = tmp_path / "bad.pkl"
        with pytest.raises(ValueError, match="A.defence"):
            save(p, out)
        assert not out.exists()  # nothing written on failure

    def test_save_force_overrides(self, tmp_path, capsys):
        p = _make_clean_params()
        p.defence["A"] = -4.04
        out = tmp_path / "forced.pkl"
        save(p, out, force=True)
        assert out.exists()
        captured = capsys.readouterr()
        assert "force=True" in captured.out
        assert "A.defence" in captured.out

    def test_save_with_prior_drift_blocks(self, tmp_path):
        prior = _make_clean_params()
        curr = deepcopy(prior)
        curr.attack["A"] = prior.attack["A"] + 2.0
        out = tmp_path / "drift.pkl"
        with pytest.raises(ValueError, match="drift"):
            save(curr, out, prior=prior)


class TestCurrentSnapshotHealthy:
    """Smoke test: the snapshot we ship right now must pass the gate.

    If this fails, either the snapshot drifted or the gate is too tight —
    investigate before merging.
    """
    def test_latest_snapshot_passes(self):
        snap_dir = dixon_coles.__file__  # type: ignore
        # locate snapshots dir
        from src.config import MODELS_DIR
        d = MODELS_DIR / "dixon_coles"
        snaps = sorted(d.glob("params_*.pkl"))
        if not snaps:
            pytest.skip("no snapshot present")
        latest = snaps[-1]
        with open(latest, "rb") as f:
            params = pickle.load(f)
        issues = validate_params(params)
        assert issues == [], f"{latest.name} has issues: {issues}"
