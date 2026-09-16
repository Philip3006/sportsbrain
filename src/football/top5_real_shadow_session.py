"""Durable, provider-neutral orchestration for real Top-5 NO-BET shadow runs."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.football.production_contracts import _utc
from src.football.top5_real_shadow_attachments import (
    RealShadowClosingAttachment,
    RealShadowResultAttachment,
    RealShadowResultStatus,
)
from src.football.top5_real_shadow_contracts import (
    REAL_SHADOW_SESSION_SCHEMA,
    TEST_FIXTURE_MARKER,
    TOP5_REAL_SHADOW_LEAGUES,
    NormalizedProviderObservation,
    RealShadowContractError,
    RealShadowExperiment,
    RealShadowPredictionArtifact,
    _digest,
    _market_probabilities,
    _parse_datetime,
    _sha,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID
from src.football.top5_shadow_integration import run_offline_top5_shadow


class RealShadowSessionStatus(str, Enum):
    CREATED = "CREATED"
    OBSERVING = "OBSERVING"
    PREDICTIONS_RECORDED = "PREDICTIONS_RECORDED"
    AWAITING_RESULTS = "AWAITING_RESULTS"
    PARTIALLY_RESOLVED = "PARTIALLY_RESOLVED"
    RESULTS_RESOLVED = "RESULTS_RESOLVED"
    CLOSING_ATTACHED = "CLOSING_ATTACHED"
    COMPLETE = "COMPLETE"
    FAILED_CLOSED = "FAILED_CLOSED"


_TRANSITIONS = {
    RealShadowSessionStatus.CREATED: {
        RealShadowSessionStatus.OBSERVING,
        RealShadowSessionStatus.FAILED_CLOSED,
    },
    RealShadowSessionStatus.OBSERVING: {
        RealShadowSessionStatus.PREDICTIONS_RECORDED,
        RealShadowSessionStatus.FAILED_CLOSED,
    },
    RealShadowSessionStatus.PREDICTIONS_RECORDED: {
        RealShadowSessionStatus.AWAITING_RESULTS,
    },
    RealShadowSessionStatus.AWAITING_RESULTS: {
        RealShadowSessionStatus.PARTIALLY_RESOLVED,
        RealShadowSessionStatus.RESULTS_RESOLVED,
        RealShadowSessionStatus.CLOSING_ATTACHED,
        RealShadowSessionStatus.COMPLETE,
    },
    RealShadowSessionStatus.PARTIALLY_RESOLVED: {
        RealShadowSessionStatus.PARTIALLY_RESOLVED,
        RealShadowSessionStatus.RESULTS_RESOLVED,
        RealShadowSessionStatus.CLOSING_ATTACHED,
        RealShadowSessionStatus.COMPLETE,
    },
    RealShadowSessionStatus.RESULTS_RESOLVED: {
        RealShadowSessionStatus.RESULTS_RESOLVED,
        RealShadowSessionStatus.CLOSING_ATTACHED,
        RealShadowSessionStatus.COMPLETE,
    },
    RealShadowSessionStatus.CLOSING_ATTACHED: {
        RealShadowSessionStatus.CLOSING_ATTACHED,
        RealShadowSessionStatus.PARTIALLY_RESOLVED,
        RealShadowSessionStatus.RESULTS_RESOLVED,
        RealShadowSessionStatus.COMPLETE,
    },
    RealShadowSessionStatus.COMPLETE: {RealShadowSessionStatus.COMPLETE},
    RealShadowSessionStatus.FAILED_CLOSED: {RealShadowSessionStatus.FAILED_CLOSED},
}


def _stable_session_id(session_key: str, experiment: RealShadowExperiment, integration_sha: str, scope: tuple[str, ...], fixture_mode: bool) -> str:
    identity = {"key": session_key, "experiment": experiment.experiment_id, "integration": integration_sha, "scope": scope, "fixture_mode": fixture_mode}
    return f"top5-real-shadow:{_digest(identity)[:32]}"


@dataclass(frozen=True)
class RejectionRecord:
    fixture_key: str
    league_code: str
    provider_identity: str
    observation_digest: str
    reason: str
    rejected_at: datetime

    def validate(self) -> None:
        if not self.fixture_key.strip() or not self.league_code.strip() or not self.provider_identity.strip() or not self.observation_digest.strip() or not self.reason.strip():
            raise RealShadowContractError("rejection record lacks provenance")
        _utc(self.rejected_at, "rejected_at")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {"fixture_key": self.fixture_key, "league_code": self.league_code, "provider_identity": self.provider_identity, "observation_digest": self.observation_digest, "reason": self.reason, "rejected_at": self.rejected_at.isoformat()}

    @classmethod
    def from_payload(cls, payload: object) -> RejectionRecord:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(raw.get("fixture_key", ""), raw.get("league_code", ""), raw.get("provider_identity", ""), raw.get("observation_digest", ""), raw.get("reason", ""), _parse_datetime(raw.get("rejected_at"), "rejected_at"))


@dataclass
class RealShadowSession:
    session_id: str
    session_schema_version: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    status: RealShadowSessionStatus
    league_scope: tuple[str, ...]
    experiment: RealShadowExperiment
    integration_sha: str
    research_sha: str = FROZEN_RESEARCH_SHA
    model_identity: str = M5_CANDIDATE_ID
    observations: dict[str, NormalizedProviderObservation] = field(default_factory=dict)
    predictions: dict[str, RealShadowPredictionArtifact] = field(default_factory=dict)
    rejections: dict[str, RejectionRecord] = field(default_factory=dict)
    results: dict[str, RealShadowResultAttachment] = field(default_factory=dict)
    closings: dict[str, RealShadowClosingAttachment] = field(default_factory=dict)
    duplicate_suppressed: int = 0
    no_bet: bool = True
    publication: bool = False
    activation: bool = False
    fixture_mode: bool = False

    @classmethod
    def create(cls, session_key: str, *, experiment: RealShadowExperiment, league_scope: Sequence[str], integration_sha: str, created_at: datetime, fixture_mode: bool = False) -> RealShadowSession:
        if not isinstance(session_key, str) or not session_key.startswith("shadow-session:"):
            raise RealShadowContractError("session_key must use shadow-session namespace")
        experiment.validate()
        if not isinstance(fixture_mode, bool):
            raise RealShadowContractError("fixture_mode must be boolean")
        if isinstance(league_scope, (str, bytes)):
            raise RealShadowContractError("session league scope must be a sequence")
        scope = tuple(sorted(set(league_scope)))
        if not scope or any(league not in TOP5_REAL_SHADOW_LEAGUES for league in scope):
            raise RealShadowContractError("session league scope must contain only Top-5 leagues")
        normalized_integration_sha = _sha(integration_sha, "integration_sha")
        created = _utc(created_at, "created_at")
        return cls(_stable_session_id(session_key, experiment, normalized_integration_sha, scope, fixture_mode), REAL_SHADOW_SESSION_SCHEMA, created, None, None, RealShadowSessionStatus.CREATED, scope, experiment, normalized_integration_sha, fixture_mode=fixture_mode)

    def _transition(self, target: RealShadowSessionStatus) -> None:
        target = RealShadowSessionStatus(target)
        if target not in _TRANSITIONS[self.status]:
            raise RealShadowContractError(f"invalid session transition {self.status.value}->{target.value}")
        self.status = target
        if target in {RealShadowSessionStatus.COMPLETE, RealShadowSessionStatus.FAILED_CLOSED}:
            event_times = [self.created_at]
            event_times.extend(item.captured_at for item in self.observations.values())
            event_times.extend(item.attached_at for item in self.results.values())
            event_times.extend(item.attached_at for item in self.closings.values())
            self.completed_at = self.completed_at or max(event_times)

    def _rejection(self, observation: NormalizedProviderObservation, reason: str) -> None:
        record = RejectionRecord(observation.fixture_key, observation.league_code, observation.provider_identity, observation.observation_digest(), reason, observation.captured_at)
        record.validate()
        self.rejections[observation.fixture_key] = record

    def record_observation(self, observation: NormalizedProviderObservation) -> bool:
        if self.status not in {RealShadowSessionStatus.CREATED, RealShadowSessionStatus.OBSERVING}:
            raise RealShadowContractError("observations cannot be added after prediction finalization")
        digest = observation.observation_digest()
        old = self.observations.get(observation.fixture_key)
        if old is not None:
            if old.observation_digest() == digest:
                self.duplicate_suppressed += 1
                return False
            raise RealShadowContractError("conflicting observation for fixture")
        observation.validate(self.experiment)
        if self.status is RealShadowSessionStatus.CREATED:
            self._transition(RealShadowSessionStatus.OBSERVING)
            self.started_at = observation.captured_at
        elif self.started_at is None:
            self.started_at = observation.captured_at
        self.observations[observation.fixture_key] = observation
        if observation.league_code not in self.league_scope:
            self._rejection(observation, "observation league is outside session scope")
            return True
        if observation.eligibility_state != "eligible":
            self._rejection(observation, "provider observation is explicitly ineligible")
            return True
        if observation.observation_mode == TEST_FIXTURE_MARKER and not self.fixture_mode:
            self._rejection(observation, "test fixture is not allowed in a real session")
            return True
        if not self.experiment.signal_time.accepts(observation.kickoff_utc, observation.source_timestamp, observation.captured_at):
            self._rejection(observation, "signal-time experiment rejected observation")
            return True
        return True

    def _record_prediction(self, observation: NormalizedProviderObservation) -> None:
        integration = run_offline_top5_shadow(
            observation.league_code,
            (observation.fixture(),),
            (observation.snapshot(),),
            signal_time=self.experiment.signal_time,
            now=observation.captured_at,
            integration_sha=self.integration_sha,
            candidate_ids=(M5_CANDIDATE_ID,),
        )
        if len(integration.predictions) != 1:
            self._rejection(observation, "M5 did not produce exactly one prediction")
            return
        pipeline_prediction = integration.predictions[0]
        digest = observation.observation_digest()
        prediction_id = _digest({"session_id": self.session_id, "fixture_key": observation.fixture_key, "observation": digest})[:32]
        prediction = RealShadowPredictionArtifact(
            prediction_id=f"real-shadow-prediction:{prediction_id}", session_id=self.session_id,
            fixture_key=observation.fixture_key, league_code=observation.league_code,
            home_team=observation.home_team, away_team=observation.away_team, kickoff=observation.kickoff_utc,
            provider_identity=observation.provider_identity, bookmaker_identity=observation.bookmaker_identity,
            provider_fixture_id=observation.provider_fixture_id, observation_digest=digest,
            source_timestamp=observation.source_timestamp, captured_at=observation.captured_at,
            cascade_trace=observation.cascade_trace, quality_metadata=observation.quality_metadata,
            signal_snapshot_id=observation.signal_snapshot_id, signal_time_contract_id=self.experiment.contract_id,
            minimum_lead_minutes=self.experiment.minimum_lead_minutes, maximum_lead_minutes=self.experiment.maximum_lead_minutes,
            maximum_odds_age_seconds=self.experiment.maximum_odds_age_seconds, kickoff_tolerance_seconds=self.experiment.kickoff_tolerance_seconds,
            model_identity=M5_CANDIDATE_ID, research_sha=FROZEN_RESEARCH_SHA, integration_sha=self.integration_sha,
            probabilities=pipeline_prediction.probabilities, market_probabilities=_market_probabilities({"home": observation.home_odds, "draw": observation.draw_odds, "away": observation.away_odds}),
            artifact_sha="", marker=observation.observation_mode,
        )
        object.__setattr__(prediction, "artifact_sha", _digest(prediction._payload()))
        prediction.validate()
        self.predictions[prediction.prediction_id] = prediction

    def finalize_predictions(self) -> None:
        if self.status not in {RealShadowSessionStatus.OBSERVING, RealShadowSessionStatus.PREDICTIONS_RECORDED}:
            raise RealShadowContractError("session is not ready to finalize predictions")
        if not self.observations:
            self._transition(RealShadowSessionStatus.FAILED_CLOSED)
            return
        if self.status is RealShadowSessionStatus.OBSERVING:
            for observation in tuple(self.observations.values()):
                if observation.eligibility_state == "eligible" and observation.fixture_key not in self.rejections:
                    self._record_prediction(observation)
        if not self.predictions:
            self._transition(RealShadowSessionStatus.FAILED_CLOSED)
            return
        if self.status is RealShadowSessionStatus.OBSERVING:
            self._transition(RealShadowSessionStatus.PREDICTIONS_RECORDED)
        self._transition(RealShadowSessionStatus.AWAITING_RESULTS)

    def attach_result(self, result: RealShadowResultAttachment) -> bool:
        result.validate()
        old = self.results.get(result.prediction_id)
        if old is not None:
            if old == result:
                return False
            raise RealShadowContractError("conflicting result attachment")
        if self.status not in {RealShadowSessionStatus.AWAITING_RESULTS, RealShadowSessionStatus.PARTIALLY_RESOLVED, RealShadowSessionStatus.RESULTS_RESOLVED, RealShadowSessionStatus.CLOSING_ATTACHED}:
            raise RealShadowContractError("results require a finalized prediction session")
        prediction = self.predictions.get(result.prediction_id)
        if prediction is None or result.prediction_artifact_sha != prediction.artifact_sha or result.fixture_key != prediction.fixture_key or result.league_code != prediction.league_code:
            raise RealShadowContractError("result does not match prediction identity")
        if result.attached_at < prediction.captured_at:
            raise RealShadowContractError("result attachment precedes observed prediction")
        if RealShadowResultStatus(result.status) is RealShadowResultStatus.FINAL and result.result_timestamp < prediction.kickoff:
            raise RealShadowContractError("final result precedes kickoff")
        self.results[result.prediction_id] = result
        self._refresh_status()
        return True

    def attach_closing(self, closing: RealShadowClosingAttachment) -> bool:
        closing.validate()
        old = self.closings.get(closing.prediction_id)
        if old is not None:
            if old == closing:
                return False
            raise RealShadowContractError("conflicting closing attachment")
        if self.status not in {RealShadowSessionStatus.AWAITING_RESULTS, RealShadowSessionStatus.PARTIALLY_RESOLVED, RealShadowSessionStatus.RESULTS_RESOLVED, RealShadowSessionStatus.CLOSING_ATTACHED}:
            raise RealShadowContractError("closing requires a finalized prediction session")
        prediction = self.predictions.get(closing.prediction_id)
        if prediction is None or closing.prediction_artifact_sha != prediction.artifact_sha or closing.fixture_key != prediction.fixture_key or closing.league_code != prediction.league_code:
            raise RealShadowContractError("closing does not match prediction identity")
        if not prediction.source_timestamp <= closing.closing_timestamp <= prediction.kickoff:
            raise RealShadowContractError("closing is outside the signal-to-kickoff window")
        if closing.attached_at < prediction.captured_at:
            raise RealShadowContractError("closing attachment precedes observed prediction")
        self.closings[closing.prediction_id] = closing
        self._refresh_status()
        return True

    def _refresh_status(self) -> None:
        target = self._status_for_artifacts()
        if target is not self.status:
            self._transition(target)

    def _status_for_artifacts(self) -> RealShadowSessionStatus:
        if not self.observations:
            return RealShadowSessionStatus.CREATED
        if not self.predictions:
            return RealShadowSessionStatus.OBSERVING
        total = len(self.predictions)
        finals = sum(RealShadowResultStatus(item.status) is RealShadowResultStatus.FINAL for item in self.results.values())
        if finals == total and len(self.closings) == total:
            return RealShadowSessionStatus.COMPLETE
        if finals == total:
            return RealShadowSessionStatus.RESULTS_RESOLVED
        if self.results:
            return RealShadowSessionStatus.PARTIALLY_RESOLVED
        if self.closings:
            return RealShadowSessionStatus.CLOSING_ATTACHED
        return RealShadowSessionStatus.AWAITING_RESULTS

    def validate(self) -> None:
        try:
            self.status = RealShadowSessionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise RealShadowContractError("session status is invalid") from exc
        if self.session_schema_version != REAL_SHADOW_SESSION_SCHEMA or self.research_sha != FROZEN_RESEARCH_SHA or self.model_identity != M5_CANDIDATE_ID:
            raise RealShadowContractError("session is not bound to the frozen real-shadow contract")
        if not self.session_id.strip() or isinstance(self.league_scope, (str, bytes)):
            raise RealShadowContractError("session identity or scope is invalid")
        if not self.league_scope or any(league not in TOP5_REAL_SHADOW_LEAGUES for league in self.league_scope):
            raise RealShadowContractError("session league scope is outside Top-5")
        self.experiment.validate()
        self.integration_sha = _sha(self.integration_sha, "integration_sha")
        _utc(self.created_at, "created_at")
        if self.started_at is not None:
            _utc(self.started_at, "started_at")
        if self.completed_at is not None:
            _utc(self.completed_at, "completed_at")
        if not self.no_bet or self.publication or self.activation:
            raise RealShadowContractError("session violates NO-BET safety")
        if not isinstance(self.fixture_mode, bool):
            raise RealShadowContractError("fixture_mode must be boolean")
        if self.status is RealShadowSessionStatus.CREATED and (self.observations or self.predictions or self.rejections or self.results or self.closings or self.started_at is not None or self.completed_at is not None):
            raise RealShadowContractError("created session cannot contain lifecycle artifacts")
        for fixture_key, observation in self.observations.items():
            if fixture_key != observation.fixture_key or observation.league_code not in TOP5_REAL_SHADOW_LEAGUES:
                raise RealShadowContractError("observation identity or scope changed")
            if observation.league_code not in self.league_scope and fixture_key not in self.rejections:
                raise RealShadowContractError("out-of-scope observation is not rejected")
            observation.validate(self.experiment)
        for prediction in self.predictions.values():
            prediction.validate()
            observation = self.observations.get(prediction.fixture_key)
            if observation is None or prediction.observation_digest != observation.observation_digest():
                raise RealShadowContractError("prediction is not bound to its observed input")
            if prediction.session_id != self.session_id or prediction.integration_sha != self.integration_sha or prediction.research_sha != self.research_sha or prediction.model_identity != self.model_identity:
                raise RealShadowContractError("prediction is not bound to immutable session identity")
            if (prediction.signal_time_contract_id, prediction.minimum_lead_minutes, prediction.maximum_lead_minutes, prediction.maximum_odds_age_seconds, prediction.kickoff_tolerance_seconds) != (self.experiment.contract_id, self.experiment.minimum_lead_minutes, self.experiment.maximum_lead_minutes, self.experiment.maximum_odds_age_seconds, self.experiment.kickoff_tolerance_seconds):
                raise RealShadowContractError("prediction signal-time contract differs from session experiment")
            if prediction.marker != observation.observation_mode:
                raise RealShadowContractError("offline replay artifact entered real-shadow session")
            if prediction.marker == TEST_FIXTURE_MARKER and not self.fixture_mode:
                raise RealShadowContractError("test fixture entered a real-shadow session")
        for observation in self.observations.values():
            has_prediction = any(item.fixture_key == observation.fixture_key for item in self.predictions.values())
            if self.status not in {RealShadowSessionStatus.CREATED, RealShadowSessionStatus.OBSERVING} and observation.eligibility_state == "eligible" and not has_prediction and observation.fixture_key not in self.rejections:
                raise RealShadowContractError("eligible observation has no prediction or rejection")
        for fixture_key, rejection in self.rejections.items():
            rejection.validate()
            observation = self.observations.get(fixture_key)
            if observation is None or rejection.fixture_key != fixture_key or rejection.league_code != observation.league_code or rejection.provider_identity != observation.provider_identity or rejection.observation_digest != observation.observation_digest():
                raise RealShadowContractError("rejection is not bound to its observed input")
        for result in self.results.values():
            result.validate()
            prediction = self.predictions.get(result.prediction_id)
            if prediction is None or result.prediction_artifact_sha != prediction.artifact_sha or result.fixture_key != prediction.fixture_key or result.league_code != prediction.league_code:
                raise RealShadowContractError("result references an unknown prediction digest")
            if result.attached_at < prediction.captured_at:
                raise RealShadowContractError("result attachment precedes observed prediction")
            if RealShadowResultStatus(result.status) is RealShadowResultStatus.FINAL and result.result_timestamp < prediction.kickoff:
                raise RealShadowContractError("final result precedes kickoff")
        for closing in self.closings.values():
            closing.validate()
            prediction = self.predictions.get(closing.prediction_id)
            if prediction is None or closing.prediction_artifact_sha != prediction.artifact_sha or closing.fixture_key != prediction.fixture_key or closing.league_code != prediction.league_code:
                raise RealShadowContractError("closing references an unknown prediction digest")
            if not prediction.source_timestamp <= closing.closing_timestamp <= prediction.kickoff:
                raise RealShadowContractError("closing is outside the signal-to-kickoff window")
            if closing.attached_at < prediction.captured_at:
                raise RealShadowContractError("closing attachment precedes observed prediction")
        if self.status is RealShadowSessionStatus.FAILED_CLOSED:
            if self.predictions or self.results or self.closings:
                raise RealShadowContractError("failed-closed session cannot contain progression artifacts")
        else:
            expected = self._status_for_artifacts()
            transient = self.status is RealShadowSessionStatus.PREDICTIONS_RECORDED and expected is RealShadowSessionStatus.AWAITING_RESULTS
            if self.status is not expected and not transient:
                raise RealShadowContractError(f"session status {self.status.value} is incoherent with persisted artifacts")
        if self.status is RealShadowSessionStatus.COMPLETE and (len(self.predictions) == 0 or len(self.results) != len(self.predictions) or len(self.closings) != len(self.predictions)):
            raise RealShadowContractError("complete session is missing append-only attachments")

    def manifest(self) -> dict[str, object]:
        self.validate()
        league_counts = Counter(observation.league_code for observation in self.observations.values())
        return {
            "schema": self.session_schema_version, "session_id": self.session_id, "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status.value, "integration_sha": self.integration_sha, "research_sha": self.research_sha,
            "model_identity": self.model_identity, "league_scope": list(self.league_scope),
            "fixture_count": len(self.observations), "prediction_count": len(self.predictions), "result_count": len(self.results),
            "closing_count": len(self.closings), "rejected_count": len(self.rejections),
            "pending_result_count": len(self.predictions) - sum(RealShadowResultStatus(item.status) is RealShadowResultStatus.FINAL for item in self.results.values()),
            "provider_identities_observed": sorted({item.provider_identity for item in self.observations.values()}),
            "coverage_by_league": {league: {"discovered": league_counts[league], "predictions": sum(item.league_code == league for item in self.predictions.values()), "rejected": sum(item.league_code == league for item in self.rejections.values()), "results": sum(item.league_code == league for item in self.results.values()), "closings": sum(item.league_code == league for item in self.closings.values())} for league in self.league_scope},
            "duplicate_suppressed": self.duplicate_suppressed,
            "no_bet": self.no_bet, "publication": self.publication, "activation": self.activation,
            "fixture_mode": self.fixture_mode,
            "session_digest": self.session_digest(),
        }

    def session_digest(self) -> str:
        self.validate()
        semantic = {
            "schema": self.session_schema_version, "session_id": self.session_id, "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None, "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status.value, "league_scope": self.league_scope, "experiment": self.experiment.as_payload(),
            "integration_sha": self.integration_sha, "research_sha": self.research_sha, "model_identity": self.model_identity, "fixture_mode": self.fixture_mode,
            "observations": [self.observations[key].as_payload() for key in sorted(self.observations)],
            "predictions": [self.predictions[key].as_payload() for key in sorted(self.predictions)],
            "rejections": [self.rejections[key].as_payload() for key in sorted(self.rejections)],
            "results": [self.results[key].as_payload() for key in sorted(self.results)],
            "closings": [self.closings[key].as_payload() for key in sorted(self.closings)],
        }
        return _digest(semantic)

    def immutable_core(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "schema": self.session_schema_version,
            "created_at": self.created_at.isoformat(),
            "league_scope": list(self.league_scope),
            "experiment": self.experiment.as_payload(),
            "integration_sha": self.integration_sha,
            "research_sha": self.research_sha,
            "model_identity": self.model_identity,
            "fixture_mode": self.fixture_mode,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "activation": self.activation,
        }

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {"schema": self.session_schema_version, "session": self.manifest(), "experiment": self.experiment.as_payload(), "observations": [self.observations[key].as_payload() for key in sorted(self.observations)], "predictions": [self.predictions[key].as_payload() for key in sorted(self.predictions)], "rejections": [self.rejections[key].as_payload() for key in sorted(self.rejections)], "results": [self.results[key].as_payload() for key in sorted(self.results)], "closings": [self.closings[key].as_payload() for key in sorted(self.closings)]}

    @classmethod
    def from_payload(cls, payload: object) -> RealShadowSession:
        raw = payload if isinstance(payload, Mapping) else {}
        manifest = raw.get("session") if isinstance(raw.get("session"), Mapping) else {}
        exp_raw = raw.get("experiment") if isinstance(raw.get("experiment"), Mapping) else {}
        experiment = RealShadowExperiment(exp_raw.get("experiment_id", ""), exp_raw.get("minimum_lead_minutes", -1), exp_raw.get("maximum_lead_minutes", -1), exp_raw.get("maximum_odds_age_seconds", -1), exp_raw.get("kickoff_tolerance_seconds", -1))
        session = cls(manifest.get("session_id", ""), raw.get("schema", ""), _parse_datetime(manifest.get("created_at"), "created_at"), _parse_datetime(manifest.get("started_at"), "started_at") if manifest.get("started_at") else None, _parse_datetime(manifest.get("completed_at"), "completed_at") if manifest.get("completed_at") else None, RealShadowSessionStatus(manifest.get("status", "")), tuple(manifest.get("league_scope", ())), experiment, manifest.get("integration_sha", ""), manifest.get("research_sha", ""), manifest.get("model_identity", ""), no_bet=manifest.get("no_bet", False), publication=manifest.get("publication", True), activation=manifest.get("activation", True), fixture_mode=manifest.get("fixture_mode", False))
        session.observations = {item.fixture_key: item for item in (NormalizedProviderObservation.from_payload(item) for item in raw.get("observations", ())) }
        session.predictions = {item.prediction_id: item for item in (RealShadowPredictionArtifact.from_payload(item) for item in raw.get("predictions", ())) }
        session.rejections = {item.fixture_key: item for item in (RejectionRecord.from_payload(item) for item in raw.get("rejections", ())) }
        session.results = {item.prediction_id: item for item in (RealShadowResultAttachment.from_payload(item) for item in raw.get("results", ())) }
        session.closings = {item.prediction_id: item for item in (RealShadowClosingAttachment.from_payload(item) for item in (raw.get("closings", ()))) }
        session.duplicate_suppressed = manifest.get("duplicate_suppressed", 0)
        session.validate()
        return session


def build_session_from_payload(payload: Mapping[str, object], *, experiment: RealShadowExperiment, session_key: str, integration_sha: str, created_at: datetime, fixture_mode: bool = False) -> RealShadowSession:
    session = RealShadowSession.create(session_key, experiment=experiment, league_scope=payload.get("league_scope", TOP5_REAL_SHADOW_LEAGUES), integration_sha=integration_sha, created_at=created_at, fixture_mode=fixture_mode)
    observations = payload.get("observations", ())
    if not isinstance(observations, Sequence) or isinstance(observations, str):
        raise RealShadowContractError("observations must be a sequence")
    for item in observations:
        session.record_observation(NormalizedProviderObservation.from_payload(item))
    session.finalize_predictions()
    return session


__all__ = ["RealShadowSession", "RealShadowSessionStatus", "RejectionRecord", "build_session_from_payload"]
