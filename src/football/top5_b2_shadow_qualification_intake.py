"""Operational, no-network intake for one Builder-2 shadow qualification.

The intake is intentionally a consumer of evidence, not an evidence producer.
It accepts one manifest containing already captured canonical contracts, runs
the existing pure qualification gate, and projects an accepted result into
the canonical Builder-2 receipt.  It never imports provider clients, chooses
an authority, performs a request, or authorizes production.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.football.production_contracts import ProductionContractError
from src.football.provider_cascade.candidate_eligibility import (
    CANDIDATE_PROVIDER_IDENTITIES,
    CandidateEligibilityError,
    CandidateProviderEligibilityV1,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptV1,
    issue_builder2_qualification_receipt,
    semantic_digest,
    validate_builder2_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    NO_PRODUCTION_SIGNAL_TIME_VALUES,
    QUALIFICATION_CONTRACT_VERSION,
    TOP5_LEAGUES,
    CEOAuthorization,
    ControlledShadowCaptureAttestation,
    MinimumSamplePolicy,
    ObservationEvidenceKind,
    ProviderQualificationSession,
    ProviderQualificationStatus,
    ProviderReadinessState,
    QualificationContractError,
    QualificationTimingPolicy,
    RealProviderObservation,
    qualify_provider_observations,
)
from src.football.top5_provider_cascade_validation import (
    CASCADE_PROVIDER_ORDER,
    TOP5_CASCADE_VALIDATION_CONTRACT_VERSION,
    CascadeEvidence,
    ExpectedCascadeFixture,
    evidence_digest,
)
from src.football.top5_qualification_sample_aggregator import (
    BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION,
    Builder2QualificationSampleReportV1,
    aggregate_builder2_qualification_samples,
)
from src.football.top5_therundown_network_shadow import (
    NetworkShadowRunStatus,
    TheRundownNetworkShadowCaptureV1,
    TheRundownNetworkShadowRunResultV1,
)
from src.utils.atomic_io import atomic_write_json

INTAKE_CONTRACT_VERSION = "top5-b2-shadow-qualification-intake-v1"
RESULT_CONTRACT_VERSION = "top5-b2-shadow-qualification-intake-result-v1"
NO_NETWORK_EXECUTION = "NO NETWORK EXECUTION"
NO_BET = "NO BET"
NO_PUBLICATION = "NO PUBLICATION"
NO_PRODUCTION_ACTIVATION = "NO PRODUCTION ACTIVATION"

_MANIFEST_REQUIRED = frozenset(
    {
        "schema_version",
        "intake_id",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "fixture_key",
        "provider_identity",
        "provider_event_id",
        "provider_request_id",
        "observation_id",
        "observation_digest",
        "normalized_record_digest",
        "cascade_evidence_digest",
        "capture_attestation_digest",
        "adapter_version",
        "adapter_source_sha",
        "timing_policy_reference",
        "readiness_reference",
        "source_artifacts",
        "observation",
        "session",
        "authorization",
        "cascade_evidence",
        "capture_attestation",
        "timing_policy",
        "provider_readiness",
    }
)
_MANIFEST_OPTIONAL = frozenset({"candidate_provider_eligibility"})

_ARTIFACT_FIELDS = frozenset({"role", "path", "digest"})
_SESSION_FIELDS = frozenset(
    {
        "qualification_session_id",
        "schema_version",
        "created_at",
        "provider_identity",
        "league_scope",
        "fixture_scope",
        "configured_provider_order",
        "adapter_version",
        "adapter_source_sha",
        "qualification_state",
        "real_observation_count",
        "accepted_observation_count",
        "rejected_observation_count",
        "network_request_count",
        "selected_observation_network_request_count",
        "cascade_network_request_count",
        "quota_units_observed",
        "monetary_spend_authorized",
        "safety",
        "archive",
    }
)
_SESSION_SAFETY_FIELDS = frozenset(
    {
        "no_bet",
        "publication_enabled",
        "production_activation",
        "ledger_mutated",
        "sealed_data_accessed",
        "research_mutated",
    }
)
_AUTHORIZATION_FIELDS = frozenset(
    {
        "authorization_id",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "provider_scope",
        "league_scope",
        "fixture_scope",
        "maximum_network_requests",
        "monetary_spend_authorized",
        "issued_at",
        "expires_at",
        "single_use",
    }
)
_TIMING_FIELDS = frozenset(
    {
        "maximum_odds_age_seconds",
        "kickoff_tolerance_seconds",
        "minimum_lead_seconds",
        "maximum_lead_seconds",
        "production_signal_time_values_approved",
        "note",
    }
)
_ATTESTATION_FIELDS = frozenset(
    {
        "controlled_shadow_run_id",
        "ceo_authorization_id",
        "qualification_session_id",
        "provider_identity",
        "fixture_key",
        "provider_event_id",
        "provider_request_id",
        "adapter_version",
        "adapter_source_sha",
        "cascade_evidence_digest",
        "raw_response_digest",
        "normalized_record_digest",
        "captured_at",
        "network_execution",
        "no_bet",
        "publication",
        "monetary_spend_authorized",
        "schema_version",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "observation_id",
        "qualification_session_id",
        "evidence_kind",
        "provider_identity",
        "provider_event_id",
        "provider_request_id",
        "league",
        "fixture_key",
        "home_team",
        "away_team",
        "kickoff",
        "market_type",
        "market_phase",
        "home_odds",
        "draw_odds",
        "away_odds",
        "bookmaker_identity",
        "source_identity",
        "source_timestamp",
        "provider_timestamp_provenance",
        "captured_at",
        "request_started_at",
        "request_finished_at",
        "latency_ms",
        "adapter_version",
        "adapter_source_sha",
        "raw_response_digest",
        "normalized_record_digest",
        "cascade_evidence",
        "quota_before",
        "quota_after",
        "quota_cost_units",
        "network_request_count",
        "monetary_spend_authorized",
        "delayed_observation",
        "synthetic_reconstruction",
        "no_bet",
        "publication_enabled",
        "production_activation",
        "ledger_mutated",
        "sealed_data_accessed",
        "research_mutated",
        "capture_attestation",
    }
)
_CASCADE_FIELDS = frozenset(
    {
        "contract_version",
        "provenance",
        "configured_provider_order",
        "execution_mode",
        "attempts",
        "skipped_providers",
        "selected_provider",
        "prediction_input_allowed",
        "safety",
    }
)
_FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".env",
        "credentials",
        "secrets",
        "secret",
        "ledger",
        "sealed",
        "research",
        "worker",
        "pwa",
        "cloudflare",
        "launchd",
        "production",
        "runtime",
        ".git",
        "src",
        "scripts",
        "tests",
        "docs",
        "results",
        "models",
    }
)
_OUTPUT_FILES = (
    "manifest.json",
    "qualification_report.json",
    "receipt.json",
    "result.json",
)
_OPTIONAL_OUTPUT_FILE = "sample_report.json"


class Builder2QualificationIntakeError(QualificationContractError):
    """Malformed intake, mismatched evidence, or unsafe output request."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Builder2QualificationIntakeError(f"{name} is required")
    return value.strip()


def _component(value: object, name: str) -> str:
    text = _text(value, name)
    if text in {".", ".."} or Path(text).name != text:
        raise Builder2QualificationIntakeError(
            f"{name} must be a single safe path component"
        )
    return text


