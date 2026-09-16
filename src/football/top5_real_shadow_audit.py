"""Read-only completeness auditing for Top-5 real-shadow evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    validate_builder1_qualification_receipt,
)
from src.football.top5_qualification_sample_aggregator import (
    Builder2QualificationSampleAggregatorError,
    Builder2QualificationSampleReportV1,
)
from src.football.top5_real_shadow_attachments import (
    RealShadowClosingAttachment,
    RealShadowResultAttachment,
)
from src.football.top5_real_shadow_contracts import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    OFFLINE_REPLAY_MARKER,
    REAL_OBSERVED_MARKER,
    REAL_SHADOW_SESSION_SCHEMA,
    TEST_FIXTURE_MARKER,
    TOP5_REAL_SHADOW_LEAGUES,
    NormalizedProviderObservation,
    RealShadowContractError,
    RealShadowExperiment,
    RealShadowPredictionArtifact,
)
from src.football.top5_real_shadow_session import RealShadowSession
from src.football.top5_shadow_validation import (
    ShadowEvidenceBundle,
    ShadowValidationError,
)

AUDIT_SCHEMA_VERSION = "top5-real-shadow-evidence-audit-v1"
EVIDENCE_BUNDLE_SCHEMA = "top5-shadow-evidence-v1"
_SHA_LENGTHS = {40, 64}
_PREDICTION_INPUT_KEYS = {
    "closing",
    "closing_odds",
    "closing_probabilities",
    "closing_snapshot_id",
    "closing_snapshot_ids",
}


class AuditStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    PENDING_RESULT = "PENDING_RESULT"
    PENDING_CLOSING = "PENDING_CLOSING"
    QUALIFICATION_MISSING = "QUALIFICATION_MISSING"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    CONFLICT = "CONFLICT"
    NON_REAL_EVIDENCE = "NON_REAL_EVIDENCE"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED_CLOSED = "FAILED_CLOSED"


class ShadowAuditError(ValueError):
    """Malformed audit input or an unsafe path."""


@dataclass(frozen=True)
class AuditFinding:
    code: str
    message: str

    def as_payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _records(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ShadowAuditError(f"{field} must be a JSON array")
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ShadowAuditError(f"{field} contains a non-object record")
        result.append(item)
    return tuple(result)


def _stable_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ShadowAuditError("audit input is not deterministic JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _SHA_LENGTHS
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _index(
    values: Sequence[Mapping[str, object]], key: str
) -> tuple[dict[str, Mapping[str, object]], set[str]]:
    indexed: dict[str, Mapping[str, object]] = {}
    duplicates: set[str] = set()
    for item in values:
        identity = item.get(key)
        if not isinstance(identity, str) or not identity:
            continue
        if identity in indexed:
            duplicates.add(identity)
        indexed[identity] = item
    return indexed, duplicates


def _finding(code: str, message: str) -> AuditFinding:
    return AuditFinding(code, message)


def _validate_receipt(
    observation: Mapping[str, object],
) -> tuple[bool, Mapping[str, object] | None, list[AuditFinding]]:
    receipt = _mapping(observation.get("independent_validation"))
    canonical = _mapping(observation.get("canonical_observation"))
    if receipt is None or canonical is None:
        return (
            False,
            receipt,
            [
                _finding(
                    "QUALIFICATION_MISSING",
                    "REAL_OBSERVED requires a canonical Builder2QualificationReceiptV1 and observation envelope",
                )
            ],
        )
    try:
        validated = validate_builder1_qualification_receipt(
            receipt, expected_observation=canonical
        )
        return True, validated.as_payload(), []
    except (
        Builder2QualificationReceiptError,
        RealShadowContractError,
        TypeError,
        ValueError,
    ):
        return (
            False,
            receipt,
            [
                _finding(
                    "PROVENANCE_MISMATCH",
                    "Builder2QualificationReceiptV1 does not validate against the canonical observation",
                )
            ],
        )


def _experiment(raw: Mapping[str, object]) -> RealShadowExperiment | None:
    candidate = RealShadowExperiment(
        raw.get("experiment_id", ""),
        raw.get("minimum_lead_minutes", -1),
        raw.get("maximum_lead_minutes", -1),
        raw.get("maximum_odds_age_seconds", -1),
        raw.get("kickoff_tolerance_seconds", -1),
    )
    try:
        candidate.validate()
    except (RealShadowContractError, TypeError, ValueError):
        return None
    return candidate


def _observation_record(
    raw: Mapping[str, object],
    experiment: RealShadowExperiment | None,
) -> dict[str, Any]:
    findings: list[AuditFinding] = []
    mode = raw.get("observation_mode")
    record: dict[str, Any] = {
        "raw": raw,
        "observation": None,
        "observation_present": True,
        "observation_mode": mode,
        "qualification_present": False,
        "provenance_valid": False,
        "findings": findings,
    }
    if mode == OFFLINE_REPLAY_MARKER:
        findings.append(
            _finding("OFFLINE_REPLAY", "OFFLINE_REPLAY cannot count as real evidence")
        )
        record["status"] = AuditStatus.NON_REAL_EVIDENCE.value
        return record
    try:
        observation = NormalizedProviderObservation.from_payload(raw)
        record["observation"] = observation
    except (RealShadowContractError, TypeError, ValueError):
        findings.append(
            _finding(
                "OBSERVATION_INVALID",
                "provider observation fails its canonical structural contract",
            )
        )
        record["status"] = AuditStatus.FAILED_CLOSED.value
        return record
    if experiment is not None:
        try:
            observation.validate(experiment)
        except (RealShadowContractError, TypeError, ValueError):
            findings.append(
                _finding(
                    "OBSERVATION_INVALID",
                    "provider observation fails its canonical structural contract",
                )
            )

    if mode == TEST_FIXTURE_MARKER:
        if (
            raw.get("independent_validation") is not None
            or raw.get("canonical_observation") is not None
        ):
            findings.append(
                _finding(
                    "NON_REAL_EVIDENCE",
                    "TEST_FIXTURE cannot carry real qualification evidence",
                )
            )
            record["status"] = AuditStatus.NON_REAL_EVIDENCE.value
        else:
            record["provenance_valid"] = True
            record["status"] = AuditStatus.NON_REAL_EVIDENCE.value
        return record
    if mode != REAL_OBSERVED_MARKER:
        findings.append(
            _finding(
                "UNSUPPORTED",
                "observation mode is not an accepted real-shadow evidence mode",
            )
        )
        record["status"] = AuditStatus.UNSUPPORTED.value
        return record

    valid, receipt, receipt_findings = _validate_receipt(raw)
    record["receipt"] = receipt
    record["qualification_present"] = receipt is not None
    record["provenance_valid"] = valid
    findings.extend(receipt_findings)
    if valid:
        record["status"] = "VALID"
    else:
        record["status"] = AuditStatus.PROVENANCE_MISMATCH.value
    return record


def _prediction_record(
    raw: Mapping[str, object],
    observation_record: dict[str, Any] | None,
    session: Mapping[str, object],
    experiment: RealShadowExperiment | None,
) -> dict[str, Any]:
    findings: list[AuditFinding] = []
    prediction_id = raw.get("prediction_id")
    record: dict[str, Any] = {
        "raw": raw,
        "prediction": None,
        "prediction_id": prediction_id,
        "prediction_present": True,
        "identity_valid": False,
        "digests_valid": False,
        "signal_time_valid": False,
        "frozen_binding_valid": False,
        "closing_excluded": not any(key in raw for key in _PREDICTION_INPUT_KEYS),
        "findings": findings,
    }
    if not isinstance(prediction_id, str) or not prediction_id:
        findings.append(_finding("IDENTITY_MISMATCH", "prediction identity is missing"))
        record["status"] = AuditStatus.IDENTITY_MISMATCH.value
        return record
    try:
        prediction = RealShadowPredictionArtifact.from_payload(raw)
        prediction.validate()
        record["prediction"] = prediction
        record["digests_valid"] = True
    except (RealShadowContractError, TypeError, ValueError):
        if (
            raw.get("model_identity") != M5_CANDIDATE_ID
            or raw.get("research_sha") != FROZEN_RESEARCH_SHA
        ):
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "prediction is not bound to frozen M5 and Research",
                )
            )
        elif not _sha(raw.get("artifact_sha")):
            findings.append(
                _finding("DIGEST_MISMATCH", "prediction artifact SHA is malformed")
            )
        else:
            findings.append(
                _finding(
                    "DIGEST_MISMATCH",
                    "prediction artifact fails its immutable digest or contract validation",
                )
            )

    if observation_record is None or observation_record.get("observation") is None:
        findings.append(
            _finding("IDENTITY_MISMATCH", "prediction has no valid observed input")
        )
    else:
        observation = observation_record["observation"]
        identity_pairs = (
            ("session_id", raw.get("session_id"), session.get("session_id")),
            ("fixture_key", raw.get("fixture_key"), observation.fixture_key),
            ("league_code", raw.get("league_code"), observation.league_code),
            ("home_team", raw.get("home_team"), observation.home_team),
            ("away_team", raw.get("away_team"), observation.away_team),
            (
                "provider_identity",
                raw.get("provider_identity"),
                observation.provider_identity,
            ),
            (
                "bookmaker_identity",
                raw.get("bookmaker_identity"),
                observation.bookmaker_identity,
            ),
            (
                "provider_fixture_id",
                raw.get("provider_fixture_id"),
                observation.provider_fixture_id,
            ),
            (
                "observation_digest",
                raw.get("observation_digest"),
                observation.observation_digest(),
            ),
        )
        if all(actual == expected for _, actual, expected in identity_pairs):
            record["identity_valid"] = True
        else:
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "prediction is not bound to the exact session and observation identity",
                )
            )
        if raw.get("marker") != observation.observation_mode:
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "prediction evidence mode differs from its observation",
                )
            )

    if experiment is not None:
        timing_pairs = (
            (
                "minimum_lead_minutes",
                raw.get("minimum_lead_minutes"),
                experiment.minimum_lead_minutes,
            ),
            (
                "maximum_lead_minutes",
                raw.get("maximum_lead_minutes"),
                experiment.maximum_lead_minutes,
            ),
            (
                "maximum_odds_age_seconds",
                raw.get("maximum_odds_age_seconds"),
                experiment.maximum_odds_age_seconds,
            ),
            (
                "kickoff_tolerance_seconds",
                raw.get("kickoff_tolerance_seconds"),
                experiment.kickoff_tolerance_seconds,
            ),
        )
        if raw.get("signal_time_contract_id") == experiment.contract_id and all(
            actual == expected for _, actual, expected in timing_pairs
        ):
            record["signal_time_valid"] = True
        else:
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "caller-supplied signal-time experiment is not retained exactly",
                )
            )
    if (
        raw.get("model_identity") == M5_CANDIDATE_ID
        and raw.get("research_sha") == FROZEN_RESEARCH_SHA
    ):
        record["frozen_binding_valid"] = True
    else:
        findings.append(
            _finding(
                "PROVENANCE_MISMATCH",
                "prediction is not bound to frozen M5 and Research",
            )
        )
    if not record["closing_excluded"]:
        findings.append(
            _finding(
                "PROVENANCE_MISMATCH", "closing data is present in prediction inputs"
            )
        )
    record["status"] = (
        "VALID" if not findings else AuditStatus.PROVENANCE_MISMATCH.value
    )
    return record


def _result_record(
    raw: Mapping[str, object] | None,
    prediction: dict[str, Any],
) -> dict[str, Any]:
    if raw is None:
        return {
            "present": False,
            "final": False,
            "status": AuditStatus.PENDING_RESULT.value,
            "result": None,
            "findings": [],
        }
    findings: list[AuditFinding] = []
    result: RealShadowResultAttachment | None = None
    try:
        result = RealShadowResultAttachment.from_payload(raw)
        result.validate()
    except (RealShadowContractError, TypeError, ValueError):
        findings.append(
            _finding(
                "DIGEST_MISMATCH",
                "result attachment fails its immutable digest or contract validation",
            )
        )
    pred = prediction.get("prediction")
    if result is not None and pred is not None:
        if (
            result.prediction_id != pred.prediction_id
            or result.prediction_artifact_sha != pred.artifact_sha
            or result.fixture_key != pred.fixture_key
            or result.league_code != pred.league_code
        ):
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "result attachment is bound to another prediction or fixture",
                )
            )
        if result.attached_at < pred.captured_at:
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "result attachment precedes the prediction capture",
                )
            )
    final = (
        result is not None and getattr(result.status, "value", result.status) == "final"
    )
    if result is not None and not final:
        findings.append(_finding("PENDING_RESULT", "result is present but not final"))
    status = (
        AuditStatus.COMPLETE.value
        if not findings and final
        else (
            AuditStatus.PENDING_RESULT.value
            if not findings or any(item.code == "PENDING_RESULT" for item in findings)
            else AuditStatus.IDENTITY_MISMATCH.value
        )
    )
    return {
        "present": True,
        "final": final,
        "status": status,
        "result": result,
        "findings": findings,
        "raw": raw,
    }


def _closing_record(
    raw: Mapping[str, object] | None,
    prediction: dict[str, Any],
) -> dict[str, Any]:
    if raw is None:
        return {
            "present": False,
            "status": AuditStatus.PENDING_CLOSING.value,
            "closing": None,
            "findings": [],
        }
    findings: list[AuditFinding] = []
    closing: RealShadowClosingAttachment | None = None
    try:
        closing = RealShadowClosingAttachment.from_payload(raw)
        closing.validate()
    except (RealShadowContractError, TypeError, ValueError):
        findings.append(
            _finding(
                "DIGEST_MISMATCH",
                "closing attachment fails its immutable digest or contract validation",
            )
        )
    pred = prediction.get("prediction")
    if closing is not None and pred is not None:
        if (
            closing.prediction_id != pred.prediction_id
            or closing.prediction_artifact_sha != pred.artifact_sha
            or closing.fixture_key != pred.fixture_key
            or closing.league_code != pred.league_code
        ):
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "closing attachment is bound to another prediction or fixture",
                )
            )
        if not pred.source_timestamp <= closing.closing_timestamp <= pred.kickoff:
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "closing timestamp is outside the signal-to-kickoff window",
                )
            )
        if closing.attached_at < pred.captured_at:
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "closing attachment precedes the prediction capture",
                )
            )
    if raw.get("used_for_prediction") is True:
        findings.append(
            _finding(
                "PROVENANCE_MISMATCH",
                "closing evidence is marked as used for prediction",
            )
        )
    status = (
        AuditStatus.COMPLETE.value
        if not findings
        else (
            AuditStatus.PROVENANCE_MISMATCH.value
            if any(item.code == "PROVENANCE_MISMATCH" for item in findings)
            else AuditStatus.IDENTITY_MISMATCH.value
        )
    )
    return {
        "present": True,
        "status": status,
        "closing": closing,
        "findings": findings,
        "raw": raw,
    }


def _status_for_prediction(
    mode: object,
    findings: Sequence[AuditFinding],
    qualification_present: bool,
    result: dict[str, Any],
    closing: dict[str, Any],
    flags: Mapping[str, bool],
) -> str:
    codes = {item.code for item in findings}
    if mode == OFFLINE_REPLAY_MARKER or "NON_REAL_EVIDENCE" in codes:
        return AuditStatus.NON_REAL_EVIDENCE.value
    if mode == TEST_FIXTURE_MARKER:
        return AuditStatus.NON_REAL_EVIDENCE.value
    if not qualification_present or "QUALIFICATION_MISSING" in codes:
        return AuditStatus.QUALIFICATION_MISSING.value
    if "CONFLICT" in codes:
        return AuditStatus.CONFLICT.value
    if "DIGEST_MISMATCH" in codes:
        return AuditStatus.DIGEST_MISMATCH.value
    if "PROVENANCE_MISMATCH" in codes or not flags.get("PROVENANCE_VALID", False):
        return AuditStatus.PROVENANCE_MISMATCH.value
    if "IDENTITY_MISMATCH" in codes:
        return AuditStatus.IDENTITY_MISMATCH.value
    if not result["present"] or not result["final"]:
        return AuditStatus.PENDING_RESULT.value
    if not closing["present"]:
        return AuditStatus.PENDING_CLOSING.value
    if "EVIDENCE_BUNDLE_MISSING" in codes:
        return AuditStatus.INCOMPLETE.value
    if findings:
        return AuditStatus.FAILED_CLOSED.value
    return AuditStatus.COMPLETE.value


def _safe_bundle_audit(
    bundle: Mapping[str, object] | None,
    predictions: Mapping[str, dict[str, Any]],
    observations: Mapping[str, dict[str, Any]],
    results: Mapping[str, Mapping[str, object]],
    closings: Mapping[str, Mapping[str, object]],
    session: Mapping[str, object],
) -> tuple[bool | None, list[AuditFinding]]:
    if bundle is None:
        return False, [
            _finding(
                "EVIDENCE_BUNDLE_MISSING",
                "canonical shadow evidence bundle is not available",
            )
        ]
    findings: list[AuditFinding] = []
    try:
        ShadowEvidenceBundle.from_payload(bundle).validate()
    except (ShadowValidationError, KeyError, TypeError, ValueError):
        findings.append(
            _finding(
                "EVIDENCE_BUNDLE_INVALID",
                "shadow evidence bundle fails the canonical Builder-2 validator",
            )
        )
    bundle_session = _mapping(bundle.get("real_shadow_session"))
    if bundle_session is None or any(
        bundle_session.get(field) != session.get(field)
        for field in ("session_id", "integration_sha", "research_sha", "model_identity")
    ):
        findings.append(
            _finding(
                "IDENTITY_MISMATCH",
                "evidence bundle is not bound to the exact session identity",
            )
        )
    provenance_groups = (
        "observations",
        "predictions",
        "provider_evidence",
        "signal_time_evidence",
        "quota_cost_evidence",
        "health_evidence",
        "result_attachments",
        "closing_benchmark_evidence",
        "failure_evidence",
    )
    for group in provenance_groups:
        for item in _records(bundle.get(group), f"bundle.{group}"):
            provenance = _mapping(item.get("provenance"))
            if provenance is None or provenance.get("source_sha") != session.get(
                "integration_sha"
            ):
                findings.append(
                    _finding(
                        "PROVENANCE_MISMATCH",
                        "evidence bundle source SHA does not match the session integration SHA",
                    )
                )
            if (
                provenance is None
                or provenance.get("research_sha") != FROZEN_RESEARCH_SHA
            ):
                findings.append(
                    _finding(
                        "PROVENANCE_MISMATCH",
                        "evidence bundle Research SHA is not frozen",
                    )
                )
    raw_predictions = _records(bundle.get("predictions"), "bundle.predictions")
    for item in raw_predictions:
        prediction_id = item.get("prediction_id")
        local = (
            predictions.get(prediction_id) if isinstance(prediction_id, str) else None
        )
        provenance = _mapping(item.get("provenance"))
        if local is None:
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "evidence bundle references an unknown prediction",
                )
            )
            continue
        if not isinstance(provenance, Mapping) or provenance.get(
            "artifact_sha"
        ) != local["raw"].get("artifact_sha"):
            findings.append(
                _finding(
                    "DIGEST_MISMATCH",
                    "evidence bundle prediction digest does not match the artifact",
                )
            )
        if isinstance(provenance, Mapping) and (
            provenance.get("fixture_key") != local["raw"].get("fixture_key")
            or provenance.get("research_sha") != FROZEN_RESEARCH_SHA
            or provenance.get("model_identity") != M5_CANDIDATE_ID
        ):
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "evidence bundle prediction provenance does not match the artifact",
                )
            )
        input_kinds = item.get("input_snapshot_kinds", ())
        if isinstance(input_kinds, Sequence) and "closing" in input_kinds:
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "closing snapshot is declared as a prediction input",
                )
            )
    for item in _records(bundle.get("observations"), "bundle.observations"):
        provenance = _mapping(item.get("provenance"))
        fixture_key = provenance.get("fixture_key") if provenance else None
        local = observations.get(fixture_key) if isinstance(fixture_key, str) else None
        if local is None or local.get("observation") is None:
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH",
                    "evidence bundle references an unknown observation",
                )
            )
        elif (
            provenance.get("artifact_sha") != local["observation"].observation_digest()
        ):
            findings.append(
                _finding(
                    "DIGEST_MISMATCH",
                    "evidence bundle observation digest does not match the observation",
                )
            )
    for item in _records(bundle.get("result_attachments"), "bundle.result_attachments"):
        prediction_id = item.get("prediction_id")
        local = results.get(prediction_id) if isinstance(prediction_id, str) else None
        provenance = _mapping(item.get("provenance"))
        if local is None or provenance is None:
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH", "evidence bundle references an unknown result"
                )
            )
        elif provenance.get("artifact_sha") != local.get("attachment_sha"):
            findings.append(
                _finding(
                    "DIGEST_MISMATCH",
                    "evidence bundle result digest does not match the attachment",
                )
            )
    for item in _records(
        bundle.get("closing_benchmark_evidence"), "bundle.closing_benchmark_evidence"
    ):
        if item.get("used_for_prediction") is not False:
            findings.append(
                _finding(
                    "PROVENANCE_MISMATCH",
                    "closing benchmark is not explicitly excluded from prediction",
                )
            )
        prediction_id = item.get("prediction_id")
        local = closings.get(prediction_id) if isinstance(prediction_id, str) else None
        provenance = _mapping(item.get("provenance"))
        if local is None or provenance is None:
            findings.append(
                _finding(
                    "IDENTITY_MISMATCH", "evidence bundle references an unknown closing"
                )
            )
        elif provenance.get("artifact_sha") != local.get("attachment_sha"):
            findings.append(
                _finding(
                    "DIGEST_MISMATCH",
                    "evidence bundle closing digest does not match the attachment",
                )
            )
    safety = _mapping(bundle.get("safety"))
    if safety is not None and any(
        safety.get(name) is not expected
        for name, expected in (
            ("no_bet", True),
            ("publication_enabled", False),
            ("real_bet_created", False),
            ("ledger_mutated", False),
            ("sealed_data_accessed", False),
            ("research_mutated", False),
            ("production_activation", False),
        )
    ):
        findings.append(
            _finding(
                "PROVENANCE_MISMATCH", "evidence bundle safety assertions are unsafe"
            )
        )
    return not findings, findings


def _sample_report_audit(
    report: Mapping[str, object] | None,
    receipts: Mapping[str, Mapping[str, object]],
) -> tuple[bool | None, list[AuditFinding]]:
    if report is None:
        return None, []
    findings: list[AuditFinding] = []
    try:
        parsed = Builder2QualificationSampleReportV1.from_payload(report)
        parsed.validate()
    except (
        Builder2QualificationSampleAggregatorError,
        KeyError,
        TypeError,
        ValueError,
    ):
        findings.append(
            _finding(
                "PROVENANCE_MISMATCH",
                "Builder-2 sample report reference fails its canonical validator",
            )
        )
        return False, findings
    report_ids = {
        item.get("qualification_receipt_id")
        for item in _records(
            report.get("receipt_provenance"), "sample.receipt_provenance"
        )
    }
    if not set(receipts).issubset(report_ids):
        findings.append(
            _finding(
                "PROVENANCE_MISMATCH",
                "sample report does not reference every observed qualification receipt",
            )
        )
    return not findings, findings


def _prediction_payload(
    prediction: dict[str, Any],
    observation: dict[str, Any] | None,
    result: dict[str, Any],
    closing: dict[str, Any],
    session: Mapping[str, object],
    experiment: RealShadowExperiment | None,
    session_valid: bool,
    bundle_valid: bool | None,
    extra_findings: Sequence[AuditFinding],
) -> dict[str, object]:
    raw = prediction["raw"]
    obs_raw = observation["raw"] if observation else {}
    obs_obj = observation.get("observation") if observation else None
    receipt = observation.get("receipt") if observation else None
    findings = list(prediction["findings"])
    if observation:
        findings.extend(observation.get("findings", ()))
    findings.extend(result["findings"])
    findings.extend(closing["findings"])
    findings.extend(extra_findings)
    provenance_valid = bool(
        observation
        and observation.get("provenance_valid")
        and prediction.get("frozen_binding_valid")
    )
    identity_valid = bool(prediction.get("identity_valid"))
    digests_valid = bool(prediction.get("digests_valid"))
    lifecycle_valid = session_valid and not any(
        item.code in {"CONFLICT", "IDENTITY_MISMATCH", "DIGEST_MISMATCH"}
        for item in findings
    )
    flags = {
        "QUALIFICATION_PRESENT": bool(
            observation and observation.get("qualification_present")
        ),
        "OBSERVATION_PRESENT": bool(observation),
        "PREDICTION_PRESENT": True,
        "RESULT_PRESENT": bool(result["present"]),
        "CLOSING_PRESENT": bool(closing["present"]),
        "PROVENANCE_VALID": provenance_valid,
        "IDENTITY_VALID": identity_valid,
        "DIGESTS_VALID": digests_valid,
        "LIFECYCLE_VALID": lifecycle_valid,
        "AUDIT_COMPLETE": False,
    }
    mode = obs_raw.get("observation_mode")
    status = _status_for_prediction(
        mode,
        findings,
        flags["QUALIFICATION_PRESENT"],
        result,
        closing,
        flags,
    )
    flags["AUDIT_COMPLETE"] = (
        status == AuditStatus.COMPLETE.value
        and all(
            flags[name]
            for name in (
                "QUALIFICATION_PRESENT",
                "OBSERVATION_PRESENT",
                "PREDICTION_PRESENT",
                "RESULT_PRESENT",
                "CLOSING_PRESENT",
                "PROVENANCE_VALID",
                "IDENTITY_VALID",
                "DIGESTS_VALID",
                "LIFECYCLE_VALID",
            )
        )
        and (bundle_valid is True)
    )
    if not flags["AUDIT_COMPLETE"] and status == AuditStatus.COMPLETE.value:
        status = AuditStatus.INCOMPLETE.value
    return {
        "prediction_id": raw.get("prediction_id"),
        "fixture": raw.get("fixture_key"),
        "league": raw.get("league_code"),
        "provider": raw.get("provider_identity"),
        "provider_event_id": obs_raw.get("provider_fixture_id"),
        "provider_request_id": obs_raw.get("request_identity"),
        "qualification_receipt_id": receipt.get("qualification_receipt_id")
        if isinstance(receipt, Mapping)
        else None,
        "qualification_receipt_digest": receipt.get("receipt_digest")
        if isinstance(receipt, Mapping)
        else None,
        "observation_id": obs_raw.get("observation_id"),
        "observation_digest": receipt.get("observation_digest")
        if isinstance(receipt, Mapping)
        else (obs_obj.observation_digest() if obs_obj is not None else None),
        "normalized_record_digest": receipt.get("normalized_record_digest")
        if isinstance(receipt, Mapping)
        else None,
        "cascade_evidence_digest": receipt.get("cascade_evidence_digest")
        if isinstance(receipt, Mapping)
        else None,
        "capture_attestation_digest": receipt.get("capture_attestation_digest")
        if isinstance(receipt, Mapping)
        else None,
        "controlled_shadow_run_id": receipt.get("controlled_shadow_run_id")
        if isinstance(receipt, Mapping)
        else None,
        "qualification_session_id": receipt.get("qualification_session_id")
        if isinstance(receipt, Mapping)
        else None,
        "ceo_authorization_id": receipt.get("ceo_authorization_id")
        if isinstance(receipt, Mapping)
        else None,
        "prediction_artifact_sha": raw.get("artifact_sha"),
        "result_attachment_sha": result["raw"].get("attachment_sha")
        if result.get("raw")
        else None,
        "result_status": (
            getattr(result.get("result").status, "value", result.get("result").status)
            if result.get("result")
            else None
        ),
        "closing_attachment_sha": closing["raw"].get("attachment_sha")
        if closing.get("raw")
        else None,
        "research_sha": raw.get("research_sha"),
        "integration_sha": raw.get("integration_sha") or session.get("integration_sha"),
        "signal_time_experiment_id": experiment.experiment_id if experiment else None,
        "evidence_mode": mode,
        "completeness": flags,
        "findings": [item.as_payload() for item in findings],
        "overall_state": status,
    }


def _base_session_audit(
    payload: Mapping[str, object],
    *,
    evidence_bundle: Mapping[str, object] | None = None,
    sample_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    manifest = _mapping(payload.get("session")) or {}
    experiment_raw = _mapping(payload.get("experiment")) or {}
    experiment = _experiment(experiment_raw)
    session_id = manifest.get("session_id")
    core_findings: list[AuditFinding] = []
    if payload.get("schema") != REAL_SHADOW_SESSION_SCHEMA:
        core_findings.append(_finding("FAILED_CLOSED", "session schema is unsupported"))
    if experiment is None:
        core_findings.append(_finding("FAILED_CLOSED", "session experiment is invalid"))
    if (
        manifest.get("research_sha") != FROZEN_RESEARCH_SHA
        or manifest.get("model_identity") != M5_CANDIDATE_ID
    ):
        core_findings.append(
            _finding(
                "PROVENANCE_MISMATCH", "session is not bound to frozen M5 research"
            )
        )
    if (
        manifest.get("no_bet") is not True
        or manifest.get("publication") is not False
        or manifest.get("activation") is not False
    ):
        core_findings.append(
            _finding("PROVENANCE_MISMATCH", "session violates NO-BET safety")
        )
    scope = manifest.get("league_scope")
    if (
        not isinstance(scope, Sequence)
        or isinstance(scope, (str, bytes))
        or not scope
        or any(item not in TOP5_REAL_SHADOW_LEAGUES for item in scope)
    ):
        core_findings.append(
            _finding(
                "IDENTITY_MISMATCH",
                "session league scope is outside the five Top-5 leagues",
            )
        )
    session_obj: RealShadowSession | None = None
    try:
        session_obj = RealShadowSession.from_payload(payload)
    except (RealShadowContractError, AttributeError, KeyError, TypeError, ValueError):
        core_findings.append(
            _finding(
                "LIFECYCLE_INVALID",
                "session manifest or lifecycle artifacts fail the canonical session validator",
            )
        )
    session_valid = not core_findings and session_obj is not None
    if session_valid:
        expected_digest = session_obj.session_digest()
        if manifest.get("session_digest") != expected_digest:
            core_findings.append(
                _finding(
                    "DIGEST_MISMATCH", "session manifest digest is not deterministic"
                )
            )
            session_valid = False

    observations = _records(payload.get("observations"), "observations")
    predictions = _records(payload.get("predictions"), "predictions")
    results = _records(payload.get("results"), "results")
    closings = _records(payload.get("closings"), "closings")
    observation_map, observation_duplicates = _index(observations, "fixture_key")
    prediction_map, prediction_duplicates = _index(predictions, "prediction_id")
    result_map, result_duplicates = _index(results, "prediction_id")
    closing_map, closing_duplicates = _index(closings, "prediction_id")
    global_findings = list(core_findings)
    for name, values in (
        ("observation", observation_duplicates),
        ("prediction", prediction_duplicates),
        ("result", result_duplicates),
        ("closing", closing_duplicates),
    ):
        if values:
            global_findings.append(
                _finding("CONFLICT", f"duplicate {name} identities are present")
            )

    observation_records = {
        key: _observation_record(value, experiment)
        for key, value in observation_map.items()
    }
    receipt_map = {
        record.get("receipt", {}).get("qualification_receipt_id"): record["receipt"]
        for record in observation_records.values()
        if isinstance(record.get("receipt"), Mapping)
        and record["receipt"].get("qualification_receipt_id")
    }
    sample_valid, sample_findings = _sample_report_audit(sample_report, receipt_map)
    bundle_valid: bool | None = None
    bundle_findings: list[AuditFinding] = []
    if evidence_bundle is None:
        bundle_valid, bundle_findings = _safe_bundle_audit(
            None, {}, {}, {}, {}, manifest
        )
    else:
        bundle_valid, bundle_findings = _safe_bundle_audit(
            evidence_bundle,
            {key: {"raw": value} for key, value in prediction_map.items()},
            observation_records,
            result_map,
            closing_map,
            manifest,
        )
    prediction_records: list[dict[str, object]] = []
    for prediction_id in sorted(prediction_map):
        raw_prediction = prediction_map[prediction_id]
        observation_record = observation_records.get(raw_prediction.get("fixture_key"))
        record = _prediction_record(
            raw_prediction, observation_record, manifest, experiment
        )
        result_record = _result_record(result_map.get(prediction_id), record)
        closing_record = _closing_record(closing_map.get(prediction_id), record)
        duplicate_findings = []
        if prediction_id in prediction_duplicates:
            duplicate_findings.append(
                _finding("CONFLICT", "duplicate prediction identity is present")
            )
        if prediction_id in result_duplicates:
            duplicate_findings.append(
                _finding("CONFLICT", "duplicate result identity is present")
            )
        if prediction_id in closing_duplicates:
            duplicate_findings.append(
                _finding("CONFLICT", "duplicate closing identity is present")
            )
        prediction_records.append(
            _prediction_payload(
                record,
                observation_record,
                result_record,
                closing_record,
                manifest,
                experiment,
                session_valid,
                bundle_valid,
                [*duplicate_findings, *sample_findings, *bundle_findings],
            )
        )
    if bundle_findings:
        global_findings.extend(bundle_findings)
    if sample_findings:
        global_findings.extend(sample_findings)
    overall_state = (
        AuditStatus.COMPLETE.value
        if prediction_records
        and all(
            item["overall_state"] == AuditStatus.COMPLETE.value
            for item in prediction_records
        )
        and session_valid
        else AuditStatus.INCOMPLETE.value
    )
    if global_findings and overall_state == AuditStatus.COMPLETE.value:
        overall_state = AuditStatus.INCOMPLETE.value
    result = {
        "audit_schema": AUDIT_SCHEMA_VERSION,
        "session_id": session_id,
        "session_schema": payload.get("schema"),
        "fixture_mode": manifest.get("fixture_mode"),
        "research_sha": manifest.get("research_sha"),
        "integration_sha": manifest.get("integration_sha"),
        "model_identity": manifest.get("model_identity"),
        "qualification_sample_report": {
            "present": sample_report is not None,
            "valid": sample_valid,
        },
        "shadow_evidence_bundle": {
            "present": evidence_bundle is not None,
            "valid": bundle_valid,
        },
        "predictions": prediction_records,
        "findings": [
            item.as_payload() for item in [*global_findings, *sample_findings]
        ],
        "overall_state": overall_state,
    }
    result["deterministic_audit_digest"] = _stable_digest(result)
    return result


def audit_session_payload(
    payload: Mapping[str, object],
    *,
    evidence_bundle: Mapping[str, object] | None = None,
    sample_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Audit one session payload without network or persistence side effects."""

    if not isinstance(payload, Mapping):
        raise ShadowAuditError("session payload must be a JSON object")
    return _base_session_audit(
        payload, evidence_bundle=evidence_bundle, sample_report=sample_report
    )


