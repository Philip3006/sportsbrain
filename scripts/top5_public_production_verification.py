#!/usr/bin/env python3
"""Read-only verification of the two public Top-5 delivery surfaces.

The live mode performs anonymous HTTP GETs only.  It never sends credentials,
calls a provider, writes Cloudflare/KV, deploys, publishes, activates, or
mutates betting/ledger state.  Captured-file mode is used by tests and by
operators who must keep a production check staged until the hard gate opens.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from src.football.top5_public_acceptance import validate_public_bundle

VERIFIED = "TOP5_PUBLIC_PRODUCTION_VERIFIED"
BLOCKED = "TOP5_PUBLIC_PRODUCTION_BLOCKED"


def verify_public_captures(
    worker_payload: dict[str, Any],
    static_payload: dict[str, Any],
    *,
    now: datetime,
    worker_status: int = 200,
    static_status: int = 200,
    pwa_status: int = 200,
) -> dict[str, object]:
    reasons: list[dict[str, str]] = []
    if worker_status != 200:
        reasons.append(
            {
                "code": "PUBLIC_WORKER_INCOMPATIBLE",
                "message": f"Worker status is {worker_status}",
            }
        )
    if static_status != 200:
        reasons.append(
            {
                "code": "PUBLIC_STATIC_UNAVAILABLE",
                "message": f"static status is {static_status}",
            }
        )
    if pwa_status != 200:
        reasons.append(
            {
                "code": "PUBLIC_PWA_INCOMPATIBLE",
                "message": f"PWA status is {pwa_status}",
            }
        )
    worker = validate_public_bundle(worker_payload, now=now)
    static = validate_public_bundle(static_payload, now=now)
    for name, result in (("worker", worker), ("static", static)):
        if result.get("status") != "TOP5_PUBLIC_DELIVERY_READY":
            reasons.extend(
                {
                    "code": reason.get("code", "PUBLIC_SCHEMA_INVALID"),
                    "message": f"{name}: {reason.get('message', '')}",
                }
                for reason in result.get("reasons", [])
            )
    if worker.get("bundle_digest") != static.get("bundle_digest"):
        reasons.append(
            {
                "code": "PUBLIC_BUNDLE_PARTIAL",
                "message": "Worker/static public bundle digest mismatch",
            }
        )
    if worker.get("generation_id") != static.get("generation_id"):
        reasons.append(
            {
                "code": "PUBLIC_PROVENANCE_INVALID",
                "message": "Worker/static generation mismatch",
            }
        )
    for field in (
        "source_release_sha",
        "runtime_data_sha",
        "source_runtime_consistent",
    ):
        if worker.get(field) != static.get(field):
            reasons.append(
                {
                    "code": "PUBLIC_PROVENANCE_INVALID",
                    "message": f"Worker/static {field} mismatch",
                }
            )
    status = VERIFIED if not reasons else BLOCKED
    return {
        "status": status,
        "worker_status": worker_status,
        "static_status": static_status,
        "pwa_status": pwa_status,
        "leagues": list(worker.get("leagues", [])),
        "generation_id": worker.get("generation_id"),
        "source_release_sha": worker.get("source_release_sha"),
        "runtime_data_sha": worker.get("runtime_data_sha"),
        "source_runtime_consistent": worker.get("source_runtime_consistent"),
        "reasons": reasons,
    }


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON input must be an object: {path}")
    return value


def _fetch(url: str) -> tuple[int, dict[str, Any]]:
    request = Request(
        url, headers={"Accept": "application/json", "Cache-Control": "no-cache"}
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("public response must be a JSON object")
        return int(response.status), payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-url")
    parser.add_argument("--static-url")
    parser.add_argument("--worker-payload", type=Path)
    parser.add_argument("--static-payload", type=Path)
    parser.add_argument("--pwa-status", type=int, default=200)
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    try:
        if bool(args.worker_url) == bool(args.worker_payload) or bool(
            args.static_url
        ) == bool(args.static_payload):
            raise ValueError(
                "provide exactly one worker URL/file and one static URL/file"
            )
        raw_now = args.now or datetime.now(timezone.utc).isoformat()
        now = datetime.fromisoformat(raw_now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("--now must include timezone")
        if args.worker_url:
            worker_status, worker_payload = _fetch(args.worker_url)
        else:
            worker_status, worker_payload = 200, _read(args.worker_payload)
        if args.static_url:
            static_status, static_payload = _fetch(args.static_url)
        else:
            static_status, static_payload = 200, _read(args.static_payload)
        result = verify_public_captures(
            worker_payload,
            static_payload,
            now=now.astimezone(timezone.utc),
            worker_status=worker_status,
            static_status=static_status,
            pwa_status=args.pwa_status,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "status": BLOCKED,
            "reasons": [{"code": "PUBLIC_READ_FAILED", "message": str(exc)}],
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == VERIFIED else 1


if __name__ == "__main__":
    raise SystemExit(main())
