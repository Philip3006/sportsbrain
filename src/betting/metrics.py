"""
Canonical performance metrics for SportsBrain predictions and bets.

All functions are pure (no I/O). Each function accepts a list of normalized
record dicts and returns a MetricResult or list of bucket dicts.

Normalized record schema (all fields optional except those noted per function):
  match_id:      str
  match_date:    str        YYYY-MM-DD
  sport:         str        football | tennis
  league:        str        wm2026 | atp | wta | bl2
  market:        str        raw market key
  model_prob:    float|None probability at prediction time (NOT recalculated)
  decimal_odds:  float
  stake_amount:  float
  status:        str        won | lost | push | void | cancelled
  pnl:           float
  closing_odds:  float|None
  clv:           float|None decimal_odds_at_close - entry_odds (positive = beat line)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass
class MetricResult:
    value: float | None
    n: int
    note: str = ""


@dataclass
class SegmentedMetrics:
    n: int
    n_settled: int
    brier: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    log_loss: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    roi: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    yield_: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    total_pnl: float | None = None
    total_staked: float | None = None
    avg_odds: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    max_drawdown: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    clv_avg: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    clv_hit_rate: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    clv_coverage: MetricResult = field(default_factory=lambda: MetricResult(None, 0))
    calibration: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BINARY_STATUSES = frozenset({"won", "lost"})
_SETTLED_STATUSES = frozenset({"won", "lost", "push"})


def _binary(records: list[dict]) -> list[dict]:
    """Records with a binary outcome (won/lost) — excludes push/void/cancelled."""
    return [r for r in records if r.get("status") in _BINARY_STATUSES]


def _settled(records: list[dict]) -> list[dict]:
    """Records that are fully settled (won/lost/push)."""
    return [r for r in records if r.get("status") in _SETTLED_STATUSES]


def _outcome_binary(r: dict) -> int:
    return 1 if r.get("status") == "won" else 0


# ---------------------------------------------------------------------------
# Prediction quality
# ---------------------------------------------------------------------------

def brier_score(records: list[dict]) -> MetricResult:
    """Brier Score = mean((p − y)²). Lower is better (0 = perfect).

    Only uses records with a binary outcome (won/lost) AND a valid model_prob.
    Push/void records are excluded — they are not binary outcomes.
    model_prob must be the probability stored at prediction time.
    """
    usable = [
        r for r in _binary(records)
        if r.get("model_prob") is not None and 0.0 < r["model_prob"] < 1.0
    ]
    if not usable:
        return MetricResult(None, 0, "no records with binary outcome and valid model_prob")
    total = sum((r["model_prob"] - _outcome_binary(r)) ** 2 for r in usable)
    return MetricResult(round(total / len(usable), 6), len(usable))


def log_loss(records: list[dict]) -> MetricResult:
    """Log Loss = −mean(y·log(p) + (1−y)·log(1−p)). Lower is better.

    Only uses records with a binary outcome (won/lost) AND a valid model_prob.
    model_prob is clipped to [1e-7, 1−1e-7] to avoid log(0).
    """
    usable = [
        r for r in _binary(records)
        if r.get("model_prob") is not None and 0.0 < r["model_prob"] < 1.0
    ]
    if not usable:
        return MetricResult(None, 0, "no records with binary outcome and valid model_prob")
    eps = 1e-7
    total = 0.0
    for r in usable:
        p = max(eps, min(1 - eps, r["model_prob"]))
        y = _outcome_binary(r)
        total -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return MetricResult(round(total / len(usable), 6), len(usable))


def calibration_buckets(records: list[dict], n_buckets: int = 5) -> list[dict]:
    """Calibration analysis: predicted probability vs observed win rate.

    Returns a list of bucket dicts:
      p_low, p_high, n, predicted_mean, observed_freq, calibration_error

    Only uses records with binary outcome (won/lost) and valid model_prob.
    Buckets with n=0 are omitted.
    """
    usable = [
        r for r in _binary(records)
        if r.get("model_prob") is not None and 0.0 < r["model_prob"] < 1.0
    ]
    if not usable:
        return []
    width = 1.0 / n_buckets
    buckets: list[dict] = []
    for i in range(n_buckets):
        lo = i * width
        hi = lo + width
        bucket = [r for r in usable if lo <= r["model_prob"] < hi]
        if i == n_buckets - 1:
            bucket = [r for r in usable if lo <= r["model_prob"] <= 1.0]
        if not bucket:
            continue
        predicted_mean = sum(r["model_prob"] for r in bucket) / len(bucket)
        observed_freq = sum(_outcome_binary(r) for r in bucket) / len(bucket)
        buckets.append({
            "p_low": round(lo, 2),
            "p_high": round(hi, 2),
            "n": len(bucket),
            "predicted_mean": round(predicted_mean, 4),
            "observed_freq": round(observed_freq, 4),
            "calibration_error": round(abs(predicted_mean - observed_freq), 4),
        })
    return buckets


# ---------------------------------------------------------------------------
# Betting performance
# ---------------------------------------------------------------------------

def roi(records: list[dict]) -> MetricResult:
    """ROI = (total_pnl / total_staked) × 100, expressed as %.

    Uses only won/lost records (excludes push/void — stake typically returned).
    Returns MetricResult with value=None and a note if staked=0.
    """
    binary = _binary(records)
    if not binary:
        return MetricResult(None, 0, "no settled binary records")
    staked = sum(r.get("stake_amount", 0.0) for r in binary)
    if staked == 0:
        return MetricResult(None, len(binary), "total_staked=0")
    pnl = sum(r.get("pnl", 0.0) for r in binary)
    return MetricResult(round(pnl / staked * 100, 4), len(binary))


def yield_metric(records: list[dict]) -> MetricResult:
    """Yield: identical to ROI in sports betting convention.

    Kept separate so callers can label it correctly in output.
    """
    return roi(records)


def avg_odds(records: list[dict]) -> MetricResult:
    """Average decimal odds across all settled records (won/lost/push)."""
    settled = _settled(records)
    valid = [r for r in settled if r.get("decimal_odds") and r["decimal_odds"] > 1.0]
    if not valid:
        return MetricResult(None, 0, "no settled records with valid decimal_odds")
    mean = sum(r["decimal_odds"] for r in valid) / len(valid)
    return MetricResult(round(mean, 4), len(valid))


def total_pnl(records: list[dict]) -> float:
    """Sum of pnl across all records (won/lost/push/void)."""
    return round(sum(r.get("pnl", 0.0) for r in records), 4)


def total_staked(records: list[dict]) -> float:
    """Sum of stake_amount for binary settled records (won/lost)."""
    return round(sum(r.get("stake_amount", 0.0) for r in _binary(records)), 4)


def max_drawdown(records: list[dict], sort_key: str = "match_date") -> MetricResult:
    """Maximum peak-to-trough drawdown in cumulative P&L (in € or stake units).

    Records are sorted by sort_key before computing the running drawdown.
    Only uses records with valid pnl. Push/void records contribute pnl=0 if present.

    Returns MetricResult(value=max_dd_in_currency, n=len(used_records)).
    """
    usable = [r for r in records if r.get("pnl") is not None and r.get("status") in _SETTLED_STATUSES]
    if not usable:
        return MetricResult(None, 0, "no settled records with pnl")
    usable.sort(key=lambda r: r.get(sort_key, ""))
    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for r in usable:
        running += r.get("pnl", 0.0)
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)
    return MetricResult(round(max_dd, 4), len(usable))


# ---------------------------------------------------------------------------
# Market / CLV quality
# ---------------------------------------------------------------------------

def clv_avg(records: list[dict]) -> MetricResult:
    """Average Closing Line Value.

    clv = entry_odds − closing_odds (positive = we beat the closing line).
    Only uses records where clv is present and non-None.
    """
    with_clv = [r for r in records if r.get("clv") is not None]
    if not with_clv:
        return MetricResult(
            None, 0,
            "metric unavailable: closing_odds not persisted for these records",
        )
    mean = sum(r["clv"] for r in with_clv) / len(with_clv)
    return MetricResult(round(mean, 6), len(with_clv))


def clv_hit_rate(records: list[dict]) -> MetricResult:
    """CLV Hit Rate: fraction of bets where we beat the closing line (clv > 0).

    Only uses records where clv is present.
    """
    with_clv = [r for r in records if r.get("clv") is not None]
    if not with_clv:
        return MetricResult(
            None, 0,
            "metric unavailable: closing_odds not persisted for these records",
        )
    hits = sum(1 for r in with_clv if r["clv"] > 0)
    return MetricResult(round(hits / len(with_clv), 6), len(with_clv))


def clv_coverage(records: list[dict]) -> MetricResult:
    """Fraction of all settled records that have a closing_odds value."""
    settled = _settled(records)
    if not settled:
        return MetricResult(None, 0, "no settled records")
    with_close = sum(
        1 for r in settled
        if r.get("closing_odds") is not None and r.get("closing_odds", 0) > 0
    )
    return MetricResult(round(with_close / len(settled), 6), len(settled))


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def market_group(market: str) -> str:
    """Map raw market key to a coarse group for segmentation."""
    m = market.lower()
    if m in ("home", "away", "draw"):
        return "match_winner"
    if m in ("btts_yes", "btts_no"):
        return "btts"
    if "o/u_games" in m:
        return "ou_games"
    if "o/u" in m:
        return "ou_goals"
    if "ah" in m:
        return "asian_handicap"
    if "scorer" in m:
        return "goalscorer"
    if "goals_" in m:
        return "goals_range"
    return "other"


def compute_segment(records: list[dict]) -> dict:
    """Compute all metrics for a slice of records. Returns a JSON-safe dict."""
    binary = _binary(records)
    settled = _settled(records)
    r_roi = roi(records)
    r_brier = brier_score(records)
    r_ll = log_loss(records)
    r_dd = max_drawdown(records)
    r_clv = clv_avg(records)
    r_clv_hr = clv_hit_rate(records)
    r_clv_cov = clv_coverage(records)
    r_ao = avg_odds(records)
    cal = calibration_buckets(records)

    def _mr(m: MetricResult) -> dict:
        return {"value": m.value, "n": m.n, "note": m.note or None}

    return {
        "n": len(records),
        "n_settled": len(settled),
        "n_binary": len(binary),
        "total_pnl": total_pnl(records) if records else None,
        "total_staked": total_staked(records),
        "brier_score": _mr(r_brier),
        "log_loss": _mr(r_ll),
        "roi_pct": _mr(r_roi),
        "yield_pct": _mr(r_roi),
        "avg_odds": _mr(r_ao),
        "max_drawdown": _mr(r_dd),
        "clv_avg": _mr(r_clv),
        "clv_hit_rate": _mr(r_clv_hr),
        "clv_coverage": _mr(r_clv_cov),
        "calibration": cal,
    }