def _digest(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise Builder2QualificationIntakeError(f"{name} must be a 64-character digest")
    return text


def _source_sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise Builder2QualificationIntakeError(
            f"{name} must be a 40-character commit SHA or 64-character source digest"
        )
    return text


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise Builder2QualificationIntakeError(f"{name} must be a list")
    return tuple(value)


def _strict_mapping(
    value: object,
    *,
    required: Iterable[str],
    allowed: Iterable[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Builder2QualificationIntakeError(f"{name} must be an object")
    raw = dict(value)
    missing = set(required) - set(raw)
    if missing:
        raise Builder2QualificationIntakeError(
            f"{name} is missing required fields: {sorted(missing)}"
        )
    unknown = set(raw) - set(allowed)
    if unknown:
        raise Builder2QualificationIntakeError(
            f"{name} contains unknown fields: {sorted(unknown)}"
        )
    return raw


def _timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Builder2QualificationIntakeError(
                f"{name} is not a valid timestamp"
            ) from exc
    else:
        raise Builder2QualificationIntakeError(f"{name} is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Builder2QualificationIntakeError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _safe_external_path(value: object, name: str, *, directory: bool = False) -> Path:
    if isinstance(value, (str, os.PathLike)):
        text = os.fspath(value)
    else:
        raise Builder2QualificationIntakeError(f"{name} is required")
    path = Path(text)
    if not path.is_absolute():
        raise Builder2QualificationIntakeError(f"{name} must be an absolute path")
    resolved = path.resolve()
    for candidate in (path, resolved):
        parts = {part.casefold() for part in candidate.parts}
        path_text = str(candidate).casefold()
        if parts & _FORBIDDEN_PATH_PARTS or any(
            marker in path_text
            for marker in ("credential", "secret", "api_key", "apikey")
        ):
            raise Builder2QualificationIntakeError(f"{name} points at a protected path")
    repo_root = Path(__file__).resolve().parents[2]
    if resolved == repo_root or repo_root in resolved.parents:
        raise Builder2QualificationIntakeError(
            f"{name} must not point inside the source checkout"
        )
    if directory and resolved.exists() and not resolved.is_dir():
        raise Builder2QualificationIntakeError(f"{name} must be a directory")
    return resolved


@dataclass(frozen=True)
class Builder2QualificationSourceArtifactV1:
    """Non-secret source path and digest metadata bound by the manifest."""

    role: str
    path: str
    digest: str

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationSourceArtifactV1:
        raw = _strict_mapping(
            payload,
            required=_ARTIFACT_FIELDS,
            allowed=_ARTIFACT_FIELDS,
            name="source_artifact",
        )
        path = _safe_external_path(raw["path"], "source_artifact.path")
        return cls(
            role=_text(raw["role"], "source_artifact.role"),
            path=str(path),
            digest=_digest(raw["digest"], "source_artifact.digest"),
        )

    def validate(self) -> None:
        _text(self.role, "source_artifact.role")
        _safe_external_path(self.path, "source_artifact.path")
        _digest(self.digest, "source_artifact.digest")

    def as_payload(self) -> dict[str, str]:
        self.validate()
        return {"role": self.role, "path": self.path, "digest": self.digest}


def _session_from_payload(payload: object) -> ProviderQualificationSession:
    raw = _strict_mapping(
        payload,
        required=_SESSION_FIELDS,
        allowed=_SESSION_FIELDS,
        name="session",
    )
    safety = _strict_mapping(
        raw["safety"],
        required=_SESSION_SAFETY_FIELDS,
        allowed=_SESSION_SAFETY_FIELDS,
        name="session.safety",
    )
    session = ProviderQualificationSession(
        qualification_session_id=_text(
            raw["qualification_session_id"], "session.qualification_session_id"
        ),
        schema_version=_text(raw["schema_version"], "session.schema_version"),
        created_at=_timestamp(raw["created_at"], "session.created_at"),
        provider_identity=_text(raw["provider_identity"], "session.provider_identity"),
        league_scope=_sequence(raw["league_scope"], "session.league_scope"),
        fixture_scope=_sequence(raw["fixture_scope"], "session.fixture_scope"),
        configured_provider_order=_sequence(
            raw["configured_provider_order"], "session.configured_provider_order"
        ),
        adapter_version=_text(raw["adapter_version"], "session.adapter_version"),
        adapter_source_sha=_text(
            raw["adapter_source_sha"], "session.adapter_source_sha"
        ),
        qualification_state=raw["qualification_state"],
        real_observation_count=raw["real_observation_count"],
        accepted_observation_count=raw["accepted_observation_count"],
        rejected_observation_count=raw["rejected_observation_count"],
        network_request_count=raw["network_request_count"],
        selected_observation_network_request_count=raw[
            "selected_observation_network_request_count"
        ],
        cascade_network_request_count=raw["cascade_network_request_count"],
        quota_units_observed=raw["quota_units_observed"],
        monetary_spend_authorized=raw["monetary_spend_authorized"],
        no_bet=safety["no_bet"],
        publication_enabled=safety["publication_enabled"],
        production_activation=safety["production_activation"],
        ledger_mutated=safety["ledger_mutated"],
        sealed_data_accessed=safety["sealed_data_accessed"],
        research_mutated=safety["research_mutated"],
    )
    session.validate()
    if raw["archive"] != session.archive.paths():
        raise Builder2QualificationIntakeError(
            "session archive namespace is not canonical"
        )
    return session


def _authorization_from_payload(payload: object) -> CEOAuthorization:
    raw = _strict_mapping(
        payload,
        required=_AUTHORIZATION_FIELDS,
        allowed=_AUTHORIZATION_FIELDS,
        name="authorization",
    )
    authorization = CEOAuthorization(
        authorization_id=_text(
            raw["authorization_id"], "authorization.authorization_id"
        ),
        controlled_shadow_run_id=_text(
            raw["controlled_shadow_run_id"], "authorization.controlled_shadow_run_id"
        ),
        qualification_session_id=_text(
            raw["qualification_session_id"], "authorization.qualification_session_id"
        ),
        provider_scope=_sequence(raw["provider_scope"], "authorization.provider_scope"),
        league_scope=_sequence(raw["league_scope"], "authorization.league_scope"),
        fixture_scope=_sequence(raw["fixture_scope"], "authorization.fixture_scope"),
        maximum_network_requests=raw["maximum_network_requests"],
        monetary_spend_authorized=raw["monetary_spend_authorized"],
        issued_at=_timestamp(raw["issued_at"], "authorization.issued_at"),
        expires_at=_timestamp(raw["expires_at"], "authorization.expires_at"),
        single_use=raw["single_use"],
    )
    authorization.validate()
    return authorization


def _timing_from_payload(payload: object) -> QualificationTimingPolicy:
    raw = _strict_mapping(
        payload,
        required=_TIMING_FIELDS,
        allowed=_TIMING_FIELDS,
        name="timing_policy",
    )
    if raw["production_signal_time_values_approved"] is not False:
        raise Builder2QualificationIntakeError(
            "timing policy cannot approve production signal-time values"
        )
    if raw["note"] != NO_PRODUCTION_SIGNAL_TIME_VALUES:
        raise Builder2QualificationIntakeError("timing policy note is not fail-closed")
    policy = QualificationTimingPolicy(
        maximum_odds_age_seconds=raw["maximum_odds_age_seconds"],
        kickoff_tolerance_seconds=raw["kickoff_tolerance_seconds"],
        minimum_lead_seconds=raw["minimum_lead_seconds"],
        maximum_lead_seconds=raw["maximum_lead_seconds"],
    )
    policy.validate()
    return policy


def _cascade_from_payload(payload: object) -> CascadeEvidence:
    raw = _strict_mapping(
        payload,
        required=_CASCADE_FIELDS,
        allowed=_CASCADE_FIELDS,
        name="cascade_evidence",
    )
    if raw["contract_version"] != TOP5_CASCADE_VALIDATION_CONTRACT_VERSION:
        raise Builder2QualificationIntakeError("unsupported cascade evidence schema")
    cascade = CascadeEvidence.from_payload(raw)
    cascade.validate_structural()
    return cascade


def _attestation_from_payload(payload: object) -> ControlledShadowCaptureAttestation:
    raw = _strict_mapping(
        payload,
        required=_ATTESTATION_FIELDS,
        allowed=_ATTESTATION_FIELDS,
        name="capture_attestation",
    )
    attestation = ControlledShadowCaptureAttestation.from_payload(raw)
    attestation.validate()
    if attestation.schema_version != CAPTURE_ATTESTATION_CONTRACT_VERSION:
        raise Builder2QualificationIntakeError("unsupported capture attestation schema")
    return attestation


def _observation_from_payload(
    payload: object,
    cascade: CascadeEvidence,
    attestation: ControlledShadowCaptureAttestation,
) -> RealProviderObservation:
    raw = _strict_mapping(
        payload,
        required=_OBSERVATION_FIELDS,
        allowed=_OBSERVATION_FIELDS,
        name="observation",
    )
    embedded_cascade = _cascade_from_payload(raw["cascade_evidence"])
    embedded_attestation = _attestation_from_payload(raw["capture_attestation"])
    if evidence_digest(embedded_cascade) != evidence_digest(cascade):
        raise Builder2QualificationIntakeError(
            "observation cascade does not match the manifest cascade"
        )
    if semantic_digest(embedded_attestation.as_payload()) != semantic_digest(
        attestation.as_payload()
    ):
        raise Builder2QualificationIntakeError(
            "observation attestation does not match the manifest attestation"
        )
    observation = RealProviderObservation.from_payload(raw)
    observation = replace(
        observation, cascade_evidence=cascade, capture_attestation=attestation
    )
    observation.validate_structural()
    return observation


def _observation_digest(observation: RealProviderObservation) -> str:
    payload = observation.as_payload()
    payload.pop("capture_attestation", None)
    return semantic_digest(payload)


@dataclass(frozen=True)
class Builder2QualificationIntakeManifestV1:
    """Deterministic binding manifest for one externally captured observation."""

    schema_version: str
    intake_id: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    ceo_authorization_id: str
    fixture_key: str
    provider_identity: str
    provider_event_id: str
    provider_request_id: str
    observation_id: str
    observation_digest: str
    normalized_record_digest: str
    cascade_evidence_digest: str
    capture_attestation_digest: str
    adapter_version: str
    adapter_source_sha: str
    timing_policy_reference: str
    readiness_reference: str
    source_artifacts: tuple[Builder2QualificationSourceArtifactV1, ...]
    observation: RealProviderObservation
    session: ProviderQualificationSession
    authorization: CEOAuthorization
    cascade_evidence: CascadeEvidence
    capture_attestation: ControlledShadowCaptureAttestation
    timing_policy: QualificationTimingPolicy
    provider_readiness: Mapping[str, ProviderReadinessState | str]
    candidate_provider_eligibility: CandidateProviderEligibilityV1 | None = None

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationIntakeManifestV1:
        raw = _strict_mapping(
            payload,
            required=_MANIFEST_REQUIRED,
            allowed=(*_MANIFEST_REQUIRED, *_MANIFEST_OPTIONAL, "manifest_digest"),
            name="manifest",
        )
        source_raw = raw["source_artifacts"]
        if not isinstance(source_raw, Sequence) or isinstance(source_raw, (str, bytes)):
            raise Builder2QualificationIntakeError("source_artifacts must be a list")
        source_artifacts = tuple(
            Builder2QualificationSourceArtifactV1.from_payload(item)
            for item in source_raw
        )
        if not source_artifacts:
            raise Builder2QualificationIntakeError("source_artifacts must not be empty")
        cascade = _cascade_from_payload(raw["cascade_evidence"])
        attestation = _attestation_from_payload(raw["capture_attestation"])
        observation = _observation_from_payload(
            raw["observation"], cascade, attestation
        )
        session = _session_from_payload(raw["session"])
        authorization = _authorization_from_payload(raw["authorization"])
        timing_policy = _timing_from_payload(raw["timing_policy"])
        readiness_raw = raw["provider_readiness"]
        if not isinstance(readiness_raw, Mapping) or not readiness_raw:
            raise Builder2QualificationIntakeError(
                "provider_readiness must be a non-empty object"
            )
        provider_readiness = {
            _text(provider, "provider_readiness provider"): ProviderReadinessState(
                state
            )
            for provider, state in readiness_raw.items()
        }
        candidate_raw = raw.get("candidate_provider_eligibility")
        candidate_eligibility = (
            None
            if candidate_raw is None
            else CandidateProviderEligibilityV1.from_payload(candidate_raw)
        )
        manifest = cls(
            schema_version=_text(raw["schema_version"], "manifest.schema_version"),
            intake_id=_component(raw["intake_id"], "manifest.intake_id"),
            controlled_shadow_run_id=_text(
                raw["controlled_shadow_run_id"], "manifest.controlled_shadow_run_id"
            ),
            qualification_session_id=_text(
                raw["qualification_session_id"], "manifest.qualification_session_id"
            ),
            ceo_authorization_id=_text(
                raw["ceo_authorization_id"], "manifest.ceo_authorization_id"
            ),
            fixture_key=_text(raw["fixture_key"], "manifest.fixture_key"),
            provider_identity=_text(
                raw["provider_identity"], "manifest.provider_identity"
            ),
            provider_event_id=_text(
                raw["provider_event_id"], "manifest.provider_event_id"
            ),
            provider_request_id=_text(
                raw["provider_request_id"], "manifest.provider_request_id"
            ),
            observation_id=_text(raw["observation_id"], "manifest.observation_id"),
            observation_digest=_digest(
                raw["observation_digest"], "manifest.observation_digest"
            ),
            normalized_record_digest=_digest(
                raw["normalized_record_digest"], "manifest.normalized_record_digest"
            ),
            cascade_evidence_digest=_digest(
                raw["cascade_evidence_digest"], "manifest.cascade_evidence_digest"
            ),
            capture_attestation_digest=_digest(
                raw["capture_attestation_digest"], "manifest.capture_attestation_digest"
            ),
            adapter_version=_text(raw["adapter_version"], "manifest.adapter_version"),
            adapter_source_sha=_source_sha(
                raw["adapter_source_sha"], "manifest.adapter_source_sha"
            ),
            timing_policy_reference=_text(
                raw["timing_policy_reference"], "manifest.timing_policy_reference"
            ),
            readiness_reference=_text(
                raw["readiness_reference"], "manifest.readiness_reference"
            ),
            source_artifacts=source_artifacts,
            observation=observation,
            session=session,
            authorization=authorization,
            cascade_evidence=cascade,
            capture_attestation=attestation,
            timing_policy=timing_policy,
            provider_readiness=provider_readiness,
            candidate_provider_eligibility=candidate_eligibility,
        )
        manifest.validate()
        if (
            "manifest_digest" in raw
            and raw["manifest_digest"] != manifest.manifest_digest
        ):
            raise Builder2QualificationIntakeError(
                "manifest digest does not match its contents"
            )
        return manifest

    def validate(self) -> None:
        if self.schema_version != INTAKE_CONTRACT_VERSION:
            raise Builder2QualificationIntakeError("unsupported intake manifest schema")
        for name, value in (
            ("intake_id", self.intake_id),
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("ceo_authorization_id", self.ceo_authorization_id),
            ("fixture_key", self.fixture_key),
            ("provider_identity", self.provider_identity),
            ("provider_event_id", self.provider_event_id),
            ("provider_request_id", self.provider_request_id),
            ("observation_id", self.observation_id),
            ("adapter_version", self.adapter_version),
            ("timing_policy_reference", self.timing_policy_reference),
            ("readiness_reference", self.readiness_reference),
        ):
            _text(value, f"manifest.{name}")
        _component(self.intake_id, "manifest.intake_id")
        for name, value in (
            ("observation_digest", self.observation_digest),
            ("normalized_record_digest", self.normalized_record_digest),
            ("cascade_evidence_digest", self.cascade_evidence_digest),
            ("capture_attestation_digest", self.capture_attestation_digest),
        ):
            _digest(value, f"manifest.{name}")
        _source_sha(self.adapter_source_sha, "manifest.adapter_source_sha")
        if not self.source_artifacts:
            raise Builder2QualificationIntakeError(
                "manifest source_artifacts are required"
            )
        for artifact in self.source_artifacts:
            artifact.validate()
        if tuple(
            sorted(self.source_artifacts, key=lambda item: (item.role, item.path))
        ) != (self.source_artifacts):
            raise Builder2QualificationIntakeError(
                "source_artifacts must be deterministically sorted"
            )
        if len({item.role for item in self.source_artifacts}) != len(
            self.source_artifacts
        ):
            raise Builder2QualificationIntakeError(
                "source_artifact roles must be unique"
            )

        self.session.validate()
        self.authorization.validate()
        self.cascade_evidence.validate_structural()
        self.capture_attestation.validate()
        self.observation.validate_structural()
        self.timing_policy.validate()
        if self.provider_identity in CANDIDATE_PROVIDER_IDENTITIES:
            if self.candidate_provider_eligibility is None:
                raise Builder2QualificationIntakeError(
                    "candidate provider requires an explicit eligibility binding"
                )
            try:
                self.candidate_provider_eligibility.validate(
                    now=self.observation.captured_at
                )
                self.candidate_provider_eligibility.matches_observation(
                    self.observation
                )
            except CandidateEligibilityError as exc:
                raise Builder2QualificationIntakeError(
                    "candidate eligibility does not match canonical observation"
                ) from exc
            candidate_bindings = (
                (
                    self.candidate_provider_eligibility.controlled_shadow_run_id,
                    self.authorization.controlled_shadow_run_id,
                ),
                (
                    self.candidate_provider_eligibility.qualification_session_id,
                    self.session.qualification_session_id,
                ),
                (
                    self.candidate_provider_eligibility.authorization_id,
                    self.authorization.authorization_id,
                ),
                (
                    self.candidate_provider_eligibility.provider_identity,
                    self.provider_identity,
                ),
            )
            if any(actual != expected for actual, expected in candidate_bindings):
                raise Builder2QualificationIntakeError(
                    "candidate eligibility run/session/authorization binding mismatch"
                )
        elif self.candidate_provider_eligibility is not None:
            raise Builder2QualificationIntakeError(
                "candidate eligibility is only valid for a candidate provider"
            )
        if set(self.provider_readiness) != set(self.session.configured_provider_order):
            raise Builder2QualificationIntakeError(
                "provider_readiness must cover the configured cascade order exactly"
            )
        for provider, state in self.provider_readiness.items():
            if provider not in (
                set(CASCADE_PROVIDER_ORDER) | CANDIDATE_PROVIDER_IDENTITIES
            ):
                raise Builder2QualificationIntakeError(
                    "provider_readiness contains an unknown provider"
                )
            ProviderReadinessState(state)

        expected_fields = (
            ("controlled_shadow_run_id", self.authorization.controlled_shadow_run_id),
            (
                "controlled_shadow_run_id",
                self.capture_attestation.controlled_shadow_run_id,
            ),
            ("qualification_session_id", self.session.qualification_session_id),
            ("qualification_session_id", self.authorization.qualification_session_id),
            ("qualification_session_id", self.observation.qualification_session_id),
            (
                "qualification_session_id",
                self.capture_attestation.qualification_session_id,
            ),
            ("ceo_authorization_id", self.authorization.authorization_id),
            ("ceo_authorization_id", self.capture_attestation.ceo_authorization_id),
            ("fixture_key", self.observation.fixture_key),
            ("fixture_key", self.capture_attestation.fixture_key),
            ("provider_identity", self.observation.provider_identity),
            ("provider_identity", self.capture_attestation.provider_identity),
            ("provider_event_id", self.observation.provider_event_id),
            ("provider_event_id", self.capture_attestation.provider_event_id),
            ("provider_request_id", self.observation.provider_request_id),
            ("provider_request_id", self.capture_attestation.provider_request_id),
            ("observation_id", self.observation.observation_id),
            ("normalized_record_digest", self.observation.normalized_record_digest),
            (
                "normalized_record_digest",
                self.capture_attestation.normalized_record_digest,
            ),
            ("adapter_version", self.observation.adapter_version),
            ("adapter_version", self.capture_attestation.adapter_version),
            ("adapter_source_sha", self.observation.adapter_source_sha),
            ("adapter_source_sha", self.capture_attestation.adapter_source_sha),
        )
        manifest_values = {
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "fixture_key": self.fixture_key,
            "provider_identity": self.provider_identity,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "normalized_record_digest": self.normalized_record_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
        }
        for field_name, actual in expected_fields:
            if manifest_values[field_name] != actual:
                raise Builder2QualificationIntakeError(
                    f"manifest {field_name} binding does not match canonical evidence"
                )
        if self.observation.evidence_kind != ObservationEvidenceKind.REAL_OBSERVED:
            raise Builder2QualificationIntakeError(
                "only REAL_OBSERVED evidence may enter the operational intake"
            )
        if self.cascade_evidence.selected_provider != self.provider_identity:
            raise Builder2QualificationIntakeError("selected provider binding mismatch")
        selected = [
            attempt
            for attempt in self.cascade_evidence.attempts
            if attempt.provider_identity == self.provider_identity
        ]
        if len(selected) != 1:
            raise Builder2QualificationIntakeError(
                "manifest requires exactly one selected provider attempt"
            )
        selected_attempt = selected[0]
        if (
            selected_attempt.provider_record_id != self.provider_event_id
            or selected_attempt.request_identity != self.provider_request_id
            or selected_attempt.adapter_version != self.adapter_version
            or selected_attempt.raw_record_digest
            != self.observation.raw_response_digest
            or selected_attempt.fixture_key != self.fixture_key
        ):
            raise Builder2QualificationIntakeError(
                "selected cascade attempt identity does not match observation"
            )
        if self.cascade_evidence.prediction_input_allowed is not True:
            raise Builder2QualificationIntakeError(
                "prediction_input_allowed must be true for accepted qualification evidence"
            )
        if self.observation_digest != _observation_digest(self.observation):
            raise Builder2QualificationIntakeError(
                "observation digest binding mismatch"
            )
        if self.cascade_evidence_digest != evidence_digest(self.cascade_evidence):
            raise Builder2QualificationIntakeError(
                "cascade evidence digest binding mismatch"
            )
        if self.capture_attestation_digest != semantic_digest(
            self.capture_attestation.as_payload()
        ):
            raise Builder2QualificationIntakeError(
                "capture attestation digest binding mismatch"
            )
        if (
            self.capture_attestation.cascade_evidence_digest
            != self.cascade_evidence_digest
        ):
            raise Builder2QualificationIntakeError(
                "attestation cascade digest binding mismatch"
            )
        if self.authorization.allows(self.observation) is not None:
            raise Builder2QualificationIntakeError(
                "observation is outside the supplied CEO authorization"
            )
        if self.capture_attestation.network_execution is not True:
            raise Builder2QualificationIntakeError(
                "operational intake requires an attestation of completed external execution"
            )
        if self.session.provider_identity != "top5_cascade":
            raise Builder2QualificationIntakeError(
                "qualification session must remain cascade-scoped"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = {
            "schema_version": self.schema_version,
            "intake_id": self.intake_id,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "fixture_key": self.fixture_key,
            "provider_identity": self.provider_identity,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "capture_attestation_digest": self.capture_attestation_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "timing_policy_reference": self.timing_policy_reference,
            "readiness_reference": self.readiness_reference,
            "source_artifacts": [item.as_payload() for item in self.source_artifacts],
            "observation": self.observation.as_payload(),
            "session": self.session.as_payload(),
            "authorization": self.authorization.as_payload(),
            "cascade_evidence": self.cascade_evidence.as_payload(),
            "capture_attestation": self.capture_attestation.as_payload(),
            "timing_policy": self.timing_policy.as_payload(),
            "provider_readiness": {
                key: ProviderReadinessState(value).value
                for key, value in sorted(self.provider_readiness.items())
            },
        }
        if self.candidate_provider_eligibility is not None:
            payload["candidate_provider_eligibility"] = (
                self.candidate_provider_eligibility.as_payload()
            )
        return payload

    @property
    def manifest_digest(self) -> str:
        return semantic_digest(self.as_payload())


@dataclass(frozen=True)
class Builder2QualificationIntakeResultV1:
    """Safe derived result; the receipt remains validation-only authority."""

    intake_id: str
    manifest_digest: str
    qualification_report: Any
    receipt: Builder2QualificationReceiptV1
    sample_report: Builder2QualificationSampleReportV1 | None
    artifact_directory: str | None = None

    def validate(self) -> None:
        _text(self.intake_id, "result.intake_id")
        _digest(self.manifest_digest, "result.manifest_digest")
        self.qualification_report.validate()
        self.receipt.validate()
        if self.sample_report is not None:
            self.sample_report.validate()
            if self.sample_report.production_activation_authorized:
                raise Builder2QualificationIntakeError(
                    "sample report cannot authorize production"
                )
        if self.receipt.no_bet is not True or self.receipt.publication is not False:
            raise Builder2QualificationIntakeError("result safety contract is unsafe")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": RESULT_CONTRACT_VERSION,
            "intake_id": self.intake_id,
            "manifest_digest": self.manifest_digest,
            "qualification_report_digest": self.receipt.qualification_report_digest,
            "qualification_status": self.receipt.qualification_status.value,
            "qualification_receipt_id": self.receipt.qualification_receipt_id,
            "receipt_digest": self.receipt.receipt_digest,
            "sample_report_digest": (
                self.sample_report.report_digest
                if self.sample_report is not None
                else None
            ),
            "artifact_directory": self.artifact_directory,
            "safety": {
                "no_network_execution": True,
                "no_bet": True,
                "no_publication": True,
                "no_production_activation": True,
                "production_model_bound": False,
            },
        }


def validate_intake(
    manifest: Builder2QualificationIntakeManifestV1,
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
) -> Builder2QualificationIntakeResultV1:
    """Validate and derive one receipt without writing or performing I/O."""

    if not isinstance(manifest, Builder2QualificationIntakeManifestV1):
        raise Builder2QualificationIntakeError("canonical intake manifest is required")
    manifest.validate()
    if minimum_sample_policy is not None:
        minimum_sample_policy.validate()
    expected_fixture = ExpectedCascadeFixture(
        league=manifest.observation.league,
        fixture_key=manifest.fixture_key,
        home_team=manifest.observation.home_team,
        away_team=manifest.observation.away_team,
        kickoff=manifest.observation.kickoff,
    )
    report = qualify_provider_observations(
        (manifest.observation,),
        manifest.session,
        expected_fixture,
        manifest.timing_policy,
        manifest.provider_readiness,
        manifest.authorization,
        candidate_eligibility=manifest.candidate_provider_eligibility,
        minimum_sample_policy=minimum_sample_policy,
    )
    if (
        report.qualification_status
        is not ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    ):
        raise Builder2QualificationIntakeError(
            "external evidence did not produce a validated real observation"
        )
    accepted = [
        result for result in report.results if result.accepted and result.real_observed
    ]
    if len(accepted) != 1:
        raise Builder2QualificationIntakeError(
            "operational intake requires exactly one accepted observation result"
        )
    receipt = issue_builder2_qualification_receipt(
        report, manifest.observation, accepted[0]
    )
    validate_builder2_qualification_receipt(
        receipt,
        expected_observation=manifest.observation,
        expected_report=report,
        expected_result=accepted[0],
        expected_authorization=manifest.authorization,
        expected_cascade_evidence=manifest.cascade_evidence,
        expected_capture_attestation=manifest.capture_attestation,
    )
    result = Builder2QualificationIntakeResultV1(
        intake_id=manifest.intake_id,
        manifest_digest=manifest.manifest_digest,
        qualification_report=report,
        receipt=receipt,
        sample_report=None,
    )
    result.validate()
    return result


def qualify_five_league_shadow_run(
    shadow_run: TheRundownNetworkShadowRunResultV1,
    manifests: Sequence[Builder2QualificationIntakeManifestV1],
) -> tuple[Builder2QualificationIntakeResultV1, ...]:
    """Validate one complete shadow run and derive its five B2 receipts.

    The controlled-shadow runner remains the producer of capture evidence and
    Builder 2 remains the receipt authority.  This wrapper only binds the
    already-materialized B2 manifests to the exact successful five-league run,
    then delegates every observation check and receipt issuance to
    :func:`validate_intake`.  It performs no I/O or network execution.
    """

    if not isinstance(shadow_run, TheRundownNetworkShadowRunResultV1):
        raise Builder2QualificationIntakeError(
            "a canonical five-league shadow run result is required"
        )
    shadow_run.validate()
    if shadow_run.status is not NetworkShadowRunStatus.COMPLETED_NETWORK:
        raise Builder2QualificationIntakeError(
            "only a completed network shadow run may enter the operational intake"
        )
    if not shadow_run.all_five_succeeded:
        raise Builder2QualificationIntakeError(
            "all five controlled-shadow captures must succeed before receipt intake"
        )
    if len(shadow_run.captures) != len(TOP5_LEAGUES):
        raise Builder2QualificationIntakeError(
            "five controlled-shadow captures are required"
        )
    if not isinstance(manifests, Sequence) or isinstance(manifests, (str, bytes)):
        raise Builder2QualificationIntakeError(
            "five canonical intake manifests are required"
        )
    if len(manifests) != len(TOP5_LEAGUES):
        raise Builder2QualificationIntakeError(
            "exactly five canonical intake manifests are required"
        )
    if shadow_run.request_count != len(TOP5_LEAGUES):
        raise Builder2QualificationIntakeError(
            "successful five-league run request count is inconsistent"
        )
    expected_datapoints = sum(
        capture.response.datapoint_count for capture in shadow_run.captures
    )
    expected_quota = sum(
        float(capture.response.quota_cost_units) for capture in shadow_run.captures
    )
    if shadow_run.datapoint_count != expected_datapoints:
        raise Builder2QualificationIntakeError(
            "five-league datapoint total does not match captures"
        )
    if shadow_run.quota_cost_units != expected_quota:
        raise Builder2QualificationIntakeError(
            "five-league quota total does not match captures"
        )

    captures_by_league: dict[str, TheRundownNetworkShadowCaptureV1] = {}
    for capture in shadow_run.captures:
        if capture.target.league in captures_by_league:
            raise Builder2QualificationIntakeError(
                "five-league run contains duplicate league captures"
            )
        captures_by_league[capture.target.league] = capture
    manifests_by_league: dict[str, Builder2QualificationIntakeManifestV1] = {}
    for manifest in manifests:
        if not isinstance(manifest, Builder2QualificationIntakeManifestV1):
            raise Builder2QualificationIntakeError(
                "five-league intake items must be canonical manifests"
            )
        if manifest.observation.league in manifests_by_league:
            raise Builder2QualificationIntakeError(
                "five-league intake contains duplicate league manifests"
            )
        manifests_by_league[manifest.observation.league] = manifest

    if set(captures_by_league) != set(TOP5_LEAGUES) or set(manifests_by_league) != set(
        TOP5_LEAGUES
    ):
        raise Builder2QualificationIntakeError(
            "capture and manifest scopes must cover exactly the five Top-5 leagues"
        )

    for league in TOP5_LEAGUES:
        _bind_shadow_capture_to_manifest(
            captures_by_league[league], manifests_by_league[league], shadow_run
        )

    # Validate every manifest only after the complete run-wide binding pass so
    # a later mismatch cannot leave a partially derived receipt batch.
    return tuple(
        validate_intake(manifests_by_league[league]) for league in TOP5_LEAGUES
    )


def _bind_shadow_capture_to_manifest(
    capture: TheRundownNetworkShadowCaptureV1,
    manifest: Builder2QualificationIntakeManifestV1,
    shadow_run: TheRundownNetworkShadowRunResultV1,
) -> None:
    """Bind B4 capture identity/provenance to one canonical B2 manifest."""

    capture.validate()
    if capture.evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED:
        raise Builder2QualificationIntakeError(
            "five-league receipt intake requires REAL_OBSERVED captures"
        )
    if (
        capture.network_execution is not True
        or capture.response.network_execution is not True
    ):
        raise Builder2QualificationIntakeError(
            "five-league receipt intake requires network execution attestation"
        )
    request = capture.request
    target = capture.target
    response = capture.response
    observation = manifest.observation
    try:
        response_evidence_kind = ObservationEvidenceKind(response.evidence_kind)
    except (TypeError, ValueError) as exc:
        raise Builder2QualificationIntakeError(
            "capture response evidence kind is invalid"
        ) from exc
    if response_evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED:
        raise Builder2QualificationIntakeError(
            "five-league receipt intake requires a real provider response"
        )
    if response.http_status != 200:
        raise Builder2QualificationIntakeError(
            "five-league receipt intake requires an HTTP 200 response"
        )
    if response.retry_count != 0:
        raise Builder2QualificationIntakeError(
            "five-league receipt intake forbids provider retries"
        )
    if (
        not isinstance(response.datapoint_count, int)
        or isinstance(response.datapoint_count, bool)
        or response.datapoint_count <= 0
    ):
        raise Builder2QualificationIntakeError(
            "successful capture datapoint evidence is invalid"
        )
    if (
        response.provider != target.provider
        or response.league != target.league
        or response.fixture_key != target.fixture_key
        or response.provider_event_id != target.provider_event_id
        or response.provider_request_id != request.request_identity
        or response.home_team != target.home_team
        or response.away_team != target.away_team
        or response.home_participant_id != request.home_participant_id
        or response.away_participant_id != request.away_participant_id
    ):
        raise Builder2QualificationIntakeError(
            "capture response identity or participant binding mismatch"
        )
    if any(
        getattr(response, field) is not expected
        for field, expected in (
            ("no_bet", True),
            ("publication", False),
            ("production_activation", False),
            ("monetary_spend_authorized", False),
            ("authority_attempted", False),
            ("publication_attempted", False),
            ("activation_attempted", False),
            ("ledger_mutated", False),
            ("scheduler_registered", False),
        )
    ):
        raise Builder2QualificationIntakeError(
            "capture response carries an unsafe side-effect flag"
        )
    eligibility = manifest.candidate_provider_eligibility
    if eligibility is None:
        raise Builder2QualificationIntakeError(
            "candidate provider eligibility is required for a TheRundown capture"
        )
    try:
        expected_eligibility = CandidateProviderEligibilityV1.from_network_capture(
            capture, now=response.captured_at
        )
    except (CandidateEligibilityError, TypeError, ValueError) as exc:
        raise Builder2QualificationIntakeError(
            "capture cannot produce a valid candidate eligibility binding"
        ) from exc
    if eligibility != expected_eligibility:
        raise Builder2QualificationIntakeError(
            "candidate eligibility does not exactly match the capture"
        )

    run_bindings = (
        (request.controlled_shadow_run_id, shadow_run.controlled_shadow_run_id),
        (request.qualification_session_id, shadow_run.qualification_session_id),
        (request.authorization_id, shadow_run.authorization_id),
    )
    if any(actual != expected for actual, expected in run_bindings):
        raise Builder2QualificationIntakeError(
            "capture run/session/authorization identity is not bound to the run"
        )
    manifest_bindings = (
        (manifest.controlled_shadow_run_id, request.controlled_shadow_run_id),
        (manifest.qualification_session_id, request.qualification_session_id),
        (manifest.ceo_authorization_id, request.authorization_id),
        (manifest.provider_identity, target.provider),
        (manifest.fixture_key, target.fixture_key),
        (manifest.provider_event_id, response.provider_event_id),
        (manifest.provider_request_id, response.provider_request_id),
        (manifest.observation_id, capture.observation_id),
    )
    if any(actual != expected for actual, expected in manifest_bindings):
        raise Builder2QualificationIntakeError(
            "manifest identity is not bound to the exact capture"
        )
    if response.provider_request_id != request.request_identity:
        raise Builder2QualificationIntakeError(
            "provider request identity does not match the authorized request"
        )
    if capture.observation_id is None or capture.observation_digest is None:
        raise Builder2QualificationIntakeError(
            "successful capture observation identity/digest is missing"
        )
    _digest(capture.observation_digest, "capture.observation_digest")
    if (
        capture.canonical_capture_attestation
        != manifest.capture_attestation.as_payload()
    ):
        raise Builder2QualificationIntakeError(
            "capture attestation is not the exact B4 attestation"
        )
    if capture.capture_attestation_digest != semantic_digest(
        manifest.capture_attestation.as_payload()
    ):
        raise Builder2QualificationIntakeError(
            "capture attestation digest does not match the canonical attestation"
        )
    observation_bindings = (
        (observation.provider_identity, target.provider),
        (observation.league, target.league),
        (observation.fixture_key, target.fixture_key),
        (observation.home_team, target.home_team),
        (observation.away_team, target.away_team),
        (observation.kickoff, target.kickoff),
        (observation.provider_event_id, response.provider_event_id),
        (observation.provider_request_id, response.provider_request_id),
        (observation.bookmaker_identity, response.bookmaker_identity),
        (observation.source_identity, response.source_identity),
        (observation.source_timestamp, response.source_timestamp),
        (observation.captured_at, response.captured_at),
        (observation.request_started_at, response.request_started_at),
        (observation.request_finished_at, response.request_finished_at),
        (observation.home_odds, response.home_odds),
        (observation.draw_odds, response.draw_odds),
        (observation.away_odds, response.away_odds),
        (observation.adapter_version, response.adapter_version),
        (observation.adapter_source_sha.lower(), response.adapter_source_sha.lower()),
        (observation.raw_response_digest.lower(), response.raw_response_digest.lower()),
        (
            observation.normalized_record_digest.lower(),
            response.normalized_record_digest.lower(),
        ),
        (observation.quota_before, response.quota_before),
        (observation.quota_after, response.quota_after),
        (observation.quota_cost_units, response.quota_cost_units),
        (observation.network_request_count, 1),
    )
    if any(actual != expected for actual, expected in observation_bindings):
        raise Builder2QualificationIntakeError(
            "observation fixture/provider/market/provenance binding mismatch"
        )
    if manifest.cascade_evidence_digest != response.cascade_evidence_digest:
        raise Builder2QualificationIntakeError(
            "cascade evidence digest is not bound to the capture"
        )


def _safe_evidence_directory(path: object) -> Path:
    return _safe_external_path(path, "evidence_directory", directory=True)


def _load_json(path: object, name: str) -> dict[str, object]:
    safe_path = _safe_external_path(path, name)
    try:
        raw = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Builder2QualificationIntakeError(f"cannot read {name}") from exc
    if not isinstance(raw, Mapping):
        raise Builder2QualificationIntakeError(f"{name} must contain a JSON object")
    return dict(raw)


def load_intake_manifest(path: str | Path) -> Builder2QualificationIntakeManifestV1:
    return Builder2QualificationIntakeManifestV1.from_payload(
        _load_json(path, "manifest")
    )


def load_receipts_from_directory(
    path: str | Path,
) -> tuple[Builder2QualificationReceiptV1, ...]:
    directory = _safe_evidence_directory(path)
    if not directory.exists() or not directory.is_dir():
        raise Builder2QualificationIntakeError("receipt directory does not exist")
    paths = sorted(
        candidate
        for candidate in directory.rglob("*.json")
        if candidate.name
        not in {
            "manifest.json",
            "qualification_report.json",
            "result.json",
            "sample_report.json",
        }
    )
    receipts: list[Builder2QualificationReceiptV1] = []
    for candidate in paths:
        raw = _load_json(candidate, "receipt artifact")
        if raw.get("schema_version") != "top5-builder2-qualification-receipt-v1":
            raise Builder2QualificationIntakeError(
                f"non-canonical receipt artifact encountered: {candidate.name}"
            )
        receipt = Builder2QualificationReceiptV1.from_payload(raw)
        receipt.validate()
        receipts.append(receipt)
    if not receipts:
        raise Builder2QualificationIntakeError(
            "receipt directory contains no canonical receipts"
        )
    return tuple(receipts)


def _publish_artifacts(
    directory: Path,
    payloads: Mapping[str, Mapping[str, object]],
) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{directory.name}.staging-", dir=directory.parent)
    )
    try:
        if directory.exists():
            raise Builder2QualificationIntakeError(
                "intake artifact directory appeared during publication; refusing overwrite"
            )
        for filename in sorted(payloads):
            atomic_write_json(
                staging / filename,
                payloads[filename],
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        os.replace(staging, directory)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def run_intake(
    manifest: Builder2QualificationIntakeManifestV1,
    evidence_directory: str | Path,
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
    existing_receipts: Iterable[Builder2QualificationReceiptV1 | Mapping[str, object]]
    | None = None,
) -> Builder2QualificationIntakeResultV1:
    """Validate, optionally aggregate, and atomically publish external artifacts."""

    output_root = _safe_evidence_directory(evidence_directory)
    # Sample policy is an aggregation input, not part of the one-observation
    # receipt identity.  This keeps the receipt stable when callers change
    # their measurement policy over the same captured evidence.
    result = validate_intake(manifest)
    prior_receipts = tuple(existing_receipts or ())
    sample_report = None
    if existing_receipts is not None or minimum_sample_policy is not None:
        sample_report = aggregate_builder2_qualification_samples(
            (*prior_receipts, result.receipt),
            minimum_sample_policy=minimum_sample_policy,
        )
        result = Builder2QualificationIntakeResultV1(
            intake_id=result.intake_id,
            manifest_digest=result.manifest_digest,
            qualification_report=result.qualification_report,
            receipt=result.receipt,
            sample_report=sample_report,
        )
        result.validate()

    target = output_root / manifest.intake_id
    if output_root.exists():
        for sibling in sorted(output_root.iterdir()):
            if not sibling.is_dir() or sibling.name.startswith("."):
                continue
            sibling_manifest_path = sibling / "manifest.json"
            if not sibling_manifest_path.exists():
                continue
            sibling_manifest = _load_json(
                sibling_manifest_path, "existing intake manifest"
            )
            if (
                sibling_manifest.get("controlled_shadow_run_id")
                == manifest.controlled_shadow_run_id
                and sibling_manifest.get("observation_id") == manifest.observation_id
                and sibling.name != manifest.intake_id
            ):
                if sibling_manifest.get("manifest_digest") != manifest.manifest_digest:
                    raise Builder2QualificationIntakeError(
                        "conflicting intake for the same controlled run and observation"
                    )
                raise Builder2QualificationIntakeError(
                    "duplicate intake for the same controlled run and observation"
                )
    if target.exists():
        existing_manifest_path = target / "manifest.json"
        if not existing_manifest_path.exists():
            raise Builder2QualificationIntakeError(
                "existing intake directory lacks its manifest; refusing overwrite"
            )
        existing_manifest = _load_json(
            existing_manifest_path, "existing intake manifest"
        )
        existing_digest = existing_manifest.get("manifest_digest")
        if existing_digest != manifest.manifest_digest:
            raise Builder2QualificationIntakeError(
                "conflicting manifest for an existing intake identity"
            )
        existing_receipt = _load_json(target / "receipt.json", "existing receipt")
        if existing_receipt.get("receipt_digest") != result.receipt.receipt_digest:
            raise Builder2QualificationIntakeError(
                "conflicting receipt for an existing intake identity"
            )
        existing_sample_path = target / _OPTIONAL_OUTPUT_FILE
        if existing_sample_path.exists() != (result.sample_report is not None):
            raise Builder2QualificationIntakeError(
                "existing intake output set conflicts with the requested aggregation"
            )
        if result.sample_report is not None:
            existing_sample = _load_json(existing_sample_path, "existing sample report")
            if (
                existing_sample.get("report_digest")
                != result.sample_report.report_digest
            ):
                raise Builder2QualificationIntakeError(
                    "conflicting sample report for an existing intake identity"
                )
        return Builder2QualificationIntakeResultV1(
            intake_id=result.intake_id,
            manifest_digest=result.manifest_digest,
            qualification_report=result.qualification_report,
            receipt=result.receipt,
            sample_report=result.sample_report,
            artifact_directory=str(target),
        )

    manifest_payload = manifest.as_payload()
    manifest_payload["manifest_digest"] = manifest.manifest_digest
    payloads: dict[str, Mapping[str, object]] = {
        "manifest.json": manifest_payload,
        "qualification_report.json": result.qualification_report.as_payload(),
        "receipt.json": result.receipt.as_payload(),
    }
    if result.sample_report is not None:
        payloads["sample_report.json"] = result.sample_report.as_payload()
    published_result = Builder2QualificationIntakeResultV1(
        intake_id=result.intake_id,
        manifest_digest=result.manifest_digest,
        qualification_report=result.qualification_report,
        receipt=result.receipt,
        sample_report=result.sample_report,
        artifact_directory=str(target),
    )
    payloads["result.json"] = published_result.as_payload()
    _publish_artifacts(target, payloads)
    return published_result


def _report_artifact_summary(raw: Mapping[str, object]) -> dict[str, object]:
    if raw.get("contract_version") != QUALIFICATION_CONTRACT_VERSION:
        raise Builder2QualificationIntakeError(
            "unsupported qualification report schema"
        )
    required = {
        "contract_version",
        "qualification_status",
        "session",
        "results",
        "coverage",
        "freshness",
        "failure_counts",
        "unresolved",
    }
    if not required <= set(raw):
        raise Builder2QualificationIntakeError(
            "qualification report is missing required fields"
        )
    return {
        "artifact_type": "qualification_report",
        "qualification_status": raw["qualification_status"],
        "result_count": len(raw["results"])
        if isinstance(raw["results"], Sequence)
        else None,
        "unresolved": raw["unresolved"],
        "no_production_signal_time": raw.get(
            "signal_time_note", NO_PRODUCTION_SIGNAL_TIME_VALUES
        ),
        "production_activation_authorized": raw.get(
            "production_activation_authorized", False
        ),
    }


def inspect_artifact(path: str | Path) -> dict[str, object]:
    raw = _load_json(path, "artifact")
    schema = raw.get("schema_version") or raw.get("contract_version")
    if schema == INTAKE_CONTRACT_VERSION:
        manifest = Builder2QualificationIntakeManifestV1.from_payload(raw)
        return {
            "artifact_type": "manifest",
            "digest": manifest.manifest_digest,
            "payload": manifest.as_payload(),
        }
    if schema == "top5-builder2-qualification-receipt-v1":
        receipt = Builder2QualificationReceiptV1.from_payload(raw)
        receipt.validate()
        return {
            "artifact_type": "receipt",
            "digest": receipt.receipt_digest,
            "payload": receipt.as_payload(),
        }
    if schema == BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION:
        report = Builder2QualificationSampleReportV1.from_payload(raw)
        report.validate()
        return {
            "artifact_type": "sample_report",
            "digest": report.report_digest,
            "payload": report.as_payload(),
        }
    if schema == QUALIFICATION_CONTRACT_VERSION:
        return _report_artifact_summary(raw)
    if schema == RESULT_CONTRACT_VERSION:
        required = {
            "schema_version",
            "intake_id",
            "manifest_digest",
            "receipt_digest",
            "safety",
        }
        if not required <= set(raw):
            raise Builder2QualificationIntakeError(
                "intake result is missing required fields"
            )
        if raw["safety"] != {
            "no_network_execution": True,
            "no_bet": True,
            "no_publication": True,
            "no_production_activation": True,
            "production_model_bound": False,
        }:
            raise Builder2QualificationIntakeError(
                "intake result safety state is unsafe"
            )
        return {"artifact_type": "intake_result", "payload": raw}
    raise Builder2QualificationIntakeError("unknown intake artifact schema")


inspect_intake_artifact = inspect_artifact
Builder2QualificationIntakeSourceArtifactV1 = Builder2QualificationSourceArtifactV1


def _policy_from_args(args: argparse.Namespace) -> MinimumSamplePolicy | None:
    values = (args.minimum_real_observations, args.minimum_distinct_fixtures)
    if values == (None, None):
        return None
    if None in values:
        raise Builder2QualificationIntakeError(
            "minimum-real-observations and minimum-distinct-fixtures must be supplied together"
        )
    return MinimumSamplePolicy(*values)


def _banners() -> dict[str, bool]:
    return {
        NO_NETWORK_EXECUTION: True,
        NO_BET: True,
        NO_PUBLICATION: True,
        NO_PRODUCTION_ACTIVATION: True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Builder-2 controlled-shadow qualification intake (no network execution)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("manifest")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("manifest")
    run_parser.add_argument("--evidence-dir", required=True)
    run_parser.add_argument("--receipt-dir")
    run_parser.add_argument("--minimum-real-observations", type=int)
    run_parser.add_argument("--minimum-distinct-fixtures", type=int)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("artifact")

    aggregate_parser = subparsers.add_parser("aggregate")
    aggregate_parser.add_argument("receipt_dir")
    aggregate_parser.add_argument("--output")
    aggregate_parser.add_argument("--minimum-real-observations", type=int)
    aggregate_parser.add_argument("--minimum-distinct-fixtures", type=int)

    try:
        args = parser.parse_args(argv)
        if args.command == "validate":
            manifest = load_intake_manifest(args.manifest)
            result = validate_intake(manifest)
            payload = {
                "status": "VALID",
                "manifest_digest": result.manifest_digest,
                "qualification_status": result.receipt.qualification_status.value,
                "receipt_digest": result.receipt.receipt_digest,
                "safety": _banners(),
            }
        elif args.command == "run":
            manifest = load_intake_manifest(args.manifest)
            policy = _policy_from_args(args)
            receipts = (
                load_receipts_from_directory(args.receipt_dir)
                if args.receipt_dir
                else None
            )
            result = run_intake(
                manifest,
                args.evidence_dir,
                minimum_sample_policy=policy,
                existing_receipts=receipts,
            )
            payload = {**result.as_payload(), "safety": _banners()}
        elif args.command == "inspect":
            payload = inspect_artifact(args.artifact)
            payload["safety"] = _banners()
        else:
            policy = _policy_from_args(args)
            receipts = load_receipts_from_directory(args.receipt_dir)
            report = aggregate_builder2_qualification_samples(
                receipts, minimum_sample_policy=policy
            )
            if args.output:
                output = _safe_external_path(args.output, "aggregate output")
                atomic_write_json(
                    output,
                    report.as_payload(),
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            payload = {
                "status": "AGGREGATED",
                **report.as_payload(),
                "safety": _banners(),
            }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except (
        Builder2QualificationIntakeError,
        QualificationContractError,
        ProductionContractError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "REJECTED", "error": str(exc), "safety": _banners()},
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2


__all__ = [
    "INTAKE_CONTRACT_VERSION",
    "NO_BET",
    "NO_NETWORK_EXECUTION",
    "NO_PRODUCTION_ACTIVATION",
    "NO_PUBLICATION",
    "Builder2QualificationIntakeError",
    "Builder2QualificationIntakeManifestV1",
    "Builder2QualificationIntakeResultV1",
    "Builder2QualificationIntakeSourceArtifactV1",
    "Builder2QualificationSourceArtifactV1",
    "inspect_artifact",
    "inspect_intake_artifact",
    "load_intake_manifest",
    "load_receipts_from_directory",
    "main",
    "qualify_five_league_shadow_run",
    "run_intake",
    "validate_intake",
]
