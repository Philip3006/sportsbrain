"""I8: Market-Performance-Feedback-Loop — persist ROI after settle, load in scanner."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["match_id", "match_date", "home", "away", "market",
                  "decimal_odds", "stake_pct", "stake_amount", "placed_date",
                  "status", "pnl", "closing_odds", "clv", "pinnacle_ref_odds", "source"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            row = {k: "" for k in fieldnames}
            row.update(r)
            w.writerow(row)


# ---------------------------------------------------------------------------
# write_market_performance
# ---------------------------------------------------------------------------

def test_write_market_performance_creates_file(tmp_path, monkeypatch):
    """Settle writes market_performance.json with ROI per market."""
    import scripts.settle_bets as sb
    from src import config as cfg
    monkeypatch.setattr(cfg, "DATA_CACHE", tmp_path)

    ledger = tmp_path / "ledger_philip.csv"
    _write_ledger(ledger, [
        {"market": "o/u2.5_under", "status": "lost", "stake_amount": "10", "pnl": "-10"},
        {"market": "o/u2.5_under", "status": "lost", "stake_amount": "10", "pnl": "-10"},
        {"market": "draw",         "status": "won",  "stake_amount": "10", "pnl": "15"},
    ])
    monkeypatch.setattr(sb, "ledger_path_for", lambda u: ledger)

    sb.write_market_performance(["philip"])

    perf = json.loads((tmp_path / "market_performance.json").read_text())
    assert "markets" in perf
    assert "o/u2.5_under" in perf["markets"]
    under = perf["markets"]["o/u2.5_under"]
    assert under["n_lost"] == 2
    assert under["roi"] == pytest.approx(-1.0, abs=0.01)


def test_penalized_flag_requires_min_bets(tmp_path, monkeypatch):
    """Markets with n < MARKET_PERF_MIN_BETS are NOT penalized even if ROI is bad."""
    import scripts.settle_bets as sb
    from src import config as cfg
    monkeypatch.setattr(cfg, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(cfg, "MARKET_PERF_MIN_BETS", 10)

    ledger = tmp_path / "ledger_philip.csv"
    # Only 5 bets — below threshold of 10
    _write_ledger(ledger, [
        {"market": "o/u3.0_under", "status": "lost", "stake_amount": "10", "pnl": "-10"},
    ] * 5)
    monkeypatch.setattr(sb, "ledger_path_for", lambda u: ledger)

    sb.write_market_performance(["philip"])

    perf = json.loads((tmp_path / "market_performance.json").read_text())
    assert not perf["markets"]["o/u3.0_under"]["penalized"]


def test_penalized_flag_set_when_threshold_met(tmp_path, monkeypatch):
    """Market is penalized when n >= MIN_BETS and ROI < threshold."""
    import scripts.settle_bets as sb
    from src import config as cfg
    monkeypatch.setattr(cfg, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(cfg, "MARKET_PERF_MIN_BETS", 3)
    monkeypatch.setattr(cfg, "MARKET_PERF_ROI_PENALTY_THRESHOLD", -0.20)

    ledger = tmp_path / "ledger_philip.csv"
    # 3 bets all lost → -100% ROI
    _write_ledger(ledger, [
        {"market": "btts_yes", "status": "lost", "stake_amount": "10", "pnl": "-10"},
    ] * 3)
    monkeypatch.setattr(sb, "ledger_path_for", lambda u: ledger)

    sb.write_market_performance(["philip"])

    perf = json.loads((tmp_path / "market_performance.json").read_text())
    assert perf["markets"]["btts_yes"]["penalized"]


# ---------------------------------------------------------------------------
# _blocked_markets (reads from file first)
# ---------------------------------------------------------------------------

def test_blocked_markets_reads_from_cache_file(tmp_path, monkeypatch):
    """_blocked_markets() prefers market_performance.json over live ledger."""
    from src import config as cfg
    monkeypatch.setattr(cfg, "DATA_CACHE", tmp_path)

    perf = {
        "updated_at": "2026-08-03T00:00:00Z",
        "markets": {
            "o/u2.5_under": {"roi": -0.47, "penalized": True, "n_total": 10},
            "draw":          {"roi":  0.96, "penalized": False, "n_total": 7},
        },
    }
    (tmp_path / "market_performance.json").write_text(json.dumps(perf))

    from src.scanner.scoring import _blocked_markets
    blocked = _blocked_markets()
    assert "o/u2.5_under" in blocked
    assert "draw" not in blocked


def test_blocked_markets_falls_back_when_no_file(tmp_path, monkeypatch):
    """_blocked_markets() falls back to live ledger when cache file absent."""
    from src import config as cfg
    monkeypatch.setattr(cfg, "DATA_CACHE", tmp_path)  # empty dir → no file

    # Mock ledger_summary to return something controlled
    import src.betting.ledger as ledger_mod
    monkeypatch.setattr(
        ledger_mod, "ledger_summary",
        lambda **kw: {"by_market": {
            "home": {"won": 3, "lost": 5, "roi_pct": -35.0},
        }},
    )

    from src.scanner.scoring import _blocked_markets
    blocked = _blocked_markets()
    assert "home" in blocked
