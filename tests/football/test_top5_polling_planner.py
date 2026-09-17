from dataclasses import replace

import pytest

from src.football.production_contracts import ProductionContractError
from src.football.top5_polling_planner import (
    PollingPlanStatus,
    SignalTimeWindow,
    Top5PollingPlannerInputs,
    plan_top5_polling,
)


def _inputs(**overrides):
    base = Top5PollingPlannerInputs(
        daily_datapoint_budget=100,
        monthly_datapoint_budget=3000,
        reserve_percentage=0.10,
        active_fixtures=10,
        bookmakers=2,
        outcomes=3,
        observed_datapoints_per_request=1,
        observed_datapoints_per_fixture=0.10,
        desired_signal_time_windows=(SignalTimeWindow("signal", 120, 0, 10),),
        polling_frequency_seconds=3600,
    )
    return replace(base, **overrides)


def test_cost_uses_runtime_request_fixture_bookmaker_and_outcome_inputs():
    inputs = _inputs(
        active_fixtures=20,
        bookmakers=3,
        outcomes=3,
        observed_datapoints_per_request=2,
        observed_datapoints_per_fixture=0.25,
    )
    assert inputs.estimated_datapoints_per_snapshot == pytest.approx(47.0)


def test_windows_concentrate_polls_and_ignore_far_future_zero_fixture_window():
    inputs = _inputs(
        desired_signal_time_windows=(
            SignalTimeWindow("far-future", 1440, 720, 0),
            SignalTimeWindow("signal", 120, 0, 10),
        ),
        polling_frequency_seconds=3600,
    )
    plan = plan_top5_polling(inputs)
    assert plan.status is PollingPlanStatus.SAFE
    assert plan.windows[0].snapshots_per_day == 0
    assert plan.windows[1].snapshots_per_day == 2
    assert plan.estimated_daily_datapoints == pytest.approx(14.0)


def test_safe_plan_preserves_runtime_reserve():
    plan = plan_top5_polling(_inputs())
    assert plan.status is PollingPlanStatus.SAFE
    assert plan.estimated_datapoints_per_snapshot == pytest.approx(7.0)
    assert plan.estimated_daily_datapoints == pytest.approx(14.0)
    assert plan.estimated_monthly_datapoints == pytest.approx(420.0)
    assert plan.remaining_daily_quota == pytest.approx(86.0)
    assert plan.remaining_monthly_quota == pytest.approx(2580.0)
    assert plan.remaining_daily_reserve == pytest.approx(10.0)
    assert plan.remaining_monthly_reserve == pytest.approx(300.0)
    assert plan.safe_max_snapshots_per_day == 12
    assert plan.safe_max_snapshots_per_month == 385
    assert plan.safe_max_snapshots == 12


def test_degraded_plan_is_within_full_budget_but_consumes_daily_reserve():
    inputs = _inputs(
        daily_datapoint_budget=16,
        monthly_datapoint_budget=600,
        reserve_percentage=0.25,
        observed_datapoints_per_fixture=0.06,
        desired_signal_time_windows=(SignalTimeWindow("signal", 60, 0, 10),),
        polling_frequency_seconds=1200,
    )
    plan = plan_top5_polling(inputs)
    assert plan.status is PollingPlanStatus.DEGRADED
    assert plan.estimated_daily_datapoints == pytest.approx(13.8)
    assert plan.remaining_daily_reserve == pytest.approx(2.2)
    assert plan.minimum_safe_polling_interval_seconds == 1800


def test_unsafe_plan_exceeds_full_budget_but_reports_a_slower_safe_interval():
    inputs = _inputs(
        daily_datapoint_budget=10,
        monthly_datapoint_budget=300,
        reserve_percentage=0.20,
        observed_datapoints_per_fixture=0.06,
        desired_signal_time_windows=(SignalTimeWindow("signal", 60, 0, 10),),
        polling_frequency_seconds=1200,
    )
    plan = plan_top5_polling(inputs)
    assert plan.status is PollingPlanStatus.UNSAFE
    assert plan.estimated_daily_datapoints == pytest.approx(13.8)
    assert plan.minimum_safe_polling_interval_seconds == 3600


def test_exhaustion_projection_uses_runtime_monthly_budget_and_days():
    inputs = _inputs(
        monthly_datapoint_budget=1000,
        days_in_billing_month=20,
    )
    plan = plan_top5_polling(inputs)
    assert plan.projected_quota_exhaustion_days == pytest.approx(64.285714)
    assert plan.projected_full_quota_exhaustion_days == pytest.approx(71.428571)


def test_no_window_coverage_is_degraded_and_visible():
    inputs = _inputs(
        desired_signal_time_windows=(SignalTimeWindow("future", 1440, 720, 0),),
    )
    plan = plan_top5_polling(inputs)
    assert plan.status is PollingPlanStatus.DEGRADED
    assert plan.coverage_warning is not None
    assert plan.minimum_safe_polling_interval_seconds is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_datapoint_budget", 0),
        ("monthly_datapoint_budget", -1),
        ("reserve_percentage", 1.0),
        ("active_fixtures", -1),
        ("bookmakers", -1),
        ("outcomes", -1),
        ("observed_datapoints_per_fixture", -0.1),
        ("polling_frequency_seconds", 0),
    ],
)
def test_invalid_runtime_inputs_fail_closed(field, value):
    with pytest.raises(ProductionContractError):
        plan_top5_polling(replace(_inputs(), **{field: value}))


def test_window_fixture_count_cannot_exceed_active_scope():
    with pytest.raises(ProductionContractError):
        plan_top5_polling(
            _inputs(
                desired_signal_time_windows=(SignalTimeWindow("signal", 60, 0, 11),),
            )
        )


def test_payload_is_deterministic_and_contains_status_and_window_breakdown():
    plan = plan_top5_polling(_inputs())
    assert plan.as_payload() == plan.as_payload()
    assert plan.as_payload()["status"] == "SAFE"
    assert plan.as_payload()["windows"][0]["snapshots_per_day"] == 2
