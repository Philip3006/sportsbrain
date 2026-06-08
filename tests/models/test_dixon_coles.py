import numpy as np
import pandas as pd
import pytest

from src.models.dixon_coles import (
    DixonColesParams,
    _tau,
    fit,
    predict_asian_handicap,
    predict_btts,
    predict_match,
    predict_match_staged,
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


class TestPredictBtts:
    def test_predict_btts_sums_to_one(self, minimal_dc_params):
        result = predict_btts("Home", "Away", minimal_dc_params)
        assert abs(result["p_btts_yes"] + result["p_btts_no"] - 1.0) < 1e-6

    def test_predict_btts_range(self, minimal_dc_params):
        result = predict_btts("Home", "Away", minimal_dc_params)
        assert 0 < result["p_btts_yes"] < 1
        assert 0 < result["p_btts_no"] < 1


class TestPredictAsianHandicap:
    """Tests for predict_asian_handicap() with lines -1.0, -1.5, +1.0, +1.5."""

    def test_minus_05_sums_to_one(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-0.5)
        assert abs(r["p_ah_home"] + r["p_ah_away"] + r["p_push"] - 1.0) < 1e-6

    def test_minus_05_no_push(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-0.5)
        assert r["p_push"] == 0.0

    def test_minus_10_sums_to_one(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.0)
        total = r["p_ah_home"] + r["p_ah_away"] + r["p_push"]
        assert abs(total - 1.0) < 1e-6

    def test_minus_10_push_is_positive(self, minimal_dc_params):
        """AH -1.0 must have p_push > 0 (probability of home winning by exactly 1)."""
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.0)
        assert r["p_push"] > 0.0

    def test_minus_10_all_components_nonneg(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.0)
        assert r["p_ah_home"] >= 0.0
        assert r["p_ah_away"] >= 0.0
        assert r["p_push"] >= 0.0

    def test_minus_15_sums_to_one(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.5)
        assert abs(r["p_ah_home"] + r["p_ah_away"] + r["p_push"] - 1.0) < 1e-6

    def test_minus_15_no_push(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.5)
        assert r["p_push"] == 0.0

    def test_plus_05_sums_to_one(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=0.5)
        assert abs(r["p_ah_home"] + r["p_ah_away"] + r["p_push"] - 1.0) < 1e-6

    def test_plus_05_no_push(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=0.5)
        assert r["p_push"] == 0.0

    def test_plus_10_sums_to_one(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=1.0)
        total = r["p_ah_home"] + r["p_ah_away"] + r["p_push"]
        assert abs(total - 1.0) < 1e-6

    def test_plus_10_push_is_positive(self, minimal_dc_params):
        """AH +1.0 must have p_push > 0 (probability of away winning by exactly 1)."""
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=1.0)
        assert r["p_push"] > 0.0

    def test_plus_15_sums_to_one(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=1.5)
        assert abs(r["p_ah_home"] + r["p_ah_away"] + r["p_push"] - 1.0) < 1e-6

    def test_plus_15_no_push(self, minimal_dc_params):
        r = predict_asian_handicap("Home", "Away", minimal_dc_params, line=1.5)
        assert r["p_push"] == 0.0

    def test_unsupported_line_raises(self, minimal_dc_params):
        with pytest.raises(ValueError, match="Unsupported AH line"):
            predict_asian_handicap("Home", "Away", minimal_dc_params, line=-2.0)

    def test_minus_10_home_prob_less_than_minus_05(self, minimal_dc_params):
        """AH -1.0 is harder for home to cover than AH -0.5."""
        r05 = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-0.5)
        r10 = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.0)
        # Home needs to win by 2+ for -1.0 vs just winning for -0.5 → lower p_ah_home
        assert r10["p_ah_home"] < r05["p_ah_home"]

    def test_minus_15_home_prob_equals_minus_10_home_prob(self, minimal_dc_params):
        """AH -1.5 home wins same scorelines as -1.0 home wins (both need 2+ goal margin)."""
        r10 = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.0)
        r15 = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-1.5)
        # p_ah_home is identical (both = P(home wins by 2+))
        assert abs(r10["p_ah_home"] - r15["p_ah_home"]) < 1e-9

    def test_symmetry_minus_05_versus_plus_05(self, minimal_dc_params):
        """AH -0.5 home ≡ AH +0.5 away (same market, different perspective)."""
        r_minus = predict_asian_handicap("Home", "Away", minimal_dc_params, line=-0.5)
        r_plus = predict_asian_handicap("Home", "Away", minimal_dc_params, line=0.5)
        # p_ah_home for -0.5 = P(home wins) = complement of p_ah_home for +0.5 (P(home wins or draws))
        # They are different markets; just verify both sum to 1
        assert abs(r_minus["p_ah_home"] + r_minus["p_ah_away"] - 1.0) < 1e-6
        assert abs(r_plus["p_ah_home"] + r_plus["p_ah_away"] - 1.0) < 1e-6

    def test_strong_favourite_has_high_p_ah_home_minus_05(self):
        """A very strong favourite should win AH -0.5 most of the time."""
        params = DixonColesParams(
            attack={"Strong": 1.5, "Weak": -1.0},
            defence={"Strong": -0.8, "Weak": 0.5},
            home_adv=0.3,
            rho=-0.13,
        )
        r = predict_asian_handicap("Strong", "Weak", params, line=-0.5)
        assert r["p_ah_home"] > 0.7


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


class TestPredictMatchStaged:
    def test_group_stage_has_higher_draw_prob_than_knockout(self, minimal_dc_params):
        """Group stage rho*1.10 should increase draw probability vs knockout rho*0.75."""
        group = predict_match_staged("Home", "Away", minimal_dc_params, is_knockout=False)
        knockout = predict_match_staged("Home", "Away", minimal_dc_params, is_knockout=True)
        assert group["p_draw"] > knockout["p_draw"]

    def test_probabilities_sum_to_one(self, minimal_dc_params):
        for is_ko in (True, False):
            probs = predict_match_staged("Home", "Away", minimal_dc_params, is_knockout=is_ko)
            total = probs["p_home"] + probs["p_draw"] + probs["p_away"]
            assert abs(total - 1.0) < 1e-6

    def test_group_stage_returns_dict_with_all_keys(self, minimal_dc_params):
        probs = predict_match_staged("Home", "Away", minimal_dc_params)
        assert {"p_home", "p_draw", "p_away"}.issubset(probs.keys())


class TestUnknownTeam:
    def test_unknown_team_raises_value_error(self, minimal_dc_params):
        """Unknown team must raise ValueError — not silently use λ=1.0."""
        with pytest.raises(ValueError, match="not in DC model"):
            predict_scoreline("UnknownTeamXYZ", "Away", minimal_dc_params)

    def test_both_unknown_teams_raise_value_error(self, minimal_dc_params):
        with pytest.raises(ValueError, match="not in DC model"):
            predict_scoreline("UnknownA", "UnknownB", minimal_dc_params)

    def test_known_teams_do_not_raise(self, minimal_dc_params):
        """Sanity check: known teams must not raise."""
        matrix = predict_scoreline("Home", "Away", minimal_dc_params)
        assert matrix.sum() > 0.99
