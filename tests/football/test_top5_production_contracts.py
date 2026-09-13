import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    TOP5_STAGED_ARTIFACT_PREFIX,
    ActivationMode,
    Fixture,
    FixtureIngestor,
    LeagueProductionConfig,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionInput,
    ProductionContractError,
    ShadowSignalArtifact,
    SignalTimeContract,
    validate_artifact_ownership,
    validate_disabled_top5_configs,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
SIGNAL_TIME = SignalTimeContract(60, 180, 900)
FIXTURE = Fixture("fixture-1", "sample", "Home", "Away", NOW + timedelta(minutes=90))


def _config(**overrides):
    values = {
        "league_code": "sample",
        "display_name": "Sample League",
        "provider_sport_key": "soccer_sample_league",
        "fixture_source": "provider",
        "result_source": "results_router",
        "model_adapter_id": "model-under-review",
    }
    values.update(overrides)
    return LeagueProductionConfig(**values)


def _signal_snapshot():
    return MarketSnapshot(
        fixture_key=FIXTURE.fixture_key,
        captured_at=NOW,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="provider",
        odds={"home": 2.0},
    )


def test_new_league_configs_are_disabled_by_default():
    config = _config()
    assert config.activation_mode is ActivationMode.DISABLED
    validate_disabled_top5_configs([config])


def test_enabled_config_requires_explicit_signal_time_contract():
    with pytest.raises(ProductionContractError, match="signal-time"):
        _config(activation_mode=ActivationMode.SHADOW).validate()


def test_signal_time_contract_validates_window_and_odds_freshness():
    assert SIGNAL_TIME.accepts(FIXTURE.kickoff, NOW, NOW)
    assert not SIGNAL_TIME.accepts(FIXTURE.kickoff, NOW - timedelta(minutes=16), NOW)
    assert not SIGNAL_TIME.accepts(NOW + timedelta(minutes=30), NOW, NOW)


@pytest.mark.parametrize(
    ("kickoff", "odds_captured_at", "now", "field"),
    [
        (FIXTURE.kickoff.replace(tzinfo=None), NOW, NOW, "kickoff"),
        (FIXTURE.kickoff, NOW.replace(tzinfo=None), NOW, "odds_captured_at"),
        (FIXTURE.kickoff, NOW, NOW.replace(tzinfo=None), "now"),
    ],
)
def test_signal_time_contract_rejects_naive_timestamps(
    kickoff: datetime,
    odds_captured_at: datetime,
    now: datetime,
    field: str,
):
    with pytest.raises(ProductionContractError, match=field):
        SIGNAL_TIME.accepts(kickoff, odds_captured_at, now)


def test_signal_time_contract_normalizes_aware_offsets_before_evaluation():
    plus_two = timezone(timedelta(hours=2))

    assert SIGNAL_TIME.accepts(FIXTURE.kickoff, NOW, NOW)
    assert SIGNAL_TIME.accepts(
        FIXTURE.kickoff.astimezone(plus_two),
        NOW.astimezone(plus_two),
        NOW.astimezone(plus_two),
    )


def test_closing_odds_cannot_become_prediction_input():
    closing = MarketSnapshot(
        fixture_key=FIXTURE.fixture_key,
        captured_at=NOW,
        kind=MarketSnapshotKind.CLOSING,
        source="provider",
        odds={"home": 2.0},
    )
    with pytest.raises(ProductionContractError, match="closing odds"):
        PredictionInput.create(FIXTURE, closing, {}, SIGNAL_TIME, NOW)


def test_signal_time_prediction_input_requires_fixture_identity_and_freshness():
    model_input = PredictionInput.create(FIXTURE, _signal_snapshot(), {"form": 1.0}, SIGNAL_TIME, NOW)
    assert model_input.signal_snapshot.kind is MarketSnapshotKind.SIGNAL_TIME


def test_shadow_artifacts_require_complete_provenance_and_no_bet():
    artifact = ShadowSignalArtifact(
        signal_id="shadow-1",
        fixture_key=FIXTURE.fixture_key,
        league_code="sample",
        source="provider",
        model_adapter_id="model-under-review",
        activation_mode=ActivationMode.SHADOW,
        no_bet_flag=True,
        reason="shadow_only",
    )
    artifact.validate()
    with pytest.raises(ProductionContractError, match="no-bet"):
        ShadowSignalArtifact(**{**artifact.__dict__, "no_bet_flag": False}).validate()


@pytest.mark.parametrize(
    "path",
    [
        "src/football/new_scanner.py",
        "scripts/top5_scan.py",
        "tests/football/test_top5.py",
        ".github/workflows/top5.yml",
        "cloudflare/worker.js",
        "results/ledger_philip.csv",
    ],
)
def test_artifact_ownership_rejects_source_and_financial_paths(path):
    with pytest.raises(ProductionContractError, match="runtime-safe"):
        validate_artifact_ownership(path)


def test_artifact_ownership_allows_only_staged_shadow_artifacts():
    validate_artifact_ownership(f"{TOP5_STAGED_ARTIFACT_PREFIX}sample.json")


@pytest.mark.parametrize(
    "path",
    [
        "docs/data/signals.json",
        "docs/data/top5/research.json",
        "docs/data/top5/shadow/.env.json",
        "docs/data/top5/shadow/secrets.json",
        "docs/data/top5/../signals.json",
        "data/cache/top5_shadow.json",
        "models/top5/model.pkl",
    ],
)
def test_artifact_ownership_rejects_non_shadow_staged_paths(path):
    with pytest.raises(ProductionContractError, match="runtime-safe"):
        validate_artifact_ownership(path)


def test_fixture_ingestor_contract_supports_a_league_isolated_adapter():
    class FakeIngestor:
        def fetch(self, config):
            assert config.league_code == "sample"
            return [FIXTURE]

    adapter = FakeIngestor()
    assert isinstance(adapter, FixtureIngestor)
    assert adapter.fetch(_config()) == [FIXTURE]


def test_scaffold_has_no_ledger_or_publisher_entry_point():
    import src.football.production_contracts as contracts

    source = inspect.getsource(contracts)
    for prohibited in ("append_bets", "write_signals_json", "upload_signals_to_cloud"):
        assert prohibited not in source
