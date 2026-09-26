"""Typed adapter from canonical Top-5 lifecycle objects to the public read model.

This module projects validated domain objects only. It does not create lifecycle
state, infer market values, or grant activation/publication authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite

from src.football.top5_lifecycle_public import project_top5_lifecycle
from src.football.top5_signal_lifecycle import (
    EXPECTED_PROVIDER_IDENTITY,
    Top5SignalLifecycle,
    Top5SignalLifecycleVersion,
)

_EXPECTED_OUTCOMES = frozenset({"home", "draw", "away"})
_PROVENANCE_FIELDS = ("source_sha", "research_sha", "model_artifact_hash")


class Top5SignalLifecyclePublicAdapterError(ValueError):
    """A lifecycle set does not bind exactly to its prediction artifact."""


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise Top5SignalLifecyclePublicAdapterError(f"{field} must be non-empty text")
    return value


def _instant(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Top5SignalLifecyclePublicAdapterError(
                f"{field} must be an ISO timestamp"
            ) from exc
    else:
        raise Top5SignalLifecyclePublicAdapterError(
            f"{field} must be a timezone-aware timestamp"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5SignalLifecyclePublicAdapterError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime | str, field: str) -> str:
    _instant(value, field)
    return value.isoformat() if isinstance(value, datetime) else value


def _probabilities(value: Mapping[str, object]) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise Top5SignalLifecyclePublicAdapterError(
            "prediction probabilities must be a non-empty mapping"
        )
    result: dict[str, float] = {}
    for raw_outcome, raw_probability in value.items():
        outcome = _required_text(raw_outcome, "prediction outcome")
        if outcome not in _EXPECTED_OUTCOMES:
            raise Top5SignalLifecyclePublicAdapterError(
                f"unsupported prediction outcome identity: {outcome}"
            )
        if isinstance(raw_probability, bool) or not isinstance(
            raw_probability, (int, float)
        ):
            raise Top5SignalLifecyclePublicAdapterError(
                f"prediction probability for {outcome} must be numeric"
            )
        probability = float(raw_probability)
        if not isfinite(probability) or not 0 <= probability <= 1:
            raise Top5SignalLifecyclePublicAdapterError(
                f"prediction probability for {outcome} is invalid"
            )
        result[outcome] = probability
    return result


def _validate_version_window(
    lifecycle: Top5SignalLifecycle, version: Top5SignalLifecycleVersion
) -> None:
    signal_time = lifecycle.contract.as_signal_time_contract(version.stage)
    if not signal_time.accepts(
        version.kickoff, version.odds_captured_at, version.prediction_generated_at
    ):
        raise Top5SignalLifecyclePublicAdapterError(
            f"{version.stage.value} lifecycle has stale or out-of-window odds"
        )


def project_top5_signal_lifecycles(
    lifecycles: Sequence[Top5SignalLifecycle],
    *,
    prediction_probabilities: Mapping[str, object],
    fixture_identity: str,
    league_identity: str,
    candidate_identity: str,
    model_identity: str,
    provider_authority: str,
    prediction_timestamp: datetime | str,
    signal_timestamp: datetime | str,
    snapshot_id: str,
    provenance: Mapping[str, object],
    market_identity: str | None = None,
) -> dict[str, dict[str, object]]:
    """Project the complete per-outcome lifecycle set for one prediction.

    Output keys are the canonical prediction outcomes and output values are
    validated by ``project_top5_lifecycle`` before they leave this adapter.
    The public snapshot binding is the current version's snapshot, while the
    current timestamp is the exact timestamp representation carried by the
    surrounding prediction artifact.
    """
    fixture = _required_text(fixture_identity, "fixture_identity")
    league = _required_text(league_identity, "league_identity")
    candidate = _required_text(candidate_identity, "candidate_identity")
    model = _required_text(model_identity, "model_identity")
    provider = _required_text(provider_authority, "provider_authority")
    snapshot = _required_text(snapshot_id, "snapshot_id")
    if provider != EXPECTED_PROVIDER_IDENTITY:
        raise Top5SignalLifecyclePublicAdapterError(
            "public lifecycle provider authority must remain the_odds_api"
        )

    probabilities = _probabilities(prediction_probabilities)
    if not isinstance(lifecycles, Sequence) or isinstance(
        lifecycles, (str, bytes, bytearray)
    ):
        raise Top5SignalLifecyclePublicAdapterError(
            "signal lifecycles must be a sequence of canonical lifecycle objects"
        )
    if len(lifecycles) != len(probabilities):
        raise Top5SignalLifecyclePublicAdapterError(
            "lifecycle outcomes must cover every prediction probability exactly once"
        )

    expected_prediction_time = _instant(prediction_timestamp, "prediction_timestamp")
    expected_signal_time = _instant(signal_timestamp, "signal_timestamp")
    prediction_timestamp_text = _timestamp_text(
        prediction_timestamp, "prediction_timestamp"
    )
    expected_provenance: dict[str, str] = {}
    if not isinstance(provenance, Mapping):
        raise Top5SignalLifecyclePublicAdapterError("prediction provenance is required")
    for field in _PROVENANCE_FIELDS:
        expected_provenance[field] = _required_text(
            provenance.get(field), f"provenance.{field}"
        )
    expected_provenance["snapshot_id"] = snapshot

    by_outcome: dict[str, Top5SignalLifecycle] = {}
    market_ids: set[str] = set()
    for lifecycle in lifecycles:
        if not isinstance(lifecycle, Top5SignalLifecycle):
            raise Top5SignalLifecyclePublicAdapterError(
                "only canonical Top5SignalLifecycle objects are accepted"
            )
        try:
            lifecycle.validate()
        except (TypeError, ValueError) as exc:
            raise Top5SignalLifecyclePublicAdapterError(
                f"canonical lifecycle validation failed: {exc}"
            ) from exc
        initial = lifecycle.initial_version
        current = lifecycle.current_version
        outcome = initial.outcome_id
        if outcome in by_outcome:
            raise Top5SignalLifecyclePublicAdapterError(
                f"duplicate lifecycle outcome identity: {outcome}"
            )
        by_outcome[outcome] = lifecycle
        market_ids.add(initial.market_id)

        if outcome not in probabilities:
            raise Top5SignalLifecyclePublicAdapterError(
                f"extra lifecycle outcome identity: {outcome}"
            )
        if (
            initial.fixture_key != fixture
            or initial.league_code != league
            or initial.model_identity != model
            or initial.candidate_id != candidate
        ):
            raise Top5SignalLifecyclePublicAdapterError(
                "lifecycle fixture/league/model/candidate binding mismatch"
            )
        if (
            initial.provider_identity != provider
            or current.provider_identity != provider
        ):
            raise Top5SignalLifecyclePublicAdapterError(
                "lifecycle provider binding mismatch"
            )
        if market_identity is not None and initial.market_id != market_identity:
            raise Top5SignalLifecyclePublicAdapterError(
                "lifecycle market identity mismatch"
            )
        if current.snapshot_id != snapshot:
            raise Top5SignalLifecyclePublicAdapterError(
                "current lifecycle snapshot_id disagrees with prediction artifact"
            )
        if current.prediction_generated_at != expected_prediction_time:
            raise Top5SignalLifecyclePublicAdapterError(
                "current lifecycle prediction timestamp disagrees with prediction artifact"
            )
        if current.odds_captured_at != expected_signal_time:
            raise Top5SignalLifecyclePublicAdapterError(
                "current lifecycle odds timestamp disagrees with signal timestamp"
            )
        for version in lifecycle.versions:
            _validate_version_window(lifecycle, version)
            for field, expected in expected_provenance.items():
                actual = getattr(version, field, None)
                if field == "snapshot_id" and version is not current:
                    continue
                if field != "snapshot_id" and actual != expected:
                    raise Top5SignalLifecyclePublicAdapterError(
                        f"lifecycle provenance binding mismatch: {field}"
                    )
        if current.outcome_id != outcome or outcome not in current.probabilities:
            raise Top5SignalLifecyclePublicAdapterError(
                "current lifecycle selected outcome is missing"
            )
        if current.probabilities[outcome] != probabilities[outcome]:
            raise Top5SignalLifecyclePublicAdapterError(
                f"current lifecycle probability disagrees for {outcome}"
            )

    if set(by_outcome) != set(probabilities):
        raise Top5SignalLifecyclePublicAdapterError(
            "lifecycle outcomes do not exactly match prediction probabilities"
        )
    if len(market_ids) != 1:
        raise Top5SignalLifecyclePublicAdapterError(
            "lifecycle outcomes do not share one canonical market identity"
        )

    result: dict[str, dict[str, object]] = {}
    for outcome in sorted(probabilities):
        lifecycle = by_outcome[outcome]
        initial = lifecycle.initial_version
        current = lifecycle.current_version
        initial_probability = initial.probabilities.get(outcome)
        current_probability = current.probabilities.get(outcome)
        if initial_probability is None or current_probability is None:
            raise Top5SignalLifecyclePublicAdapterError(
                f"lifecycle probability history is incomplete for {outcome}"
            )
        public: dict[str, object] = {
            "schema_version": "top5-lifecycle-public-v1",
            "lifecycle_id": lifecycle.lifecycle_id,
            "initial_record_id": (
                f"top5-lifecycle-initial-v1:{initial.version_digest}"
            ),
            "lifecycle_version": current.version_number,
            "lifecycle_stage": current.stage.value,
            "initial_generated_at": initial.prediction_generated_at.isoformat(),
            "current_generated_at": prediction_timestamp_text,
            "initial_probability": initial_probability,
            "current_probability": current_probability,
            "fixture_identity": initial.fixture_key,
            "model_identity": initial.model_identity,
            "provenance_binding": dict(expected_provenance),
        }
        if len(lifecycle.versions) == 2:
            public["probability_delta"] = current_probability - initial_probability
            if current.classification is None:
                raise Top5SignalLifecyclePublicAdapterError(
                    "refined lifecycle classification is missing"
                )
            public["refinement_classification"] = current.classification.value

        initial_market = initial.implied_probabilities.get(outcome)
        current_market = current.implied_probabilities.get(outcome)
        if initial_market is not None and current_market is not None:
            public["initial_market_probability"] = initial_market
            public["current_market_probability"] = current_market
            initial_edge = initial.edges.get(outcome)
            current_edge = current.edges.get(outcome)
            if initial_edge is not None and current_edge is not None:
                public["initial_edge_pp"] = initial_edge
                public["current_edge_pp"] = current_edge
                public["edge_delta_pp"] = current_edge - initial_edge

        result[outcome] = project_top5_lifecycle(
            public,
            fixture_identity=fixture,
            model_identity=model,
            provenance=expected_provenance,
        )
    return result
