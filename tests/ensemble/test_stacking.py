"""Phase 2.1 — Stacker meta-learner unit tests."""
import numpy as np
import pytest

from src.ensemble.stacking import (
    Stacker,
    build_stacker_features,
    feature_columns,
)


def _balanced_y(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 3, size=n)


def _synthetic_X(n: int, seed: int = 7) -> np.ndarray:
    """Random features in the [0, 1] simplex per group."""
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        dc = rng.dirichlet([1.0, 1.0, 1.0])  # [a, d, h]
        lg = rng.dirichlet([1.0, 1.0, 1.0])
        sh = rng.dirichlet([1.0, 1.0, 1.0])  # [a, d, h] from shin
        rows.append(build_stacker_features(
            dc_probs={"p_home": dc[2], "p_draw": dc[1], "p_away": dc[0]},
            lgbm_probs=np.array([lg[0], lg[1], lg[2]]),
            shin_probs=(sh[2], sh[1], sh[0]),
            is_knockout=bool(rng.integers(0, 2)),
            is_neutral=bool(rng.integers(0, 2)),
        ))
    return np.vstack(rows)


class TestFeatureBuilder:
    def test_feature_length_matches_columns(self):
        dc = {"p_home": 0.4, "p_draw": 0.3, "p_away": 0.3}
        feat = build_stacker_features(dc, None, None)
        assert feat.shape == (len(feature_columns()),)

    def test_lgbm_none_falls_back_to_dc(self):
        dc = {"p_home": 0.4, "p_draw": 0.3, "p_away": 0.3}
        feat = build_stacker_features(dc, lgbm_probs=None, shin_probs=None)
        # LGBM positions 3,4,5 should equal DC positions 0,1,2
        assert feat[3] == feat[0]
        assert feat[4] == feat[1]
        assert feat[5] == feat[2]

    def test_shin_none_uses_uniform(self):
        dc = {"p_home": 0.4, "p_draw": 0.3, "p_away": 0.3}
        feat = build_stacker_features(dc, None, None)
        # Shin positions 6,7,8 should all be ~1/3
        assert abs(feat[6] - 1.0 / 3) < 1e-9
        assert abs(feat[7] - 1.0 / 3) < 1e-9
        assert abs(feat[8] - 1.0 / 3) < 1e-9

    def test_divergence_terms(self):
        dc = {"p_home": 0.6, "p_draw": 0.2, "p_away": 0.2}
        shin = (0.4, 0.3, 0.3)
        feat = build_stacker_features(dc, None, shin)
        # dc_vs_shin_home = dc.p_home - shin.p_home = 0.6 - 0.4 = 0.2
        assert abs(feat[11] - 0.2) < 1e-9


class TestStackerFit:
    def test_fit_and_predict_shape(self):
        X = _synthetic_X(80)
        y = _balanced_y(80)
        s = Stacker().fit(X, y)
        proba = s.predict_proba(X)
        assert proba.shape == (80, 3)
        assert np.allclose(proba.sum(axis=1), 1.0, atol=1e-9)

    def test_predict_one_returns_dict(self):
        X = _synthetic_X(60)
        y = _balanced_y(60)
        s = Stacker().fit(X, y)
        out = s.predict_one(
            dc_probs={"p_home": 0.5, "p_draw": 0.3, "p_away": 0.2},
            shin_probs=(0.45, 0.30, 0.25),
        )
        assert set(out.keys()) == {"p_home", "p_draw", "p_away"}
        assert abs(sum(out.values()) - 1.0) < 1e-9

    def test_save_load_round_trip(self, tmp_path):
        X = _synthetic_X(50)
        y = _balanced_y(50)
        s = Stacker().fit(X, y)
        path = tmp_path / "stacker.pkl"
        s.save(path)
        s2 = Stacker.load(path)
        out1 = s.predict_proba(X[:5])
        out2 = s2.predict_proba(X[:5])
        np.testing.assert_allclose(out1, out2)

    def test_predict_before_fit_raises(self):
        s = Stacker()
        with pytest.raises(RuntimeError):
            s.predict_proba(np.zeros((1, len(feature_columns()))))

    def test_wrong_feature_count_raises(self):
        s = Stacker()
        bad = np.zeros((5, 3))
        with pytest.raises(ValueError):
            s.fit(bad, np.array([0, 1, 2, 0, 1]))


class TestStackerOnSeparableData:
    """If the home probability strongly predicts outcome, the stacker should
    converge to picking 'home' as the most likely class."""

    def test_learns_dominant_signal(self):
        rng = np.random.default_rng(11)
        rows = []
        labels = []
        for _ in range(300):
            p_h = rng.uniform(0.1, 0.9)
            p_d = (1 - p_h) * rng.uniform(0.2, 0.8)
            p_a = max(0.0, 1 - p_h - p_d)
            dc = {"p_home": p_h, "p_draw": p_d, "p_away": p_a}
            rows.append(build_stacker_features(dc, None, None))
            # outcome biased toward p_home
            r = rng.uniform(0, 1)
            if r < p_h:
                labels.append(2)
            elif r < p_h + p_d:
                labels.append(1)
            else:
                labels.append(0)
        X = np.vstack(rows)
        y = np.array(labels)
        s = Stacker().fit(X, y)
        # On a case where p_home=0.9, the stacker should give p_home > 0.5.
        dc = {"p_home": 0.9, "p_draw": 0.06, "p_away": 0.04}
        out = s.predict_one(dc)
        assert out["p_home"] > 0.5
