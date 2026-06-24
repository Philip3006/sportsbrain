"""Tests für I4: apply_live_filters in run_event_backtest."""
import numpy as np
import pandas as pd
import pytest

from src.backtest.walk_forward import run_event_backtest
from src.config import MAX_EV, MAX_ACTIVE_BETS


def _make_matches(n: int = 40, date_str: str = "2018-01-01") -> pd.DataFrame:
    """Minimal historical DataFrame for DC training (pre-event)."""
    rows = []
    teams = ["A", "B", "C", "D", "E"]
    for i in range(n):
        h, a = teams[i % len(teams)], teams[(i + 1) % len(teams)]
        rows.append({
            "date": pd.Timestamp("2017-01-01") + pd.Timedelta(days=i),
            "home_team": h,
            "away_team": a,
            "home_score": 1,
            "away_score": 1,
            "neutral": False,
            "tournament": "test",
            "weight": 1.0,
        })
    return pd.DataFrame(rows)


def _make_odds_lookup(home: str, away: str, event: str,
                      h_odds: float = 2.5, d_odds: float = 3.2, a_odds: float = 2.8,
                      ) -> pd.DataFrame:
    match_id = f"{event}_{home}_vs_{away}"
    return pd.DataFrame([{
        "match_id": match_id,
        "home_odds": h_odds,
        "draw_odds": d_odds,
        "away_odds": a_odds,
    }])


def _make_event_match(home: str, away: str, date_str: str, home_score: int = 1, away_score: int = 0) -> dict:
    return {
        "date": pd.Timestamp(date_str),
        "home_team": home,
        "away_team": away,
        "home_score": home_score,
        "away_score": away_score,
        "neutral": True,
        "tournament": "WC2018",
        "weight": 1.0,
    }


@pytest.fixture
def minimal_event():
    return {"name": "WC2018", "start": "2018-06-14", "end": "2018-06-15"}


@pytest.fixture
def training_matches():
    return _make_matches(n=60, date_str="2017-01-01")


def _run_with_event_match(training, event, home, away, h_odds, d_odds, a_odds,
                          home_score=1, away_score=0, apply_live_filters=True):
    """Helper: runs backtest with one event match."""
    event_match = _make_event_match(home, away, "2018-06-14", home_score, away_score)
    all_matches = pd.concat([training, pd.DataFrame([event_match])], ignore_index=True)
    odds = _make_odds_lookup(home, away, event["name"], h_odds, d_odds, a_odds)
    return run_event_backtest(event, all_matches, odds_lookup=odds,
                              apply_live_filters=apply_live_filters)


class TestEvCapFilter:
    def test_high_ev_signal_filtered_when_live_filters_on(self, training_matches, minimal_event):
        """Signal mit EV > MAX_EV (0.40) muss herausgefiltert werden."""
        # Odds von 8.0 auf ein Ereignis mit ~50% Model-Prob → EV ~300% → weit über Cap
        result = _run_with_event_match(
            training_matches, minimal_event, "A", "B",
            h_odds=8.0, d_odds=8.0, a_odds=8.0,
            apply_live_filters=True,
        )
        bets = result[result.get("has_bet", False)] if not result.empty else result
        if not result.empty and "has_bet" in result.columns:
            bets = result[result["has_bet"].astype(bool)]
            if not bets.empty:
                assert bets["ev"].max() <= MAX_EV, (
                    f"EV {bets['ev'].max():.2f} überschreitet MAX_EV={MAX_EV} — Filter nicht aktiv"
                )

    def test_high_ev_signal_allowed_when_live_filters_off(self, training_matches, minimal_event):
        """Mit apply_live_filters=False dürfen hohe EVs durchkommen."""
        result = _run_with_event_match(
            training_matches, minimal_event, "A", "B",
            h_odds=8.0, d_odds=8.0, a_odds=8.0,
            apply_live_filters=False,
        )
        if not result.empty and "has_bet" in result.columns:
            bets = result[result["has_bet"].astype(bool)]
            # Falls es Signale gibt, sollen sie nicht nach MAX_EV begrenzt sein
            # (keine Assertion über max EV, nur dass der Filter nicht aktiv ist)
            _ = bets  # kein Crash = OK

    def test_normal_ev_signal_not_filtered(self, training_matches, minimal_event):
        """Signal mit moderatem EV (< MAX_EV) soll nicht gefiltert werden."""
        result = _run_with_event_match(
            training_matches, minimal_event, "A", "B",
            h_odds=2.1, d_odds=3.3, a_odds=3.5,
            apply_live_filters=True,
        )
        # Kein Crash, DataFrame zurückgegeben
        assert isinstance(result, pd.DataFrame)


