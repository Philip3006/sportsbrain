# Top-5 TheRundown quota-safe polling plan

This planner is a deterministic, provider-neutral calculation for a future
TheRundown candidate shadow run. It does not call a provider, change a
scheduler, activate a provider, publish signals, or purchase quota.

## Runtime contract

`Top5PollingPlannerInputs` requires the current runtime values for:

- daily and monthly datapoint budgets;
- reserve percentage;
- active fixtures, bookmakers, and outcomes;
- observed datapoints per request and per fixture;
- named signal-time windows and their fixture counts;
- polling frequency in seconds.

The optional billing-month length defaults to 30 days and can be overridden.
No TheRundown plan limit is embedded in the implementation.

An observed per-fixture cost is applied to each fixture × bookmaker × outcome
unit. The snapshot estimate is therefore:

`request_cost + fixtures × bookmakers × outcomes × fixture_cost`

Window costs use the exact fixture count assigned to that window. A window
with zero fixtures produces zero snapshots, which prevents aggressive polling
of far-future fixtures. Overlapping windows are allowed and are charged as
separate passes so that the planner does not hide duplicated work.

## Status semantics

- `SAFE`: projected daily and monthly consumption stay below their respective
  reserve-protected budgets.
- `DEGRADED`: the proposal stays within the full budgets but consumes some or
  all of the configured reserve, or has no fixtures inside its requested
  windows.
- `UNSAFE`: the proposal exceeds a full daily/monthly budget. The output still
  reports a slower safe interval when one exists.

The minimum safe interval is calculated by deterministic search over the
current windows. It is the shortest interval that keeps both reserve-protected
budgets intact; it is not a scheduler instruction.

## Conservative example

```python
from src.football.top5_polling_planner import (
    SignalTimeWindow,
    Top5PollingPlannerInputs,
    plan_top5_polling,
)

inputs = Top5PollingPlannerInputs(
    daily_datapoint_budget=100,
    monthly_datapoint_budget=3000,
    reserve_percentage=0.20,
    active_fixtures=20,
    bookmakers=3,
    outcomes=3,
    observed_datapoints_per_request=1,
    observed_datapoints_per_fixture=0.05,
    desired_signal_time_windows=(
        SignalTimeWindow("signal-2h", 120, 60, 5),
        SignalTimeWindow("signal-60m", 60, 15, 10),
        SignalTimeWindow("final-15m", 15, 0, 5),
    ),
    polling_frequency_seconds=1800,
)
plan = plan_top5_polling(inputs)
```

The deterministic result is:

| Output | Value |
|---|---:|
| Status | `SAFE` |
| Full active-fixture snapshot | `10.0` datapoints |
| Scheduled snapshots/day | `5` |
| Estimated daily consumption | `20.75` |
| Estimated monthly consumption | `622.5` |
| Reserve-protected daily budget | `80` |
| Reserve-protected monthly budget | `2400` |
| Minimum safe interval | `400` seconds |

The proposed 30-minute cadence is therefore conservative for these supplied
runtime values. The five snapshots are concentrated in the three windows and
cover 5, 10, and 5 fixtures respectively; no snapshot is allocated to
far-future fixtures.

## Operational boundary

This branch adds only the planner, deterministic tests, and this document. It
does not import or edit the APP-B2 TheRundown adapter, diagnostic, fixtures,
or tests from PR #88. A future qualification run must provide fresh runtime
budget/cost evidence before using the planner output.
