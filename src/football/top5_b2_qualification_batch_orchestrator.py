"""Deterministic, no-network orchestration of Builder-2 evidence packages.

This module is deliberately a thin batch layer over the accepted Builder-2
contracts.  It validates already-captured intake manifests, delegates
qualification and receipt issuance to the existing canonical implementations,
then delegates sample and decision-packet production to their existing
consumers.  It never executes a provider, creates authorization, or changes
source evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from src.football.production_contracts import ProductionContractError
from src.football.provider_cascade.candidate_eligibility import (
    CANDIDATE_PROVIDER_IDENTITIES,
)
from src.football.top5_b2_shadow_decision_packet import (
    Builder2QualificationDecisionPacketError,
    Builder2QualificationDecisionPacketV1,
    Builder2QualificationIntakeArtifactV1,
    build_builder2_qualification_decision_packet,
)
from src.football.top5_b2_shadow_qualification_intake import (
    Builder2QualificationIntakeError,
    Builder2QualificationIntakeManifestV1,
    Builder2QualificationIntakeResultV1,
    _safe_external_path,
    validate_intake,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
    semantic_digest,
    validate_builder2_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    TOP5_LEAGUES,
    MinimumSamplePolicy,
    ObservationEvidenceKind,
    ProviderQualificationReport,
    QualificationContractError,
    RealProviderObservation,
    qualify_provider_observations,
)
from src.football.top5_provider_cascade_validation import ExpectedCascadeFixture
from src.football.top5_qualification_sample_aggregator import (
    Builder2QualificationSampleReportV1,
    aggregate_builder2_qualification_samples,
)
from src.football.top5_therundown_network_shadow import (
    NETWORK_RUN_SCHEMA_VERSION,
    NetworkShadowRunStatus,
    TheRundownNetworkRequestV1,
    TheRundownNetworkResponseV1,
    TheRundownNetworkShadowCaptureV1,
    TheRundownNetworkShadowRunResultV1,
)
from src.football.top5_therundown_shadow_canary import TheRundownCanaryTargetV1
from src.utils.atomic_io import atomic_write_json, atomic_write_text

BATCH_CONTRACT_VERSION = "top5-b2-qualification-batch-orchestrator-v1"
BATCH_SCHEMA_VERSION = BATCH_CONTRACT_VERSION
FIVE_LEAGUE_SHADOW_PACKAGE_SCHEMA_VERSION = "top5-b2-five-league-shadow-package-v1"
FIVE_LEAGUE_RECEIPT_PACKAGE_SCHEMA_VERSION = "top5-b2-five-league-receipt-package-v1"
FIVE_LEAGUE_AUTHORITY_DOSSIER_SCHEMA_VERSION = "top5-b2-authority-input-dossier-v1"

QUALIFIED = "QUALIFIED"
ALREADY_QUALIFIED = "ALREADY_QUALIFIED"
REJECTED = "REJECTED"
INCOMPLETE = "INCOMPLETE"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
DIGEST_MISMATCH = "DIGEST_MISMATCH"
CONFLICT = "CONFLICT"
NON_REAL_EVIDENCE = "NON_REAL_EVIDENCE"
FAILED_CLOSED = "FAILED_CLOSED"

_VALID_STATES = frozenset(
    {
        QUALIFIED,
        ALREADY_QUALIFIED,
        REJECTED,
        INCOMPLETE,
        IDENTITY_MISMATCH,
        PROVENANCE_MISMATCH,
        DIGEST_MISMATCH,
        CONFLICT,
        NON_REAL_EVIDENCE,
        FAILED_CLOSED,
    }
)

_BATCH_SAFETY = {
    "production_activation_authorized": False,
    "controlled_activation_authorized": False,
    "publication_authorized": False,
    "betting_authorized": False,
    "model_authorized": False,
    "signal_time_authorized": False,
    "provider_network_execution": False,
    "no_provider_calls": True,
    "no_source_evidence_mutation": True,
}

_FIVE_LEAGUE_SAFETY = {
    "provider_authority_granted": False,
    "production_activation_authorized": False,
    "controlled_activation_authorized": False,
    "publication_authorized": False,
    "betting_authorized": False,
    "model_authorized": False,
    "signal_time_authorized": False,
    "provider_network_execution": False,
    "no_provider_calls": True,
    "no_source_evidence_mutation": True,
}


class Builder2QualificationBatchError(ProductionContractError):
    """Malformed or unsafe batch input/output."""


class Builder2QualificationBatchItemStatus(str, Enum):
    QUALIFIED = QUALIFIED
    ALREADY_QUALIFIED = ALREADY_QUALIFIED
    REJECTED = REJECTED
    INCOMPLETE = INCOMPLETE
    IDENTITY_MISMATCH = IDENTITY_MISMATCH
    PROVENANCE_MISMATCH = PROVENANCE_MISMATCH
    DIGEST_MISMATCH = DIGEST_MISMATCH
    CONFLICT = CONFLICT
    NON_REAL_EVIDENCE = NON_REAL_EVIDENCE
    FAILED_CLOSED = FAILED_CLOSED


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Builder2QualificationBatchError(f"{name} is required")
    return value.strip()


def _digest(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise Builder2QualificationBatchError(
            f"{name} must be a 64-character hexadecimal digest"
        )
    return text


def _canonical_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sorted_unique(values: Iterable[str], name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({_text(value, name) for value in values}))
    return normalized


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _report_digest(report: ProviderQualificationReport) -> str:
    """Reproduce the canonical report projection used by Receipt V1."""

    report.validate()
    payload = report.as_payload()
    for key in ("results", "coverage", "freshness"):
        payload[key] = sorted(payload[key], key=_canonical_key)
    payload["unresolved"] = sorted(payload["unresolved"])
    return semantic_digest(payload)


def _failure_reason(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


@dataclass(frozen=True)
class Builder2QualificationBatchItemV1:
    """One canonical capture/intake package supplied to the batch layer."""

    manifest: Builder2QualificationIntakeManifestV1
    receipt: Builder2QualificationReceiptV1 | None = None
    qualification_report: ProviderQualificationReport | Mapping[str, object] | None = (
        None
    )
    result: Mapping[str, object] | None = None
    sample_report: Builder2QualificationSampleReportV1 | None = None
    artifact_path: str = "external-capture"

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationBatchItemV1:
        if isinstance(payload, Builder2QualificationBatchItemV1):
            payload.validate()
            return payload
        if isinstance(payload, Builder2QualificationIntakeManifestV1):
            return cls(manifest=payload)
        if not isinstance(payload, Mapping):
            raise Builder2QualificationBatchError("batch item must be an object")
        raw = dict(payload)
        if "manifest" not in raw and "intake_id" in raw:
            manifest = Builder2QualificationIntakeManifestV1.from_payload(raw)
            item = cls(manifest=manifest)
            item.validate()
            return item
        allowed = {
            "manifest",
            "receipt",
            "qualification_report",
            "result",
            "sample_report",
            "artifact_path",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise Builder2QualificationBatchError(
                f"batch item contains unknown fields: {sorted(unknown)}"
            )
        if "manifest" not in raw:
            raise Builder2QualificationBatchError("batch item manifest is required")
        manifest = (
            raw["manifest"]
            if isinstance(raw["manifest"], Builder2QualificationIntakeManifestV1)
            else Builder2QualificationIntakeManifestV1.from_payload(raw["manifest"])
        )
        receipt_raw = raw.get("receipt")
        receipt = None
        if receipt_raw is not None:
            receipt = (
                receipt_raw
                if isinstance(receipt_raw, Builder2QualificationReceiptV1)
                else Builder2QualificationReceiptV1.from_payload(receipt_raw)
            )
        report = raw.get("qualification_report")
        if report is not None and not isinstance(
            report, (ProviderQualificationReport, Mapping)
        ):
            raise Builder2QualificationBatchError(
                "qualification_report must be a canonical report object"
            )
        result = raw.get("result")
        if result is not None and not isinstance(result, Mapping):
            raise Builder2QualificationBatchError("result must be an object")
        sample_raw = raw.get("sample_report")
        sample = None
        if sample_raw is not None:
            sample = (
                sample_raw
                if isinstance(sample_raw, Builder2QualificationSampleReportV1)
                else Builder2QualificationSampleReportV1.from_payload(sample_raw)
            )
        item = cls(
            manifest=manifest,
            receipt=receipt,
            qualification_report=report,
            result=dict(result) if isinstance(result, Mapping) else None,
            sample_report=sample,
            artifact_path=_text(
                raw.get("artifact_path", "external-capture"), "artifact_path"
            ),
        )
        item.validate()
        return item

    def validate(self) -> None:
        if not isinstance(self.manifest, Builder2QualificationIntakeManifestV1):
            raise Builder2QualificationBatchError(
                "canonical intake manifest is required"
            )
        self.manifest.validate()
        if self.receipt is not None:
            self.receipt.validate()
        if isinstance(self.qualification_report, ProviderQualificationReport):
            self.qualification_report.validate()
        elif self.qualification_report is not None and not isinstance(
            self.qualification_report, Mapping
        ):
            raise Builder2QualificationBatchError(
                "qualification_report must be a canonical report object"
            )
        if self.result is not None and not isinstance(self.result, Mapping):
            raise Builder2QualificationBatchError("result must be an object")
        if self.sample_report is not None:
            self.sample_report.validate()
        _text(self.artifact_path, "artifact_path")

    @property
    def intake_id(self) -> str:
        return self.manifest.intake_id

    @property
    def manifest_digest(self) -> str:
        return self.manifest.manifest_digest

    def as_payload(self) -> dict[str, object]:
        self.validate()
        report: object = self.qualification_report
        if isinstance(report, ProviderQualificationReport):
            report = report.as_payload()
        return {
            "manifest": self.manifest.as_payload(),
            "receipt": self.receipt.as_payload() if self.receipt else None,
            "qualification_report": report,
            "result": dict(self.result) if self.result else None,
            "sample_report": self.sample_report.as_payload()
            if self.sample_report
            else None,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class Builder2QualificationBatchItemResultV1:
    """Deterministic item-level outcome and retained failure evidence."""

    intake_id: str
    manifest_digest: str
    status: str
    reasons: tuple[str, ...]
    controlled_shadow_run_id: str | None = None
    qualification_session_id: str | None = None
    ceo_authorization_id: str | None = None
    fixture_key: str | None = None
    league: str | None = None
    provider_identity: str | None = None
    provider_event_id: str | None = None
    provider_request_id: str | None = None
    observation_id: str | None = None
    observation_digest: str | None = None
    capture_attestation_digest: str | None = None
    qualification_report_digest: str | None = None
    receipt_id: str | None = None
    receipt_digest: str | None = None
    failure_codes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status in {QUALIFIED, ALREADY_QUALIFIED}

    def validate(self) -> None:
        _text(self.intake_id, "item_result.intake_id")
        _digest(self.manifest_digest, "item_result.manifest_digest")
        if self.status not in _VALID_STATES:
            raise Builder2QualificationBatchError("item_result.status is invalid")
        if tuple(sorted(set(self.reasons))) != self.reasons:
            raise Builder2QualificationBatchError("item_result.reasons must be sorted")
        if tuple(sorted(set(self.failure_codes))) != self.failure_codes:
            raise Builder2QualificationBatchError(
                "item_result.failure_codes must be sorted"
            )
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("ceo_authorization_id", self.ceo_authorization_id),
            ("fixture_key", self.fixture_key),
            ("league", self.league),
            ("provider_identity", self.provider_identity),
            ("provider_event_id", self.provider_event_id),
            ("provider_request_id", self.provider_request_id),
            ("observation_id", self.observation_id),
        ):
            if value is not None:
                _text(value, f"item_result.{name}")
        for name, value in (
            ("observation_digest", self.observation_digest),
            ("capture_attestation_digest", self.capture_attestation_digest),
            ("qualification_report_digest", self.qualification_report_digest),
            ("receipt_digest", self.receipt_digest),
        ):
            if value is not None:
                _digest(value, f"item_result.{name}")
        if self.receipt_id is not None:
            _text(self.receipt_id, "item_result.receipt_id")
        for value in (*self.reasons, *self.failure_codes):
            _text(value, "item_result.reason")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "intake_id": self.intake_id,
            "manifest_digest": self.manifest_digest,
            "status": self.status,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "fixture_key": self.fixture_key,
            "league": self.league,
            "provider_identity": self.provider_identity,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "capture_attestation_digest": self.capture_attestation_digest,
            "qualification_report_digest": self.qualification_report_digest,
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "failure_codes": list(self.failure_codes),
        }

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationBatchItemResultV1:
        if not isinstance(payload, Mapping):
            raise Builder2QualificationBatchError("item_result must be an object")
        raw = dict(payload)
        required = {
            "intake_id",
            "manifest_digest",
            "status",
            "accepted",
            "reasons",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_id",
            "fixture_key",
            "league",
            "provider_identity",
            "provider_event_id",
            "provider_request_id",
            "observation_id",
            "observation_digest",
            "capture_attestation_digest",
            "qualification_report_digest",
            "receipt_id",
            "receipt_digest",
            "failure_codes",
        }
        if set(raw) != required:
            raise Builder2QualificationBatchError(
                f"item_result fields mismatch: {sorted(set(raw) ^ required)}"
            )
        if not isinstance(raw["accepted"], bool):
            raise Builder2QualificationBatchError("item_result.accepted is invalid")
        result = cls(
            intake_id=raw["intake_id"],
            manifest_digest=raw["manifest_digest"],
            status=raw["status"],
            reasons=tuple(raw["reasons"]),
            controlled_shadow_run_id=raw["controlled_shadow_run_id"],
            qualification_session_id=raw["qualification_session_id"],
            ceo_authorization_id=raw["ceo_authorization_id"],
            fixture_key=raw["fixture_key"],
            league=raw["league"],
            provider_identity=raw["provider_identity"],
            provider_event_id=raw["provider_event_id"],
            provider_request_id=raw["provider_request_id"],
            observation_id=raw["observation_id"],
            observation_digest=raw["observation_digest"],
            capture_attestation_digest=raw["capture_attestation_digest"],
            qualification_report_digest=raw["qualification_report_digest"],
            receipt_id=raw["receipt_id"],
            receipt_digest=raw["receipt_digest"],
            failure_codes=tuple(raw["failure_codes"]),
        )
        if result.accepted != raw["accepted"]:
            raise Builder2QualificationBatchError(
                "item_result.accepted is inconsistent"
            )
        result.validate()
        return result


@dataclass(frozen=True)
class _IdentityRecord:
    index: int
    item: Builder2QualificationBatchItemV1

    @property
    def manifest(self) -> Builder2QualificationIntakeManifestV1:
        return self.item.manifest

    @property
    def receipt(self) -> Builder2QualificationReceiptV1 | None:
        return self.item.receipt

    @property
    def manifest_digest(self) -> str:
        return self.item.manifest_digest

    def observation_fingerprint(self) -> tuple[str, ...]:
        manifest = self.manifest
        return (
            manifest.controlled_shadow_run_id,
            manifest.qualification_session_id,
            manifest.ceo_authorization_id,
            manifest.fixture_key,
            manifest.provider_identity,
            manifest.provider_event_id,
            manifest.provider_request_id,
            manifest.observation_id,
            manifest.observation_digest,
            manifest.normalized_record_digest,
            manifest.capture_attestation_digest,
        )


_SHADOW_PACKAGE_FIELDS = frozenset(
    {
        "schema_version",
        "package_id",
        "package_digest",
        "shadow_run",
        "capture_envelopes",
        "manifests",
    }
)
_SHADOW_RUN_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "authorization_id",
        "captures",
        "failures",
        "request_count",
        "datapoint_count",
        "quota_cost_units",
        "receipt_eligible",
        "authority_changed",
        "publication",
        "production_activation",
        "monetary_spend_authorized",
    }
)
_SHADOW_CAPTURE_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "league",
        "fixture_key",
        "provider_event_id",
        "request_identity",
        "ceo_authorization_identity",
        "authorization_digest",
        "configuration_digest",
        "participant_scope",
        "evidence_kind",
        "network_execution",
        "candidate_only",
        "receipt_eligible",
        "observation_id",
        "observation_digest",
        "capture_attestation_digest",
        "capture_attestation_input",
        "observation_input",
        "failure_reason",
        "qualification_input",
        "builder2_receipt_input",
        "safety",
        "response",
    }
)
_SHADOW_ENVELOPE_FIELDS = frozenset({"target", "request", "capture"})


def _package_mapping(
    value: object,
    *,
    required: Iterable[str],
    allowed: Iterable[str] | None = None,
    name: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Builder2QualificationBatchError(f"{name} must be an object")
    raw = dict(value)
    required_set = set(required)
    allowed_set = required_set if allowed is None else set(allowed)
    missing = required_set - set(raw)
    unknown = set(raw) - allowed_set
    if missing or unknown:
        raise Builder2QualificationBatchError(
            f"{name} fields mismatch: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return raw


def _package_timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Builder2QualificationBatchError(
                f"{name} is not a valid timestamp"
            ) from exc
    else:
        raise Builder2QualificationBatchError(f"{name} is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Builder2QualificationBatchError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _shadow_target_from_payload(value: object) -> TheRundownCanaryTargetV1:
    raw = _package_mapping(
        value,
        required={
            "provider",
            "league",
            "fixture_key",
            "provider_event_id",
            "home_team",
            "away_team",
            "kickoff",
        },
        name="capture target",
    )
    target = TheRundownCanaryTargetV1(
        provider=raw["provider"],
        league=raw["league"],
        fixture_key=raw["fixture_key"],
        provider_event_id=raw["provider_event_id"],
        home_team=raw["home_team"],
        away_team=raw["away_team"],
        kickoff=_package_timestamp(raw["kickoff"], "target.kickoff"),
    )
    target.validate()
    return target


def _shadow_request_from_payload(value: object) -> TheRundownNetworkRequestV1:
    raw = _package_mapping(
        value,
        required={
            "schema_version",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "authorization_id",
            "ceo_authorization_identity",
            "authorization_digest",
            "authorization_expires_at",
            "configuration_digest",
            "target",
            "request_identity",
            "home_participant_id",
            "away_participant_id",
            "sequence",
            "market_type",
            "execution_mode",
        },
        name="capture request",
    )
    if raw["schema_version"] != "top5-therundown-network-request-v1":
        raise Builder2QualificationBatchError("unsupported capture request schema")
    request = TheRundownNetworkRequestV1(
        controlled_shadow_run_id=raw["controlled_shadow_run_id"],
        qualification_session_id=raw["qualification_session_id"],
        authorization_id=raw["authorization_id"],
        ceo_authorization_identity=raw["ceo_authorization_identity"],
        authorization_digest=raw["authorization_digest"],
        authorization_expires_at=_package_timestamp(
            raw["authorization_expires_at"], "request.authorization_expires_at"
        ),
        configuration_digest=raw["configuration_digest"],
        target=_shadow_target_from_payload(raw["target"]),
        request_identity=raw["request_identity"],
        home_participant_id=raw["home_participant_id"],
        away_participant_id=raw["away_participant_id"],
        sequence=raw["sequence"],
        market_type=raw["market_type"],
        execution_mode=raw["execution_mode"],
    )
    request.validate(now=request.authorization_expires_at - timedelta(microseconds=1))
    return request


def _shadow_response_from_payload(value: object) -> TheRundownNetworkResponseV1:
    raw = _package_mapping(
        value,
        required=set(TheRundownNetworkResponseV1.__dataclass_fields__),
        name="capture response",
    )
    return TheRundownNetworkResponseV1.from_payload(raw)


def _shadow_capture_from_envelope(value: object) -> TheRundownNetworkShadowCaptureV1:
    raw = _package_mapping(
        value,
        required=_SHADOW_ENVELOPE_FIELDS,
        name="capture envelope",
    )
    capture_raw = _package_mapping(
        raw["capture"],
        required=_SHADOW_CAPTURE_FIELDS,
        name="capture envelope.capture",
    )
    request = _shadow_request_from_payload(raw["request"])
    target = _shadow_target_from_payload(raw["target"])
    response = _shadow_response_from_payload(capture_raw["response"])
    try:
        evidence_kind = ObservationEvidenceKind(capture_raw["evidence_kind"])
    except (TypeError, ValueError) as exc:
        raise Builder2QualificationBatchError(
            "capture evidence kind is invalid"
        ) from exc
    capture = TheRundownNetworkShadowCaptureV1(
        target=target,
        request=request,
        response=response,
        evidence_kind=evidence_kind,
        network_execution=capture_raw["network_execution"],
        observation_id=capture_raw["observation_id"],
        observation_digest=capture_raw["observation_digest"],
        capture_attestation_digest=capture_raw["capture_attestation_digest"],
        capture_attestation_input=capture_raw["capture_attestation_input"],
        observation_input=capture_raw["observation_input"],
        failure_reason=capture_raw["failure_reason"],
        candidate_only=capture_raw["candidate_only"],
        receipt_eligible=capture_raw["receipt_eligible"],
    )
    if capture.as_payload() != capture_raw:
        raise Builder2QualificationBatchError(
            "capture envelope contains a non-canonical capture projection"
        )
    capture.validate()
    return capture


def _shadow_run_from_payload(
    value: object,
    captures: tuple[TheRundownNetworkShadowCaptureV1, ...],
) -> TheRundownNetworkShadowRunResultV1:
    raw = _package_mapping(value, required=_SHADOW_RUN_FIELDS, name="shadow run")
    if raw["schema_version"] != NETWORK_RUN_SCHEMA_VERSION:
        raise Builder2QualificationBatchError("unsupported shadow run schema")
    if raw["captures"] != [capture.as_payload() for capture in captures]:
        raise Builder2QualificationBatchError(
            "shadow run captures do not match the full B4 capture envelopes"
        )
    try:
        status = NetworkShadowRunStatus(raw["status"])
    except (TypeError, ValueError) as exc:
        raise Builder2QualificationBatchError("shadow run status is invalid") from exc
    run = TheRundownNetworkShadowRunResultV1(
        status=status,
        controlled_shadow_run_id=raw["controlled_shadow_run_id"],
        qualification_session_id=raw["qualification_session_id"],
        authorization_id=raw["authorization_id"],
        captures=captures,
        failures=tuple(raw["failures"]),
        request_count=raw["request_count"],
        datapoint_count=raw["datapoint_count"],
        quota_cost_units=raw["quota_cost_units"],
        receipt_eligible=raw["receipt_eligible"],
        authority_changed=raw["authority_changed"],
        publication=raw["publication"],
        production_activation=raw["production_activation"],
        monetary_spend_authorized=raw["monetary_spend_authorized"],
    )
    run.validate()
    return run


@dataclass(frozen=True)
class Builder2FiveLeagueShadowPackageV1:
    """Full B4 artifact package needed by the one-shot offline B2 stage."""

    schema_version: str
    package_id: str
    package_digest: str
    shadow_run: TheRundownNetworkShadowRunResultV1
    manifests: tuple[Builder2QualificationIntakeManifestV1, ...]

    def _payload(self, *, include_identity: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "shadow_run": self.shadow_run.as_payload(),
            "capture_envelopes": [
                {
                    "target": capture.target.as_payload(),
                    "request": capture.request.as_payload(),
                    "capture": capture.as_payload(),
                }
                for capture in self.shadow_run.captures
            ],
            "manifests": [manifest.as_payload() for manifest in self.manifests],
        }
        if include_identity:
            payload["package_id"] = self.package_id
            payload["package_digest"] = self.package_digest
        return payload

    def validate(self) -> None:
        if self.schema_version != FIVE_LEAGUE_SHADOW_PACKAGE_SCHEMA_VERSION:
            raise Builder2QualificationBatchError(
                "unsupported five-league package schema"
            )
        self.shadow_run.validate()
        if self.shadow_run.status is not NetworkShadowRunStatus.COMPLETED_NETWORK:
            raise Builder2QualificationBatchError(
                "five-league package must contain COMPLETED_NETWORK evidence"
            )
        if not self.shadow_run.all_five_succeeded:
            raise Builder2QualificationBatchError(
                "five-league package must contain five successful captures"
            )
        if len(self.shadow_run.captures) != len(TOP5_LEAGUES):
            raise Builder2QualificationBatchError(
                "five-league package must contain exactly five captures"
            )
        if len(self.manifests) != len(TOP5_LEAGUES):
            raise Builder2QualificationBatchError(
                "five-league package must contain exactly five manifests"
            )
        capture_leagues = tuple(
            capture.target.league for capture in self.shadow_run.captures
        )
        manifest_leagues = tuple(
            manifest.observation.league for manifest in self.manifests
        )
        if set(capture_leagues) != set(TOP5_LEAGUES) or len(
            set(capture_leagues)
        ) != len(capture_leagues):
            raise Builder2QualificationBatchError(
                "capture package must cover EPL, BL1, LL, SA and L1 exactly once"
            )
        if set(manifest_leagues) != set(TOP5_LEAGUES) or len(
            set(manifest_leagues)
        ) != len(manifest_leagues):
            raise Builder2QualificationBatchError(
                "manifest package must cover EPL, BL1, LL, SA and L1 exactly once"
            )
        for manifest in self.manifests:
            manifest.validate()
        base = self._payload(include_identity=False)
        expected_digest = semantic_digest(base)
        _digest(self.package_digest, "five-league package_digest")
        if self.package_digest.lower() != expected_digest:
            raise Builder2QualificationBatchError("five-league package digest mismatch")
        if self.package_id != f"b2b4pkg-{expected_digest[:24]}":
            raise Builder2QualificationBatchError(
                "five-league package ID is not deterministic"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_identity=True)

    @classmethod
    def from_payload(cls, payload: object) -> Builder2FiveLeagueShadowPackageV1:
        raw = _package_mapping(
            payload,
            required=_SHADOW_PACKAGE_FIELDS,
            name="five-league shadow package",
        )
        envelopes = raw["capture_envelopes"]
        manifests = raw["manifests"]
        if not isinstance(envelopes, Sequence) or isinstance(envelopes, (str, bytes)):
            raise Builder2QualificationBatchError("capture_envelopes must be a list")
        if not isinstance(manifests, Sequence) or isinstance(manifests, (str, bytes)):
            raise Builder2QualificationBatchError("manifests must be a list")
        captures = tuple(_shadow_capture_from_envelope(item) for item in envelopes)
        run = _shadow_run_from_payload(raw["shadow_run"], captures)
        parsed_manifests = tuple(
            item
            if isinstance(item, Builder2QualificationIntakeManifestV1)
            else Builder2QualificationIntakeManifestV1.from_payload(item)
            for item in manifests
        )
        package = cls(
            schema_version=raw["schema_version"],
            package_id=raw["package_id"],
            package_digest=raw["package_digest"],
            shadow_run=run,
            manifests=parsed_manifests,
        )
        package.validate()
        return package


def build_five_league_shadow_package(
    shadow_run: TheRundownNetworkShadowRunResultV1,
    manifests: Sequence[Builder2QualificationIntakeManifestV1],
) -> Builder2FiveLeagueShadowPackageV1:
    """Materialize one canonical B4 package without network or source writes."""

    package = Builder2FiveLeagueShadowPackageV1(
        schema_version=FIVE_LEAGUE_SHADOW_PACKAGE_SCHEMA_VERSION,
        package_id="",
        package_digest="",
        shadow_run=shadow_run,
        manifests=tuple(manifests),
    )
    package.shadow_run.validate()
    for manifest in package.manifests:
        manifest.validate()
    expected_digest = semantic_digest(package._payload(include_identity=False))
    result = replace(
        package,
        package_id=f"b2b4pkg-{expected_digest[:24]}",
        package_digest=expected_digest,
    )
    result.validate()
    return result


def load_five_league_shadow_package(
    path: str | Path,
) -> Builder2FiveLeagueShadowPackageV1:
    """Load one external, serialized B4 package read-only."""

    try:
        candidate = _safe_external_path(path, "five-league shadow package")
    except Builder2QualificationIntakeError as exc:
        raise Builder2QualificationBatchError(str(exc)) from exc
    if not candidate.is_file():
        raise Builder2QualificationBatchError(
            "five-league shadow package must be one external JSON file"
        )
    try:
        return Builder2FiveLeagueShadowPackageV1.from_payload(
            _load_json(candidate, "five-league shadow package")
        )
    except Builder2QualificationBatchError:
        raise
    except (TypeError, ValueError) as exc:
        raise Builder2QualificationBatchError(
            f"five-league shadow package is invalid: {exc}"
        ) from exc


@dataclass(frozen=True)
class Builder2FiveLeagueReceiptBindingV1:
    """One immutable receipt-to-capture binding for the downstream dossier."""

    league: str
    provider_identity: str
    fixture_key: str
    provider_event_id: str
    provider_request_id: str
    observation_id: str
    home_team: str
    away_team: str
    home_participant_id: str
    away_participant_id: str
    kickoff: datetime
    bookmaker_identity: str
    source_identity: str
    source_timestamp: datetime
    captured_at: datetime
    raw_response_digest: str
    provider_record_digest: str
    observation_digest: str
    normalized_record_digest: str
    cascade_evidence_digest: str
    capture_attestation_digest: str
    adapter_version: str
    adapter_source_sha: str
    configuration_digest: str
    home_odds: float
    draw_odds: float
    away_odds: float
    quota_before: int
    quota_after: int
    quota_cost_units: float
    datapoint_count: int
    rate_limit_remaining: int
    rate_limit_reset_at: datetime
    account_tier: str
    provider_delay_seconds: float
    http_status: int
    retry_count: int
    evidence_kind: str
    network_execution: bool
    no_bet: bool
    publication: bool
    production_activation: bool
    monetary_spend_authorized: bool
    source_artifact_digests: tuple[str, ...]
    receipt_id: str
    receipt_digest: str
    qualification_result_digest: str

    def _payload(self) -> dict[str, object]:
        return {
            "league": self.league,
            "provider_identity": self.provider_identity,
            "fixture_key": self.fixture_key,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
            "kickoff": self.kickoff.astimezone(timezone.utc).isoformat(),
            "bookmaker_identity": self.bookmaker_identity,
            "source_identity": self.source_identity,
            "source_timestamp": self.source_timestamp.astimezone(
                timezone.utc
            ).isoformat(),
            "captured_at": self.captured_at.astimezone(timezone.utc).isoformat(),
            "raw_response_digest": self.raw_response_digest,
            "provider_record_digest": self.provider_record_digest,
            "observation_digest": self.observation_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "capture_attestation_digest": self.capture_attestation_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "configuration_digest": self.configuration_digest,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "quota_before": self.quota_before,
            "quota_after": self.quota_after,
            "quota_cost_units": self.quota_cost_units,
            "datapoint_count": self.datapoint_count,
            "rate_limit_remaining": self.rate_limit_remaining,
            "rate_limit_reset_at": self.rate_limit_reset_at.astimezone(
                timezone.utc
            ).isoformat(),
            "account_tier": self.account_tier,
            "provider_delay_seconds": self.provider_delay_seconds,
            "http_status": self.http_status,
            "retry_count": self.retry_count,
            "evidence_kind": self.evidence_kind,
            "network_execution": self.network_execution,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
            "source_artifact_digests": list(self.source_artifact_digests),
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "qualification_result_digest": self.qualification_result_digest,
        }

    def validate(self) -> None:
        for name, value in self._payload().items():
            if name in {
                "source_artifact_digests",
                "home_odds",
                "draw_odds",
                "away_odds",
                "quota_before",
                "quota_after",
                "quota_cost_units",
                "datapoint_count",
                "rate_limit_remaining",
                "provider_delay_seconds",
                "http_status",
                "retry_count",
                "network_execution",
                "no_bet",
                "publication",
                "production_activation",
                "monetary_spend_authorized",
            }:
                continue
            if name.endswith(("digest", "_sha")):
                _digest(value, f"binding.{name}")
            elif name in {
                "kickoff",
                "source_timestamp",
                "captured_at",
                "rate_limit_reset_at",
            }:
                _package_timestamp(value, f"binding.{name}")
            else:
                _text(value, f"binding.{name}")
        for name, value in (
            ("home_odds", self.home_odds),
            ("draw_odds", self.draw_odds),
            ("away_odds", self.away_odds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise Builder2QualificationBatchError(
                    f"binding.{name} must be a positive number"
                )
        for name, value in (
            ("quota_cost_units", self.quota_cost_units),
            ("provider_delay_seconds", self.provider_delay_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise Builder2QualificationBatchError(
                    f"binding.{name} must be a non-negative number"
                )
        for name, value in (
            ("quota_before", self.quota_before),
            ("quota_after", self.quota_after),
            ("datapoint_count", self.datapoint_count),
            ("rate_limit_remaining", self.rate_limit_remaining),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Builder2QualificationBatchError(
                    f"binding.{name} must be a non-negative integer"
                )
        if self.quota_after > self.quota_before:
            raise Builder2QualificationBatchError(
                "binding quota_after exceeds quota_before"
            )
        if self.http_status != 200 or self.retry_count != 0:
            raise Builder2QualificationBatchError(
                "binding requires HTTP 200 and zero retries"
            )
        if self.evidence_kind != ObservationEvidenceKind.REAL_OBSERVED.value:
            raise Builder2QualificationBatchError(
                "binding evidence must be REAL_OBSERVED"
            )
        for name, value, expected in (
            ("network_execution", self.network_execution, True),
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise Builder2QualificationBatchError(f"binding unsafe field: {name}")
        _digest(self.provider_record_digest, "binding.provider_record_digest")
        if self.league not in TOP5_LEAGUES:
            raise Builder2QualificationBatchError("binding league is outside Top-5")
        if self.provider_identity not in CANDIDATE_PROVIDER_IDENTITIES:
            raise Builder2QualificationBatchError(
                "binding provider is not candidate-only"
            )
        if tuple(sorted(self.source_artifact_digests)) != self.source_artifact_digests:
            raise Builder2QualificationBatchError(
                "binding source digests are not sorted"
            )
        if not self.source_artifact_digests:
            raise Builder2QualificationBatchError(
                "binding source provenance is missing"
            )
        for value in self.source_artifact_digests:
            _text(value, "binding.source_artifact_digest")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload()

    @classmethod
    def from_payload(cls, payload: object) -> Builder2FiveLeagueReceiptBindingV1:
        fields = frozenset(Builder2FiveLeagueReceiptBindingV1.__dataclass_fields__)
        raw = _package_mapping(payload, required=fields, name="receipt binding")
        binding = cls(
            league=raw["league"],
            provider_identity=raw["provider_identity"],
            fixture_key=raw["fixture_key"],
            provider_event_id=raw["provider_event_id"],
            provider_request_id=raw["provider_request_id"],
            observation_id=raw["observation_id"],
            home_team=raw["home_team"],
            away_team=raw["away_team"],
            home_participant_id=raw["home_participant_id"],
            away_participant_id=raw["away_participant_id"],
            kickoff=_package_timestamp(raw["kickoff"], "binding.kickoff"),
            bookmaker_identity=raw["bookmaker_identity"],
            source_identity=raw["source_identity"],
            source_timestamp=_package_timestamp(
                raw["source_timestamp"], "binding.source_timestamp"
            ),
            captured_at=_package_timestamp(raw["captured_at"], "binding.captured_at"),
            raw_response_digest=raw["raw_response_digest"],
            provider_record_digest=raw["provider_record_digest"],
            observation_digest=raw["observation_digest"],
            normalized_record_digest=raw["normalized_record_digest"],
            cascade_evidence_digest=raw["cascade_evidence_digest"],
            capture_attestation_digest=raw["capture_attestation_digest"],
            adapter_version=raw["adapter_version"],
            adapter_source_sha=raw["adapter_source_sha"],
            configuration_digest=raw["configuration_digest"],
            home_odds=raw["home_odds"],
            draw_odds=raw["draw_odds"],
            away_odds=raw["away_odds"],
            quota_before=raw["quota_before"],
            quota_after=raw["quota_after"],
            quota_cost_units=raw["quota_cost_units"],
            datapoint_count=raw["datapoint_count"],
            rate_limit_remaining=raw["rate_limit_remaining"],
            rate_limit_reset_at=_package_timestamp(
                raw["rate_limit_reset_at"], "binding.rate_limit_reset_at"
            ),
            account_tier=raw["account_tier"],
            provider_delay_seconds=raw["provider_delay_seconds"],
            http_status=raw["http_status"],
            retry_count=raw["retry_count"],
            evidence_kind=raw["evidence_kind"],
            network_execution=raw["network_execution"],
            no_bet=raw["no_bet"],
            publication=raw["publication"],
            production_activation=raw["production_activation"],
            monetary_spend_authorized=raw["monetary_spend_authorized"],
            source_artifact_digests=tuple(raw["source_artifact_digests"]),
            receipt_id=raw["receipt_id"],
            receipt_digest=raw["receipt_digest"],
            qualification_result_digest=raw["qualification_result_digest"],
        )
        binding.validate()
        return binding


@dataclass(frozen=True)
class Builder2FiveLeagueAuthorityInputDossierV1:
    """Immutable, non-authorizing handoff dossier for Builder 3."""

    schema_version: str
    dossier_id: str
    dossier_digest: str
    leagues: tuple[str, ...]
    provider_identity: str
    candidate_provider_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    ceo_authorization_id: str
    configuration_digest: str
    adapter_source_sha: str
    source_artifact_digests: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    receipt_digests: tuple[str, ...]
    qualification_result_digests: tuple[str, ...]
    bindings: tuple[Builder2FiveLeagueReceiptBindingV1, ...]
    generation_timestamp: datetime
    authority_granted: bool = False
    provider_authority_selected: bool = False
    publication: bool = False
    production_activation: bool = False
    betting: bool = False

    def _payload(self, *, include_identity: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "leagues": list(self.leagues),
            "provider_identity": self.provider_identity,
            "candidate_provider_identity": self.candidate_provider_identity,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "configuration_digest": self.configuration_digest,
            "adapter_source_sha": self.adapter_source_sha,
            "source_artifact_digests": list(self.source_artifact_digests),
            "receipt_ids": list(self.receipt_ids),
            "receipt_digests": list(self.receipt_digests),
            "qualification_result_digests": list(self.qualification_result_digests),
            "bindings": [binding.as_payload() for binding in self.bindings],
            "generation_timestamp": self.generation_timestamp.astimezone(
                timezone.utc
            ).isoformat(),
            "authority_granted": self.authority_granted,
            "provider_authority_selected": self.provider_authority_selected,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "betting": self.betting,
        }
        if include_identity:
            payload["dossier_id"] = self.dossier_id
            payload["dossier_digest"] = self.dossier_digest
        return payload

    def validate(self) -> None:
        if self.schema_version != FIVE_LEAGUE_AUTHORITY_DOSSIER_SCHEMA_VERSION:
            raise Builder2QualificationBatchError(
                "unsupported authority dossier schema"
            )
        if self.leagues != TOP5_LEAGUES:
            raise Builder2QualificationBatchError(
                "authority dossier league order is invalid"
            )
        if self.provider_identity not in CANDIDATE_PROVIDER_IDENTITIES:
            raise Builder2QualificationBatchError(
                "authority dossier provider is not candidate-only"
            )
        if self.candidate_provider_identity != self.provider_identity:
            raise Builder2QualificationBatchError(
                "candidate/provider identity mismatch"
            )
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("ceo_authorization_id", self.ceo_authorization_id),
        ):
            _text(value, f"dossier.{name}")
        for name, value in (
            ("configuration_digest", self.configuration_digest),
            ("adapter_source_sha", self.adapter_source_sha),
        ):
            _digest(value, f"dossier.{name}")
        if len(self.bindings) != len(TOP5_LEAGUES):
            raise Builder2QualificationBatchError(
                "authority dossier must have five bindings"
            )
        if tuple(binding.league for binding in self.bindings) != TOP5_LEAGUES:
            raise Builder2QualificationBatchError(
                "authority dossier bindings are not canonical"
            )
        for binding in self.bindings:
            binding.validate()
            if binding.provider_identity != self.provider_identity:
                raise Builder2QualificationBatchError(
                    "provider identity drift in dossier"
                )
            if binding.configuration_digest != self.configuration_digest:
                raise Builder2QualificationBatchError(
                    "configuration digest drift in dossier"
                )
            if binding.adapter_source_sha.lower() != self.adapter_source_sha.lower():
                raise Builder2QualificationBatchError(
                    "adapter source SHA drift in dossier"
                )
        expected_ids = tuple(binding.receipt_id for binding in self.bindings)
        expected_digests = tuple(binding.receipt_digest for binding in self.bindings)
        expected_results = tuple(
            binding.qualification_result_digest for binding in self.bindings
        )
        if self.receipt_ids != expected_ids or self.receipt_digests != expected_digests:
            raise Builder2QualificationBatchError(
                "receipt identity projection drift in dossier"
            )
        if self.qualification_result_digests != expected_results:
            raise Builder2QualificationBatchError(
                "qualification-result projection drift in dossier"
            )
        if tuple(sorted(self.source_artifact_digests)) != self.source_artifact_digests:
            raise Builder2QualificationBatchError(
                "dossier source provenance is not sorted"
            )
        if not self.source_artifact_digests:
            raise Builder2QualificationBatchError(
                "dossier source provenance is missing"
            )
        _package_timestamp(self.generation_timestamp, "dossier.generation_timestamp")
        if any(
            value is not expected
            for value, expected in (
                (self.authority_granted, False),
                (self.provider_authority_selected, False),
                (self.publication, False),
                (self.production_activation, False),
                (self.betting, False),
            )
        ):
            raise Builder2QualificationBatchError(
                "authority dossier contains an unsafe flag"
            )
        expected_digest = semantic_digest(self._payload(include_identity=False))
        _digest(self.dossier_digest, "dossier_digest")
        if self.dossier_digest.lower() != expected_digest:
            raise Builder2QualificationBatchError("authority dossier digest mismatch")
        if self.dossier_id != f"b2aid-{expected_digest[:24]}":
            raise Builder2QualificationBatchError(
                "authority dossier ID is not deterministic"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_identity=True)

    @classmethod
    def from_payload(cls, payload: object) -> Builder2FiveLeagueAuthorityInputDossierV1:
        required = frozenset(
            {
                "schema_version",
                "dossier_id",
                "dossier_digest",
                "leagues",
                "provider_identity",
                "candidate_provider_identity",
                "controlled_shadow_run_id",
                "qualification_session_id",
                "ceo_authorization_id",
                "configuration_digest",
                "adapter_source_sha",
                "source_artifact_digests",
                "receipt_ids",
                "receipt_digests",
                "qualification_result_digests",
                "bindings",
                "generation_timestamp",
                "authority_granted",
                "provider_authority_selected",
                "publication",
                "production_activation",
                "betting",
            }
        )
        raw = _package_mapping(payload, required=required, name="authority dossier")
        bindings = tuple(
            Builder2FiveLeagueReceiptBindingV1.from_payload(item)
            for item in raw["bindings"]
        )
        dossier = cls(
            schema_version=raw["schema_version"],
            dossier_id=raw["dossier_id"],
            dossier_digest=raw["dossier_digest"],
            leagues=tuple(raw["leagues"]),
            provider_identity=raw["provider_identity"],
            candidate_provider_identity=raw["candidate_provider_identity"],
            controlled_shadow_run_id=raw["controlled_shadow_run_id"],
            qualification_session_id=raw["qualification_session_id"],
            ceo_authorization_id=raw["ceo_authorization_id"],
            configuration_digest=raw["configuration_digest"],
            adapter_source_sha=raw["adapter_source_sha"],
            source_artifact_digests=tuple(raw["source_artifact_digests"]),
            receipt_ids=tuple(raw["receipt_ids"]),
            receipt_digests=tuple(raw["receipt_digests"]),
            qualification_result_digests=tuple(raw["qualification_result_digests"]),
            bindings=bindings,
            generation_timestamp=_package_timestamp(
                raw["generation_timestamp"], "dossier.generation_timestamp"
            ),
            authority_granted=raw["authority_granted"],
            provider_authority_selected=raw["provider_authority_selected"],
            publication=raw["publication"],
            production_activation=raw["production_activation"],
            betting=raw["betting"],
        )
        dossier.validate()
        return dossier


def _build_five_league_dossier(
    package: Builder2FiveLeagueShadowPackageV1,
    results: Sequence[Builder2QualificationIntakeResultV1],
) -> Builder2FiveLeagueAuthorityInputDossierV1:
    if len(results) != len(TOP5_LEAGUES):
        raise Builder2QualificationBatchError(
            "B2 receipt derivation did not produce five results"
        )
    captures = {
        capture.target.league: capture for capture in package.shadow_run.captures
    }
    manifests = {
        manifest.observation.league: manifest for manifest in package.manifests
    }
    bindings: list[Builder2FiveLeagueReceiptBindingV1] = []
    for league, result in zip(TOP5_LEAGUES, results, strict=True):
        receipt = result.receipt
        if receipt is None:
            raise Builder2QualificationBatchError(
                f"missing canonical receipt for {league}"
            )
        capture = captures[league]
        manifest = manifests[league]
        response = capture.response
        required_response_fields = (
            response.source_timestamp,
            response.captured_at,
            response.rate_limit_reset_at,
            response.quota_before,
            response.quota_after,
            response.rate_limit_remaining,
            response.home_odds,
            response.draw_odds,
            response.away_odds,
            response.provider_delay_seconds,
            response.http_status,
        )
        if any(value is None for value in required_response_fields):
            raise Builder2QualificationBatchError(
                f"missing canonical response provenance for {league}"
            )
        binding = Builder2FiveLeagueReceiptBindingV1(
            league=league,
            provider_identity=manifest.provider_identity,
            fixture_key=manifest.fixture_key,
            provider_event_id=manifest.provider_event_id,
            provider_request_id=manifest.provider_request_id,
            observation_id=manifest.observation_id,
            home_team=response.home_team,
            away_team=response.away_team,
            home_participant_id=response.home_participant_id,
            away_participant_id=response.away_participant_id,
            kickoff=capture.target.kickoff,
            bookmaker_identity=response.bookmaker_identity,
            source_identity=response.source_identity,
            source_timestamp=response.source_timestamp,
            captured_at=response.captured_at,
            raw_response_digest=response.raw_response_digest,
            provider_record_digest=response.provider_record_digest,
            observation_digest=manifest.observation_digest,
            normalized_record_digest=manifest.normalized_record_digest,
            cascade_evidence_digest=manifest.cascade_evidence_digest,
            capture_attestation_digest=manifest.capture_attestation_digest,
            adapter_version=manifest.adapter_version,
            adapter_source_sha=manifest.adapter_source_sha,
            configuration_digest=capture.request.configuration_digest,
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            quota_before=response.quota_before,
            quota_after=response.quota_after,
            quota_cost_units=response.quota_cost_units,
            datapoint_count=response.datapoint_count,
            rate_limit_remaining=response.rate_limit_remaining,
            rate_limit_reset_at=response.rate_limit_reset_at,
            account_tier=response.account_tier,
            provider_delay_seconds=response.provider_delay_seconds,
            http_status=response.http_status,
            retry_count=response.retry_count,
            evidence_kind=ObservationEvidenceKind(response.evidence_kind).value,
            network_execution=response.network_execution,
            no_bet=response.no_bet,
            publication=response.publication,
            production_activation=response.production_activation,
            monetary_spend_authorized=response.monetary_spend_authorized,
            source_artifact_digests=tuple(
                sorted(
                    f"{artifact.role}:{artifact.digest}"
                    for artifact in manifest.source_artifacts
                )
            ),
            receipt_id=receipt.qualification_receipt_id,
            receipt_digest=receipt.receipt_digest,
            qualification_result_digest=receipt.qualification_result_digest,
        )
        binding.validate()
        bindings.append(binding)
    provider_identities = {manifest.provider_identity for manifest in package.manifests}
    configuration_digests = {binding.configuration_digest for binding in bindings}
    source_shas = {binding.adapter_source_sha.lower() for binding in bindings}
    if (
        len(provider_identities) != 1
        or not provider_identities <= CANDIDATE_PROVIDER_IDENTITIES
    ):
        raise Builder2QualificationBatchError(
            "five-league provider identity is not canonical candidate-only"
        )
    if len(configuration_digests) != 1:
        raise Builder2QualificationBatchError(
            "five-league configuration digest is inconsistent"
        )
    if len(source_shas) != 1:
        raise Builder2QualificationBatchError(
            "five-league adapter source SHA is inconsistent"
        )
    captured_at = [
        capture.response.captured_at for capture in package.shadow_run.captures
    ]
    if any(value is None for value in captured_at):
        raise Builder2QualificationBatchError(
            "five-league capture timestamp is missing"
        )
    source_artifact_digests = tuple(
        sorted(
            {
                digest
                for binding in bindings
                for digest in binding.source_artifact_digests
            }
        )
    )
    dossier = Builder2FiveLeagueAuthorityInputDossierV1(
        schema_version=FIVE_LEAGUE_AUTHORITY_DOSSIER_SCHEMA_VERSION,
        dossier_id="",
        dossier_digest="",
        leagues=TOP5_LEAGUES,
        provider_identity=next(iter(provider_identities)),
        candidate_provider_identity=next(iter(provider_identities)),
        controlled_shadow_run_id=package.shadow_run.controlled_shadow_run_id,
        qualification_session_id=package.shadow_run.qualification_session_id,
        ceo_authorization_id=package.shadow_run.authorization_id,
        configuration_digest=next(iter(configuration_digests)),
        adapter_source_sha=next(iter(source_shas)),
        source_artifact_digests=source_artifact_digests,
        receipt_ids=tuple(binding.receipt_id for binding in bindings),
        receipt_digests=tuple(binding.receipt_digest for binding in bindings),
        qualification_result_digests=tuple(
            binding.qualification_result_digest for binding in bindings
        ),
        bindings=tuple(bindings),
        generation_timestamp=max(captured_at),
    )
    expected_digest = semantic_digest(dossier._payload(include_identity=False))
    return replace(
        dossier,
        dossier_id=f"b2aid-{expected_digest[:24]}",
        dossier_digest=expected_digest,
    )


@dataclass(frozen=True)
class Builder2FiveLeagueReceiptPackageV1:
    """Atomic five-receipt output plus the non-authorizing B3 dossier."""

    schema_version: str
    package_id: str
    package_digest: str
    source_package_id: str
    source_package_digest: str
    receipts: tuple[Builder2QualificationReceiptV1, ...]
    dossier: Builder2FiveLeagueAuthorityInputDossierV1
    safety: Mapping[str, bool] = field(
        default_factory=lambda: dict(_FIVE_LEAGUE_SAFETY)
    )

    def _payload(self, *, include_identity: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_package_id": self.source_package_id,
            "source_package_digest": self.source_package_digest,
            "receipts": [receipt.as_payload() for receipt in self.receipts],
            "dossier": self.dossier.as_payload(),
            "safety": dict(sorted(self.safety.items())),
        }
        if include_identity:
            payload["package_id"] = self.package_id
            payload["package_digest"] = self.package_digest
        return payload

    def validate(self) -> None:
        if self.schema_version != FIVE_LEAGUE_RECEIPT_PACKAGE_SCHEMA_VERSION:
            raise Builder2QualificationBatchError(
                "unsupported five-league receipt package schema"
            )
        _text(self.source_package_id, "source_package_id")
        _digest(self.source_package_digest, "source_package_digest")
        if len(self.receipts) != len(TOP5_LEAGUES):
            raise Builder2QualificationBatchError(
                "receipt package must contain exactly five receipts"
            )
        for receipt in self.receipts:
            receipt.validate()
        self.dossier.validate()
        if tuple(binding.receipt_id for binding in self.dossier.bindings) != tuple(
            receipt.qualification_receipt_id for receipt in self.receipts
        ):
            raise Builder2QualificationBatchError(
                "receipt package/dossier identity mismatch"
            )
        if dict(self.safety) != _FIVE_LEAGUE_SAFETY:
            raise Builder2QualificationBatchError(
                "receipt package safety state is unsafe"
            )
        expected_digest = semantic_digest(self._payload(include_identity=False))
        _digest(self.package_digest, "receipt package_digest")
        if self.package_digest.lower() != expected_digest:
            raise Builder2QualificationBatchError("receipt package digest mismatch")
        if self.package_id != f"b2b5rp-{expected_digest[:24]}":
            raise Builder2QualificationBatchError(
                "receipt package ID is not deterministic"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_identity=True)

    @classmethod
    def from_payload(cls, payload: object) -> Builder2FiveLeagueReceiptPackageV1:
        required = frozenset(
            {
                "schema_version",
                "package_id",
                "package_digest",
                "source_package_id",
                "source_package_digest",
                "receipts",
                "dossier",
                "safety",
            }
        )
        raw = _package_mapping(
            payload, required=required, name="five-league receipt package"
        )
        receipts_raw = raw["receipts"]
        if not isinstance(receipts_raw, Sequence) or isinstance(
            receipts_raw, (str, bytes)
        ):
            raise Builder2QualificationBatchError(
                "receipt package receipts must be a list"
            )
        package = cls(
            schema_version=raw["schema_version"],
            package_id=raw["package_id"],
            package_digest=raw["package_digest"],
            source_package_id=raw["source_package_id"],
            source_package_digest=raw["source_package_digest"],
            receipts=tuple(
                item
                if isinstance(item, Builder2QualificationReceiptV1)
                else Builder2QualificationReceiptV1.from_payload(item)
                for item in receipts_raw
            ),
            dossier=Builder2FiveLeagueAuthorityInputDossierV1.from_payload(
                raw["dossier"]
            ),
            safety=raw["safety"],
        )
        package.validate()
        return package


def consume_five_league_shadow_package(
    package: Builder2FiveLeagueShadowPackageV1,
) -> Builder2FiveLeagueReceiptPackageV1:
    """Atomically derive five B2 receipts and one non-authorizing dossier in memory."""

    package.validate()
    from src.football.top5_b2_shadow_qualification_intake import (
        qualify_five_league_shadow_run,
    )

    results = qualify_five_league_shadow_run(package.shadow_run, package.manifests)
    if len(results) != len(TOP5_LEAGUES) or any(
        result.receipt is None for result in results
    ):
        raise Builder2QualificationBatchError(
            "five-league qualification did not produce five receipts"
        )
    receipts = tuple(result.receipt for result in results)
    if any(receipt is None for receipt in receipts):
        raise Builder2QualificationBatchError("receipt derivation was incomplete")
    dossier = _build_five_league_dossier(package, results)
    output = Builder2FiveLeagueReceiptPackageV1(
        schema_version=FIVE_LEAGUE_RECEIPT_PACKAGE_SCHEMA_VERSION,
        package_id="",
        package_digest="",
        source_package_id=package.package_id,
        source_package_digest=package.package_digest,
        receipts=tuple(receipts),
        dossier=dossier,
    )
    expected_digest = semantic_digest(output._payload(include_identity=False))
    output = replace(
        output,
        package_id=f"b2b5rp-{expected_digest[:24]}",
        package_digest=expected_digest,
    )
    output.validate()
    return output


def write_five_league_receipt_package(
    package: Builder2FiveLeagueReceiptPackageV1,
    *,
    output_json: str | Path,
) -> None:
    """Commit the complete 5/5 result as one atomic, idempotent JSON artifact."""

    package.validate()
    try:
        path = _safe_external_path(output_json, "five-league receipt output")
    except Builder2QualificationIntakeError as exc:
        raise Builder2QualificationBatchError(str(exc)) from exc
    if path.exists():
        existing = Builder2FiveLeagueReceiptPackageV1.from_payload(
            _load_json(path, "existing five-league receipt output")
        )
        if existing.package_digest != package.package_digest:
            raise Builder2QualificationBatchError(
                "receipt output path contains a conflicting prior package"
            )
        return
    atomic_write_json(
        path,
        package.as_payload(),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )


def run_five_league_receipt_pipeline(
    input_path: str | Path,
    *,
    output_json: str | Path | None = None,
) -> Builder2FiveLeagueReceiptPackageV1:
    """Canonical file-backed offline B4 -> B2 one-shot path."""

    package = load_five_league_shadow_package(input_path)
    result = consume_five_league_shadow_package(package)
    if output_json is not None:
        write_five_league_receipt_package(result, output_json=output_json)
    return result


@dataclass
class _CrossItemFindings:
    conflict_indices: set[int]
    reasons_by_index: dict[int, set[str]]
    duplicate_run_ids: set[str]
    duplicate_receipt_ids: set[str]
    duplicate_observation_ids: set[str]
    divergent_receipt_ids: set[str]
    divergent_observation_ids: set[str]
    divergent_observation_digests: set[str]
    conflicting_ceo_authorization_ids: set[str]
    conflicting_capture_attestation_digests: set[str]
    mixed_intake_identity_ids: set[str]
    fixture_provider_conflict_keys: set[str]
    replayed_evidence_ids: set[str]


def _new_findings() -> _CrossItemFindings:
    return _CrossItemFindings(
        conflict_indices=set(),
        reasons_by_index=defaultdict(set),
        duplicate_run_ids=set(),
        duplicate_receipt_ids=set(),
        duplicate_observation_ids=set(),
        divergent_receipt_ids=set(),
        divergent_observation_ids=set(),
        divergent_observation_digests=set(),
        conflicting_ceo_authorization_ids=set(),
        conflicting_capture_attestation_digests=set(),
        mixed_intake_identity_ids=set(),
        fixture_provider_conflict_keys=set(),
        replayed_evidence_ids=set(),
    )


def _mark_conflict(
    findings: _CrossItemFindings,
    left: _IdentityRecord,
    right: _IdentityRecord,
    reason: str,
) -> None:
    findings.conflict_indices.update((left.index, right.index))
    findings.reasons_by_index[left.index].add(reason)
    findings.reasons_by_index[right.index].add(reason)


def _cross_item_findings(records: Sequence[_IdentityRecord]) -> _CrossItemFindings:
    findings = _new_findings()
    for position, left in enumerate(records):
        for right in records[position + 1 :]:
            lm = left.manifest
            rm = right.manifest
            same_manifest = left.manifest_digest == right.manifest_digest
            if lm.intake_id == rm.intake_id and not same_manifest:
                findings.mixed_intake_identity_ids.add(lm.intake_id)
                _mark_conflict(findings, left, right, "MIXED_INTAKE_IDENTITY")
            if lm.controlled_shadow_run_id == rm.controlled_shadow_run_id:
                findings.duplicate_run_ids.add(lm.controlled_shadow_run_id)
                if not same_manifest:
                    _mark_conflict(findings, left, right, "DUPLICATE_RUN_ID_CONFLICT")
            if lm.observation_id == rm.observation_id:
                findings.duplicate_observation_ids.add(lm.observation_id)
                if lm.observation_digest != rm.observation_digest:
                    findings.divergent_observation_ids.add(lm.observation_id)
                    findings.divergent_observation_digests.update(
                        (lm.observation_digest, rm.observation_digest)
                    )
                    _mark_conflict(
                        findings, left, right, "DIVERGENT_OBSERVATION_DIGEST"
                    )
                elif left.observation_fingerprint() != right.observation_fingerprint():
                    _mark_conflict(
                        findings, left, right, "DUPLICATE_OBSERVATION_IDENTITY_CONFLICT"
                    )
            if (
                lm.ceo_authorization_id == rm.ceo_authorization_id
                and not same_manifest
                and (
                    lm.controlled_shadow_run_id != rm.controlled_shadow_run_id
                    or lm.qualification_session_id != rm.qualification_session_id
                )
            ):
                findings.conflicting_ceo_authorization_ids.add(lm.ceo_authorization_id)
                _mark_conflict(
                    findings, left, right, "CONFLICTING_CEO_AUTHORIZATION_BINDING"
                )
            if lm.controlled_shadow_run_id == rm.controlled_shadow_run_id:
                if lm.ceo_authorization_id != rm.ceo_authorization_id:
                    findings.conflicting_ceo_authorization_ids.update(
                        (lm.ceo_authorization_id, rm.ceo_authorization_id)
                    )
                    _mark_conflict(
                        findings, left, right, "CONFLICTING_CEO_AUTHORIZATION_BINDING"
                    )
                if lm.capture_attestation_digest != rm.capture_attestation_digest:
                    findings.conflicting_capture_attestation_digests.update(
                        (lm.capture_attestation_digest, rm.capture_attestation_digest)
                    )
                    _mark_conflict(
                        findings, left, right, "CONFLICTING_CAPTURE_ATTESTATION"
                    )
            if (lm.fixture_key, lm.provider_identity) == (
                rm.fixture_key,
                rm.provider_identity,
            ):
                fixture_provider = f"{lm.fixture_key}|{lm.provider_identity}"
                if (
                    not same_manifest
                    and left.observation_fingerprint()
                    != right.observation_fingerprint()
                ):
                    findings.fixture_provider_conflict_keys.add(fixture_provider)
                    _mark_conflict(
                        findings, left, right, "FIXTURE_PROVIDER_IDENTITY_CONFLICT"
                    )
            if (
                left.receipt is not None
                and right.receipt is not None
                and left.receipt.qualification_receipt_id
                == right.receipt.qualification_receipt_id
            ):
                receipt_id = left.receipt.qualification_receipt_id
                findings.duplicate_receipt_ids.add(receipt_id)
                if left.receipt.receipt_digest != right.receipt.receipt_digest:
                    findings.divergent_receipt_ids.add(receipt_id)
                    _mark_conflict(findings, left, right, "DIVERGENT_RECEIPT_DIGEST")
                elif not same_manifest:
                    findings.replayed_evidence_ids.add(receipt_id)
            if same_manifest:
                findings.replayed_evidence_ids.add(lm.intake_id)
                if (
                    left.receipt
                    and right.receipt
                    and left.receipt.receipt_digest != right.receipt.receipt_digest
                ):
                    findings.divergent_receipt_ids.add(
                        left.receipt.qualification_receipt_id
                    )
                    _mark_conflict(findings, left, right, "DIVERGENT_RECEIPT_DIGEST")
    return findings


def _expected_fixture(
    manifest: Builder2QualificationIntakeManifestV1,
) -> ExpectedCascadeFixture:
    return ExpectedCascadeFixture(
        league=manifest.observation.league,
        fixture_key=manifest.fixture_key,
        home_team=manifest.observation.home_team,
        away_team=manifest.observation.away_team,
        kickoff=manifest.observation.kickoff,
    )


def _qualify_manifest(
    manifest: Builder2QualificationIntakeManifestV1,
) -> ProviderQualificationReport:
    """Call the existing pure qualification gate with manifest context."""

    return qualify_provider_observations(
        (manifest.observation,),
        manifest.session,
        _expected_fixture(manifest),
        manifest.timing_policy,
        manifest.provider_readiness,
        manifest.authorization,
    )


def _validate_supplied_report(
    supplied: ProviderQualificationReport | Mapping[str, object] | None,
    derived: ProviderQualificationReport,
) -> None:
    if supplied is None:
        return
    if isinstance(supplied, ProviderQualificationReport):
        supplied_digest = _report_digest(supplied)
    elif isinstance(supplied, Mapping):
        # The accepted Decision Packet parser is the canonical strict parser
        # for serialized ProviderQualificationReport projections.
        probe = build_builder2_qualification_decision_packet(
            qualification_reports=(supplied,)
        )
        supplied_digest = probe.qualification_report_digests[0]
    else:
        raise Builder2QualificationBatchError("qualification report is invalid")
    if supplied_digest != _report_digest(derived):
        raise Builder2QualificationBatchError(
            "qualification report digest does not match captured evidence"
        )


def _status_for_failure(
    observation: RealProviderObservation,
    failure_codes: Sequence[str],
) -> str:
    codes = set(failure_codes)
    if observation.evidence_kind != ObservationEvidenceKind.REAL_OBSERVED:
        return NON_REAL_EVIDENCE
    if "EVIDENCE_NOT_REAL" in codes:
        return NON_REAL_EVIDENCE
    if any("DIGEST" in code or "NORMALIZED" in code for code in codes):
        return DIGEST_MISMATCH
    if any(
        code.startswith("AUTHORIZATION")
        or code
        in {
            "WRONG_FIXTURE",
            "WRONG_LEAGUE",
            "TEAM_ALIAS_MISMATCH",
            "HOME_AWAY_INVERSION",
        }
        for code in codes
    ):
        return IDENTITY_MISMATCH
    if any(
        code
        in {
            "REAL_PROVENANCE_MISSING",
            "CAPTURE_ATTESTATION_MISSING",
            "CAPTURE_ATTESTATION_INVALID",
            "ADAPTER_PROVENANCE_MISSING",
            "PROVIDER_EVENT_ID_MISSING",
            "PROVIDER_REQUEST_ID_MISSING",
        }
        for code in codes
    ):
        return PROVENANCE_MISMATCH
    if not failure_codes:
        return INCOMPLETE
    return REJECTED


def _item_result(
    item: Builder2QualificationBatchItemV1,
    status: str,
    reasons: Iterable[str],
    *,
    report: ProviderQualificationReport | None = None,
    receipt: Builder2QualificationReceiptV1 | None = None,
    failure_codes: Iterable[str] = (),
) -> Builder2QualificationBatchItemResultV1:
    manifest = item.manifest
    observation = manifest.observation
    result = Builder2QualificationBatchItemResultV1(
        intake_id=manifest.intake_id,
        manifest_digest=manifest.manifest_digest,
        status=status,
        reasons=_sorted_unique(reasons, "item reason"),
        controlled_shadow_run_id=manifest.controlled_shadow_run_id,
        qualification_session_id=manifest.qualification_session_id,
        ceo_authorization_id=manifest.ceo_authorization_id,
        fixture_key=manifest.fixture_key,
        league=observation.league,
        provider_identity=manifest.provider_identity,
        provider_event_id=manifest.provider_event_id,
        provider_request_id=manifest.provider_request_id,
        observation_id=manifest.observation_id,
        observation_digest=manifest.observation_digest,
        capture_attestation_digest=manifest.capture_attestation_digest,
        qualification_report_digest=_report_digest(report) if report else None,
        receipt_id=receipt.qualification_receipt_id if receipt else None,
        receipt_digest=receipt.receipt_digest if receipt else None,
        failure_codes=_sorted_unique(failure_codes, "failure code"),
    )
    result.validate()
    return result


def _derived_result_payload(
    manifest: Builder2QualificationIntakeManifestV1,
    report: ProviderQualificationReport,
    receipt: Builder2QualificationReceiptV1,
) -> dict[str, object]:
    return Builder2QualificationIntakeResultV1(
        intake_id=manifest.intake_id,
        manifest_digest=manifest.manifest_digest,
        qualification_report=report,
        receipt=receipt,
        sample_report=None,
        artifact_directory=None,
    ).as_payload()


def _artifact(
    item: Builder2QualificationBatchItemV1,
    *,
    report: ProviderQualificationReport | None,
    receipt: Builder2QualificationReceiptV1 | None,
    result: Mapping[str, object] | None,
    missing: Iterable[str] = (),
) -> Builder2QualificationIntakeArtifactV1:
    artifact = Builder2QualificationIntakeArtifactV1(
        manifest=item.manifest,
        qualification_report=report,
        receipt=receipt,
        result=result,
        # Recompute one canonical sample report across the batch. A supplied
        # per-package sample is validated on input but is not another authority.
        sample_report=None,
        artifact_path=item.artifact_path,
        missing_artifacts=_sorted_unique(missing, "missing artifact"),
    )
    artifact.validate()
    return artifact


def _process_item(
    item: Builder2QualificationBatchItemV1,
) -> tuple[
    Builder2QualificationBatchItemResultV1,
    Builder2QualificationIntakeArtifactV1 | None,
    Builder2QualificationReceiptV1 | None,
]:
    manifest = item.manifest
    try:
        manifest.validate()
        if item.sample_report is not None:
            item.sample_report.validate()
        if item.receipt is not None:
            report = _qualify_manifest(manifest)
            _validate_supplied_report(item.qualification_report, report)
            result = next(
                (
                    candidate
                    for candidate in report.results
                    if candidate.observation_id == manifest.observation_id
                ),
                None,
            )
            if result is None or not (result.accepted and result.real_observed):
                codes = tuple(
                    _enum_value(code)
                    for code in (result.failure_codes if result else ())
                )
                status = _status_for_failure(manifest.observation, codes)
                return (
                    _item_result(
                        item,
                        status,
                        (*codes, "EXISTING_RECEIPT_CANNOT_BIND"),
                        report=report,
                        receipt=item.receipt,
                        failure_codes=codes,
                    ),
                    _artifact(
                        item,
                        report=report,
                        receipt=None,
                        result=None,
                        missing=("receipt.json", "result.json"),
                    ),
                    None,
                )
            validate_builder2_qualification_receipt(
                item.receipt,
                expected_observation=manifest.observation,
                expected_report=report,
                expected_result=result,
                expected_authorization=manifest.authorization,
                expected_cascade_evidence=manifest.cascade_evidence,
                expected_capture_attestation=manifest.capture_attestation,
            )
            derived_result = item.result or _derived_result_payload(
                manifest, report, item.receipt
            )
            artifact = _artifact(
                item,
                report=report,
                receipt=item.receipt,
                result=derived_result,
            )
            return (
                _item_result(
                    item,
                    ALREADY_QUALIFIED,
                    (ALREADY_QUALIFIED,),
                    report=report,
                    receipt=item.receipt,
                ),
                artifact,
                item.receipt,
            )

        try:
            intake_result = validate_intake(manifest)
            report = intake_result.qualification_report
            receipt = intake_result.receipt
            return (
                _item_result(
                    item, QUALIFIED, (QUALIFIED,), report=report, receipt=receipt
                ),
                _artifact(
                    item,
                    report=report,
                    receipt=receipt,
                    result=item.result or intake_result.as_payload(),
                ),
                receipt,
            )
        except Builder2QualificationIntakeError:
            # The operational intake intentionally raises on rejection.  Ask
            # the same pure qualification gate for the exact item failure
            # taxonomy so the batch can retain it without issuing a receipt.
            report = _qualify_manifest(manifest)
            result = next(
                (
                    candidate
                    for candidate in report.results
                    if candidate.observation_id == manifest.observation_id
                ),
                None,
            )
            codes = tuple(
                _enum_value(code) for code in (result.failure_codes if result else ())
            )
            status = _status_for_failure(manifest.observation, codes)
            return (
                _item_result(
                    item, status, codes or (status,), report=report, failure_codes=codes
                ),
                _artifact(
                    item,
                    report=report,
                    receipt=None,
                    result=None,
                    missing=("receipt.json", "result.json"),
                ),
                None,
            )
    except Builder2QualificationReceiptError as exc:
        return (
            _item_result(
                item,
                DIGEST_MISMATCH
                if "digest" in str(exc).lower()
                else PROVENANCE_MISMATCH,
                (_failure_reason(exc),),
            ),
            _artifact(
                item,
                report=None,
                receipt=None,
                result=None,
                missing=("qualification_report.json", "receipt.json", "result.json"),
            ),
            None,
        )
    except (
        Builder2QualificationBatchError,
        Builder2QualificationDecisionPacketError,
        QualificationContractError,
        TypeError,
        ValueError,
    ) as exc:
        reason = _failure_reason(exc)
        lower = reason.lower()
        status = (
            DIGEST_MISMATCH
            if "digest" in lower
            else IDENTITY_MISMATCH
            if "binding" in lower or "identity" in lower
            else FAILED_CLOSED
        )
        return (
            _item_result(item, status, (reason,)),
            _artifact(
                item,
                report=None,
                receipt=None,
                result=None,
                missing=("qualification_report.json", "receipt.json", "result.json"),
            ),
            None,
        )


def _parse_items(
    items: Iterable[
        Builder2QualificationBatchItemV1
        | Builder2QualificationIntakeManifestV1
        | Mapping[str, object]
    ],
) -> tuple[tuple[Builder2QualificationBatchItemV1, ...], tuple[tuple[str, str], ...]]:
    if isinstance(items, (str, bytes, Mapping)):
        raise Builder2QualificationBatchError("batch items must be an iterable")
    try:
        candidates = tuple(items)
    except TypeError as exc:
        raise Builder2QualificationBatchError("batch items must be iterable") from exc
    parsed: list[Builder2QualificationBatchItemV1] = []
    failures: list[tuple[str, str]] = []
    for index, candidate in enumerate(candidates):
        try:
            parsed.append(Builder2QualificationBatchItemV1.from_payload(candidate))
        except (
            Builder2QualificationBatchError,
            QualificationContractError,
            TypeError,
            ValueError,
        ) as exc:
            failures.append((f"invalid-{index}", _failure_reason(exc)))
    parsed.sort(
        key=lambda item: (
            item.manifest_digest,
            0 if item.receipt is not None else 1,
            _canonical_key(item.as_payload()),
        )
    )
    return tuple(parsed), tuple(sorted(failures))


def _dedupe_records(records: Sequence[_IdentityRecord]) -> dict[str, int]:
    primary: dict[str, int] = {}
    for record in records:
        primary.setdefault(record.manifest_digest, record.index)
    return primary


@dataclass(frozen=True)
class Builder2QualificationBatchResultV1:
    """Complete deterministic batch result, including canonical projections."""

    schema_version: str
    batch_id: str
    batch_digest: str
    input_item_count: int
    processed_item_count: int
    accepted_count: int
    rejected_count: int
    items: tuple[Builder2QualificationBatchItemResultV1, ...]
    receipt_ids: tuple[str, ...]
    receipt_digests: tuple[str, ...]
    qualification_session_ids: tuple[str, ...]
    controlled_shadow_run_ids: tuple[str, ...]
    ceo_authorization_ids: tuple[str, ...]
    per_provider_counts: Mapping[str, int]
    per_league_counts: Mapping[str, int]
    duplicate_run_ids: tuple[str, ...]
    duplicate_receipt_ids: tuple[str, ...]
    duplicate_observation_ids: tuple[str, ...]
    divergent_receipt_ids: tuple[str, ...]
    divergent_observation_ids: tuple[str, ...]
    divergent_observation_digests: tuple[str, ...]
    conflicting_ceo_authorization_ids: tuple[str, ...]
    conflicting_capture_attestation_digests: tuple[str, ...]
    mixed_intake_identity_ids: tuple[str, ...]
    fixture_provider_conflict_keys: tuple[str, ...]
    replayed_evidence_ids: tuple[str, ...]
    failure_taxonomy: Mapping[str, int]
    sample_report: Builder2QualificationSampleReportV1
    decision_packet: Builder2QualificationDecisionPacketV1
    unresolved_items: tuple[str, ...]
    safety: Mapping[str, bool]

    @property
    def item_results(self) -> tuple[Builder2QualificationBatchItemResultV1, ...]:
        return self.items

    @property
    def sample_sufficient(self) -> bool | None:
        return self.sample_report.sample_sufficient

    def _payload(self, *, include_identity: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "input_item_count": self.input_item_count,
            "processed_item_count": self.processed_item_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "items": [item.as_payload() for item in self.items],
            "receipt_ids": list(self.receipt_ids),
            "receipt_digests": list(self.receipt_digests),
            "qualification_session_ids": list(self.qualification_session_ids),
            "controlled_shadow_run_ids": list(self.controlled_shadow_run_ids),
            "ceo_authorization_ids": list(self.ceo_authorization_ids),
            "per_provider_counts": dict(sorted(self.per_provider_counts.items())),
            "per_league_counts": dict(sorted(self.per_league_counts.items())),
            "duplicate_run_ids": list(self.duplicate_run_ids),
            "duplicate_receipt_ids": list(self.duplicate_receipt_ids),
            "duplicate_observation_ids": list(self.duplicate_observation_ids),
            "divergent_receipt_ids": list(self.divergent_receipt_ids),
            "divergent_observation_ids": list(self.divergent_observation_ids),
            "divergent_observation_digests": list(self.divergent_observation_digests),
            "conflicting_ceo_authorization_ids": list(
                self.conflicting_ceo_authorization_ids
            ),
            "conflicting_capture_attestation_digests": list(
                self.conflicting_capture_attestation_digests
            ),
            "mixed_intake_identity_ids": list(self.mixed_intake_identity_ids),
            "fixture_provider_conflict_keys": list(self.fixture_provider_conflict_keys),
            "replayed_evidence_ids": list(self.replayed_evidence_ids),
            "failure_taxonomy": dict(sorted(self.failure_taxonomy.items())),
            "sample_report": self.sample_report.as_payload(),
            "decision_packet": self.decision_packet.as_payload(),
            "unresolved_items": list(self.unresolved_items),
            "safety": dict(sorted(self.safety.items())),
        }
        if include_identity:
            payload["batch_id"] = self.batch_id
            payload["batch_digest"] = self.batch_digest
        return payload

    def validate(self) -> None:
        if self.schema_version != BATCH_SCHEMA_VERSION:
            raise Builder2QualificationBatchError("unsupported batch schema")
        for name, value in (
            ("input_item_count", self.input_item_count),
            ("processed_item_count", self.processed_item_count),
            ("accepted_count", self.accepted_count),
            ("rejected_count", self.rejected_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Builder2QualificationBatchError(f"{name} must be non-negative")
        if self.processed_item_count > self.input_item_count:
            raise Builder2QualificationBatchError("processed items exceed inputs")
        if self.accepted_count + self.rejected_count != self.processed_item_count:
            raise Builder2QualificationBatchError("batch item counts are inconsistent")
        for item in self.items:
            item.validate()
        item_keys = [
            (
                item.intake_id,
                item.manifest_digest,
                item.status,
                item.as_payload()["reasons"],
            )
            for item in self.items
        ]
        if item_keys != sorted(item_keys):
            raise Builder2QualificationBatchError("batch items are not deterministic")
        for name, values in (
            ("receipt_ids", self.receipt_ids),
            ("receipt_digests", self.receipt_digests),
            ("qualification_session_ids", self.qualification_session_ids),
            ("controlled_shadow_run_ids", self.controlled_shadow_run_ids),
            ("ceo_authorization_ids", self.ceo_authorization_ids),
            ("duplicate_run_ids", self.duplicate_run_ids),
            ("duplicate_receipt_ids", self.duplicate_receipt_ids),
            ("duplicate_observation_ids", self.duplicate_observation_ids),
            ("divergent_receipt_ids", self.divergent_receipt_ids),
            ("divergent_observation_ids", self.divergent_observation_ids),
            ("divergent_observation_digests", self.divergent_observation_digests),
            (
                "conflicting_ceo_authorization_ids",
                self.conflicting_ceo_authorization_ids,
            ),
            (
                "conflicting_capture_attestation_digests",
                self.conflicting_capture_attestation_digests,
            ),
            ("mixed_intake_identity_ids", self.mixed_intake_identity_ids),
            ("fixture_provider_conflict_keys", self.fixture_provider_conflict_keys),
            ("replayed_evidence_ids", self.replayed_evidence_ids),
            ("unresolved_items", self.unresolved_items),
        ):
            if tuple(sorted(set(values))) != values:
                raise Builder2QualificationBatchError(f"{name} is not deterministic")
            for value in values:
                _text(value, name)
        for name, counts in (
            ("per_provider_counts", self.per_provider_counts),
            ("per_league_counts", self.per_league_counts),
            ("failure_taxonomy", self.failure_taxonomy),
        ):
            for key, value in counts.items():
                _text(key, f"{name} key")
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise Builder2QualificationBatchError(f"{name} values are invalid")
        if dict(self.safety) != _BATCH_SAFETY:
            raise Builder2QualificationBatchError("batch safety state is unsafe")
        self.sample_report.validate()
        self.decision_packet.validate()
        expected = semantic_digest(self._payload(include_identity=False))
        if self.batch_digest.lower() != expected:
            raise Builder2QualificationBatchError("batch digest mismatch")
        if self.batch_id != f"b2qb-{expected[:24]}":
            raise Builder2QualificationBatchError("batch ID is not deterministic")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_identity=True)

    def as_markdown(self) -> str:
        self.validate()
        lines = [
            "# Top-5 B2 Qualification Batch Orchestrator",
            "",
            f"- Batch: `{self.batch_id}`",
            f"- Digest: `{self.batch_digest}`",
            f"- Items: `{self.input_item_count}` input / `{self.processed_item_count}` processed",
            f"- Accepted: `{self.accepted_count}`",
            f"- Rejected: `{self.rejected_count}`",
            f"- Receipts: `{', '.join(self.receipt_ids) or 'none'}`",
            f"- Sample sufficient: `{self.sample_sufficient}` (caller policy only)",
            f"- Decision Packet: `{self.decision_packet.packet_id}`",
            "",
            "## Item outcomes",
            "",
        ]
        lines.extend(
            f"- `{item.intake_id}` — `{item.status}` — {', '.join(item.reasons) or 'none'}"
            for item in self.items
        )
        lines.extend(
            [
                "",
                "## Conflicts and unresolved items",
                "",
                f"- Duplicate runs: `{', '.join(self.duplicate_run_ids) or 'none'}`",
                f"- Duplicate observations: `{', '.join(self.duplicate_observation_ids) or 'none'}`",
                f"- Divergent receipts: `{', '.join(self.divergent_receipt_ids) or 'none'}`",
                f"- Fixture/provider conflicts: `{', '.join(self.fixture_provider_conflict_keys) or 'none'}`",
                f"- Unresolved: `{', '.join(self.unresolved_items) or 'none'}`",
                "",
                "## Safety",
                "",
                "- Evidence processing is deterministic and validation-only.",
                "- No provider/network execution, publication, betting, model binding, or production authority is created.",
                "- Sample sufficiency is measurement only; CEO authorization remains required.",
                "",
            ]
        )
        return "\n".join(lines)

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationBatchResultV1:
        if not isinstance(payload, Mapping):
            raise Builder2QualificationBatchError("batch result must be an object")
        raw = dict(payload)
        required = {
            "schema_version",
            "batch_id",
            "batch_digest",
            "input_item_count",
            "processed_item_count",
            "accepted_count",
            "rejected_count",
            "items",
            "receipt_ids",
            "receipt_digests",
            "qualification_session_ids",
            "controlled_shadow_run_ids",
            "ceo_authorization_ids",
            "per_provider_counts",
            "per_league_counts",
            "duplicate_run_ids",
            "duplicate_receipt_ids",
            "duplicate_observation_ids",
            "divergent_receipt_ids",
            "divergent_observation_ids",
            "divergent_observation_digests",
            "conflicting_ceo_authorization_ids",
            "conflicting_capture_attestation_digests",
            "mixed_intake_identity_ids",
            "fixture_provider_conflict_keys",
            "replayed_evidence_ids",
            "failure_taxonomy",
            "sample_report",
            "decision_packet",
            "unresolved_items",
            "safety",
        }
        if set(raw) != required:
            raise Builder2QualificationBatchError(
                f"batch result fields mismatch: {sorted(set(raw) ^ required)}"
            )
        result = cls(
            schema_version=raw["schema_version"],
            batch_id=raw["batch_id"],
            batch_digest=raw["batch_digest"],
            input_item_count=raw["input_item_count"],
            processed_item_count=raw["processed_item_count"],
            accepted_count=raw["accepted_count"],
            rejected_count=raw["rejected_count"],
            items=tuple(
                Builder2QualificationBatchItemResultV1.from_payload(item)
                for item in raw["items"]
            ),
            receipt_ids=tuple(raw["receipt_ids"]),
            receipt_digests=tuple(raw["receipt_digests"]),
            qualification_session_ids=tuple(raw["qualification_session_ids"]),
            controlled_shadow_run_ids=tuple(raw["controlled_shadow_run_ids"]),
            ceo_authorization_ids=tuple(raw["ceo_authorization_ids"]),
            per_provider_counts=raw["per_provider_counts"],
            per_league_counts=raw["per_league_counts"],
            duplicate_run_ids=tuple(raw["duplicate_run_ids"]),
            duplicate_receipt_ids=tuple(raw["duplicate_receipt_ids"]),
            duplicate_observation_ids=tuple(raw["duplicate_observation_ids"]),
            divergent_receipt_ids=tuple(raw["divergent_receipt_ids"]),
            divergent_observation_ids=tuple(raw["divergent_observation_ids"]),
            divergent_observation_digests=tuple(raw["divergent_observation_digests"]),
            conflicting_ceo_authorization_ids=tuple(
                raw["conflicting_ceo_authorization_ids"]
            ),
            conflicting_capture_attestation_digests=tuple(
                raw["conflicting_capture_attestation_digests"]
            ),
            mixed_intake_identity_ids=tuple(raw["mixed_intake_identity_ids"]),
            fixture_provider_conflict_keys=tuple(raw["fixture_provider_conflict_keys"]),
            replayed_evidence_ids=tuple(raw["replayed_evidence_ids"]),
            failure_taxonomy=raw["failure_taxonomy"],
            sample_report=Builder2QualificationSampleReportV1.from_payload(
                raw["sample_report"]
            ),
            decision_packet=Builder2QualificationDecisionPacketV1.from_payload(
                raw["decision_packet"]
            ),
            unresolved_items=tuple(raw["unresolved_items"]),
            safety=raw["safety"],
        )
        result.validate()
        return result


def _load_json(path: Path, name: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Builder2QualificationBatchError(f"cannot read {name}") from exc
    if not isinstance(raw, Mapping):
        raise Builder2QualificationBatchError(f"{name} must contain an object")
    return dict(raw)


def load_builder2_qualification_batch_inputs(
    path: str | Path,
) -> tuple[Builder2QualificationBatchItemV1, ...]:
    """Read complete external capture/intake package directories, read-only."""

    candidate = _safe_external_path(path, "batch input", directory=False)
    if candidate.is_file():
        raw = _load_json(candidate, "batch input")
        if "items" in raw:
            items_raw = raw["items"]
            if not isinstance(items_raw, Sequence) or isinstance(
                items_raw, (str, bytes)
            ):
                raise Builder2QualificationBatchError(
                    "batch input items must be a list"
                )
            return tuple(
                Builder2QualificationBatchItemV1.from_payload(item)
                for item in items_raw
            )
        return (Builder2QualificationBatchItemV1.from_payload(raw),)
    if not candidate.exists() or not candidate.is_dir():
        raise Builder2QualificationBatchError("batch input directory does not exist")
    manifest_paths = sorted(candidate.rglob("manifest.json"))
    if not manifest_paths:
        raise Builder2QualificationBatchError(
            "batch input contains no canonical manifests"
        )
    allowed = {
        "manifest.json",
        "qualification_report.json",
        "receipt.json",
        "result.json",
        "sample_report.json",
    }
    orphan_files = sorted(
        path.name
        for path in candidate.rglob("*.json")
        if path.name in allowed - {"manifest.json"}
        and not (path.parent / "manifest.json").exists()
    )
    if orphan_files:
        raise Builder2QualificationBatchError(
            f"orphan intake artifacts have no manifest: {orphan_files}"
        )
    items: list[Builder2QualificationBatchItemV1] = []
    for manifest_path in manifest_paths:
        parent = manifest_path.parent
        unexpected = sorted(
            item.name for item in parent.glob("*.json") if item.name not in allowed
        )
        if unexpected:
            raise Builder2QualificationBatchError(
                f"unsupported intake artifact(s): {unexpected}"
            )
        package: dict[str, object] = {
            "manifest": _load_json(manifest_path, "manifest"),
            "artifact_path": str(parent),
        }
        for filename, payload_field in (
            ("qualification_report.json", "qualification_report"),
            ("receipt.json", "receipt"),
            ("result.json", "result"),
            ("sample_report.json", "sample_report"),
        ):
            file_path = parent / filename
            if file_path.exists():
                package[payload_field] = _load_json(file_path, filename)
        items.append(Builder2QualificationBatchItemV1.from_payload(package))
    return tuple(items)


def _invalid_item_result(
    item_id: str, reason: str
) -> Builder2QualificationBatchItemResultV1:
    digest = semantic_digest({"invalid_item": item_id, "reason": reason})
    status = NON_REAL_EVIDENCE if "real_observed" in reason.lower() else FAILED_CLOSED
    return Builder2QualificationBatchItemResultV1(
        intake_id=item_id,
        manifest_digest=digest,
        status=status,
        reasons=(reason,),
    )


def orchestrate_builder2_qualification_batch(
    items: Iterable[
        Builder2QualificationBatchItemV1
        | Builder2QualificationIntakeManifestV1
        | Mapping[str, object]
    ],
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
) -> Builder2QualificationBatchResultV1:
    """Process a batch without network execution or source-evidence mutation."""

    if minimum_sample_policy is not None:
        if not isinstance(minimum_sample_policy, MinimumSamplePolicy):
            raise Builder2QualificationBatchError(
                "minimum_sample_policy must be the caller-supplied canonical policy"
            )
        minimum_sample_policy.validate()
    parsed, parse_failures = _parse_items(items)
    records = tuple(_IdentityRecord(index, item) for index, item in enumerate(parsed))
    findings = _cross_item_findings(records)
    primary_by_manifest = _dedupe_records(records)
    item_results: dict[int, Builder2QualificationBatchItemResultV1] = {}
    artifacts: dict[int, Builder2QualificationIntakeArtifactV1] = {}
    receipts: list[Builder2QualificationReceiptV1] = []

    for record in records:
        if record.index in findings.conflict_indices:
            reasons = findings.reasons_by_index.get(record.index, {"CONFLICT"})
            item_results[record.index] = _item_result(record.item, CONFLICT, reasons)
            artifacts[record.index] = _artifact(
                record.item,
                report=None,
                receipt=None,
                result=None,
                missing=("qualification_report.json", "receipt.json", "result.json"),
            )
            continue
        primary_index = primary_by_manifest[record.manifest_digest]
        if primary_index != record.index:
            primary_result = item_results.get(primary_index)
            if primary_result is None:
                continue
            item_results[record.index] = replace(
                primary_result,
                status=ALREADY_QUALIFIED,
                reasons=(ALREADY_QUALIFIED, "EXACT_EVIDENCE_REPLAY"),
            )
            continue
        result, artifact, receipt = _process_item(record.item)
        item_results[record.index] = result
        if artifact is not None:
            artifacts[record.index] = artifact
        if receipt is not None:
            receipts.append(receipt)

    # Exact malformed input is retained as a failed-closed item and cannot
    # enter the canonical receipt/sample/packet chain.
    for item_id, reason in parse_failures:
        item_results[len(item_results)] = _invalid_item_result(item_id, reason)

    artifact_values = tuple(
        artifacts[index]
        for index in sorted(artifacts)
        if index in item_results and item_results[index].status != CONFLICT
    )
    # The expression above intentionally retains rejected report artifacts but
    # excludes conflict artifacts from canonical receipt input.  Deduplicate
    # exact manifest artifacts before the packet projection.
    unique_artifacts: dict[tuple[str, str], Builder2QualificationIntakeArtifactV1] = {}
    for artifact in artifact_values:
        manifest_key = (
            artifact.manifest.intake_id
            if artifact.manifest
            else artifact.artifact_path,
            artifact.manifest.manifest_digest if artifact.manifest else "",
        )
        unique_artifacts.setdefault(manifest_key, artifact)
    artifact_values = tuple(unique_artifacts[key] for key in sorted(unique_artifacts))
    sample = aggregate_builder2_qualification_samples(
        receipts,
        minimum_sample_policy=minimum_sample_policy,
    )
    packet = build_builder2_qualification_decision_packet(
        intake_artifacts=artifact_values,
        minimum_sample_policy=minimum_sample_policy,
    )
    sorted_items = tuple(
        sorted(
            item_results.values(),
            key=lambda item: (
                item.intake_id,
                item.manifest_digest,
                item.status,
                item.reasons,
            ),
        )
    )
    accepted_items = tuple(item for item in sorted_items if item.accepted)
    receipt_ids = tuple(
        sorted({item.receipt_id for item in accepted_items if item.receipt_id})
    )
    receipt_digests = tuple(
        sorted({item.receipt_digest for item in accepted_items if item.receipt_digest})
    )
    receipt_occurrences = Counter(
        item.receipt_id for item in accepted_items if item.receipt_id
    )
    observation_occurrences = Counter(
        item.observation_id for item in accepted_items if item.observation_id
    )
    duplicate_receipt_ids = set(findings.duplicate_receipt_ids)
    duplicate_receipt_ids.update(
        receipt_id for receipt_id, count in receipt_occurrences.items() if count > 1
    )
    duplicate_observation_ids = set(findings.duplicate_observation_ids)
    duplicate_observation_ids.update(
        observation_id
        for observation_id, count in observation_occurrences.items()
        if count > 1
    )
    failure_taxonomy = Counter()
    for item in sorted_items:
        failure_taxonomy.update(item.failure_codes)
        if item.status not in {QUALIFIED, ALREADY_QUALIFIED}:
            failure_taxonomy[item.status] += 1
    failure_taxonomy.update(
        reason for reasons in findings.reasons_by_index.values() for reason in reasons
    )
    unresolved = set(packet.unresolved_items)
    unresolved.update(findings.divergent_receipt_ids)
    unresolved.update(findings.divergent_observation_ids)
    unresolved.update(findings.fixture_provider_conflict_keys)
    unresolved.update(findings.conflicting_ceo_authorization_ids)
    if findings.conflict_indices:
        unresolved.add("CROSS_ITEM_CONFLICT_REQUIRES_CEO_REVIEW")
    if parse_failures:
        unresolved.add("MALFORMED_ITEM_REQUIRES_CEO_REVIEW")
    batch = Builder2QualificationBatchResultV1(
        schema_version=BATCH_SCHEMA_VERSION,
        batch_id="",
        batch_digest="",
        input_item_count=len(parsed) + len(parse_failures),
        processed_item_count=len(sorted_items),
        accepted_count=len(accepted_items),
        rejected_count=len(sorted_items) - len(accepted_items),
        items=sorted_items,
        receipt_ids=receipt_ids,
        receipt_digests=receipt_digests,
        qualification_session_ids=tuple(
            sorted(
                {
                    item.qualification_session_id
                    for item in accepted_items
                    if item.qualification_session_id
                }
            )
        ),
        controlled_shadow_run_ids=tuple(
            sorted(
                {
                    item.controlled_shadow_run_id
                    for item in accepted_items
                    if item.controlled_shadow_run_id
                }
            )
        ),
        ceo_authorization_ids=tuple(
            sorted(
                {
                    item.ceo_authorization_id
                    for item in accepted_items
                    if item.ceo_authorization_id
                }
            )
        ),
        per_provider_counts=dict(sample.eligible_per_provider_counts),
        per_league_counts=dict(sample.eligible_per_league_counts),
        duplicate_run_ids=tuple(sorted(findings.duplicate_run_ids)),
        duplicate_receipt_ids=tuple(
            sorted(duplicate_receipt_ids | set(sample.duplicate_receipt_ids))
        ),
        duplicate_observation_ids=tuple(
            sorted(duplicate_observation_ids | set(sample.duplicate_observation_ids))
        ),
        divergent_receipt_ids=tuple(
            sorted(
                set(findings.divergent_receipt_ids) | set(sample.divergent_receipt_ids)
            )
        ),
        divergent_observation_ids=tuple(sorted(findings.divergent_observation_ids)),
        divergent_observation_digests=tuple(
            sorted(findings.divergent_observation_digests)
        ),
        conflicting_ceo_authorization_ids=tuple(
            sorted(findings.conflicting_ceo_authorization_ids)
        ),
        conflicting_capture_attestation_digests=tuple(
            sorted(findings.conflicting_capture_attestation_digests)
        ),
        mixed_intake_identity_ids=tuple(sorted(findings.mixed_intake_identity_ids)),
        fixture_provider_conflict_keys=tuple(
            sorted(findings.fixture_provider_conflict_keys)
        ),
        replayed_evidence_ids=tuple(sorted(findings.replayed_evidence_ids)),
        failure_taxonomy=dict(sorted(failure_taxonomy.items())),
        sample_report=sample,
        decision_packet=packet,
        unresolved_items=tuple(sorted(unresolved)),
        safety=dict(_BATCH_SAFETY),
    )
    base = batch._payload(include_identity=False)
    digest = semantic_digest(base)
    batch = Builder2QualificationBatchResultV1(
        **{**batch.__dict__, "batch_id": f"b2qb-{digest[:24]}", "batch_digest": digest}
    )
    batch.validate()
    return batch


def write_batch_outputs(
    result: Builder2QualificationBatchResultV1,
    *,
    output_json: str | Path | None = None,
    output_markdown: str | Path | None = None,
) -> None:
    result.validate()
    if output_json is not None:
        atomic_write_json(
            _safe_external_path(output_json, "output JSON"),
            result.as_payload(),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    if output_markdown is not None:
        atomic_write_text(
            _safe_external_path(output_markdown, "output Markdown"),
            result.as_markdown(),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a no-network Builder-2 qualification batch result."
    )
    parser.add_argument("input_path", nargs="?")
    parser.add_argument(
        "--five-league-input",
        help="one external canonical B4 five-league package JSON",
    )
    parser.add_argument(
        "--five-league-output",
        help="one external atomic B2 five-receipt/dossier JSON output",
    )
    parser.add_argument("--output-json")
    parser.add_argument("--output-markdown")
    parser.add_argument("--minimum-real-observations", type=int)
    parser.add_argument("--minimum-distinct-fixtures", type=int)
    try:
        args = parser.parse_args(argv)
        if args.five_league_input is not None:
            if (
                args.input_path is not None
                or args.output_json is not None
                or args.output_markdown is not None
                or args.minimum_real_observations is not None
                or args.minimum_distinct_fixtures is not None
                or args.five_league_output is None
            ):
                raise Builder2QualificationBatchError(
                    "five-league mode requires only --five-league-input and --five-league-output"
                )
            result = run_five_league_receipt_pipeline(
                args.five_league_input,
                output_json=args.five_league_output,
            )
            print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
            return 0
        if args.input_path is None:
            raise Builder2QualificationBatchError(
                "input_path or --five-league-input is required"
            )
        if (args.minimum_real_observations is None) != (
            args.minimum_distinct_fixtures is None
        ):
            raise Builder2QualificationBatchError(
                "caller sample policy values must be supplied together"
            )
        policy = None
        if args.minimum_real_observations is not None:
            policy = MinimumSamplePolicy(
                args.minimum_real_observations,
                args.minimum_distinct_fixtures,
            )
            policy.validate()
        items = load_builder2_qualification_batch_inputs(args.input_path)
        result = orchestrate_builder2_qualification_batch(
            items,
            minimum_sample_policy=policy,
        )
        write_batch_outputs(
            result,
            output_json=args.output_json,
            output_markdown=args.output_markdown,
        )
        print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
        print(result.as_markdown())
        return 0
    except (
        Builder2QualificationBatchError,
        Builder2QualificationIntakeError,
        Builder2QualificationDecisionPacketError,
        QualificationContractError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": FAILED_CLOSED, "error": str(exc), "safety": _BATCH_SAFETY},
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2


run_builder2_qualification_batch = orchestrate_builder2_qualification_batch
Builder2QualificationBatchResult = Builder2QualificationBatchResultV1
Builder2QualificationBatchItem = Builder2QualificationBatchItemV1
BatchItemStatus = Builder2QualificationBatchItemStatus

__all__ = [
    "ALREADY_QUALIFIED",
    "BATCH_CONTRACT_VERSION",
    "BATCH_SCHEMA_VERSION",
    "CONFLICT",
    "DIGEST_MISMATCH",
    "FAILED_CLOSED",
    "FIVE_LEAGUE_AUTHORITY_DOSSIER_SCHEMA_VERSION",
    "FIVE_LEAGUE_RECEIPT_PACKAGE_SCHEMA_VERSION",
    "FIVE_LEAGUE_SHADOW_PACKAGE_SCHEMA_VERSION",
    "IDENTITY_MISMATCH",
    "INCOMPLETE",
    "NON_REAL_EVIDENCE",
    "PROVENANCE_MISMATCH",
    "QUALIFIED",
    "REJECTED",
    "BatchItemStatus",
    "Builder2FiveLeagueAuthorityInputDossierV1",
    "Builder2FiveLeagueReceiptBindingV1",
    "Builder2FiveLeagueReceiptPackageV1",
    "Builder2FiveLeagueShadowPackageV1",
    "Builder2QualificationBatchError",
    "Builder2QualificationBatchItem",
    "Builder2QualificationBatchItemResultV1",
    "Builder2QualificationBatchItemStatus",
    "Builder2QualificationBatchItemV1",
    "Builder2QualificationBatchResult",
    "Builder2QualificationBatchResultV1",
    "build_five_league_shadow_package",
    "consume_five_league_shadow_package",
    "load_builder2_qualification_batch_inputs",
    "load_five_league_shadow_package",
    "main",
    "orchestrate_builder2_qualification_batch",
    "run_builder2_qualification_batch",
    "run_five_league_receipt_pipeline",
    "write_batch_outputs",
    "write_five_league_receipt_package",
]


if __name__ == "__main__":
    raise SystemExit(main())
