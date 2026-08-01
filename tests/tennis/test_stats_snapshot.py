"""J8-I7: Serve-Stats-Snapshot System — append + asof-Lookup."""
from __future__ import annotations

import json


def test_snapshot_writes_jsonl(tmp_path, monkeypatch):
    from scripts import tennis_stats_snapshot as snap
    from src.data.tennis_stats import ServeAggregate

    fake_history = tmp_path / "tennis_stats_history.jsonl"
    monkeypatch.setattr(snap, "SNAPSHOT_HISTORY", fake_history)
    monkeypatch.setattr(snap, "PLAYER_CACHE_DIR", tmp_path / "cache")

    def _fake_aggregate(name, last_n, tour="atp", **kw):
        return ServeAggregate(n_matches=15, dominance_rate=0.55,
                              ace_rate=0.06, df_rate=0.03,
                              win_rate=0.65, ace_df_ratio=2.0)
    monkeypatch.setattr("src.data.tennis_stats.fetch_aggregate", _fake_aggregate)

    entry = snap._snapshot_player("Alcaraz", "atp", dry_run=False)
    assert entry is not None
    lines = fake_history.read_text().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["player"] == "Alcaraz"
    assert payload["dominance_rate"] == 0.55


def test_load_snapshot_asof_returns_newest_before_date(tmp_path, monkeypatch):
    from scripts import tennis_stats_snapshot as snap

    fake_history = tmp_path / "tennis_stats_history.jsonl"
    fake_history.write_text(
        json.dumps({"snapshot_date": "2026-06-01", "player": "Alcaraz",
                    "tour": "atp", "n_matches": 12, "dominance_rate": 0.53,
                    "ace_rate": 0.05, "df_rate": 0.03, "win_rate": 0.6,
                    "ace_df_ratio": 1.7}) + "\n"
        + json.dumps({"snapshot_date": "2026-07-15", "player": "Alcaraz",
                      "tour": "atp", "n_matches": 15, "dominance_rate": 0.57,
                      "ace_rate": 0.06, "df_rate": 0.03, "win_rate": 0.7,
                      "ace_df_ratio": 2.0}) + "\n"
        + json.dumps({"snapshot_date": "2026-08-01", "player": "Alcaraz",
                      "tour": "atp", "n_matches": 18, "dominance_rate": 0.60,
                      "ace_rate": 0.07, "df_rate": 0.03, "win_rate": 0.75,
                      "ace_df_ratio": 2.3}) + "\n"
    )
    monkeypatch.setattr(snap, "SNAPSHOT_HISTORY", fake_history)

    # Match am 2026-07-20 → nur Snapshots vor diesem Datum kommen in Frage
    r = snap.load_snapshot_asof("Alcaraz", "2026-07-20", tour="atp")
    assert r is not None
    assert r["snapshot_date"] == "2026-07-15"


def test_load_snapshot_asof_empty_history_returns_none(tmp_path, monkeypatch):
    from scripts import tennis_stats_snapshot as snap
    fake = tmp_path / "empty.jsonl"
    monkeypatch.setattr(snap, "SNAPSHOT_HISTORY", fake)
    assert snap.load_snapshot_asof("Anyone", "2026-01-01") is None
