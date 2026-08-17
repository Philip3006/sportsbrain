"""SQLite-backed Ledger (N-Rev5).

WAL-mode enables concurrent reads without blocking writes — unlike the CSV
path which requires an exclusive flock for every read-modify-write cycle.

This module provides:
  - LedgerDB: context manager wrapping a WAL-mode SQLite connection
  - migrate_csv_to_sqlite(): one-shot import of existing CSV ledger

The CSV path in ledger.py remains the write-path for now; LedgerDB is used
for concurrent reads (open_bets, summary) and as the migration target. A full
write-path migration is tracked in ROADMAP.md (N-Rev5).

Schema deliberately mirrors the _FIELDS list in ledger.py so migration is
lossless.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_log = logging.getLogger("sportsbrain.betting.db")

_DDL = """
CREATE TABLE IF NOT EXISTS bets (
    match_id        TEXT NOT NULL,
    match_date      TEXT,
    home            TEXT,
    away            TEXT,
    market          TEXT,
    decimal_odds    REAL,
    stake_pct       REAL,
    stake_amount    REAL,
    placed_date     TEXT,
    status          TEXT DEFAULT 'open',
    pnl             REAL DEFAULT 0.0,
    closing_odds    REAL,
    clv             REAL,
    pinnacle_ref_odds REAL,
    source          TEXT DEFAULT 'value',
    model_prob      REAL,
    stake_reason    TEXT,
    PRIMARY KEY (match_id, market)
);
CREATE INDEX IF NOT EXISTS idx_bets_status   ON bets (status);
CREATE INDEX IF NOT EXISTS idx_bets_placed   ON bets (placed_date);
"""

# P0-A: new identity columns added after initial schema — safe ALTER TABLE migration.
# Each runs only when the column is missing (PRAGMA table_info check via executescript).
_DDL_MIGRATE_P0A = [
    "ALTER TABLE bets ADD COLUMN league             TEXT DEFAULT ''",
    "ALTER TABLE bets ADD COLUMN signal_id          TEXT DEFAULT ''",
    "ALTER TABLE bets ADD COLUMN fixture_key        TEXT DEFAULT ''",
    "ALTER TABLE bets ADD COLUMN sport              TEXT DEFAULT ''",
    "ALTER TABLE bets ADD COLUMN bankroll_at_placement TEXT DEFAULT ''",
    "ALTER TABLE bets ADD COLUMN cap_applied        TEXT DEFAULT ''",
]


def open_db(path: Path) -> sqlite3.Connection:
    """Open (or create) a WAL-mode SQLite ledger at *path*.

    Applies the base DDL and safe P0-A identity column migrations
    (ALTER TABLE … ADD COLUMN, ignored if column already exists).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL)
    # P0-A: safe schema migration — add new identity columns only when missing.
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(bets)").fetchall()
    }
    for stmt in _DDL_MIGRATE_P0A:
        col = stmt.split("ADD COLUMN")[1].strip().split()[0]
        if col not in existing_cols:
            conn.execute(stmt)
    conn.commit()
    return conn