def _summary(audits: Sequence[Mapping[str, object]]) -> dict[str, object]:
    predictions = [
        item
        for audit in audits
        for item in audit.get("predictions", ())
        if isinstance(item, Mapping)
    ]
    states = Counter(item.get("overall_state") for item in predictions)
    per_league = Counter(
        item.get("league") for item in predictions if item.get("league")
    )
    per_provider = Counter(
        item.get("provider") for item in predictions if item.get("provider")
    )
    return {
        "total_predictions": len(predictions),
        "complete": states[AuditStatus.COMPLETE.value],
        "incomplete": len(predictions) - states[AuditStatus.COMPLETE.value],
        "pending_result": states[AuditStatus.PENDING_RESULT.value],
        "pending_closing": states[AuditStatus.PENDING_CLOSING.value],
        "qualification_failures": sum(
            item.get("overall_state") == AuditStatus.QUALIFICATION_MISSING.value
            for item in predictions
        ),
        "provenance_conflicts": states[AuditStatus.PROVENANCE_MISMATCH.value],
        "identity_conflicts": states[AuditStatus.IDENTITY_MISMATCH.value],
        "digest_conflicts": states[AuditStatus.DIGEST_MISMATCH.value],
        "real_observed": sum(
            item.get("evidence_mode") == REAL_OBSERVED_MARKER for item in predictions
        ),
        "test_fixture": sum(
            item.get("evidence_mode") == TEST_FIXTURE_MARKER for item in predictions
        ),
        "offline_replay_rejections": sum(
            item.get("overall_state") == AuditStatus.NON_REAL_EVIDENCE.value
            for item in predictions
        ),
        "per_league": dict(sorted(per_league.items())),
        "per_provider": dict(sorted(per_provider.items())),
    }


