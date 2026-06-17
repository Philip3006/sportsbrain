import csv
import json
from datetime import datetime, timedelta, timezone

from src.betting.value_detector import BetSignal
from src.notifications import web_dashboard


def _write_ledger(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def _open_row(**overrides):
    row = {
        "match_id": "BRA_vs_ARG",
        "match_date": "2026-06-20",
        "home": "Brazil",
        "away": "Argentina",
        "market": "home",
        "decimal_odds": "2.00",
        "stake_pct": "0.05",
        "stake_amount": "5.00",
        "placed_date": "2026-06-18",
        "status": "open",
        "pnl": "0.00",
        "closing_odds": "0.0",
        "clv": "",
        "pinnacle_ref_odds": "",
        "source": "test",
        "model_prob": "0.55",
    }
    row.update(overrides)
    return row


def test_open_bets_marks_shorter_current_odds_as_good_clv_signal(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row()])
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)

    rows = web_dashboard._get_open_bets_from_ledger({
        "Brazil vs Argentina": {"home": 1.90},
    })

    assert rows[0]["drift_pct"] == -5.0
    assert rows[0]["clv_signal"] == "good"


def test_drop_goals_range_signals_removes_suspended_markets():
    rows = [
        {"match": "England vs Croatia", "market": "goals_2_4_no"},
        {"match": "England vs Croatia", "market": "h1_goals_2_4_no"},
        {"match": "England vs Croatia", "market": "h2_goals_2_4"},
        {"match": "England vs Croatia", "market": "o/u2.5_under"},
    ]

    filtered = web_dashboard._drop_goals_range_signals(rows)

    assert filtered == [{"match": "England vs Croatia", "market": "o/u2.5_under"}]


def test_open_bets_maps_away_ah_market_to_normalized_all_odds_key(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(market="ah+1.5_away", decimal_odds="1.95")])
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)

    rows = web_dashboard._get_open_bets_from_ledger({
        "Brazil vs Argentina": {"ah-1.5_away": 1.83},
    })

    assert rows[0]["current_odds"] == 1.83


def test_bets_view_groups_future_open_bet_into_open_tab(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row()])
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_LIVE_SCORES_PATH", tmp_path / "live_scores.json")

    future_ko = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bets = web_dashboard._build_bets_view(
        [{"home": "Brazil", "away": "Argentina", "kickoff": future_ko}],
        {"Brazil vs Argentina": {"home": 1.90}},
        [],
    )

    assert len(bets["open"]) == 1
    assert bets["summary"] == {"open": 1, "live": 0, "settled": 0}


def test_bets_view_moves_decided_under_to_settled_live(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(market="o/u2.5_under", decimal_odds="1.90", stake_amount="10.00")])
    live_scores = tmp_path / "live_scores.json"
    live_scores.write_text(
        '{"BRA_vs_ARG":{"home":"Brazil","away":"Argentina","home_score":2,"away_score":1,"completed":false}}'
    )
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_LIVE_SCORES_PATH", live_scores)

    live_ko = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bets = web_dashboard._build_bets_view(
        [{"home": "Brazil", "away": "Argentina", "kickoff": live_ko}],
        {"Brazil vs Argentina": {"under25": 3.10}},
        [],
    )

    assert len(bets["settled"]) == 1
    assert bets["settled"][0]["status"] == "lost"
    assert bets["settled"][0]["status_source"] == "live"
    assert bets["settled"][0]["pnl"] == -10.0


def test_bets_view_keeps_live_home_result_bet_in_live_tab(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(market="home")])
    live_scores = tmp_path / "live_scores.json"
    live_scores.write_text(
        '{"BRA_vs_ARG":{"home":"Brazil","away":"Argentina","home_score":1,"away_score":0,"completed":false}}'
    )
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_LIVE_SCORES_PATH", live_scores)

    live_ko = (datetime.now(timezone.utc) - timedelta(minutes=22)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bets = web_dashboard._build_bets_view(
        [{"home": "Brazil", "away": "Argentina", "kickoff": live_ko}],
        {"Brazil vs Argentina": {"home": 1.70}},
        [],
    )

    assert len(bets["live"]) == 1
    assert bets["live"][0]["scoreline"] == "1:0"
    assert bets["summary"] == {"open": 0, "live": 1, "settled": 0}


def test_bets_view_exposes_last_goal_scorer_for_live_tab(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(market="home")])
    live_scores = tmp_path / "live_scores.json"
    live_scores.write_text(
        '{"BRA_vs_ARG":{"home":"Brazil","away":"Argentina","home_score":1,"away_score":0,"completed":false,"last_goal_scorer":"Joao Felix Sequeira"}}'
    )
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_LIVE_SCORES_PATH", live_scores)

    live_ko = (datetime.now(timezone.utc) - timedelta(minutes=22)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bets = web_dashboard._build_bets_view(
        [{"home": "Brazil", "away": "Argentina", "kickoff": live_ko}],
        {"Brazil vs Argentina": {"home": 1.70}},
        [],
    )

    assert bets["live"][0]["last_goal_scorer"] == "Joao Felix Sequeira"


def test_bets_view_moves_scorer_bet_to_settled_when_scorer_name_matches(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(market="scorer_João Félix Sequeira", decimal_odds="3.40", stake_amount="8.00")])
    live_scores = tmp_path / "live_scores.json"
    live_scores.write_text(
        '{"espn_123":{"home":"Brazil","away":"Argentina","home_score":1,"away_score":0,"completed":false,"scorer_names":["Joao Felix Sequeira"]}}'
    )
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_LIVE_SCORES_PATH", live_scores)

    live_ko = (datetime.now(timezone.utc) - timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bets = web_dashboard._build_bets_view(
        [{"home": "Brazil", "away": "Argentina", "kickoff": live_ko}],
        {"Brazil vs Argentina": {"home": 1.75}},
        [],
    )

    assert len(bets["settled"]) == 1
    assert bets["settled"][0]["status"] == "won"
    assert bets["settled"][0]["status_source"] == "live"
    assert bets["settled"][0]["pnl"] == 19.2


def test_bets_view_keeps_completed_unresolved_scorer_bet_open(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(market="scorer_Julian Alvarez", decimal_odds="2.50", stake_amount="10.00")])
    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_LIVE_SCORES_PATH", tmp_path / "missing_live_scores.json")

    bets = web_dashboard._build_bets_view(
        [],
        {"Brazil vs Argentina": {"home": 1.75}},
        [{
            "home": "Brazil",
            "away": "Argentina",
            "home_score": 3,
            "away_score": 0,
            "commence_time": "2026-06-17T01:00:00Z",
        }],
    )

    assert len(bets["open"]) == 1
    assert bets["open"][0]["market"] == "scorer_Julian Alvarez"
    assert bets["open"][0]["badge_text"] == "Manuell prüfen"
    assert bets["summary"] == {"open": 1, "live": 0, "settled": 0}


