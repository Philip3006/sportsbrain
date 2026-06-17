import csv
import json
from datetime import datetime, timedelta, timezone

import scripts.live_score_push as live_score_push


def _write_ledger(path):
    fieldnames = [
        "match_id", "match_date", "home", "away", "market", "decimal_odds",
        "stake_pct", "stake_amount", "placed_date", "status", "pnl",
        "closing_odds", "clv", "pinnacle_ref_odds", "source", "model_prob",
    ]
    row = {
        "match_id": "BRA_vs_ARG",
        "match_date": "2026-06-20",
        "home": "Brazil",
        "away": "Argentina",
        "market": "scorer_Joao Felix Sequeira",
        "decimal_odds": "3.40",
        "stake_pct": "0.05",
        "stake_amount": "8.00",
        "placed_date": "2026-06-18",
        "status": "open",
        "pnl": "0.00",
        "closing_odds": "0.0",
        "clv": "",
        "pinnacle_ref_odds": "",
        "source": "test",
        "model_prob": "0.25",
    }
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def test_live_score_push_persists_scorer_names_and_refreshes_dashboard(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    signals = tmp_path / "signals.json"
    cache = tmp_path / "live_scores.json"
    _write_ledger(ledger)

    kickoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    signals.write_text(json.dumps({
        "schedule": [{"home": "Brazil", "away": "Argentina", "kickoff": kickoff}],
    }))

    monkeypatch.setattr(live_score_push, "LEDGER", ledger)
    monkeypatch.setattr(live_score_push, "SIGNALS", signals)
    monkeypatch.setattr(live_score_push, "CACHE_PATH", cache)
    monkeypatch.setattr("src.data.odds_api.fetch_wm_live_scores", lambda **kwargs: [{
        "match_id": "espn_123",
        "home": "Brazil",
        "away": "Argentina",
        "home_score": 1,
        "away_score": 0,
        "completed": False,
        "commence_time": kickoff,
    }])
    monkeypatch.setattr("src.data.odds_api.fetch_espn_goal_scorers", lambda event_id: ["Joao Felix Sequeira"])
    monkeypatch.setattr("src.notifications.web_push._send_notification", lambda **kwargs: False)
    monkeypatch.setattr("src.notifications.flags.flag", lambda team: "")

    refresh_calls = []
    monkeypatch.setattr("src.notifications.web_dashboard.write_signals_json", lambda: refresh_calls.append(True))

    assert live_score_push.main() == 0

    saved = json.loads(cache.read_text())
    assert saved["espn_123"]["scorer_names"] == ["Joao Felix Sequeira"]
    assert saved["espn_123"]["last_goal_scorer"] == "Joao Felix Sequeira"
    assert refresh_calls == [True]
