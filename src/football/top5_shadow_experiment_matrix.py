"""Neutral, deterministic experiment evidence from Top-5 measurements."""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from itertools import combinations
from math import isfinite, log

from src.football.top5_real_shadow_contracts import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    REAL_OBSERVED_MARKER,
)
from src.football.top5_real_shadow_measurement import (
    MEASUREMENT_SCHEMA_VERSION,
)

MATRIX_SCHEMA_VERSION = "top5-shadow-experiment-evidence-matrix-v1"
_OUTCOMES = ("away", "draw", "home")
_CALIBRATION_BINS = 10
_BOOTSTRAP_RESAMPLES = 2000


class MatrixError(ValueError):
    """Malformed or unsafe Measurement Pack input."""


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
        raise MatrixError("matrix input is not deterministic JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _records(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MatrixError(f"{field} must be a JSON array")
    result: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MatrixError(f"{field} contains a non-object record")
        result.append(item)
    return tuple(result)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _probabilities(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(_OUTCOMES):
        raise MatrixError("prediction probabilities must contain exactly 1X2 outcomes")
    result = {name: float(value[name]) for name in _OUTCOMES}
    if any(not isfinite(item) or not 0 <= item <= 1 for item in result.values()):
        raise MatrixError("prediction probabilities must be finite and normalized")
    if abs(sum(result.values()) - 1.0) > 1e-9:
        raise MatrixError("prediction probabilities must sum to one")
    return result


def _prediction_class(probabilities: Mapping[str, float]) -> str:
    return max(_OUTCOMES, key=lambda name: float(probabilities[name]))


def _metrics(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if not records:
        return {
            "eligible_n": 0,
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
            "calibration_summary": {
                "mean_absolute_probability_error": None,
                "expected_calibration_error": None,
                "bin_count": 0,
            },
        }
    brier: list[float] = []
    losses: list[float] = []
    realized: list[float] = []
    confidence: list[float] = []
    calibration: list[float] = []
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(_CALIBRATION_BINS)]
    outcomes = Counter()
    predicted = Counter()
    for record in records:
        probabilities = record["probabilities"]
        actual = str(record["actual_outcome"])
        selected = _prediction_class(probabilities)
        selected_confidence = float(probabilities[selected])
        indicators = {name: float(name == actual) for name in _OUTCOMES}
        brier.append(
            sum(
                (float(probabilities[name]) - indicators[name]) ** 2
                for name in _OUTCOMES
            )
            / len(_OUTCOMES)
        )
        losses.append(-log(max(float(probabilities[actual]), 1e-15)))
        realized.append(float(probabilities[actual]))
        confidence.append(selected_confidence)
        outcomes[actual] += 1
        predicted[selected] += 1
        calibration.extend(
            abs(float(probabilities[name]) - indicators[name]) for name in _OUTCOMES
        )
        index = min(int(selected_confidence * _CALIBRATION_BINS), _CALIBRATION_BINS - 1)
        bins[index].append((selected_confidence, selected == actual))
    ece = sum(
        len(values)
        / len(records)
        * abs(
            sum(item[0] for item in values) / len(values)
            - sum(item[1] for item in values) / len(values)
        )
        for values in bins
        if values
    )
    return {
        "eligible_n": len(records),
        "brier_score": sum(brier) / len(brier),
        "log_loss": sum(losses) / len(losses),
        "top_probability_accuracy": sum(
            _prediction_class(record["probabilities"]) == record["actual_outcome"]
            for record in records
        )
        / len(records),
        "mean_predicted_probability_of_realized_outcome": sum(realized) / len(realized),
        "outcome_distribution": {name: outcomes[name] for name in _OUTCOMES},
        "predicted_class_distribution": {name: predicted[name] for name in _OUTCOMES},
        "confidence_distribution": {
            "count": len(confidence),
            "mean": sum(confidence) / len(confidence),
            "minimum": min(confidence),
            "maximum": max(confidence),
            "histogram": [len(values) for values in bins],
        },
        "calibration_summary": {
            "mean_absolute_probability_error": sum(calibration) / len(calibration),
            "expected_calibration_error": ece,
            "bin_count": sum(bool(values) for values in bins),
        },
    }


def _closing_summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    deltas: list[float] = []
    for record in records:
        closing = _mapping(record.get("closing"))
        if closing is None:
            continue
        if closing.get("used_for_prediction") is not False:
            raise MatrixError("closing benchmark is not excluded from prediction")
        closing_probabilities = _probabilities(closing.get("closing_probabilities"))
        probabilities = record["probabilities"]
        selected = _prediction_class(probabilities)
        deltas.append(float(probabilities[selected]) - closing_probabilities[selected])
    if not deltas:
        return {
            "available": False,
            "label": "BENCHMARK / CLV MEASUREMENT ONLY",
            "reason": "no valid closing evidence is available",
            "count": 0,
        }
    return {
        "available": True,
        "label": "BENCHMARK / CLV MEASUREMENT ONLY",
        "count": len(deltas),
        "mean_signed_probability_delta": sum(deltas) / len(deltas),
        "mean_absolute_probability_delta": sum(abs(value) for value in deltas)
        / len(deltas),
    }


def _group_summary(
    records: Sequence[Mapping[str, object]], key: str
) -> dict[str, object]:
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        value = record.get(key)
        if isinstance(value, str) and value:
            groups[value].append(record)
    result: dict[str, object] = {}
    for name, values in sorted(groups.items()):
        summary = _metrics(values)
        summary["distinct_fixture_count"] = len({item["fixture"] for item in values})
        summary["closing_benchmark"] = _closing_summary(values)
        result[name] = summary
    return result


def _source_exclusions(
    measurement: Mapping[str, object], measurement_id: str
) -> list[dict[str, object]]:
    exclusions = []
    for item in _records(measurement.get("excluded_evidence", []), "excluded_evidence"):
        exclusions.append(
            {
                "measurement_id": measurement_id,
                "prediction_id": item.get("prediction_id"),
                "fixture": item.get("fixture"),
                "signal_time_experiment_id": item.get("signal_time_experiment_id"),
                "reason": item.get("reason", "MEASUREMENT_EXCLUDED"),
                "finding_codes": sorted(item.get("finding_codes", [])),
            }
        )
    return exclusions


def _validate_measurement(
    measurement: Mapping[str, object], source_index: int
) -> tuple[str, str, list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(measurement, Mapping):
        raise MatrixError(f"source {source_index} must be a JSON object")
    if measurement.get("measurement_schema") != MEASUREMENT_SCHEMA_VERSION:
        raise MatrixError(
            f"source {source_index} has an unsupported measurement schema"
        )
    measurement_id = measurement.get("measurement_id")
    measurement_digest = measurement.get("measurement_digest")
    if not isinstance(measurement_id, str) or not measurement_id:
        raise MatrixError(f"source {source_index} lacks measurement_id")
    if not isinstance(measurement_digest, str) or len(measurement_digest) != 64:
        raise MatrixError(f"source {source_index} lacks measurement_digest")
    expected = dict(measurement)
    expected.pop("measurement_digest", None)
    if _stable_digest(expected) != measurement_digest:
        raise MatrixError(f"source {source_index} measurement digest is invalid")
    if measurement.get("overall_state") != "COMPLETE":
        raise MatrixError(f"source {source_index} is not a complete measurement")
    cohort = _mapping(measurement.get("cohort_integrity")) or {}
    if cohort.get("state") != "VALID":
        raise MatrixError(f"source {source_index} cohort is not valid")
    safety = _mapping(measurement.get("safety_invariants")) or {}
    required_false = (
        "production_activation_authorized",
        "publication_authorized",
        "betting_authorized",
        "model_approved_for_production",
        "signal_time_approved_for_production",
        "network_accessed",
        "provider_ranking_emitted",
        "closing_used_for_prediction",
        "sealed_data_accessed",
    )
    if safety.get("read_only") is not True or any(
        safety.get(field) is not False for field in required_false
    ):
        raise MatrixError(f"source {source_index} carries unsafe authority")
    if measurement.get("research_sha") != FROZEN_RESEARCH_SHA:
        raise MatrixError(f"source {source_index} Research SHA is not frozen")
    if measurement.get("model_identity") != M5_CANDIDATE_ID:
        raise MatrixError(f"source {source_index} model identity is not M5")
    raw_records = _records(
        measurement.get("eligible_predictions"), "eligible_predictions"
    )
    if measurement.get("eligible_count") != len(raw_records):
        raise MatrixError(f"source {source_index} eligible count is inconsistent")
    records: list[dict[str, object]] = []
    prediction_ids: list[str] = []
    for raw in raw_records:
        required_text = (
            "prediction_id",
            "fixture",
            "league",
            "provider",
            "signal_time_experiment_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "prediction_artifact_sha",
            "result_attachment_sha",
        )
        if any(
            not isinstance(raw.get(field), str) or not raw[field]
            for field in required_text
        ):
            raise MatrixError(
                f"source {source_index} contains incomplete prediction provenance"
            )
        if raw.get("evidence_mode") != REAL_OBSERVED_MARKER:
            raise MatrixError(f"source {source_index} contains non-real evidence")
        if raw.get("research_sha") != FROZEN_RESEARCH_SHA:
            raise MatrixError(f"source {source_index} contains mixed Research SHA")
        if raw.get("model_identity") != M5_CANDIDATE_ID:
            raise MatrixError(f"source {source_index} contains mixed model identity")
        actual = raw.get("actual_outcome")
        if actual not in _OUTCOMES:
            raise MatrixError(f"source {source_index} contains an invalid result")
        probabilities = _probabilities(raw.get("probabilities"))
        record = dict(raw)
        record["probabilities"] = probabilities
        prediction_ids.append(str(raw["prediction_id"]))
        records.append(record)
    if len(set(prediction_ids)) != len(prediction_ids):
        raise MatrixError(f"source {source_index} contains duplicate prediction IDs")
    source_ids = measurement.get("eligible_prediction_ids")
    if source_ids != sorted(prediction_ids):
        raise MatrixError(f"source {source_index} prediction ID index is inconsistent")
    return (
        measurement_id,
        measurement_digest,
        records,
        _source_exclusions(measurement, measurement_id),
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap_interval(
    deltas: Mapping[str, Sequence[float]], seed_material: object
) -> dict[str, object]:
    if not deltas or any(len(values) < 2 for values in deltas.values()):
        return {
            "status": "UNAVAILABLE / INSUFFICIENT_PAIRED_SAMPLE",
            "method": "deterministic paired bootstrap percentile",
            "confidence_level": 0.95,
            "resamples": 0,
        }
    seed = int(_stable_digest(seed_material)[:16], 16)
    rng = random.Random(seed)
    size = len(next(iter(deltas.values())))
    samples: dict[str, list[float]] = {name: [] for name in deltas}
    for _ in range(_BOOTSTRAP_RESAMPLES):
        indices = [rng.randrange(size) for _ in range(size)]
        for name, values in deltas.items():
            samples[name].append(sum(values[index] for index in indices) / size)
    return {
        "status": "AVAILABLE",
        "method": "deterministic paired bootstrap percentile",
        "confidence_level": 0.95,
        "resamples": _BOOTSTRAP_RESAMPLES,
        "metrics": {
            name: {
                "lower": _percentile(values, 0.025),
                "upper": _percentile(values, 0.975),
            }
            for name, values in samples.items()
        },
    }


def _paired_comparisons(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_experiment: dict[str, dict[str, Mapping[str, object]]] = defaultdict(dict)
    for record in records:
        by_experiment[str(record["signal_time_experiment_id"])][
            str(record["fixture"])
        ] = record
    comparisons: list[dict[str, object]] = []
    for experiment_a, experiment_b in combinations(sorted(by_experiment), 2):
        shared = sorted(
            set(by_experiment[experiment_a]).intersection(by_experiment[experiment_b])
        )
        pairs = [
            (by_experiment[experiment_a][fixture], by_experiment[experiment_b][fixture])
            for fixture in shared
        ]
        brier_deltas = []
        log_loss_deltas = []
        accuracy_deltas = []
        for first, second in pairs:
            if first["actual_outcome"] != second["actual_outcome"]:
                raise MatrixError("paired fixture has conflicting result identities")
            first_probabilities = first["probabilities"]
            second_probabilities = second["probabilities"]
            actual = first["actual_outcome"]
            brier_deltas.append(
                sum(
                    (float(first_probabilities[name]) - float(name == actual)) ** 2
                    for name in _OUTCOMES
                )
                / len(_OUTCOMES)
                - sum(
                    (float(second_probabilities[name]) - float(name == actual)) ** 2
                    for name in _OUTCOMES
                )
                / len(_OUTCOMES)
            )
            log_loss_deltas.append(
                -log(max(float(first_probabilities[actual]), 1e-15))
                + log(max(float(second_probabilities[actual]), 1e-15))
            )
            accuracy_deltas.append(
                float(_prediction_class(first_probabilities) == actual)
                - float(_prediction_class(second_probabilities) == actual)
            )
        deltas = {
            "brier_delta": brier_deltas,
            "log_loss_delta": log_loss_deltas,
            "accuracy_delta": accuracy_deltas,
        }
        comparison: dict[str, object] = {
            "experiment_A": experiment_a,
            "experiment_B": experiment_b,
            "direction": "experiment_A - experiment_B",
            "paired_fixture_count": len(pairs),
            "mean_paired_brier_delta": (
                sum(brier_deltas) / len(brier_deltas) if pairs else None
            ),
            "mean_paired_log_loss_delta": (
                sum(log_loss_deltas) / len(log_loss_deltas) if pairs else None
            ),
            "mean_paired_accuracy_delta": (
                sum(accuracy_deltas) / len(accuracy_deltas) if pairs else None
            ),
            "confidence_interval": _bootstrap_interval(
                deltas,
                {
                    "experiment_A": experiment_a,
                    "experiment_B": experiment_b,
                    "fixtures": shared,
                },
            ),
        }
        comparisons.append(comparison)
    return comparisons


def build_experiment_matrix(
    measurements: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build a neutral experiment matrix from immutable Measurement Pack outputs."""

    if not isinstance(measurements, Sequence) or isinstance(measurements, (str, bytes)):
        raise MatrixError("measurements must be a sequence")
    source_records: list[dict[str, object]] = []
    source_summaries: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    hard_findings: list[str] = []
    for index, measurement in enumerate(measurements):
        try:
            measurement_id, digest, records, source_excluded = _validate_measurement(
                measurement, index
            )
        except MatrixError as exc:
            hard_findings.append("MALFORMED_MEASUREMENT")
            exclusions.append(
                {
                    "source_index": index,
                    "reason": "MALFORMED_MEASUREMENT",
                    "detail": str(exc),
                }
            )
            continue
        source_summaries.append(
            {
                "measurement_id": measurement_id,
                "measurement_digest": digest,
                "eligible_count": len(records),
            }
        )
        exclusions.extend(source_excluded)
        for record in records:
            source_records.append(
                {
                    **record,
                    "source_measurement_id": measurement_id,
                    "source_measurement_digest": digest,
                }
            )
    source_summaries.sort(key=lambda item: str(item["measurement_id"]))
    if not source_summaries:
        hard_findings.append("NO_VALID_MEASUREMENT_INPUT")
    by_prediction: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in source_records:
        by_prediction[str(record["prediction_id"])].append(record)
    for prediction_id, values in sorted(by_prediction.items()):
        if len(values) > 1:
            fingerprints = {
                (item["prediction_artifact_sha"], item["fixture"]) for item in values
            }
            hard_findings.append(
                "DIVERGENT_PREDICTION_ARTIFACT"
                if len(fingerprints) > 1
                else "DUPLICATE_PREDICTION_ID"
            )
            exclusions.extend(
                {
                    "prediction_id": prediction_id,
                    "fixture": item["fixture"],
                    "signal_time_experiment_id": item["signal_time_experiment_id"],
                    "reason": "DUPLICATE_PREDICTION_ID",
                    "finding_codes": ["CONFLICT"],
                }
                for item in values
            )
    by_experiment_fixture: dict[tuple[str, str], list[Mapping[str, object]]] = (
        defaultdict(list)
    )
    by_fixture: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    identities = set()
    for record in source_records:
        experiment = str(record["signal_time_experiment_id"])
        fixture = str(record["fixture"])
        by_experiment_fixture[(experiment, fixture)].append(record)
        by_fixture[fixture].append(record)
        identities.add((record["research_sha"], record["model_identity"]))
    for values in by_experiment_fixture.values():
        if len(values) > 1:
            hard_findings.append("DUPLICATE_FIXTURE_WITHIN_EXPERIMENT")
    if len(identities) > 1:
        hard_findings.append("MIXED_RESEARCH_OR_MODEL_IDENTITY")
    for fixture, values in by_fixture.items():
        if len({item["actual_outcome"] for item in values}) > 1:
            hard_findings.append("CONFLICTING_RESULT_IDENTITY")
    hard_findings = sorted(set(hard_findings))
    if hard_findings:
        exclusions.extend(
            {
                "prediction_id": record.get("prediction_id"),
                "fixture": record.get("fixture"),
                "signal_time_experiment_id": record.get("signal_time_experiment_id"),
                "reason": "MATRIX_FAILED_CLOSED",
                "finding_codes": hard_findings,
            }
            for record in source_records
        )
        source_records = []
    source_records.sort(key=lambda item: str(item["prediction_id"]))
    exclusions.sort(
        key=lambda item: (
            str(item.get("prediction_id", "")),
            str(item.get("reason", "")),
        )
    )
    experiment_ids = sorted(
        {str(item["signal_time_experiment_id"]) for item in source_records}
    )
    experiment_summaries: dict[str, object] = {}
    for experiment_id in experiment_ids:
        values = [
            item
            for item in source_records
            if item["signal_time_experiment_id"] == experiment_id
        ]
        summary = _metrics(values)
        summary["experiment_id"] = experiment_id
        summary["distinct_fixture_count"] = len({item["fixture"] for item in values})
        summary["closing_benchmark"] = _closing_summary(values)
        relevant_exclusions = [
            item
            for item in exclusions
            if item.get("signal_time_experiment_id") == experiment_id
        ]
        summary["excluded_count"] = len(relevant_exclusions)
        summary["exclusion_taxonomy"] = dict(
            sorted(Counter(item["reason"] for item in relevant_exclusions).items())
        )
        experiment_summaries[experiment_id] = summary
    source_digest = _stable_digest(source_summaries)
    matrix_basis = [
        {
            "prediction_id": item["prediction_id"],
            "fixture": item["fixture"],
            "experiment": item["signal_time_experiment_id"],
            "artifact": item["prediction_artifact_sha"],
            "result": item["result_attachment_sha"],
            "probabilities": item["probabilities"],
            "actual_outcome": item["actual_outcome"],
        }
        for item in source_records
    ]
    matrix_basis_digest = _stable_digest(matrix_basis)
    matrix_id = f"top5-experiment-matrix:{_stable_digest({'sources': source_digest, 'basis': matrix_basis_digest})[:32]}"
    report: dict[str, object] = {
        "matrix_schema": MATRIX_SCHEMA_VERSION,
        "matrix_id": matrix_id,
        "source_measurements": source_summaries,
        "source_measurement_digest": source_digest,
        "experiment_ids": experiment_ids,
        "eligible_count": len(source_records),
        "unique_fixture_count": len({item["fixture"] for item in source_records}),
        "unique_prediction_count": len(
            {item["prediction_id"] for item in source_records}
        ),
        "experiment_summaries": experiment_summaries,
        "paired_comparisons": (
            [] if hard_findings else _paired_comparisons(source_records)
        ),
        "breakdowns": {
            "league": _group_summary(source_records, "league"),
            "provider": _group_summary(source_records, "provider"),
            "controlled_shadow_run_id": _group_summary(
                source_records, "controlled_shadow_run_id"
            ),
            "qualification_session_id": _group_summary(
                source_records, "qualification_session_id"
            ),
        },
        "exclusions": exclusions,
        "exclusion_taxonomy": dict(
            sorted(Counter(item["reason"] for item in exclusions).items())
        ),
        "closing_benchmark": _closing_summary(source_records),
        "provenance": {
            "research_sha": FROZEN_RESEARCH_SHA,
            "model_identity": M5_CANDIDATE_ID,
            "source_measurement_count": len(source_summaries),
            "deterministic_order": "experiment_id, fixture, prediction_id",
        },
        "unresolved_evidence_limitations": [
            "descriptive evidence only; no experiment or provider decision is emitted",
            "paired confidence intervals are unavailable below two shared fixtures",
            "closing values remain benchmark-only and never affect admission",
        ],
        "production_activation_authorized": False,
        "publication_authorized": False,
        "betting_authorized": False,
        "model_approved_for_production": False,
        "signal_time_approved_for_production": False,
        "experiment_winner_selected": False,
        "provider_winner_selected": False,
        "cohort_integrity": {
            "state": "FAILED_CLOSED" if hard_findings else "VALID",
            "findings": hard_findings,
        },
        "overall_state": "FAILED_CLOSED" if hard_findings else "COMPLETE",
    }
    report["matrix_digest"] = _stable_digest(report)
    return report


def render_markdown(report: Mapping[str, object]) -> str:
    """Render a concise, non-authoritative matrix summary."""

    lines = [
        "Top-5 Shadow Experiment Evidence Matrix V1",
        "READ ONLY | NO NETWORK | NO BET | NO PUBLICATION | NO PRODUCTION ACTIVATION",
        f"Overall state: {report.get('overall_state')}",
        f"Experiments: {len(report.get('experiment_ids', []))} | eligible: {report.get('eligible_count', 0)}",
        f"Paired comparisons: {len(report.get('paired_comparisons', []))}",
        "No experiment, provider, Signal-Time, or production decision authority.",
    ]
    return "\n".join(lines)


__all__ = [
    "MATRIX_SCHEMA_VERSION",
    "MatrixError",
    "build_experiment_matrix",
    "render_markdown",
]
