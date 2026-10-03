"""Regression coverage for odds-refresh staging, bounded retry, and provider scope."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _signal(*, sport: str = "football") -> dict:
    return {
        "sport": sport,
        "match": "Alpha vs Beta",
        "market": "home",
        "league": "bl2" if sport == "football" else "atp",
        "kickoff": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "model_prob": 55.0,
    }


def test_football_refresh_uses_explicit_schedule_sport_key(monkeypatch):
    from src.signals import odds_refresher

    captured = []

    class Quote:
        h2h_home = 2.0
        h2h_draw = 3.2
        h2h_away = 3.8

    monkeypatch.setattr(odds_refresher, "is_provider_available", lambda _name: True)
    monkeypatch.setattr(odds_refresher, "record_success", lambda _name: None)
    monkeypatch.setattr(
        "src.football.odds.the_odds_api.fetch",
        lambda hint: captured.append(hint) or Quote(),
    )

    quote, source, tier = odds_refresher._fetch_football_quote(
        {**_signal(), "sport_key": "soccer_epl", "league": "EPL"}
    )

    assert quote is not None
    assert source == "the_odds_api"
    assert tier == 1
    assert captured[0]["sport_key"] == "soccer_epl"


def test_legacy_bl2_signal_refresh_keeps_its_canonical_sport_key(monkeypatch):
    from src.signals import odds_refresher

    captured = []

    class Quote:
        h2h_home = 2.0
        h2h_draw = 3.2
        h2h_away = 3.8

    monkeypatch.setattr(odds_refresher, "is_provider_available", lambda _name: True)
    monkeypatch.setattr(odds_refresher, "record_success", lambda _name: None)
    monkeypatch.setattr(
        "src.football.odds.the_odds_api.fetch",
        lambda hint: captured.append(hint) or Quote(),
    )

    quote, _source, _tier = odds_refresher._fetch_football_quote(_signal())

    assert quote is not None
    assert captured[0]["sport_key"] == "soccer_germany_bundesliga2"


def test_football_refresh_rejects_missing_or_unknown_sport_key_before_provider(
    monkeypatch,
):
    from src.signals import odds_refresher

    calls = []
    monkeypatch.setattr(odds_refresher, "is_provider_available", lambda _name: True)
    monkeypatch.setattr(
        "src.football.odds.the_odds_api.fetch",
        lambda hint: calls.append(hint),
    )

    for context in (
        {**_signal(), "league": "not-a-real-league"},
        {**_signal(), "league": "bl2", "sport_key": "soccer_unknown"},
        {**_signal(), "league": "EPL", "sport_key": "soccer_spain_la_liga"},
    ):
        assert odds_refresher._fetch_football_quote(context) == (None, "", 0)

    assert calls == []


def test_schedule_only_unknown_football_has_zero_requests_and_no_snapshot(monkeypatch):
    from src.signals import odds_refresher

    schedule = [
        {
            "sport": "football",
            "home": "Alpha",
            "away": "Beta",
            "league": "unknown-league",
            "kickoff": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "event_status": "UPCOMING",
        }
    ]
    calls = []
    snapshots = []
    monkeypatch.setattr(odds_refresher, "_load_signals", list)
    monkeypatch.setattr(odds_refresher, "_load_schedule", lambda: schedule)
    monkeypatch.setattr(odds_refresher, "load_odds_state", dict)
    monkeypatch.setattr(odds_refresher, "load_current_match_odds", dict)
    monkeypatch.setattr(
        odds_refresher,
        "update_current_match_odds",
        lambda *args: snapshots.append(args),
    )
    monkeypatch.setattr(
        "src.football.odds.the_odds_api.fetch",
        lambda hint: calls.append(hint),
    )

    result = odds_refresher.run_refresh(dry_run=False)

    assert result["failed"] == 1
    assert calls == []
    assert snapshots == []


def test_retry_backoff_prevents_immediate_repeat_attempt():
    from src.signals.odds_refresher import _is_refresh_due, _retry_after

    signal = _signal()
    retry_after, failures = _retry_after(signal, None, datetime.now(timezone.utc))

    assert failures == 1
    assert (
        _is_refresh_due(signal, {"retry_after_ts": retry_after, "odds_ts": None})
        is False
    )


def test_tennis_refresh_caches_one_provider_call_and_excludes_websearch(monkeypatch):
    from src.signals.odds_refresher import _refresh_tennis
    from src.tennis.odds import merger

    calls: list[dict] = []

    class Quote:
        h2h_a = 1.8
        h2h_b = 2.1
        source = "tennis_explorer"
        source_tier = 1
        no_bet_flag = False
        ts = datetime.now(timezone.utc)

    def fetch(match_hint, **kwargs):
        calls.append(kwargs)
        return Quote(), [
            {
                "provider": "tennis_explorer",
                "attempted": True,
                "eligible": True,
                "status_class": "success",
                "http_status": None,
                "result": "usable_quote",
                "error_class": None,
            }
        ]

    monkeypatch.setattr(merger, "fetch_best_odds_with_diagnostics", fetch)
    cache: dict = {}
    signal = _signal(sport="tennis")

    assert _refresh_tennis(signal, cache) == (1.8, "tennis_explorer", 1)
    assert _refresh_tennis(signal, cache) == (1.8, "tennis_explorer", 1)
    assert len(calls) == 1
    assert calls[0]["include_websearch"] is False
    assert calls[0]["allow_implied"] is False


def test_tennis_snapshot_preserves_both_primary_outcomes_from_one_fetch(monkeypatch):
    from src.signals.odds_refresher import _refresh_tennis_snapshot
    from src.tennis.odds import merger

    calls = []

    class Quote:
        h2h_a = 1.8
        h2h_b = 2.1
        source = "tennis_explorer"
        source_tier = 1
        bookmaker = "consensus"
        no_bet_flag = False
        ts = datetime.now(timezone.utc)

    monkeypatch.setattr(
        merger,
        "fetch_best_odds_with_diagnostics",
        lambda *_args, **_kwargs: calls.append(1) or (Quote(), []),
    )
    snapshot = _refresh_tennis_snapshot(_signal(sport="tennis"), {})

    assert snapshot["outcomes"] == {"home": 1.8, "away": 2.1}
    assert snapshot["source"] == "tennis_explorer"
    assert snapshot["current"] is True
    assert len(calls) == 1


def test_tennis_snapshot_uses_provider_observation_time_for_freshness(monkeypatch):
    from src.signals.odds_refresher import _refresh_tennis_snapshot
    from src.tennis.odds import merger

    observed_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
        minutes=8
    )

    class Quote:
        h2h_a = 1.8
        h2h_b = 2.1
        source = "tennis_explorer"
        source_tier = 2
        bookmaker = "consensus"
        no_bet_flag = False
        ts = observed_at

    monkeypatch.setattr(
        merger, "fetch_best_odds_with_diagnostics", lambda *_a, **_kw: (Quote(), [])
    )
    snapshot = _refresh_tennis_snapshot(_signal(sport="tennis"), {})

    assert snapshot is not None
    assert snapshot["odds_ts"] == snapshot["source_ts"]
    assert snapshot["odds_ts"] == observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert snapshot["captured_at"] > snapshot["odds_ts"]


def test_tennis_snapshot_rejects_missing_or_stale_quote_timestamp(monkeypatch):
    from src.signals.odds_refresher import _refresh_tennis_snapshot
    from src.tennis.odds import merger

    class Quote:
        h2h_a = 1.8
        h2h_b = 2.1
        source = "tennis_explorer"
        source_tier = 2
        bookmaker = "consensus"
        no_bet_flag = False
        ts = None

    monkeypatch.setattr(
        merger, "fetch_best_odds_with_diagnostics", lambda *_a, **_kw: (Quote(), [])
    )
    assert _refresh_tennis_snapshot(_signal(sport="tennis"), {}) is None

    Quote.ts = datetime.now(timezone.utc) - timedelta(minutes=31)
    assert _refresh_tennis_snapshot(_signal(sport="tennis"), {}) is None


def test_schedule_only_match_is_refreshed_without_a_signal(monkeypatch):
    from src.signals import odds_refresher

    schedule = [
        {
            "sport": "tennis",
            "home": "Alpha",
            "away": "Beta",
            "kickoff": (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "event_status": "UPCOMING",
            "tournament": "Test Open",
        }
    ]
    snapshots = []
    monkeypatch.setattr(odds_refresher, "_load_signals", list)
    monkeypatch.setattr(odds_refresher, "_load_schedule", lambda: schedule)
    monkeypatch.setattr(odds_refresher, "load_odds_state", dict)
    monkeypatch.setattr(odds_refresher, "load_current_match_odds", dict)
    monkeypatch.setattr(
        odds_refresher,
        "_refresh_tennis_snapshot",
        lambda *_args, **_kwargs: {
            "sport": "tennis",
            "match": "Alpha vs Beta",
            "home": "Alpha",
            "away": "Beta",
            "fixture_key": "tennis:alpha vs beta",
            "outcomes": {"home": 1.8, "away": 2.1},
            "odds_ts": "2026-10-02T12:00:00Z",
            "captured_at": "2026-10-02T12:00:00Z",
            "source_ts": "2026-10-02T11:59:00Z",
            "source": "tennis_explorer",
            "bookmaker": "consensus",
            "odds_fetch_tier": 1,
            "freshness": "current",
            "current": True,
        },
    )
    monkeypatch.setattr(
        odds_refresher,
        "update_current_match_odds",
        lambda key, value: snapshots.append((key, value)),
    )
    from src.notifications import web_dashboard

    monkeypatch.setattr(
        web_dashboard, "write_signals_json_all_users", lambda **_kwargs: []
    )
    monkeypatch.setattr(odds_refresher, "_publish_staged_signals", lambda _path: None)
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STAGE_BASE", "/tmp")
    monkeypatch.setattr(
        odds_refresher.tempfile,
        "mkdtemp",
        lambda **_kwargs: "/tmp/sportsbrain-test-stage",
    )

    result = odds_refresher.run_refresh()

    assert result["refreshed"] == 1
    assert result["refreshed_by_sport"] == {"football": 0, "tennis": 1}
    assert snapshots[0][1]["outcomes"] == {"home": 1.8, "away": 2.1}


def test_refresh_stages_public_artifacts_without_mutating_active_snapshot(
    monkeypatch, tmp_path
):
    from src.notifications import web_dashboard
    from src.signals import odds_refresher

    active = tmp_path / "active-signals.json"
    active.write_text('{"football": []}\n')
    stage = tmp_path / "stage"
    monkeypatch.setattr(odds_refresher, "_SIGNALS_JSON", active)
    monkeypatch.setattr(odds_refresher, "_load_signals", lambda: [_signal()])
    monkeypatch.setattr(odds_refresher, "_load_schedule", list)
    monkeypatch.setattr(odds_refresher, "load_odds_state", dict)
    monkeypatch.setattr(
        odds_refresher,
        "_refresh_football_snapshot",
        lambda _signal: {
            "sport": "football",
            "match": "Alpha vs Beta",
            "home": "Alpha",
            "away": "Beta",
            "fixture_key": "football:alpha vs beta",
            "outcomes": {"home": 2.0, "draw": 3.2, "away": 3.8},
            "odds_ts": "2026-10-02T12:00:00Z",
            "captured_at": "2026-10-02T12:00:00Z",
            "source_ts": "2026-10-02T12:00:00Z",
            "source": "the_odds_api",
            "bookmaker": "consensus",
            "odds_fetch_tier": 1,
            "freshness": "current",
            "current": True,
        },
    )
    monkeypatch.setattr(
        odds_refresher, "update_odds_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        odds_refresher, "update_current_match_odds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STAGE_BASE", str(tmp_path))
    monkeypatch.setattr(
        odds_refresher.tempfile, "mkdtemp", lambda **_kwargs: str(stage)
    )
    published: list[object] = []
    monkeypatch.setattr(
        odds_refresher, "_publish_staged_signals", lambda path: published.append(path)
    )

    def write_all_users(**_kwargs):
        staged = stage / "docs" / "data" / "signals_philip.json"
        staged.parent.mkdir(parents=True)
        staged.write_text('{"football": [{"signal_id": "fresh"}]}\n')
        (stage / "docs" / "data" / "signals.json").write_text(staged.read_text())
        return []

    monkeypatch.setattr(web_dashboard, "write_signals_json_all_users", write_all_users)

    summary = odds_refresher.run_refresh()

    assert summary["refreshed"] == 1
    assert summary["due_by_sport"] == {"football": 1, "tennis": 0}
    assert summary["refreshed_by_sport"] == {"football": 1, "tennis": 0}
    assert active.read_text() == '{"football": []}\n'
    assert published == [stage]
    assert (stage / "docs" / "data" / "signals.json").exists()


def test_publication_failure_is_an_explicit_refresh_result(monkeypatch, tmp_path):
    from src.notifications import web_dashboard
    from src.signals import odds_refresher

    monkeypatch.setattr(odds_refresher, "_load_signals", lambda: [_signal()])
    monkeypatch.setattr(odds_refresher, "_load_schedule", list)
    monkeypatch.setattr(odds_refresher, "load_odds_state", dict)
    monkeypatch.setattr(
        odds_refresher,
        "_refresh_football_snapshot",
        lambda _signal: {
            "sport": "football",
            "match": "Alpha vs Beta",
            "home": "Alpha",
            "away": "Beta",
            "fixture_key": "football:alpha vs beta",
            "outcomes": {"home": 2.0, "draw": 3.2, "away": 3.8},
            "odds_ts": "2026-10-02T12:00:00Z",
            "captured_at": "2026-10-02T12:00:00Z",
            "source_ts": "2026-10-02T12:00:00Z",
            "source": "the_odds_api",
            "bookmaker": "consensus",
            "odds_fetch_tier": 1,
            "freshness": "current",
            "current": True,
        },
    )
    monkeypatch.setattr(
        odds_refresher, "update_odds_state", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        odds_refresher, "update_current_match_odds", lambda *_args, **_kwargs: None
    )
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STAGE_BASE", str(tmp_path))
    monkeypatch.setattr(
        odds_refresher.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path / "stage")
    )
    monkeypatch.setattr(
        web_dashboard, "write_signals_json_all_users", lambda **_kwargs: ["philip"]
    )

    assert odds_refresher.run_refresh()["publication_failed"] is True
