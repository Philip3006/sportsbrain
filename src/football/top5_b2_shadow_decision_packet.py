"""Deterministic, read-only CEO decision packet for Builder-2 evidence.

The packet consumes only canonical qualification reports, Builder2 receipt V1,
Builder2 sample reports, and Builder2 intake artifacts.  It summarizes what is
already present and what remains unresolved; it never issues authority,
selects a model or signal time, or authorizes production.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.football.production_contracts import ProductionContractError
from src.football.top5_b2_shadow_qualification_intake import (
    INTAKE_CONTRACT_VERSION,
    RESULT_CONTRACT_VERSION,
    Builder2QualificationIntakeManifestV1,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
    semantic_digest,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    NO_PRODUCTION_SIGNAL_TIME_VALUES,
    QUALIFICATION_CONTRACT_VERSION,
    MinimumSamplePolicy,
    ProviderQualificationReport,
    ProviderQualificationStatus,
    QualificationContractError,
)
from src.football.top5_qualification_sample_aggregator import (
    BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION,
    Builder2FixtureProviderConflictV1,
    Builder2QualificationReceiptProvenanceV1,
    Builder2QualificationSampleAggregatorError,
    Builder2QualificationSampleReportV1,
    aggregate_builder2_qualification_samples,
)
from src.utils.atomic_io import atomic_write_json, atomic_write_text

DECISION_PACKET_CONTRACT_VERSION = "top5-b2-controlled-shadow-decision-packet-v1"
DECISION_PACKET_SCHEMA_VERSION = DECISION_PACKET_CONTRACT_VERSION

EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"
EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
SAMPLE_BELOW_CALLER_POLICY = "SAMPLE_BELOW_CALLER_POLICY"
SAMPLE_MEETS_CALLER_POLICY = "SAMPLE_MEETS_CALLER_POLICY"
SAMPLE_POLICY_NOT_SUPPLIED = "SAMPLE_POLICY_NOT_SUPPLIED"
EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
AUTHORITY_MISMATCH = "AUTHORITY_MISMATCH"
FAILED_CLOSED = "FAILED_CLOSED"

MISSING_INTAKE_ARTIFACT = "MISSING_INTAKE_ARTIFACT"
MISSING_QUALIFICATION_REPORT = "MISSING_QUALIFICATION_REPORT"
INTAKE_BINDING_MISMATCH = "INTAKE_BINDING_MISMATCH"
SAMPLE_REPORT_MISMATCH = "SAMPLE_REPORT_MISMATCH"

_REPORT_FIELDS = frozenset(
    {
        "contract_version",
        "qualification_status",
        "session",
        "results",
        "provider_statuses",
        "coverage",
        "freshness",
        "failure_counts",
        "unresolved",
        "minimum_sample_policy",
        "production_sample_sufficient",
        "accepted_real_observation_count",
        "accepted_distinct_fixture_count",
        "cascade_network_request_count",
        "selected_observation_network_request_count",
        "quota_units_observed",
        "signal_time_note",
        "production_activation_authorized",
        "recommendation",
    }
)
_REPORT_RESULT_FIELDS = frozenset(
    {
        "observation_id",
        "provider_identity",
        "league",
        "status",
        "accepted",
        "real_observed",
        "failure_codes",
        "cascade_errors",
        "duplicate_suppressed",
        "source_age_seconds",
        "capture_latency_ms",
        "time_to_kickoff_seconds",
        "cascade_network_request_count",
        "cascade_quota_units",
    }
)
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
_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "intake_id",
        "manifest_digest",
        "qualification_report_digest",
        "qualification_status",
        "qualification_receipt_id",
        "receipt_digest",
        "sample_report_digest",
        "artifact_directory",
        "safety",
    }
)
_RESULT_SAFETY = {
    "no_network_execution": True,
    "no_bet": True,
    "no_publication": True,
    "no_production_activation": True,
    "production_model_bound": False,
}
_PACKET_SAFETY = {
    "no_network_execution": True,
    "no_bet": True,
    "publication_authorized": False,
    "betting_authorized": False,
    "model_authorized": False,
    "signal_time_authorized": False,
    "production_activation_authorized": False,
    "controlled_activation_authorized": False,
    "production_model_bound": False,
    "provider_authority_selected": False,
}


class Builder2QualificationDecisionPacketError(ProductionContractError):
    """Malformed canonical input or an unsafe packet state."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Builder2QualificationDecisionPacketError(f"{name} is required")
    return value.strip()


