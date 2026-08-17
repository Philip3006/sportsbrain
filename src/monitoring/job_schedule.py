"""Job schedule expectation model — MON-002 / MON-012.

Single source of truth for when each job is expected to run.
Active scheduler/workflow/launchd source wins over old JOB_SCHEDULE constants.

Provides:
  JobExpectation — typed expectation for a job
  ExpectationResult — evaluation result (in_window, is_overdue)
  JOB_EXPECTATIONS — registry keyed by health-job name
  evaluate_expectation(exp, last_run_at, now) -> ExpectationResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Expectation kinds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntervalExpectation:
    """Periodic job expected at a fixed interval (launchd StartInterval or simple cron)."""

    interval_s: int
    grace_s: int
    cadence: str = ""
    kind: str = field(default="interval", init=False)


@dataclass(frozen=True)
class CronSetExpectation:
    """Job driven by a set of fixed UTC cron trigger points (daily or weekly).

    Each entry in `points` is (weekday_or_none, hour, minute) UTC.
    weekday uses Python datetime.weekday() convention: 0=Monday … 6=Sunday.
    Use None for "every day" cron entries.

    Evaluation:
    - Find the most recent past cron point (last_expected).
    - If last_run_at >= last_expected: job ran in time → not_expected until next point.
    - Else: expected; overdue if (now − last_expected) > grace_s.
    """

    points: tuple[tuple[int | None, int, int], ...]  # (weekday|None, hour, minute)
    grace_s: int
    cadence: str = ""
    kind: str = field(default="cron_set", init=False)


@dataclass(frozen=True)
class WindowedExpectation:
    """Job active only in specific (weekday, start_hour, end_hour_inclusive) UTC windows.

    Outside any window: not_expected (never stale).
    Inside a window: evaluated against `interval_s` + `grace_s`.

    weekday uses Python datetime.weekday() convention: 0=Monday … 6=Sunday.
    """

    windows: tuple[tuple[int, int, int], ...]  # (weekday, start_hour, end_hour_inclusive)
    interval_s: int
    grace_s: int
    cadence: str = ""
    kind: str = field(default="windowed", init=False)


@dataclass(frozen=True)
class EventWithFallbackExpectation:
    """Event-driven job; staleness is governed by the fallback polling interval.

    Primary trigger is an external event (e.g. repository_dispatch).
    In absence of events the fallback interval determines when the job is overdue.
    """

    fallback_interval_s: int
    grace_s: int
    cadence: str = ""
    kind: str = field(default="event_with_fallback", init=False)


JobExpectation = (
    IntervalExpectation
    | CronSetExpectation
    | WindowedExpectation
    | EventWithFallbackExpectation
)


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExpectationResult:
    """Result of evaluating a JobExpectation at a given moment.

    in_window=False → job is not expected to run right now; skip staleness (MON-002).
    in_window=True, is_overdue=True → job should have run but hasn't; mark stale.
    in_window=True, is_overdue=False → job is within its expected cadence.
    """

    in_window: bool
    is_overdue: bool
    cadence: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_most_recent_cron_point(
    points: tuple[tuple[int | None, int, int], ...],
    now: datetime,
) -> datetime | None:
    """Return the most recent past cron fire time strictly before or equal to `now`.

    Looks back up to 8 calendar days so weekly cron points are always found.
    `now` must be timezone-aware UTC.
    """
    best: datetime | None = None
    for day_offset in range(8):
        candidate_day = now - timedelta(days=day_offset)
        for (weekday, hour, minute) in points:
            if weekday is not None and candidate_day.weekday() != weekday:
                continue
            candidate = candidate_day.replace(
                hour=hour, minute=minute, second=0, microsecond=0,
            )
            if candidate > now:
                continue
            if best is None or candidate > best:
                best = candidate
    return best


def _find_next_cron_point(
    points: tuple[tuple[int | None, int, int], ...],
    now: datetime,
) -> datetime | None:
    """Return earliest future cron fire time strictly after `now`.

    Looks forward up to 8 calendar days so weekly cron points are always found.
    `now` must be timezone-aware UTC.
    """
    best: datetime | None = None
    for day_offset in range(9):
        candidate_day = now + timedelta(days=day_offset)
        for (weekday, hour, minute) in points:
            if weekday is not None and candidate_day.weekday() != weekday:
                continue
            candidate = candidate_day.replace(
                hour=hour, minute=minute, second=0, microsecond=0,
            )
            if candidate <= now:
                continue
            if best is None or candidate < best:
                best = candidate
    return best


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_expectation(
    exp: JobExpectation,
    last_run_at: datetime | None,
    now: datetime,
) -> ExpectationResult:
    """Evaluate job expectation state at `now` with optional `last_run_at`.

    `now` must be timezone-aware UTC.

    Raises ValueError for unsupported/malformed expectation kinds so callers
    can fail closed rather than silently returning healthy (MON-002).
    """
    if isinstance(exp, IntervalExpectation):
        return _eval_interval(exp, last_run_at, now)
    if isinstance(exp, CronSetExpectation):
        return _eval_cron_set(exp, last_run_at, now)
    if isinstance(exp, WindowedExpectation):
        return _eval_windowed(exp, last_run_at, now)
    if isinstance(exp, EventWithFallbackExpectation):
        return _eval_event_fallback(exp, last_run_at, now)
    raise ValueError(f"Unsupported JobExpectation type: {type(exp)!r}")


def _eval_interval(
    exp: IntervalExpectation,
    last_run_at: datetime | None,
    now: datetime,
) -> ExpectationResult:
    if last_run_at is None:
        return ExpectationResult(in_window=True, is_overdue=True, cadence=exp.cadence)
    age_s = (now - last_run_at).total_seconds()
    return ExpectationResult(
        in_window=True,
        is_overdue=age_s > exp.interval_s + exp.grace_s,
        cadence=exp.cadence,
    )


def _eval_cron_set(
    exp: CronSetExpectation,
    last_run_at: datetime | None,
    now: datetime,
) -> ExpectationResult:
    last_expected = _find_most_recent_cron_point(exp.points, now)
    if last_expected is None:
        # No past cron point found within 8 days — treat as overdue.
        return ExpectationResult(in_window=True, is_overdue=True, cadence=exp.cadence)

    ran_in_time = last_run_at is not None and last_run_at >= last_expected
    if ran_in_time:
        # Job ran after the most recent expected trigger — not due again yet.
        return ExpectationResult(in_window=False, is_overdue=False, cadence=exp.cadence)

    # Job has not run since last_expected.
    age_s = (now - last_expected).total_seconds()
    return ExpectationResult(
        in_window=True,
        is_overdue=age_s > exp.grace_s,
        cadence=exp.cadence,
    )


def _eval_windowed(
    exp: WindowedExpectation,
    last_run_at: datetime | None,
    now: datetime,
) -> ExpectationResult:
    dow = now.weekday()  # 0=Monday … 6=Sunday
    hour = now.hour
    current_window = next(
        (w for w in exp.windows if dow == w[0] and w[1] <= hour <= w[2]),
        None,
    )
    if current_window is None:
        return ExpectationResult(in_window=False, is_overdue=False, cadence=exp.cadence)

    _, start_hour, _ = current_window
    window_start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)

    if last_run_at is not None and last_run_at >= window_start:
        # Ran within current window — normal interval + grace semantics.
        age_s = (now - last_run_at).total_seconds()
        is_overdue = age_s > exp.interval_s + exp.grace_s
    else:
        # No run yet in current window — allow grace_s from window start before overdue.
        window_age_s = (now - window_start).total_seconds()
        is_overdue = window_age_s > exp.grace_s

    return ExpectationResult(in_window=True, is_overdue=is_overdue, cadence=exp.cadence)


def _eval_event_fallback(
    exp: EventWithFallbackExpectation,
    last_run_at: datetime | None,
    now: datetime,
) -> ExpectationResult:
    if last_run_at is None:
        return ExpectationResult(in_window=True, is_overdue=True, cadence=exp.cadence)
    age_s = (now - last_run_at).total_seconds()
    return ExpectationResult(
        in_window=True,
        is_overdue=age_s > exp.fallback_interval_s + exp.grace_s,
        cadence=exp.cadence,
    )


# ---------------------------------------------------------------------------
# Canonical job expectation registry
# ---------------------------------------------------------------------------
# Source-of-truth: active scheduler/workflow/launchd files.
# No second cadence definition — cadence strings derive from this registry.
#
# Scheduler reconciliation notes:
#
# tennis_scan: .github/workflows/tennis_scan.yml
#   8 daily UTC cron points: 02/06/09/12/15/18/21/23 UTC.
#   (CEO audit listed 9 incl. 04:00; actual workflow has 8. Source wins.)
#
# tennis_retrain: health key written by TWO workflows:
#   - tennis_lgbm_retrain.yml (primary, daily 05:00 UTC)
#   - tennis_elo_refresh.yml (secondary, weekly Mon 03:00 UTC)
#   They run at non-overlapping times. Primary (lgbm daily) governs expectation.
#   Monitoring design debt: separate health keys would be cleaner.
#
# bundesliga2_live_push: .github/workflows/bundesliga2_live_push.yml
#   Fri */2 18-22, Sat */2 11-22, Sun */2 11-22 UTC.
#   Outside these windows: not_expected (never stale).
#
# bundesliga2_closing_odds: .github/workflows/bundesliga2_closing_odds.yml
#   Fri 18:25, Sat 10:55, Sat 18:25, Sun 11:25 UTC.
#   Long weekday gaps must not false-stale (MON-002).
#
# consume_pending_bets: .github/workflows/consume_pending_bets.yml
#   Primary: repository_dispatch; Fallback: */30 * * * * (30-min cron).
#
# odds_refresh: launchd/com.sportsbrain.odds-refresh.plist
#   StartInterval = 300 s.

JOB_EXPECTATIONS: dict[str, JobExpectation] = {
    # --- Standard interval jobs ---
    "daily_scan": IntervalExpectation(
        interval_s=24 * 3600, grace_s=2 * 3600,
        cadence="1×/Tag 07:00",
    ),
    "auto_retrain": IntervalExpectation(
        interval_s=12 * 3600, grace_s=1 * 3600,
        cadence="2×/Tag 06:00 + 18:00",
    ),
    "closing_odds": IntervalExpectation(
        interval_s=12 * 3600, grace_s=1 * 3600,
        cadence="2×/Tag 14:00 + 18:00",
    ),
    "live_score_push": IntervalExpectation(
        interval_s=120, grace_s=300,
        cadence="alle 2 Min",
    ),
    "prematch_scan": IntervalExpectation(
        interval_s=1800, grace_s=900,
        cadence="alle 30 Min (im Fenster)",
    ),
    "settle": IntervalExpectation(
        interval_s=3600, grace_s=600,
        cadence="stündlich 00:30–04:30",
    ),
    "aggregate_health": IntervalExpectation(
        interval_s=120, grace_s=300,
        cadence="alle 2 Min (huckepack)",
    ),

    # --- Tennis ---
    # tennis_scan.yml: 8 daily UTC cron points
    "tennis_scan": CronSetExpectation(
        points=(
            (None, 2, 0), (None, 6, 0), (None, 9, 0), (None, 12, 0),
            (None, 15, 0), (None, 18, 0), (None, 21, 0), (None, 23, 0),
        ),
        grace_s=3600,
        cadence="8×/Tag 02/06/09/12/15/18/21/23 UTC",
    ),
    # tennis_settle.yml: '15 6-22/2 * * *' → 06:15/08:15/.../22:15 UTC (9 daily points)
    "tennis_settle": CronSetExpectation(
        points=tuple((None, h, 15) for h in range(6, 23, 2)),
        grace_s=3600,
        cadence="9×/Tag 06:15–22:15/2h UTC",
    ),
    "tennis_closing_odds": IntervalExpectation(
        interval_s=1800, grace_s=900,
        cadence="alle 30 Min",
    ),
    # tennis_lgbm_retrain.yml: daily 05:00 UTC (primary scheduler)
    "tennis_retrain": CronSetExpectation(
        points=((None, 5, 0),),
        grace_s=3600,
        cadence="1×/Tag 05:00 UTC (LGBM primary; Elo wöchentlich Mo 03:00 UTC)",
    ),

    # --- 2. Bundesliga ---
    # bundesliga2_scan.yml: daily 06:00 UTC + Fr 15:00 / Sa 08:00 / So 08:00 UTC
    # Python weekday: Fri=4, Sat=5, Sun=6
    "bundesliga2_scan": CronSetExpectation(
        points=(
            (None, 6, 0),   # täglich 06:00 UTC
            (4, 15, 0),     # Fr 15:00 UTC
            (5, 8, 0),      # Sa 08:00 UTC
            (6, 8, 0),      # So 08:00 UTC
        ),
        grace_s=3600,
        cadence="täglich 06:00 UTC + Fr 15:00 / Sa 08:00 / So 08:00 UTC",
    ),
    # bundesliga2_settle.yml: Mo/Di 20:30 / Fr 21:30 / Sa 14:30+21:30 / So 14:45+21:00 UTC
    # Python weekday: Mon=0, Tue=1, Fri=4, Sat=5, Sun=6
    "bundesliga2_settle": CronSetExpectation(
        points=(
            (0, 20, 30),   # Mo 20:30 UTC
            (1, 20, 30),   # Di 20:30 UTC
            (4, 21, 30),   # Fr 21:30 UTC
            (5, 14, 30),   # Sa 14:30 UTC
            (5, 21, 30),   # Sa 21:30 UTC
            (6, 14, 45),   # So 14:45 UTC
            (6, 21, 0),    # So 21:00 UTC
        ),
        grace_s=3600,
        cadence="Mo/Di 20:30 / Fr 21:30 / Sa 14:30+21:30 / So 14:45+21:00 UTC",
    ),
    # bundesliga2_retrain.yml: daily 05:00 UTC
    "bundesliga2_retrain": CronSetExpectation(
        points=((None, 5, 0),),
        grace_s=4 * 3600,
        cadence="täglich 05:00 UTC",
    ),
    # bundesliga2_live_push.yml: windowed Fri/Sat/Sun
    # cron: '*/2 18-22 * * 5' (Fri), '*/2 11-22 * * 6' (Sat), '*/2 11-22 * * 0' (Sun)
    # Python weekday: Fri=4, Sat=5, Sun=6
    "bundesliga2_live_push": WindowedExpectation(
        windows=((4, 18, 22), (5, 11, 22), (6, 11, 22)),
        interval_s=120,
        grace_s=300,
        cadence="alle 2 Min Fr 18-23 / Sa 11-23 / So 11-23 UTC",
    ),
    # bundesliga2_closing_odds.yml: weekly cron set
    # Fri 18:25, Sat 10:55, Sat 18:25, Sun 11:25 UTC
    # Python weekday: Fri=4, Sat=5, Sun=6
    "bundesliga2_closing_odds": CronSetExpectation(
        points=((4, 18, 25), (5, 10, 55), (5, 18, 25), (6, 11, 25)),
        grace_s=1800,
        cadence="Fr 18:25 / Sa 10:55+18:25 / So 11:25 UTC",
    ),

    # --- Event-driven / local ---
    # consume_pending_bets.yml: repository_dispatch primary + */30 fallback
    "consume_pending_bets": EventWithFallbackExpectation(
        fallback_interval_s=1800,
        grace_s=600,
        cadence="event-driven + 30-Min-Fallback",
    ),
    # launchd/com.sportsbrain.odds-refresh.plist: StartInterval=300
    "odds_refresh": IntervalExpectation(
        interval_s=300,
        grace_s=300,
        cadence="alle 5 Min (launchd)",
    ),
}


def nominal_interval_s(exp: JobExpectation) -> int:
    """Canonical 'how often does this job run' for display (next_expected_in_s)."""
    if isinstance(exp, IntervalExpectation):
        return exp.interval_s
    if isinstance(exp, CronSetExpectation):
        return exp.grace_s  # nominal: grace approximates the per-cycle window
    if isinstance(exp, WindowedExpectation):
        return exp.interval_s
    if isinstance(exp, EventWithFallbackExpectation):
        return exp.fallback_interval_s
    return 3600


def next_trigger_s(exp: JobExpectation, now: datetime) -> int | None:
    """Seconds until the next expected scheduler trigger from `now`.

    Returns None when the value cannot be truthfully computed.
    CronSet and Windowed kinds compute the actual time to next point/window.
    """
    if isinstance(exp, IntervalExpectation):
        return exp.interval_s
    if isinstance(exp, CronSetExpectation):
        nxt = _find_next_cron_point(exp.points, now)
        return max(0, int((nxt - now).total_seconds())) if nxt is not None else None
    if isinstance(exp, WindowedExpectation):
        dow = now.weekday()
        hour = now.hour
        in_window = any(dow == w[0] and w[1] <= hour <= w[2] for w in exp.windows)
        if in_window:
            return exp.interval_s
        best: datetime | None = None
        for day_offset in range(8):
            candidate_day = now + timedelta(days=day_offset)
            for (weekday, start_hour, _end) in exp.windows:
                if candidate_day.weekday() != weekday:
                    continue
                candidate = candidate_day.replace(
                    hour=start_hour, minute=0, second=0, microsecond=0,
                )
                if candidate <= now:
                    continue
                if best is None or candidate < best:
                    best = candidate
        return int((best - now).total_seconds()) if best is not None else None
    if isinstance(exp, EventWithFallbackExpectation):
        return exp.fallback_interval_s
    return None
