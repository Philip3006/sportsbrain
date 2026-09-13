import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    ArtifactOwner,
    ActivationMode,
    DISABLED_TOP5_LEAGUE_CONFIGS,
    Fixture,
    FixtureIngestor,
    LeagueProductionConfig,
    MarketSnapshot,
    MarketSnapshotKind,
    OddsRequest,
    PredictionInput,
    PredictionArtifact,
    ProviderMapping,
    ProductionContractError,
    RolloutEvidence,
    RolloutStage,
    RuntimeStateBinding,
    RuntimeStateOwner,
    ShadowSignalArtifact,
    SignalTimeContract,
    TOP5_HEALTH_PREFIX,
    TOP5_SHADOW_ARCHIVE_PREFIX,
    TOP5_STAGED_ARTIFACT_PREFIX,
    validate_artifact_ownership,
    validate_disabled_top5_configs,
)
from src.football.production_pipeline import run_shadow_pipeline

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


@pytest.mark.parametrize("field", ["kickoff", "odds_captured_at", "now"])
def test_signal_time_contract_rejects_naive_timestamps(field):
    values = {"kickoff": FIXTURE.kickoff, "odds_captured_at": NOW, "now": NOW}
    values[field] = values[field].replace(tzinfo=None)
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        SIGNAL_TIME.accepts(**values)


def test_aware_contract_timestamps_normalize_to_utc():
    offset = timezone(timedelta(hours=2))
    fixture = Fixture("offset-fixture", "sample", "Home", "Away", datetime(2026, 9, 11, 14, 30, tzinfo=offset))
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=datetime(2026, 9, 11, 14, tzinfo=offset),
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="provider",
        odds={"home": 2.0},
    )
    request = OddsRequest(
        league_code="sample",
        provider_name="provider",
        sport_key="soccer_sample_league",
        fixture_keys=(fixture.fixture_key,),
        markets=("h2h",),
        regions=("eu",),
        requested_at=datetime(2026, 9, 11, 14, tzinfo=offset),
    )
    artifact = PredictionArtifact(
        prediction_id="prediction-offset",
        fixture_key=fixture.fixture_key,
        league_code="sample",
        model_adapter_id="model-under-review",
        generated_at=datetime(2026, 9, 11, 14, tzinfo=offset),
        snapshot_id="snapshot-offset",
        snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
        probabilities={"home": 0.5},
    )
    assert fixture.kickoff.tzinfo is timezone.utc
    assert snapshot.captured_at.tzinfo is timezone.utc
    assert request.requested_at.tzinfo is timezone.utc
    assert artifact.generated_at.tzinfo is timezone.utc
    assert fixture.kickoff.hour == 12
    assert snapshot.captured_at.hour == request.requested_at.hour == artifact.generated_at.hour == 12


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Fixture("naive", "sample", "Home", "Away", NOW.replace(tzinfo=None)),
        lambda: MarketSnapshot("fixture-1", NOW.replace(tzinfo=None), MarketSnapshotKind.SIGNAL_TIME, "provider", {"home": 2.0}),
        lambda: OddsRequest("sample", "provider", "soccer_sample_league", ("fixture-1",), ("h2h",), ("eu",), NOW.replace(tzinfo=None)),
        lambda: PredictionArtifact("naive", "fixture-1", "sample", "model", NOW.replace(tzinfo=None), "snapshot", MarketSnapshotKind.SIGNAL_TIME, {"home": 0.5}),
    ],
)
def test_contract_objects_reject_naive_timestamps(factory):
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        factory()


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
        "docs/data/signals.json",
        "results/shadow/BL1/fixture-1.json",
        "results/health/football.json",
        "models/top5/prediction.json",
        "research/top5/notes.json",
        "docs/data/top5/shadow/../secrets.json",
    ],
)
def test_artifact_ownership_rejects_source_and_financial_paths(path):
    with pytest.raises(ProductionContractError, match="runtime-safe"):
        validate_artifact_ownership(path)


def test_artifact_ownership_allows_staged_public_artifacts_only():
    validate_artifact_ownership(f"{TOP5_STAGED_ARTIFACT_PREFIX}signals.json")


