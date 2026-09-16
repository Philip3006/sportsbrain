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
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from src.football.production_contracts import ProductionContractError
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
from src.utils.atomic_io import atomic_write_json, atomic_write_text

BATCH_CONTRACT_VERSION = "top5-b2-qualification-batch-orchestrator-v1"
BATCH_SCHEMA_VERSION = BATCH_CONTRACT_VERSION

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
        for filename, field in (
            ("qualification_report.json", "qualification_report"),
            ("receipt.json", "receipt"),
            ("result.json", "result"),
            ("sample_report.json", "sample_report"),
        ):
            file_path = parent / filename
            if file_path.exists():
                package[field] = _load_json(file_path, filename)
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
    parser.add_argument("input_path")
    parser.add_argument("--output-json")
    parser.add_argument("--output-markdown")
    parser.add_argument("--minimum-real-observations", type=int)
    parser.add_argument("--minimum-distinct-fixtures", type=int)
    try:
        args = parser.parse_args(argv)
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
    "IDENTITY_MISMATCH",
    "INCOMPLETE",
    "NON_REAL_EVIDENCE",
    "PROVENANCE_MISMATCH",
    "QUALIFIED",
    "REJECTED",
    "BatchItemStatus",
    "Builder2QualificationBatchError",
    "Builder2QualificationBatchItem",
    "Builder2QualificationBatchItemResultV1",
    "Builder2QualificationBatchItemStatus",
    "Builder2QualificationBatchItemV1",
    "Builder2QualificationBatchResult",
    "Builder2QualificationBatchResultV1",
    "load_builder2_qualification_batch_inputs",
    "main",
    "orchestrate_builder2_qualification_batch",
    "run_builder2_qualification_batch",
    "write_batch_outputs",
]
