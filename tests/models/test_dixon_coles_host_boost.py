"""I6 — Host-Boost (WM-2026 Gastgeber USA/CAN/MEX).

Tests, dass host_boost λ_home multiplikativ erhöht, λ_away unverändert lässt
und die Wahrscheinlichkeits-Marginalien in plausible Richtung verschieben.
"""
import pytest

from src.models.dixon_coles import (
    _lambdas,
    predict_match,
    predict_scoreline,
    predict_totals,
    predict_btts,
    predict_xg,
)


class TestHostBoostLambdas:
    def test_default_no_change(self, minimal_dc_params):
        lh1, la1 = _lambdas("Home", "Away", minimal_dc_params, neutral=True)
        lh2, la2 = _lambdas("Home", "Away", minimal_dc_params, neutral=True, host_boost=1.0)
        assert lh1 == pytest.approx(lh2)
        assert la1 == pytest.approx(la2)

    def test_boost_multiplies_lh_only(self, minimal_dc_params):
        lh_base, la_base = _lambdas("Home", "Away", minimal_dc_params, neutral=True)
        lh_boost, la_boost = _lambdas(
            "Home", "Away", minimal_dc_params, neutral=True, host_boost=1.05
        )
        assert lh_boost == pytest.approx(lh_base * 1.05, rel=1e-6)
        assert la_boost == pytest.approx(la_base, rel=1e-6)


class TestHostBoostPredictMatch:
    def test_boost_increases_p_home(self, minimal_dc_params):
        p_base = predict_match("Home", "Away", minimal_dc_params, neutral=True)
        p_boost = predict_match("Home", "Away", minimal_dc_params, neutral=True, host_boost=1.05)
        assert p_boost["p_home"] > p_base["p_home"]
        assert p_boost["p_away"] < p_base["p_away"]
        # Probs still normalized
        assert sum(p_boost.values()) == pytest.approx(1.0, abs=1e-6)


class TestHostBoostMarketPropagation:
    """Sicher stellen, dass host_boost durch alle Markt-Funktionen propagiert."""

    def test_scoreline_matrix_still_normalized(self, minimal_dc_params):
        m = predict_scoreline("Home", "Away", minimal_dc_params, neutral=True, host_boost=1.05)
        assert m.sum() == pytest.approx(1.0, abs=1e-6)

    def test_totals_over_increases_with_boost(self, minimal_dc_params):
        base = predict_totals("Home", "Away", minimal_dc_params, line=2.5, neutral=True)
        boost = predict_totals(
            "Home", "Away", minimal_dc_params, line=2.5, neutral=True, host_boost=1.05
        )
        # mehr Tore beim Host → P(over) muss steigen
        assert boost["p_over"] > base["p_over"]

    def test_btts_increases_with_boost(self, minimal_dc_params):
        base = predict_btts("Home", "Away", minimal_dc_params, neutral=True)
        boost = predict_btts("Home", "Away", minimal_dc_params, neutral=True, host_boost=1.05)
        # Wenn nur lh hoch geht, steigt P(btts_yes) (Heim trifft öfter)
        assert boost["p_btts_yes"] > base["p_btts_yes"]

    def test_xg_home_higher_with_boost(self, minimal_dc_params):
        xh_base, xa_base = predict_xg("Home", "Away", minimal_dc_params, neutral=True)
        xh_boost, xa_boost = predict_xg(
            "Home", "Away", minimal_dc_params, neutral=True, host_boost=1.05
        )
        assert xh_boost == pytest.approx(xh_base * 1.05, rel=1e-6)
        assert xa_boost == pytest.approx(xa_base, rel=1e-6)


class TestHostBoostConfig:
    def test_config_constants_defined(self):
        from src.config import HOST_BOOST_ENABLED, HOST_LAMBDA_BOOST, HOST_NATIONS
        assert HOST_NATIONS == {"United States", "Canada", "Mexico"}
        assert 1.0 <= HOST_LAMBDA_BOOST <= 1.15
        assert isinstance(HOST_BOOST_ENABLED, bool)