def test_disabled_registry_covers_top5_without_binding_a_model():
    from src.config import LEAGUE_REGISTRY

    assert {config.provider_sport_key for config in DISABLED_TOP5_LEAGUE_CONFIGS} == {
        "soccer_germany_bundesliga",
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_1",
    }
    assert all(config.activation_mode is ActivationMode.DISABLED for config in DISABLED_TOP5_LEAGUE_CONFIGS)
    assert all(config.model_adapter_id == "unbound" for config in DISABLED_TOP5_LEAGUE_CONFIGS)
    assert not {config.provider_sport_key for config in DISABLED_TOP5_LEAGUE_CONFIGS}.intersection(LEAGUE_REGISTRY)


def test_odds_request_is_bulk_and_stable_for_cache_coalescing():
    config = _config(
        provider_mapping=ProviderMapping(
            provider_name="fixture-provider",
            competition_id="sample-competition",
            sport_key="soccer_sample_league",
            markets=("totals", "h2h", "h2h"),
            regions=("uk", "eu"),
        )
    )
    other = Fixture("fixture-2", "sample", "Other Home", "Other Away", NOW + timedelta(minutes=120))
    request = OddsRequest.for_config(config, [other, FIXTURE], NOW)
    assert request.fixture_keys == ("fixture-1", "fixture-2")
    assert request.markets == ("h2h", "totals")
    assert request.regions == ("eu", "uk")
    assert request.request_key == OddsRequest.for_config(config, [FIXTURE, other], NOW).request_key


def test_prediction_artifact_rejects_closing_snapshot_kind():
    artifact = PredictionArtifact(
        prediction_id="prediction-1",
        fixture_key=FIXTURE.fixture_key,
        league_code=FIXTURE.league_code,
        model_adapter_id="model-under-review",
        generated_at=NOW,
        snapshot_id="snapshot-1",
        snapshot_kind=MarketSnapshotKind.CLOSING,
        probabilities={"home": 0.5},
    )
    with pytest.raises(ProductionContractError, match="closing"):
        artifact.validate()


def test_artifact_ownership_can_be_scoped_to_explicit_owner():
    validate_artifact_ownership(f"{TOP5_SHADOW_ARCHIVE_PREFIX}BL1/fixture-1.json", ArtifactOwner.SHADOW_ARCHIVE)
    validate_artifact_ownership(f"{TOP5_HEALTH_PREFIX}run.json", ArtifactOwner.HEALTH)
    with pytest.raises(ProductionContractError, match="ownership"):
        validate_artifact_ownership(f"{TOP5_STAGED_ARTIFACT_PREFIX}signals.json", ArtifactOwner.SHADOW_ARCHIVE)


def test_runtime_state_binding_requires_external_owner_and_safe_path():
    RuntimeStateBinding(
        RuntimeStateOwner.PROVIDER_BUDGET,
        "data/cache/provider_budget.json",
    ).validate()
    with pytest.raises(ProductionContractError, match="external"):
        RuntimeStateBinding(
            RuntimeStateOwner.PROVIDER_BUDGET,
            "data/cache/provider_budget.json",
            external_required=False,
        ).validate()
    with pytest.raises(ProductionContractError, match="public"):
        RuntimeStateBinding(RuntimeStateOwner.TOP5_SHADOW, "docs/data/top5.json").validate()


def test_rollout_controlled_activation_requires_every_gate():
    evidence = RolloutEvidence(
        research_approved=True,
        adapter_ready=True,
        offline_compatible=True,
        shadow_inference=True,
        signal_time_validated=True,
        provider_validated=True,
        shadow_performance=True,
        ceo_approved=True,
    )
    evidence.require(RolloutStage.CONTROLLED_ACTIVATION)
    with pytest.raises(ProductionContractError, match="ceo_approved"):
        RolloutEvidence(
            research_approved=True,
            adapter_ready=True,
            offline_compatible=True,
            shadow_inference=True,
            signal_time_validated=True,
            provider_validated=True,
            shadow_performance=True,
        ).require(RolloutStage.CEO_APPROVED)


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


