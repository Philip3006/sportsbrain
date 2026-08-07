"""Tests für BL2-Settlement: League-Filter, CLV-Backfill, void-Handling."""
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.betting.ledger import _backfill_clv_bl2, settle_from_results, _FIELDS as LEDGER_COLS


def _make_ledger(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in LEDGER_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[LEDGER_COLS]


def test_clv_backfill_fills_bl2_rows(tmp_path: Path) -> None:
    """CLV wird für BL2-Zeilen gefüllt wenn Closing-Odds-Cache vorhanden."""
    closing = {"Kaiserslautern_vs_Karlsruhe_20260809": {"closing_home": "2.00", "closing_draw": "3.50", "closing_away": "4.00"}}
    cache_file = tmp_path / "bundesliga2_closing_odds.json"
    cache_file.write_text(json.dumps(closing))

    df = _make_ledger([{
        "match_id": "bl2_mock_0",
        "home": "Kaiserslautern",
        "away": "Karlsruhe",
        "match_date": "2026-08-09",
        "market": "home",
        "decimal_odds": "2.20",
        "closing_odds": "",
        "clv": "",
        "league": "bl2",
        "status": "won",
        "pnl": "1.20",
    }])

    import src.config as cfg
    original = cfg.DATA_CACHE
    cfg.DATA_CACHE = tmp_path
    try:
        result = _backfill_clv_bl2(df)
    finally:
        cfg.DATA_CACHE = original

    assert result.loc[0, "closing_odds"] == "2.000"
    clv = float(result.loc[0, "clv"])
    assert abs(clv - (2.20 / 2.00 - 1.0)) < 0.001


def test_clv_backfill_skips_non_bl2(tmp_path: Path) -> None:
    """CLV-Backfill ignoriert Nicht-BL2-Zeilen."""
    closing = {"Germany_vs_Spain_20260720": {"closing_home": "1.80"}}
    cache_file = tmp_path / "bundesliga2_closing_odds.json"
    cache_file.write_text(json.dumps(closing))

    df = _make_ledger([{
        "match_id": "wm_match_0",
        "home": "Germany",
        "away": "Spain",
        "match_date": "2026-07-20",
        "market": "home",
        "decimal_odds": "2.00",
        "closing_odds": "",
        "clv": "",
        "league": "wm2026",
        "status": "won",
        "pnl": "1.00",
    }])

    import src.config as cfg
    original = cfg.DATA_CACHE
    cfg.DATA_CACHE = tmp_path
    try:
        result = _backfill_clv_bl2(df)
    finally:
        cfg.DATA_CACHE = original

    assert result.loc[0, "closing_odds"] == ""


def test_clv_backfill_no_crash_without_cache(tmp_path: Path) -> None:
    """Kein Crash wenn Closing-Odds-Cache fehlt."""
    df = _make_ledger([{
        "match_id": "bl2_x", "home": "Bochum", "away": "Hertha",
        "match_date": "2026-08-09", "market": "home",
        "decimal_odds": "2.00", "closing_odds": "", "clv": "",
        "league": "bl2", "status": "won", "pnl": "1.00",
    }])
    import src.config as cfg
    original = cfg.DATA_CACHE
    cfg.DATA_CACHE = tmp_path  # kein closing_odds.json vorhanden
    try:
        result = _backfill_clv_bl2(df)
    finally:
        cfg.DATA_CACHE = original

    assert result.loc[0, "clv"] == ""


def test_clv_already_set_not_overwritten(tmp_path: Path) -> None:
    """Bereits gesetzte CLV-Werte werden nicht überschrieben."""
    closing = {"Hannover_vs_Wolfsburg_20260809": {"closing_home": "1.50"}}
    (tmp_path / "bundesliga2_closing_odds.json").write_text(json.dumps(closing))

    df = _make_ledger([{
        "match_id": "bl2_mock_0", "home": "Hannover", "away": "Wolfsburg",
        "match_date": "2026-08-09", "market": "home",
        "decimal_odds": "2.00", "closing_odds": "1.90", "clv": "0.0526",
        "league": "bl2", "status": "won", "pnl": "1.00",
    }])

    import src.config as cfg
    original = cfg.DATA_CACHE
    cfg.DATA_CACHE = tmp_path
    try:
        result = _backfill_clv_bl2(df)
    finally:
        cfg.DATA_CACHE = original

    # Nicht überschrieben — original closing_odds bleibt
    assert result.loc[0, "closing_odds"] == "1.90"
