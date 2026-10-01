"""Preview-only conversion of existing signal history into universal contracts."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.learning.adapters import AdapterError, prediction_from_record
from src.learning.outcome_contracts import LifecycleError


@dataclass(frozen=True)
class MigrationPreview:
    source_system: str
    path: str
    records_seen: int
    eligible_records: int
    convertible_records: int
    malformed_or_unconvertible: int
    already_represented: int
    duplicates: int
    conflicts: int
    reasons: dict[str, int]
    prediction_ids: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "sportsbrain-universal-migration-preview-v1",
            "source_system": self.source_system,
            "path": self.path,
            "records_seen": self.records_seen,
            "eligible_records": self.eligible_records,
            "convertible_records": self.convertible_records,
            "malformed_or_unconvertible": self.malformed_or_unconvertible,
            "already_represented": self.already_represented,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "reasons": dict(sorted(self.reasons.items())),
            "prediction_ids": list(self.prediction_ids),
        }


def preview_jsonl(
    path: Path,
    *,
    source_system: str,
    already_represented_ids: Iterable[str] = (),
) -> MigrationPreview:
    """Inspect a JSONL source without writing or changing any file."""

    represented = set(already_represented_ids)
    records_seen = 0
    eligible = 0
    convertible = 0
    malformed = 0
    already = 0
    duplicates = 0
    conflicts = 0
    reasons: Counter[str] = Counter()
    prediction_ids: list[str] = []
    by_source: dict[str, dict[str, Any]] = {}
    by_prediction: dict[str, dict[str, Any]] = {}

    if not path.exists():
        reasons["missing_file"] += 1
        return MigrationPreview(
            source_system=source_system,
            path=str(path),
            records_seen=0,
            eligible_records=0,
            convertible_records=0,
            malformed_or_unconvertible=0,
            already_represented=0,
            duplicates=0,
            conflicts=0,
            reasons=dict(reasons),
            prediction_ids=(),
        )

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        records_seen += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            reasons["invalid_json"] += 1
            continue
        if not isinstance(raw, dict):
            malformed += 1
            reasons["record_not_object"] += 1
            continue
        source_id = raw.get("source_record_id") or raw.get("record_id")
        if isinstance(source_id, str) and source_id:
            prior = by_source.get(source_id)
            if prior is not None:
                if prior != raw:
                    conflicts += 1
                    reasons["conflicting_source_record"] += 1
                else:
                    duplicates += 1
                    reasons["duplicate_source_record"] += 1
                continue
            by_source[source_id] = raw
        try:
            prediction = prediction_from_record(raw, source_system=source_system)
        except (AdapterError, LifecycleError, TypeError, ValueError) as exc:
            malformed += 1
            reasons[type(exc).__name__ + ":" + str(exc)] += 1
            continue
        prior_prediction = by_prediction.get(prediction.prediction_id)
        if prior_prediction is not None:
            if prior_prediction != raw:
                conflicts += 1
                reasons["conflicting_prediction_identity"] += 1
            else:
                duplicates += 1
                reasons["duplicate_prediction_identity"] += 1
            continue
        by_prediction[prediction.prediction_id] = raw
        eligible += 1
        if prediction.prediction_id in represented:
            already += 1
            reasons["already_represented"] += 1
            continue
        convertible += 1
        prediction_ids.append(prediction.prediction_id)

    return MigrationPreview(
        source_system=source_system,
        path=str(path),
        records_seen=records_seen,
        eligible_records=eligible,
        convertible_records=convertible,
        malformed_or_unconvertible=malformed,
        already_represented=already,
        duplicates=duplicates,
        conflicts=conflicts,
        reasons=dict(reasons),
        prediction_ids=tuple(sorted(prediction_ids)),
    )


def preview_json_document(
    path: Path,
    *,
    source_system: str,
    already_represented_ids: Iterable[str] = (),
) -> MigrationPreview:
    """Describe a JSON document source without pretending it is JSONL history."""

    if not path.exists():
        return preview_jsonl(
            path,
            source_system=source_system,
            already_represented_ids=already_represented_ids,
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return MigrationPreview(
            source_system=source_system,
            path=str(path),
            records_seen=0,
            eligible_records=0,
            convertible_records=0,
            malformed_or_unconvertible=1,
            already_represented=0,
            duplicates=0,
            conflicts=0,
            reasons={"invalid_json_document": 1, "error": str(exc)},
            prediction_ids=(),
        )
    records = document.get("signals", []) if isinstance(document, dict) else document
    if not isinstance(records, list):
        records = [records]
    if not records:
        return MigrationPreview(
            source_system=source_system,
            path=str(path),
            records_seen=0,
            eligible_records=0,
            convertible_records=0,
            malformed_or_unconvertible=0,
            already_represented=0,
            duplicates=0,
            conflicts=0,
            reasons={"no_historical_records": 1},
            prediction_ids=(),
        )
    return MigrationPreview(
        source_system=source_system,
        path=str(path),
        records_seen=len(records),
        eligible_records=0,
        convertible_records=0,
        malformed_or_unconvertible=len(records),
        already_represented=0,
        duplicates=0,
        conflicts=0,
        reasons={"json_document_requires_explicit_adapter": len(records)},
        prediction_ids=(),
    )


def preview_sources(
    sources: Iterable[tuple[str, Path]],
    *,
    already_represented_ids: Iterable[str] = (),
) -> tuple[MigrationPreview, ...]:
    """Return independent previews in deterministic source/path order."""

    return tuple(
        preview_jsonl(
            path,
            source_system=source_system,
            already_represented_ids=already_represented_ids,
        )
        for source_system, path in sorted(
            sources, key=lambda item: (item[0], str(item[1]))
        )
    )