def test_shadow_pipeline_has_no_live_side_effect_dependency():
    import src.football.production_pipeline as pipeline

    source = inspect.getsource(pipeline)
    for prohibited in ("requests", "pickle", "append_bets", "write_signals_json", "upload_signals_to_cloud"):
        assert prohibited not in source


def test_shadow_pipeline_uses_one_injected_bulk_request_and_no_bet_sink():
    config = _config(
        activation_mode=ActivationMode.SHADOW,
        signal_time=SIGNAL_TIME,
        provider_mapping=ProviderMapping(
            provider_name="fixture-provider",
            competition_id="sample-competition",
            sport_key="soccer_sample_league",
        ),
    )
    snapshot = MarketSnapshot(
        fixture_key=FIXTURE.fixture_key,
        captured_at=NOW,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="fixture-provider",
        odds={"home": 2.0},
        snapshot_id="snapshot-1",
    )

    class FakeIngestor:
        def fetch(self, _config):
            return [FIXTURE]

    class FakeProvider:
        name = "fixture-provider"

        def __init__(self):
            self.requests = []

        def fetch(self, request):
            self.requests.append(request)
            return [snapshot]

    class FakeFeatures:
        def build(self, _fixture, _snapshot):
            return {"form": 1.0}

    class FakeModel:
        def predict(self, model_input):
            assert model_input.signal_snapshot.kind is MarketSnapshotKind.SIGNAL_TIME
            return {"home": 0.55, "away": 0.45}

    class FakeDecider:
        def decide(self, _fixture, _prediction, _snapshot):
            return ShadowSignalArtifact(
                signal_id="signal-1",
                fixture_key=FIXTURE.fixture_key,
                league_code=FIXTURE.league_code,
                source="fixture-provider",
                model_adapter_id="model-under-review",
                activation_mode=ActivationMode.SHADOW,
                no_bet_flag=True,
                reason="shadow_only",
            )

    class FakeSink:
        def __init__(self):
            self.artifacts = []

        def write(self, artifact):
            self.artifacts.append(artifact)

    provider = FakeProvider()
    sink = FakeSink()
    result = run_shadow_pipeline(
        config,
        FakeIngestor(),
        provider,
        FakeFeatures(),
        FakeModel(),
        FakeDecider(),
        now=NOW,
        sink=sink,
    )
    assert len(provider.requests) == 1
    assert result.health.status == "ok"
    assert result.health.as_payload()["no_bet"] is True
    assert len(result.predictions) == len(result.signals) == len(sink.artifacts) == 1
    assert sink.artifacts[0].no_bet_flag is True


def test_shadow_pipeline_rejects_closing_provider_output():
    config = _config(
        activation_mode=ActivationMode.SHADOW,
        signal_time=SIGNAL_TIME,
        provider_mapping=ProviderMapping(
            provider_name="fixture-provider",
            competition_id="sample-competition",
            sport_key="soccer_sample_league",
        ),
    )
    closing = MarketSnapshot(
        fixture_key=FIXTURE.fixture_key,
        captured_at=NOW,
        kind=MarketSnapshotKind.CLOSING,
        source="fixture-provider",
        odds={"home": 2.0},
    )

    class FakeIngestor:
        def fetch(self, _config):
            return [FIXTURE]

    class FakeProvider:
        name = "fixture-provider"

        def fetch(self, _request):
            return [closing]

    with pytest.raises(ProductionContractError, match="closing snapshot"):
        run_shadow_pipeline(
            config,
            FakeIngestor(),
            FakeProvider(),
            object(),
            object(),
            object(),
            now=NOW,
        )


def test_shadow_pipeline_rejects_naive_run_timestamp_before_any_work():
    config = _config(
        activation_mode=ActivationMode.SHADOW,
        signal_time=SIGNAL_TIME,
        provider_mapping=ProviderMapping(
            provider_name="fixture-provider",
            competition_id="sample-competition",
            sport_key="soccer_sample_league",
        ),
    )

    with pytest.raises(ProductionContractError, match="timezone-aware"):
        run_shadow_pipeline(
            config,
            object(),
            object(),
            object(),
            object(),
            object(),
            now=NOW.replace(tzinfo=None),
        )
