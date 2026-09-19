"""Quota-safe, provider-neutral Top-5 polling planning.

The planner describes a possible candidate-shadow schedule only.  It has no
network, scheduler, provider, publication, betting, or quota-purchase side
effects.  All account limits and observed costs are supplied at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import ceil, floor, isfinite

from src.football.production_contracts import ProductionContractError


class PollingPlanStatus(str, Enum):
    SAFE = "SAFE"
    DEGRADED = "DEGRADED"
    UNSAFE = "UNSAFE"


@dataclass(frozen=True)
class SignalTimeWindow:
    """A non-empty window measured before kickoff.

    ``start_minutes_before_kickoff`` is the earlier point in the window and
    ``end_minutes_before_kickoff`` is the later point.  ``fixture_count`` is
    the exact number of fixtures that a snapshot in this window covers; zero
    deliberately means no request for far-future or otherwise out-of-scope
    fixtures.
    """

    name: str
    start_minutes_before_kickoff: int
    end_minutes_before_kickoff: int
    fixture_count: int

    @property
    def duration_seconds(self) -> int:
        return (
            self.start_minutes_before_kickoff - self.end_minutes_before_kickoff
        ) * 60

    def validate(self, active_fixtures: int) -> None:
        if not self.name.strip():
            raise ProductionContractError("signal-time window requires a name")
        values = (
            self.start_minutes_before_kickoff,
            self.end_minutes_before_kickoff,
            self.fixture_count,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) for value in values
        ):
            raise ProductionContractError("signal-time window fields must be integers")
        if self.start_minutes_before_kickoff <= self.end_minutes_before_kickoff:
            raise ProductionContractError(
                "signal-time window must have positive duration"
            )
        if self.end_minutes_before_kickoff < 0:
            raise ProductionContractError(
                "signal-time window cannot extend after kickoff"
            )
        if self.fixture_count < 0 or self.fixture_count > active_fixtures:
            raise ProductionContractError(
                "signal-time window fixture count exceeds active fixtures"
            )


@dataclass(frozen=True)
class Top5PollingPlannerInputs:
    """Runtime assumptions for one candidate-shadow polling plan."""

    daily_datapoint_budget: float
    monthly_datapoint_budget: float
    reserve_percentage: float
    active_fixtures: int
    bookmakers: int
    outcomes: int
    observed_datapoints_per_request: float
    observed_datapoints_per_fixture: float
    desired_signal_time_windows: tuple[SignalTimeWindow, ...]
    polling_frequency_seconds: int
    days_in_billing_month: int = 30

    def validate(self) -> None:
        budgets = (self.daily_datapoint_budget, self.monthly_datapoint_budget)
        if any(not isfinite(float(value)) or float(value) <= 0 for value in budgets):
            raise ProductionContractError(
                "datapoint budgets must be finite and positive"
            )
        if (
            not isfinite(float(self.reserve_percentage))
            or not 0 <= self.reserve_percentage < 1
        ):
            raise ProductionContractError("reserve percentage must be in [0, 1)")
        counts = (self.active_fixtures, self.bookmakers, self.outcomes)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts
        ):
            raise ProductionContractError(
                "planner counts must be non-negative integers"
            )
        if self.active_fixtures and (self.bookmakers == 0 or self.outcomes == 0):
            raise ProductionContractError(
                "active fixtures require bookmakers and outcomes"
            )
        costs = (
            self.observed_datapoints_per_request,
            self.observed_datapoints_per_fixture,
        )
        if any(not isfinite(float(value)) or float(value) < 0 for value in costs):
            raise ProductionContractError(
                "observed datapoint costs must be finite and non-negative"
            )
        if self.active_fixtures and sum(costs) <= 0:
            raise ProductionContractError(
                "active fixture plan requires a positive observed cost"
            )
        if not self.desired_signal_time_windows:
            raise ProductionContractError("planner requires at least one signal window")
        if (
            not isinstance(self.polling_frequency_seconds, int)
            or isinstance(self.polling_frequency_seconds, bool)
            or self.polling_frequency_seconds <= 0
        ):
            raise ProductionContractError("polling frequency must be positive seconds")
        if (
            not isinstance(self.days_in_billing_month, int)
            or isinstance(self.days_in_billing_month, bool)
            or self.days_in_billing_month <= 0
        ):
            raise ProductionContractError("billing month length must be positive days")
        names = [window.name for window in self.desired_signal_time_windows]
        if len(set(names)) != len(names):
            raise ProductionContractError("signal-time window names must be unique")
        for window in self.desired_signal_time_windows:
            window.validate(self.active_fixtures)

    @property
    def daily_reserve_datapoints(self) -> float:
        return self.daily_datapoint_budget * self.reserve_percentage

    @property
    def monthly_reserve_datapoints(self) -> float:
        return self.monthly_datapoint_budget * self.reserve_percentage

    @property
    def usable_daily_budget(self) -> float:
        return self.daily_datapoint_budget - self.daily_reserve_datapoints

    @property
    def usable_monthly_budget(self) -> float:
        return self.monthly_datapoint_budget - self.monthly_reserve_datapoints

    def datapoints_for_fixture_count(self, fixture_count: int) -> float:
        if fixture_count < 0 or fixture_count > self.active_fixtures:
            raise ProductionContractError("snapshot fixture count is out of scope")
        return self.observed_datapoints_per_request + (
            fixture_count
            * self.bookmakers
            * self.outcomes
            * self.observed_datapoints_per_fixture
        )

    @property
    def estimated_datapoints_per_snapshot(self) -> float:
        """Cost for a snapshot covering the complete active fixture set."""

        return self.datapoints_for_fixture_count(self.active_fixtures)


@dataclass(frozen=True)
class PollingWindowEstimate:
    name: str
    fixture_count: int
    duration_seconds: int
    snapshots_per_day: int
    estimated_datapoints_per_snapshot: float
    estimated_daily_datapoints: float

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "fixture_count": self.fixture_count,
            "duration_seconds": self.duration_seconds,
            "snapshots_per_day": self.snapshots_per_day,
            "estimated_datapoints_per_snapshot": self.estimated_datapoints_per_snapshot,
            "estimated_daily_datapoints": self.estimated_daily_datapoints,
        }


@dataclass(frozen=True)
class _ScheduleEvaluation:
    windows: tuple[PollingWindowEstimate, ...]
    snapshots_per_day: int
    daily_datapoints: float


@dataclass(frozen=True)
class Top5PollingPlan:
    """Deterministic output for a proposed schedule."""

    status: PollingPlanStatus
    inputs: Top5PollingPlannerInputs
    windows: tuple[PollingWindowEstimate, ...]
    estimated_datapoints_per_snapshot: float
    estimated_daily_datapoints: float
    estimated_monthly_datapoints: float
    safe_max_snapshots_per_day: int
    safe_max_snapshots_per_month: int
    safe_max_snapshots: int
    minimum_safe_polling_interval_seconds: int | None
    projected_quota_exhaustion_days: float | None
    projected_full_quota_exhaustion_days: float | None
    remaining_daily_quota: float
    remaining_monthly_quota: float
    remaining_daily_reserve: float
    remaining_monthly_reserve: float
    coverage_warning: str | None = None

    def validate(self) -> None:
        self.inputs.validate()
        if self.estimated_daily_datapoints < 0 or self.estimated_monthly_datapoints < 0:
            raise ProductionContractError("polling plan consumption cannot be negative")
        if self.safe_max_snapshots < 0:
            raise ProductionContractError("safe snapshot count cannot be negative")
        if self.status is PollingPlanStatus.SAFE and self.coverage_warning:
            raise ProductionContractError("safe plan cannot carry a coverage warning")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "status": self.status.value,
            "estimated_datapoints_per_snapshot": self.estimated_datapoints_per_snapshot,
            "estimated_daily_datapoints": self.estimated_daily_datapoints,
            "estimated_monthly_datapoints": self.estimated_monthly_datapoints,
            "safe_max_snapshots_per_day": self.safe_max_snapshots_per_day,
            "safe_max_snapshots_per_month": self.safe_max_snapshots_per_month,
            "safe_max_snapshots": self.safe_max_snapshots,
            "minimum_safe_polling_interval_seconds": self.minimum_safe_polling_interval_seconds,
            "projected_quota_exhaustion_days": self.projected_quota_exhaustion_days,
            "projected_full_quota_exhaustion_days": self.projected_full_quota_exhaustion_days,
            "remaining_daily_quota": self.remaining_daily_quota,
            "remaining_monthly_quota": self.remaining_monthly_quota,
            "remaining_daily_reserve": self.remaining_daily_reserve,
            "remaining_monthly_reserve": self.remaining_monthly_reserve,
            "coverage_warning": self.coverage_warning,
            "windows": [window.as_payload() for window in self.windows],
        }


def _evaluate_schedule(
    inputs: Top5PollingPlannerInputs, interval_seconds: int
) -> _ScheduleEvaluation:
    estimates: list[PollingWindowEstimate] = []
    for window in inputs.desired_signal_time_windows:
        snapshots = (
            ceil(window.duration_seconds / interval_seconds)
            if window.fixture_count
            else 0
        )
        cost = inputs.datapoints_for_fixture_count(window.fixture_count)
        estimates.append(
            PollingWindowEstimate(
                window.name,
                window.fixture_count,
                window.duration_seconds,
                snapshots,
                cost,
                snapshots * cost,
            )
        )
    windows = tuple(estimates)
    return _ScheduleEvaluation(
        windows,
        sum(window.snapshots_per_day for window in windows),
        sum(window.estimated_daily_datapoints for window in windows),
    )


def _safe_interval(
    inputs: Top5PollingPlannerInputs, evaluation: _ScheduleEvaluation
) -> int | None:
    if not evaluation.snapshots_per_day:
        return None

    max_window_seconds = max(
        window.duration_seconds for window in inputs.desired_signal_time_windows
    )

    def is_safe(interval_seconds: int) -> bool:
        candidate = _evaluate_schedule(inputs, interval_seconds)
        monthly = candidate.daily_datapoints * inputs.days_in_billing_month
        return (
            candidate.daily_datapoints <= inputs.usable_daily_budget
            and monthly <= inputs.usable_monthly_budget
        )

    if not is_safe(max_window_seconds):
        return None
    low, high = 1, max_window_seconds
    while low < high:
        middle = (low + high) // 2
        if is_safe(middle):
            high = middle
        else:
            low = middle + 1
    return low


def plan_top5_polling(inputs: Top5PollingPlannerInputs) -> Top5PollingPlan:
    """Calculate a reserve-aware schedule without executing any request."""

    inputs.validate()
    evaluation = _evaluate_schedule(inputs, inputs.polling_frequency_seconds)
    daily = evaluation.daily_datapoints
    monthly = daily * inputs.days_in_billing_month
    average_cost = (
        daily / evaluation.snapshots_per_day if evaluation.snapshots_per_day else 0
    )
    safe_daily = floor(inputs.usable_daily_budget / average_cost) if average_cost else 0
    safe_monthly_total = (
        floor(inputs.usable_monthly_budget / average_cost) if average_cost else 0
    )
    safe_max = min(
        safe_daily,
        floor(safe_monthly_total / inputs.days_in_billing_month)
        if inputs.days_in_billing_month
        else 0,
    )
    remaining_daily = max(0.0, inputs.daily_datapoint_budget - daily)
    remaining_monthly = max(0.0, inputs.monthly_datapoint_budget - monthly)
    remaining_daily_reserve = max(
        0.0,
        inputs.daily_datapoint_budget - max(daily, inputs.usable_daily_budget),
    )
    remaining_monthly_reserve = max(
        0.0,
        inputs.monthly_datapoint_budget - max(monthly, inputs.usable_monthly_budget),
    )
    minimum_interval = _safe_interval(inputs, evaluation)
    coverage_warning = (
        "active fixtures are outside all requested signal-time windows"
        if inputs.active_fixtures and not evaluation.snapshots_per_day
        else None
    )
    if (
        daily > inputs.daily_datapoint_budget
        or monthly > inputs.monthly_datapoint_budget
        or minimum_interval is None
        and evaluation.snapshots_per_day > 0
    ):
        status = PollingPlanStatus.UNSAFE
    elif (
        daily > inputs.usable_daily_budget
        or monthly > inputs.usable_monthly_budget
        or coverage_warning
    ):
        status = PollingPlanStatus.DEGRADED
    else:
        status = PollingPlanStatus.SAFE
    plan = Top5PollingPlan(
        status=status,
        inputs=inputs,
        windows=evaluation.windows,
        estimated_datapoints_per_snapshot=inputs.estimated_datapoints_per_snapshot,
        estimated_daily_datapoints=daily,
        estimated_monthly_datapoints=monthly,
        safe_max_snapshots_per_day=safe_daily,
        safe_max_snapshots_per_month=safe_monthly_total,
        safe_max_snapshots=safe_max,
        minimum_safe_polling_interval_seconds=minimum_interval,
        projected_quota_exhaustion_days=(
            inputs.usable_monthly_budget / daily if daily else None
        ),
        projected_full_quota_exhaustion_days=(
            inputs.monthly_datapoint_budget / daily if daily else None
        ),
        remaining_daily_quota=remaining_daily,
        remaining_monthly_quota=remaining_monthly,
        remaining_daily_reserve=remaining_daily_reserve,
        remaining_monthly_reserve=remaining_monthly_reserve,
        coverage_warning=coverage_warning,
    )
    plan.validate()
    return plan


__all__ = [
    "PollingPlanStatus",
    "PollingWindowEstimate",
    "SignalTimeWindow",
    "Top5PollingPlan",
    "Top5PollingPlannerInputs",
    "plan_top5_polling",
]
