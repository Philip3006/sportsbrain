"""Offline universal settlement and migration-preview command.

The command never selects a provider and never performs network or ledger
work.  It only consumes explicit JSON/JSONL evidence files.  Without
``--write`` it is read-only, including when a store path is supplied.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Running a script by path places ``scripts/`` on sys.path, not the project
# root.  Resolve the local package explicitly; this does not import or contact
# any provider.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.learning.adapters import prediction_from_record
from src.learning.migration import preview_sources
from src.learning.outcome_contracts import (
    AuthoritativeResultV1,
    JsonlOutcomeStore,
    LifecycleError,
)
from src.learning.prediction_store import (
    JsonlPredictionEvidenceStore,
    capture_predictions,
)
from src.learning.settlement import settle_predictions

DEFAULT_SOURCES = (
    ("generic_football", Path("data/cache/signal_history.jsonl")),
    (
        "nations_league",
        Path("results/research/nations_league_v1_1_live_prediction_store.jsonl"),
    ),
    ("tennis", Path("results/tennis_live_signals.json")),
    ("bundesliga2", Path("data/cache/bl2_signal_history.jsonl")),
    ("top5", Path("docs/data/top5/signals.jsonl")),
)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise LifecycleError(f"{path} contains a non-object record")
            records.append(raw)
    return records


def _source_arg(value: str) -> tuple[str, Path]:
    source, separator, raw_path = value.partition("=")
    if not separator or not source or not raw_path:
        raise argparse.ArgumentTypeError("expected SOURCE=PATH")
    return source, Path(raw_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prediction-source",
        action="append",
        type=_source_arg,
        metavar="SOURCE=PATH",
        help="explicit prediction JSONL source; repeatable",
    )
    parser.add_argument(
        "--result-jsonl",
        action="append",
        type=Path,
        help="canonical authoritative-result JSONL; repeatable",
    )
    parser.add_argument("--store", type=Path, help="explicit append-only store path")
    parser.add_argument(
        "--prediction-store",
        type=Path,
        help="explicit append-only prediction evidence path",
    )
    parser.add_argument(
        "--settled-at",
        help="UTC settlement timestamp for an offline settlement pass",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="explicitly append validated results/attachments to --store",
    )
    parser.add_argument(
        "--migrate-preview",
        action="store_true",
        help="preview conversions only; this is the default without inputs",
    )
    return parser


def _migration_output() -> dict[str, Any]:
    existing = tuple(item for item in DEFAULT_SOURCES if item[1].exists())
    return {
        "mode": "migration_preview",
        "read_only": True,
        "sources": [item.to_payload() for item in preview_sources(existing)],
        "unavailable_sources": [
            str(path) for _, path in DEFAULT_SOURCES if not path.exists()
        ],
    }


def _settlement_output(args: argparse.Namespace) -> dict[str, Any]:
    if not args.prediction_source or not args.result_jsonl or not args.settled_at:
        raise LifecycleError(
            "settlement requires --prediction-source, --result-jsonl, and --settled-at"
        )
    if args.write and args.store is None:
        raise LifecycleError("--write requires an explicit --store")
    if args.write and args.prediction_store is None:
        raise LifecycleError("--write requires an explicit --prediction-store")
    predictions = []
    for source_system, path in args.prediction_source:
        for raw in _jsonl(path):
            predictions.append(prediction_from_record(raw, source_system=source_system))
    results = []
    for path in args.result_jsonl:
        for raw in _jsonl(path):
            results.append(AuthoritativeResultV1.from_payload(raw))
    store = JsonlOutcomeStore(args.store) if args.store else None
    prediction_store = (
        JsonlPredictionEvidenceStore(args.prediction_store)
        if args.prediction_store
        else None
    )
    if prediction_store is not None:
        capture_predictions(predictions, store=prediction_store, write=args.write)
    report = settle_predictions(
        predictions,
        results,
        settled_at=args.settled_at,
        store=store,
        write=args.write,
    )
    return {
        "mode": "settlement",
        "read_only": not args.write,
        "report": report.to_payload(),
        "provider_requests": 0,
        "ledger_mutations": 0,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        if not args.prediction_source and not args.result_jsonl:
            output = _migration_output()
        else:
            output = _settlement_output(args)
    except (OSError, LifecycleError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
