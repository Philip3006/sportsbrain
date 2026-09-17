"""Zero-network release-day preflight for a future Top-5 provider run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.football.provider_cascade.contracts import (
    NetworkAuthorizationContract,
    ProviderCascadeConfig,
)
from src.football.provider_cascade.readiness import (
    QuotaStateStore,
    release_day_preflight,
)


def _authorization(value: object) -> NetworkAuthorizationContract | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("network_authorization must be a mapping")
    authorization = NetworkAuthorizationContract(
        controlled_shadow_run_ref=str(value.get("controlled_shadow_run_ref", "")),
        authorized_providers=tuple(
            str(provider) for provider in value.get("authorized_providers", ())
        ),
    )
    authorization.validate()
    return authorization


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a zero-network Top-5 provider release preflight."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="JSON containing cascade_config and secret-free readiness evidence",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        help="Optional external operator-owned quota-state JSON path",
    )
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    if not isinstance(payload, dict):
        raise TypeError("preflight input must be a JSON object")
    config_value = payload.get("cascade_config")
    if not isinstance(config_value, dict):
        raise TypeError("cascade_config must be supplied as a JSON object")
    config = ProviderCascadeConfig.from_mapping(config_value)
    store = QuotaStateStore(args.state_path) if args.state_path else QuotaStateStore()
    result = release_day_preflight(
        config,
        authorization=_authorization(payload.get("network_authorization")),
        quota_state_store=store,
        credentials=payload.get("credentials_present"),
        identity_readiness=payload.get("identity_readiness"),
        readiness_states=payload.get("readiness_states"),
    )
    print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
    return 0 if result.status == "READY_FOR_PROVIDER_RUN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