def test_signal_to_dict_exposes_safety_gates():
    signal = BetSignal(
        match_id="BRA_ARG",
        home="Brazil",
        away="Argentina",
        market="home",
        model_prob=0.56,
        fair_prob=0.48,
        decimal_odds=2.05,
        ev=0.148,
        kelly_f=0.13,
        stake_pct=0.08,
        confidence="LOW",
        stake_eur=8.0,
        min_ev_pct=3.0,
    )

    row = web_dashboard._signal_to_dict(signal)

    assert row["safety_gates"]["positive_ev"] is True
    assert row["safety_gates"]["min_ev"] is True
    assert row["safety_gates"]["kelly"] is True
    assert row["safety_gates"]["low_confidence"] is True
    assert row["safety_gates"]["stake_capped"] is True


def test_build_system_status_reports_stale_scan_and_settlement_due(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, [_open_row(match_date="2026-06-16")])
    api_usage = tmp_path / "api_usage.json"
    api_usage.write_text(json.dumps({"requests_used": 9950, "requests_remaining": 50}))
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    for name in [
        "prematch_scan_cron.log",
        "launchd_auto_retrain.log",
        "closing_odds_cron.log",
        "consume_pending_bets.log",
        "launchd_live_score_push.log",
    ]:
        (results_dir / name).write_text("===== 2026-06-18 job start =====\n===== 2026-06-18 job done =====\n")
    models_dir = tmp_path / "models"
    (models_dir / "dixon_coles").mkdir(parents=True)
    (models_dir / "lgbm").mkdir(parents=True)
    (models_dir / "dixon_coles" / "params_20260618.pkl").write_bytes(b"model")
    (models_dir / "lgbm" / "gate.json").write_text(json.dumps({"passed": True, "holdout": "wc2022"}))
    (models_dir / "lgbm" / "anchor.json").write_text(json.dumps({"use_anchor": True}))

    monkeypatch.setattr(web_dashboard, "_LEDGER_PATH", ledger)
    monkeypatch.setattr(web_dashboard, "_API_USAGE_PATH", api_usage)
    monkeypatch.setattr(web_dashboard, "_RESULTS_DIR", results_dir)
    monkeypatch.setattr(web_dashboard, "_MODELS_DIR", models_dir)
    now = datetime(2026, 6, 18, 12, tzinfo=timezone.utc)

    status = web_dashboard._build_system_status(
        "2026-06-16T09:00:00Z",
        [{"home": "Brazil", "away": "Argentina", "kickoff": "2026-06-19T12:00:00Z"}],
        {"Brazil vs Argentina": {"home": 2.0}},
        now=now,
    )

    codes = {alert["code"] for alert in status["alerts"]}
    assert status["system_health"]["status"] == "warn"
    assert status["system_health"]["open_bets"] == 1
    assert status["data_freshness"]["signals_stale"] is True
    assert "STALE_SCAN" in codes
    assert "LOW_ODDS_QUOTA" in codes
    assert "SETTLEMENT_DUE" in codes


def test_scan_log_status_downgrades_historical_error_after_clean_done(tmp_path):
    log = tmp_path / "job.log"
    log.write_text(
        "--- [2026-06-18 10:00:00 CEST] job started ---\n"
        "push failed: fetch first\n"
        "--- [2026-06-18 10:01:00 CEST] job error: push failed ---\n"
        "--- [2026-06-18 10:05:00 CEST] job started ---\n"
        "--- [2026-06-18 10:05:10 CEST] job done ---\n"
    )

    status = web_dashboard._scan_log_status(log)

    assert status["status"] == "warn"
    assert status["message"] == "Fehler im Log, letzter Lauf wirkt abgeschlossen"
