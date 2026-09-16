"""Guarded CLI for validating and acceptance-testing a controlled shadow run.

The normal ``execute`` command is deliberately unavailable without the
explicit ``--acceptance-fake`` switch.  The switch only selects the injected
fake transport; it never creates an authority artifact and never contacts a
provider.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.football.provider_cascade.execution_harness import (
    ControlledShadowExecutionHarness,
    FakeControlledShadowTransport,
    HarnessContractError,
    HarnessExecutionBlocked,
    ProviderTransportResponse,
)
from src.football.provider_cascade.preparation import load_preparation


def _read_json(path: str) -> dict[str, object]:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise HarnessContractError("artifact paths must be absolute")
    try:
        value: Any = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessContractError("artifact could not be read as JSON") from exc
    if not isinstance(value, dict):
        raise HarnessContractError("artifact JSON must be an object")
    return value


def _fake_transport(path: str | None) -> FakeControlledShadowTransport:
    if path is None:
        return FakeControlledShadowTransport()
    raw = _read_json(path)
    responses: dict[tuple[str, str], ProviderTransportResponse] = {}
    for key, value in raw.items():
        if not isinstance(value, dict) or "::" not in key:
            raise HarnessContractError("fake responses must use provider::ACTION keys")
        provider, action = key.split("::", 1)
        responses[(provider, action)] = ProviderTransportResponse(**value)
    return FakeControlledShadowTransport(responses)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Top-5 controlled shadow execution harness"
    )
    parser.add_argument("command", choices=("validate-run", "dry-run", "execute"))
    parser.add_argument(
        "--preparation", required=True, help="absolute preparation JSON path"
    )
    parser.add_argument(
        "--authorization", required=True, help="absolute CEO authorization JSON path"
    )
    parser.add_argument(
        "--fake-responses", help="absolute JSON path for injected fake responses"
    )
    parser.add_argument(
        "--acceptance-fake",
        action="store_true",
        help="permit the test-only fake transport",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preparation = load_preparation(args.preparation)
        authorization = _read_json(args.authorization)
        harness = ControlledShadowExecutionHarness()
        if args.command in {"validate-run", "dry-run"}:
            report = harness.validate_run(preparation, authorization)
            print(json.dumps(report.as_payload(), sort_keys=True))
            return 0
        if not args.acceptance_fake:
            raise HarnessExecutionBlocked(
                "real execute is disabled; use an explicitly injected acceptance fake"
            )
        result = harness.execute(
            preparation,
            authorization,
            transport=_fake_transport(args.fake_responses),
        )
        print(
            json.dumps(
                {
                    "status": result.status.value,
                    "controlled_shadow_run_id": result.controlled_shadow_run_id,
                    "selected_provider": result.selected_provider,
                    "network_request_count": result.network_request_count,
                    "quota_cost_units": result.quota_cost_units,
                    "attestation": result.attestation.as_payload(),
                },
                sort_keys=True,
            )
        )
        return 0
    except (HarnessContractError, HarnessExecutionBlocked) as exc:
        print(
            json.dumps(
                {"valid": False, "fail_closed": True, "reason": str(exc)},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
