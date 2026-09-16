"""Deterministic, read-only measurement of audited Top-5 shadow evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from math import isfinite, log

from src.football.top5_real_shadow_attachments import (
    RealShadowClosingAttachment,
    RealShadowResultAttachment,
)
from src.football.top5_real_shadow_audit import (
    AUDIT_SCHEMA_VERSION,
    AuditStatus,
    audit_session_payload,
)
from src.football.top5_real_shadow_contracts import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    REAL_OBSERVED_MARKER,
    RealShadowContractError,
    RealShadowPredictionArtifact,
)

MEASUREMENT_SCHEMA_VERSION = "top5-real-shadow-measurement-v1"
EVIDENCE_BUNDLE_SCHEMA = "top5-shadow-evidence-v1"
_OUTCOMES = ("away", "draw", "home")
_CALIBRATION_BINS = 10


class MeasurementError(ValueError):
    """Malformed or unsafe measurement input."""


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
        raise MeasurementError("measurement input is not deterministic JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _records(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MeasurementError(f"{field} must be a JSON array")
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MeasurementError(f"{field} contains a non-object record")
        result.append(item)
    return tuple(result)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _reason_for_audit(item: Mapping[str, object]) -> str:
    state = item.get("overall_state")
    if state == AuditStatus.NON_REAL_EVIDENCE.value:
        return "NON_REAL_EVIDENCE"
    findings = item.get("findings", ())
    codes = {
        finding.get("code") for finding in findings if isinstance(finding, Mapping)
    }
    for reason in (
        "CONFLICT",
        "DIGEST_MISMATCH",
        "PROVENANCE_MISMATCH",
        "IDENTITY_MISMATCH",
        "QUALIFICATION_MISSING",
        "PENDING_RESULT",
    ):
        if reason in codes or state == reason:
            return reason
    if state not in {AuditStatus.COMPLETE.value, AuditStatus.PENDING_CLOSING.value}:
        return "AUDIT_INCOMPLETE"
    return "AUDIT_INCOMPLETE"


def _metric_template() -> dict[str, object]:
    return {
        "N": 0,
        "brier_score": None,
        "log_loss": None,
        "top_probability_accuracy": None,
        "mean_predicted_probability_of_realized_outcome": None,
        "outcome_distribution": {name: 0 for name in _OUTCOMES},
        "predicted_class_distribution": {name: 0 for name in _OUTCOMES},
        "confidence_distribution": {
            "count": 0,
            "mean": None,
            "minimum": None,
            "maximum": None,
            "histogram": [0] * _CALIBRATION_BINS,
        },
        "calibration_bins": [
            {
                "bin": index,
                "lower_bound": index / _CALIBRATION_BINS,
                "upper_bound": (index + 1) / _CALIBRATION_BINS,
                "count": 0,
                "mean_confidence": None,
                "accuracy": None,
            }
            for index in range(_CALIBRATION_BINS)
        ],
        "calibration_error_summary": {
            "mean_absolute_probability_error": None,
            "expected_calibration_error": None,
            "bin_count": 0,
        },
    }


def _prediction_class(probabilities: Mapping[str, float]) -> str:
    return max(_OUTCOMES, key=lambda name: float(probabilities[name]))


def _metric_summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    report = _metric_template()
    if not records:
        return report
    brier_values: list[float] = []
    log_loss_values: list[float] = []
    realized_probabilities: list[float] = []
    confidence_values: list[float] = []
    calibration_values: list[float] = []
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(_CALIBRATION_BINS)]
    outcome_counts = Counter()
    predicted_counts = Counter()
    for record in records:
        probabilities = record["probabilities"]
        actual = str(record["actual_outcome"])
        predicted = _prediction_class(probabilities)
        confidence = float(probabilities[predicted])
        indicator = {name: float(name == actual) for name in _OUTCOMES}
        brier_values.append(
            sum(
                (float(probabilities[name]) - indicator[name]) ** 2
                for name in _OUTCOMES
            )
            / len(_OUTCOMES)
        )
        log_loss_values.append(-log(max(float(probabilities[actual]), 1e-15)))
        realized_probabilities.append(float(probabilities[actual]))
        confidence_values.append(confidence)
        outcome_counts[actual] += 1
        predicted_counts[predicted] += 1
        calibration_values.extend(
            abs(float(probabilities[name]) - indicator[name]) for name in _OUTCOMES
        )
        bin_index = min(int(confidence * _CALIBRATION_BINS), _CALIBRATION_BINS - 1)
        bins[bin_index].append((confidence, predicted == actual))

    calibration_bins = []
    for index, values in enumerate(bins):
        count = len(values)
        calibration_bins.append(
            {
                "bin": index,
                "lower_bound": index / _CALIBRATION_BINS,
                "upper_bound": (index + 1) / _CALIBRATION_BINS,
                "count": count,
                "mean_confidence": (
                    sum(value[0] for value in values) / count if count else None
                ),
                "accuracy": (
                    sum(value[1] for value in values) / count if count else None
                ),
            }
        )
    ece = sum(
        len(values)
        / len(records)
        * abs(
            sum(value[0] for value in values) / len(values)
            - sum(value[1] for value in values) / len(values)
        )
        for values in bins
        if values
    )
    report.update(
        {
            "N": len(records),
            "brier_score": sum(brier_values) / len(brier_values),
            "log_loss": sum(log_loss_values) / len(log_loss_values),
            "top_probability_accuracy": sum(
                _prediction_class(record["probabilities"])
                == str(record["actual_outcome"])
                for record in records
            )
            / len(records),
            "mean_predicted_probability_of_realized_outcome": sum(
                realized_probabilities
            )
            / len(realized_probabilities),
            "outcome_distribution": {name: outcome_counts[name] for name in _OUTCOMES},
            "predicted_class_distribution": {
                name: predicted_counts[name] for name in _OUTCOMES
            },
            "confidence_distribution": {
                "count": len(confidence_values),
                "mean": sum(confidence_values) / len(confidence_values),
                "minimum": min(confidence_values),
                "maximum": max(confidence_values),
                "histogram": [len(values) for values in bins],
            },
            "calibration_bins": calibration_bins,
            "calibration_error_summary": {
                "mean_absolute_probability_error": sum(calibration_values)
                / len(calibration_values),
                "expected_calibration_error": ece,
                "bin_count": sum(bool(values) for values in bins),
            },
        }
    )
    return report


def _implied_probabilities(odds: Mapping[str, float]) -> dict[str, float]:
    inverse = {name: 1.0 / float(odds[name]) for name in _OUTCOMES}
    total = sum(inverse.values())
    if not isfinite(total) or total <= 0:
        raise MeasurementError("closing odds cannot produce implied probabilities")
    return {name: inverse[name] / total for name in _OUTCOMES}


def _group_summary(
    records: Sequence[Mapping[str, object]], key: str
) -> dict[str, object]:
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        value = record.get(key)
        if isinstance(value, str) and value:
            groups[value].append(record)
    return {
        name: _metric_summary(
            sorted(values, key=lambda item: str(item["prediction_id"]))
        )
        for name, values in sorted(groups.items())
    }


def _safe_exclusion(
    item: Mapping[str, object], reason: str, *, session_id: object
) -> dict[str, object]:
    findings = item.get("findings", ())
    return {
        "prediction_id": item.get("prediction_id"),
        "fixture": item.get("fixture"),
        "session_id": session_id,
        "signal_time_experiment_id": item.get("signal_time_experiment_id"),
        "reason": reason,
        "audit_state": item.get("overall_state"),
        "finding_codes": sorted(
            {
                finding.get("code")
                for finding in findings
                if isinstance(finding, Mapping) and finding.get("code")
            }
        ),
    }


def _public_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
    closing = candidate.get("closing")
    closing_payload = None
    if isinstance(closing, RealShadowClosingAttachment):
        closing_payload = {
            "attachment_sha": closing.attachment_sha,
            "closing_snapshot_id": closing.closing_snapshot_id,
            "closing_probabilities": _implied_probabilities(closing.odds),
            "used_for_prediction": False,
        }
    return {
        key: candidate[key]
        for key in (
            "prediction_id",
            "fixture",
            "league",
            "provider",
            "bookmaker",
            "signal_time_experiment_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "session_id",
            "research_sha",
            "model_identity",
            "probabilities",
            "actual_outcome",
            "prediction_artifact_sha",
            "result_attachment_sha",
        )
    } | {"closing": closing_payload, "evidence_mode": REAL_OBSERVED_MARKER}


def _candidate(
    payload: Mapping[str, object],
    audit: Mapping[str, object],
    audit_item: Mapping[str, object],
) -> dict[str, object] | None:
    prediction_id = audit_item.get("prediction_id")
    raw_predictions = {
        item.get("prediction_id"): item
        for item in _records(payload.get("predictions"), "predictions")
    }
    raw = raw_predictions.get(prediction_id)
    if not isinstance(raw, Mapping):
        return None
    results = {
        item.get("prediction_id"): item
        for item in _records(payload.get("results"), "results")
    }
    closings = {
        item.get("prediction_id"): item
        for item in _records(payload.get("closings"), "closings")
    }
    try:
        prediction = RealShadowPredictionArtifact.from_payload(raw)
        prediction.validate()
        result_raw = results.get(prediction_id)
        if not isinstance(result_raw, Mapping):
            return None
        result = RealShadowResultAttachment.from_payload(result_raw)
        result.validate()
        if result.actual_outcome not in _OUTCOMES:
            return None
        closing_raw = closings.get(prediction_id)
        closing = None
        if isinstance(closing_raw, Mapping):
            closing = RealShadowClosingAttachment.from_payload(closing_raw)
            closing.validate()
    except (RealShadowContractError, TypeError, ValueError, KeyError):
        return None
    return {
        "prediction_id": prediction.prediction_id,
        "fixture": prediction.fixture_key,
        "league": prediction.league_code,
        "provider": prediction.provider_identity,
        "bookmaker": prediction.bookmaker_identity,
        "signal_time_experiment_id": audit_item.get("signal_time_experiment_id"),
        "controlled_shadow_run_id": audit_item.get("controlled_shadow_run_id"),
        "qualification_session_id": audit_item.get("qualification_session_id"),
        "qualification_receipt_id": audit_item.get("qualification_receipt_id"),
        "session_id": prediction.session_id,
        "research_sha": prediction.research_sha,
        "model_identity": prediction.model_identity,
        "probabilities": {
            name: float(prediction.probabilities[name]) for name in _OUTCOMES
        },
        "actual_outcome": result.actual_outcome,
        "prediction_artifact_sha": prediction.artifact_sha,
        "result_attachment_sha": result.attachment_sha,
        "closing": closing,
        "audit_digest": audit.get("deterministic_audit_digest"),
    }


def _closing_summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    observations: list[dict[str, object]] = []
    for record in records:
        closing = record.get("closing")
        if closing is None:
            continue
        if not isinstance(closing, RealShadowClosingAttachment):
            raise MeasurementError("closing attachment is not canonical")
        closing_probabilities = _implied_probabilities(closing.odds)
        probabilities = record["probabilities"]
        selected = _prediction_class(probabilities)
        signed_delta = float(probabilities[selected]) - closing_probabilities[selected]
        observations.append(
            {
                "prediction_id": record["prediction_id"],
                "fixture": record["fixture"],
                "provider": record["provider"],
                "bookmaker": record["bookmaker"],
                "selected_outcome": selected,
                "prediction_implied_probability": float(probabilities[selected]),
                "closing_implied_probability": closing_probabilities[selected],
                "signed_probability_delta": signed_delta,
                "outcome_probability_delta": {
                    name: float(probabilities[name]) - closing_probabilities[name]
                    for name in _OUTCOMES
                },
                "used_for_prediction": False,
            }
        )
    if not observations:
        return {
            "available": False,
            "label": "BENCHMARK / CLV MEASUREMENT ONLY",
            "reason": "no valid closing attachment is available",
            "count": 0,
            "observations": [],
        }
    deltas = [float(item["signed_probability_delta"]) for item in observations]
    return {
        "available": True,
        "label": "BENCHMARK / CLV MEASUREMENT ONLY",
        "count": len(observations),
        "mean_signed_probability_delta": sum(deltas) / len(deltas),
        "mean_absolute_probability_delta": sum(abs(value) for value in deltas)
        / len(deltas),
        "observations": sorted(
            observations, key=lambda item: str(item["prediction_id"])
        ),
    }


def _directory_inputs(
    payloads: Sequence[Mapping[str, object]],
) -> tuple[tuple[Mapping[str, object], Mapping[str, object] | None], ...]:
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
    ordered = sorted(
        sessions,
        key=lambda item: str(
            (_mapping(item.get("session")) or {}).get("session_id", "")
        ),
    )
    return tuple(
        (
            item,
            bundles.get(
                str((_mapping(item.get("session")) or {}).get("session_id", ""))
            ),
        )
        for item in ordered
    )


def _measure_session_components(
    payload: Mapping[str, object],
    *,
    evidence_bundle: Mapping[str, object] | None = None,
    sample_report: Mapping[str, object] | None = None,
) -> tuple[
    Mapping[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """Collect only canonical candidates after the auditor has checked a session."""

    if not isinstance(payload, Mapping):
        raise MeasurementError("session payload must be a JSON object")
    audit = audit_session_payload(
        payload, evidence_bundle=evidence_bundle, sample_report=sample_report
    )
    session_id = audit.get("session_id")
    audit_items = {
        item.get("prediction_id"): item
        for item in _records(audit.get("predictions"), "audit.predictions")
    }
    candidates: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    cohort_records: list[dict[str, object]] = []
    for prediction_id, item in sorted(
        audit_items.items(), key=lambda pair: str(pair[0])
    ):
        raw_prediction = next(
            (
                value
                for value in _records(payload.get("predictions"), "predictions")
                if value.get("prediction_id") == prediction_id
            ),
            {},
        )
        cohort_records.append(
            {
                "prediction_id": prediction_id,
                "evidence_mode": item.get("evidence_mode"),
                "research_sha": item.get("research_sha"),
                "model_identity": raw_prediction.get("model_identity"),
            }
        )
        flags = _mapping(item.get("completeness")) or {}
        core_complete = all(
            flags.get(name) is True
            for name in (
                "QUALIFICATION_PRESENT",
                "OBSERVATION_PRESENT",
                "PREDICTION_PRESENT",
                "RESULT_PRESENT",
                "PROVENANCE_VALID",
                "IDENTITY_VALID",
                "DIGESTS_VALID",
                "LIFECYCLE_VALID",
            )
        )
        metric_eligible = (
            item.get("evidence_mode") == REAL_OBSERVED_MARKER
            and core_complete
            and item.get("overall_state")
            in {AuditStatus.COMPLETE.value, AuditStatus.PENDING_CLOSING.value}
        )
        candidate = _candidate(payload, audit, item)
        if metric_eligible and candidate is not None:
            candidates.append(candidate)
        else:
            reason = _reason_for_audit(item)
            if candidate is None and reason == "AUDIT_INCOMPLETE":
                reason = "INVALID_CANONICAL_ARTIFACT"
            excluded.append(_safe_exclusion(item, reason, session_id=session_id))
    return audit, candidates, excluded, cohort_records


def measure_session_payload(
    payload: Mapping[str, object],
    *,
    evidence_bundle: Mapping[str, object] | None = None,
    sample_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Measure one session after the canonical auditor has checked it."""

    audit, candidates, excluded, _ = _measure_session_components(
        payload, evidence_bundle=evidence_bundle, sample_report=sample_report
    )
    return _assemble_report([(audit, candidates, excluded, _)], input_count=1)


