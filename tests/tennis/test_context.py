"""J8-I9: Kontextfeatures (rest, altitude, timezone travel)."""
from __future__ import annotations

from src.tennis.context import (
    altitude_factor,
    lookup_venue,
    rest_days_between,
    tz_travel_penalty,
)


def test_lookup_venue_substring():
    m = lookup_venue("atp_bogota_open")
    assert m is not None
    assert m.altitude_m == 2640
    assert m.country == "CO"


def test_lookup_venue_unknown_returns_none():
    assert lookup_venue("atp_atlanta_open") is None
    assert lookup_venue("") is None


def test_rest_days_between_normal():
    assert rest_days_between("2026-08-01", "2026-08-08") == 7.0


def test_rest_days_between_missing_defaults_to_prior():
    assert rest_days_between(None, "2026-08-08") == 7.0
    assert rest_days_between("2026-08-01", None) == 7.0


def test_rest_days_capped_at_30():
    assert rest_days_between("2020-01-01", "2026-08-08") == 30.0


def test_altitude_factor_scales():
    bogota = lookup_venue("bogota")
    assert altitude_factor(bogota) == 1.0
    kitz = lookup_venue("kitzbuhel")
    assert altitude_factor(kitz) == 0.0  # 760m < 1000m Cutoff
    gst = lookup_venue("gstaad")
    assert 0.0 < altitude_factor(gst) < 0.1


def test_tz_travel_penalty():
    ausopen = lookup_venue("australian_open")
    # Europa → Melbourne (Δ=10)
    assert tz_travel_penalty(+1, ausopen) > 0.6
    # Melbourne-Home → 0
    assert tz_travel_penalty(+11, ausopen) == 0.0


def test_tz_penalty_none_venue_zero():
    assert tz_travel_penalty(0, None) == 0.0
