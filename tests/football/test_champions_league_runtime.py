from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.config import FOOTBALL_LEAGUES_WHITELIST, LEAGUE_REGISTRY
from src.football.champions_league_runtime import (
    CHAMPIONS_LEAGUE_CODE,
    CHAMPIONS_LEAGUE_PROVIDER,
    CHAMPIONS_LEAGUE_SPORT_KEY,
    ChampionsLeagueHealth,
    ChampionsLeaguePhase,
    ChampionsLeagueResult,
    ChampionsLeagueRunBundle,
    ChampionsLeagueRuntime,
    ChampionsLeagueRuntimeConfig,
    ChampionsLeagueRuntimeError,
    ChampionsLeagueSafety,
    ChampionsLeagueSettlement,
    InMemoryChampionsLeagueLifecycleStore,
)
from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionArtifact,
    ShadowSignalArtifact,
)

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _fixture(key: str = "ucl-1") -> Fixture:
    return Fixture(
        key,
        CHAMPIONS_LEAGUE_CODE,
        f"Home {key}",
        f"Away {key}",
        NOW + timedelta(hours=2),
    )


def _bundle(*, run_id: str = "ucl-run-1", fixture_keys: tuple[str, ...] = ("ucl-1",)):
    fixtures = tuple(_fixture(key) for key in fixture_keys)
    snapshots = tuple(
        MarketSnapshot(
            fixture_key=fixture.fixture_key,
            captured_at=NOW,
            kind=MarketSnapshotKind.SIGNAL_TIME,
            source=CHAMPIONS_LEAGUE_PROVIDER,
            odds={"home": 2.1, "draw": 3.4, "away": 3.3},
            snapshot_id=f"snapshot-{fixture.fixture_key}",
        )
        for fixture in fixtures
    )
    predictions = tuple(
        PredictionArtifact(
            prediction_id=f"prediction-{fixture.fixture_key}",
            fixture_key=fixture.fixture_key,
            league_code=CHAMPIONS_LEAGUE_CODE,
            model_adapter_id="unbound",
            generated_at=NOW,
            snapshot_id=f"snapshot-{fixture.fixture_key}",
            snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
            probabilities={"home": 0.4, "draw": 0.3, "away": 0.3},
        )
        for fixture in fixtures
    )
    signals = tuple(
        ShadowSignalArtifact(
            signal_id=f"signal-{fixture.fixture_key}",
            fixture_key=fixture.fixture_key,
            league_code=CHAMPIONS_LEAGUE_CODE,
            source=CHAMPIONS_LEAGUE_PROVIDER,
            model_adapter_id="unbound",
            activation_mode=ActivationMode.SHADOW,
            no_bet_flag=True,
            reason="disabled CL offline rehearsal",
        )
        for fixture in fixtures
    )
    results = tuple(
        ChampionsLeagueResult(
            result_id=f"result-{fixture.fixture_key}",
            fixture_key=fixture.fixture_key,
            home_score=1,
            away_score=0,
            observed_at=NOW,
            source="offline-result-fixture",
        )
        for fixture in fixtures
    )
    settlements = tuple(
        ChampionsLeagueSettlement(
            fixture_key=fixture.fixture_key,
            result_id=f"result-{fixture.fixture_key}",
            status="settled",
            settled_at=NOW,
        )
        for fixture in fixtures
    )
    return ChampionsLeagueRunBundle(
        run_id=run_id,
        fixtures=fixtures,
        snapshots=snapshots,
        predictions=predictions,
        signals=signals,
        results=results,
        settlements=settlements,
    )


def test_cl_support_is_catalogue_only_and_not_live_registered():
    assert CHAMPIONS_LEAGUE_SPORT_KEY in FOOTBALL_LEAGUES_WHITELIST
    assert CHAMPIONS_LEAGUE_SPORT_KEY not in LEAGUE_REGISTRY
    config = ChampionsLeagueRuntimeConfig()
    config.validate()
    assert config.activation_mode is ActivationMode.DISABLED
    assert config.provider_name == CHAMPIONS_LEAGUE_PROVIDER
    assert config.network_enabled is False
    assert config.scheduler_enabled is False


@pytest.mark.parametrize(
    "override",
    [
        {"provider_name": "therundown_experimental"},
        {"network_enabled": True},
        {"scheduler_enabled": True},
        {"publication_enabled": True},
        {"betting_enabled": True},
        {"ledger_enabled": True},
        {"retry_attempts": 2},
        {"model_adapter_id": "production-model"},
        {"activation_mode": ActivationMode.SHADOW},
    ],
)
def test_cl_runtime_rejects_authority_or_side_effect_escalation(override):
    with pytest.raises(ChampionsLeagueRuntimeError):
        ChampionsLeagueRuntimeConfig(**override).validate()


def test_offline_run_records_complete_order_and_is_idempotent():
    store = InMemoryChampionsLeagueLifecycleStore()
    runtime = ChampionsLeagueRuntime(store=store)
    bundle = _bundle()

    first = runtime.run_offline(bundle, now=NOW)
    second = runtime.run_offline(bundle, now=NOW + timedelta(seconds=1))

    assert first == second
    assert first.phases == (
        ChampionsLeaguePhase.FIXTURES_DISCOVERED,
        ChampionsLeaguePhase.PREMATCH_SCANNED,
        ChampionsLeaguePhase.ODDS_REFRESHED,
        ChampionsLeaguePhase.PREDICTION_DISPATCHED,
        ChampionsLeaguePhase.SIGNAL_EMITTED,
        ChampionsLeaguePhase.RESULT_INGESTED,
        ChampionsLeaguePhase.SETTLED,
    )
    assert len(store.events_for_run(bundle.run_id)) == 7
    assert runtime.recover(bundle.run_id) == store.events_for_run(bundle.run_id)
    assert first.activation_allowed is False
    assert first.publication_allowed is False
    assert first.betting_allowed is False


