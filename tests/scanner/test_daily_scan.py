"""Tests for confederation-aware min_edge logic and model agreement in daily_scan."""

import numpy as np

from src.scanner.scoring import _confederation_min_edge, _count_model_agreement
from src.betting.value_detector import BetSignal


class TestConfederationMinEdge:
    def test_conmebol_away_gets_higher_edge(self):
        # Brazil is CONMEBOL — away market should be 1.5x
        result = _confederation_min_edge("Germany", "Brazil", "away", base_min_edge=0.03)
        assert abs(result - 0.045) < 1e-6  # 0.03 * 1.5 = 0.045

    def test_concacaf_away_gets_higher_edge(self):
        # Mexico is CONCACAF — away market should be 1.3x
        result = _confederation_min_edge("Germany", "Mexico", "away", base_min_edge=0.03)
        assert abs(result - 0.039) < 1e-6  # 0.03 * 1.3 = 0.039

    def test_uefa_away_unchanged(self):
        # France is UEFA — away market unchanged
        result = _confederation_min_edge("Germany", "France", "away", base_min_edge=0.03)
        assert abs(result - 0.03) < 1e-6

    def test_home_market_always_unchanged(self):
        # Home market is never boosted regardless of confederation
        result = _confederation_min_edge("Brazil", "Germany", "home", base_min_edge=0.03)
        assert abs(result - 0.03) < 1e-6

    def test_draw_market_always_unchanged(self):
        # Draw market is never boosted regardless of away team's confederation
        result = _confederation_min_edge("Germany", "Brazil", "draw", base_min_edge=0.03)
        assert abs(result - 0.03) < 1e-6

    def test_caf_away_unchanged(self):
        # CAF teams (e.g. Morocco) are not in the boost list — unchanged
        result = _confederation_min_edge("Germany", "Morocco", "away", base_min_edge=0.03)
        assert abs(result - 0.03) < 1e-6

    def test_unknown_team_unchanged(self):
        # Unknown team not in TEAM_CONFEDERATION — falls back to base
        result = _confederation_min_edge("Germany", "UnknownFC", "away", base_min_edge=0.03)
        assert abs(result - 0.03) < 1e-6

    def test_custom_base_min_edge_scaled_correctly(self):
        # Verify scaling works with a non-default base edge
        result = _confederation_min_edge("Germany", "Brazil", "away", base_min_edge=0.05)
        assert abs(result - 0.075) < 1e-6  # 0.05 * 1.5 = 0.075

    def test_argentina_conmebol_away_gets_higher_edge(self):
        # Argentina is also CONMEBOL
        result = _confederation_min_edge("France", "Argentina", "away", base_min_edge=0.03)
        assert abs(result - 0.045) < 1e-6

    def test_canada_concacaf_away_gets_higher_edge(self):
        # Canada is CONCACAF
        result = _confederation_min_edge("Germany", "Canada", "away", base_min_edge=0.03)
        assert abs(result - 0.039) < 1e-6


class TestCountModelAgreement:
    def _make_signal(self, market, model_prob, fair_prob, decimal_odds=2.0):
        return BetSignal(
            match_id="test", home="A", away="B",
            market=market,
            model_prob=model_prob, fair_prob=fair_prob,
            decimal_odds=decimal_odds, ev=0.10,
            kelly_f=0.05, stake_pct=0.01,
            confidence="MEDIUM", stake_eur=10.0,
        )

    def test_all_three_agree(self):
        # DC, Elo, LGBM all above fair_prob → 3/3
        signal = self._make_signal("home", model_prob=0.60, fair_prob=0.40)
        dc_probs = {"p_home": 0.55}  # above 0.40
        elo_prob = 0.55              # above 0.40
        lgbm_probs = np.array([0.10, 0.20, 0.70])  # home=index2=0.70 above 0.40
        result = _count_model_agreement(signal, dc_probs, elo_prob, lgbm_probs)
        assert result == 3

    def test_only_ensemble_agrees(self):
        # DC below, Elo below, LGBM below fair_prob → 0/3
        signal = self._make_signal("home", model_prob=0.60, fair_prob=0.40)
        dc_probs = {"p_home": 0.30}   # below 0.40
        elo_prob = 0.35               # below 0.40
        lgbm_probs = np.array([0.30, 0.40, 0.30])  # home=index2=0.30 below 0.40
        result = _count_model_agreement(signal, dc_probs, elo_prob, lgbm_probs)
        assert result == 0

    def test_two_of_three_agree(self):
        # DC and LGBM agree, Elo does not
        signal = self._make_signal("away", model_prob=0.40, fair_prob=0.30)
        dc_probs = {"p_away": 0.35}   # above 0.30
        elo_prob = 0.25               # below 0.30 — disagrees
        lgbm_probs = np.array([0.42, 0.28, 0.30])  # away=index0=0.42 above 0.30
        result = _count_model_agreement(signal, dc_probs, elo_prob, lgbm_probs)
        assert result == 2

    def test_draw_market(self):
        signal = self._make_signal("draw", model_prob=0.35, fair_prob=0.28)
        dc_probs = {"p_draw": 0.33}   # above 0.28
        elo_prob = 0.31               # above 0.28
        lgbm_probs = np.array([0.35, 0.36, 0.29])  # draw=index1=0.36 above 0.28
        result = _count_model_agreement(signal, dc_probs, elo_prob, lgbm_probs)
        assert result == 3

    def test_non_1x2_market_returns_zero(self):
        # AH and totals markets: _MODEL_IDX has no entry, so LGBM contributes 0.
        # DC key "p_ah-0.5_home" missing → 0.0 not > fair_prob.
        # Elo set below fair_prob so it also contributes 0 → total 0.
        signal = self._make_signal("ah-0.5_home", model_prob=0.55, fair_prob=0.50)
        dc_probs = {"p_home": 0.60}   # key "p_ah-0.5_home" missing → 0.0
        elo_prob = 0.45               # below fair_prob=0.50 — no agreement
        lgbm_probs = np.array([0.20, 0.25, 0.55])  # no _MODEL_IDX entry for market
        result = _count_model_agreement(signal, dc_probs, elo_prob, lgbm_probs)
        assert result == 0

    def test_missing_dc_key_counts_zero_for_dc(self):
        # If dc_probs doesn't have the key for the market, DC contributes 0
        signal = self._make_signal("home", model_prob=0.60, fair_prob=0.40)
        dc_probs = {}  # empty — "p_home" missing → 0.0 not > 0.40
        elo_prob = 0.55
        lgbm_probs = np.array([0.10, 0.20, 0.70])  # home=index2=0.70 above 0.40
        result = _count_model_agreement(signal, dc_probs, elo_prob, lgbm_probs)
        assert result == 2  # only Elo and LGBM
