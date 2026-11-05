"""Independent fail-closed tests for Champions League qualification."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.champions_league_qualification import (
    ApprovedInformationTimeBoundary,
    AggregateMode,
    CalibrationEvidence,
    CandidateArtifactIdentity,
    ChampionsLeagueDataset,
    ChampionsLeagueMatch,
    ChampionsLeagueModelMetadata,
    ContaminationPass,
    ChampionsLeagueQualificationError,
    ChampionsLeagueShadowEvidence,
    FeatureSchemaIdentity,
    FeatureProvenance,
    FinalEvaluationEvidence,
    OfflineQualificationReport,
    RollbackIdentity,
    RuntimeInputs,
    SourceProvenance,
    TeamAliasRegistry,
    TrainingPartitionIdentity,
    VenueSemantics,
    expected_format_era,
    normalize_competition_id,
    qualify_champions_league,
    validate_champions_league_qualification,
    validate_shadow_evidence,
    validate_temporal_partitions,
)


UTC = timezone.utc
DIGEST = "a" * 64


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2023, 7, 1, hour, tzinfo=UTC) + timedelta(days=day)


def aliases() -> TeamAliasRegistry:
    return TeamAliasRegistry(
        {
            "Real Madrid CF": "rm",
            "FC Bayern Munich": "fcb",
            "Paris Saint-Germain": "psg",
            "Inter Milan": "inter",
        }
    )


def partitions() -> dict[str, dict[str, datetime]]:
    return {
        "development": {"start_at": at(0), "end_at": at(31)},
        "calibration": {"start_at": at(31), "end_at": at(62)},
        "holdout": {"start_at": at(62), "end_at": at(93)},
    }


def match(
    fixture_id: str,
    kickoff: datetime,
    home: str,
    away: str,
    home_id: str,
    away_id: str,
    partition: str,
    *,
    aggregate_mode: str = AggregateMode.NONE.value,
    aggregate_tie_id: str | None = None,
    leg: int | None = None,
    aggregate_home_score_before: int | None = None,
    aggregate_away_score_before: int | None = None,
    aggregate_context_available_at: datetime | None = None,
    home_score: int = 1,
    away_score: int = 0,
    venue_semantics: str = VenueSemantics.HOME_AWAY.value,
    neutral_venue: bool | None = False,
) -> ChampionsLeagueMatch:
    return ChampionsLeagueMatch(
        fixture_id=fixture_id,
        observation_id=fixture_id,
        competition_id="ucl",
        season="2023/24",
        format_era="ucl_group_stage_2004_2024",
        stage="group stage",
        round="Group",
        home_team=home,
        away_team=away,
        home_team_id=home_id,
        away_team_id=away_id,
        kickoff_at=kickoff,
        aggregate_mode=aggregate_mode,
        aggregate_tie_id=aggregate_tie_id,
        leg=leg,
        aggregate_home_score_before=aggregate_home_score_before,
        aggregate_away_score_before=aggregate_away_score_before,
        aggregate_context_available_at=aggregate_context_available_at,
        venue_semantics=venue_semantics,
        neutral_venue=neutral_venue,
        venue_id="venue-1",
        result_status="final",
        home_score=home_score,
        away_score=away_score,
        result_available_at=kickoff + timedelta(hours=2),
        source_available_at=kickoff + timedelta(hours=3),
        partition=partition,
        source_record_id=f"source-{fixture_id}",
    )


def valid_rows() -> tuple[ChampionsLeagueMatch, ...]:
    return (
        match("fixture-1", at(9), "Real Madrid CF", "FC Bayern Munich", "rm", "fcb", "development"),
        match("fixture-2", at(40), "FC Bayern Munich", "Paris Saint-Germain", "fcb", "psg", "calibration"),
        match("fixture-3", at(71), "Paris Saint-Germain", "Real Madrid CF", "psg", "rm", "holdout"),
    )


def valid_dataset() -> ChampionsLeagueDataset:
    return ChampionsLeagueDataset.from_rows(
        dataset_id="ucl-qualification-dataset",
        rows=valid_rows(),
        partitions=partitions(),
        as_of=at(94),
        generated_at=at(95),
        aliases=aliases(),
    )


def valid_metadata(dataset: ChampionsLeagueDataset) -> ChampionsLeagueModelMetadata:
    return ChampionsLeagueModelMetadata.build(
        model_id="ucl-model",
        model_version="v1",
        artifact_sha=DIGEST,
        dataset_manifest_sha=dataset.manifest.manifest_sha,
        feature_names=("historical_form",),
        trained_at=at(96),
        input_market_snapshot_kinds=("signal_time",),
    )


def valid_feature(dataset: ChampionsLeagueDataset) -> FeatureProvenance:
    prediction_at = at(71, 9)
    return FeatureProvenance(
        feature_name="historical_form",
        fixture_id="fixture-3",
        partition="holdout",
        prediction_at=prediction_at,
        source_available_at=at(71, 8),
        source_kind="historical_form",
        source_digest=DIGEST,
        generated_at=at(71, 8),
        source_event_at=at(40),
        source_fixture_ids=("fixture-1", "fixture-2"),
        source_partitions=("development", "calibration"),
        market_snapshot_kind="signal_time",
    )


def valid_evidence(dataset: ChampionsLeagueDataset) -> ChampionsLeagueShadowEvidence:
    metadata = valid_metadata(dataset)
    prediction_at = at(71, 9)
    return ChampionsLeagueShadowEvidence.create(
        evidence_id="evidence-1",
        prediction_id="prediction-1",
        fixture_id="fixture-3",
        partition="holdout",
        prediction_at=prediction_at,
        generated_at=at(71, 9, ) + timedelta(minutes=5),
        model_id=metadata.model_id,
        model_artifact_sha=metadata.artifact_sha,
        dataset_manifest_sha=dataset.manifest.manifest_sha,
        feature_schema_hash=metadata.feature_schema_hash,
        features=(valid_feature(dataset),),
        input_market_snapshot_kind="signal_time",
    )


def assert_rejected(callable_object, message: str | None = None) -> None:
    with pytest.raises(ChampionsLeagueQualificationError, match=message):
        callable_object()


def test_valid_dataset_round_trips_and_qualification_is_independent() -> None:
    dataset = valid_dataset()
    metadata = valid_metadata(dataset)
    evidence = valid_evidence(dataset)

    report = qualify_champions_league(dataset, metadata, (evidence,))

    assert report.accepted is True
    assert report.dataset_rows == 3
    assert report.shadow_evidence_rows == 1
    validate_champions_league_qualification(dataset, metadata, (evidence,))
    round_tripped = ChampionsLeagueDataset.from_mapping(dataset.as_payload())
    assert round_tripped.manifest.manifest_sha == dataset.manifest.manifest_sha
    assert round_tripped.rows[0].as_payload(round_tripped.aliases)["competition_id"] == "uefa_champions_league"


def test_canonical_competition_and_historical_format_boundaries_are_deterministic() -> None:
    assert normalize_competition_id("UEFA Champions League") == "uefa_champions_league"
    assert normalize_competition_id("European Cup") == "uefa_champions_league"
    assert expected_format_era("1991-92") == "european_cup_pre_1992"
    assert expected_format_era("1992-93") == "champions_league_group_stage_1992_2003"
    assert expected_format_era("2003-04") == "champions_league_two_group_stages_2003_2004"
    assert expected_format_era("2004-05") == "champions_league_group_stage_2004_2024"
    assert expected_format_era("2024-25") == "champions_league_league_phase_2024_plus"

    row = valid_rows()[0]
    assert_rejected(lambda: replace(row, fixture_id="fixture id").validate(aliases()), "fixture_id")
    assert_rejected(lambda: replace(row, season="2023-25").validate(aliases()), "season")
    assert_rejected(lambda: replace(row, format_era="league_phase").validate(aliases()), "historical format")
    assert_rejected(lambda: replace(row, round="last sixteen").validate(aliases()), "round")
    assert_rejected(lambda: replace(row, home_team_id="not-rm").validate(aliases()), "team alias")


def test_stage_round_and_home_away_neutral_semantics_are_fail_closed() -> None:
    row = valid_rows()[0]
    assert_rejected(lambda: replace(row, stage="unknown").validate(aliases()), "stage")
    assert_rejected(
        lambda: replace(row, venue_semantics=VenueSemantics.NEUTRAL.value, neutral_venue=False).validate(aliases()),
        "neutral_venue",
    )
    final = replace(row, stage="final", round="final", venue_semantics="home_away", neutral_venue=False)
    assert_rejected(lambda: final.validate(aliases()), "neutral-venue")


def test_two_leg_aggregate_context_reconciles_prior_result_and_orientation() -> None:
    first = match(
        "tie-leg-1",
        at(9),
        "Real Madrid CF",
        "FC Bayern Munich",
        "rm",
        "fcb",
        "development",
        aggregate_mode=AggregateMode.TWO_LEG.value,
        aggregate_tie_id="tie-1",
        leg=1,
        home_score=1,
        away_score=0,
    )
    second = match(
        "tie-leg-2",
        at(19),
        "FC Bayern Munich",
        "Real Madrid CF",
        "fcb",
        "rm",
        "development",
        aggregate_mode=AggregateMode.TWO_LEG.value,
        aggregate_tie_id="tie-1",
        leg=2,
        aggregate_home_score_before=0,
        aggregate_away_score_before=1,
        aggregate_context_available_at=at(9, 16),
    )
    dataset = ChampionsLeagueDataset.from_rows(
        dataset_id="ucl-two-leg",
        rows=(first, second),
        partitions=partitions(),
        as_of=at(94),
        generated_at=at(95),
        aliases=aliases(),
    )
    assert len(dataset.rows) == 2

    conflicting = replace(second, aggregate_away_score_before=2)
    invalid = replace(dataset, rows=(first, conflicting))
    assert_rejected(invalid.validate, "aggregate scores conflict")
    assert_rejected(
        lambda: replace(second, aggregate_context_available_at=None).validate(aliases()),
        "aggregate context availability",
    )


def test_duplicate_rows_conflicting_results_and_mixed_partitions_are_rejected() -> None:
    dataset = valid_dataset()
    duplicate = replace(dataset, rows=dataset.rows + (dataset.rows[0],))
    assert_rejected(duplicate.validate, "duplicate observation")

    conflicting = replace(dataset.rows[0], observation_id="fixture-1-conflict", home_score=9)
    conflict_dataset = replace(dataset, rows=dataset.rows + (conflicting,))
    assert_rejected(conflict_dataset.validate, "conflicting observations")

    mixed = replace(dataset.rows[0], partition="holdout")
    mixed_dataset = replace(dataset, rows=(mixed,) + dataset.rows[1:])
    assert_rejected(mixed_dataset.validate, "wrong temporal partition")


def test_partition_windows_require_all_partitions_and_chronological_order() -> None:
    validate_temporal_partitions(partitions())
    reversed_windows = {
        "development": {"start_at": at(31), "end_at": at(62)},
        "calibration": {"start_at": at(0), "end_at": at(31)},
        "holdout": {"start_at": at(62), "end_at": at(93)},
    }
    assert_rejected(lambda: validate_temporal_partitions(reversed_windows), "chronological")
    missing = partitions()
    del missing["holdout"]
    assert_rejected(lambda: validate_temporal_partitions(missing), "all three")


def test_chronological_result_and_source_availability_are_required() -> None:
    row = valid_rows()[0]
    assert_rejected(
        lambda: replace(row, result_available_at=row.kickoff_at - timedelta(seconds=1)).validate(aliases()),
        "result availability",
    )
    assert_rejected(
        lambda: replace(
            row,
            source_available_at=row.result_available_at - timedelta(seconds=1),
        ).validate(aliases()),
        "source availability",
    )
    assert_rejected(
        lambda: replace(row, source_available_at=None).validate(aliases()),
        "source availability",
    )


def test_feature_provenance_rejects_future_source_target_and_closing_information() -> None:
    dataset = valid_dataset()
    feature = valid_feature(dataset)
    assert_rejected(
        lambda: replace(feature, source_available_at=feature.prediction_at + timedelta(seconds=1)).validate(dataset),
        "not available",
    )
    assert_rejected(
        lambda: replace(feature, source_fixture_ids=("fixture-3",)).validate(dataset),
        "future or target",
    )
    assert_rejected(
        lambda: replace(feature, source_event_at=feature.prediction_at + timedelta(seconds=1)).validate(dataset),
        "future",
    )
    assert_rejected(
        lambda: replace(feature, market_snapshot_kind="closing").validate(dataset),
        "closing market",
    )
    assert_rejected(
        lambda: replace(feature, source_kind="closing_market").validate(dataset),
        "closing-market",
    )


def test_manifest_model_metadata_and_shadow_evidence_hashes_and_inputs_are_bound() -> None:
    dataset = valid_dataset()
    metadata = valid_metadata(dataset)
    evidence = valid_evidence(dataset)

    assert_rejected(lambda: replace(dataset.manifest, manifest_sha="b" * 64).validate(), "digest changed")
    assert_rejected(lambda: replace(metadata, metadata_sha="b" * 64).validate(dataset.manifest), "digest changed")
    assert_rejected(
        lambda: ChampionsLeagueModelMetadata.build(
            model_id="ucl-model",
            model_version="v1",
            artifact_sha=DIGEST,
            dataset_manifest_sha=dataset.manifest.manifest_sha,
            feature_names=("closing_odds",),
            trained_at=at(96),
        ).validate(dataset.manifest),
        "future or closing",
    )
    assert_rejected(
        lambda: replace(evidence, input_market_snapshot_kind="closing").validate(dataset, metadata),
        "closing market",
    )
    assert_rejected(
        lambda: replace(evidence, generated_at=evidence.prediction_at - timedelta(minutes=1)).validate(dataset, metadata),
        "precedes prediction",
    )


def test_shadow_evidence_duplicate_observations_and_prediction_ids_are_rejected() -> None:
    dataset = valid_dataset()
    metadata = valid_metadata(dataset)
    evidence = valid_evidence(dataset)
    assert_rejected(lambda: validate_shadow_evidence((evidence, evidence), dataset, metadata), "duplicate")
    other = ChampionsLeagueShadowEvidence.create(
        evidence_id="evidence-2",
        prediction_id=evidence.prediction_id,
        fixture_id=evidence.fixture_id,
        partition=evidence.partition,
        prediction_at=evidence.prediction_at,
        generated_at=evidence.generated_at,
        model_id=evidence.model_id,
        model_artifact_sha=evidence.model_artifact_sha,
        dataset_manifest_sha=evidence.dataset_manifest_sha,
        feature_schema_hash=evidence.feature_schema_hash,
        features=evidence.features,
        input_market_snapshot_kind=evidence.input_market_snapshot_kind,
    )
    assert_rejected(lambda: validate_shadow_evidence((evidence, other), dataset, metadata), "duplicate")


def test_malformed_timestamps_fail_closed_during_construction() -> None:
    row = valid_rows()[0]
    assert_rejected(
        lambda: replace(row, kickoff_at=datetime(2023, 7, 10, 12)).validate(aliases()),
        "timezone-aware",
    )
    assert_rejected(
        lambda: FeatureProvenance(
            feature_name="historical_form",
            fixture_id="fixture-3",
            partition="holdout",
            prediction_at=datetime(2023, 9, 10, 9),
            source_available_at=at(71, 8),
            source_kind="historical_form",
            source_digest=DIGEST,
        ),
        "timezone-aware",
    )


def offline_bundle() -> dict[str, object]:
    candidate = CandidateArtifactIdentity.build(
        candidate_id="ucl-candidate-1",
        artifact_id="ucl-artifact-1",
        artifact_sha="b" * 64,
        version="v1",
    )
    schema = FeatureSchemaIdentity.build(
        schema_id="ucl-features",
        schema_version="v1",
        feature_names=("historical_form",),
    )
    boundary = ApprovedInformationTimeBoundary.build(
        boundary_id="ucl-boundary-1",
        cutoff_at=at(100),
        approved_by="research-governance",
        approved_at=at(99),
        policy="pre_kickoff_only",
    )
    training = TrainingPartitionIdentity.build(
        partition_id="ucl-train-1",
        dataset_id="ucl-dataset-1",
        start_at=at(0),
        end_at=at(30),
        row_count=100,
        boundary_id=boundary.boundary_id,
    )
    sources = (
        SourceProvenance.build(
            provenance_id="src-training",
            source_name="ucl-training",
            source_sha="c" * 64,
            dataset_id="ucl-dataset-1",
            available_at=at(20),
            retrieved_at=at(21),
            row_count=100,
            boundary_id=boundary.boundary_id,
            role="training",
        ),
        SourceProvenance.build(
            provenance_id="src-calibration",
            source_name="ucl-calibration",
            source_sha="d" * 64,
            dataset_id="ucl-dataset-1",
            available_at=at(50),
            retrieved_at=at(51),
            row_count=40,
            boundary_id=boundary.boundary_id,
            role="calibration",
        ),
        SourceProvenance.build(
            provenance_id="src-final",
            source_name="ucl-final",
            source_sha="e" * 64,
            dataset_id="ucl-dataset-1",
            available_at=at(80),
            retrieved_at=at(81),
            row_count=40,
            boundary_id=boundary.boundary_id,
            role="final_evaluation",
        ),
        SourceProvenance.build(
            provenance_id="src-runtime",
            source_name="ucl-runtime",
            source_sha="f" * 64,
            dataset_id="ucl-dataset-1",
            available_at=at(80),
            retrieved_at=at(81),
            row_count=1,
            boundary_id=boundary.boundary_id,
            role="runtime",
        ),
    )
    calibration = CalibrationEvidence.build(
        evidence_id="calibration-1",
        partition_id="ucl-calibration-1",
        partition_sha="1" * 64,
        candidate_artifact_id=candidate.artifact_id,
        artifact_sha=candidate.artifact_sha,
        feature_schema_sha=schema.schema_sha,
        boundary_id=boundary.boundary_id,
        provenance_id="src-calibration",
        metric_name="brier_score",
        metric_value=0.18,
        sample_count=40,
    )
    final = FinalEvaluationEvidence.build(
        evidence_id="final-1",
        evaluation_id="evaluation-1",
        partition_id="ucl-holdout-1",
        partition_sha="2" * 64,
        candidate_artifact_id=candidate.artifact_id,
        artifact_sha=candidate.artifact_sha,
        feature_schema_sha=schema.schema_sha,
        boundary_id=boundary.boundary_id,
        provenance_id="src-final",
        metric_name="log_loss",
        metric_value=0.42,
        sample_count=40,
        result_source_id="settled-results-1",
        evaluated_at=at(90),
    )
    contamination = ContaminationPass.build(
        check_id="contamination-1",
        checked_at=at(95),
        method="fixture-and-source-digest-intersection-v1",
        training_source_sha="c" * 64,
        calibration_source_sha="d" * 64,
        final_evaluation_source_sha="e" * 64,
    )
    runtime = RuntimeInputs.build(
        runtime_id="runtime-1",
        runtime_version="v1",
        candidate_artifact_id=candidate.artifact_id,
        artifact_sha=candidate.artifact_sha,
        feature_schema_sha=schema.schema_sha,
        boundary_id=boundary.boundary_id,
        input_names=schema.feature_names,
        config_sha="9" * 64,
        provenance_id="src-runtime",
    )
    rollback = RollbackIdentity.build(
        rollback_id="rollback-1",
        target_artifact_id="ucl-artifact-previous",
        target_artifact_sha="a" * 64,
        target_version="v0",
        config_sha="8" * 64,
        tested_at=at(96),
    )
    return {
        "report_id": "ucl-offline-report-1",
        "candidate_artifact_identity": candidate,
        "feature_schema_identity": schema,
        "approved_information_time_boundary": boundary,
        "training_partition_identity": training,
        "calibration_evidence": (calibration,),
        "final_evaluation_evidence": (final,),
        "source_provenance": sources,
        "contamination_pass": contamination,
        "runtime_inputs": runtime,
        "rollback_identity": rollback,
    }


def test_offline_qualification_requires_and_binds_every_evidence_family() -> None:
    from src.football.champions_league_qualification import qualify_offline_candidate

    report = qualify_offline_candidate(**offline_bundle())
    assert report.accepted is True
    assert report.errors == ()
    assert validate_offline_report(report).report_sha == report.report_sha
    payload = report.as_payload()
    assert set(
        (
            "candidate_artifact_identity",
            "feature_schema_identity",
            "approved_information_time_boundary",
            "training_partition_identity",
            "calibration_evidence",
            "final_evaluation_evidence",
            "source_provenance",
            "contamination_pass",
            "runtime_inputs",
            "rollback_identity",
        )
    ) <= set(payload)


def validate_offline_report(report: OfflineQualificationReport) -> OfflineQualificationReport:
    from src.football.champions_league_qualification import validate_offline_qualification

    return validate_offline_qualification(report)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("calibration_evidence", (), "calibration evidence"),
        ("final_evaluation_evidence", (), "final-evaluation"),
        ("source_provenance", (), "source provenance"),
        ("contamination_pass", None, "contamination pass"),
        ("runtime_inputs", None, "runtime inputs"),
        ("rollback_identity", None, "rollback identity"),
    ),
)
def test_offline_qualification_missing_required_evidence_rejects(
    field: str, replacement: object, message: str
) -> None:
    from src.football.champions_league_qualification import qualify_offline_candidate

    bundle = offline_bundle()
    bundle[field] = replacement
    report = qualify_offline_candidate(**bundle)
    assert report.accepted is False
    assert message in report.errors[0]


def test_offline_qualification_rejects_identity_mismatch_future_inputs_and_contamination() -> None:
    from src.football.champions_league_qualification import qualify_offline_candidate

    bundle = offline_bundle()
    candidate = bundle["candidate_artifact_identity"]
    assert isinstance(candidate, CandidateArtifactIdentity)
    bundle["candidate_artifact_identity"] = replace(candidate, artifact_sha="9" * 64)
    assert "identity digest changed" in qualify_offline_candidate(**bundle).errors[0]

    bundle = offline_bundle()
    runtime = bundle["runtime_inputs"]
    assert isinstance(runtime, RuntimeInputs)
    bundle["runtime_inputs"] = replace(runtime, input_names=("closing_odds",))
    assert "future or closing-market" in qualify_offline_candidate(**bundle).errors[0]

    bundle = offline_bundle()
    contamination = bundle["contamination_pass"]
    assert isinstance(contamination, ContaminationPass)
    bundle["contamination_pass"] = ContaminationPass.build(
        check_id=contamination.check_id,
        checked_at=contamination.checked_at,
        method=contamination.method,
        training_source_sha=contamination.training_source_sha,
        calibration_source_sha=contamination.calibration_source_sha,
        final_evaluation_source_sha=contamination.final_evaluation_source_sha,
        overlap_count=1,
    )
    report = qualify_offline_candidate(**bundle)
    assert report.accepted is False
    assert "contamination overlap" in report.errors[0]


def test_offline_report_round_trip_and_tampering_are_fail_closed() -> None:
    from src.football.champions_league_qualification import (
        qualify_offline_candidate,
    )

    report = qualify_offline_candidate(**offline_bundle())
    restored = OfflineQualificationReport.from_mapping(report.as_payload())
    assert restored.as_payload() == report.as_payload()
    tampered = dict(report.as_payload())
    tampered["accepted"] = False
    tampered["errors"] = ["manual override"]
    with pytest.raises(ChampionsLeagueQualificationError, match="digest changed"):
        validate_offline_report(OfflineQualificationReport.from_mapping(tampered))
