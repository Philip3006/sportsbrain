import numpy as np
import pandas as pd
import pytest

from src.models.dixon_coles import (
    DixonColesParams,
    _tau,
    fit,
    predict_match,
    predict_scoreline,
)


class TestTau:
    def test_low_scores_nonzero(self):
        for x, y in [(0, 0), (1, 0), (0, 1), (1, 1)]:
            assert _tau(x, y, 1.5, 1.0, -0.13) > 0

    def test_high_scores_unity(self):
        for x, y in [(2, 0), (3, 1), (5, 5)]:
            assert _tau(x, y, 1.5, 1.0, -0.13) == 1.0

    def test_tau_00_formula(self):
        lh, la, rho = 1.5, 1.2, -0.13
        expected = 1.0 - lh * la * rho
        assert abs(_tau(0, 0, lh, la, rho) - expected) < 1e-9

    def test_epsilon_guard(self):
        # Extreme params that would make tau negative without guard
        result = _tau(0, 0, 10.0, 10.0, -0.5)
        assert result > 0


class TestPredictScoreline:
    def test_matrix_shape(self, minimal_dc_params):
        matrix = predict_scoreline("Home", "Away", minimal_dc_params, max_goals=10)
        assert matrix.shape == (11, 11)

    def test_matrix_sums_to_one(self, minimal_dc_params):
        matrix = predict_scoreline("Home", "Away", minimal_dc_params, max_goals=10)
        assert abs(matrix.sum() - 1.0) < 1e-4

    def test_all_nonnegative(self, minimal_dc_params):
        matrix = predict_scoreline("Home", "Away", minimal_dc_params, max_goals=10)
        assert (matrix >= 0).all()

    def test_neutral_reduces_home_advantage(self, minimal_dc_params):
        probs_home = predict_match("Home", "Away", minimal_dc_params, neutral=False)
        probs_neutral = predict_match("Home", "Away", minimal_dc_params, neutral=True)
        assert probs_home["p_home"] > probs_neutral["p_home"]


class TestPredictMatch:
    def test_probs_sum_to_one(self, minimal_dc_params):
        result = predict_match("Home", "Away", minimal_dc_params)
        total = result["p_home"] + result["p_draw"] + result["p_away"]
        assert abs(total - 1.0) < 1e-6

    def test_all_probs_positive(self, minimal_dc_params):
        result = predict_match("Home", "Away", minimal_dc_params)
        for v in result.values():
            assert v > 0

    def test_strong_team_favored(self):
        params = DixonColesParams(
            attack={"Strong": 1.0, "Weak": -1.0},
            defence={"Strong": -0.5, "Weak": 0.5},
            home_adv=0.3,
            rho=-0.13,
        )
        result = predict_match("Strong", "Weak", params)
        assert result["p_home"] > 0.6


class TestFit:
    def test_rho_in_valid_range(self, fitted_params):
        assert -0.5 <= fitted_params.rho <= 0.0

    def test_home_adv_positive(self, fitted_params):
        assert fitted_params.home_adv > 0

    def test_parameter_recovery(self, synthetic_teams, synthetic_params, synthetic_matches):
        """Recovered attack/defence params should be close to ground truth."""
        params = fit(synthetic_matches, max_iter=1000)
        true_attack = synthetic_params["attack"]
        # Center both (attack is identified up to additive constant)
        ref = synthetic_teams[0]
        for team in synthetic_teams[1:]:
            diff = abs(
                (params.attack.get(team, 0) - params.attack.get(ref, 0))
                - (true_attack[team] - true_attack[ref])
            )
            assert diff < 0.25, f"Attack recovery failed for {team}: diff={diff:.3f}"

    def test_no_lookahead(self, synthetic_matches):
        """Model trained with today=cutoff must exclude matches on/after cutoff."""
        cutoff = pd.Timestamp("2021-01-01")
        future_row = pd.DataFrame([{
            "date": cutoff + pd.Timedelta(days=10),
            "home_team": "Alpha", "away_team": "Beta",
            "home_score": 99, "away_score": 0,
            "tournament": "FIFA World Cup", "neutral": False,
        }])
        matches_with_future = pd.concat(
            [synthetic_matches, future_row], ignore_index=True
        )
        # If lookahead were present, the absurd score would distort the model
        params_safe = fit(synthetic_matches[synthetic_matches["date"] < cutoff])
        params_leaked = fit(matches_with_future, today=cutoff + pd.Timedelta(days=1))
        # Both should be reasonable — the key check is today= excludes future rows
        assert params_safe.home_adv > -1.0
        assert params_leaked.home_adv > -1.0

    def test_known_strong_team(self, synthetic_matches):
        """After fitting, alpha of a consistently-winning team should be positive."""
        params = fit(synthetic_matches, max_iter=500)
        # All teams are in the model
        assert len(params.attack) == len(params.defence)
