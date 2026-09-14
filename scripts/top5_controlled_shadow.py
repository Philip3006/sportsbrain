#!/usr/bin/env python3
"""Run one explicitly acknowledged, no-bet Top-5 real shadow cycle."""
from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.football.top5_real_shadow import (
    RealShadowExecutionError,
    RealShadowQuotaError,
    ShadowExperiment,
    run_controlled_shadow_cycle,
    write_shadow_archive,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled Top-5 real shadow; never places or publishes bets")
    parser.add_argument("--env-file", type=Path, default=Path.home() / "sportsbrain" / ".env")
    parser.add_argument("--quota-remaining", type=int, required=True)
    parser.add_argument("--safety-reserve", type=int, default=10)
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--min-lead-minutes", type=int, required=True)
    parser.add_argument("--max-lead-minutes", type=int, required=True)
    parser.add_argument("--max-odds-age-seconds", type=int, required=True)
    parser.add_argument("--ack-no-bet", action="store_true")
    args = parser.parse_args()
    if not args.ack_no_bet:
        parser.error("--ack-no-bet is required for a controlled shadow run")
    try:
        api_key = _read_protected_api_key(args.env_file)
        result = run_controlled_shadow_cycle(
            api_key=api_key,
            timing=ShadowExperiment(
                experiment_id=args.experiment_id,
                minimum_lead_minutes=args.min_lead_minutes,
                maximum_lead_minutes=args.max_lead_minutes,
                maximum_odds_age_seconds=args.max_odds_age_seconds,
            ),
            quota_remaining=args.quota_remaining,
            safety_reserve=args.safety_reserve,
            integration_sha=args.integration_sha,
        )
        archive = write_shadow_archive(result)
        payload = result.as_payload()
        payload["archive_path"] = str(archive)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (RealShadowQuotaError, RealShadowExecutionError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1


def _read_protected_api_key(path: Path) -> str:
    path = path.expanduser()
    if not path.is_absolute():
        raise RealShadowExecutionError("credential file must be an absolute path")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RealShadowExecutionError("credential file permissions are too broad")
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "ODDS_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    raise RealShadowExecutionError("ODDS_API_KEY is missing")


if __name__ == "__main__":
    raise SystemExit(main())
