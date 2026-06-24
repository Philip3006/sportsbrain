"""Tests for archive_signals() and backfill_signal_outcomes."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.value_detector import BetSignal
from src.scanner.output import archive_signals, SIGNAL_HISTORY


def _make_signal(match_id: str = "m1", market: str = "home") -> BetSignal:
    return BetSignal(
        match_id=match_id,
        home="Germany",
        away="Spain",
        market=market,
        model_prob=0.45,
        fair_prob=0.38,
        decimal_odds=2.30,
        ev=0.035,
        kelly_f=0.012,
        stake_pct=0.05,
        confidence="MEDIUM",
        stake_eur=5.0,
        n_models_agree=2,
    )


def test_archive_writes_new_signals(tmp_path):
    history = tmp_path / "signal_history.jsonl"
    with patch("src.scanner.output.SIGNAL_HISTORY", history):
        signals = [_make_signal("m1", "home"), _make_signal("m1", "draw")]
        selected = {("m1", "home")}
        n = archive_signals(signals, selected, "2026-06-24", sport="football")
    assert n == 2
    rows = [json.loads(l) for l in history.read_text().splitlines()]
    assert len(rows) == 2
    placed = [r for r in rows if r["placed"]]
    assert len(placed) == 1
    assert placed[0]["market"] == "home"


def test_archive_deduplicates(tmp_path):
    history = tmp_path / "signal_history.jsonl"
    with patch("src.scanner.output.SIGNAL_HISTORY", history):
        signals = [_make_signal("m1", "home")]
        selected: set = set()
        n1 = archive_signals(signals, selected, "2026-06-24", sport="football")
        n2 = archive_signals(signals, selected, "2026-06-24", sport="football")
    assert n1 == 1
    assert n2 == 0
    rows = [json.loads(l) for l in history.read_text().splitlines()]
    assert len(rows) == 1


def test_archive_different_scan_dates_not_deduped(tmp_path):
    history = tmp_path / "signal_history.jsonl"
    with patch("src.scanner.output.SIGNAL_HISTORY", history):
        signals = [_make_signal("m1", "home")]
        selected: set = set()
        n1 = archive_signals(signals, selected, "2026-06-24", sport="football")
        n2 = archive_signals(signals, selected, "2026-06-25", sport="football")
    assert n1 == 1
    assert n2 == 1


def test_archive_fields(tmp_path):
    history = tmp_path / "signal_history.jsonl"
    with patch("src.scanner.output.SIGNAL_HISTORY", history):
        signals = [_make_signal("match_abc", "o/u2.5_over")]
        archive_signals(signals, {("match_abc", "o/u2.5_over")}, "2026-06-24T10:00:00Z", sport="tennis")
    row = json.loads(history.read_text().splitlines()[0])
    assert row["match_id"] == "match_abc"
    assert row["market"] == "o/u2.5_over"
    assert row["scan_date"] == "2026-06-24"
    assert row["sport"] == "tennis"
    assert row["placed"] is True
    assert row["outcome"] is None
    assert row["ev_pct"] == pytest.approx(3.5, abs=0.01)


def test_backfill_resolves_outcomes(tmp_path):
    history = tmp_path / "signal_history.jsonl"
    perf_path = tmp_path / "signal_performance.json"

    # Pre-populate with a past signal without outcome
    row = {
        "scan_ts": "2026-06-20T10:00:00Z",
        "scan_date": "2026-06-20",
        "sport": "football",
        "match_id": "game1",
        "home": "Germany",
        "away": "Spain",
        "market": "home",
        "model_prob": 0.45,
        "fair_prob": 0.38,
        "decimal_odds": 2.30,
        "ev_pct": 3.5,
        "confidence": "MEDIUM",
        "n_models_agree": 2,
        "placed": True,
        "outcome": None,
        "outcome_ts": None,
    }
    history.write_text(json.dumps(row) + "\n")

    mock_scores = {
        "game1": {"home": "Germany", "away": "Spain", "home_score": 2, "away_score": 1},
        "Germany vs Spain": {"home": "Germany", "away": "Spain", "home_score": 2, "away_score": 1},
    }

    from scripts.backfill_signal_outcomes import backfill
    with (
        patch("src.scanner.output.SIGNAL_HISTORY", history),
        patch("scripts.backfill_signal_outcomes.SIGNAL_HISTORY", history),
        patch("scripts.backfill_signal_outcomes.SIGNAL_PERF", perf_path),
        patch("scripts.backfill_signal_outcomes.fetch_scores", return_value=mock_scores),
    ):
        perf = backfill(dry_run=False)

    updated = json.loads(history.read_text().splitlines()[0])
    assert updated["outcome"] == "won"
    assert updated["outcome_ts"] is not None
    assert perf["by_market"]["home"]["n_outcome"] == 1
    assert perf["by_market"]["home"]["accuracy"] == 1.0
