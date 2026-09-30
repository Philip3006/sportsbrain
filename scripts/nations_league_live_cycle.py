"""Run one deterministic Nations League LIVE lifecycle cycle.

The command is plan-only by default.  ``--execute-offline`` may materialize
provider-free predictions from a sealed #229 input-state, but it never calls a
provider, reads credentials, publishes, bets, or touches the ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_live_runtime import (
    NationsLeagueLiveRuntimeError,
    append_live_store,
    build_fresh_input_state,
    commit_active_registry,
    load_active_release,
    load_live_store,
    refresh_and_activate,
    run_live_cycle,
)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def run_cycle(
    *,
    manifest: Path,
    input_state: Path | None,
    registry: Path,
    as_of: str,
    store: Path,
    execute_offline: bool,
    base_timeline: Path | None = None,
    result_extension: Path | None = None,
    source_release_sha: str = "",
    input_state_output: Path | None = None,
    campaign: Path | None = None,
) -> dict:
    manifest_value = _read(manifest)
    registry_value = _read(registry)
    retrain = {"status": "NO_OP", "retrain_required": False}
    if (base_timeline is None) != (result_extension is None):
        raise ValueError("base timeline and result extension must be supplied together")
    if base_timeline is not None:
        state = build_fresh_input_state(
            manifest_value,
            _read(base_timeline),
            _read(result_extension),
            prediction_cutoff=as_of,
        )
        if input_state_output is not None:
            input_state_output.parent.mkdir(parents=True, exist_ok=True)
            input_state_output.write_text(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if execute_offline:
            if not source_release_sha:
                raise ValueError("source release SHA is required for live refresh")
            updated_registry, active, retrain = refresh_and_activate(
                registry_value,
                state,
                source_release_sha=source_release_sha,
                activated_at=as_of,
            )
            if retrain["status"] == "RETRAINED":
                commit_active_registry(registry, updated_registry)
        else:
            active = load_active_release(registry)
    else:
        if input_state is None:
            raise ValueError("input-state is required when fresh artifacts are absent")
        state = _read(input_state)
        active = load_active_release(registry)
    existing = load_live_store(store)
    idempotency_records = list(existing)
    if campaign is not None:
        campaign_records = _read(campaign).get("records")
        if not isinstance(campaign_records, list):
            raise TypeError("campaign records are missing")
        idempotency_records.extend(campaign_records)
    result = run_live_cycle(
        manifest_value,
        state,
        active,
        as_of=as_of,
        existing_records=idempotency_records,
        execute=execute_offline,
    )
    if execute_offline and result["appended_records"]:
        append_live_store(store, existing, result["appended_records"])
        result = {**result, "store_status": "APPENDED"}
    else:
        result = {**result, "store_status": "PLAN_ONLY"}
    final_status = result["status"]
    if retrain.get("status") == "RETRAINED":
        final_status = "RETRAINED_AND_MATERIALIZED" if result["appended_count"] else "RETRAINED"
    return {**result, "status": final_status, "retrain": retrain}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input-state", type=Path)
    parser.add_argument("--base-timeline", type=Path)
    parser.add_argument("--result-extension", type=Path)
    parser.add_argument("--source-release-sha")
    parser.add_argument("--input-state-output", type=Path)
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--execute-offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = run_cycle(
            manifest=args.manifest,
            input_state=args.input_state,
            registry=args.registry,
            as_of=args.as_of,
            store=args.store,
            execute_offline=args.execute_offline,
            base_timeline=args.base_timeline,
            result_extension=args.result_extension,
            source_release_sha=args.source_release_sha or "",
            input_state_output=args.input_state_output,
            campaign=args.campaign,
        )
    except (OSError, ValueError, KeyError, TypeError, NationsLeagueLiveRuntimeError) as exc:
        print(f"Nations League LIVE cycle blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