def audit_directory_payloads(
    payloads: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Audit a deterministic set of session payloads in input order-independent form."""

    sessions = [
        item for item in payloads if isinstance(item, Mapping) and "session" in item
    ]
    bundles: dict[str, Mapping[str, object]] = {}
    for item in payloads:
        if item.get("contract_version") != EVIDENCE_BUNDLE_SCHEMA:
            continue
        manifest = _mapping(item.get("real_shadow_session")) or {}
        session_id = manifest.get("session_id")
        if isinstance(session_id, str) and session_id:
            bundles[session_id] = item
    audits = [
        audit_session_payload(
            item,
            evidence_bundle=bundles.get(
                str((_mapping(item.get("session")) or {}).get("session_id", ""))
            ),
        )
        for item in sorted(
            sessions,
            key=lambda value: str(
                (_mapping(value.get("session")) or {}).get("session_id", "")
            ),
        )
    ]
    summary = _summary(audits)
    result: dict[str, object] = {
        "audit_schema": AUDIT_SCHEMA_VERSION,
        "audits": audits,
        "summary": summary,
        "overall_state": AuditStatus.COMPLETE.value
        if audits
        and all(
            item.get("overall_state") == AuditStatus.COMPLETE.value for item in audits
        )
        else AuditStatus.INCOMPLETE.value,
    }
    result["deterministic_audit_digest"] = _stable_digest(result)
    return result


def audit_prediction_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Audit a standalone prediction while refusing to infer missing lifecycle evidence."""

    raw = (
        payload.get("prediction")
        if isinstance(payload.get("prediction"), Mapping)
        else payload
    )
    if not isinstance(raw, Mapping):
        raise ShadowAuditError("prediction payload must be a JSON object")
    try:
        prediction = RealShadowPredictionArtifact.from_payload(raw)
        prediction.validate()
        valid = True
    except (RealShadowContractError, TypeError, ValueError):
        valid = False
    item = {
        "prediction_id": raw.get("prediction_id"),
        "fixture": raw.get("fixture_key"),
        "league": raw.get("league_code"),
        "provider": raw.get("provider_identity"),
        "prediction_artifact_sha": raw.get("artifact_sha"),
        "research_sha": raw.get("research_sha"),
        "integration_sha": raw.get("integration_sha"),
        "completeness": {
            "QUALIFICATION_PRESENT": False,
            "OBSERVATION_PRESENT": False,
            "PREDICTION_PRESENT": True,
            "RESULT_PRESENT": False,
            "CLOSING_PRESENT": False,
            "PROVENANCE_VALID": valid and raw.get("marker") == REAL_OBSERVED_MARKER,
            "IDENTITY_VALID": valid,
            "DIGESTS_VALID": valid,
            "LIFECYCLE_VALID": False,
            "AUDIT_COMPLETE": False,
        },
        "findings": [
            _finding(
                "INCOMPLETE",
                "standalone prediction has no session, observation, result, or closing context",
            ).as_payload()
        ],
        "overall_state": AuditStatus.INCOMPLETE.value,
    }
    result: dict[str, object] = {
        "audit_schema": AUDIT_SCHEMA_VERSION,
        "session_id": raw.get("session_id"),
        "predictions": [item],
        "summary": _summary([{"predictions": [item]}]),
        "overall_state": AuditStatus.INCOMPLETE.value,
    }
    result["deterministic_audit_digest"] = _stable_digest(result)
    return result


def load_json(path: Path) -> Mapping[str, object]:
    """Load one local JSON artifact; no network or write path is available."""

    resolved = path.expanduser().resolve()
    if any(part.casefold() == "ledger" for part in resolved.parts):
        raise ShadowAuditError("ledger paths are outside the read-only audit boundary")
    try:
        value = json.loads(resolved.read_text())
    except (OSError, ValueError) as exc:
        raise ShadowAuditError("audit artifact is unreadable JSON") from exc
    if not isinstance(value, Mapping):
        raise ShadowAuditError("audit artifact must be a JSON object")
    return value


def render_markdown(report: Mapping[str, object]) -> str:
    """Render only redacted identities, counts, states, and findings."""

    summary = report.get("summary", {})
    lines = [
        "Top-5 Real Shadow Evidence Audit",
        "READ ONLY | NO NETWORK | NO BET | NO PUBLICATION | NO PRODUCTION ACTIVATION",
        f"Overall state: {report.get('overall_state')}",
        f"Predictions: {summary.get('total_predictions', 0)} | complete: {summary.get('complete', 0)} | incomplete: {summary.get('incomplete', 0)}",
    ]
    for audit in report.get("audits", ()):  # directory reports
        if isinstance(audit, Mapping):
            lines.append(
                f"- session {audit.get('session_id')}: {audit.get('overall_state')}"
            )
    for item in report.get("predictions", ()):
        if isinstance(item, Mapping):
            lines.append(
                f"- prediction {item.get('prediction_id')}: {item.get('overall_state')}"
            )
    return "\n".join(lines)


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AuditStatus",
    "ShadowAuditError",
    "audit_directory_payloads",
    "audit_prediction_payload",
    "audit_session_payload",
    "load_json",
    "render_markdown",
]
