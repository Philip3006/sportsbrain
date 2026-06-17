import pandas as pd

from scripts.confirm_bets import _make_signal
from src.betting.ledger import append_bets
from src.betting.value_detector import BetSignal


def _dashboard_signal() -> dict:
    return {
        "match": "Brazil vs Argentina",
        "market": "home",
        "odds": 2.1,
        "model_prob": 55.0,
        "fair_prob": 48.0,
        "ev_pct": 15.5,
        "stake_eur": 7.5,
        "stake_pct": 7.5,
        "confidence": "MEDIUM",
        "kickoff": "2026-06-20T19:00:00Z",
        "n_models_agree": 2,
    }


def test_make_signal_returns_complete_bet_signal():
    sig = _make_signal(_dashboard_signal(), bankroll=100.0)

    assert isinstance(sig, BetSignal)
    assert sig.home == "Brazil"
    assert sig.away == "Argentina"
    assert sig.market == "home"
    assert sig.decimal_odds == 2.1
    assert sig.model_prob == 0.55
    assert sig.fair_prob == 0.48
    assert sig.ev == 0.155
    assert sig.stake_eur == 7.5
    assert sig.stake_pct == 0.075
    assert sig.kelly_f == 0.075
    assert sig.n_models_agree == 2


def test_make_signal_can_be_appended_to_ledger(tmp_path):
    sig = _make_signal(_dashboard_signal(), bankroll=100.0)
    ledger = tmp_path / "ledger.csv"

    n = append_bets([sig], bankroll=100.0, path=ledger, match_date="2026-06-20")

    assert n == 1
    df = pd.read_csv(ledger)
    assert df.loc[0, "home"] == "Brazil"
    assert df.loc[0, "away"] == "Argentina"
    assert df.loc[0, "stake_amount"] == 7.5
    assert df.loc[0, "stake_pct"] == 0.075


def test_make_signal_derives_stake_pct_when_missing():
    data = _dashboard_signal()
    data.pop("stake_pct")

    sig = _make_signal(data, bankroll=150.0)

    assert sig.stake_pct == 0.05
    assert sig.kelly_f == 0.05
