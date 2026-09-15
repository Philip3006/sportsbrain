"""Builder-2 evidence projection for the offline replay lifecycle."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from src.football.top5_research_binding import M5_CANDIDATE_ID
from src.football.top5_shadow_replay import (
    ENGINEERING_VALIDATION_MARKER,
    OFFLINE_REPLAY_MARKER,
    OfflineReplayArchive,
    OfflineReplayError,
    ReplayClosingAttachment,
    ReplayPredictionArtifact,
    ReplayResultAttachment,
    ReplayResultStatus,
    _stable_digest,
)
from src.football.top5_shadow_replay_runner import OfflineReplayRun
from src.football.top5_shadow_validation import (
    ClosingBenchmarkEvidence,
    EvidenceProvenance,
    HealthEvidence,
    PredictionEvidence,
    QuotaCostEvidence,
    ResultAttachment,
    SafetyAssertions,
    ShadowEvidenceBundle,
    ShadowObservationEvidence,
    SignalTimeEvidence,
    calculate_performance_metrics,
    validate_coverage,
)


def _provenance(
    kind: str,
    prediction: ReplayPredictionArtifact,
    artifact_id: str,
    fixture_key: str | None,
    artifact_sha: str | None = None,
) -> EvidenceProvenance:
    return EvidenceProvenance(
        evidence_id=f"offline:{kind}:{artifact_id}",
        artifact_id=artifact_id,
        artifact_sha=artifact_sha
        or (
            prediction.artifact_sha
            if kind == "prediction"
            else _stable_digest({"kind": kind, "artifact_id": artifact_id})
        ),
        source_sha=prediction.source_sha,
        research_sha=prediction.research_sha,
        league_code=prediction.league_code,
        candidate_id=M5_CANDIDATE_ID,
        model_identity=M5_CANDIDATE_ID,
        generated_at=prediction.replayed_at,
        fixture_key=fixture_key,
    )


def _observation_for(
    prediction: ReplayPredictionArtifact, resolved: bool
) -> ShadowObservationEvidence:
    return ShadowObservationEvidence(
        provenance=_provenance(
            "observation",
            prediction,
            f"observation:{prediction.prediction_id}",
            prediction.fixture_key,
        ),
        discovered=True,
        eligible=True,
        valid_odds=True,
        prediction_id=prediction.prediction_id,
        provider_covered=False,
        result_resolved=resolved,
        no_bet=True,
        publication_enabled=False,
    )


def _prediction_evidence(prediction: ReplayPredictionArtifact) -> PredictionEvidence:
    return PredictionEvidence(
        provenance=_provenance(
            "prediction", prediction, prediction.prediction_id, prediction.fixture_key
        ),
        prediction_id=prediction.prediction_id,
        signal_snapshot_id=prediction.snapshot_id,
        input_snapshot_kinds=("signal_time",),
        probabilities=prediction.probabilities,
        market_probabilities=prediction.market_probabilities,
        no_bet=True,
        publication_enabled=False,
    )


def _signal_evidence(prediction: ReplayPredictionArtifact) -> SignalTimeEvidence:
    age = (prediction.replayed_at - prediction.source_timestamp).total_seconds()
    return SignalTimeEvidence(
        provenance=_provenance(
            "signal",
            prediction,
            f"signal:{prediction.prediction_id}",
            prediction.fixture_key,
        ),
        candidate_name=f"{OFFLINE_REPLAY_MARKER}:{M5_CANDIDATE_ID}",
        eligible=True,
        odds_age_seconds=age,
        maximum_odds_age_seconds=prediction.signal_time_maximum_age_seconds,
        request_load=0.0,
        fallback_used=False,
        stale_rejected=False,
        latency_ms=0.0,
        quota_cost_units=0.0,
        operational_complexity=0.0,
    )


def _health_evidence(prediction: ReplayPredictionArtifact) -> HealthEvidence:
    return HealthEvidence(
        provenance=_provenance(
            "health", prediction, f"health:{prediction.league_code}", None
        ),
        provider_health="historical_input_only",
        inference_health="offline_replay",
        publisher_health="disabled",
        result_source_health="offline_attachment_only",
        activation_state="shadow",
        registered=False,
        no_bet=True,
        publication_enabled=False,
    )


def _result_evidence(
    prediction: ReplayPredictionArtifact, result: ReplayResultAttachment
) -> ResultAttachment:
    return ResultAttachment(
        provenance=_provenance(
            "result",
            prediction,
            f"result:{prediction.prediction_id}",
            prediction.fixture_key,
            result.attachment_sha,
        ),
        prediction_id=prediction.prediction_id,
        actual_outcome=result.actual_outcome,
        resolved=ReplayResultStatus(result.status) is ReplayResultStatus.FINAL,
        result_source=result.result_source,
        result_delay_seconds=(result.attached_at - prediction.replayed_at).total_seconds(),
    )


def _market_probabilities_from_odds(odds: Mapping[str, float]) -> Mapping[str, float]:
    inverse = {name: 1.0 / float(odds[name]) for name in ("away", "draw", "home")}
    total = sum(inverse.values())
    return {name: inverse[name] / total for name in inverse}


def _closing_evidence(
    prediction: ReplayPredictionArtifact, closing: ReplayClosingAttachment
) -> ClosingBenchmarkEvidence:
    return ClosingBenchmarkEvidence(
        provenance=_provenance(
            "closing",
            prediction,
            f"closing:{prediction.prediction_id}",
            prediction.fixture_key,
            closing.attachment_sha,
        ),
        prediction_id=prediction.prediction_id,
        signal_snapshot_id=prediction.snapshot_id,
        closing_snapshot_id=closing.attachment_sha,
        signal_probabilities=prediction.market_probabilities,
        closing_probabilities=_market_probabilities_from_odds(closing.odds),
        used_for_prediction=False,
    )


def build_builder2_bundle(archive: OfflineReplayArchive) -> ShadowEvidenceBundle:
    if not archive.predictions:
        raise OfflineReplayError("cannot build evidence from an empty replay archive")
    ordered = tuple(sorted(archive.predictions.values(), key=lambda item: item.prediction_id))
    start = min(prediction.replayed_at for prediction in ordered)
    end = max(
        [prediction.replayed_at for prediction in ordered]
        + [result.attached_at for result in archive.results.values()]
        + [closing.attached_at for closing in archive.closings.values()]
    )
    if end <= start:
        end += timedelta(microseconds=1)
    health_predictions = {}
    for prediction in ordered:
        health_predictions.setdefault(prediction.league_code, prediction)
    bundle = ShadowEvidenceBundle(
        evidence_window_start=start,
        evidence_window_end=end,
        safety=SafetyAssertions(True, False, False, False, False, False, False),
        observations=tuple(
            _observation_for(prediction, prediction.prediction_id in archive.results)
            for prediction in ordered
        ),
        predictions=tuple(_prediction_evidence(prediction) for prediction in ordered),
        signal_time_evidence=tuple(_signal_evidence(prediction) for prediction in ordered),
        quota_cost_evidence=(
            QuotaCostEvidence(
                _provenance("quota", ordered[0], f"quota:{ordered[0].prediction_id}", None),
                "offline_replay",
                len(ordered),
                len(ordered),
                0,
                0,
                0,
                0,
                0,
                0,
                0.0,
            ),
        ),
        health_evidence=tuple(_health_evidence(prediction) for prediction in health_predictions.values()),
        result_attachments=tuple(
            _result_evidence(prediction, archive.results[prediction.prediction_id])
            for prediction in ordered
            if prediction.prediction_id in archive.results
        ),
        closing_benchmark_evidence=tuple(
            _closing_evidence(prediction, archive.closings[prediction.prediction_id])
            for prediction in ordered
            if prediction.prediction_id in archive.closings
        ),
    )
    bundle.validate()
    return bundle


def performance_payload(run: OfflineReplayRun) -> dict[str, object]:
    metrics = calculate_performance_metrics(build_builder2_bundle(run.archive)).as_payload()
    return {
        **metrics,
        "offline_replay": True,
        "engineering_validation_only": True,
        "fully_attached_observation_count": metrics["resolved_result_count"],
        "metric_input": "resolved_results_only; closing_benchmark_separate",
        "unresolved_result_count": sum(item.unresolved_results for item in run.coverage),
        "rejected_count": sum(item.predictions_rejected for item in run.coverage),
        "profitability_claim": False,
        "significance_claim": False,
        "deployable_edge_claim": False,
    }


def run_payload(run: OfflineReplayRun) -> dict[str, object]:
    return {
        "marker": OFFLINE_REPLAY_MARKER,
        "status": ENGINEERING_VALIDATION_MARKER,
        "archive": run.archive.as_payload(),
        "coverage": [item.as_payload() for item in run.coverage],
        "performance": performance_payload(run),
        "builder2_contract": build_builder2_bundle(run.archive).as_payload(),
    }


def archive_coverage(run: OfflineReplayRun) -> Mapping[str, object]:
    """Return an honest per-league report without invoking a CEO gate."""

    builder2_coverage = validate_coverage(build_builder2_bundle(run.archive)).as_payload()
    return {
        "offline_replay": True,
        "engineering_validation_only": True,
        "by_league": [item.as_payload() for item in run.coverage],
        "builder2_coverage_compatibility": builder2_coverage,
    }
