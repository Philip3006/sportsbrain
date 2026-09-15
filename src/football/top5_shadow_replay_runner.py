"""Deterministic offline replay orchestration and coverage reporting."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.football.production_contracts import SignalTimeContract
from src.football.top5_research_binding import M5_CANDIDATE_ID
from src.football.top5_shadow_integration import run_offline_top5_shadow
from src.football.top5_shadow_replay import (
    TOP5_REPLAY_LEAGUES,
    HistoricalReplayInput,
    OfflineReplayArchive,
    OfflineReplayError,
    ReplayPredictionArtifact,
    ReplayResultStatus,
)


@dataclass(frozen=True)
class ReplayCoverage:
    league_code: str
    candidate_historical_fixtures: int
    legitimate_replay_inputs: int
    predictions_produced: int
    predictions_rejected: int
    results_attached: int
    unresolved_results: int
    closing_benchmarks_attached: int
    performance_eligible_observations: int

    def as_payload(self) -> dict[str, object]:
        return {
            "league": self.league_code,
            "candidate_historical_fixtures": self.candidate_historical_fixtures,
            "legitimate_replay_inputs": self.legitimate_replay_inputs,
            "predictions_produced": self.predictions_produced,
            "predictions_rejected": self.predictions_rejected,
            "results_attached": self.results_attached,
            "unresolved_results": self.unresolved_results,
            "closing_benchmarks_attached": self.closing_benchmarks_attached,
            "performance_eligible_observations": self.performance_eligible_observations,
        }


@dataclass
class OfflineReplayRun:
    archive: OfflineReplayArchive
    candidate_counts: Mapping[str, int]
    legitimate_counts: Mapping[str, int]

    @property
    def coverage(self) -> tuple[ReplayCoverage, ...]:
        produced = Counter(
            prediction.league_code for prediction in self.archive.predictions.values()
        )
        return tuple(
            ReplayCoverage(
                league_code=league,
                candidate_historical_fixtures=self.candidate_counts.get(league, 0),
                legitimate_replay_inputs=self.legitimate_counts.get(league, 0),
                predictions_produced=produced[league],
                predictions_rejected=self.candidate_counts.get(league, 0) - produced[league],
                results_attached=sum(
                    prediction.league_code == league
                    for prediction in self.archive.results.values()
                ),
                unresolved_results=sum(
                    self.archive.predictions[prediction_id].league_code == league
                    and ReplayResultStatus(attachment.status) is not ReplayResultStatus.FINAL
                    for prediction_id, attachment in self.archive.results.items()
                ),
                closing_benchmarks_attached=sum(
                    prediction.league_code == league
                    for prediction in self.archive.closings.values()
                ),
                performance_eligible_observations=sum(
                    prediction.league_code == league
                    and prediction_id in self.archive.results
                    and ReplayResultStatus(self.archive.results[prediction_id].status)
                    is ReplayResultStatus.FINAL
                    for prediction_id, prediction in self.archive.predictions.items()
                ),
            )
            for league in TOP5_REPLAY_LEAGUES
        )

    def to_builder2_bundle(self):
        from src.football.top5_shadow_replay_evidence import build_builder2_bundle

        return build_builder2_bundle(self.archive)

    def performance_payload(self) -> dict[str, object]:
        from src.football.top5_shadow_replay_evidence import performance_payload

        return performance_payload(self)

    def as_payload(self) -> dict[str, object]:
        from src.football.top5_shadow_replay_evidence import run_payload

        return run_payload(self)


def run_offline_replay(
    inputs: Sequence[HistoricalReplayInput],
    *,
    signal_time: SignalTimeContract,
    integration_sha: str,
) -> OfflineReplayRun:
    """Produce deterministic M5 prediction artifacts from static DEV inputs."""

    if not inputs:
        raise OfflineReplayError("offline replay requires at least one historical candidate")
    if len(integration_sha) != 40 or any(
        char not in "0123456789abcdef" for char in integration_sha.lower()
    ):
        raise OfflineReplayError("replay requires an exact integration SHA")
    signal_time.validate()
    archive = OfflineReplayArchive()
    candidates = Counter(input_data.fixture.league_code for input_data in inputs)
    legitimate = Counter()
    produced = Counter()
    seen: set[tuple[str, str]] = set()
    ordered_inputs = sorted(
        inputs, key=lambda item: (item.fixture.league_code, item.fixture.fixture_key)
    )
    for input_data in ordered_inputs:
        input_data.validate()
        identity = (input_data.fixture.league_code, input_data.fixture.fixture_key)
        if identity in seen:
            raise OfflineReplayError("duplicate historical fixture identity")
        seen.add(identity)
        if input_data.signal_snapshot is None:
            continue
        legitimate[input_data.fixture.league_code] += 1
        result = run_offline_top5_shadow(
            input_data.fixture.league_code,
            (input_data.fixture,),
            (input_data.signal_snapshot,),
            signal_time=signal_time,
            now=input_data.replayed_at,
            integration_sha=integration_sha,
            candidate_ids=(M5_CANDIDATE_ID,),
        )
        if not result.predictions:
            continue
        prediction = result.predictions[0]
        replay_prediction = ReplayPredictionArtifact.create(
            prediction.prediction_id,
            input_data,
            signal_time,
            prediction.probabilities,
            integration_sha,
            prediction.snapshot_id,
        )
        archive.add_prediction(replay_prediction)
        produced[input_data.fixture.league_code] += 1
    return OfflineReplayRun(archive, dict(candidates), dict(legitimate))
