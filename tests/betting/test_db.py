"""Tests für src/betting/db.py (SQLite Ledger)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.betting.db import (
    ledger_db,
    open_bets,
    open_db,
    settle_bet,
    summary,
    upsert_bet,
    migrate_csv_to_sqlite,
)


def _sample_bet(**overrides) -> dict:
    base = {
        "match_id": "test_A_vs_B_20260801",
        "match_date": "2026-08-01",
        "home": "Player A",
        "away": "Player B",
        "market": "home",
        "decimal_odds": 2.10,
        "stake_pct": 0.05,
        "stake_amount": 5.0,
        "placed_date": "2026-08-01",
        "status": "open",
        "pnl": 0.0,
        "closing_odds": None,
        "clv": None,
        "pinnacle_ref_odds": None,
        "source": "value",
        "model_prob": 0.52,
        "stake_reason": "",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_ledger.db"


def test_open_db_creates_tables(db_path):
    conn = open_db(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    assert "bets" in tables


def test_wal_mode(db_path):
    conn = open_db(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_upsert_and_read(db_path):
    with ledger_db(db_path) as conn:
        upsert_bet(conn, _sample_bet())
    conn2 = open_db(db_path)
    rows = open_bets(conn2)
    conn2.close()
    assert len(rows) == 1
    assert rows[0]["market"] == "home"


def test_insert_or_ignore_idempotent(db_path):
    with ledger_db(db_path) as conn:
        upsert_bet(conn, _sample_bet())
        upsert_bet(conn, _sample_bet())  # second insert ignored
    conn2 = open_db(db_path)
    assert len(open_bets(conn2)) == 1
    conn2.close()


def test_settle_bet(db_path):
    with ledger_db(db_path) as conn:
        upsert_bet(conn, _sample_bet())
        settle_bet(conn, "test_A_vs_B_20260801", "home", "won", 5.25)
    conn2 = open_db(db_path)
    assert len(open_bets(conn2)) == 0
    s = summary(conn2)
    conn2.close()
    assert s["n_won"] == 1
    assert abs(s["total_pnl"] - 5.25) < 0.01


def test_summary_empty(db_path):
    conn = open_db(db_path)
    s = summary(conn)
    conn.close()
    assert s["n_bets"] == 0
    assert s["total_pnl"] == pytest.approx(0.0)


def test_summary_multiple_markets(db_path):
    with ledger_db(db_path) as conn:
        upsert_bet(conn, _sample_bet(match_id="m1", market="home"))
        upsert_bet(conn, _sample_bet(match_id="m1", market="away"))
        settle_bet(conn, "m1", "home", "won", 5.0)
        settle_bet(conn, "m1", "away", "lost", -5.0)
    conn2 = open_db(db_path)
    s = summary(conn2)
    conn2.close()
    assert s["n_bets"] == 2
    assert s["total_pnl"] == pytest.approx(0.0)


def test_migrate_csv_to_sqlite(db_path, tmp_path):
    csv_path = tmp_path / "test.csv"
    csv_path.write_text(
        "match_id,match_date,home,away,market,decimal_odds,stake_pct,"
        "stake_amount,placed_date,status,pnl,closing_odds,clv,"
        "pinnacle_ref_odds,source,model_prob,stake_reason\n"
        "m1,2026-08-01,A,B,home,2.1,0.05,5.0,2026-08-01,won,5.5,,,,value,0.52,\n"
        "m2,2026-08-02,C,D,away,1.9,0.03,3.0,2026-08-02,open,0.0,,,,value,0.48,\n"
    )
    n = migrate_csv_to_sqlite(csv_path, db_path)
    assert n == 2
    conn = open_db(db_path)
    s = summary(conn)
    ob = open_bets(conn)
    conn.close()
    assert s["n_bets"] == 2
    assert len(ob) == 1


def test_migrate_missing_csv(db_path, tmp_path):
    n = migrate_csv_to_sqlite(tmp_path / "nonexistent.csv", db_path)
    assert n == 0
