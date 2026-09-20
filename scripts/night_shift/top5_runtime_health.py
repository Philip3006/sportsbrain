#!/usr/bin/env python3
"""Print the bounded, offline-only Top-5 runtime health decision.

The command is intentionally read-only.  It reports a deterministic JSON
decision and exits zero only for ``TOP5_RUNTIME_HEALTH_OFFLINE_READY``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from monitoring.top5.runtime_health import (  # noqa: E402
    BLOCKED_STATUS,
    READY_STATUS,
    Top5RuntimeEvidence,
    assess_top5_runtime_health,
    run_offline_runtime_health,
)

MAX_INPUT_BYTES = 32 * 1024


def _load_evidence(path: Path) -> Top5RuntimeEvidence:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("evidence input exceeds the bounded size")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read evidence input: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("evidence input must be a JSON object")
    return Top5RuntimeEvidence.from_mapping(payload.get("evidence") or payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to inspect (presence checks only)",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="optional bounded offline evidence JSON; skips repository inspection",
    )
    parser.add_argument(
        "--runtime-dirtiness",
        action="append",
        default=[],
        metavar="PATH",
        help="informational runtime dirtiness path; may be repeated",
    )
    parser.add_argument(
        "--source-dirtiness",
        action="append",
        default=[],
        metavar="PATH",
        help="unexpected source dirtiness path; fail-closed when present",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.evidence:
            report = assess_top5_runtime_health(_load_evidence(args.evidence))
        else:
            report = run_offline_runtime_health(
                args.repo_root,
                runtime_dirtiness=args.runtime_dirtiness,
                source_dirtiness=args.source_dirtiness,
            )
    except (OSError, TypeError, ValueError) as exc:
        payload = {
            "status": BLOCKED_STATUS,
            "ready": False,
            "mode": "offline-only",
            "checks": {"input_valid": False},
            "blockers": [str(exc)[:256]],
            "warnings": [],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    print(json.dumps(report.as_payload(), indent=2, sort_keys=True))
    return 0 if report.status == READY_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
