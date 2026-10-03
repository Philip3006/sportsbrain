"""Regression coverage for scanners preserving odds refreshed by other writers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.notifications.web_dashboard as dashboard
from src.signals import signal_status


def _timestamp(minutes_ago: int) -> str:
    value = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
        minutes=minutes_ago
    )
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(
    home: str,
    away: str,
    *,
    observed_at: str | None = None,
    captured_at: str | None = None,
    odds: tuple[float, float] = (1.9, 2.0),
    current: bool = True,
    private_context: bool = False,
) -> dict:
    observed_at = observed_at or _timestamp(5)
    captured_at = captured_at or observed_at
    snapshot = {
        "sport": "tennis",
        "match": f"{home} vs {away}",
        "home": home,
        "away": away,
        "fixture_key": f"tennis:{home.casefold()} vs {away.casefold()}",
        "outcomes": {"home": odds[0], "away": odds[1]},
        "odds_ts": observed_at,
        "source_ts": observed_at,
        "captured_at": captured_at,
        "source": "tennis_explorer",
        "bookmaker": "consensus",
        "odds_fetch_tier": 2,
        "freshness": "current" if current else "stale",
        "current": current,
    }
    if private_context:
        snapshot["signal_markets"] = {"private_runtime_context": 999}
    return snapshot


def _key(home: str, away: str) -> str:
    return f"tennis:{home.casefold()} vs {away.casefold()}"


def _prepare_writer(
    tmp_path: Path,
    monkeypatch,
    *,
    existing: dict,
    local: dict | None,
) -> tuple[Path, Path]:
    data_dir = tmp_path / "docs" / "data"
    data_dir.mkdir(parents=True)
    source = data_dir / "signals_philip.json"
    source.write_text(
        json.dumps(
            {
                "schedule": [],
                "football": [],
                "tennis": [],
                "current_match_odds": existing,
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / "runtime" / "current_match_odds.json"
    if local is not None:
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(json.dumps(local), encoding="utf-8")

    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    monkeypatch.setattr(signal_status, "_MATCH_ODDS_PATH", sidecar)
    monkeypatch.setattr(
        dashboard, "_ledger_path_for", lambda _user: tmp_path / "no-ledger.csv"
    )
    monkeypatch.setattr(dashboard, "_build_history", lambda **_kwargs: [])
    monkeypatch.setattr(dashboard, "_get_closed_bets", lambda **_kwargs: [])
    monkeypatch.setattr(
        dashboard, "_get_settled_bets_for_dashboard", lambda **_kwargs: []
    )
    monkeypatch.setattr(dashboard, "_build_wm_stats", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard, "_build_tennis_stats", lambda **_kwargs: {})
    monkeypatch.setattr(dashboard, "upload_signals_to_cloud", lambda **_kwargs: True)
    monkeypatch.delenv("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", raising=False)

    # The writer normally supplements the payload with scores; keep this
    # integration test offline and independent of provider configuration.
    from src.data import odds_api

    monkeypatch.setattr(odds_api, "fetch_wm_scores", lambda **_kwargs: [])
    return source, data_dir / "signals.json"


def _run_tennis_scan_writer(monkeypatch) -> None:
    dashboard.write_signals_json(
        schedule=[
            {
                "sport": "tennis",
                "home": "New Schedule Player",
                "away": "Other Player",
                "kickoff": "2026-10-04T10:00:00Z",
            }
        ],
        open_bets=[],
        user="philip",
    )


def _read_map(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["current_match_odds"]


def test_tennis_scan_without_sidecar_preserves_all_seven_public_odds(
    tmp_path, monkeypatch
):
    names = [
        ("Katerina Siniakova", "Elina Svitolina"),
        ("Donna Vekic", "Lin Zhu"),
        ("Jelena Ostapenko", "Paula Badosa"),
        ("Alex de Minaur", "Quentin Halys"),
        ("Alexander Zverev", "Juncheng Shang"),
        ("Andrey Rublev", "Roman Safiullin"),
        ("Francisco Cerundolo", "Jakub Mensik"),
    ]
    before = {
        _key(home, away): _snapshot(
            home, away, observed_at=_timestamp(45), current=False
        )
        for home, away in names
    }
    source, shared = _prepare_writer(
        tmp_path, monkeypatch, existing=before, local=None
    )
    assert not (tmp_path / "runtime" / "current_match_odds.json").exists()

    _run_tennis_scan_writer(monkeypatch)

    for output in (source, shared):
        after = _read_map(output)
        assert len(after) == 7
        assert set(after) == set(before)
        for match_key in before:
            for field in (
                "outcomes",
                "odds_ts",
                "source",
                "source_ts",
                "captured_at",
            ):
                assert after[match_key][field] == before[match_key][field]
            assert after[match_key]["current"] is False
            assert after[match_key]["freshness"] == "stale"


def test_newer_local_observation_updates_only_its_match(tmp_path, monkeypatch):
    a = ("Player A", "Player B")
    b = ("Player C", "Player D")
    old_a = _snapshot(*a, observed_at=_timestamp(20), odds=(1.8, 2.1))
    old_b = _snapshot(*b, observed_at=_timestamp(12), odds=(2.0, 1.9))
    new_a = _snapshot(*a, observed_at=_timestamp(4), odds=(1.95, 2.05))
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*a): old_a, _key(*b): old_b},
        local={_key(*a): new_a},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)
    assert after[_key(*a)]["outcomes"] == new_a["outcomes"]
    assert after[_key(*a)]["odds_ts"] == new_a["odds_ts"]
    assert after[_key(*b)]["outcomes"] == old_b["outcomes"]
    assert after[_key(*b)]["source_ts"] == old_b["source_ts"]


def test_older_local_observation_cannot_replace_newer_public_snapshot(
    tmp_path, monkeypatch
):
    match = ("Player A", "Player B")
    existing = _snapshot(*match, observed_at=_timestamp(4), odds=(1.9, 2.0))
    local = _snapshot(*match, observed_at=_timestamp(20), odds=(1.7, 2.2))
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*match): existing},
        local={_key(*match): local},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)[_key(*match)]
    assert after["outcomes"] == existing["outcomes"]
    assert after["source_ts"] == existing["source_ts"]


def test_malformed_local_timestamp_does_not_delete_existing_snapshot(
    tmp_path, monkeypatch
):
    match = ("Player A", "Player B")
    existing = _snapshot(*match, observed_at=_timestamp(8), odds=(1.9, 2.0))
    malformed = _snapshot(*match, observed_at="not-a-timestamp", odds=(2.4, 1.5))
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*match): existing},
        local={_key(*match): malformed},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)[_key(*match)]
    assert after["outcomes"] == existing["outcomes"]
    assert after["odds_ts"] == existing["odds_ts"]
    assert after["source_ts"] == existing["source_ts"]


def test_malformed_local_market_does_not_delete_existing_snapshot(
    tmp_path, monkeypatch
):
    match = ("Player A", "Player B")
    existing = _snapshot(*match, observed_at=_timestamp(8), odds=(1.9, 2.0))
    malformed = _snapshot(*match, observed_at=_timestamp(2), odds=(2.4, 1.5))
    malformed["outcomes"] = {"home": 2.4}
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*match): existing},
        local={_key(*match): malformed},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)[_key(*match)]
    assert after["outcomes"] == existing["outcomes"]
    assert after["source_ts"] == existing["source_ts"]


def test_tennis_local_odds_timestamp_cannot_advance_past_source_observation(
    tmp_path, monkeypatch
):
    match = ("Player A", "Player B")
    existing = _snapshot(*match, observed_at=_timestamp(12), odds=(1.9, 2.0))
    fabricated = _snapshot(
        *match,
        observed_at=_timestamp(8),
        captured_at=_timestamp(1),
        odds=(2.4, 1.5),
    )
    fabricated["odds_ts"] = _timestamp(2)
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*match): existing},
        local={_key(*match): fabricated},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)[_key(*match)]
    assert after["outcomes"] == existing["outcomes"]
    assert after["odds_ts"] == existing["odds_ts"]
    assert after["source_ts"] == existing["source_ts"]


def test_empty_existing_and_missing_local_state_publish_empty_map(
    tmp_path, monkeypatch
):
    source, shared = _prepare_writer(
        tmp_path, monkeypatch, existing={}, local=None
    )

    _run_tennis_scan_writer(monkeypatch)

    assert _read_map(source) == {}
    assert _read_map(shared) == {}


def test_pr275_provenance_survives_republication_without_timestamp_changes(
    tmp_path, monkeypatch
):
    match = ("Katerina Siniakova", "Elina Svitolina")
    observed_at = _timestamp(8)
    captured_at = _timestamp(7)
    quote = _snapshot(
        *match,
        observed_at=observed_at,
        captured_at=captured_at,
        odds=(1.83, 2.04),
        private_context=True,
    )
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={},
        local={_key(*match): quote},
    )

    _run_tennis_scan_writer(monkeypatch)

    public_quote = _read_map(source)[_key(*match)]
    for field in ("outcomes", "odds_ts", "source", "source_ts", "captured_at"):
        assert public_quote[field] == quote[field]
    assert public_quote["current"] is True
    assert public_quote["freshness"] == "current"
    assert "signal_markets" not in public_quote
    assert "bankroll_state" not in json.dumps(public_quote)
    assert "open_bets" not in json.dumps(public_quote)


def test_repushing_stale_snapshot_does_not_make_it_current_or_advance_timestamps(
    tmp_path, monkeypatch
):
    match = ("Player A", "Player B")
    stale = _snapshot(
        *match, observed_at=_timestamp(45), current=False, odds=(1.9, 2.0)
    )
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*match): stale},
        local={},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)[_key(*match)]
    assert after["current"] is False
    assert after["freshness"] == "stale"
    assert after["odds_ts"] == stale["odds_ts"]
    assert after["source_ts"] == stale["source_ts"]
    assert after["captured_at"] == stale["captured_at"]


def test_newer_but_stale_observation_remains_stale_after_republication(
    tmp_path, monkeypatch
):
    match = ("Player A", "Player B")
    existing = _snapshot(
        *match, observed_at=_timestamp(90), current=False, odds=(1.8, 2.1)
    )
    newer_stale = _snapshot(
        *match, observed_at=_timestamp(45), current=False, odds=(1.9, 2.0)
    )
    source, _shared = _prepare_writer(
        tmp_path,
        monkeypatch,
        existing={_key(*match): existing},
        local={_key(*match): newer_stale},
    )

    _run_tennis_scan_writer(monkeypatch)

    after = _read_map(source)[_key(*match)]
    assert after["outcomes"] == newer_stale["outcomes"]
    assert after["odds_ts"] == newer_stale["odds_ts"]
    assert after["current"] is False
    assert after["freshness"] == "stale"
