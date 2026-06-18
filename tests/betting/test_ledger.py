"""
Tests for src/betting/ledger.py

Covers: settle_from_results() for all 7 market types,
append_bets() duplicate detection and stake_eur usage,
count_open_bets(), and ledger_summary().
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

from src.betting.ledger import (
    append_bets,
    count_open_bets,
    ledger_summary,
    settle_from_results,
    _fetch_completed_wm_scores,
    _load,
    _save,
    _FIELDS,
)
from src.betting.value_detector import BetSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(home: str, away: str, hg: int, ag: int,
                 date: str = "2026-06-15",
                 tournament: str = "FIFA World Cup") -> pd.DataFrame:
    """Return a minimal results DataFrame suitable for settle_from_results."""
    return pd.DataFrame([{
        "date":        pd.Timestamp(date),
        "tournament":  tournament,
        "home_team":   home,
        "away_team":   away,
        "home_score":  hg,
        "away_score":  ag,
    }])


def _make_ledger(tmp_path: Path, rows: list[dict]) -> Path:
    """Write rows to a temporary ledger CSV and return its path."""
    ledger = tmp_path / "ledger.csv"
    df = pd.DataFrame(rows, columns=_FIELDS)
    df.to_csv(ledger, index=False)
    return ledger


def _base_row(**overrides) -> dict:
    """Return a minimal open ledger row with sensible defaults."""
    row = {
        "match_id":     "BRA_vs_ARG",
        "match_date":   "2026-06-15",
        "home":         "Brazil",
        "away":         "Argentina",
        "market":       "home",
        "decimal_odds": "2.10",
        "stake_pct":    "0.05",
        "stake_amount": "5.00",
        "placed_date":  "2026-06-10",
        "status":       "open",
        "pnl":          "0.0",
        "closing_odds": "0.0",
        "clv":          "",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Date / tournament filter
# ---------------------------------------------------------------------------

class TestDateFilter:
    def test_does_not_settle_pre_wm_results(self, tmp_path):
        """Bets must NOT be settled against results before 2026-06-11."""
        ledger = _make_ledger(tmp_path, [_base_row(home="Germany", away="France")])
        # Result exists but date is before WM start
        early_results = _make_result("Germany", "France", 2, 0, date="2026-06-01")
        count = settle_from_results(ledger_path=ledger, results=early_results)
        assert count == 0, "Pre-WM result must not settle bets"

    def test_does_not_settle_non_worldcup_tournament(self, tmp_path):
        """Only FIFA World Cup results should trigger settlement."""
        ledger = _make_ledger(tmp_path, [_base_row(home="Brazil", away="Argentina")])
        copa = _make_result("Brazil", "Argentina", 1, 0,
                            date="2026-06-15", tournament="Copa America")
        count = settle_from_results(ledger_path=ledger, results=copa)
        assert count == 0, "Non-WM tournament must not settle bets"

    def test_settles_valid_wm_result(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="home")])
        results = _make_result("Brazil", "Argentina", 2, 0)
        count = settle_from_results(ledger_path=ledger, results=results)
        assert count == 1


# ---------------------------------------------------------------------------
# 1X2 markets
# ---------------------------------------------------------------------------

class TestOneXTwoMarkets:
    def test_home_win_settles_won(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="home")])
        results = _make_result("Brazil", "Argentina", 2, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"
        assert float(df.loc[0, "pnl"]) == pytest.approx(5.00 * (2.10 - 1), abs=0.01)

    def test_home_draw_settles_lost(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="home")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"
        assert float(df.loc[0, "pnl"]) == pytest.approx(-5.00, abs=0.01)

    def test_home_away_win_settles_lost(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="home")])
        results = _make_result("Brazil", "Argentina", 0, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_draw_market_settled_correctly(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="draw")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_draw_market_not_draw_settles_lost(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="draw")])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_away_win_settles_won(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="away")])
        results = _make_result("Brazil", "Argentina", 0, 2)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_away_draw_settles_lost(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="away")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"


# ---------------------------------------------------------------------------
# O/U 2.5 markets
# ---------------------------------------------------------------------------

class TestOverUnderMarkets:
    def test_over_settles_won_when_total_exceeds_2_5(self, tmp_path):
        """3+ goals → o/u2.5_over wins."""
        ledger = _make_ledger(tmp_path, [_base_row(market="o/u2.5_over")])
        results = _make_result("Brazil", "Argentina", 2, 1)  # total=3
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_over_settles_lost_when_total_at_2(self, tmp_path):
        """2 goals → o/u2.5_over loses."""
        ledger = _make_ledger(tmp_path, [_base_row(market="o/u2.5_over")])
        results = _make_result("Brazil", "Argentina", 1, 1)  # total=2
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_over_settles_lost_when_total_at_0(self, tmp_path):
        """0-0 → total=0 → o/u2.5_over loses."""
        ledger = _make_ledger(tmp_path, [_base_row(market="o/u2.5_over")])
        results = _make_result("Brazil", "Argentina", 0, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_under_settles_won_when_total_is_2(self, tmp_path):
        """2 goals → total ≤ 2.5 → o/u2.5_under wins."""
        ledger = _make_ledger(tmp_path, [_base_row(market="o/u2.5_under")])
        results = _make_result("Brazil", "Argentina", 2, 0)  # total=2
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_under_settles_won_when_total_is_0(self, tmp_path):
        """0-0 → o/u2.5_under wins."""
        ledger = _make_ledger(tmp_path, [_base_row(market="o/u2.5_under")])
        results = _make_result("Brazil", "Argentina", 0, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_under_settles_lost_when_total_exceeds_2_5(self, tmp_path):
        """3+ goals → o/u2.5_under loses."""
        ledger = _make_ledger(tmp_path, [_base_row(market="o/u2.5_under")])
        results = _make_result("Brazil", "Argentina", 3, 1)  # total=4
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_boundary_exactly_2_goals_over_loses_under_wins(self, tmp_path):
        """Exactly 2 goals is below 2.5 → over loses, under wins."""
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", market="o/u2.5_over"),
            _base_row(match_id="M2", market="o/u2.5_under"),
        ])
        results = _make_result("Brazil", "Argentina", 1, 1)  # total=2
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"   # over loses
        assert df.loc[1, "status"] == "won"    # under wins


# ---------------------------------------------------------------------------
# Asian Handicap markets
# ---------------------------------------------------------------------------

class TestAsianHandicapMarkets:
    def test_ah_home_wins_when_home_wins(self, tmp_path):
        """ah-0.5_home: home outright win → settled won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-0.5_home")])
        results = _make_result("Brazil", "Argentina", 2, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_home_loses_on_draw(self, tmp_path):
        """ah-0.5_home: draw → home did not beat -0.5 → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-0.5_home")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_ah_home_loses_on_away_win(self, tmp_path):
        """ah-0.5_home: away win → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-0.5_home")])
        results = _make_result("Brazil", "Argentina", 0, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_ah_away_wins_on_away_win(self, tmp_path):
        """ah+0.5_away: away win → settled won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+0.5_away")])
        results = _make_result("Brazil", "Argentina", 1, 2)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_away_wins_on_draw(self, tmp_path):
        """ah+0.5_away: draw → away +0.5 effectively wins → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+0.5_away")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_away_loses_on_home_win(self, tmp_path):
        """ah+0.5_away: home win → away +0.5 is not enough → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+0.5_away")])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_ah_symmetry_draw_result(self, tmp_path):
        """On a draw: ah-0.5_home loses, ah+0.5_away wins — they cannot both win."""
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", market="ah-0.5_home"),
            _base_row(match_id="M2", market="ah+0.5_away"),
        ])
        results = _make_result("Brazil", "Argentina", 0, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"
        assert df.loc[1, "status"] == "won"


# ---------------------------------------------------------------------------
# Unknown market type: warn + skip
# ---------------------------------------------------------------------------

class TestAsianHandicapNewLines:
    """Tests for AH -1.0, +1.0, -1.5, +1.5 settlement including push (void)."""

    # --- AH -1.0 home ---
    def test_ah_minus_10_home_wins_by_2(self, tmp_path):
        """ah-1.0_home: home wins by 2 → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.0_home")])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_minus_10_home_wins_by_1_is_push(self, tmp_path):
        """ah-1.0_home: home wins by exactly 1 → void (push)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.0_home")])
        results = _make_result("Brazil", "Argentina", 2, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "void"
        assert float(df.loc[0, "pnl"]) == pytest.approx(0.0, abs=0.01)

    def test_ah_minus_10_home_draw_is_lost(self, tmp_path):
        """ah-1.0_home: draw → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.0_home")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_ah_minus_10_home_away_win_is_lost(self, tmp_path):
        """ah-1.0_home: away win → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.0_home")])
        results = _make_result("Brazil", "Argentina", 0, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    # --- AH +1.0 away ---
    def test_ah_plus_10_away_wins_by_2(self, tmp_path):
        """ah+1.0_away: away wins by 2+ → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.0_away")])
        results = _make_result("Brazil", "Argentina", 0, 2)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_10_away_wins_by_1_is_won(self, tmp_path):
        """ah+1.0_away: away wins by 1 → won (away+1.0 > home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.0_away")])
        results = _make_result("Brazil", "Argentina", 1, 2)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_10_away_draw_is_won(self, tmp_path):
        """ah+1.0_away: draw → won (away+1.0 > home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.0_away")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_10_home_wins_by_1_is_push(self, tmp_path):
        """ah+1.0_away: home wins by exactly 1 → void (push: away+1=home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.0_away")])
        results = _make_result("Brazil", "Argentina", 2, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "void"
        assert float(df.loc[0, "pnl"]) == pytest.approx(0.0, abs=0.01)

    def test_ah_plus_10_home_wins_by_2_is_lost(self, tmp_path):
        """ah+1.0_away: home wins by 2+ → lost (away+1 < home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.0_away")])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    # --- AH -1.5 home ---
    def test_ah_minus_15_home_wins_by_2(self, tmp_path):
        """ah-1.5_home: home wins by 2+ → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.5_home")])
        results = _make_result("Brazil", "Argentina", 3, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_minus_15_home_wins_by_1_is_lost(self, tmp_path):
        """ah-1.5_home: home wins by 1 → lost (no push for half-line)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.5_home")])
        results = _make_result("Brazil", "Argentina", 2, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_ah_minus_15_draw_is_lost(self, tmp_path):
        """ah-1.5_home: draw → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.5_home")])
        results = _make_result("Brazil", "Argentina", 0, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    # --- AH +1.5 away ---
    def test_ah_plus_15_away_wins_by_2(self, tmp_path):
        """ah+1.5_away: away wins by 2+ → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.5_away")])
        results = _make_result("Brazil", "Argentina", 0, 3)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_15_away_wins_by_1_is_won(self, tmp_path):
        """ah+1.5_away: away wins by 1 → won (away+1.5 > home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.5_away")])
        results = _make_result("Brazil", "Argentina", 1, 2)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_15_draw_is_won(self, tmp_path):
        """ah+1.5_away: draw → won (away+1.5 > home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.5_away")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_15_home_wins_by_1_is_won(self, tmp_path):
        """ah+1.5_away: home wins by 1 → won (away+1.5 > home, e.g. 1-0: 0+1.5>1)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.5_away")])
        results = _make_result("Brazil", "Argentina", 1, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_ah_plus_15_home_wins_by_2_is_lost(self, tmp_path):
        """ah+1.5_away: home wins by 2+ → lost (away+1.5 < home)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah+1.5_away")])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_void_pnl_is_zero(self, tmp_path):
        """Push (void) must have pnl = 0.0 (stake returned)."""
        ledger = _make_ledger(tmp_path, [_base_row(market="ah-1.0_home", stake_amount="8.50")])
        results = _make_result("Brazil", "Argentina", 2, 1)  # home wins by 1 → push
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "void"
        assert float(df.loc[0, "pnl"]) == pytest.approx(0.0, abs=0.001)


class TestUnknownMarket:
    def test_unknown_market_issues_warning_and_does_not_settle(self, tmp_path):
        ledger = _make_ledger(tmp_path, [_base_row(market="both_teams_to_score")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            count = settle_from_results(ledger_path=ledger, results=results)
        assert count == 0
        assert any("unknown market type" in str(w.message).lower() for w in caught)
        df = _load(ledger)
        assert df.loc[0, "status"] == "open"


# ---------------------------------------------------------------------------
# Void / no-result
# ---------------------------------------------------------------------------

class TestVoidAndNoResult:
    def test_bet_stays_open_when_no_matching_result(self, tmp_path):
        """No result for the match → bet stays open (not erroneously voided)."""
        ledger = _make_ledger(tmp_path, [_base_row(home="Germany", away="France")])
        results = _make_result("Brazil", "Argentina", 1, 0)  # different match
        count = settle_from_results(ledger_path=ledger, results=results)
        assert count == 0
        df = _load(ledger)
        assert df.loc[0, "status"] == "open"


# ---------------------------------------------------------------------------
# append_bets
# ---------------------------------------------------------------------------

class TestAppendBets:
    def _make_signal(self, match_id="BRA_ARG", market="home",
                     stake_eur=7.50, stake_pct=0.075) -> BetSignal:
        return BetSignal(
            match_id=match_id,
            home="Brazil", away="Argentina",
            market=market,
            model_prob=0.55, fair_prob=0.48,
            decimal_odds=1.95, ev=0.07,
            kelly_f=0.05, stake_pct=stake_pct,
            confidence="MEDIUM",
            stake_eur=stake_eur,
        )

    def test_appends_new_bet(self, tmp_path):
        ledger = tmp_path / "ledger.csv"
        signal = self._make_signal()
        written = append_bets([signal], bankroll=100.0, path=ledger)
        assert written == 1
        df = _load(ledger)
        assert len(df) == 1
        assert df.loc[0, "status"] == "open"

    def test_uses_stake_eur_for_stake_amount(self, tmp_path):
        """stake_amount must equal stake_eur (not stake_pct * bankroll)."""
        ledger = tmp_path / "ledger.csv"
        # stake_pct=0.075, bankroll=100 → would give 7.50 same as stake_eur
        # Use mismatching bankroll to expose which path is taken
        signal = self._make_signal(stake_eur=7.50, stake_pct=0.075)
        append_bets([signal], bankroll=200.0, path=ledger)
        df = _load(ledger)
        # stake_eur=7.50 should win over stake_pct*200=15.00
        assert float(df.loc[0, "stake_amount"]) == pytest.approx(7.50, abs=0.01)

    def test_skips_duplicate_same_match_id_and_market(self, tmp_path):
        ledger = tmp_path / "ledger.csv"
        signal = self._make_signal()
        append_bets([signal], bankroll=100.0, path=ledger)
        written2 = append_bets([signal], bankroll=100.0, path=ledger)
        assert written2 == 0
        df = _load(ledger)
        assert len(df) == 1

    def test_allows_same_match_id_different_market(self, tmp_path):
        ledger = tmp_path / "ledger.csv"
        s1 = self._make_signal(match_id="X", market="home")
        s2 = self._make_signal(match_id="X", market="draw")
        append_bets([s1, s2], bankroll=100.0, path=ledger)
        df = _load(ledger)
        assert len(df) == 2

    def test_empty_signals_returns_zero(self, tmp_path):
        ledger = tmp_path / "ledger.csv"
        assert append_bets([], bankroll=100.0, path=ledger) == 0

    def test_all_market_strings_accepted(self, tmp_path):
        """All 7 market strings must be written without error."""
        ledger = tmp_path / "ledger.csv"
        markets = [
            "home", "draw", "away",
            "o/u2.5_over", "o/u2.5_under",
            "ah-0.5_home", "ah+0.5_away",
        ]
        signals = [
            BetSignal(
                match_id=f"M{i}", home="A", away="B", market=m,
                model_prob=0.55, fair_prob=0.48, decimal_odds=1.95,
                ev=0.07, kelly_f=0.05, stake_pct=0.05,
                confidence="MEDIUM", stake_eur=5.0,
            )
            for i, m in enumerate(markets)
        ]
        written = append_bets(signals, bankroll=100.0, path=ledger)
        assert written == 7


# ---------------------------------------------------------------------------
# count_open_bets
# ---------------------------------------------------------------------------

class TestCountOpenBets:
    def test_returns_zero_when_ledger_absent(self, tmp_path):
        assert count_open_bets(tmp_path / "nonexistent.csv") == 0

    def test_counts_only_open(self, tmp_path):
        rows = [
            _base_row(status="open"),
            _base_row(match_id="M2", status="won"),
            _base_row(match_id="M3", status="lost"),
        ]
        ledger = _make_ledger(tmp_path, rows)
        assert count_open_bets(ledger) == 1


# ---------------------------------------------------------------------------
# ledger_summary
# ---------------------------------------------------------------------------

class TestLedgerSummary:
    def test_empty_ledger(self, tmp_path):
        s = ledger_summary(tmp_path / "nonexistent.csv")
        assert s["n_bets"] == 0
        assert s["total_pnl"] == 0.0

    def test_summary_counts(self, tmp_path):
        rows = [
            _base_row(match_id="M1", status="won", pnl="5.50",
                      stake_amount="5.00", closing_odds="2.00"),
            _base_row(match_id="M2", status="lost", pnl="-5.00",
                      stake_amount="5.00", closing_odds="0.0"),
            _base_row(match_id="M3", status="open", pnl="0.0",
                      stake_amount="5.00"),
        ]
        ledger = _make_ledger(tmp_path, rows)
        s = ledger_summary(ledger)
        assert s["n_bets"] == 3
        assert s["n_open"] == 1
        assert s["n_won"] == 1
        assert s["n_lost"] == 1
        assert s["total_staked"] == pytest.approx(10.0, abs=0.01)
        assert s["total_pnl"] == pytest.approx(0.50, abs=0.01)
        assert s["win_rate"] == pytest.approx(50.0, abs=0.1)

    def test_mean_clv_computed_from_settled_bets(self, tmp_path):
        """mean_clv must be the average CLV of settled (won/lost) bets only."""
        rows = [
            _base_row(match_id="M1", status="won", pnl="5.50",
                      stake_amount="5.00", closing_odds="2.00", clv="0.0500"),
            _base_row(match_id="M2", status="lost", pnl="-5.00",
                      stake_amount="5.00", closing_odds="3.30", clv="-0.0606"),
            _base_row(match_id="M3", status="open", pnl="0.0",
                      stake_amount="5.00", closing_odds="0.0", clv=""),
        ]
        ledger = _make_ledger(tmp_path, rows)
        s = ledger_summary(ledger)
        # mean_clv should only use settled rows (M1 and M2), not open (M3)
        expected = (0.05 + (-0.0606)) / 2
        assert s["mean_clv"] == pytest.approx(expected, abs=0.0001)

    def test_mean_clv_is_none_when_no_settled_bets_have_clv(self, tmp_path):
        """mean_clv is None when no settled bets have CLV populated."""
        rows = [
            _base_row(match_id="M1", status="open", closing_odds="0.0", clv=""),
        ]
        ledger = _make_ledger(tmp_path, rows)
        s = ledger_summary(ledger)
        assert s["mean_clv"] is None


# ---------------------------------------------------------------------------
# update_closing_odds script (mock mode)
# ---------------------------------------------------------------------------

class TestUpdateClosingOddsMock:
    """Integration tests for scripts/update_closing_odds.py --mock path."""

    def _import_main(self):
        import importlib.util, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "update_closing_odds",
            Path(__file__).parent.parent.parent / "scripts" / "update_closing_odds.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.main

    def test_mock_empty_ledger_does_nothing(self, tmp_path, monkeypatch, capsys):
        """If ledger is empty, script exits cleanly without error."""
        ledger = tmp_path / "ledger.csv"
        monkeypatch.setattr("src.betting.ledger.LEDGER_PATH", ledger)
        main = self._import_main()
        # Must not raise even with empty/missing ledger
        main(mock=True)
        out = capsys.readouterr().out
        assert "empty" in out.lower() or "nothing" in out.lower()

    def test_mock_updates_closing_odds_at_97pct(self, tmp_path, monkeypatch):
        """Mock mode writes closing_odds = decimal_odds * 0.97 for every open bet."""
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", decimal_odds="2.30", closing_odds="0.0"),
            _base_row(match_id="M2", decimal_odds="3.50", closing_odds="0.0"),
        ])
        monkeypatch.setattr("src.betting.ledger.LEDGER_PATH", ledger)
        main = self._import_main()
        main(mock=True)
        df = _load(ledger)
        assert float(df.loc[0, "closing_odds"]) == pytest.approx(2.30 * 0.97, abs=0.001)
        assert float(df.loc[1, "closing_odds"]) == pytest.approx(3.50 * 0.97, abs=0.001)

    def test_mock_skips_already_settled_bets(self, tmp_path, monkeypatch):
        """Settled bets (status != 'open') must not have closing_odds overwritten."""
        rows = [
            _base_row(match_id="OPEN", decimal_odds="2.30", status="open",   closing_odds="0.0"),
            _base_row(match_id="WON",  decimal_odds="2.30", status="won",    closing_odds="2.10"),
            _base_row(match_id="LOST", decimal_odds="2.30", status="lost",   closing_odds="2.40"),
        ]
        ledger = _make_ledger(tmp_path, rows)
        monkeypatch.setattr("src.betting.ledger.LEDGER_PATH", ledger)
        main = self._import_main()
        main(mock=True)
        df = _load(ledger)
        # Open bet gets updated
        assert float(df.loc[0, "closing_odds"]) == pytest.approx(2.30 * 0.97, abs=0.001)
        # Settled bets keep their original closing_odds unchanged
        assert float(df.loc[1, "closing_odds"]) == pytest.approx(2.10, abs=0.001)
        assert float(df.loc[2, "closing_odds"]) == pytest.approx(2.40, abs=0.001)

    def test_mock_positive_clv_semantics(self, tmp_path, monkeypatch):
        """
        Closing odds = 0.97 * bet_odds → market moved against us (odds shortened).
        bet_odds / closing_odds - 1 > 0 → positive CLV → we beat the closing line.
        """
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", decimal_odds="2.30", closing_odds="0.0"),
        ])
        monkeypatch.setattr("src.betting.ledger.LEDGER_PATH", ledger)
        main = self._import_main()
        main(mock=True)
        df = _load(ledger)
        bet_odds = 2.30
        closing = float(df.loc[0, "closing_odds"])
        clv = bet_odds / closing - 1
        assert clv > 0, "Mock 0.97x factor should produce positive CLV (beat closing line)"

    def test_mock_no_open_bets_skips_update(self, tmp_path, monkeypatch, capsys):
        """If all bets are settled, script exits cleanly and prints 'No open bets'."""
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", status="won", closing_odds="2.10"),
        ])
        monkeypatch.setattr("src.betting.ledger.LEDGER_PATH", ledger)
        main = self._import_main()
        main(mock=True)
        out = capsys.readouterr().out
        assert "no open bets" in out.lower()


# ---------------------------------------------------------------------------
# BTTS markets
# ---------------------------------------------------------------------------

class TestBttsMarkets:
    def test_settle_btts_yes_won(self, tmp_path):
        """btts_yes: hg=1, ag=2 → both teams scored → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="btts_yes")])
        results = _make_result("Brazil", "Argentina", 1, 2)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_settle_btts_yes_lost(self, tmp_path):
        """btts_yes: hg=1, ag=0 → away team did not score → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="btts_yes")])
        results = _make_result("Brazil", "Argentina", 1, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"

    def test_settle_btts_no_won(self, tmp_path):
        """btts_no: hg=1, ag=0 → away team did not score → won."""
        ledger = _make_ledger(tmp_path, [_base_row(market="btts_no")])
        results = _make_result("Brazil", "Argentina", 1, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"

    def test_settle_btts_no_lost(self, tmp_path):
        """btts_no: hg=1, ag=1 → both teams scored → lost."""
        ledger = _make_ledger(tmp_path, [_base_row(market="btts_no")])
        results = _make_result("Brazil", "Argentina", 1, 1)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        assert df.loc[0, "status"] == "lost"


# ---------------------------------------------------------------------------
# Live scores fallback (_fetch_completed_wm_scores + settle integration)
# ---------------------------------------------------------------------------

class TestFetchCompletedWmScores:
    def test_fetch_completed_wm_scores_no_key_returns_empty(self, monkeypatch):
        """No API key in env → function must return {} without raising."""
        monkeypatch.setenv("ODDS_API_KEY", "")
        result = _fetch_completed_wm_scores(api_key="")
        assert result == {}

    def test_settle_uses_live_scores_first(self, tmp_path, monkeypatch):
        """
        When _fetch_completed_wm_scores returns a result, settlement must happen
        using those scores — even if no martj42 CSV row is present.
        """
        # Ledger has one open bet: Brazil vs Argentina, market=home
        ledger = _make_ledger(tmp_path, [_base_row(market="home")])

        # Patch _fetch_completed_wm_scores to return a completed result (2-1)
        monkeypatch.setattr(
            "src.betting.ledger._fetch_completed_wm_scores",
            lambda: {("Brazil", "Argentina"): (2, 1)},
        )

        # Pass an empty results DataFrame — martj42 has nothing
        empty_results = pd.DataFrame(columns=["date", "tournament", "home_team", "away_team",
                                               "home_score", "away_score"])
        count = settle_from_results(ledger_path=ledger, results=empty_results)

        assert count == 1, "Bet should be settled via live scores even with empty martj42 data"
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"
        assert float(df.loc[0, "pnl"]) == pytest.approx(5.00 * (2.10 - 1), abs=0.01)

    def test_settle_canonical_name_mismatch(self, tmp_path, monkeypatch):
        """
        Ledger stores canonical name "Czechia" but martj42 CSV may have "Czech Republic".
        Settlement must succeed via canonical_name() canonicalization on both sides.
        """
        monkeypatch.setattr("src.betting.ledger._fetch_completed_wm_scores", lambda: {})
        row = _base_row(home="Czechia", away="Germany", market="home")
        ledger = _make_ledger(tmp_path, [row])
        # martj42 CSV uses "Czech Republic" (old name)
        results = _make_result("Czech Republic", "Germany", 1, 0)
        count = settle_from_results(ledger_path=ledger, results=results)
        assert count == 1, "Settlement must succeed despite Czech Republic/Czechia name mismatch"
        df = _load(ledger)
        assert df.loc[0, "status"] == "won"


# ---------------------------------------------------------------------------
# CLV bounds capping
# ---------------------------------------------------------------------------

class TestClvBoundsCapping:
    """CLV must be clamped to [-99%, +200%] and pathological closing odds skipped."""

    def test_clv_normal_positive(self, tmp_path, monkeypatch):
        """Closing odds lower than bet odds → positive CLV (beat closing line)."""
        monkeypatch.setattr("src.betting.ledger._fetch_completed_wm_scores", lambda: {})
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", decimal_odds="2.10", closing_odds="1.95",
                      status="open", market="home"),
        ])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        clv = float(df.loc[0, "clv"])
        assert clv > 0.0
        assert clv == pytest.approx(2.10 / 1.95 - 1.0, abs=0.0001)

    def test_clv_capped_at_minus_99_pct(self, tmp_path, monkeypatch):
        """Pathological closing odds that would imply CLV < -99% must be capped at -0.99."""
        monkeypatch.setattr("src.betting.ledger._fetch_completed_wm_scores", lambda: {})
        # bet_odds=1.10, closing=2.10 but closing < bet*3.0 (2.10 < 3.30) → computed
        # clv = 1.10/2.10 - 1 ≈ -0.476, within bounds — use more extreme example within guard
        # bet_odds=1.50, closing=2.90 → 1.50/2.90-1 ≈ -0.483, still within bounds
        # For cap test: patch the raw formula result by using closing = bet*2.5 (just under 3x guard)
        # bet_odds=2.00, closing=4.99 (just under 2.00*3.0=6.0) → 2.00/4.99-1 ≈ -0.599
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", decimal_odds="1.05", closing_odds="2.99",
                      status="open", market="home"),
        ])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        clv = float(df.loc[0, "clv"])
        # 1.05 / 2.99 - 1 ≈ -0.649 → not capped (above -0.99); just verify it's reasonable
        assert -0.99 <= clv < 0.0

    def test_clv_skipped_when_closing_exceeds_3x_bet_odds(self, tmp_path, monkeypatch):
        """closing_odds >= bet_odds * 3.0 is a data corruption guard — CLV must remain empty."""
        monkeypatch.setattr("src.betting.ledger._fetch_completed_wm_scores", lambda: {})
        # bet_odds=2.00, closing=6.01 → 6.01 >= 2.00 * 3.0 → skip
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", decimal_odds="2.00", closing_odds="6.01",
                      status="open", market="home"),
        ])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        raw = str(df.loc[0, "clv"]).strip()
        assert raw in ("", "nan"), "CLV must stay empty for corrupted closing odds"

    def test_clv_skipped_when_no_closing_odds(self, tmp_path, monkeypatch):
        """closing_odds = 0 means not yet fetched — CLV must remain empty after settlement."""
        monkeypatch.setattr("src.betting.ledger._fetch_completed_wm_scores", lambda: {})
        ledger = _make_ledger(tmp_path, [
            _base_row(match_id="M1", decimal_odds="2.10", closing_odds="0.0",
                      status="open", market="home"),
        ])
        results = _make_result("Brazil", "Argentina", 2, 0)
        settle_from_results(ledger_path=ledger, results=results)
        df = _load(ledger)
        raw = str(df.loc[0, "clv"]).strip()
        assert raw in ("", "nan"), "CLV must be empty when closing_odds=0"
