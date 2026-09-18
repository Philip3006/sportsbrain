"""Deterministic replay engine for the offline Champions League shadow seam."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from types import MappingProxyType

from src.football.provider_cascade.champions_league_shadow import (
    CL_SHADOW_CONTRACT_VERSION,
    CLEventState,
    CLFixture,
    CLReplayError,
    CLRejectionReason,
    CLShadowContractError,
    CLShadowObservation,
    CLShadowPolicy,
    _digest,
    _team,
)


@dataclass(frozen=True)
class CLObservationDecision:
    """Replay decision with expected data-quality rejection reasons."""

    observation: CLShadowObservation
    accepted: bool
    reasons: tuple[CLRejectionReason, ...] = ()


def evaluate_observation(
    observation: CLShadowObservation,
    expected: CLFixture | None,
    policy: CLShadowPolicy,
) -> CLObservationDecision:
    """Evaluate one replay record without raising for expected data rejection."""

    policy.validate()
    reasons: list[CLRejectionReason] = []

    def reject(reason: CLRejectionReason) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if expected is None:
        reject(CLRejectionReason.UNKNOWN_FIXTURE)
    else:
        expected.validate()
        if observation.fixture_key != expected.fixture_key:
            reject(CLRejectionReason.FIXTURE_ID_MISMATCH)
        if expected.provider_event_id is not None and observation.provider_event_id != expected.provider_event_id:
            reject(CLRejectionReason.FIXTURE_ID_MISMATCH)
        if observation.competition_code != expected.competition_code:
            reject(CLRejectionReason.COMPETITION_MISMATCH)
        if (_team(observation.home_team), _team(observation.away_team)) != (
            _team(expected.home_team), _team(expected.away_team)
        ):
            reject(CLRejectionReason.PARTICIPANT_MISMATCH)
        if abs((observation.kickoff_utc - expected.kickoff_utc).total_seconds()) > policy.kickoff_tolerance_seconds:
            reject(CLRejectionReason.KICKOFF_MISMATCH)

    try:
        observation.validate(require_quota_metadata=policy.require_quota_metadata)
    except CLShadowContractError:
        if observation.market_type != "football:pre_match:regulation_1x2":
            reject(CLRejectionReason.WRONG_MARKET)
        if any(value is None for value in (observation.home_odds, observation.draw_odds, observation.away_odds)):
            reject(CLRejectionReason.INCOMPLETE_REGULATION_1X2)
        if observation.source_timestamp is None:
            reject(CLRejectionReason.MISSING_TIMESTAMP)
        if observation.provenance is None:
            reject(CLRejectionReason.MISSING_PROVENANCE)
        if policy.require_quota_metadata and observation.quota is None:
            reject(CLRejectionReason.MISSING_QUOTA_METADATA)
        if observation.quota is not None:
            try:
                observation.quota.validate()
            except CLShadowContractError:
                reject(CLRejectionReason.INVALID_QUOTA_METADATA)
        if any(
            value is not None
            and (
                not isinstance(value, (int, float))
                or not isfinite(float(value))
                or value <= 1.0
            )
            for value in (observation.home_odds, observation.draw_odds, observation.away_odds)
        ):
            reject(CLRejectionReason.INVALID_ODDS)

    try:
        event_state = CLEventState(observation.event_state)
    except (TypeError, ValueError):
        event_state = None
    if event_state is None:
        reject(CLRejectionReason.IN_PLAY)
    if event_state is CLEventState.STALE:
        reject(CLRejectionReason.STALE_EVENT)
    if observation.in_play or event_state is CLEventState.IN_PLAY:
        reject(CLRejectionReason.IN_PLAY)
    if observation.captured_at >= observation.kickoff_utc:
        reject(CLRejectionReason.IN_PLAY)
    if observation.source_timestamp is not None:
        if observation.source_timestamp > observation.captured_at:
            reject(CLRejectionReason.INVALID_TIMESTAMP)
        else:
            age = (observation.captured_at - observation.source_timestamp).total_seconds()
            if age < 0:
                reject(CLRejectionReason.INVALID_TIMESTAMP)
            elif age > policy.maximum_odds_age_seconds:
                reject(CLRejectionReason.STALE_EVENT)
    if observation.provenance is not None:
        try:
            observation.provenance.validate()
        except CLShadowContractError:
            reject(CLRejectionReason.MISSING_PROVENANCE)
    return CLObservationDecision(observation, not reasons, tuple(reasons))


@dataclass(frozen=True)
class CLReplayRejection:
    fixture_key: str
    provider_identity: str
    bookmaker_identity: str
    reasons: tuple[CLRejectionReason, ...]
    observation_digest: str

    def as_payload(self) -> dict[str, object]:
        return {
            "fixture_key": self.fixture_key,
            "provider_identity": self.provider_identity,
            "bookmaker_identity": self.bookmaker_identity,
            "reasons": [reason.value for reason in self.reasons],
            "observation_digest": self.observation_digest,
        }


@dataclass(frozen=True)
class CLReplayResult:
    fixtures: tuple[CLFixture, ...]
    accepted_observations: tuple[CLShadowObservation, ...]
    rejected_observations: tuple[CLReplayRejection, ...]
    retained_bookmakers: Mapping[str, tuple[str, ...]]
    network_called: bool = False
    replay_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "retained_bookmakers", MappingProxyType(dict(self.retained_bookmakers)))

    def validate(self) -> None:
        if self.network_called:
            raise CLShadowContractError("offline replay cannot report a network call")
        fixture_keys = {fixture.fixture_key for fixture in self.fixtures}
        if len(fixture_keys) != len(self.fixtures):
            raise CLShadowContractError("replay result contains duplicate fixtures")
        for fixture in self.fixtures:
            fixture.validate()
        for observation in self.accepted_observations:
            observation.validate()
            if observation.fixture_key not in fixture_keys:
                raise CLShadowContractError("accepted observation has unknown fixture")

    @property
    def coverage(self) -> dict[str, int]:
        reasons = [reason for item in self.rejected_observations for reason in item.reasons]
        return {
            "fixture_count": len(self.fixtures),
            "observation_count": len(self.accepted_observations) + len(self.rejected_observations),
            "accepted_count": len(self.accepted_observations),
            "rejected_count": len(self.rejected_observations),
            "bookmaker_count": sum(len(items) for items in self.retained_bookmakers.values()),
            "stale_event_rejections": reasons.count(CLRejectionReason.STALE_EVENT),
            "in_play_rejections": reasons.count(CLRejectionReason.IN_PLAY),
            "participant_mismatch_rejections": reasons.count(CLRejectionReason.PARTICIPANT_MISMATCH),
            "duplicate_rejections": reasons.count(CLRejectionReason.DUPLICATE_OBSERVATION),
        }

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = {
            "contract_version": CL_SHADOW_CONTRACT_VERSION,
            "network_called": False,
            "fixtures": [fixture.__dict__ | {"kickoff_utc": fixture.kickoff_utc.isoformat()} for fixture in self.fixtures],
            "accepted_observations": [item.as_payload() for item in self.accepted_observations],
            "rejected_observations": [item.as_payload() for item in self.rejected_observations],
            "retained_bookmakers": {key: list(value) for key, value in sorted(self.retained_bookmakers.items())},
            "coverage": self.coverage,
        }
        payload["replay_digest"] = self.replay_digest
        return payload


def _safe_digest(observation: CLShadowObservation) -> str:
    try:
        return observation.digest()
    except CLShadowContractError:
        return _digest({
            "fixture_key": observation.fixture_key,
            "provider_event_id": observation.provider_event_id,
            "bookmaker_identity": observation.bookmaker_identity,
            "captured_at": str(observation.captured_at),
        })


def replay_cl_shadow(
    fixtures: Sequence[CLFixture],
    observations: Sequence[CLShadowObservation],
    *,
    policy: CLShadowPolicy | None = None,
) -> CLReplayResult:
    """Run a deterministic, no-network replay over injected CL evidence."""

    policy = policy or CLShadowPolicy()
    policy.validate()
    fixture_rows = tuple(sorted(fixtures, key=lambda fixture: fixture.fixture_key))
    expected: dict[str, CLFixture] = {}
    for fixture in fixture_rows:
        fixture.validate()
        if fixture.fixture_key in expected:
            raise CLReplayError(f"duplicate fixture: {fixture.fixture_key}")
        expected[fixture.fixture_key] = fixture

    accepted: list[CLShadowObservation] = []
    rejected: list[CLReplayRejection] = []
    seen: set[tuple[str, str, str, str, str | None]] = set()
    for observation in observations:
        decision = evaluate_observation(observation, expected.get(observation.fixture_key), policy)
        key = (
            observation.fixture_key, observation.provider_identity,
            observation.provider_event_id, observation.bookmaker_identity,
            observation.source_timestamp.isoformat() if observation.source_timestamp else None,
        )
        reasons = list(decision.reasons)
        if key in seen:
            if CLRejectionReason.DUPLICATE_OBSERVATION not in reasons:
                reasons.append(CLRejectionReason.DUPLICATE_OBSERVATION)
            decision = replace(decision, accepted=False, reasons=tuple(reasons))
        seen.add(key)
        if decision.accepted:
            accepted.append(observation)
        else:
            rejected.append(CLReplayRejection(
                observation.fixture_key, observation.provider_identity,
                observation.bookmaker_identity, decision.reasons, _safe_digest(observation),
            ))

    accepted.sort(key=lambda item: (
        item.fixture_key, item.bookmaker_identity, item.provider_identity,
        item.source_timestamp or datetime.min.replace(tzinfo=timezone.utc),
        item.provider_event_id,
    ))
    rejected.sort(key=lambda item: (item.fixture_key, item.bookmaker_identity, item.provider_identity, item.observation_digest))
    retained: dict[str, tuple[str, ...]] = {}
    for item in accepted:
        retained[item.fixture_key] = tuple(sorted(set((*retained.get(item.fixture_key, ()), item.bookmaker_identity))))
    draft = CLReplayResult(fixture_rows, tuple(accepted), tuple(rejected), retained)
    digest = _digest({key: value for key, value in draft.as_payload().items() if key != "replay_digest"})
    return replace(draft, replay_digest=digest)


__all__ = [
    "CLObservationDecision", "CLReplayRejection", "CLReplayResult",
    "evaluate_observation", "replay_cl_shadow",
]