def _digest(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise Builder2QualificationDecisionPacketError(
            f"{name} must be a 64-character hexadecimal digest"
        )
    return text


def _strict_mapping(
    value: object,
    *,
    required: Iterable[str],
    allowed: Iterable[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Builder2QualificationDecisionPacketError(f"{name} must be an object")
    raw = dict(value)
    missing = set(required) - set(raw)
    if missing:
        raise Builder2QualificationDecisionPacketError(
            f"{name} is missing required fields: {sorted(missing)}"
        )
    unknown = set(raw) - set(allowed)
    if unknown:
        raise Builder2QualificationDecisionPacketError(
            f"{name} contains unknown fields: {sorted(unknown)}"
        )
    return raw


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Builder2QualificationDecisionPacketError(
            f"{name} must be a non-negative integer"
        )
    return value


def _sorted_texts(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise Builder2QualificationDecisionPacketError(f"{name} must be a list")
    normalized = tuple(_text(item, name) for item in values)
    return tuple(sorted(set(normalized)))


def _canonical_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_report_payload(raw: Mapping[str, object]) -> dict[str, object]:
    payload = dict(raw)
    for key in ("results", "coverage", "freshness"):
        values = payload.get(key)
        if not isinstance(values, list):
            raise Builder2QualificationDecisionPacketError(
                f"qualification report {key} must be a list"
            )
        payload[key] = sorted(values, key=_canonical_key)
    unresolved = payload.get("unresolved")
    if not isinstance(unresolved, list):
        raise Builder2QualificationDecisionPacketError(
            "qualification report unresolved must be a list"
        )
    payload["unresolved"] = sorted(unresolved)
    return payload


def _policy_from_payload(value: object, name: str) -> MinimumSamplePolicy | None:
    if value is None:
        return None
    raw = _strict_mapping(
        value,
        required={
            "minimum_real_observations",
            "minimum_distinct_fixtures",
            "production_approval",
        },
        allowed={
            "minimum_real_observations",
            "minimum_distinct_fixtures",
            "production_approval",
        },
        name=name,
    )
    if raw["production_approval"] is not False:
        raise Builder2QualificationDecisionPacketError(
            f"{name} cannot contain production approval"
        )
    policy = MinimumSamplePolicy(
        raw["minimum_real_observations"], raw["minimum_distinct_fixtures"]
    )
    policy.validate()
    return policy


@dataclass(frozen=True)
class Builder2DecisionPacketReportSummaryV1:
    report_digest: str
    qualification_status: str
    qualification_session_id: str
    accepted_observation_ids: tuple[str, ...]
    rejected_observation_ids: tuple[str, ...]
    rejected_failure_codes: Mapping[str, tuple[str, ...]]
    failure_counts: Mapping[str, int]
    unresolved: tuple[str, ...]
    production_sample_sufficient: bool | None
    signal_time_note: str

    def validate(self) -> None:
        _digest(self.report_digest, "report_digest")
        _text(self.qualification_status, "qualification_status")
        try:
            ProviderQualificationStatus(self.qualification_status)
        except (TypeError, ValueError) as exc:
            raise Builder2QualificationDecisionPacketError(
                "qualification_status is invalid"
            ) from exc
        _text(self.qualification_session_id, "qualification_session_id")
        for name, values in (
            ("accepted_observation_ids", self.accepted_observation_ids),
            ("rejected_observation_ids", self.rejected_observation_ids),
            ("unresolved", self.unresolved),
        ):
            if tuple(sorted(set(values))) != values:
                raise Builder2QualificationDecisionPacketError(
                    f"{name} must be sorted and unique"
                )
            for value in values:
                _text(value, name)
        for observation_id, codes in self.rejected_failure_codes.items():
            _text(observation_id, "rejected_failure_codes key")
            if tuple(sorted(set(codes))) != codes:
                raise Builder2QualificationDecisionPacketError(
                    "rejected failure codes must be sorted and unique"
                )
            for code in codes:
                _text(code, "rejected failure code")
        for name, counts in (("failure_counts", self.failure_counts),):
            for key, value in counts.items():
                _text(key, f"{name} key")
                _nonnegative_int(value, f"{name} value")
        if self.production_sample_sufficient is not None and not isinstance(
            self.production_sample_sufficient, bool
        ):
            raise Builder2QualificationDecisionPacketError(
                "production_sample_sufficient must be boolean or null"
            )
        if self.signal_time_note != NO_PRODUCTION_SIGNAL_TIME_VALUES:
            raise Builder2QualificationDecisionPacketError(
                "signal-time approval cannot be asserted"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "report_digest": self.report_digest,
            "qualification_status": self.qualification_status,
            "qualification_session_id": self.qualification_session_id,
            "accepted_observation_ids": list(self.accepted_observation_ids),
            "rejected_observation_ids": list(self.rejected_observation_ids),
            "rejected_failure_codes": {
                key: list(value)
                for key, value in sorted(self.rejected_failure_codes.items())
            },
            "failure_counts": dict(sorted(self.failure_counts.items())),
            "unresolved": list(self.unresolved),
            "production_sample_sufficient": self.production_sample_sufficient,
            "signal_time_note": self.signal_time_note,
        }

    @classmethod
    def from_payload(cls, payload: object) -> Builder2DecisionPacketReportSummaryV1:
        raw = _strict_mapping(
            payload,
            required={
                "report_digest",
                "qualification_status",
                "qualification_session_id",
                "accepted_observation_ids",
                "rejected_observation_ids",
                "rejected_failure_codes",
                "failure_counts",
                "unresolved",
                "production_sample_sufficient",
                "signal_time_note",
            },
            allowed={
                "report_digest",
                "qualification_status",
                "qualification_session_id",
                "accepted_observation_ids",
                "rejected_observation_ids",
                "rejected_failure_codes",
                "failure_counts",
                "unresolved",
                "production_sample_sufficient",
                "signal_time_note",
            },
            name="report summary",
        )
        rejected_raw = raw["rejected_failure_codes"]
        counts_raw = raw["failure_counts"]
        if not isinstance(rejected_raw, Mapping) or not isinstance(counts_raw, Mapping):
            raise Builder2QualificationDecisionPacketError(
                "report summary maps are invalid"
            )
        summary = cls(
            report_digest=_text(raw["report_digest"], "report_digest"),
            qualification_status=raw["qualification_status"],
            qualification_session_id=_text(
                raw["qualification_session_id"], "qualification_session_id"
            ),
            accepted_observation_ids=_sorted_texts(
                raw["accepted_observation_ids"], "accepted_observation_ids"
            ),
            rejected_observation_ids=_sorted_texts(
                raw["rejected_observation_ids"], "rejected_observation_ids"
            ),
            rejected_failure_codes={
                _text(key, "rejected_failure_codes key"): _sorted_texts(
                    value, "rejected_failure_codes"
                )
                for key, value in rejected_raw.items()
            },
            failure_counts={str(key): value for key, value in counts_raw.items()},
            unresolved=_sorted_texts(raw["unresolved"], "unresolved"),
            production_sample_sufficient=raw["production_sample_sufficient"],
            signal_time_note=raw["signal_time_note"],
        )
        summary.validate()
        return summary


@dataclass(frozen=True)
class Builder2DecisionPacketIntakeSummaryV1:
    intake_id: str
    manifest_digest: str | None
    qualification_report_digest: str | None
    receipt_id: str | None
    receipt_digest: str | None
    source_artifacts: tuple[Mapping[str, str], ...]
    missing_artifacts: tuple[str, ...]
    binding_issues: tuple[str, ...]

    def validate(self) -> None:
        _text(self.intake_id, "intake_id")
        for name, value in (
            ("manifest_digest", self.manifest_digest),
            ("qualification_report_digest", self.qualification_report_digest),
            ("receipt_digest", self.receipt_digest),
        ):
            if value is not None:
                _digest(value, name)
        if self.receipt_id is not None:
            _text(self.receipt_id, "receipt_id")
        for name, values in (
            ("missing_artifacts", self.missing_artifacts),
            ("binding_issues", self.binding_issues),
        ):
            if tuple(sorted(set(values))) != values:
                raise Builder2QualificationDecisionPacketError(
                    f"{name} must be sorted and unique"
                )
            for value in values:
                _text(value, name)
        previous: tuple[str, str] | None = None
        for artifact in self.source_artifacts:
            raw = _strict_mapping(
                artifact,
                required={"role", "path", "digest"},
                allowed={"role", "path", "digest"},
                name="source_artifact",
            )
            key = (
                _text(raw["role"], "source_artifact.role"),
                _text(raw["path"], "source_artifact.path"),
            )
            _digest(raw["digest"], "source_artifact.digest")
            if previous is not None and key < previous:
                raise Builder2QualificationDecisionPacketError(
                    "source_artifacts must be sorted"
                )
            previous = key

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "intake_id": self.intake_id,
            "manifest_digest": self.manifest_digest,
            "qualification_report_digest": self.qualification_report_digest,
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "source_artifacts": [
                dict(sorted(item.items())) for item in self.source_artifacts
            ],
            "missing_artifacts": list(self.missing_artifacts),
            "binding_issues": list(self.binding_issues),
        }

    @classmethod
    def from_payload(cls, payload: object) -> Builder2DecisionPacketIntakeSummaryV1:
        raw = _strict_mapping(
            payload,
            required={
                "intake_id",
                "manifest_digest",
                "qualification_report_digest",
                "receipt_id",
                "receipt_digest",
                "source_artifacts",
                "missing_artifacts",
                "binding_issues",
            },
            allowed={
                "intake_id",
                "manifest_digest",
                "qualification_report_digest",
                "receipt_id",
                "receipt_digest",
                "source_artifacts",
                "missing_artifacts",
                "binding_issues",
            },
            name="intake summary",
        )
        source = raw["source_artifacts"]
        if not isinstance(source, (list, tuple)):
            raise Builder2QualificationDecisionPacketError(
                "intake summary source_artifacts must be a list"
            )
        summary = cls(
            intake_id=_text(raw["intake_id"], "intake_id"),
            manifest_digest=raw["manifest_digest"],
            qualification_report_digest=raw["qualification_report_digest"],
            receipt_id=raw["receipt_id"],
            receipt_digest=raw["receipt_digest"],
            source_artifacts=tuple(dict(item) for item in source),
            missing_artifacts=_sorted_texts(
                raw["missing_artifacts"], "missing_artifacts"
            ),
            binding_issues=_sorted_texts(raw["binding_issues"], "binding_issues"),
        )
        summary.validate()
        return summary


@dataclass(frozen=True)
class Builder2QualificationIntakeArtifactV1:
    """One intake directory projection; absent files remain visible."""

    manifest: Builder2QualificationIntakeManifestV1 | None = None
    qualification_report: ProviderQualificationReport | Mapping[str, object] | None = (
        None
    )
    receipt: Builder2QualificationReceiptV1 | None = None
    result: Mapping[str, object] | None = None
    sample_report: Builder2QualificationSampleReportV1 | None = None
    artifact_path: str = "external-intake"
    missing_artifacts: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationIntakeArtifactV1:
        raw = _strict_mapping(
            payload,
            required=set(),
            allowed={
                "manifest",
                "qualification_report",
                "receipt",
                "result",
                "sample_report",
                "artifact_path",
                "missing_artifacts",
            },
            name="intake_artifact",
        )
        missing = set(
            _sorted_texts(raw.get("missing_artifacts", ()), "missing_artifacts")
        )
        manifest = None
        if "manifest" not in raw or raw["manifest"] is None:
            missing.add("manifest.json")
        else:
            manifest = (
                raw["manifest"]
                if isinstance(raw["manifest"], Builder2QualificationIntakeManifestV1)
                else Builder2QualificationIntakeManifestV1.from_payload(raw["manifest"])
            )
        report = raw.get("qualification_report")
        if report is None:
            missing.add("qualification_report.json")
        elif not isinstance(report, (ProviderQualificationReport, Mapping)):
            raise Builder2QualificationDecisionPacketError(
                "qualification_report must be a canonical report object"
            )
        receipt = None
        if "receipt" not in raw or raw["receipt"] is None:
            missing.add("receipt.json")
        else:
            receipt = _receipt(raw["receipt"], "intake receipt")
        result = raw.get("result")
        if result is None:
            missing.add("result.json")
        elif not isinstance(result, Mapping):
            raise Builder2QualificationDecisionPacketError(
                "intake result must be an object"
            )
        sample = raw.get("sample_report")
        if sample is not None:
            sample = _sample_report(sample, "intake sample report")
        return cls(
            manifest=manifest,
            qualification_report=report,
            receipt=receipt,
            result=dict(result) if isinstance(result, Mapping) else None,
            sample_report=sample,
            artifact_path=_text(
                raw.get("artifact_path", "external-intake"), "artifact_path"
            ),
            missing_artifacts=tuple(sorted(missing)),
        )

    def validate(self) -> None:
        if self.manifest is not None:
            self.manifest.validate()
        if self.qualification_report is not None:
            _report_snapshot(self.qualification_report)
        if self.receipt is not None:
            self.receipt.validate()
        if self.sample_report is not None:
            self.sample_report.validate()
        if self.result is not None:
            _validate_result(self.result)
        _text(self.artifact_path, "artifact_path")
        _sorted_texts(self.missing_artifacts, "missing_artifacts")


@dataclass(frozen=True)
class _ReportSnapshot:
    report_digest: str
    qualification_status: str
    qualification_session_id: str
    accepted_observation_ids: tuple[str, ...]
    rejected_observation_ids: tuple[str, ...]
    rejected_failure_codes: Mapping[str, tuple[str, ...]]
    failure_counts: Mapping[str, int]
    unresolved: tuple[str, ...]
    production_sample_sufficient: bool | None
    signal_time_note: str


def _receipt(value: object, name: str) -> Builder2QualificationReceiptV1:
    if isinstance(value, Builder2QualificationReceiptV1):
        value.validate()
        return value
    if not isinstance(value, Mapping):
        raise Builder2QualificationDecisionPacketError(
            f"{name} must be a canonical receipt"
        )
    try:
        receipt = Builder2QualificationReceiptV1.from_payload(value)
        receipt.validate()
    except (
        Builder2QualificationReceiptError,
        QualificationContractError,
        TypeError,
        ValueError,
    ) as exc:
        raise Builder2QualificationDecisionPacketError(
            f"{name} is invalid: {exc}"
        ) from exc
    if dict(value) != receipt.as_payload():
        raise Builder2QualificationDecisionPacketError(
            f"{name} contains unknown or non-canonical fields"
        )
    return receipt


def _sample_report(value: object, name: str) -> Builder2QualificationSampleReportV1:
    if isinstance(value, Builder2QualificationSampleReportV1):
        value.validate()
        return value
    if not isinstance(value, Mapping):
        raise Builder2QualificationDecisionPacketError(
            f"{name} must be a canonical sample report"
        )
    try:
        report = Builder2QualificationSampleReportV1.from_payload(value)
        report.validate()
    except (
        Builder2QualificationSampleAggregatorError,
        QualificationContractError,
        TypeError,
        ValueError,
    ) as exc:
        raise Builder2QualificationDecisionPacketError(
            f"{name} is invalid: {exc}"
        ) from exc
    if dict(value) != report.as_payload():
        raise Builder2QualificationDecisionPacketError(
            f"{name} contains unknown or non-canonical fields"
        )
    return report


def _validate_result(raw: Mapping[str, object]) -> None:
    fields = _strict_mapping(
        raw, required=_RESULT_FIELDS, allowed=_RESULT_FIELDS, name="intake result"
    )
    if fields["schema_version"] != RESULT_CONTRACT_VERSION:
        raise Builder2QualificationDecisionPacketError(
            "unsupported intake result schema"
        )
    for name in (
        "intake_id",
        "manifest_digest",
        "qualification_report_digest",
        "qualification_receipt_id",
        "receipt_digest",
    ):
        _text(fields[name], f"intake result {name}")
    if fields["sample_report_digest"] is not None:
        _digest(fields["sample_report_digest"], "intake result sample_report_digest")
    if fields["artifact_directory"] is not None:
        _text(fields["artifact_directory"], "intake result artifact_directory")
    if fields["safety"] != _RESULT_SAFETY:
        raise Builder2QualificationDecisionPacketError(
            "intake result safety state is unsafe"
        )


def _report_snapshot(
    value: ProviderQualificationReport | Mapping[str, object],
) -> _ReportSnapshot:
    if isinstance(value, ProviderQualificationReport):
        value.validate()
        raw = value.as_payload()
    elif isinstance(value, Mapping):
        raw = _strict_mapping(
            value,
            required=_REPORT_FIELDS,
            allowed=_REPORT_FIELDS,
            name="qualification report",
        )
    else:
        raise Builder2QualificationDecisionPacketError(
            "qualification report is required"
        )
    if raw["contract_version"] != QUALIFICATION_CONTRACT_VERSION:
        raise Builder2QualificationDecisionPacketError(
            "unsupported qualification report schema"
        )
    if (
        raw["production_activation_authorized"] is not False
        or raw["recommendation"] is not None
    ):
        raise Builder2QualificationDecisionPacketError(
            "qualification report contains production authority"
        )
    if raw["signal_time_note"] != NO_PRODUCTION_SIGNAL_TIME_VALUES:
        raise Builder2QualificationDecisionPacketError(
            "qualification report signal-time state is unsafe"
        )
    session = _strict_mapping(
        raw["session"],
        required=_SESSION_FIELDS,
        allowed=_SESSION_FIELDS,
        name="qualification report session",
    )
    _strict_mapping(
        session["safety"],
        required=_SESSION_SAFETY_FIELDS,
        allowed=_SESSION_SAFETY_FIELDS,
        name="qualification report session safety",
    )
    if session["monetary_spend_authorized"] is not False or session["safety"] != {
        "no_bet": True,
        "publication_enabled": False,
        "production_activation": False,
        "ledger_mutated": False,
        "sealed_data_accessed": False,
        "research_mutated": False,
    }:
        raise Builder2QualificationDecisionPacketError(
            "qualification report session safety is unsafe"
        )
    session_id = _text(
        session["qualification_session_id"], "qualification report session ID"
    )
    results = raw["results"]
    if not isinstance(results, list):
        raise Builder2QualificationDecisionPacketError(
            "qualification report results must be a list"
        )
    accepted: list[str] = []
    rejected: list[str] = []
    rejected_codes: dict[str, tuple[str, ...]] = {}
    for index, item in enumerate(results):
        result = _strict_mapping(
            item,
            required=_REPORT_RESULT_FIELDS,
            allowed=_REPORT_RESULT_FIELDS,
            name=f"qualification result {index}",
        )
        observation_id = _text(
            result["observation_id"], "qualification result observation_id"
        )
        _text(result["provider_identity"], "qualification result provider_identity")
        _text(result["league"], "qualification result league")
        try:
            ProviderQualificationStatus(result["status"])
        except (TypeError, ValueError) as exc:
            raise Builder2QualificationDecisionPacketError(
                "qualification result status is invalid"
            ) from exc
        if not isinstance(result["accepted"], bool) or not isinstance(
            result["real_observed"], bool
        ):
            raise Builder2QualificationDecisionPacketError(
                "qualification result safety flags must be boolean"
            )
        codes = _sorted_texts(
            result["failure_codes"], "qualification result failure_codes"
        )
        if result["accepted"] and result["real_observed"]:
            accepted.append(observation_id)
        else:
            rejected.append(observation_id)
            rejected_codes[observation_id] = codes
    failure_counts_raw = raw["failure_counts"]
    if not isinstance(failure_counts_raw, Mapping):
        raise Builder2QualificationDecisionPacketError(
            "qualification report failure_counts must be an object"
        )
    failure_counts = {
        str(key): _nonnegative_int(value, "failure_counts value")
        for key, value in failure_counts_raw.items()
    }
    unresolved = _sorted_texts(raw["unresolved"], "qualification report unresolved")
    policy = _policy_from_payload(
        raw["minimum_sample_policy"], "qualification report minimum_sample_policy"
    )
    sufficient = raw["production_sample_sufficient"]
    if sufficient is not None and not isinstance(sufficient, bool):
        raise Builder2QualificationDecisionPacketError(
            "production_sample_sufficient must be boolean or null"
        )
    if sufficient is True and policy is None:
        raise Builder2QualificationDecisionPacketError(
            "report sample sufficiency lacks caller policy"
        )
    for name in (
        "accepted_real_observation_count",
        "accepted_distinct_fixture_count",
        "cascade_network_request_count",
        "selected_observation_network_request_count",
    ):
        _nonnegative_int(raw[name], f"qualification report {name}")
    if raw["quota_units_observed"] < 0 or isinstance(raw["quota_units_observed"], bool):
        raise Builder2QualificationDecisionPacketError(
            "qualification report quota is invalid"
        )
    canonical = _canonical_report_payload(raw)
    return _ReportSnapshot(
        report_digest=semantic_digest(canonical),
        qualification_status=ProviderQualificationStatus(
            raw["qualification_status"]
        ).value,
        qualification_session_id=session_id,
        accepted_observation_ids=tuple(sorted(set(accepted))),
        rejected_observation_ids=tuple(sorted(set(rejected))),
        rejected_failure_codes=dict(sorted(rejected_codes.items())),
        failure_counts=dict(sorted(failure_counts.items())),
        unresolved=unresolved,
        production_sample_sufficient=sufficient,
        signal_time_note=raw["signal_time_note"],
    )


def _report_summary(snapshot: _ReportSnapshot) -> Builder2DecisionPacketReportSummaryV1:
    summary = Builder2DecisionPacketReportSummaryV1(
        report_digest=snapshot.report_digest,
        qualification_status=snapshot.qualification_status,
        qualification_session_id=snapshot.qualification_session_id,
        accepted_observation_ids=snapshot.accepted_observation_ids,
        rejected_observation_ids=snapshot.rejected_observation_ids,
        rejected_failure_codes=snapshot.rejected_failure_codes,
        failure_counts=snapshot.failure_counts,
        unresolved=snapshot.unresolved,
        production_sample_sufficient=snapshot.production_sample_sufficient,
        signal_time_note=snapshot.signal_time_note,
    )
    summary.validate()
    return summary


def _validate_intake_bindings(
    artifact: Builder2QualificationIntakeArtifactV1,
    report: _ReportSnapshot | None,
) -> tuple[Builder2DecisionPacketIntakeSummaryV1, tuple[str, ...]]:
    issues = set(artifact.missing_artifacts)
    binding: set[str] = set()
    manifest = artifact.manifest
    receipt = artifact.receipt
    result = artifact.result
    if (
        (manifest is None or receipt is None or report is None or result is None)
        and receipt is not None
        and report is None
    ):
        issues.add(MISSING_QUALIFICATION_REPORT)
    if manifest is not None and receipt is not None:
        pairs = {
            "controlled_shadow_run_id": (
                manifest.controlled_shadow_run_id,
                receipt.controlled_shadow_run_id,
            ),
            "qualification_session_id": (
                manifest.qualification_session_id,
                receipt.qualification_session_id,
            ),
            "ceo_authorization_id": (
                manifest.ceo_authorization_id,
                receipt.ceo_authorization_id,
            ),
            "fixture_key": (manifest.fixture_key, receipt.fixture_key),
            "provider_identity": (
                manifest.provider_identity,
                receipt.provider_identity,
            ),
            "provider_event_id": (
                manifest.provider_event_id,
                receipt.provider_event_id,
            ),
            "provider_request_id": (
                manifest.provider_request_id,
                receipt.provider_request_id,
            ),
            "observation_id": (manifest.observation_id, receipt.observation_id),
            "normalized_record_digest": (
                manifest.normalized_record_digest,
                receipt.normalized_record_digest,
            ),
            "adapter_version": (manifest.adapter_version, receipt.adapter_version),
            "adapter_source_sha": (
                manifest.adapter_source_sha,
                receipt.adapter_source_sha,
            ),
        }
        binding.update(
            key for key, (expected, actual) in pairs.items() if expected != actual
        )
    if report is not None and receipt is not None:
        if receipt.qualification_report_digest.lower() != report.report_digest.lower():
            binding.add("qualification_report_digest")
        if receipt.qualification_session_id != report.qualification_session_id:
            binding.add("qualification_session_id")
        if receipt.observation_id not in (
            *report.accepted_observation_ids,
            *report.rejected_observation_ids,
        ):
            binding.add("observation_id")
    if result is not None:
        _validate_result(result)
        expected = {
            "intake_id": manifest.intake_id if manifest else None,
            "manifest_digest": manifest.manifest_digest if manifest else None,
            "qualification_report_digest": report.report_digest if report else None,
            "qualification_receipt_id": receipt.qualification_receipt_id
            if receipt
            else None,
            "receipt_digest": receipt.receipt_digest if receipt else None,
        }
        for key, expected_value in expected.items():
            if expected_value is not None and result[key] != expected_value:
                binding.add(key)
    if binding:
        issues.add(INTAKE_BINDING_MISMATCH)
    summary = Builder2DecisionPacketIntakeSummaryV1(
        intake_id=manifest.intake_id if manifest else artifact.artifact_path,
        manifest_digest=manifest.manifest_digest if manifest else None,
        qualification_report_digest=report.report_digest if report else None,
        receipt_id=receipt.qualification_receipt_id if receipt else None,
        receipt_digest=receipt.receipt_digest if receipt else None,
        source_artifacts=tuple(
            {
                "role": item.role,
                "path": item.path,
                "digest": item.digest,
            }
            for item in (manifest.source_artifacts if manifest else ())
        ),
        missing_artifacts=tuple(sorted(issues)),
        binding_issues=tuple(sorted(binding)),
    )
    summary.validate()
    return summary, tuple(sorted(issues))


def _provenance_key(item: Builder2QualificationReceiptProvenanceV1) -> tuple[str, str]:
    return item.qualification_receipt_id, item.receipt_digest


def _sample_from_inputs(
    receipts: tuple[Builder2QualificationReceiptV1, ...],
    supplied: tuple[Builder2QualificationSampleReportV1, ...],
    policy: MinimumSamplePolicy | None,
) -> tuple[
    Builder2QualificationSampleReportV1, tuple[str, ...], MinimumSamplePolicy | None
]:
    if policy is not None:
        if not isinstance(policy, MinimumSamplePolicy):
            raise Builder2QualificationDecisionPacketError(
                "minimum_sample_policy must be canonical"
            )
        policy.validate()
    issues: set[str] = set()
    supplied_policy = next(
        (
            item.minimum_sample_policy
            for item in supplied
            if item.minimum_sample_policy is not None
        ),
        None,
    )
    effective_policy = policy if policy is not None else supplied_policy
    if receipts:
        derived = aggregate_builder2_qualification_samples(
            receipts, minimum_sample_policy=effective_policy
        )
        derived_keys = tuple(
            sorted(_provenance_key(item) for item in derived.receipt_provenance)
        )
        for item in supplied:
            if (
                tuple(sorted(_provenance_key(row) for row in item.receipt_provenance))
                != derived_keys
            ):
                issues.add(SAMPLE_REPORT_MISMATCH)
        return derived, tuple(sorted(issues)), effective_policy
    if supplied:
        by_digest = {item.report_digest: item for item in supplied}
        if len(by_digest) > 1:
            issues.add(SAMPLE_REPORT_MISMATCH)
        chosen = by_digest[min(by_digest)]
        if policy is not None and chosen.minimum_sample_policy != policy:
            issues.add(AUTHORITY_MISMATCH)
            effective_policy = policy
        return chosen, tuple(sorted(issues)), effective_policy
    return (
        aggregate_builder2_qualification_samples(
            (), minimum_sample_policy=effective_policy
        ),
        tuple(sorted(issues)),
        effective_policy,
    )


def _receipt_summary_from_sample(
    sample: Builder2QualificationSampleReportV1,
) -> tuple[Builder2QualificationReceiptProvenanceV1, ...]:
    return tuple(sorted(sample.receipt_provenance, key=_provenance_key))


def _strict_input_iter(value: object, name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, Mapping)):
        raise Builder2QualificationDecisionPacketError(f"{name} must be an iterable")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise Builder2QualificationDecisionPacketError(
            f"{name} must be iterable"
        ) from exc


def build_builder2_qualification_decision_packet(
    receipts: Iterable[Builder2QualificationReceiptV1 | Mapping[str, object]] = (),
    *,
    qualification_reports: Iterable[
        ProviderQualificationReport | Mapping[str, object]
    ] = (),
    sample_reports: Iterable[
        Builder2QualificationSampleReportV1 | Mapping[str, object]
    ] = (),
    intake_artifacts: Iterable[
        Builder2QualificationIntakeArtifactV1 | Mapping[str, object]
    ] = (),
    minimum_sample_policy: MinimumSamplePolicy | None = None,
) -> Builder2QualificationDecisionPacketV1:
    """Build a deterministic packet without issuing or inferring authority."""

    normalized_receipts = tuple(
        _receipt(item, f"receipt {index}")
        for index, item in enumerate(_strict_input_iter(receipts, "receipts"))
    )
    artifacts = tuple(
        item
        if isinstance(item, Builder2QualificationIntakeArtifactV1)
        else Builder2QualificationIntakeArtifactV1.from_payload(item)
        for item in _strict_input_iter(intake_artifacts, "intake_artifacts")
    )
    for artifact in artifacts:
        artifact.validate()
    artifact_receipts = tuple(
        item.receipt for item in artifacts if item.receipt is not None
    )
    all_receipts = normalized_receipts + artifact_receipts
    report_inputs = tuple(
        _strict_input_iter(qualification_reports, "qualification_reports")
    )
    report_inputs += tuple(
        item.qualification_report
        for item in artifacts
        if item.qualification_report is not None
    )
    snapshots_by_digest: dict[str, _ReportSnapshot] = {}
    for index, item in enumerate(report_inputs):
        snapshot = _report_snapshot(item)  # type: ignore[arg-type]
        snapshots_by_digest.setdefault(snapshot.report_digest, snapshot)
    snapshots = tuple(snapshots_by_digest[key] for key in sorted(snapshots_by_digest))
    supplied_samples = tuple(
        item
        if isinstance(item, Builder2QualificationSampleReportV1)
        else _sample_report(item, f"sample report {index}")
        for index, item in enumerate(
            _strict_input_iter(sample_reports, "sample_reports")
        )
    )
    supplied_samples += tuple(
        item.sample_report for item in artifacts if item.sample_report is not None
    )
    sample, sample_issues, effective_policy = _sample_from_inputs(
        all_receipts, supplied_samples, minimum_sample_policy
    )
    sample.validate()

    intake_summaries: list[Builder2DecisionPacketIntakeSummaryV1] = []
    missing: set[str] = set()
    receipt_to_intake: set[tuple[str, str]] = set()
    authority_mismatches: set[str] = set()
    for artifact in artifacts:
        report = None
        if artifact.qualification_report is not None:
            report = _report_snapshot(artifact.qualification_report)
        summary, issues = _validate_intake_bindings(artifact, report)
        intake_summaries.append(summary)
        missing.update(issues)
        if summary.receipt_id and summary.receipt_digest:
            receipt_to_intake.add((summary.receipt_id, summary.receipt_digest))
        authority_mismatches.update(summary.binding_issues)
    for receipt in all_receipts:
        if (
            receipt.qualification_receipt_id,
            receipt.receipt_digest,
        ) not in receipt_to_intake:
            missing.add(MISSING_INTAKE_ARTIFACT)
        matching = [
            item
            for item in snapshots
            if item.report_digest == receipt.qualification_report_digest
        ]
        if not matching:
            missing.add(MISSING_QUALIFICATION_REPORT)
        else:
            report = matching[0]
            if report.qualification_session_id != receipt.qualification_session_id:
                authority_mismatches.add("qualification_session_id")
            if receipt.observation_id not in (
                *report.accepted_observation_ids,
                *report.rejected_observation_ids,
            ):
                authority_mismatches.add("observation_id")

    report_summaries = tuple(
        sorted(
            (_report_summary(item) for item in snapshots),
            key=lambda item: item.report_digest,
        )
    )
    receipt_summaries = _receipt_summary_from_sample(sample)
    controlled_runs = tuple(
        sorted({item.controlled_shadow_run_id for item in receipt_summaries})
    )
    sessions = tuple(
        sorted({item.qualification_session_id for item in receipt_summaries})
    )
    authorizations = tuple(
        sorted({item.ceo_authorization_id for item in receipt_summaries})
    )
    report_session_ids = {item.qualification_session_id for item in report_summaries}
    source_artifacts = tuple(
        sorted(
            {
                (item["role"], item["path"], item["digest"])
                for summary in intake_summaries
                for item in summary.source_artifacts
            }
        )
    )
    hard_conflict = bool(
        sample_issues
        or sample.divergent_receipt_ids
        or sample.observation_identity_conflict_ids
        or sample.observation_identity_conflict_digests
        or sample.fixture_provider_conflicts
    )
    evidence_complete = (
        bool(all_receipts)
        and not missing
        and not authority_mismatches
        and bool(report_session_ids)
    )
    if hard_conflict:
        status = EVIDENCE_CONFLICT
    elif authority_mismatches:
        status = AUTHORITY_MISMATCH
    elif not evidence_complete:
        status = EVIDENCE_INCOMPLETE
    elif effective_policy is not None and sample.sample_sufficient is True:
        status = SAMPLE_MEETS_CALLER_POLICY
    elif effective_policy is not None:
        status = SAMPLE_BELOW_CALLER_POLICY
    else:
        status = EVIDENCE_COMPLETE
    sample_state = (
        SAMPLE_POLICY_NOT_SUPPLIED
        if effective_policy is None
        else SAMPLE_MEETS_CALLER_POLICY
        if sample.sample_sufficient is True
        else SAMPLE_BELOW_CALLER_POLICY
    )
    unresolved = set(sample.unattributed_fixture_keys)
    unresolved.update(
        item for summary in report_summaries for item in summary.unresolved
    )
    unresolved.add("CEO_REVIEW_REQUIRED")
    if missing:
        unresolved.update(sorted(missing))
    if authority_mismatches:
        unresolved.add("AUTHORITY_BINDING_REQUIRES_CEO_REVIEW")
    if hard_conflict:
        unresolved.add("EVIDENCE_CONFLICT_REQUIRES_CEO_REVIEW")
    if effective_policy is None:
        unresolved.add("MINIMUM_SAMPLE_POLICY_NOT_SUPPLIED")
    ceo_decisions = {
        "REVIEW_EVIDENCE_COMPLETENESS",
        "DECIDE_SIGNAL_TIME_AUTHORITY",
        "DECIDE_PRODUCTION_MODEL_AUTHORITY",
        "DECIDE_CONTROLLED_ACTIVATION",
        "DECIDE_PUBLICATION_AND_BETTING",
    }
    packet = Builder2QualificationDecisionPacketV1(
        schema_version=DECISION_PACKET_SCHEMA_VERSION,
        packet_id="",
        packet_digest="",
        status=status,
        evidence_completeness=EVIDENCE_COMPLETE
        if evidence_complete
        else EVIDENCE_INCOMPLETE,
        sample_state=sample_state,
        intake_ids=tuple(sorted(summary.intake_id for summary in intake_summaries)),
        controlled_shadow_run_ids=controlled_runs,
        qualification_session_ids=sessions,
        ceo_authorization_ids=authorizations,
        qualification_report_digests=tuple(
            sorted(item.report_digest for item in report_summaries)
        ),
        receipt_provenance=receipt_summaries,
        qualification_report_summaries=report_summaries,
        intake_summaries=tuple(
            sorted(intake_summaries, key=lambda item: item.intake_id)
        ),
        input_receipt_count=sample.input_receipt_count,
        total_valid_receipts=sample.total_valid_receipts,
        eligible_real_observation_count=sample.eligible_distinct_observation_count,
        eligible_fixture_count=sample.eligible_distinct_fixture_count,
        per_league_counts=dict(sample.eligible_per_league_counts),
        per_provider_counts=dict(sample.eligible_per_provider_counts),
        duplicate_receipt_ids=sample.duplicate_receipt_ids,
        duplicate_observation_ids=sample.duplicate_observation_ids,
        duplicate_observation_digests=sample.duplicate_observation_digests,
        divergent_receipt_ids=sample.divergent_receipt_ids,
        observation_identity_conflict_ids=sample.observation_identity_conflict_ids,
        observation_identity_conflict_digests=sample.observation_identity_conflict_digests,
        fixture_provider_conflicts=sample.fixture_provider_conflicts,
        unattributed_fixture_keys=sample.unattributed_fixture_keys,
        exclusion_taxonomy=dict(sample.failure_taxonomy),
        sample_report_digest=sample.report_digest,
        freshness=sample.freshness.as_payload(),
        observation_coverage=sample.observation_coverage.as_payload(),
        minimum_sample_policy=effective_policy,
        sample_sufficient=sample.sample_sufficient,
        evidence_completeness_reasons=tuple(sorted(missing)),
        unresolved_items=tuple(sorted(unresolved)),
        ceo_decisions_required=tuple(sorted(ceo_decisions)),
        source_artifact_digests=tuple(
            {"role": role, "path": path, "digest": digest}
            for role, path, digest in source_artifacts
        ),
        safety=dict(_PACKET_SAFETY),
    )
    base = packet._payload(include_identity=False)
    digest = semantic_digest(base)
    packet = Builder2QualificationDecisionPacketV1(
        **{
            **packet.__dict__,
            "packet_id": f"b2dp-{digest[:24]}",
            "packet_digest": digest,
        }
    )
    packet.validate()
    return packet


@dataclass(frozen=True)
class Builder2QualificationDecisionPacketV1:
    schema_version: str
    packet_id: str
    packet_digest: str
    status: str
    evidence_completeness: str
    sample_state: str
    intake_ids: tuple[str, ...]
    controlled_shadow_run_ids: tuple[str, ...]
    qualification_session_ids: tuple[str, ...]
    ceo_authorization_ids: tuple[str, ...]
    qualification_report_digests: tuple[str, ...]
    receipt_provenance: tuple[Builder2QualificationReceiptProvenanceV1, ...]
    qualification_report_summaries: tuple[Builder2DecisionPacketReportSummaryV1, ...]
    intake_summaries: tuple[Builder2DecisionPacketIntakeSummaryV1, ...]
    input_receipt_count: int
    total_valid_receipts: int
    eligible_real_observation_count: int
    eligible_fixture_count: int
    per_league_counts: Mapping[str, int]
    per_provider_counts: Mapping[str, int]
    duplicate_receipt_ids: tuple[str, ...]
    duplicate_observation_ids: tuple[str, ...]
    duplicate_observation_digests: tuple[str, ...]
    divergent_receipt_ids: tuple[str, ...]
    observation_identity_conflict_ids: tuple[str, ...]
    observation_identity_conflict_digests: tuple[str, ...]
    fixture_provider_conflicts: tuple[Builder2FixtureProviderConflictV1, ...]
    unattributed_fixture_keys: tuple[str, ...]
    exclusion_taxonomy: Mapping[str, int]
    sample_report_digest: str
    freshness: Mapping[str, object]
    observation_coverage: Mapping[str, object]
    minimum_sample_policy: MinimumSamplePolicy | None
    sample_sufficient: bool | None
    evidence_completeness_reasons: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    ceo_decisions_required: tuple[str, ...]
    source_artifact_digests: tuple[Mapping[str, str], ...]
    safety: Mapping[str, bool]

    def _payload(self, *, include_identity: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "status": self.status,
            "evidence_completeness": self.evidence_completeness,
            "sample_state": self.sample_state,
            "intake_ids": list(self.intake_ids),
            "controlled_shadow_run_ids": list(self.controlled_shadow_run_ids),
            "qualification_session_ids": list(self.qualification_session_ids),
            "ceo_authorization_ids": list(self.ceo_authorization_ids),
            "qualification_report_digests": list(self.qualification_report_digests),
            "receipt_provenance": [
                item.as_payload() for item in self.receipt_provenance
            ],
            "qualification_report_summaries": [
                item.as_payload() for item in self.qualification_report_summaries
            ],
            "intake_summaries": [item.as_payload() for item in self.intake_summaries],
            "input_receipt_count": self.input_receipt_count,
            "total_valid_receipts": self.total_valid_receipts,
            "eligible_real_observation_count": self.eligible_real_observation_count,
            "eligible_fixture_count": self.eligible_fixture_count,
            "per_league_counts": dict(sorted(self.per_league_counts.items())),
            "per_provider_counts": dict(sorted(self.per_provider_counts.items())),
            "duplicate_receipt_ids": list(self.duplicate_receipt_ids),
            "duplicate_observation_ids": list(self.duplicate_observation_ids),
            "duplicate_observation_digests": list(self.duplicate_observation_digests),
            "divergent_receipt_ids": list(self.divergent_receipt_ids),
            "observation_identity_conflict_ids": list(
                self.observation_identity_conflict_ids
            ),
            "observation_identity_conflict_digests": list(
                self.observation_identity_conflict_digests
            ),
            "fixture_provider_conflicts": [
                item.as_payload() for item in self.fixture_provider_conflicts
            ],
            "unattributed_fixture_keys": list(self.unattributed_fixture_keys),
            "exclusion_taxonomy": dict(sorted(self.exclusion_taxonomy.items())),
            "sample_report_digest": self.sample_report_digest,
            "freshness": dict(sorted(self.freshness.items())),
            "observation_coverage": dict(sorted(self.observation_coverage.items())),
            "minimum_sample_policy": self.minimum_sample_policy.as_payload()
            if self.minimum_sample_policy
            else None,
            "sample_sufficient": self.sample_sufficient,
            "evidence_completeness_reasons": list(self.evidence_completeness_reasons),
            "unresolved_items": list(self.unresolved_items),
            "ceo_decisions_required": list(self.ceo_decisions_required),
            "source_artifact_digests": [
                dict(sorted(item.items())) for item in self.source_artifact_digests
            ],
            "safety": dict(sorted(self.safety.items())),
        }
        if include_identity:
            payload["packet_id"] = self.packet_id
            payload["packet_digest"] = self.packet_digest
        return payload

    def validate(self) -> None:
        if self.schema_version != DECISION_PACKET_SCHEMA_VERSION:
            raise Builder2QualificationDecisionPacketError(
                "unsupported decision packet schema"
            )
        if self.status not in {
            EVIDENCE_COMPLETE,
            EVIDENCE_INCOMPLETE,
            SAMPLE_BELOW_CALLER_POLICY,
            SAMPLE_MEETS_CALLER_POLICY,
            EVIDENCE_CONFLICT,
            AUTHORITY_MISMATCH,
            FAILED_CLOSED,
        }:
            raise Builder2QualificationDecisionPacketError(
                "invalid decision packet status"
            )
        if self.evidence_completeness not in {EVIDENCE_COMPLETE, EVIDENCE_INCOMPLETE}:
            raise Builder2QualificationDecisionPacketError(
                "invalid evidence completeness state"
            )
        if self.sample_state not in {
            SAMPLE_POLICY_NOT_SUPPLIED,
            SAMPLE_BELOW_CALLER_POLICY,
            SAMPLE_MEETS_CALLER_POLICY,
        }:
            raise Builder2QualificationDecisionPacketError("invalid sample state")
        for name, values in (
            ("intake_ids", self.intake_ids),
            ("controlled_shadow_run_ids", self.controlled_shadow_run_ids),
            ("qualification_session_ids", self.qualification_session_ids),
            ("ceo_authorization_ids", self.ceo_authorization_ids),
            ("qualification_report_digests", self.qualification_report_digests),
            ("duplicate_receipt_ids", self.duplicate_receipt_ids),
            ("duplicate_observation_ids", self.duplicate_observation_ids),
            ("duplicate_observation_digests", self.duplicate_observation_digests),
            ("divergent_receipt_ids", self.divergent_receipt_ids),
            (
                "observation_identity_conflict_ids",
                self.observation_identity_conflict_ids,
            ),
            (
                "observation_identity_conflict_digests",
                self.observation_identity_conflict_digests,
            ),
            ("unattributed_fixture_keys", self.unattributed_fixture_keys),
            ("evidence_completeness_reasons", self.evidence_completeness_reasons),
            ("unresolved_items", self.unresolved_items),
            ("ceo_decisions_required", self.ceo_decisions_required),
        ):
            if tuple(sorted(set(values))) != values:
                raise Builder2QualificationDecisionPacketError(
                    f"{name} must be sorted and unique"
                )
            for value in values:
                _text(value, name)
        for name, value in (
            ("input_receipt_count", self.input_receipt_count),
            ("total_valid_receipts", self.total_valid_receipts),
            ("eligible_real_observation_count", self.eligible_real_observation_count),
            ("eligible_fixture_count", self.eligible_fixture_count),
        ):
            _nonnegative_int(value, name)
        if self.total_valid_receipts > self.input_receipt_count:
            raise Builder2QualificationDecisionPacketError(
                "valid receipts exceed input receipts"
            )
        if self.eligible_real_observation_count > self.total_valid_receipts:
            raise Builder2QualificationDecisionPacketError(
                "eligible observations exceed valid receipts"
            )
        if self.minimum_sample_policy is not None:
            self.minimum_sample_policy.validate()
        if self.sample_sufficient is not None and (
            self.minimum_sample_policy is None
            or not isinstance(self.sample_sufficient, bool)
        ):
            raise Builder2QualificationDecisionPacketError(
                "sample sufficiency lacks caller policy"
            )
        if self.minimum_sample_policy is None:
            if (
                self.sample_sufficient is not None
                or self.sample_state != SAMPLE_POLICY_NOT_SUPPLIED
            ):
                raise Builder2QualificationDecisionPacketError(
                    "sample state is inconsistent with absent caller policy"
                )
        else:
            expected_sufficient = (
                self.eligible_real_observation_count
                >= self.minimum_sample_policy.minimum_real_observations
                and self.eligible_fixture_count
                >= self.minimum_sample_policy.minimum_distinct_fixtures
            )
            if self.sample_sufficient is not expected_sufficient:
                raise Builder2QualificationDecisionPacketError(
                    "sample sufficiency does not match caller policy"
                )
            expected_state = (
                SAMPLE_MEETS_CALLER_POLICY
                if expected_sufficient
                else SAMPLE_BELOW_CALLER_POLICY
            )
            if self.sample_state != expected_state:
                raise Builder2QualificationDecisionPacketError(
                    "sample state does not match caller policy"
                )
        for mapping_name, mapping in (
            ("per_league_counts", self.per_league_counts),
            ("per_provider_counts", self.per_provider_counts),
            ("exclusion_taxonomy", self.exclusion_taxonomy),
        ):
            for key, value in mapping.items():
                _text(key, f"{mapping_name} key")
                _nonnegative_int(value, f"{mapping_name} value")
        _digest(self.sample_report_digest, "sample_report_digest")
        for name, availability in (
            ("freshness", self.freshness),
            ("observation_coverage", self.observation_coverage),
        ):
            if not isinstance(availability, Mapping):
                raise Builder2QualificationDecisionPacketError(
                    f"{name} must be an object"
                )
            _text(availability.get("metric"), f"{name}.metric")
            if not isinstance(availability.get("supported"), bool):
                raise Builder2QualificationDecisionPacketError(
                    f"{name}.supported must be boolean"
                )
        for item in self.receipt_provenance:
            item.validate()
        if (
            tuple(sorted(self.receipt_provenance, key=_provenance_key))
            != self.receipt_provenance
        ):
            raise Builder2QualificationDecisionPacketError(
                "receipt provenance is not ordered"
            )
        for item in self.qualification_report_summaries:
            item.validate()
        if (
            tuple(
                sorted(
                    self.qualification_report_summaries,
                    key=lambda item: item.report_digest,
                )
            )
            != self.qualification_report_summaries
        ):
            raise Builder2QualificationDecisionPacketError(
                "report summaries are not ordered"
            )
        for item in self.intake_summaries:
            item.validate()
        if (
            tuple(sorted(self.intake_summaries, key=lambda item: item.intake_id))
            != self.intake_summaries
        ):
            raise Builder2QualificationDecisionPacketError(
                "intake summaries are not ordered"
            )
        for item in self.fixture_provider_conflicts:
            item.validate()
        if (
            tuple(
                sorted(
                    self.fixture_provider_conflicts,
                    key=lambda item: (
                        item.fixture_key,
                        item.provider_identity,
                        item.reason,
                    ),
                )
            )
            != self.fixture_provider_conflicts
        ):
            raise Builder2QualificationDecisionPacketError(
                "fixture conflicts are not ordered"
            )
        if dict(self.safety) != _PACKET_SAFETY:
            raise Builder2QualificationDecisionPacketError(
                "decision packet safety state is unsafe"
            )
        expected_digest = semantic_digest(self._payload(include_identity=False))
        if self.packet_digest.lower() != expected_digest:
            raise Builder2QualificationDecisionPacketError(
                "decision packet digest mismatch"
            )
        if self.packet_id != f"b2dp-{expected_digest[:24]}":
            raise Builder2QualificationDecisionPacketError(
                "decision packet ID is not deterministic"
            )

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationDecisionPacketV1:
        raw = _strict_mapping(
            payload,
            required={
                "schema_version",
                "packet_id",
                "packet_digest",
                "status",
                "evidence_completeness",
                "sample_state",
                "intake_ids",
                "controlled_shadow_run_ids",
                "qualification_session_ids",
                "ceo_authorization_ids",
                "qualification_report_digests",
                "receipt_provenance",
                "qualification_report_summaries",
                "intake_summaries",
                "input_receipt_count",
                "total_valid_receipts",
                "eligible_real_observation_count",
                "eligible_fixture_count",
                "per_league_counts",
                "per_provider_counts",
                "duplicate_receipt_ids",
                "duplicate_observation_ids",
                "duplicate_observation_digests",
                "divergent_receipt_ids",
                "observation_identity_conflict_ids",
                "observation_identity_conflict_digests",
                "fixture_provider_conflicts",
                "unattributed_fixture_keys",
                "exclusion_taxonomy",
                "sample_report_digest",
                "freshness",
                "observation_coverage",
                "minimum_sample_policy",
                "sample_sufficient",
                "evidence_completeness_reasons",
                "unresolved_items",
                "ceo_decisions_required",
                "source_artifact_digests",
                "safety",
            },
            allowed={
                "schema_version",
                "packet_id",
                "packet_digest",
                "status",
                "evidence_completeness",
                "sample_state",
                "intake_ids",
                "controlled_shadow_run_ids",
                "qualification_session_ids",
                "ceo_authorization_ids",
                "qualification_report_digests",
                "receipt_provenance",
                "qualification_report_summaries",
                "intake_summaries",
                "input_receipt_count",
                "total_valid_receipts",
                "eligible_real_observation_count",
                "eligible_fixture_count",
                "per_league_counts",
                "per_provider_counts",
                "duplicate_receipt_ids",
                "duplicate_observation_ids",
                "duplicate_observation_digests",
                "divergent_receipt_ids",
                "observation_identity_conflict_ids",
                "observation_identity_conflict_digests",
                "fixture_provider_conflicts",
                "unattributed_fixture_keys",
                "exclusion_taxonomy",
                "sample_report_digest",
                "freshness",
                "observation_coverage",
                "minimum_sample_policy",
                "sample_sufficient",
                "evidence_completeness_reasons",
                "unresolved_items",
                "ceo_decisions_required",
                "source_artifact_digests",
                "safety",
            },
            name="decision packet",
        )
        policy = _policy_from_payload(
            raw["minimum_sample_policy"], "decision packet minimum_sample_policy"
        )
        conflicts = tuple(
            Builder2FixtureProviderConflictV1.from_payload(item)
            for item in raw["fixture_provider_conflicts"]
        )
        packet = cls(
            schema_version=raw["schema_version"],
            packet_id=_text(raw["packet_id"], "packet_id"),
            packet_digest=_text(raw["packet_digest"], "packet_digest"),
            status=raw["status"],
            evidence_completeness=raw["evidence_completeness"],
            sample_state=raw["sample_state"],
            intake_ids=_sorted_texts(raw["intake_ids"], "intake_ids"),
            controlled_shadow_run_ids=_sorted_texts(
                raw["controlled_shadow_run_ids"], "controlled_shadow_run_ids"
            ),
            qualification_session_ids=_sorted_texts(
                raw["qualification_session_ids"], "qualification_session_ids"
            ),
            ceo_authorization_ids=_sorted_texts(
                raw["ceo_authorization_ids"], "ceo_authorization_ids"
            ),
            qualification_report_digests=_sorted_texts(
                raw["qualification_report_digests"], "qualification_report_digests"
            ),
            receipt_provenance=tuple(
                Builder2QualificationReceiptProvenanceV1.from_payload(item)
                for item in raw["receipt_provenance"]
            ),
            qualification_report_summaries=tuple(
                Builder2DecisionPacketReportSummaryV1.from_payload(item)
                for item in raw["qualification_report_summaries"]
            ),
            intake_summaries=tuple(
                Builder2DecisionPacketIntakeSummaryV1.from_payload(item)
                for item in raw["intake_summaries"]
            ),
            input_receipt_count=raw["input_receipt_count"],
            total_valid_receipts=raw["total_valid_receipts"],
            eligible_real_observation_count=raw["eligible_real_observation_count"],
            eligible_fixture_count=raw["eligible_fixture_count"],
            per_league_counts=raw["per_league_counts"],
            per_provider_counts=raw["per_provider_counts"],
            duplicate_receipt_ids=_sorted_texts(
                raw["duplicate_receipt_ids"], "duplicate_receipt_ids"
            ),
            duplicate_observation_ids=_sorted_texts(
                raw["duplicate_observation_ids"], "duplicate_observation_ids"
            ),
            duplicate_observation_digests=_sorted_texts(
                raw["duplicate_observation_digests"], "duplicate_observation_digests"
            ),
            divergent_receipt_ids=_sorted_texts(
                raw["divergent_receipt_ids"], "divergent_receipt_ids"
            ),
            observation_identity_conflict_ids=_sorted_texts(
                raw["observation_identity_conflict_ids"],
                "observation_identity_conflict_ids",
            ),
            observation_identity_conflict_digests=_sorted_texts(
                raw["observation_identity_conflict_digests"],
                "observation_identity_conflict_digests",
            ),
            fixture_provider_conflicts=conflicts,
            unattributed_fixture_keys=_sorted_texts(
                raw["unattributed_fixture_keys"], "unattributed_fixture_keys"
            ),
            exclusion_taxonomy=raw["exclusion_taxonomy"],
            sample_report_digest=_text(
                raw["sample_report_digest"], "sample_report_digest"
            ),
            freshness=raw["freshness"],
            observation_coverage=raw["observation_coverage"],
            minimum_sample_policy=policy,
            sample_sufficient=raw["sample_sufficient"],
            evidence_completeness_reasons=_sorted_texts(
                raw["evidence_completeness_reasons"], "evidence_completeness_reasons"
            ),
            unresolved_items=_sorted_texts(raw["unresolved_items"], "unresolved_items"),
            ceo_decisions_required=_sorted_texts(
                raw["ceo_decisions_required"], "ceo_decisions_required"
            ),
            source_artifact_digests=tuple(
                dict(item) for item in raw["source_artifact_digests"]
            ),
            safety=raw["safety"],
        )
        packet.validate()
        return packet

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_identity=True)

    def as_markdown(self) -> str:
        self.validate()
        lines = [
            "# Top-5 B2 Controlled Shadow Decision Packet",
            "",
            f"- Status: `{self.status}`",
            f"- Packet: `{self.packet_id}`",
            f"- Digest: `{self.packet_digest}`",
            f"- Evidence completeness: `{self.evidence_completeness}`",
            f"- Sample state: `{self.sample_state}`",
            "",
            "## Evidence",
            "",
            f"- Eligible real observations: `{self.eligible_real_observation_count}`",
            f"- Eligible fixtures: `{self.eligible_fixture_count}`",
            f"- Valid receipts: `{self.total_valid_receipts}` of `{self.input_receipt_count}` inputs",
            f"- Controlled-shadow runs: `{', '.join(self.controlled_shadow_run_ids) or 'none'}`",
            f"- Qualification sessions: `{', '.join(self.qualification_session_ids) or 'none'}`",
            f"- CEO authorizations referenced: `{', '.join(self.ceo_authorization_ids) or 'none'}`",
            f"- Providers: `{', '.join(f'{k}={v}' for k, v in sorted(self.per_provider_counts.items())) or 'none'}`",
            f"- Leagues: `{', '.join(f'{k}={v}' for k, v in sorted(self.per_league_counts.items())) or 'none'}`",
            f"- Sample sufficient: `{self.sample_sufficient}`",
            "",
            "## Conflicts and unresolved items",
            "",
            f"- Exclusions: `{json.dumps(dict(sorted(self.exclusion_taxonomy.items())), sort_keys=True)}`",
            f"- Unresolved: `{', '.join(self.unresolved_items) or 'none'}`",
            "",
            "## CEO decisions still required",
            "",
        ]
        lines.extend(f"- {item}" for item in self.ceo_decisions_required)
        lines.extend(
            [
                "",
                "## Safety",
                "",
                "- Informational, validation-only packet.",
                "- Production activation, model, signal-time, publication, and betting authorization are all `false`.",
                "- No provider authority is selected; no network execution is performed by this layer.",
                "",
            ]
        )
        return "\n".join(lines)


def _safe_external_path(value: object, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise Builder2QualificationDecisionPacketError(f"{name} must be a path")
    path = Path(os.fspath(value))
    if not path.is_absolute():
        raise Builder2QualificationDecisionPacketError(f"{name} must be absolute")
    resolved = path.resolve()
    repo_root = Path(__file__).resolve().parents[2]
    if resolved == repo_root or repo_root in resolved.parents:
        raise Builder2QualificationDecisionPacketError(
            f"{name} must be outside the source checkout"
        )
    forbidden = {
        ".git",
        "credentials",
        "secrets",
        "sealed",
        "research",
        "ledger",
        "cloudflare",
        "launchd",
        "runtime",
        "production",
    }
    if {part.casefold() for part in resolved.parts} & forbidden:
        raise Builder2QualificationDecisionPacketError(
            f"{name} points at a protected path"
        )
    return resolved


def _load_json(path: Path, name: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Builder2QualificationDecisionPacketError(f"cannot read {name}") from exc
    if not isinstance(raw, Mapping):
        raise Builder2QualificationDecisionPacketError(f"{name} must contain an object")
    return dict(raw)


def load_decision_packet_inputs(path: str | Path) -> dict[str, tuple[object, ...]]:
    """Read canonical input artifacts from an external evidence directory."""

    root = _safe_external_path(path, "input directory")
    if not root.exists() or not root.is_dir():
        raise Builder2QualificationDecisionPacketError("input directory does not exist")
    artifacts: list[Builder2QualificationIntakeArtifactV1] = []
    known_paths: set[Path] = set()
    for manifest_path in sorted(root.rglob("manifest.json")):
        parent = manifest_path.parent
        known_paths.add(manifest_path)
        payload: dict[str, object] = {"artifact_path": str(parent)}
        for filename, field in (
            ("manifest.json", "manifest"),
            ("qualification_report.json", "qualification_report"),
            ("receipt.json", "receipt"),
            ("result.json", "result"),
            ("sample_report.json", "sample_report"),
        ):
            candidate = parent / filename
            if candidate.exists():
                known_paths.add(candidate)
                payload[field] = _load_json(candidate, filename)
        artifacts.append(Builder2QualificationIntakeArtifactV1.from_payload(payload))
    receipts: list[Builder2QualificationReceiptV1] = []
    samples: list[Builder2QualificationSampleReportV1] = []
    reports: list[Mapping[str, object]] = []
    for candidate in sorted(root.rglob("*.json")):
        if candidate in known_paths:
            continue
        raw = _load_json(candidate, "canonical input artifact")
        schema = raw.get("schema_version") or raw.get("contract_version")
        if schema == "top5-builder2-qualification-receipt-v1":
            receipts.append(_receipt(raw, str(candidate)))
        elif schema == BUILDER2_QUALIFICATION_SAMPLE_AGGREGATOR_CONTRACT_VERSION:
            samples.append(_sample_report(raw, str(candidate)))
        elif schema == QUALIFICATION_CONTRACT_VERSION:
            reports.append(raw)
        elif schema in {INTAKE_CONTRACT_VERSION, RESULT_CONTRACT_VERSION}:
            raise Builder2QualificationDecisionPacketError(
                f"orphan intake artifact is not a complete intake directory: {candidate.name}"
            )
        else:
            raise Builder2QualificationDecisionPacketError(
                f"unsupported input artifact: {candidate.name}"
            )
    return {
        "receipts": tuple(receipts),
        "qualification_reports": tuple(reports),
        "sample_reports": tuple(samples),
        "intake_artifacts": tuple(artifacts),
    }


def build_decision_packet_from_directory(
    path: str | Path,
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
) -> Builder2QualificationDecisionPacketV1:
    inputs = load_decision_packet_inputs(path)
    return build_builder2_qualification_decision_packet(
        **inputs, minimum_sample_policy=minimum_sample_policy
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only Top-5 B2 CEO decision packet."
    )
    parser.add_argument("input_directory")
    parser.add_argument("--output-json")
    parser.add_argument("--output-markdown")
    parser.add_argument("--minimum-real-observations", type=int)
    parser.add_argument("--minimum-distinct-fixtures", type=int)
    try:
        args = parser.parse_args(argv)
        if (args.minimum_real_observations is None) != (
            args.minimum_distinct_fixtures is None
        ):
            raise Builder2QualificationDecisionPacketError(
                "caller sample policy values must be supplied together"
            )
        policy = None
        if args.minimum_real_observations is not None:
            policy = MinimumSamplePolicy(
                args.minimum_real_observations, args.minimum_distinct_fixtures
            )
            policy.validate()
        packet = build_decision_packet_from_directory(
            args.input_directory, minimum_sample_policy=policy
        )
        if args.output_json:
            atomic_write_json(
                _safe_external_path(args.output_json, "output JSON"),
                packet.as_payload(),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        if args.output_markdown:
            atomic_write_text(
                _safe_external_path(args.output_markdown, "output Markdown"),
                packet.as_markdown(),
            )
        print(
            json.dumps(
                packet.as_payload(), indent=2, ensure_ascii=False, sort_keys=True
            )
        )
        print(packet.as_markdown())
        return 0
    except (
        Builder2QualificationDecisionPacketError,
        QualificationContractError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": FAILED_CLOSED, "error": str(exc), "safety": _PACKET_SAFETY},
                sort_keys=True,
            ),
            file=os.sys.stderr,
        )
        return 2


build_decision_packet = build_builder2_qualification_decision_packet
Builder2QualificationDecisionPacket = Builder2QualificationDecisionPacketV1

__all__ = [
    "AUTHORITY_MISMATCH",
    "DECISION_PACKET_CONTRACT_VERSION",
    "DECISION_PACKET_SCHEMA_VERSION",
    "EVIDENCE_COMPLETE",
    "EVIDENCE_CONFLICT",
    "EVIDENCE_INCOMPLETE",
    "FAILED_CLOSED",
    "SAMPLE_BELOW_CALLER_POLICY",
    "SAMPLE_MEETS_CALLER_POLICY",
    "SAMPLE_POLICY_NOT_SUPPLIED",
    "Builder2DecisionPacketIntakeSummaryV1",
    "Builder2DecisionPacketReportSummaryV1",
    "Builder2QualificationDecisionPacket",
    "Builder2QualificationDecisionPacketError",
    "Builder2QualificationDecisionPacketV1",
    "Builder2QualificationIntakeArtifactV1",
    "build_builder2_qualification_decision_packet",
    "build_decision_packet",
    "build_decision_packet_from_directory",
    "load_decision_packet_inputs",
    "main",
]
