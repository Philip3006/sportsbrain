"""Fail-closed compatibility contract for public Top-5 signal lifecycles.

This module is a read-model adapter only. It does not authorize activation or
publication and deliberately excludes provider, account, and betting authority
from lifecycle provenance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from math import isfinite

TOP5_LIFECYCLE_SCHEMA_VERSION = "top5-lifecycle-public-v1"
TOP5_LIFECYCLE_FIELDS = frozenset(
    {
        "schema_version",
        "lifecycle_id",
        "initial_record_id",
        "lifecycle_version",
        "lifecycle_stage",
        "initial_generated_at",
        "current_generated_at",
        "updated_at",
        "initial_probability",
        "current_probability",
        "probability_delta",
        "initial_market_probability",
        "current_market_probability",
        "initial_edge_pp",
        "current_edge_pp",
        "edge_delta_pp",
        "refinement_classification",
        "provenance_binding",
        "model_identity",
        "fixture_identity",
    }
)
TOP5_LIFECYCLE_PROVENANCE_FIELDS = frozenset(
    {
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "evidence_digest",
        "snapshot_id",
    }
)
_STAGES = frozenset({"INITIAL", "REFINED", "WITHDRAWN"})
_CLASSIFICATIONS = frozenset({"STRENGTHENED", "WEAKENED", "UNCHANGED", "WITHDRAWN"})
_NUMERIC_FIELDS = frozenset(
    {
        "initial_probability",
        "current_probability",
        "probability_delta",
        "initial_market_probability",
        "current_market_probability",
        "initial_edge_pp",
        "current_edge_pp",
        "edge_delta_pp",
    }
)
_PROBABILITY_FIELDS = frozenset(
    {
        "initial_probability",
        "current_probability",
        "initial_market_probability",
        "current_market_probability",
    }
)


class Top5LifecyclePublicError(ValueError):
    """Malformed, inconsistent, or unsafe public lifecycle metadata."""


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Top5LifecyclePublicError(f"Top-5 lifecycle {field} is required")
    return value.strip()


def _timestamp(value: object, field: str) -> tuple[str, datetime]:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Top5LifecyclePublicError(
            f"Top-5 lifecycle {field} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5LifecyclePublicError(
            f"Top-5 lifecycle {field} must include a timezone"
        )
    return text, parsed


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Top5LifecyclePublicError(f"Top-5 lifecycle {field} must be numeric")
    number = float(value)
    if not isfinite(number):
        raise Top5LifecyclePublicError(f"Top-5 lifecycle {field} must be finite")
    if field in _PROBABILITY_FIELDS and not 0 <= number <= 1:
        raise Top5LifecyclePublicError(
            f"Top-5 lifecycle {field} must be between zero and one"
        )
    return number


def project_top5_lifecycle(
    value: object,
    *,
    fixture_identity: object,
    model_identity: object,
    provenance: Mapping[str, object],
) -> dict[str, object]:
    """Validate and project a lifecycle object using explicit public fields.

    The emitted contract is per market/outcome signal. Missing optional history
    stays missing; this function never derives or fabricates a probability,
    market probability, edge, delta, or timestamp.
    """
    if not isinstance(value, Mapping):
        raise Top5LifecyclePublicError("Top-5 lifecycle must be an object")

    if value.get("provider_authority") is not None or value.get("provider") is not None:
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle cannot declare provider authority"
        )

    if value.get("schema_version") != TOP5_LIFECYCLE_SCHEMA_VERSION:
        raise Top5LifecyclePublicError("unsupported Top-5 lifecycle schema_version")

    lifecycle_id = _required_text(value.get("lifecycle_id"), "lifecycle_id")
    initial_record_id = _required_text(
        value.get("initial_record_id"), "initial_record_id"
    )
    version = value.get("lifecycle_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle lifecycle_version must be a positive integer"
        )
    stage = _required_text(value.get("lifecycle_stage"), "lifecycle_stage").upper()
    if stage not in _STAGES:
        raise Top5LifecyclePublicError("unsupported Top-5 lifecycle_stage")
    if (stage == "INITIAL") != (version == 1):
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle INITIAL must be version 1 and later stages must be version 2 or greater"
        )

    initial_text, initial_time = _timestamp(
        value.get("initial_generated_at"), "initial_generated_at"
    )
    current_value = value.get("current_generated_at")
    updated_value = value.get("updated_at")
    if current_value is not None and updated_value is not None:
        current_canonical, _ = _timestamp(current_value, "current_generated_at")
        updated_canonical, _ = _timestamp(updated_value, "updated_at")
        if current_canonical != updated_canonical:
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle current_generated_at and updated_at disagree"
            )
    current_text, current_time = _timestamp(
        current_value if current_value is not None else updated_value,
        "current_generated_at",
    )
    if current_time < initial_time:
        raise Top5LifecyclePublicError("Top-5 lifecycle timestamps move backwards")
    if stage == "INITIAL" and current_time != initial_time:
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle INITIAL timestamps must identify the same capture"
        )

    expected_fixture = _required_text(fixture_identity, "fixture identity")
    expected_model = _required_text(model_identity, "model identity")
    fixture = _required_text(value.get("fixture_identity"), "fixture_identity")
    model = _required_text(value.get("model_identity"), "model_identity")
    if fixture != expected_fixture or model != expected_model:
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle fixture/model identity binding mismatch"
        )

    raw_binding = value.get("provenance_binding")
    if not isinstance(raw_binding, Mapping) or not raw_binding:
        raise Top5LifecyclePublicError("Top-5 lifecycle provenance_binding is required")
    binding: dict[str, str] = {}
    for key, raw in raw_binding.items():
        if key in {"provider", "provider_authority"}:
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle cannot bind provider authority"
            )
        if key not in TOP5_LIFECYCLE_PROVENANCE_FIELDS:
            continue
        text = _required_text(raw, f"provenance_binding.{key}")
        expected = provenance.get(key)
        if not isinstance(expected, str) or not expected or expected != text:
            raise Top5LifecyclePublicError(
                f"Top-5 lifecycle provenance binding mismatch: {key}"
            )
        binding[key] = text
    if not binding:
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle provenance_binding has no public provenance fields"
        )

    result: dict[str, object] = {
        "schema_version": TOP5_LIFECYCLE_SCHEMA_VERSION,
        "lifecycle_id": lifecycle_id,
        "initial_record_id": initial_record_id,
        "lifecycle_version": version,
        "lifecycle_stage": stage,
        "initial_generated_at": initial_text,
        "current_generated_at": current_text,
        "provenance_binding": binding,
        "model_identity": model,
        "fixture_identity": fixture,
    }
    numbers: dict[str, float] = {}
    for field in sorted(_NUMERIC_FIELDS):
        if field in value and value[field] is not None:
            numbers[field] = _number(value[field], field)

    if "probability_delta" in numbers:
        if not {"initial_probability", "current_probability"}.issubset(numbers):
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle probability_delta requires initial and current probability"
            )
        expected_delta = numbers["current_probability"] - numbers["initial_probability"]
        if abs(numbers["probability_delta"] - expected_delta) > 1e-8:
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle probability_delta is inconsistent"
            )
    if "edge_delta_pp" in numbers:
        if not {"initial_edge_pp", "current_edge_pp"}.issubset(numbers):
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle edge_delta_pp requires initial and current edge"
            )
        expected_delta = numbers["current_edge_pp"] - numbers["initial_edge_pp"]
        if abs(numbers["edge_delta_pp"] - expected_delta) > 1e-8:
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle edge_delta_pp is inconsistent"
            )
    for edge_field, probability_field, market_field in (
        ("initial_edge_pp", "initial_probability", "initial_market_probability"),
        ("current_edge_pp", "current_probability", "current_market_probability"),
    ):
        if edge_field in numbers:
            if probability_field not in numbers or market_field not in numbers:
                raise Top5LifecyclePublicError(
                    f"Top-5 lifecycle {edge_field} requires model and market probability"
                )
            expected_edge = (numbers[probability_field] - numbers[market_field]) * 100
            if abs(numbers[edge_field] - expected_edge) > 0.01:
                raise Top5LifecyclePublicError(
                    f"Top-5 lifecycle {edge_field} is inconsistent"
                )
    if "refinement_classification" in value:
        classification = _required_text(
            value["refinement_classification"], "refinement_classification"
        ).upper()
        if classification not in _CLASSIFICATIONS:
            raise Top5LifecyclePublicError(
                "unsupported Top-5 lifecycle refinement_classification"
            )
        if (stage == "WITHDRAWN") != (classification == "WITHDRAWN"):
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle stage/classification mismatch"
            )
        result["refinement_classification"] = classification
    elif stage == "WITHDRAWN":
        raise Top5LifecyclePublicError(
            "Top-5 lifecycle WITHDRAWN requires WITHDRAWN classification"
        )
    result.update(numbers)
    return {
        key: item
        for key, item in result.items()
        if key in TOP5_LIFECYCLE_FIELDS and key != "updated_at"
    }


def collapse_top5_lifecycle_versions(
    records: Sequence[object],
) -> list[object]:
    """Collapse consistent versions of one signal and reject forked histories."""
    chains: dict[str, list[tuple[int, int, Mapping[str, object]]]] = {}
    initial_ids: dict[str, str] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or "lifecycle" not in record:
            continue
        lifecycle = record["lifecycle"]
        if not isinstance(lifecycle, Mapping):
            raise Top5LifecyclePublicError("Top-5 lifecycle must be an object")
        lifecycle_id = _required_text(lifecycle.get("lifecycle_id"), "lifecycle_id")
        initial_id = _required_text(
            lifecycle.get("initial_record_id"), "initial_record_id"
        )
        prior_id = initial_ids.setdefault(initial_id, lifecycle_id)
        if prior_id != lifecycle_id:
            raise Top5LifecyclePublicError(
                "Top-5 lifecycle ID changed within one initial record chain"
            )
        version = lifecycle.get("lifecycle_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise Top5LifecyclePublicError("invalid Top-5 lifecycle version")
        chains.setdefault(lifecycle_id, []).append((version, index, record))

    removed: set[int] = set()
    for lifecycle_id, versions in chains.items():
        if len(versions) < 2:
            continue
        versions.sort(key=lambda item: item[0])
        if len({version for version, _, _ in versions}) != len(versions):
            raise Top5LifecyclePublicError(
                f"duplicate Top-5 lifecycle version for {lifecycle_id}"
            )
        previous: Mapping[str, object] | None = None
        previous_time: datetime | None = None
        previous_stage = ""
        initial_id = ""
        for position, (version, index, record) in enumerate(versions):
            lifecycle = record["lifecycle"]
            assert isinstance(lifecycle, Mapping)
            stage = str(lifecycle.get("lifecycle_stage", "")).upper()
            if position == 0:
                if version != 1 or stage != "INITIAL":
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle chain {lifecycle_id} lacks version-1 INITIAL"
                    )
                initial_id = str(lifecycle.get("initial_record_id", ""))
            else:
                assert previous is not None
                prior_lifecycle = previous["lifecycle"]
                assert isinstance(prior_lifecycle, Mapping)
                if version <= int(prior_lifecycle["lifecycle_version"]):
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle version regressed for {lifecycle_id}"
                    )
                if lifecycle.get("initial_record_id") != initial_id:
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle initial identity changed for {lifecycle_id}"
                    )
                if lifecycle.get("initial_generated_at") != previous["lifecycle"].get(
                    "initial_generated_at"
                ):
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle initial timestamp changed for {lifecycle_id}"
                    )
                for field in (
                    "initial_probability",
                    "initial_market_probability",
                    "initial_edge_pp",
                ):
                    if lifecycle.get(field) != previous["lifecycle"].get(field):
                        raise Top5LifecyclePublicError(
                            f"Top-5 lifecycle initial history changed for {lifecycle_id}"
                        )
                for key in ("fixture_key", "league", "market", "model_identity"):
                    if record.get(key) != previous.get(key):
                        raise Top5LifecyclePublicError(
                            f"Top-5 lifecycle {key} changed for {lifecycle_id}"
                        )
                previous_binding = previous["lifecycle"].get("provenance_binding")
                current_binding = lifecycle.get("provenance_binding")
                if not isinstance(previous_binding, Mapping) or not isinstance(
                    current_binding, Mapping
                ):
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle provenance is malformed for {lifecycle_id}"
                    )
                previous_immutable_binding = {
                    key: value
                    for key, value in previous_binding.items()
                    if key != "snapshot_id"
                }
                current_immutable_binding = {
                    key: value
                    for key, value in current_binding.items()
                    if key != "snapshot_id"
                }
                if previous_immutable_binding != current_immutable_binding or (
                    "snapshot_id" in previous_binding
                ) != ("snapshot_id" in current_binding):
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle provenance changed for {lifecycle_id}"
                    )
                if previous_stage == "WITHDRAWN":
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle {lifecycle_id} continued after withdrawal"
                    )
                if stage == "INITIAL":
                    raise Top5LifecyclePublicError(
                        f"Top-5 lifecycle {lifecycle_id} regressed to INITIAL"
                    )
            _, current_time = _timestamp(
                lifecycle.get("current_generated_at"), "current_generated_at"
            )
            if previous_time is not None and current_time < previous_time:
                raise Top5LifecyclePublicError(
                    f"Top-5 lifecycle timestamps move backwards for {lifecycle_id}"
                )
            previous = record
            previous_time = current_time
            previous_stage = stage
            if position < len(versions) - 1:
                removed.add(index)

    return [record for index, record in enumerate(records) if index not in removed]