@contextmanager
def ledger_db(path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager: opens LedgerDB, commits on exit, rolls back on error."""
    conn = open_db(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def open_bets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all bets with status='open' as dicts."""
    rows = conn.execute(
        "SELECT * FROM bets WHERE status = 'open' ORDER BY placed_date"
    ).fetchall()
    return [dict(r) for r in rows]


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate stats: n_bets, n_open, n_won, n_lost, total_staked, total_pnl, roi_pct."""
    row = conn.execute("""
        SELECT
            COUNT(*) AS n_bets,
            SUM(CASE WHEN status='open'  THEN 1 ELSE 0 END) AS n_open,
            SUM(CASE WHEN status='won'   THEN 1 ELSE 0 END) AS n_won,
            SUM(CASE WHEN status='lost'  THEN 1 ELSE 0 END) AS n_lost,
            SUM(CASE WHEN status='void'  THEN 1 ELSE 0 END) AS n_void,
            SUM(stake_amount) AS total_staked,
            SUM(pnl)          AS total_pnl
        FROM bets
    """).fetchone()
    if row is None or row["n_bets"] == 0:
        return {"n_bets": 0, "n_open": 0, "n_won": 0, "n_lost": 0, "n_void": 0,
                "total_staked": 0.0, "total_pnl": 0.0, "roi_pct": 0.0}
    staked = row["total_staked"] or 0.0
    pnl    = row["total_pnl"]    or 0.0
    return {
        "n_bets":       row["n_bets"],
        "n_open":       row["n_open"],
        "n_won":        row["n_won"],
        "n_lost":       row["n_lost"],
        "n_void":       row["n_void"],
        "total_staked": staked,
        "total_pnl":    pnl,
        "roi_pct":      (pnl / staked * 100) if staked > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT OR IGNORE INTO bets (
    match_id, match_date, home, away, market,
    decimal_odds, stake_pct, stake_amount, placed_date, status, pnl,
    closing_odds, clv, pinnacle_ref_odds, source, model_prob, stake_reason,
    league, signal_id, fixture_key, sport, bankroll_at_placement, cap_applied
) VALUES (
    :match_id, :match_date, :home, :away, :market,
    :decimal_odds, :stake_pct, :stake_amount, :placed_date, :status, :pnl,
    :closing_odds, :clv, :pinnacle_ref_odds, :source, :model_prob, :stake_reason,
    :league, :signal_id, :fixture_key, :sport, :bankroll_at_placement, :cap_applied
)
"""

_UPDATE_SETTLE_SQL = """
UPDATE bets SET status=:status, pnl=:pnl WHERE match_id=:match_id AND market=:market
"""


def upsert_bet(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(_INSERT_SQL, {
        "match_id":        row.get("match_id", ""),
        "match_date":      row.get("match_date", ""),
        "home":            row.get("home", ""),
        "away":            row.get("away", ""),
        "market":          row.get("market", ""),
        "decimal_odds":    float(row.get("decimal_odds", 0)),
        "stake_pct":       float(row.get("stake_pct", 0)),
        "stake_amount":    float(row.get("stake_amount", 0)),
        "placed_date":     row.get("placed_date", ""),
        "status":          row.get("status", "open"),
        "pnl":             float(row.get("pnl", 0)),
        "closing_odds":    _float_or_none(row.get("closing_odds")),
        "clv":             _float_or_none(row.get("clv")),
        "pinnacle_ref_odds": _float_or_none(row.get("pinnacle_ref_odds")),
        "source":          row.get("source", "value") or "value",
        "model_prob":      _float_or_none(row.get("model_prob")),
        "stake_reason":    row.get("stake_reason", "") or "",
        # P0-A: canonical identity + risk provenance
        "league":                  row.get("league", "") or "",
        "signal_id":               row.get("signal_id", "") or "",
        "fixture_key":             row.get("fixture_key", "") or "",
        "sport":                   row.get("sport", "") or "",
        "bankroll_at_placement":   row.get("bankroll_at_placement", "") or "",
        "cap_applied":             row.get("cap_applied", "") or "",
    })


def settle_bet(conn: sqlite3.Connection, match_id: str, market: str, status: str, pnl: float) -> None:
    conn.execute(_UPDATE_SETTLE_SQL, {"status": status, "pnl": pnl,
                                      "match_id": match_id, "market": market})


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "" or (isinstance(v, float) and v != v):  # noqa: PLR0124
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CSV → SQLite migration
# ---------------------------------------------------------------------------

def migrate_csv_to_sqlite(csv_path: Path, db_path: Path) -> int:
    """Import all rows from *csv_path* into *db_path*. Returns rows imported.

    Idempotent: INSERT OR IGNORE means re-running is safe. Does not remove
    rows that were deleted from the CSV — intended as a one-shot bootstrap.
    """
    import pandas as pd

    if not csv_path.exists():
        _log.warning("migrate: %s not found — nothing to migrate", csv_path)
        return 0

    df = pd.read_csv(csv_path, dtype=str).fillna("")
    with ledger_db(db_path) as conn:
        for _, row in df.iterrows():
            upsert_bet(conn, dict(row))
    _log.info("migrate: %d rows imported from %s → %s", len(df), csv_path, db_path)
    return len(df)
