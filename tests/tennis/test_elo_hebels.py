from src.tennis.elo_hebels import bayesian_dampen, altitude_adjust, apply_elo_hebels


def test_bayesian_full_confidence_when_enough_history():
    assert bayesian_dampen(0.75, 20, 20) == 0.75
    assert bayesian_dampen(0.30, 50, 50) == 0.30


def test_bayesian_dampens_when_low_history():
    p = bayesian_dampen(0.80, 5, 5, n_ref=20)
    assert 0.5 < p < 0.80  # gezogen gegen 0.5


def test_bayesian_uses_min_of_two_counts():
    p_low = bayesian_dampen(0.80, 2, 100, n_ref=20)
    p_high = bayesian_dampen(0.80, 100, 2, n_ref=20)
    assert abs(p_low - p_high) < 1e-9


def test_altitude_no_shift_for_sea_level():
    assert altitude_adjust(0.60, "wimbledon") == 0.60


def test_altitude_shifts_for_bogota_with_serve_bias():
    p_low_serve = altitude_adjust(0.50, "atp_bogota", serve_bias_a=0.0, serve_bias_b=0.0)
    p_high_serve = altitude_adjust(0.50, "atp_bogota", serve_bias_a=1.0, serve_bias_b=-1.0)
    assert p_low_serve == 0.50
    assert p_high_serve > 0.50


def test_altitude_capped():
    p = altitude_adjust(0.50, "quito", serve_bias_a=10.0, serve_bias_b=-10.0, max_shift=0.03)
    assert p <= 0.53


def test_full_pipeline_neutral_when_flags_off():
    assert apply_elo_hebels(0.60, enable_bayesian=False, enable_altitude=False) == 0.60
