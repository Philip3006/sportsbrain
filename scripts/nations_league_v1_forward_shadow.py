#!/usr/bin/env python3
"""Manual, offline forward-shadow runner for the frozen Nations League v1 model.

This CLI consumes caller-supplied canonical fixture/training JSON only.  It has
no provider, credential, publication, betting, or ledger integration.  The
store is JSONL so prediction and settlement events are physically appended;
original prediction lines are never rewritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_v1 import (
    append_shadow_record,
    append_shadow_settlement,
    build_forward_shadow_prediction,
    build_shadow_settlement,
    calculate_forward_metrics,
)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(value, dict):
                raise TypeError(f"JSONL line {line_number} is not an object")
            records.append(value)
    return records


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _fixture(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("fixture"), dict):
        value = value["fixture"]
    if not isinstance(value, dict):
        raise TypeError("fixture input must be an object")
    return value


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("records", value.get("matches"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("training input must be a list of records")
    return value


def _predict(args: argparse.Namespace) -> int:
    fixture = _fixture(_read_json(args.fixture))
    training = _records(_read_json(args.training))
    provenance = _read_json(args.input_provenance)
    record = build_forward_shadow_prediction(
        fixture,
        phase=args.phase,
        prediction_timestamp=args.prediction_timestamp,
        training_records=training,
        input_provenance=provenance,
    )
    existing = _read_records(args.store)
    prediction_records = [
        row for row in existing if row.get("record_type", "prediction") == "prediction"
    ]
    append_shadow_record(prediction_records, record)
    if any(row.get("record_id") == record["record_id"] for row in existing):
        raise ValueError("prediction record already exists")
    _append(args.store, record)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


def _settle(args: argparse.Namespace) -> int:
    records = _read_records(args.store)
    prediction = next(
        (
            row
            for row in records
            if row.get("record_type", "prediction") == "prediction"
            and row.get("record_id") == args.record_id
        ),
        None,
    )
    if prediction is None:
        raise ValueError("prediction record not found")
    settlement = build_shadow_settlement(
        prediction,
        home_score=args.home_score,
        away_score=args.away_score,
        result_safe_available_at=args.result_safe_available_at,
        settled_at=args.settled_at,
        result_provenance=args.result_provenance,
    )
    append_shadow_settlement(records, settlement)
    _append(args.store, settlement)
    print(json.dumps(settlement, ensure_ascii=False, sort_keys=True))
    return 0


def _metrics(args: argparse.Namespace) -> int:
    result = calculate_forward_metrics(_read_records(args.store))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    predict = commands.add_parser("predict", help="append one v1 prediction")
    predict.add_argument("--fixture", type=Path, required=True)
    predict.add_argument("--training", type=Path, required=True)
    predict.add_argument("--input-provenance", type=Path, required=True)
    predict.add_argument("--phase", choices=("initial", "refinement"), required=True)
    predict.add_argument("--prediction-timestamp", required=True)
    predict.add_argument("--store", type=Path, required=True)
    predict.set_defaults(handler=_predict)

    settle = commands.add_parser("settle", help="append one result settlement")
    settle.add_argument("--store", type=Path, required=True)
    settle.add_argument("--record-id", required=True)
    settle.add_argument("--home-score", type=int, required=True)
    settle.add_argument("--away-score", type=int, required=True)
    settle.add_argument("--result-safe-available-at", required=True)
    settle.add_argument("--settled-at", required=True)
    settle.add_argument("--result-provenance", required=True)
    settle.set_defaults(handler=_settle)

    metrics = commands.add_parser("metrics", help="calculate settled evidence")
    metrics.add_argument("--store", type=Path, required=True)
    metrics.set_defaults(handler=_metrics)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, TypeError, ValueError) as exc:
        print(f"forward shadow blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
