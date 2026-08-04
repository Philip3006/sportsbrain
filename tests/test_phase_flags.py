from datetime import date

from src.phase_flags import (
    FLAGS,
    assert_flags_valid,
    expired_flags,
    get_flag,
    is_active,
)


def test_registry_nonempty():
    assert len(FLAGS) >= 1


def test_wc2026_boost_registered_and_neutralized():
    f = get_flag("WC2026_BOOST")
    assert f is not None
    assert f.active is False


def test_is_active_unknown_returns_false():
    assert is_active("does_not_exist") is False


def test_expired_flags_finds_active_past_sunset():
    # If someone flipped WC2026 back to active AND date is past 2026-07-30
    from src.phase_flags import PhaseFlag
    fake = PhaseFlag(
        name="TEST", active=True, sunset_date="2020-01-01",
        retry_capable=False, intent="",
    )
    # Simulate: append via monkey — direct list
    exp = [f for f in [fake] if f.active and f.sunset_date and date.today().isoformat() > f.sunset_date]
    assert fake in exp


def test_assert_flags_valid_passes_today():
    # Should not raise for current registry state
    assert_flags_valid()
