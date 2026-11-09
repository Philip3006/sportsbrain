"""Deterministic, offline evidence contract for future Champions League work.

This module is deliberately a validator, not a qualification runner.  It
does not load models or datasets, call providers, write runtime state, or
authorize publication.  Every accepted report is a signed description of
evidence supplied by its caller; missing, copied, stale, or inconsistent
evidence is rejected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import Any

CONTRACT_VERSION = "champions-league-candidate-evidence-v1"
OFFLINE_QUALIFICATION_CONTRACT_VERSION = CONTRACT_VERSION
COMPETITION_ID = "uefa_champions_league"
COMPETITION_NAME = "UEFA Champions League"
CHAMPIONS_LEAGUE_COMPETITION_ID = COMPETITION_ID
CHAMPIONS_LEAGUE_COMPETITION_NAME = COMPETITION_NAME
CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION = CONTRACT_VERSION
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_FORBIDDEN_INPUT_TOKENS = (
    "closing",
    "final_odds",
    "post_match",
    "post_kickoff",
    "after_kickoff",
    "future",
)


class ChampionsLeagueCandidateEvidenceError(ValueError):
    """Raised when an offline candidate-evidence contract is not admissible."""


ChampionsLeagueQualificationError = ChampionsLeagueCandidateEvidenceError


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ChampionsLeagueCandidateEvidenceError(f"{name} is required")
    return value


def _identity(value: object, name: str) -> str:
    text = _text(value, name)
    if _ID_RE.fullmatch(text) is None:
        raise ChampionsLeagueCandidateEvidenceError(f"{name} is malformed")
    return text


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if _SHA_RE.fullmatch(text) is None:
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be a hexadecimal digest")
    return text


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise ChampionsLeagueCandidateEvidenceError(
                f"{name} must be an ISO-8601 timestamp"
            ) from exc
    raise ChampionsLeagueCandidateEvidenceError(f"{name} is required")


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be boolean")
    return value


def _count(value: object, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be a non-negative integer")
    if value == 0 and not allow_zero:
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be positive")
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be a sequence")
    return tuple(value)


def _input_name(value: object, name: str) -> str:
    text = _text(value, name)
    token = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    if not token or any(
        token == forbidden or token.startswith(f"{forbidden}_")
        for forbidden in _FORBIDDEN_INPUT_TOKENS
    ):
        raise ChampionsLeagueCandidateEvidenceError(
            f"{name} contains future or closing-market information"
        )
    return text


def _component(value: object) -> object:
    if value is None:
        return None
    if hasattr(value, "as_payload"):
        return value.as_payload()
    if isinstance(value, Mapping):
        return dict(value)
    raise ChampionsLeagueCandidateEvidenceError("evidence component is malformed")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChampionsLeagueCandidateEvidenceError(f"{name} must be an object")
    return value


@dataclass(frozen=True)
class CandidateArtifactIdentity:
    candidate_id: str
    artifact_id: str
    artifact_sha: str
    version: str
    artifact_kind: str
    identity_sha: str
    dataset_manifest_id: str = ""
    dataset_manifest_sha: str = ""

    @classmethod
    def build(
        cls,
        *,
        candidate_id: str,
        artifact_id: str,
        artifact_sha: str,
        version: str,
        artifact_kind: str = "model",
        dataset_manifest_id: str = "",
        dataset_manifest_sha: str = "",
    ) -> "CandidateArtifactIdentity":
        value = cls(
            candidate_id, artifact_id, artifact_sha, version, artifact_kind, "0" * 64,
            dataset_manifest_id, dataset_manifest_sha,
        )
        return replace(value, identity_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "CandidateArtifactIdentity":
        item = _mapping(raw, "candidate artifact identity")
        return cls(
            item.get("candidate_id", ""),
            item.get("artifact_id", item.get("model_artifact_id", "")),
            item.get("artifact_sha", item.get("model_artifact_sha", "")),
            item.get("version", item.get("artifact_version", "")),
            item.get("artifact_kind", "model"),
            item.get("identity_sha", item.get("candidate_artifact_identity_sha", "")),
            item.get("dataset_manifest_id", item.get("manifest_id", "")),
            item.get("dataset_manifest_sha", item.get("manifest_sha", "")),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "artifact_id": self.artifact_id,
            "artifact_sha": str(self.artifact_sha).lower(),
            "version": self.version,
            "artifact_kind": self.artifact_kind,
            "dataset_manifest_id": self.dataset_manifest_id,
            "dataset_manifest_sha": str(self.dataset_manifest_sha).lower(),
        }

    def validate(self, *, require_dataset_binding: bool = False) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.artifact_id, "artifact_id"),
            (self.version, "candidate artifact version"),
            (self.artifact_kind, "candidate artifact kind"),
        ):
            _identity(value, name)
        _sha(self.artifact_sha, "candidate artifact_sha")
        _sha(self.identity_sha, "candidate artifact identity_sha")
        if require_dataset_binding:
            _identity(self.dataset_manifest_id, "candidate dataset_manifest_id")
            _sha(self.dataset_manifest_sha, "candidate dataset_manifest_sha")
        elif self.dataset_manifest_id or self.dataset_manifest_sha:
            _identity(self.dataset_manifest_id, "candidate dataset_manifest_id")
            _sha(self.dataset_manifest_sha, "candidate dataset_manifest_sha")
        if _digest(self._payload_without_sha()) != self.identity_sha:
            raise ChampionsLeagueCandidateEvidenceError(
                "candidate artifact identity digest changed"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "identity_sha": self.identity_sha}


@dataclass(frozen=True)
class DatasetManifestBinding:
    manifest_id: str
    dataset_id: str
    manifest_sha: str
    partition_ids: tuple[str, ...]
    schema_version: str
    binding_sha: str

    @classmethod
    def build(
        cls,
        *,
        manifest_id: str = "",
        dataset_id: str = "",
        manifest_sha: str = "",
        partition_ids: Sequence[str] = (),
        schema_version: str = "v1",
        dataset_manifest_id: str | None = None,
        training_partition_id: str | None = None,
        calibration_partition_id: str | None = None,
        holdout_partition_id: str | None = None,
    ) -> "DatasetManifestBinding":
        if dataset_manifest_id is not None:
            manifest_id = dataset_manifest_id
        declared_partitions = tuple(partition_ids) or tuple(
            value
            for value in (
                training_partition_id,
                calibration_partition_id,
                holdout_partition_id,
            )
            if value is not None
        )
        value = cls(
            manifest_id, dataset_id, manifest_sha, declared_partitions, schema_version, "0" * 64
        )
        return replace(value, binding_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "DatasetManifestBinding":
        item = _mapping(raw, "dataset manifest binding")
        return cls(
            item.get("manifest_id", item.get("dataset_manifest_id", "")),
            item.get("dataset_id", ""),
            item.get("manifest_sha", item.get("dataset_manifest_sha", "")),
            tuple(str(v) for v in _sequence(item.get("partition_ids", item.get("partitions", ())), "manifest partition_ids")),
            item.get("schema_version", ""),
            item.get("binding_sha", item.get("dataset_manifest_binding_sha", "")),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "dataset_id": self.dataset_id,
            "manifest_sha": self.manifest_sha.lower(),
            "partition_ids": list(self.partition_ids),
            "schema_version": self.schema_version,
        }

    def validate(self) -> None:
        for value, name in (
            (self.manifest_id, "manifest_id"),
            (self.dataset_id, "manifest dataset_id"),
            (self.schema_version, "manifest schema_version"),
        ):
            _identity(value, name)
        _sha(self.manifest_sha, "manifest_sha")
        if not self.partition_ids:
            raise ChampionsLeagueCandidateEvidenceError("dataset manifest has no partitions")
        normalized = tuple(_identity(value, "manifest partition_id") for value in self.partition_ids)
        if len(set(normalized)) != len(normalized):
            raise ChampionsLeagueCandidateEvidenceError("dataset manifest has duplicate partitions")
        _sha(self.binding_sha, "dataset manifest binding_sha")
        if _digest(self._payload_without_sha()) != self.binding_sha:
            raise ChampionsLeagueCandidateEvidenceError("dataset manifest binding digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "binding_sha": self.binding_sha}


DatasetManifestIdentity = DatasetManifestBinding
DatasetManifest = DatasetManifestBinding
ChampionsLeagueDatasetManifest = DatasetManifestBinding


@dataclass(frozen=True)
class FeatureSchemaIdentity:
    schema_id: str
    schema_version: str
    feature_names: tuple[str, ...]
    schema_sha: str

    @classmethod
    def build(cls, *, schema_id: str, schema_version: str, feature_names: Sequence[str]) -> "FeatureSchemaIdentity":
        value = cls(schema_id, schema_version, tuple(feature_names), "0" * 64)
        return replace(value, schema_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "FeatureSchemaIdentity":
        item = _mapping(raw, "feature schema identity")
        return cls(
            item.get("schema_id", item.get("feature_schema_id", "")),
            item.get("schema_version", ""),
            tuple(str(v) for v in _sequence(item.get("feature_names", item.get("features", ())), "feature_names")),
            item.get("schema_sha", item.get("feature_schema_hash", "")),
        )

    def _payload_without_sha(self) -> dict[str, object]:
        return {"schema_id": self.schema_id, "schema_version": self.schema_version, "feature_names": list(self.feature_names)}

    def validate(self) -> None:
        _identity(self.schema_id, "feature schema_id")
        _identity(self.schema_version, "feature schema_version")
        if not self.feature_names:
            raise ChampionsLeagueCandidateEvidenceError("feature schema must declare features")
        names = tuple(_input_name(value, "feature name") for value in self.feature_names)
        if len(set(names)) != len(names):
            raise ChampionsLeagueCandidateEvidenceError("feature schema contains duplicate features")
        _sha(self.schema_sha, "feature schema_sha")
        if _digest(self._payload_without_sha()) != self.schema_sha:
            raise ChampionsLeagueCandidateEvidenceError("feature schema identity digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "schema_sha": self.schema_sha}


@dataclass(frozen=True)
class ApprovedInformationTimeBoundary:
    boundary_id: str
    cutoff_at: datetime
    approved: bool
    approved_by: str
    approved_at: datetime
    policy: str
    boundary_sha: str

    @classmethod
    def build(cls, *, boundary_id: str, cutoff_at: datetime, approved_by: str, approved_at: datetime, policy: str, approved: bool = True) -> "ApprovedInformationTimeBoundary":
        value = cls(boundary_id, cutoff_at, approved, approved_by, approved_at, policy, "0" * 64)
        return replace(value, boundary_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ApprovedInformationTimeBoundary":
        item = _mapping(raw, "information-time boundary")
        return cls(item.get("boundary_id", ""), _parse_time(item.get("cutoff_at"), "boundary cutoff_at"), item.get("approved", False), item.get("approved_by", ""), _parse_time(item.get("approved_at"), "boundary approved_at"), item.get("policy", ""), item.get("boundary_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"boundary_id": self.boundary_id, "cutoff_at": _utc(self.cutoff_at, "boundary cutoff_at").isoformat(), "approved": self.approved, "approved_by": self.approved_by, "approved_at": _utc(self.approved_at, "boundary approved_at").isoformat(), "policy": self.policy}

    def validate(self) -> None:
        _identity(self.boundary_id, "boundary_id")
        _utc(self.cutoff_at, "boundary cutoff_at")
        if _bool(self.approved, "boundary approved") is not True:
            raise ChampionsLeagueCandidateEvidenceError("information-time boundary is not approved")
        _identity(self.approved_by, "boundary approved_by")
        _utc(self.approved_at, "boundary approved_at")
        _text(self.policy, "boundary policy")
        _sha(self.boundary_sha, "boundary_sha")
        if _digest(self._payload_without_sha()) != self.boundary_sha:
            raise ChampionsLeagueCandidateEvidenceError("information-time boundary identity changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "boundary_sha": self.boundary_sha}


InformationTimeBoundary = ApprovedInformationTimeBoundary


@dataclass(frozen=True)
class TrainingPartitionIdentity:
    partition_id: str
    dataset_id: str
    partition_role: str
    partition_sha: str
    start_at: datetime
    end_at: datetime
    row_count: int
    boundary_id: str
    dataset_manifest_id: str = ""
    dataset_manifest_sha: str = ""

    @classmethod
    def build(cls, *, partition_id: str, dataset_id: str, start_at: datetime, end_at: datetime, row_count: int, boundary_id: str = "", partition_role: str = "training", dataset_manifest_id: str = "", dataset_manifest_sha: str = "") -> "TrainingPartitionIdentity":
        value = cls(partition_id, dataset_id, partition_role, "0" * 64, start_at, end_at, row_count, boundary_id, dataset_manifest_id, dataset_manifest_sha)
        return replace(value, partition_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "TrainingPartitionIdentity":
        item = _mapping(raw, "training partition identity")
        return cls(item.get("partition_id", item.get("training_partition_id", "")), item.get("dataset_id", ""), item.get("partition_role", item.get("role", "")), item.get("partition_sha", item.get("training_partition_sha", "")), _parse_time(item.get("start_at"), "training partition start_at"), _parse_time(item.get("end_at"), "training partition end_at"), item.get("row_count", -1), item.get("boundary_id", ""), item.get("dataset_manifest_id", ""), item.get("dataset_manifest_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"partition_id": self.partition_id, "dataset_id": self.dataset_id, "partition_role": self.partition_role, "start_at": _utc(self.start_at, "training partition start_at").isoformat(), "end_at": _utc(self.end_at, "training partition end_at").isoformat(), "row_count": self.row_count, "boundary_id": self.boundary_id, "dataset_manifest_id": self.dataset_manifest_id, "dataset_manifest_sha": self.dataset_manifest_sha.lower()}

    def validate(self, manifest: DatasetManifestBinding | None = None, boundary: ApprovedInformationTimeBoundary | None = None, *, require_manifest_binding: bool = False) -> None:
        _identity(self.partition_id, "training partition_id")
        _identity(self.dataset_id, "training dataset_id")
        if self.partition_role.casefold() not in {"training", "development"}:
            raise ChampionsLeagueCandidateEvidenceError("training partition identity must declare the training role")
        start, end = _utc(self.start_at, "training partition start_at"), _utc(self.end_at, "training partition end_at")
        if end <= start:
            raise ChampionsLeagueCandidateEvidenceError("training partition window is empty")
        _sha(self.partition_sha, "training partition_sha")
        _count(self.row_count, "training partition row_count")
        if boundary is not None:
            boundary.validate()
            if not self.boundary_id or self.boundary_id != boundary.boundary_id:
                raise ChampionsLeagueCandidateEvidenceError("training partition uses a different time boundary")
            if end > boundary.cutoff_at:
                raise ChampionsLeagueCandidateEvidenceError("training partition extends beyond the approved boundary")
        if require_manifest_binding and manifest is None:
            raise ChampionsLeagueCandidateEvidenceError("training partition dataset manifest binding is required")
        if manifest is not None:
            manifest.validate()
            if self.dataset_id != manifest.dataset_id:
                raise ChampionsLeagueCandidateEvidenceError("training partition uses a different dataset")
            if self.dataset_manifest_id != manifest.manifest_id or self.dataset_manifest_sha.lower() != manifest.manifest_sha:
                raise ChampionsLeagueCandidateEvidenceError("training partition is not bound to the dataset manifest")
            if self.partition_id not in manifest.partition_ids:
                raise ChampionsLeagueCandidateEvidenceError("training partition is absent from the dataset manifest")
        if require_manifest_binding:
            _identity(self.dataset_manifest_id, "training dataset_manifest_id")
            _sha(self.dataset_manifest_sha, "training dataset_manifest_sha")
        if _digest(self._payload_without_sha()) != self.partition_sha:
            raise ChampionsLeagueCandidateEvidenceError("training partition identity changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload_without_sha(), "partition_sha": self.partition_sha}


@dataclass(frozen=True)
class CalibrationEvidence:
    evidence_id: str
    partition_id: str
    partition_sha: str
    candidate_artifact_id: str
    artifact_sha: str
    feature_schema_sha: str
    boundary_id: str
    provenance_id: str
    metric_name: str
    metric_value: float
    sample_count: int
    passed: bool
    evidence_sha: str
    partition_role: str = "calibration"
    dataset_manifest_id: str = ""
    dataset_manifest_sha: str = ""

    @classmethod
    def build(cls, *, evidence_id: str, partition_id: str, partition_sha: str, candidate_artifact_id: str, artifact_sha: str, feature_schema_sha: str, boundary_id: str = "", provenance_id: str, metric_name: str, metric_value: float, sample_count: int, passed: bool = True, partition_role: str = "calibration", dataset_manifest_id: str = "", dataset_manifest_sha: str = "") -> "CalibrationEvidence":
        value = cls(evidence_id, partition_id, partition_sha, candidate_artifact_id, artifact_sha, feature_schema_sha, boundary_id, provenance_id, metric_name, metric_value, sample_count, passed, "0" * 64, partition_role, dataset_manifest_id, dataset_manifest_sha)
        return replace(value, evidence_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "CalibrationEvidence":
        item = _mapping(raw, "calibration evidence")
        return cls(item.get("evidence_id", ""), item.get("partition_id", ""), item.get("partition_sha", ""), item.get("candidate_artifact_id", item.get("artifact_id", "")), item.get("artifact_sha", ""), item.get("feature_schema_sha", item.get("feature_schema_hash", "")), item.get("boundary_id", ""), item.get("provenance_id", ""), item.get("metric_name", ""), item.get("metric_value", float("nan")), item.get("sample_count", -1), item.get("passed", False), item.get("evidence_sha", ""), item.get("partition_role", "calibration"), item.get("dataset_manifest_id", ""), item.get("dataset_manifest_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"evidence_id": self.evidence_id, "partition_id": self.partition_id, "partition_sha": self.partition_sha.lower(), "candidate_artifact_id": self.candidate_artifact_id, "artifact_sha": self.artifact_sha.lower(), "feature_schema_sha": self.feature_schema_sha.lower(), "boundary_id": self.boundary_id, "provenance_id": self.provenance_id, "metric_name": self.metric_name, "metric_value": self.metric_value, "sample_count": self.sample_count, "passed": self.passed, "partition_role": self.partition_role, "dataset_manifest_id": self.dataset_manifest_id, "dataset_manifest_sha": self.dataset_manifest_sha.lower()}

    def validate(self, candidate: CandidateArtifactIdentity | None = None, schema: FeatureSchemaIdentity | None = None, manifest: DatasetManifestBinding | None = None) -> None:
        for value, name in ((self.evidence_id, "calibration evidence_id"), (self.partition_id, "calibration partition_id"), (self.candidate_artifact_id, "calibration candidate_artifact_id"), (self.provenance_id, "calibration provenance_id")):
            _identity(value, name)
        _sha(self.partition_sha, "calibration partition_sha"); _sha(self.artifact_sha, "calibration artifact_sha"); _sha(self.feature_schema_sha, "calibration feature_schema_sha")
        if self.partition_role.casefold() != "calibration":
            raise ChampionsLeagueCandidateEvidenceError("evidence is not calibration evidence")
        if self.boundary_id: _identity(self.boundary_id, "calibration boundary_id")
        _text(self.metric_name, "calibration metric_name")
        if not isinstance(self.metric_value, (int, float)) or not isfinite(float(self.metric_value)):
            raise ChampionsLeagueCandidateEvidenceError("calibration metric_value must be finite")
        _count(self.sample_count, "calibration sample_count")
        if _bool(self.passed, "calibration passed") is not True:
            raise ChampionsLeagueCandidateEvidenceError("calibration evidence did not pass")
        _sha(self.evidence_sha, "calibration evidence_sha")
        if candidate is not None and (self.candidate_artifact_id != candidate.artifact_id or self.artifact_sha.lower() != candidate.artifact_sha.lower()):
            raise ChampionsLeagueCandidateEvidenceError("calibration evidence is bound to a different artifact")
        if schema is not None and self.feature_schema_sha.lower() != schema.schema_sha:
            raise ChampionsLeagueCandidateEvidenceError("calibration evidence uses a different feature schema")
        if manifest is not None and (self.dataset_manifest_id != manifest.manifest_id or self.dataset_manifest_sha.lower() != manifest.manifest_sha):
            raise ChampionsLeagueCandidateEvidenceError("calibration evidence is not bound to the dataset manifest")
        if _digest(self._payload_without_sha()) != self.evidence_sha:
            raise ChampionsLeagueCandidateEvidenceError("calibration evidence digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate(); return {**self._payload_without_sha(), "evidence_sha": self.evidence_sha}


@dataclass(frozen=True)
class SourceProvenance:
    provenance_id: str
    source_name: str
    source_sha: str
    dataset_id: str
    available_at: datetime
    retrieved_at: datetime
    row_count: int
    boundary_id: str
    immutable: bool
    authoritative: bool
    provenance_sha: str
    role: str = "dataset"
    manifest_id: str = ""
    manifest_sha: str = ""

    @classmethod
    def build(cls, *, provenance_id: str, source_name: str, source_sha: str, dataset_id: str, available_at: datetime, retrieved_at: datetime, row_count: int, boundary_id: str = "", immutable: bool = True, authoritative: bool = True, role: str = "dataset", manifest_id: str = "", manifest_sha: str = "") -> "SourceProvenance":
        value = cls(provenance_id, source_name, source_sha, dataset_id, available_at, retrieved_at, row_count, boundary_id, immutable, authoritative, "0" * 64, role, manifest_id, manifest_sha)
        return replace(value, provenance_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "SourceProvenance":
        item = _mapping(raw, "source provenance")
        return cls(item.get("provenance_id", item.get("source_provenance_id", "")), item.get("source_name", item.get("source", "")), item.get("source_sha", item.get("dataset_sha", "")), item.get("dataset_id", ""), _parse_time(item.get("available_at", item.get("as_of")), "source available_at"), _parse_time(item.get("retrieved_at"), "source retrieved_at"), item.get("row_count", -1), item.get("boundary_id", ""), item.get("immutable", False), item.get("authoritative", False), item.get("provenance_sha", ""), item.get("role", "dataset"), item.get("manifest_id", ""), item.get("manifest_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"provenance_id": self.provenance_id, "source_name": self.source_name, "source_sha": self.source_sha.lower(), "dataset_id": self.dataset_id, "available_at": _utc(self.available_at, "source available_at").isoformat(), "retrieved_at": _utc(self.retrieved_at, "source retrieved_at").isoformat(), "row_count": self.row_count, "boundary_id": self.boundary_id, "immutable": self.immutable, "authoritative": self.authoritative, "role": self.role, "manifest_id": self.manifest_id, "manifest_sha": self.manifest_sha.lower()}

    def validate(self, manifest: DatasetManifestBinding | None = None) -> None:
        for value, name in ((self.provenance_id, "provenance_id"), (self.source_name, "source_name"), (self.dataset_id, "source dataset_id"), (self.role, "source role")):
            _identity(value, name)
        _sha(self.source_sha, "source_sha")
        available, retrieved = _utc(self.available_at, "source available_at"), _utc(self.retrieved_at, "source retrieved_at")
        if retrieved < available: raise ChampionsLeagueCandidateEvidenceError("source retrieval predates source availability")
        _count(self.row_count, "source row_count")
        if _bool(self.immutable, "source immutable") is not True or _bool(self.authoritative, "source authoritative") is not True:
            raise ChampionsLeagueCandidateEvidenceError("source provenance is not authoritative and immutable")
        _sha(self.provenance_sha, "provenance_sha")
        if manifest is not None:
            manifest.validate()
            if self.dataset_id != manifest.dataset_id or self.manifest_id != manifest.manifest_id or self.manifest_sha.lower() != manifest.manifest_sha:
                raise ChampionsLeagueCandidateEvidenceError("source provenance is not bound to the dataset manifest")
        if _digest(self._payload_without_sha()) != self.provenance_sha:
            raise ChampionsLeagueCandidateEvidenceError("source provenance digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate(); return {**self._payload_without_sha(), "provenance_sha": self.provenance_sha}


ProvenanceRecord = SourceProvenance


@dataclass(frozen=True)
class ContaminationPass:
    check_id: str
    passed: bool
    checked_at: datetime
    method: str
    training_source_sha: str
    calibration_source_sha: str
    final_evaluation_source_sha: str
    overlap_count: int
    leakage_count: int
    check_sha: str
    manifest_sha: str = ""

    @classmethod
    def build(cls, *, check_id: str, checked_at: datetime, method: str, training_source_sha: str, calibration_source_sha: str, final_evaluation_source_sha: str, overlap_count: int = 0, leakage_count: int = 0, passed: bool = True, manifest_sha: str = "") -> "ContaminationPass":
        value = cls(check_id, passed, checked_at, method, training_source_sha, calibration_source_sha, final_evaluation_source_sha, overlap_count, leakage_count, "0" * 64, manifest_sha)
        return replace(value, check_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ContaminationPass":
        item = _mapping(raw, "contamination pass")
        return cls(item.get("check_id", ""), item.get("passed", False), _parse_time(item.get("checked_at"), "contamination checked_at"), item.get("method", ""), item.get("training_source_sha", ""), item.get("calibration_source_sha", ""), item.get("final_evaluation_source_sha", ""), item.get("overlap_count", -1), item.get("leakage_count", -1), item.get("check_sha", ""), item.get("manifest_sha", item.get("dataset_manifest_sha", "")))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"check_id": self.check_id, "passed": self.passed, "checked_at": _utc(self.checked_at, "contamination checked_at").isoformat(), "method": self.method, "training_source_sha": self.training_source_sha.lower(), "calibration_source_sha": self.calibration_source_sha.lower(), "final_evaluation_source_sha": self.final_evaluation_source_sha.lower(), "overlap_count": self.overlap_count, "leakage_count": self.leakage_count, "manifest_sha": self.manifest_sha.lower()}

    def validate(self, manifest: DatasetManifestBinding | None = None) -> None:
        _identity(self.check_id, "contamination check_id")
        if _bool(self.passed, "contamination passed") is not True: raise ChampionsLeagueCandidateEvidenceError("contamination check did not pass")
        _utc(self.checked_at, "contamination checked_at"); _text(self.method, "contamination method")
        for value, name in ((self.training_source_sha, "contamination training_source_sha"), (self.calibration_source_sha, "contamination calibration_source_sha"), (self.final_evaluation_source_sha, "contamination final_evaluation_source_sha")): _sha(value, name)
        if _count(self.overlap_count, "contamination overlap_count", allow_zero=True) != 0: raise ChampionsLeagueCandidateEvidenceError("contamination overlap is non-zero")
        if _count(self.leakage_count, "contamination leakage_count", allow_zero=True) != 0: raise ChampionsLeagueCandidateEvidenceError("contamination leakage is non-zero")
        _sha(self.check_sha, "contamination check_sha")
        if manifest is not None and self.manifest_sha.lower() != manifest.manifest_sha: raise ChampionsLeagueCandidateEvidenceError("contamination result is not bound to the dataset manifest")
        if _digest(self._payload_without_sha()) != self.check_sha: raise ChampionsLeagueCandidateEvidenceError("contamination pass digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate(); return {**self._payload_without_sha(), "check_sha": self.check_sha}


ContaminationResult = ContaminationPass
ContaminationEvidence = ContaminationPass


@dataclass(frozen=True)
class RuntimeInputs:
    runtime_id: str
    runtime_version: str
    candidate_artifact_id: str
    artifact_sha: str
    feature_schema_sha: str
    boundary_id: str
    input_names: tuple[str, ...]
    input_sha: str
    config_sha: str
    provenance_id: str
    runtime_sha: str
    dataset_manifest_id: str = ""
    dataset_manifest_sha: str = ""

    @classmethod
    def build(cls, *, runtime_id: str, runtime_version: str, candidate_artifact_id: str, artifact_sha: str, feature_schema_sha: str, boundary_id: str = "", input_names: Sequence[str], config_sha: str, provenance_id: str, dataset_manifest_id: str = "", dataset_manifest_sha: str = "") -> "RuntimeInputs":
        names = tuple(input_names); value = cls(runtime_id, runtime_version, candidate_artifact_id, artifact_sha, feature_schema_sha, boundary_id, names, _digest({"input_names": list(names)}), config_sha, provenance_id, "0" * 64, dataset_manifest_id, dataset_manifest_sha)
        return replace(value, runtime_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RuntimeInputs":
        item = _mapping(raw, "runtime inputs")
        names = tuple(str(v) for v in _sequence(item.get("input_names", item.get("runtime_input_names", ())), "runtime input_names"))
        return cls(item.get("runtime_id", ""), item.get("runtime_version", ""), item.get("candidate_artifact_id", item.get("artifact_id", "")), item.get("artifact_sha", ""), item.get("feature_schema_sha", item.get("feature_schema_hash", "")), item.get("boundary_id", ""), names, item.get("input_sha", ""), item.get("config_sha", ""), item.get("provenance_id", item.get("source_provenance_id", "")), item.get("runtime_sha", ""), item.get("dataset_manifest_id", ""), item.get("dataset_manifest_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"runtime_id": self.runtime_id, "runtime_version": self.runtime_version, "candidate_artifact_id": self.candidate_artifact_id, "artifact_sha": self.artifact_sha.lower(), "feature_schema_sha": self.feature_schema_sha.lower(), "boundary_id": self.boundary_id, "input_names": list(self.input_names), "input_sha": self.input_sha.lower(), "config_sha": self.config_sha.lower(), "provenance_id": self.provenance_id, "dataset_manifest_id": self.dataset_manifest_id, "dataset_manifest_sha": self.dataset_manifest_sha.lower()}

    def validate(self, candidate: CandidateArtifactIdentity | None = None, schema: FeatureSchemaIdentity | None = None, manifest: DatasetManifestBinding | None = None) -> None:
        for value, name in ((self.runtime_id, "runtime_id"), (self.runtime_version, "runtime_version"), (self.candidate_artifact_id, "runtime candidate_artifact_id"), (self.provenance_id, "runtime provenance_id")): _identity(value, name)
        _sha(self.artifact_sha, "runtime artifact_sha"); _sha(self.feature_schema_sha, "runtime feature_schema_sha"); _sha(self.config_sha, "runtime config_sha"); _sha(self.input_sha, "runtime input_sha"); _sha(self.runtime_sha, "runtime_sha")
        names = tuple(_input_name(value, "runtime input name") for value in self.input_names)
        if not names or len(set(names)) != len(names): raise ChampionsLeagueCandidateEvidenceError("runtime inputs must be non-empty and unique")
        if _digest({"input_names": list(names)}) != self.input_sha: raise ChampionsLeagueCandidateEvidenceError("runtime input identity changed")
        if candidate is not None and (self.candidate_artifact_id != candidate.artifact_id or self.artifact_sha.lower() != candidate.artifact_sha.lower()): raise ChampionsLeagueCandidateEvidenceError("runtime uses a different candidate artifact")
        if schema is not None and (self.feature_schema_sha.lower() != schema.schema_sha or set(names) != set(schema.feature_names)): raise ChampionsLeagueCandidateEvidenceError("runtime inputs are incompatible with the feature schema")
        if manifest is not None and (self.dataset_manifest_id != manifest.manifest_id or self.dataset_manifest_sha.lower() != manifest.manifest_sha): raise ChampionsLeagueCandidateEvidenceError("runtime inputs are not bound to the dataset manifest")
        if _digest(self._payload_without_sha()) != self.runtime_sha: raise ChampionsLeagueCandidateEvidenceError("runtime input identity digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate(); return {**self._payload_without_sha(), "runtime_sha": self.runtime_sha}


RuntimeInputCompatibility = RuntimeInputs
RuntimeInputIdentity = RuntimeInputs


@dataclass(frozen=True)
class FinalEvaluationEvidence:
    evidence_id: str
    evaluation_id: str
    partition_id: str
    partition_sha: str
    candidate_artifact_id: str
    artifact_sha: str
    feature_schema_sha: str
    boundary_id: str
    provenance_id: str
    metric_name: str
    metric_value: float
    sample_count: int
    legitimate: bool
    out_of_sample: bool
    held_out: bool
    results_resolved: bool
    result_source_id: str
    evaluated_at: datetime
    evidence_sha: str
    partition_role: str = "final_evaluation"
    dataset_manifest_id: str = ""
    dataset_manifest_sha: str = ""

    @classmethod
    def build(cls, *, evidence_id: str, evaluation_id: str, partition_id: str, partition_sha: str, candidate_artifact_id: str, artifact_sha: str, feature_schema_sha: str, boundary_id: str = "", provenance_id: str, metric_name: str, metric_value: float, sample_count: int, result_source_id: str, evaluated_at: datetime, legitimate: bool = True, out_of_sample: bool = True, held_out: bool = True, results_resolved: bool = True, partition_role: str = "final_evaluation", dataset_manifest_id: str = "", dataset_manifest_sha: str = "") -> "FinalEvaluationEvidence":
        value = cls(evidence_id, evaluation_id, partition_id, partition_sha, candidate_artifact_id, artifact_sha, feature_schema_sha, boundary_id, provenance_id, metric_name, metric_value, sample_count, legitimate, out_of_sample, held_out, results_resolved, result_source_id, evaluated_at, "0" * 64, partition_role, dataset_manifest_id, dataset_manifest_sha)
        return replace(value, evidence_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "FinalEvaluationEvidence":
        item = _mapping(raw, "final evaluation evidence")
        return cls(item.get("evidence_id", ""), item.get("evaluation_id", ""), item.get("partition_id", ""), item.get("partition_sha", ""), item.get("candidate_artifact_id", item.get("artifact_id", "")), item.get("artifact_sha", ""), item.get("feature_schema_sha", item.get("feature_schema_hash", "")), item.get("boundary_id", ""), item.get("provenance_id", ""), item.get("metric_name", ""), item.get("metric_value", float("nan")), item.get("sample_count", -1), item.get("legitimate", False), item.get("out_of_sample", False), item.get("held_out", False), item.get("results_resolved", False), item.get("result_source_id", ""), _parse_time(item.get("evaluated_at"), "final evaluation evaluated_at"), item.get("evidence_sha", ""), item.get("partition_role", "final_evaluation"), item.get("dataset_manifest_id", ""), item.get("dataset_manifest_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"evidence_id": self.evidence_id, "evaluation_id": self.evaluation_id, "partition_id": self.partition_id, "partition_sha": self.partition_sha.lower(), "candidate_artifact_id": self.candidate_artifact_id, "artifact_sha": self.artifact_sha.lower(), "feature_schema_sha": self.feature_schema_sha.lower(), "boundary_id": self.boundary_id, "provenance_id": self.provenance_id, "metric_name": self.metric_name, "metric_value": self.metric_value, "sample_count": self.sample_count, "legitimate": self.legitimate, "out_of_sample": self.out_of_sample, "held_out": self.held_out, "results_resolved": self.results_resolved, "result_source_id": self.result_source_id, "evaluated_at": _utc(self.evaluated_at, "final evaluation evaluated_at").isoformat(), "partition_role": self.partition_role, "dataset_manifest_id": self.dataset_manifest_id, "dataset_manifest_sha": self.dataset_manifest_sha.lower()}

    def validate(self, candidate: CandidateArtifactIdentity | None = None, schema: FeatureSchemaIdentity | None = None, manifest: DatasetManifestBinding | None = None) -> None:
        for value, name in ((self.evidence_id, "final evaluation evidence_id"), (self.evaluation_id, "evaluation_id"), (self.partition_id, "final evaluation partition_id"), (self.candidate_artifact_id, "final evaluation candidate_artifact_id"), (self.provenance_id, "final evaluation provenance_id"), (self.result_source_id, "final evaluation result_source_id")): _identity(value, name)
        for value, name in ((self.partition_sha, "final evaluation partition_sha"), (self.artifact_sha, "final evaluation artifact_sha"), (self.feature_schema_sha, "final evaluation feature_schema_sha")): _sha(value, name)
        if self.partition_role.casefold() not in {"final_evaluation", "holdout", "test"}: raise ChampionsLeagueCandidateEvidenceError("evidence is not final-evaluation evidence")
        _text(self.metric_name, "final evaluation metric_name"); _count(self.sample_count, "final evaluation sample_count"); _utc(self.evaluated_at, "final evaluation evaluated_at")
        if not isinstance(self.metric_value, (int, float)) or not isfinite(float(self.metric_value)): raise ChampionsLeagueCandidateEvidenceError("final evaluation metric_value must be finite")
        for value, name in ((self.legitimate, "final evaluation legitimate"), (self.out_of_sample, "final evaluation out_of_sample"), (self.held_out, "final evaluation held_out"), (self.results_resolved, "final evaluation results_resolved")): 
            if _bool(value, name) is not True: raise ChampionsLeagueCandidateEvidenceError("final evaluation evidence is not legitimate")
        _sha(self.evidence_sha, "final evaluation evidence_sha")
        if candidate is not None and (self.candidate_artifact_id != candidate.artifact_id or self.artifact_sha.lower() != candidate.artifact_sha.lower()): raise ChampionsLeagueCandidateEvidenceError("final evaluation is bound to a different artifact")
        if schema is not None and self.feature_schema_sha.lower() != schema.schema_sha: raise ChampionsLeagueCandidateEvidenceError("final evaluation uses a different feature schema")
        if manifest is not None and (self.dataset_manifest_id != manifest.manifest_id or self.dataset_manifest_sha.lower() != manifest.manifest_sha): raise ChampionsLeagueCandidateEvidenceError("final evaluation is not bound to the dataset manifest")
        if _digest(self._payload_without_sha()) != self.evidence_sha: raise ChampionsLeagueCandidateEvidenceError("final evaluation evidence digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate(); return {**self._payload_without_sha(), "evidence_sha": self.evidence_sha}


FinalEvaluationReport = FinalEvaluationEvidence
ModelArtifactIdentity = CandidateArtifactIdentity


@dataclass(frozen=True)
class RollbackIdentity:
    rollback_id: str
    target_artifact_id: str
    target_artifact_sha: str
    target_version: str
    config_sha: str
    tested: bool
    tested_at: datetime
    rollback_sha: str

    @classmethod
    def build(cls, *, rollback_id: str, target_artifact_id: str, target_artifact_sha: str, target_version: str, config_sha: str, tested_at: datetime, tested: bool = True) -> "RollbackIdentity":
        value = cls(rollback_id, target_artifact_id, target_artifact_sha, target_version, config_sha, tested, tested_at, "0" * 64)
        return replace(value, rollback_sha=_digest(value._payload_without_sha()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RollbackIdentity":
        item = _mapping(raw, "rollback identity")
        return cls(item.get("rollback_id", ""), item.get("target_artifact_id", ""), item.get("target_artifact_sha", ""), item.get("target_version", ""), item.get("config_sha", ""), item.get("tested", False), _parse_time(item.get("tested_at"), "rollback tested_at"), item.get("rollback_sha", ""))

    def _payload_without_sha(self) -> dict[str, object]:
        return {"rollback_id": self.rollback_id, "target_artifact_id": self.target_artifact_id, "target_artifact_sha": self.target_artifact_sha.lower(), "target_version": self.target_version, "config_sha": self.config_sha.lower(), "tested": self.tested, "tested_at": _utc(self.tested_at, "rollback tested_at").isoformat()}

    def validate(self) -> None:
        for value, name in ((self.rollback_id, "rollback_id"), (self.target_artifact_id, "rollback target_artifact_id"), (self.target_version, "rollback target_version")): _identity(value, name)
        _sha(self.target_artifact_sha, "rollback target_artifact_sha"); _sha(self.config_sha, "rollback config_sha")
        if _bool(self.tested, "rollback tested") is not True: raise ChampionsLeagueCandidateEvidenceError("rollback identity was not tested")
        _utc(self.tested_at, "rollback tested_at"); _sha(self.rollback_sha, "rollback_sha")
        if _digest(self._payload_without_sha()) != self.rollback_sha: raise ChampionsLeagueCandidateEvidenceError("rollback identity digest changed")


def _coerce(value: object, cls: type[Any]) -> object:
    if value is None or isinstance(value, cls): return value
    if isinstance(value, Mapping): return cls.from_mapping(value)
    return value


def _coerce_many(value: object, cls: type[Any]) -> tuple[object, ...]:
    if value is None: return ()
    return tuple(_coerce(item, cls) for item in _sequence(value, "evidence"))


@dataclass(frozen=True)
class OfflineQualificationReport:
    report_id: str
    competition_id: str = COMPETITION_ID
    candidate_artifact_identity: object = None
    dataset_manifest_binding: object = None
    feature_schema_identity: object = None
    approved_information_time_boundary: object = None
    training_partition_identity: object = None
    calibration_evidence: tuple[object, ...] = ()
    final_evaluation_evidence: tuple[object, ...] = ()
    source_provenance: tuple[object, ...] = ()
    contamination_pass: object = None
    runtime_inputs: object = None
    rollback_identity: object = None
    accepted: bool = False
    errors: tuple[str, ...] = ()
    report_sha: str = ""
    contract_version: str = CONTRACT_VERSION

    @property
    def valid(self) -> bool: return self.accepted

    def _payload_without_sha(self) -> dict[str, object]:
        return {
            "report_id": self.report_id, "competition_id": self.competition_id, "contract_version": self.contract_version,
            "candidate_artifact_identity": _component(self.candidate_artifact_identity), "dataset_manifest_binding": _component(self.dataset_manifest_binding),
            "feature_schema_identity": _component(self.feature_schema_identity), "approved_information_time_boundary": _component(self.approved_information_time_boundary),
            "training_partition_identity": _component(self.training_partition_identity), "calibration_evidence": [_component(v) for v in self.calibration_evidence],
            "final_evaluation_evidence": [_component(v) for v in self.final_evaluation_evidence], "source_provenance": [_component(v) for v in self.source_provenance],
            "contamination_pass": _component(self.contamination_pass), "runtime_inputs": _component(self.runtime_inputs), "rollback_identity": _component(self.rollback_identity),
            "accepted": self.accepted, "errors": list(self.errors),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "OfflineQualificationReport":
        item = _mapping(raw, "offline qualification report")
        return cls(
            item.get("report_id", ""), item.get("competition_id", item.get("competition", "")),
            _coerce(item.get("candidate_artifact_identity", item.get("candidate_artifact")), CandidateArtifactIdentity),
            _coerce(item.get("dataset_manifest_binding", item.get("dataset_manifest_identity", item.get("manifest_binding"))), DatasetManifestBinding),
            _coerce(item.get("feature_schema_identity", item.get("feature_schema")), FeatureSchemaIdentity),
            _coerce(item.get("approved_information_time_boundary", item.get("information_time_boundary")), ApprovedInformationTimeBoundary),
            _coerce(item.get("training_partition_identity", item.get("training_partition")), TrainingPartitionIdentity),
            _coerce_many(item.get("calibration_evidence", ()), CalibrationEvidence),
            _coerce_many(item.get("final_evaluation_evidence", item.get("final_evaluation", ())), FinalEvaluationEvidence),
            _coerce_many(item.get("source_provenance", ()), SourceProvenance),
            _coerce(item.get("contamination_pass", item.get("contamination")), ContaminationPass),
            _coerce(item.get("runtime_inputs", item.get("runtime_input_compatibility")), RuntimeInputs),
            _coerce(item.get("rollback_identity"), RollbackIdentity), item.get("accepted", False),
            tuple(str(v) for v in _sequence(item.get("errors", ()), "report errors")), item.get("report_sha", ""), item.get("contract_version", CONTRACT_VERSION),
        )

    def _validate_components(self) -> None:
        candidate, manifest, schema, training = self.candidate_artifact_identity, self.dataset_manifest_binding, self.feature_schema_identity, self.training_partition_identity
        if not isinstance(candidate, CandidateArtifactIdentity): raise ChampionsLeagueCandidateEvidenceError("candidate artifact identity is required")
        if not isinstance(manifest, DatasetManifestBinding): raise ChampionsLeagueCandidateEvidenceError("dataset manifest binding is required")
        if not isinstance(schema, FeatureSchemaIdentity): raise ChampionsLeagueCandidateEvidenceError("feature schema identity is required")
        if not isinstance(training, TrainingPartitionIdentity): raise ChampionsLeagueCandidateEvidenceError("training partition identity is required")
        manifest.validate(); candidate.validate(require_dataset_binding=True)
        if candidate.dataset_manifest_id != manifest.manifest_id or candidate.dataset_manifest_sha.lower() != manifest.manifest_sha: raise ChampionsLeagueCandidateEvidenceError("candidate artifact is not bound to the dataset manifest")
        schema.validate(); boundary = self.approved_information_time_boundary
        if boundary is not None:
            if not isinstance(boundary, ApprovedInformationTimeBoundary): raise ChampionsLeagueCandidateEvidenceError("information-time boundary is malformed")
            boundary.validate()
        training.validate(manifest, boundary, require_manifest_binding=True)
        if not self.calibration_evidence: raise ChampionsLeagueCandidateEvidenceError("calibration evidence is required")
        if not self.final_evaluation_evidence: raise ChampionsLeagueCandidateEvidenceError("legitimate final-evaluation evidence is required")
        if not self.source_provenance: raise ChampionsLeagueCandidateEvidenceError("source provenance is required")
        provenance: dict[str, SourceProvenance] = {}
        for item in self.source_provenance:
            if not isinstance(item, SourceProvenance): raise ChampionsLeagueCandidateEvidenceError("source provenance is malformed")
            item.validate(manifest)
            if item.provenance_id in provenance: raise ChampionsLeagueCandidateEvidenceError("duplicate source provenance identity")
            provenance[item.provenance_id] = item
        roles = {
            item.role.casefold().replace("-", "_").replace(" ", "_")
            for item in provenance.values()
        }
        roles.discard("dataset")
        if not {"training", "calibration", "final_evaluation", "runtime"} <= roles:
            raise ChampionsLeagueCandidateEvidenceError(
                "source provenance must cover training, calibration, final evaluation, and runtime"
            )
        cal_ids: set[str] = set(); cal_partitions: set[str] = set()
        for item in self.calibration_evidence:
            if not isinstance(item, CalibrationEvidence): raise ChampionsLeagueCandidateEvidenceError("calibration evidence is malformed")
            item.validate(candidate, schema, manifest)
            if item.evidence_id in cal_ids: raise ChampionsLeagueCandidateEvidenceError("duplicate calibration evidence identity")
            if item.provenance_id not in provenance: raise ChampionsLeagueCandidateEvidenceError("calibration evidence lacks source provenance")
            cal_ids.add(item.evidence_id); cal_partitions.add(item.partition_id)
        eval_ids: set[str] = set()
        for item in self.final_evaluation_evidence:
            if not isinstance(item, FinalEvaluationEvidence): raise ChampionsLeagueCandidateEvidenceError("final evaluation evidence is malformed")
            item.validate(candidate, schema, manifest)
            if item.evidence_id in eval_ids: raise ChampionsLeagueCandidateEvidenceError("duplicate final evaluation evidence identity")
            if item.provenance_id not in provenance: raise ChampionsLeagueCandidateEvidenceError("final evaluation evidence lacks source provenance")
            if item.partition_id == training.partition_id or item.partition_id in cal_partitions: raise ChampionsLeagueCandidateEvidenceError("final evaluation reuses a training or calibration partition")
            eval_ids.add(item.evidence_id)
        contamination = self.contamination_pass
        if not isinstance(contamination, ContaminationPass): raise ChampionsLeagueCandidateEvidenceError("contamination pass is required")
        contamination.validate(manifest)
        known_sources = {item.source_sha for item in provenance.values()}
        if not {contamination.training_source_sha, contamination.calibration_source_sha, contamination.final_evaluation_source_sha} <= known_sources: raise ChampionsLeagueCandidateEvidenceError("contamination pass references unknown source provenance")
        runtime = self.runtime_inputs
        if not isinstance(runtime, RuntimeInputs): raise ChampionsLeagueCandidateEvidenceError("runtime inputs are required")
        runtime.validate(candidate, schema, manifest)
        if runtime.provenance_id not in provenance: raise ChampionsLeagueCandidateEvidenceError("runtime inputs lack source provenance")
        if self.rollback_identity is not None:
            if not isinstance(self.rollback_identity, RollbackIdentity): raise ChampionsLeagueCandidateEvidenceError("rollback identity is malformed")
            self.rollback_identity.validate()
            if self.rollback_identity.target_artifact_id == candidate.artifact_id or self.rollback_identity.target_artifact_sha.lower() == candidate.artifact_sha.lower(): raise ChampionsLeagueCandidateEvidenceError("rollback target must differ from candidate artifact")

    def validate(self) -> None:
        _identity(self.report_id, "qualification report_id")
        if self.competition_id != COMPETITION_ID: raise ChampionsLeagueCandidateEvidenceError("qualification report is for another competition")
        if self.contract_version != CONTRACT_VERSION: raise ChampionsLeagueCandidateEvidenceError("unsupported candidate-evidence contract version")
        if not isinstance(self.accepted, bool) or not isinstance(self.errors, tuple): raise ChampionsLeagueCandidateEvidenceError("qualification report state is malformed")
        _sha(self.report_sha, "qualification report_sha")
        if _digest(self._payload_without_sha()) != self.report_sha: raise ChampionsLeagueCandidateEvidenceError("qualification report digest changed")
        if self.accepted:
            if self.errors: raise ChampionsLeagueCandidateEvidenceError("accepted report contains errors")
            self._validate_components()
        elif not self.errors: raise ChampionsLeagueCandidateEvidenceError("rejected report must contain errors")

    def raise_if_rejected(self) -> None:
        self.validate()
        if not self.accepted: raise ChampionsLeagueCandidateEvidenceError("; ".join(self.errors))

    def as_payload(self) -> dict[str, object]:
        self.validate(); return {**self._payload_without_sha(), "report_sha": self.report_sha}


ChampionsLeagueOfflineQualificationReport = OfflineQualificationReport
QualificationReport = OfflineQualificationReport


def _rejected(report_id: object, error: str) -> OfflineQualificationReport:
    value = OfflineQualificationReport(report_id=str(report_id), accepted=False, errors=(error,), report_sha="0" * 64)
    return replace(value, report_sha=_digest(value._payload_without_sha()))


def qualify_offline_candidate(bundle: Mapping[str, object] | None = None, *, report_id: str = "champions-league-offline-qualification", candidate_artifact_identity: object = None, dataset_manifest_binding: object = None, dataset_manifest_identity: object = None, feature_schema_identity: object = None, approved_information_time_boundary: object = None, training_partition_identity: object = None, calibration_evidence: Sequence[object] = (), final_evaluation_evidence: Sequence[object] = (), source_provenance: Sequence[object] = (), contamination_pass: object = None, runtime_inputs: object = None, rollback_identity: object = None) -> OfflineQualificationReport:
    try:
        if bundle is not None:
            item = _mapping(bundle, "offline qualification input")
            report_id = item.get("report_id", report_id)
            candidate_artifact_identity = item.get("candidate_artifact_identity", item.get("candidate_artifact", candidate_artifact_identity))
            dataset_manifest_binding = item.get("dataset_manifest_binding", item.get("dataset_manifest_identity", item.get("manifest_binding", dataset_manifest_binding or dataset_manifest_identity)))
            feature_schema_identity = item.get("feature_schema_identity", item.get("feature_schema", feature_schema_identity))
            approved_information_time_boundary = item.get("approved_information_time_boundary", item.get("information_time_boundary", approved_information_time_boundary))
            training_partition_identity = item.get("training_partition_identity", item.get("training_partition", training_partition_identity))
            calibration_evidence = item.get("calibration_evidence", calibration_evidence); final_evaluation_evidence = item.get("final_evaluation_evidence", item.get("final_evaluation", final_evaluation_evidence)); source_provenance = item.get("source_provenance", source_provenance)
            contamination_pass = item.get("contamination_pass", item.get("contamination", contamination_pass)); runtime_inputs = item.get("runtime_inputs", item.get("runtime_input_compatibility", runtime_inputs)); rollback_identity = item.get("rollback_identity", rollback_identity)
        report = OfflineQualificationReport(str(report_id), COMPETITION_ID, _coerce(candidate_artifact_identity, CandidateArtifactIdentity), _coerce(dataset_manifest_binding or dataset_manifest_identity, DatasetManifestBinding), _coerce(feature_schema_identity, FeatureSchemaIdentity), _coerce(approved_information_time_boundary, ApprovedInformationTimeBoundary), _coerce(training_partition_identity, TrainingPartitionIdentity), tuple(_coerce_many(calibration_evidence, CalibrationEvidence)), tuple(_coerce_many(final_evaluation_evidence, FinalEvaluationEvidence)), tuple(_coerce_many(source_provenance, SourceProvenance)), _coerce(contamination_pass, ContaminationPass), _coerce(runtime_inputs, RuntimeInputs), _coerce(rollback_identity, RollbackIdentity), True, (), "0" * 64, CONTRACT_VERSION)
        report._validate_components()
        return replace(report, report_sha=_digest(report._payload_without_sha()))
    except (ChampionsLeagueCandidateEvidenceError, TypeError, ValueError) as exc:
        return _rejected(report_id, str(exc))


def validate_offline_qualification(report: OfflineQualificationReport | Mapping[str, object]) -> OfflineQualificationReport:
    value = report if isinstance(report, OfflineQualificationReport) else OfflineQualificationReport.from_mapping(report)
    value.raise_if_rejected(); return value


qualify_candidate_evidence = qualify_offline_candidate
validate_candidate_evidence = validate_offline_qualification
validate_champions_league_offline_qualification = validate_offline_qualification
qualify_champions_league_offline = qualify_offline_candidate


__all__ = [
    "ApprovedInformationTimeBoundary", "CandidateArtifactIdentity", "CalibrationEvidence", "ChampionsLeagueCandidateEvidenceError", "ChampionsLeagueQualificationError", "COMPETITION_ID", "COMPETITION_NAME", "CONTRACT_VERSION", "OFFLINE_QUALIFICATION_CONTRACT_VERSION", "CHAMPIONS_LEAGUE_COMPETITION_ID", "CHAMPIONS_LEAGUE_COMPETITION_NAME", "CHAMPIONS_LEAGUE_QUALIFICATION_CONTRACT_VERSION", "ContaminationPass", "ContaminationResult", "DatasetManifest", "ChampionsLeagueDatasetManifest", "DatasetManifestBinding", "DatasetManifestIdentity", "FeatureSchemaIdentity", "FinalEvaluationEvidence", "FinalEvaluationReport", "InformationTimeBoundary", "ModelArtifactIdentity", "OfflineQualificationReport", "ProvenanceRecord", "QualificationReport", "RollbackIdentity", "RuntimeInputs", "RuntimeInputCompatibility", "RuntimeInputIdentity", "SourceProvenance", "TrainingPartitionIdentity", "qualify_candidate_evidence", "qualify_champions_league_offline", "qualify_offline_candidate", "validate_candidate_evidence", "validate_champions_league_offline_qualification", "validate_offline_qualification"
]
