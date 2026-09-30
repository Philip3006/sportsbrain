"""Manual offline Nations League lifecycle queue; defaults to read-only planning."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.nations_league_v1 import (
    MODEL_VERSION,
    build_forward_shadow_prediction,
    deterministic_record_id,
    model_digest,
    sha256_json,
    validate_shadow_record,
    validate_target_fixture,
)


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be explicit UTC")
    return parsed


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def due_state(kickoff, as_of):
    lead = utc(kickoff) - utc(as_of)
    if lead <= timedelta(0):
        return "STARTED"
    if lead > timedelta(hours=26):
        return "TOO_EARLY"
    if lead >= timedelta(hours=22):
        return "INITIAL_DUE"
    if lead > timedelta(minutes=120):
        return "BETWEEN_WINDOWS"
    if lead >= timedelta(minutes=60):
        return "REFINEMENT_DUE"
    return "TOO_LATE"


def read_store(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def plan(manifest, as_of, store, existing=(), expected_model_digest=None):
    clock = utc(as_of)
    if expected_model_digest is not None and expected_model_digest != model_digest():
        raise ValueError("wrong model digest")
    if manifest.get("schema") != "nations-league-future-fixture-manifest-v1":
        raise ValueError("invalid manifest schema")
    if manifest.get("forward_shadow_model") != MODEL_VERSION:
        raise ValueError("wrong manifest model")
    if utc(manifest["observed_at_utc"]) > clock:
        raise ValueError("manifest observation is after as-of")
    if "manifest_digest" in manifest and manifest["manifest_digest"] != sha256_json(
        {k: v for k, v in manifest.items() if k != "manifest_digest"}
    ):
        raise ValueError("manifest digest mismatch")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise TypeError("manifest fixtures must be a list")
    captures = {}
    for record in existing:
        if record.get("record_type", "prediction") != "prediction":
            continue
        validate_shadow_record(record)
        if record.get("input_provenance_digest") != sha256_json(
            record.get("input_provenance")
        ):
            raise ValueError("conflicting capture provenance digest")
        if record.get("input_provenance", {}).get(
            "fixture_source_digest"
        ) != record.get("source_digest"):
            raise ValueError("conflicting capture provenance binding")
        key = (record["fixture_id"], record["phase"])
        if key in captures:
            raise ValueError("conflicting duplicate capture")
        if record["record_id"] != deterministic_record_id(*key):
            raise ValueError("conflicting capture identity")
        captures[key] = record
    rows, seen = [], set()
    for fixture in fixtures:
        validate_target_fixture(fixture)
        identity = fixture["fixture_id"]
        if identity in seen:
            raise ValueError("duplicate manifest fixture")
        seen.add(identity)
        if fixture.get("status") not in {"VERIFIED", "STARTED"}:
            raise ValueError("manifest fixture must have verified exact kickoff")
        if not fixture.get("source_provenance_records"):
            raise ValueError("manifest source provenance records required")
        state = due_state(fixture["kickoff_utc"], as_of)
        if fixture["status"] == "STARTED" and state != "STARTED":
            raise ValueError("started manifest fixture has future kickoff")
        windows = {}
        statuses = {}
        for phase, maximum, minimum in (
            ("initial", timedelta(hours=26), timedelta(hours=22)),
            ("refinement", timedelta(minutes=120), timedelta(minutes=60)),
        ):
            kickoff = utc(fixture["kickoff_utc"])
            start, end = kickoff - maximum, kickoff - minimum
            windows[phase] = {"start": stamp(start), "end": stamp(end)}
            record = captures.get((identity, phase))
            if record:
                for field in ("kickoff_utc", "home_team", "away_team", "source_digest"):
                    if record[field] != fixture[field]:
                        raise ValueError("conflicting capture fixture binding")
                statuses[phase] = "ALREADY_CAPTURED"
            else:
                statuses[phase] = (
                    "DUE"
                    if start <= clock <= end
                    else "MISSED"
                    if clock > end
                    else "PENDING"
                )
        lifecycle = {"INITIAL_DUE": "initial", "REFINEMENT_DUE": "refinement"}.get(
            state
        )
        rows.append(
            {
                "fixture_id": identity,
                "state": state,
                "lifecycle": lifecycle,
                "prediction_cutoff": stamp(clock) if lifecycle else None,
                "kickoff": fixture["kickoff_utc"],
                "frozen_model_digest": model_digest(),
                "required_input_provenance": {
                    "timeline_digest": "caller-supplied canonical timeline SHA-256",
                    "fixture_source_digest": fixture["source_digest"],
                    "training_cutoff": stamp(clock),
                },
                "destination_shadow_store": str(store),
                "windows": windows,
                "capture_status": statuses,
                "status": statuses[lifecycle] if lifecycle else state,
            }
        )
    rows.sort(key=lambda row: (row["kickoff"], row["fixture_id"]))
    summary = {}
    for phase in ("initial", "refinement"):
        upcoming = [
            row["windows"][phase]["start"]
            for row in rows
            if row["capture_status"][phase] == "PENDING"
        ]
        summary[f"NEXT {phase.upper()}"] = min(upcoming, default=None)
    for label, value in (
        ("DUE NOW", "DUE"),
        ("MISSED", "MISSED"),
        ("CAPTURED", "ALREADY_CAPTURED"),
    ):
        summary[label] = [
            f"{row['fixture_id']}:{phase}"
            for row in rows
            for phase, status in row["capture_status"].items()
            if status == value
        ]
    return {
        "as_of": stamp(clock),
        "mode": "PLAN_ONLY",
        "no_bet": True,
        "signal_status": "SHADOW_ONLY",
        "fixtures": rows,
        "summary": summary,
    }


def execute(manifest, as_of, store, training, provenance, expected_model_digest=None):
    # Manual execution serializes inspection and append under one OS lock.
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        existing = [json.loads(line) for line in handle if line.strip()]
        result = plan(manifest, as_of, store, existing, expected_model_digest)
        fixtures = {row["fixture_id"]: row for row in manifest["fixtures"]}
        pending = []
        for row in result["fixtures"]:
            if row["status"] != "DUE":
                continue
            identity = row["fixture_id"]
            inputs = provenance[identity]
            if inputs["fixture_source_digest"] != fixtures[identity]["source_digest"]:
                raise ValueError("input provenance fixture digest mismatch")
            if utc(inputs["training_cutoff"]) != utc(as_of):
                raise ValueError("training cutoff must match prediction cutoff")
            pending.append(
                build_forward_shadow_prediction(
                    fixtures[identity],
                    phase=row["lifecycle"],
                    prediction_timestamp=as_of,
                    training_records=training,
                    input_provenance=inputs,
                )
            )
        # Validate all due predictions before writing any prediction lines.
        handle.write("".join(json.dumps(row, sort_keys=True) + "\n" for row in pending))
        handle.flush()
        result["mode"] = "OFFLINE_EXECUTE"
        result["appended_record_ids"] = [row["record_id"] for row in pending]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--store", type=Path, default=Path("local-shadow/nations-league.jsonl")
    )
    parser.add_argument("--model-digest")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute-offline", action="store_true")
    parser.add_argument("--training", type=Path)
    parser.add_argument("--input-provenance", type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        if args.execute_offline:
            if not args.training or not args.input_provenance:
                raise ValueError(
                    "offline execution requires training and per-fixture provenance"
                )
            result = execute(
                manifest,
                args.as_of,
                args.store,
                json.loads(args.training.read_text()),
                json.loads(args.input_provenance.read_text()),
                args.model_digest,
            )
        else:
            result = plan(
                manifest,
                args.as_of,
                args.store,
                read_store(args.store),
                args.model_digest,
            )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(f"forward queue blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