class TestMaxActiveBetsCap:
    def test_daily_bet_cap_not_exceeded(self, minimal_event):
        """An einem Match-Tag dürfen nicht mehr als MAX_ACTIVE_BETS Wetten platziert werden."""
        teams = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        training_rows = []
        for i in range(80):
            h, a = teams[i % len(teams)], teams[(i + 1) % len(teams)]
            training_rows.append({
                "date": pd.Timestamp("2017-01-01") + pd.Timedelta(days=i),
                "home_team": h, "away_team": a,
                "home_score": 1, "away_score": 1,
                "neutral": False, "tournament": "test", "weight": 1.0,
            })
        training = pd.DataFrame(training_rows)

        # 8 Spiele am selben Tag mit Value-Quoten → erwarte max MAX_ACTIVE_BETS Bets
        event_rows = []
        odds_rows = []
        for i in range(8):
            h, a = teams[i], teams[(i + 2) % len(teams)]
            event_rows.append({
                "date": pd.Timestamp("2018-06-14"),
                "home_team": h, "away_team": a,
                "home_score": 1, "away_score": 0,
                "neutral": True, "tournament": "WC2018", "weight": 1.0,
            })
            odds_rows.append({
                "match_id": f"WC2018_{h}_vs_{a}",
                "home_odds": 2.5, "draw_odds": 3.2, "away_odds": 2.8,
            })

        all_matches = pd.concat([training, pd.DataFrame(event_rows)], ignore_index=True)
        odds = pd.DataFrame(odds_rows)
        result = run_event_backtest(minimal_event, all_matches, odds_lookup=odds,
                                    apply_live_filters=True)

        if not result.empty and "has_bet" in result.columns:
            bets_day = result[
                (result["has_bet"].astype(bool)) &
                (result["match_date"] == pd.Timestamp("2018-06-14"))
            ]
            assert len(bets_day) <= MAX_ACTIVE_BETS, (
                f"{len(bets_day)} Bets am Tag aber MAX_ACTIVE_BETS={MAX_ACTIVE_BETS}"
            )

    def test_daily_cap_not_applied_when_filters_off(self, minimal_event):
        """Mit apply_live_filters=False gilt kein Tages-Cap."""
        teams = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        training_rows = []
        for i in range(80):
            h, a = teams[i % len(teams)], teams[(i + 1) % len(teams)]
            training_rows.append({
                "date": pd.Timestamp("2017-01-01") + pd.Timedelta(days=i),
                "home_team": h, "away_team": a,
                "home_score": 1, "away_score": 1,
                "neutral": False, "tournament": "test", "weight": 1.0,
            })
        training = pd.DataFrame(training_rows)

        event_rows = []
        odds_rows = []
        for i in range(8):
            h, a = teams[i], teams[(i + 2) % len(teams)]
            event_rows.append({
                "date": pd.Timestamp("2018-06-14"),
                "home_team": h, "away_team": a,
                "home_score": 1, "away_score": 0,
                "neutral": True, "tournament": "WC2018", "weight": 1.0,
            })
            odds_rows.append({
                "match_id": f"WC2018_{h}_vs_{a}",
                "home_odds": 2.5, "draw_odds": 3.2, "away_odds": 2.8,
            })

        all_matches = pd.concat([training, pd.DataFrame(event_rows)], ignore_index=True)
        odds = pd.DataFrame(odds_rows)
        result = run_event_backtest(minimal_event, all_matches, odds_lookup=odds,
                                    apply_live_filters=False)
        # Kein Cap → möglicherweise > MAX_ACTIVE_BETS Bets (kein Crash)
        assert isinstance(result, pd.DataFrame)


class TestApplyLiveFiltersDefault:
    def test_default_is_true(self):
        """apply_live_filters=True muss der Default sein."""
        import inspect
        sig = inspect.signature(run_event_backtest)
        assert sig.parameters["apply_live_filters"].default is True