def measure_directory_payloads(
    payloads: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Measure a deterministic directory-like collection of session artifacts."""

    inputs = _directory_inputs(payloads)
    audits: list[
        tuple[
            Mapping[str, object],
            list[dict[str, object]],
            list[dict[str, object]],
            list[dict[str, object]],
        ]
    ] = []
    for payload, evidence in inputs:
        audit, candidates, excluded, cohort_records = _measure_session_components(
            payload, evidence_bundle=evidence
        )
        audits.append(
            (
                audit,
                candidates,
                excluded,
                cohort_records,
            )
        )
    return _assemble_report(audits, input_count=len(inputs))


def _assemble_report(
    audits: Sequence[
        tuple[
            Mapping[str, object],
            list[dict[str, object]],
            list[dict[str, object]],
        ]
    ],
    *,
    input_count: int,
) -> dict[str, object]:
    audit_digests = [
        {
            "session_id": audit.get("session_id"),
            "audit_digest": audit.get("deterministic_audit_digest"),
        }
        for audit, _, _, _ in audits
    ]
    audit_digests.sort(key=lambda item: str(item["session_id"]))
    candidates = [candidate for _, values, _, _ in audits for candidate in values]
    excluded = [item for _, _, values, _ in audits for item in values]
    cohort_records = [record for _, _, _, values in audits for record in values]
    candidates.sort(key=lambda item: str(item["prediction_id"]))
    excluded.sort(
        key=lambda item: (str(item.get("prediction_id")), str(item.get("reason")))
    )
    hard_findings: list[str] = []
    by_prediction: dict[str, list[dict[str, object]]] = defaultdict(list)
    for candidate in candidates:
        by_prediction[str(candidate["prediction_id"])].append(candidate)
    duplicate_ids = {
        prediction_id
        for prediction_id, values in by_prediction.items()
        if len(values) > 1
    }
    if duplicate_ids:
        for prediction_id in sorted(duplicate_ids):
            values = by_prediction[prediction_id]
            fingerprints = {
                (
                    value["prediction_artifact_sha"],
                    value["session_id"],
                    value["fixture"],
                )
                for value in values
            }
            hard_findings.append(
                "DIVERGENT_DUPLICATE_PREDICTION"
                if len(fingerprints) > 1
                else "DUPLICATE_PREDICTION"
            )
        for candidate in candidates:
            if str(candidate["prediction_id"]) in duplicate_ids:
                excluded.append(
                    {
                        "prediction_id": candidate["prediction_id"],
                        "fixture": candidate["fixture"],
                        "session_id": candidate["session_id"],
                        "reason": "DUPLICATE_PREDICTION",
                        "audit_state": AuditStatus.COMPLETE.value,
                        "finding_codes": ["CONFLICT"],
                    }
                )
        candidates = [
            candidate
            for candidate in candidates
            if str(candidate["prediction_id"]) not in duplicate_ids
        ]

    real_identity = {
        (record["research_sha"], record["model_identity"])
        for record in cohort_records
        if record.get("evidence_mode") == REAL_OBSERVED_MARKER
        and record.get("research_sha")
        and record.get("model_identity")
    }
    if len(real_identity) > 1:
        hard_findings.append("MIXED_RESEARCH_OR_MODEL_IDENTITY")

    audit_input_digest = _stable_digest(audit_digests)
    cohort_input = [
        {
            "prediction_id": candidate["prediction_id"],
            "fixture": candidate["fixture"],
            "research_sha": candidate["research_sha"],
            "model_identity": candidate["model_identity"],
            "prediction_artifact_sha": candidate["prediction_artifact_sha"],
            "result_attachment_sha": candidate["result_attachment_sha"],
            "actual_outcome": candidate["actual_outcome"],
            "probabilities": candidate["probabilities"],
        }
        for candidate in candidates
    ]
    cohort_digest = _stable_digest(cohort_input)
    measurement_id = f"top5-measurement:{_stable_digest({'audit': audit_input_digest, 'cohort': cohort_digest})[:32]}"
    failed_closed = bool(hard_findings)
    if failed_closed:
        excluded.extend(
            {
                "prediction_id": candidate["prediction_id"],
                "fixture": candidate["fixture"],
                "session_id": candidate["session_id"],
                "reason": "COHORT_FAILED_CLOSED",
                "audit_state": AuditStatus.COMPLETE.value,
                "finding_codes": sorted(set(hard_findings)),
            }
            for candidate in candidates
        )
        candidates = []
    excluded.sort(
        key=lambda item: (str(item.get("prediction_id")), str(item.get("reason")))
    )
    metrics = _metric_summary(candidates)
    groups = {
        "league": _group_summary(candidates, "league"),
        "provider": _group_summary(candidates, "provider"),
        "signal_time_experiment_id": _group_summary(
            candidates, "signal_time_experiment_id"
        ),
        "controlled_shadow_run_id": _group_summary(
            candidates, "controlled_shadow_run_id"
        ),
        "qualification_session_id": _group_summary(
            candidates, "qualification_session_id"
        ),
        "bookmaker": _group_summary(candidates, "bookmaker"),
    }
    report: dict[str, object] = {
        "measurement_schema": MEASUREMENT_SCHEMA_VERSION,
        "measurement_id": measurement_id,
        "audit_schema": AUDIT_SCHEMA_VERSION,
        "input_session_count": input_count,
        "audit_input_digests": audit_digests,
        "audit_input_digest": audit_input_digest,
        "cohort_digest": cohort_digest,
        "eligible_prediction_ids": [
            candidate["prediction_id"] for candidate in candidates
        ],
        "excluded_evidence": excluded,
        "eligible_predictions": [
            _public_candidate(candidate) for candidate in candidates
        ],
        "eligible_count": len(candidates),
        "excluded_count": len(excluded),
        "unique_fixture_count": len({candidate["fixture"] for candidate in candidates}),
        "unique_prediction_count": len(
            {candidate["prediction_id"] for candidate in candidates}
        ),
        "exclusion_taxonomy": dict(
            sorted(Counter(item["reason"] for item in excluded).items())
        ),
        "research_sha": FROZEN_RESEARCH_SHA,
        "model_identity": M5_CANDIDATE_ID,
        "experiment_ids": sorted(
            {
                candidate["signal_time_experiment_id"]
                for candidate in candidates
                if candidate.get("signal_time_experiment_id")
            }
        ),
        "primary_metrics": metrics,
        "groups": groups,
        "closing_benchmark": _closing_summary(candidates),
        "cohort_integrity": {
            "state": "FAILED_CLOSED" if failed_closed else "VALID",
            "findings": sorted(set(hard_findings)),
            "unique_fixture_count": len(
                {candidate["fixture"] for candidate in candidates}
            ),
            "unique_prediction_count": len(
                {candidate["prediction_id"] for candidate in candidates}
            ),
        },
        "safety_invariants": {
            "production_activation_authorized": False,
            "publication_authorized": False,
            "betting_authorized": False,
            "model_approved_for_production": False,
            "signal_time_approved_for_production": False,
            "read_only": True,
            "network_accessed": False,
            "provider_ranking_emitted": False,
            "closing_used_for_prediction": False,
            "sealed_data_accessed": False,
        },
        "overall_state": "FAILED_CLOSED" if failed_closed else "COMPLETE",
    }
    report["measurement_digest"] = _stable_digest(report)
    return report


def _public_report(report: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def render_markdown(report: Mapping[str, object]) -> str:
    """Render a concise, non-authoritative measurement summary."""

    metrics = _mapping(report.get("primary_metrics")) or {}
    safety = _mapping(report.get("safety_invariants")) or {}
    lines = [
        "Top-5 Real Shadow Measurement Pack V1",
        "READ ONLY | NO NETWORK | NO BET | NO PUBLICATION | NO PRODUCTION ACTIVATION",
        f"Overall state: {report.get('overall_state')}",
        f"Eligible: {report.get('eligible_count', 0)} | excluded: {report.get('excluded_count', 0)}",
        f"Brier: {metrics.get('brier_score')} | log loss: {metrics.get('log_loss')} | accuracy: {metrics.get('top_probability_accuracy')}",
        f"Closing: {(_mapping(report.get('closing_benchmark')) or {}).get('label')}",
        f"Activation authorized: {safety.get('production_activation_authorized')}",
        "Descriptive evidence only; no production decision authority.",
    ]
    return "\n".join(lines)


__all__ = [
    "MEASUREMENT_SCHEMA_VERSION",
    "MeasurementError",
    "measure_directory_payloads",
    "measure_session_payload",
    "render_markdown",
]