def test_digest_is_independent_of_input_order():
    bundle = _bundle(fixture_keys=("ucl-2", "ucl-1"))
    reversed_bundle = ChampionsLeagueRunBundle(
        run_id=bundle.run_id,
        fixtures=tuple(reversed(bundle.fixtures)),
        snapshots=tuple(reversed(bundle.snapshots)),
        predictions=tuple(reversed(bundle.predictions)),
        signals=tuple(reversed(bundle.signals)),
        results=tuple(reversed(bundle.results)),
        settlements=tuple(reversed(bundle.settlements)),
    )
    config = ChampionsLeagueRuntimeConfig()
    assert bundle.digest(config) == reversed_bundle.digest(config)


def test_changed_replay_fails_without_replacing_existing_lifecycle():
    store = InMemoryChampionsLeagueLifecycleStore()
    runtime = ChampionsLeagueRuntime(store=store)
    bundle = _bundle()
    runtime.run_offline(bundle, now=NOW)
    changed = _bundle(run_id=bundle.run_id)
    changed_snapshot = changed.snapshots[0]
    changed = ChampionsLeagueRunBundle(
        run_id=changed.run_id,
        fixtures=changed.fixtures,
        snapshots=(
            MarketSnapshot(
                fixture_key=changed_snapshot.fixture_key,
                captured_at=changed_snapshot.captured_at,
                kind=changed_snapshot.kind,
                source=changed_snapshot.source,
                odds={"home": 2.2, "draw": 3.4, "away": 3.3},
                snapshot_id=changed_snapshot.snapshot_id,
            ),
        ),
        predictions=changed.predictions,
        signals=changed.signals,
        results=changed.results,
        settlements=changed.settlements,
    )
    with pytest.raises(ChampionsLeagueRuntimeError, match="replay"):
        runtime.run_offline(changed, now=NOW)
    assert len(store.events_for_run(bundle.run_id)) == 7


@pytest.mark.parametrize(
    "mutator, message",
    [
        (
            lambda b: ChampionsLeagueRunBundle(
                **{**b.__dict__, "fixtures": (_fixture("foreign"),)}
            ),
            "unknown CL fixture",
        ),
        (
            lambda b: ChampionsLeagueRunBundle(
                **{
                    **b.__dict__,
                    "snapshots": (
                        MarketSnapshot(
                            fixture_key="ucl-1",
                            captured_at=NOW,
                            kind=MarketSnapshotKind.CLOSING,
                            source=CHAMPIONS_LEAGUE_PROVIDER,
                            odds={"home": 2.0},
                            snapshot_id="closing",
                        ),
                    ),
                }
            ),
            "closing",
        ),
        (
            lambda b: ChampionsLeagueRunBundle(
                **{
                    **b.__dict__,
                    "snapshots": (
                        MarketSnapshot(
                            fixture_key="ucl-1",
                            captured_at=NOW - timedelta(hours=2),
                            kind=MarketSnapshotKind.SIGNAL_TIME,
                            source=CHAMPIONS_LEAGUE_PROVIDER,
                            odds={"home": 2.0},
                            snapshot_id="stale",
                        ),
                    ),
                }
            ),
            "stale",
        ),
        (
            lambda b: ChampionsLeagueRunBundle(
                **{
                    **b.__dict__,
                    "safety": ChampionsLeagueSafety(no_bet=False),
                }
            ),
            "no-bet",
        ),
    ],
)
def test_cl_bundle_fail_closed(mutator, message):
    with pytest.raises(ChampionsLeagueRuntimeError, match=message):
        ChampionsLeagueRuntime().run_offline(mutator(_bundle()), now=NOW)


def test_readiness_reports_components_but_never_authorizes_activation():
    runtime = ChampionsLeagueRuntime()
    blocked = runtime.readiness(ChampionsLeagueHealth())
    assert blocked.offline_ready is False
    assert blocked.activation_allowed is False
    assert blocked.scheduler_enabled is False
    assert "fixture_discovery" in blocked.missing_components

    ready = runtime.readiness(
        ChampionsLeagueHealth(
            **{name: True for name in ChampionsLeagueHealth().__dict__}
        )
    )
    assert ready.offline_ready is True
    assert ready.activation_allowed is False
    assert ready.provider_authority == CHAMPIONS_LEAGUE_PROVIDER


def test_rollback_is_idempotent_and_non_financial():
    runtime = ChampionsLeagueRuntime()
    first = runtime.rollback("ucl-run-1", now=NOW, reason="health failure")
    second = runtime.rollback(
        "ucl-run-1", now=NOW + timedelta(seconds=1), reason="health failure"
    )
    assert first == second
    assert first.phase is ChampionsLeaguePhase.ROLLED_BACK


def test_network_execution_is_unavailable_and_module_has_no_live_side_effects():
    with pytest.raises(
        ChampionsLeagueRuntimeError, match="network execution is disabled"
    ):
        ChampionsLeagueRuntime().execute_network()
    source = inspect.getsource(
        __import__("src.football.champions_league_runtime", fromlist=["_x"])
    )
    assert "import requests" not in source
    assert "src.data.odds_api" not in source
    assert "append_bets" not in source
    assert "write_signals_json" not in source
    assert "upload_signals_to_cloud" not in source
