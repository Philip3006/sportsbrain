"""Deterministic, idempotent dispatch claims for offline shadow artifacts."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from src.football.production_contracts import (
    MarketSnapshot,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)


def signal_time_contract_id(contract: SignalTimeContract) -> str:
    """Return a stable identity for the exact timing contract values."""

    contract.validate()
    payload = {
        "max_lead_minutes": contract.maximum_minutes_before_kickoff,
        "max_odds_age_seconds": contract.maximum_odds_age_seconds,
        "min_lead_minutes": contract.minimum_minutes_before_kickoff,
        "approval_ref": contract.approval_ref,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def snapshot_generation(snapshot: MarketSnapshot) -> str:
    """Use a supplied generation, or derive one from source and UTC capture."""

    snapshot.validate()
    captured = _utc(snapshot.captured_at, "captured_at").isoformat()
    return snapshot.snapshot_id or f"{snapshot.source}:{captured}"


@dataclass(frozen=True)
class DispatchKey:
    league_code: str
    fixture_key: str
    contract_id: str
    snapshot_generation: str

    @property
    def value(self) -> str:
        fields = (
            self.league_code,
            self.fixture_key,
            self.contract_id,
            self.snapshot_generation,
        )
        if any(not value.strip() for value in fields):
            raise ProductionContractError("dispatch key fields must be non-empty")
        encoded = json.dumps(fields, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()[:24]
        return "top5-shadow:" + digest


def make_dispatch_key(
    league_code: str,
    fixture_key: str,
    contract: SignalTimeContract,
    snapshot: MarketSnapshot,
) -> DispatchKey:
    """Build the stable identity for league/fixture/contract/snapshot."""

    return DispatchKey(
        league_code=league_code,
        fixture_key=fixture_key,
        contract_id=signal_time_contract_id(contract),
        snapshot_generation=snapshot_generation(snapshot),
    )


@dataclass(frozen=True)
class DispatchClaim:
    key: DispatchKey
    accepted: bool
    retry_reason: str | None
    retry_count: int


class OfflineDispatchLedger:
    """In-memory claim store; it has no persistence or production side effect."""

    def __init__(self) -> None:
        self._claims: dict[str, int] = {}

    def claim(self, key: DispatchKey, *, retry_reason: str | None = None) -> DispatchClaim:
        """Claim once; only an explicit nonblank retry reason can repeat it."""

        key_value = key.value
        previous = self._claims.get(key_value, 0)
        reason = retry_reason.strip() if retry_reason is not None else None
        accepted = previous == 0 or bool(reason)
        retry_count = previous + 1 if accepted else previous
        if accepted:
            self._claims[key_value] = retry_count
        return DispatchClaim(key, accepted, reason, retry_count)

    def snapshot(self) -> Mapping[str, int]:
        return dict(self._claims)
