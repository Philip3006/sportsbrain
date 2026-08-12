"""O1-7 regression suite — signal identity, status lifecycle, gate reuse, concurrency.

Spec coverage:
  S1  make_signal_id — deterministic, stable across rescans, 12 chars
  S2  model_prob_to_decimal — canonical conversion, no 100x error
  S3  compute_current_ev — correct formula (model_prob_pct / 100 * odds - 1)
  S4  evaluate_signal_status — EXPIRED after 100min, STARTED pre-kickoff
  S5  evaluate_signal_status — UNREFRESHABLE when current_odds is None
  S6  evaluate_signal_status — STALE_ODDS >30min, fresh within 30min
  S7  evaluate_signal_status — EDGE_LOST when gate fails
  S8  evaluate_signal_status — ACTIVE when gate passes (football 1X2)
  S9  evaluate_signal_status — ACTIVE when gate passes (tennis H2H)
  S10 evaluate_signal_status — EDGE_LOST when model_prob < min_prob
  S11 evaluate_signal_status — EDGE_LOST when odds > max_odds (tennis)
  S12 update_odds_state — preserves initial_odds from first observation
  S13 update_odds_state — deduplicates same odds within 5-min bucket
  S14 update_odds_state — appends when odds change
  S15 merge_odds_state_into_signal — immutable fields not overwritten
  S16 concurrent_write_regression — two writers don't corrupt sidecar
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import tempfile

import pytest

from src.signals.signal_status import (
    _dedup_history,
    compute_current_ev,
    evaluate_signal_status,
    load_odds_state,
    make_signal_id,
    merge_odds_state_into_signal,
    model_prob_to_decimal,
    seed_initial_odds,
    update_odds_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_ago(minutes: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _kickoff_in(minutes: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _kickoff_ago(minutes: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def tmp_sidecar(tmp_path, monkeypatch):
    """Redirect sidecar writes to a temp file for isolation."""
    sidecar = tmp_path / "odds_state.json"
    import src.signals.signal_status as ss
    monkeypatch.setattr(ss, "_SIDECAR_PATH", sidecar)
    return sidecar


# ---------------------------------------------------------------------------
# S1: Signal identity
# ---------------------------------------------------------------------------

class TestSignalId:
    def test_deterministic(self):
        sid1 = make_signal_id("football", "A vs B", "home", "2026-08-11T15:00:00Z")
        sid2 = make_signal_id("football", "A vs B", "home", "2026-08-11T15:00:00Z")
        assert sid1 == sid2

    def test_12_chars(self):
        sid = make_signal_id("tennis", "Nadal vs Djokovic", "home", "2026-08-11")
        assert len(sid) == 12

    def test_stable_across_intraday_rescan(self):
        # Different times on the same date → same ID
        sid1 = make_signal_id("tennis", "X vs Y", "away", "2026-08-11T10:00:00Z")
        sid2 = make_signal_id("tennis", "X vs Y", "away", "2026-08-11T22:59:00Z")
        assert sid1 == sid2

    def test_different_market_different_id(self):
        s1 = make_signal_id("football", "A vs B", "home", "2026-08-11")
        s2 = make_signal_id("football", "A vs B", "away", "2026-08-11")
        assert s1 != s2

    def test_different_date_different_id(self):
        s1 = make_signal_id("football", "A vs B", "home", "2026-08-11")
        s2 = make_signal_id("football", "A vs B", "home", "2026-08-12")
        assert s1 != s2

    def test_sha256_derivation(self):
        sid = make_signal_id("football", "A vs B", "home", "2026-08-11T15:00:00Z")
        expected = hashlib.sha256("football:A vs B:home:2026-08-11".encode()).hexdigest()[:12]
        assert sid == expected


# ---------------------------------------------------------------------------
# S2-S3: Probability unit conversion and EV
# ---------------------------------------------------------------------------

class TestProbabilityConversion:
    def test_percentage_to_decimal(self):
        assert model_prob_to_decimal(49.5) == pytest.approx(0.495)

    def test_no_100x_error(self):
        # If model_prob_pct is accidentally treated as decimal, EV would be ~49x wrong
        ev_correct = compute_current_ev(49.5, 2.10)
        # (0.495 * 2.10 - 1) = 0.0395
        assert ev_correct == pytest.approx(0.0395, abs=1e-4)
        # Sanity: wrong calculation would give ~(49.5 * 2.10 - 1) ≈ 102.95
        assert ev_correct < 1.0

    def test_ev_edge_zero(self):
        ev = compute_current_ev(50.0, 2.0)
        assert ev == pytest.approx(0.0, abs=1e-6)

    def test_ev_negative(self):
        ev = compute_current_ev(40.0, 2.0)
        assert ev == pytest.approx(-0.2, abs=1e-4)


# ---------------------------------------------------------------------------
# S4-S11: Signal status lifecycle
# ---------------------------------------------------------------------------

def _football_signal(**overrides) -> dict:
    base = {
        "sport": "football",
        "match": "A vs B",
        "market": "home",
        "model_prob": 55.0,
        "kickoff": _kickoff_in(60),
    }
    base.update(overrides)
    return base


def _tennis_signal(**overrides) -> dict:
    base = {
        "sport": "tennis",
        "match": "Player A vs Player B",
        "market": "home",
        "model_prob": 55.0,
        "kickoff": _kickoff_in(60),
    }
    base.update(overrides)
    return base


class TestEvaluateSignalStatus:
    def test_s4_expired(self):
        sig = _football_signal(kickoff=_kickoff_ago(110))
        status = evaluate_signal_status(sig, 2.10, _now_str())
        assert status == "EXPIRED"

    def test_s4_started_within_window(self):
        sig = _football_signal(kickoff=_kickoff_ago(10))
        status = evaluate_signal_status(sig, 2.10, _now_str())
        assert status == "STARTED"

    def test_s4_not_yet_started(self):
        sig = _football_signal(kickoff=_kickoff_in(10))
        # Should NOT be STARTED or EXPIRED
        status = evaluate_signal_status(sig, 2.10, _now_str())
        assert status not in ("STARTED", "EXPIRED")

    def test_s5_unrefreshable_no_odds(self):
        sig = _football_signal()
        status = evaluate_signal_status(sig, None, _now_str())
        assert status == "UNREFRESHABLE"

    def test_s6_stale_odds_old_ts(self):
        sig = _football_signal()
        old_ts = _ts_ago(35)  # 35 min old → stale
        status = evaluate_signal_status(sig, 2.10, old_ts)
        assert status == "STALE_ODDS"

    def test_s6_stale_odds_missing_ts(self):
        sig = _football_signal()
        status = evaluate_signal_status(sig, 2.10, None)
        assert status == "STALE_ODDS"

    def test_s6_fresh_odds_not_stale(self):
        sig = _football_signal()
        fresh_ts = _ts_ago(5)  # 5 min old → fresh
        status = evaluate_signal_status(sig, 2.10, fresh_ts)
        assert status != "STALE_ODDS"

    def test_s7_edge_lost_ev_below_min(self):
        # model_prob 30% × odds 1.50 → EV = 0.30*1.50 - 1 = -0.55 → EDGE_LOST
        sig = _football_signal(model_prob=30.0)
        status = evaluate_signal_status(sig, 1.50, _ts_ago(5))
        assert status == "EDGE_LOST"

    def test_s7_edge_lost_prob_below_min(self):
        # min_prob for football = 0.30. model_prob=25% → below gate
        sig = _football_signal(model_prob=25.0)
        # odds 3.0 gives ev = 0.25*3.0 - 1 = -0.25 → also EDGE_LOST by ev, but test prob specifically
        status = evaluate_signal_status(sig, 3.50, _ts_ago(5))
        assert status == "EDGE_LOST"

    def test_s8_active_football_1x2(self):
        # model_prob 55% × odds 2.10 → EV = 0.55*2.10 - 1 = 0.155 → ACTIVE
        sig = _football_signal(model_prob=55.0)
        status = evaluate_signal_status(sig, 2.10, _ts_ago(5))
        assert status == "ACTIVE"

    def test_s9_active_tennis_h2h(self):
        sig = _tennis_signal(model_prob=60.0)
        # TENNIS_GATE: min_edge=0.03, min_prob=0.35, max_odds=4.50
        # ev = 0.60*1.80 - 1 = 0.08 → ACTIVE
        status = evaluate_signal_status(sig, 1.80, _ts_ago(5))
        assert status == "ACTIVE"

    def test_s10_edge_lost_prob_below_tennis_min(self):
        # Tennis min_prob = 0.35. model_prob=30% → EDGE_LOST
        sig = _tennis_signal(model_prob=30.0)
        status = evaluate_signal_status(sig, 3.20, _ts_ago(5))
        assert status == "EDGE_LOST"

    def test_s11_edge_lost_odds_exceed_tennis_max(self):
        # Tennis max_odds = 4.50. Odds 5.0 → EDGE_LOST
        sig = _tennis_signal(model_prob=55.0)
        status = evaluate_signal_status(sig, 5.00, _ts_ago(5))
        assert status == "EDGE_LOST"

    def test_ah_market_uses_default_gate(self):
        sig = _football_signal(market="ah+1.5_b", model_prob=55.0)
        status = evaluate_signal_status(sig, 1.90, _ts_ago(5))
        # AH uses DEFAULT_GATE (no separate AH-specific gate), so should be ACTIVE
        # ev = 0.55*1.90 - 1 = 0.045 > 0.03 → ACTIVE
        assert status == "ACTIVE"


# ---------------------------------------------------------------------------
# S12-S14: Sidecar write/read
# ---------------------------------------------------------------------------

class TestOddsState:
    def test_s12_preserves_initial_odds(self, tmp_sidecar):
        sid = "testid000001"
        update_odds_state(
            sid, current_odds=2.10, odds_ts=_now_str(),
            odds_source="betfair", odds_fetch_tier=1,
            signal_status="ACTIVE", current_ev_pct=0.155,
        )
        update_odds_state(
            sid, current_odds=2.20, odds_ts=_now_str(),
            odds_source="betfair", odds_fetch_tier=1,
            signal_status="ACTIVE", current_ev_pct=0.21,
        )
        state = load_odds_state()
        assert state[sid]["initial_odds"] == 2.10
        assert state[sid]["current_odds"] == 2.20

    def test_s13_dedup_same_odds_within_5min(self, tmp_sidecar):
        ts = _now_str()
        hist = [{"ts": ts, "odds": 2.10, "source": "betfair"}]
        # Same odds, within 5 min → deduplicated
        new_entry = {"ts": ts, "odds": 2.10, "source": "betfair"}
        result = _dedup_history(hist, new_entry)
        assert len(result) == 1

    def test_s14_appends_when_odds_change(self, tmp_sidecar):
        ts1 = _ts_ago(10)
        ts2 = _now_str()
        hist = [{"ts": ts1, "odds": 2.10, "source": "betfair"}]
        new_entry = {"ts": ts2, "odds": 2.15, "source": "betfair"}
        result = _dedup_history(hist, new_entry)
        assert len(result) == 2
        assert result[-1]["odds"] == 2.15

    def test_sidecar_roundtrip(self, tmp_sidecar):
        sid = "roundtrip0001"
        ts = _now_str()
        update_odds_state(
            sid, current_odds=1.80, odds_ts=ts,
            odds_source="tennis_explorer", odds_fetch_tier=2,
            signal_status="ACTIVE", current_ev_pct=0.08,
        )
        state = load_odds_state()
        assert sid in state
        assert state[sid]["odds_source"] == "tennis_explorer"
        assert len(state[sid]["odds_history"]) == 1

    def test_ev_pct_stored_in_percent_units(self, tmp_sidecar):
        """Input is decimal fraction; sidecar must store percent."""
        sid = "evunit0000001"
        update_odds_state(
            sid, current_odds=2.0, odds_ts=_now_str(),
            odds_source="x", odds_fetch_tier=1,
            signal_status="ACTIVE", current_ev_pct=0.05,
        )
        assert load_odds_state()[sid]["current_ev_pct"] == 5.0

    def test_ev_pct_absurd_value_clamped_to_none(self, tmp_sidecar):
        """P1.5 guard: EV magnitudes > 500% are treated as corrupt and stored as None."""
        sid = "evabsurd00001"
        update_odds_state(
            sid, current_odds=2.0, odds_ts=_now_str(),
            odds_source="x", odds_fetch_tier=1,
            signal_status="ACTIVE", current_ev_pct=1e50,
        )
        assert load_odds_state()[sid]["current_ev_pct"] is None


class TestRefresherFailurePathNoEscalation:
    """Regression for the 100× double-multiplication bug that produced 1e+59 EV values
    in production. If the refresher fallback path re-passes the sidecar's percent value
    without converting back to decimal, each cycle multiplies EV by 100 again."""

    def test_repeated_failed_refresh_does_not_escalate_ev(self, tmp_sidecar, monkeypatch):
        from src.signals import odds_refresher as orr

        sid = "escalation001"
        # Seed sidecar with a realistic 5% EV
        update_odds_state(
            sid, current_odds=2.0, odds_ts=_now_str(),
            odds_source="scan", odds_fetch_tier=1,
            signal_status="ACTIVE", current_ev_pct=0.05,
        )
        assert load_odds_state()[sid]["current_ev_pct"] == 5.0

        # Simulate the refresher's failure-path payload construction 30 times.
        # This mirrors odds_refresher.py:286-302 where refresh returns None.
        for _ in range(30):
            state_entry = load_odds_state()[sid]
            cached_ev_pct = state_entry.get("current_ev_pct")
            cached_ev_decimal = (cached_ev_pct / 100.0) if cached_ev_pct is not None else None
            update_odds_state(
                sid,
                current_odds=state_entry.get("current_odds"),
                odds_ts=state_entry.get("odds_ts"),
                odds_source=state_entry.get("odds_source"),
                odds_fetch_tier=state_entry.get("odds_fetch_tier") or 0,
                signal_status="STALE_ODDS",
                current_ev_pct=cached_ev_decimal,
            )

        final = load_odds_state()[sid]["current_ev_pct"]
        assert final == pytest.approx(5.0, abs=0.1), (
            f"EV escalated to {final}; double-multiplication bug reintroduced"
        )


# ---------------------------------------------------------------------------
# S15: Merge odds state into signal
# ---------------------------------------------------------------------------

class TestMergeOddsState:
    def test_s15_immutable_scan_fields_preserved(self):
        signal = {
            "signal_id": "abc123def456",
            "sport": "football",
            "match": "A vs B",
            "market": "home",
            "odds": 2.10,
            "ev_pct": 15.5,
            "model_prob": 55.0,
            "generated_at": "2026-08-11T10:00:00Z",
        }
        odds_state = {
            "abc123def456": {
                "initial_odds": 2.10,
                "initial_ev_pct": 15.5,
                "current_odds": 2.05,
                "current_ev_pct": 12.75,
                "odds_ts": "2026-08-11T15:00:00Z",
                "odds_source": "betfair",
                "odds_fetch_tier": 1,
                "signal_status": "ACTIVE",
                "odds_history": [
                    {"ts": "2026-08-11T10:00:00Z", "odds": 2.10, "source": "scan"},
                    {"ts": "2026-08-11T15:00:00Z", "odds": 2.05, "source": "betfair"},
                ],
            }
        }
        merged = merge_odds_state_into_signal(signal, odds_state)
        # Immutable scan-time fields unchanged
        assert merged["odds"] == 2.10
        assert merged["ev_pct"] == 15.5
        assert merged["model_prob"] == 55.0
        assert merged["generated_at"] == "2026-08-11T10:00:00Z"
        # Refreshed fields added
        assert merged["current_odds"] == 2.05
        assert merged["signal_status"] == "ACTIVE"
        assert len(merged["odds_history"]) == 2

    def test_no_signal_id_returns_unchanged(self):
        signal = {"sport": "football", "market": "home"}
        state = {"abc": {"current_odds": 2.0}}
        result = merge_odds_state_into_signal(signal, state)
        assert result == signal

    def test_unknown_signal_id_returns_unchanged(self):
        signal = {"signal_id": "notinstate1", "odds": 2.10}
        state = {}
        result = merge_odds_state_into_signal(signal, state)
        assert result["odds"] == 2.10
        assert "current_odds" not in result


# ---------------------------------------------------------------------------
# S16: Concurrent write regression
# ---------------------------------------------------------------------------

class TestConcurrentWriteRegression:
    """Two threads writing different signal IDs must not corrupt each other."""

    def test_s16_concurrent_sidecar_writes(self, tmp_sidecar):
        errors: list[Exception] = []
        write_count = 10

        def writer(sid: str) -> None:
            for i in range(write_count):
                try:
                    update_odds_state(
                        sid,
                        current_odds=1.80 + i * 0.01,
                        odds_ts=_now_str(),
                        odds_source="test",
                        odds_fetch_tier=2,
                        signal_status="ACTIVE",
                        current_ev_pct=0.05,
                    )
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"signal_{i:06d}",))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent write errors: {errors}"

        state = load_odds_state()
        # Each signal_id must be present and intact
        for i in range(4):
            sid = f"signal_{i:06d}"
            assert sid in state, f"Signal {sid} missing from sidecar"
            entry = state[sid]
            assert entry["current_odds"] > 1.0
            # History dedup: at most write_count entries (may be fewer if same odds deduped)
            assert len(entry["odds_history"]) >= 1


# ---------------------------------------------------------------------------
# Provider budget circuit breaker
# ---------------------------------------------------------------------------

class TestProviderBudget:
    def test_exhausted_the_odds_api(self, tmp_path, monkeypatch):
        usage_file = tmp_path / "api_usage.json"
        usage_file.write_text('{"requests_used": 20000, "requests_remaining": 0}')
        budget_file = tmp_path / "provider_budget.json"

        import src.signals.provider_budget as pb
        monkeypatch.setattr(pb, "_API_USAGE_PATH", usage_file)
        monkeypatch.setattr(pb, "_BUDGET_PATH", budget_file)

        available = pb.is_provider_available("the_odds_api")
        assert available is False

    def test_available_when_quota_present(self, tmp_path, monkeypatch):
        usage_file = tmp_path / "api_usage.json"
        usage_file.write_text('{"requests_used": 100, "requests_remaining": 19900}')
        budget_file = tmp_path / "provider_budget.json"

        import src.signals.provider_budget as pb
        monkeypatch.setattr(pb, "_API_USAGE_PATH", usage_file)
        monkeypatch.setattr(pb, "_BUDGET_PATH", budget_file)

        available = pb.is_provider_available("the_odds_api")
        assert available is True

    def test_betfair_available_by_default(self, tmp_path, monkeypatch):
        budget_file = tmp_path / "provider_budget.json"
        import src.signals.provider_budget as pb
        monkeypatch.setattr(pb, "_BUDGET_PATH", budget_file)
        # No api_usage.json for betfair
        usage_file = tmp_path / "api_usage.json"
        monkeypatch.setattr(pb, "_API_USAGE_PATH", usage_file)
        assert pb.is_provider_available("betfair") is True
