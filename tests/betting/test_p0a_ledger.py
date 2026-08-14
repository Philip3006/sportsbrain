"""P0-A ledger tests: tennis league integrity + SQLite round-trip."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

# ── Blocker 7: Tennis rows must never default to wm2026 ─────────────────────

def _make_df(rows: list[dict]) -> pd.DataFrame:
    from src.betting.ledger import _load
    # Write a temp CSV and load it via _load() to trigger migration logic
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
        df = pd.DataFrame(rows)
        df.to_csv(f.name, index=False)
        fpath = Path(f.name)
    try:
        return _load(fpath)
    finally:
        fpath.unlink(missing_ok=True)


def test_tennis_blank_league_never_becomes_wm2026():
    """P0-A regression: sport=tennis, league='' must NOT become wm2026 after _load()."""
    rows = [{
        "match_id": "abc123",
        "match_date": "2026-08-13",
        "home": "Djokovic",
        "away": "Alcaraz",
        "market": "home",
        "decimal_odds": "2.10",
        "stake_pct": "0.05",
        "stake_amount": "5.00",
        "placed_date": "2026-08-13",
        "status": "open",
        "pnl": "0.0",
        "closing_odds": "0.0",
        "clv": "",
        "pinnacle_ref_odds": "",
        "source": "value",
        "model_prob": "",
        "stake_reason": "",
        "league": "",        # blank
        "signal_id": "sig_001",
        "fixture_key": "djokovic_vs_alcaraz",
        "sport": "tennis",   # sport=tennis
        "bankroll_at_placement": "100.00",
        "cap_applied": "false",
    }]
    df = _make_df(rows)
    league_val = df.iloc[0]["league"]
    assert league_val != "wm2026", (
        f"Tennis row with blank league must NOT become 'wm2026' after _load(), got: {league_val!r}"
    )


def test_football_blank_league_still_becomes_wm2026():
    """Backward-compat: pre-P0-A football rows with blank league keep wm2026 default."""
    rows = [{
        "match_id": "foot001",
        "match_date": "2026-06-15",
        "home": "Germany",
        "away": "Scotland",
        "market": "home",
        "decimal_odds": "1.80",
        "stake_pct": "0.05",
        "stake_amount": "5.00",
        "placed_date": "2026-06-15",
        "status": "open",
        "pnl": "0.0",
        "closing_odds": "0.0",
        "clv": "",
        "pinnacle_ref_odds": "",
        "source": "value",
        "model_prob": "",
        "stake_reason": "",
        "league": "",         # blank — should get wm2026 for football
        "signal_id": "",
        "fixture_key": "",
        "sport": "football",  # football → wm2026 is appropriate
        "bankroll_at_placement": "",
        "cap_applied": "",
    }]
    df = _make_df(rows)
    assert df.iloc[0]["league"] == "wm2026", "Blank-league football rows should default to wm2026"


def test_legacy_row_no_sport_column_gets_wm2026():
    """Pre-P0-A rows without sport column at all → wm2026 (historically all WM football)."""
    rows = [{
        "match_id": "old001",
        "match_date": "2026-06-12",
        "home": "France",
        "away": "Uruguay",
        "market": "home",
        "decimal_odds": "1.70",
        "stake_pct": "0.05",
        "stake_amount": "5.00",
        "placed_date": "2026-06-12",
        "status": "won",
        "pnl": "3.50",
        "closing_odds": "1.65",
        "clv": "0.030",
        "pinnacle_ref_odds": "",
        "source": "value",
        "model_prob": "",
        "stake_reason": "",
        # Intentionally NO league, signal_id, fixture_key, sport, bankroll_at_placement, cap_applied
    }]
    df = _make_df(rows)
    assert df.iloc[0]["league"] == "wm2026"


def test_tennis_with_explicit_league_preserved():
    """Explicit league=atp is preserved unchanged — never overwritten."""
    rows = [{
        "match_id": "ten001",
        "match_date": "2026-08-13",
        "home": "Sinner",
        "away": "Zverev",
        "market": "home",
        "decimal_odds": "1.90",
        "stake_pct": "0.05",
        "stake_amount": "5.00",
        "placed_date": "2026-08-13",
        "status": "open",
        "pnl": "0.0",
        "closing_odds": "0.0",
        "clv": "",
        "pinnacle_ref_odds": "",
        "source": "value",
        "model_prob": "",
        "stake_reason": "",
        "league": "atp",
        "signal_id": "sig_sinner_001",
        "fixture_key": "sinner_vs_zverev",
        "sport": "tennis",
        "bankroll_at_placement": "100.00",
        "cap_applied": "false",
    }]
    df = _make_df(rows)
    assert df.iloc[0]["league"] == "atp"


# ── Blocker 8: SQLite round-trip preserves P0-A identity columns ─────────────

def test_sqlite_preserves_p0a_identity():
    """P0-A: CSV→SQLite round-trip preserves signal_id, fixture_key, sport, bankroll."""
    from src.betting.db import open_db, upsert_bet

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = open_db(db_path)

        row = {
            "match_id": "sqlite_test_001",
            "match_date": "2026-08-13",
            "home": "Djokovic",
            "away": "Alcaraz",
            "market": "home",
            "decimal_odds": 2.10,
            "stake_pct": 0.05,
            "stake_amount": 5.0,
            "placed_date": "2026-08-13",
            "status": "open",
            "pnl": 0.0,
            "closing_odds": None,
            "clv": None,
            "pinnacle_ref_odds": None,
            "source": "value",
            "model_prob": None,
            "stake_reason": "",
            "league": "atp",
            "signal_id": "sig_djokovic_alcaraz_home",
            "fixture_key": "djokovic_vs_alcaraz_20260813",
            "sport": "tennis",
            "bankroll_at_placement": "100.00",
            "cap_applied": "false",
        }
        upsert_bet(conn, row)
        conn.commit()

        result = conn.execute(
            "SELECT signal_id, fixture_key, sport, bankroll_at_placement, cap_applied, league "
            "FROM bets WHERE match_id = 'sqlite_test_001'"
        ).fetchone()
        conn.close()

        assert result is not None, "Row not found in SQLite"
        assert result["signal_id"] == "sig_djokovic_alcaraz_home"
        assert result["fixture_key"] == "djokovic_vs_alcaraz_20260813"
        assert result["sport"] == "tennis"
        assert result["bankroll_at_placement"] == "100.00"
        assert result["cap_applied"] == "false"
        assert result["league"] == "atp"


def test_sqlite_schema_migration_adds_p0a_columns():
    """P0-A: open_db() migration adds new columns to an existing pre-P0-A schema."""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "old.db"

        # Simulate a pre-P0-A database without the new columns
        old_conn = sqlite3.connect(str(db_path))
        old_conn.execute("""
            CREATE TABLE bets (
                match_id TEXT NOT NULL,
                match_date TEXT, home TEXT, away TEXT, market TEXT,
                decimal_odds REAL, stake_pct REAL, stake_amount REAL,
                placed_date TEXT, status TEXT DEFAULT 'open',
                pnl REAL DEFAULT 0.0, closing_odds REAL, clv REAL,
                pinnacle_ref_odds REAL, source TEXT DEFAULT 'value',
                model_prob REAL, stake_reason TEXT,
                PRIMARY KEY (match_id, market)
            )
        """)
        old_conn.commit()
        old_conn.close()

        # open_db() should add the missing P0-A columns
        from src.betting.db import open_db
        conn = open_db(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bets)").fetchall()}
        conn.close()

        for col in ("signal_id", "fixture_key", "sport", "bankroll_at_placement", "cap_applied", "league"):
            assert col in cols, f"Column '{col}' not added by P0-A migration"


def test_sqlite_migration_idempotent():
    """P0-A: Running open_db() twice on the same database is safe."""
    from src.betting.db import open_db

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "idem.db"
        open_db(db_path).close()  # first open — creates schema
        open_db(db_path).close()  # second open — migration is idempotent
