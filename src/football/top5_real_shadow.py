"""Bounded real-data Top-5 shadow execution with a hard no-bet boundary."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.football.production_contracts import (
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_dispatch import signal_time_contract_id
from src.football.top5_real_shadow_provider import (
    ProviderObservation,
    RealTop5Provider,
    Transport,
    requests_transport,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID
from src.football.top5_shadow_integration import (
    Top5ShadowIntegrationResult,
    run_offline_top5_shadow,
)
from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

REAL_SHADOW_REQUEST_COST = 1
TOP5_REAL_SHADOW_LEAGUES = tuple(sorted(TOP5_LEAGUE_ADAPTERS))
_FORBIDDEN_ID_WORDS = frozenset({"latest", "current", "best"})


class RealShadowQuotaError(ProductionContractError):
    """Raised before a provider request when the authorized quota is unsafe."""


class RealShadowExecutionError(ProductionContractError):
    """Raised for an authentication/rate-limit boundary or invalid run input."""


@dataclass(frozen=True)
class ShadowExperiment:
    """Caller-selected experiment timing, explicitly separate from production policy."""

    experiment_id: str
    minimum_lead_minutes: int
    maximum_lead_minutes: int
    maximum_odds_age_seconds: int

    def validate(self) -> None:
        if not self.experiment_id.strip():
            raise ProductionContractError("shadow experiment requires an identity")
        lowered = self.experiment_id.lower()
        if not lowered.startswith("shadow-experiment:"):
            raise ProductionContractError("shadow timing must use the shadow-experiment namespace")
        if any(word in lowered.split(":")[-1].split("-") for word in _FORBIDDEN_ID_WORDS):
            raise ProductionContractError("shadow timing identity cannot be ambiguous")
        self.signal_time_contract.validate()

    @property
    def signal_time_contract(self) -> SignalTimeContract:
        return SignalTimeContract(
            minimum_minutes_before_kickoff=self.minimum_lead_minutes,
            maximum_minutes_before_kickoff=self.maximum_lead_minutes,
            maximum_odds_age_seconds=self.maximum_odds_age_seconds,
        )

    @property
    def contract_id(self) -> str:
        self.validate()
        return signal_time_contract_id(self.signal_time_contract)

    @property
    def evidence_candidate_name(self) -> str:
        """Identify this timing experiment independently from the M5 model."""

        return f"{self.experiment_id}:{self.contract_id}"


@dataclass(frozen=True)
class RealShadowCycleResult:
    """Complete controlled cycle, retaining artifacts only in memory by default."""

    run_id: str
    started_at: datetime
    completed_at: datetime
    integration_sha: str
    research_sha: str
    timing: ShadowExperiment
    league_codes: tuple[str, ...]
    quota_remaining_before: int
    expected_request_count: int
    safety_reserve: int
    observations: tuple[ProviderObservation, ...]
    integrations: tuple[Top5ShadowIntegrationResult, ...]
    no_bet: bool = True
    publication: bool = False

    def validate(self) -> None:
        if self.research_sha != FROZEN_RESEARCH_SHA:
            raise ProductionContractError("real shadow cycle references unfrozen research")
        normalized_leagues = _normalize_league_scope(self.league_codes)
        if normalized_leagues != self.league_codes:
            raise ProductionContractError("real shadow cycle league scope is not deterministic")
        observed_leagues = tuple(sorted(observation.league_code for observation in self.observations))
        if observed_leagues != self.league_codes:
            raise ProductionContractError("real shadow cycle did not complete its selected league scope")
        if len(self.observations) != self.expected_request_count:
            raise ProductionContractError("real shadow cycle did not complete its bounded request plan")
        if self.expected_request_count != len(self.league_codes):
            raise ProductionContractError("real shadow cycle request count differs from selected scope")
        if self.quota_remaining_before < self.expected_request_count + self.safety_reserve:
            raise RealShadowQuotaError("quota preflight no longer satisfies the safety reserve")
        self.timing.validate()
        if not self.no_bet or self.publication:
            raise ProductionContractError("real shadow cycle violates no-bet/publication boundary")
        for integration in self.integrations:
            integration.validate()
            if not integration.no_bet or integration.publication:
                raise ProductionContractError("real shadow integration is not no-bet")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "implementation_sha": self.integration_sha,
            "research_sha": self.research_sha,
            "league_codes": list(self.league_codes),
            "timing_experiment": {
                "experiment_id": self.timing.experiment_id,
                "contract_id": self.timing.contract_id,
                "minimum_lead_minutes": self.timing.minimum_lead_minutes,
                "maximum_lead_minutes": self.timing.maximum_lead_minutes,
                "maximum_odds_age_seconds": self.timing.maximum_odds_age_seconds,
                "production_approved": False,
            },
            "quota": {
                "remaining_before": self.quota_remaining_before,
                "expected_requests": self.expected_request_count,
                "safety_reserve": self.safety_reserve,
                "bulk_request_cost_units": REAL_SHADOW_REQUEST_COST,
            },
            "provider": "the_odds_api",
            "observations": [observation.as_payload() for observation in self.observations],
            "integrations": [integration.as_payload() for integration in self.integrations],
            "no_bet": self.no_bet,
            "publication": self.publication,
        }

    def as_evidence_payload(self) -> dict[str, object]:
        """Emit the independent Builder 2 evidence contract."""

        self.validate()
        from src.football.top5_shadow_evidence import build_evidence_payload

        return build_evidence_payload(self)


def run_controlled_shadow_cycle(
    *,
    api_key: str,
    timing: ShadowExperiment,
    league_codes: Sequence[str],
    quota_remaining: int,
    safety_reserve: int,
    integration_sha: str,
    now: datetime | None = None,
    transport: Transport = requests_transport,
) -> RealShadowCycleResult:
    """Execute exactly one explicitly scoped controlled shadow cycle."""

    timing.validate()
    if not _is_sha(integration_sha):
        raise ProductionContractError("controlled shadow requires an exact implementation SHA")
    if quota_remaining < 0 or safety_reserve <= 0:
        raise RealShadowQuotaError("quota and safety reserve must be non-negative and positive")
    selected_leagues = _normalize_league_scope(league_codes)
    expected_requests = len(selected_leagues)
    if quota_remaining < expected_requests + safety_reserve:
        raise RealShadowQuotaError("quota is below the bounded cycle plus safety reserve")
    started_at = _utc(now or datetime.now(timezone.utc), "now")
    provider = RealTop5Provider(api_key, transport=transport)
    observations: list[ProviderObservation] = []
    integrations: list[Top5ShadowIntegrationResult] = []
    for league_code in selected_leagues:
        observation = provider.fetch_league(league_code, timing, requested_at=started_at)
        observations.append(observation)
        if observation.status_code in (401, 403, 429):
            raise RealShadowExecutionError(f"provider_{observation.status_code}")
        if observation.fixtures:
            integrations.append(
                run_offline_top5_shadow(
                    league_code,
                    observation.fixtures,
                    observation.snapshots,
                    signal_time=timing.signal_time_contract,
                    now=observation.completed_at,
                    integration_sha=integration_sha,
                    candidate_ids=(M5_CANDIDATE_ID,),
                )
            )
    completed_at = datetime.now(timezone.utc)
    run_id = _stable_id(
        "top5-real-shadow",
        (integration_sha, timing.experiment_id, ",".join(selected_leagues), started_at.isoformat()),
    )
    result = RealShadowCycleResult(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        integration_sha=integration_sha,
        research_sha=FROZEN_RESEARCH_SHA,
        timing=timing,
        league_codes=selected_leagues,
        quota_remaining_before=quota_remaining,
        expected_request_count=expected_requests,
        safety_reserve=safety_reserve,
        observations=tuple(observations),
        integrations=tuple(integrations),
    )
    result.validate()
    return result


def write_shadow_archive(result: RealShadowCycleResult) -> Path:
    """Write a redacted shadow archive outside the checkout, atomically."""

    result.validate()
    path = runtime_state_path(
        f"football/top5/shadow_runs/{result.run_id}.json",
        require_external=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.as_evidence_payload()
    if path.exists():
        existing = json.loads(path.read_text())
        if existing != payload:
            raise ProductionContractError("shadow archive identity collision")
        return path
    atomic_write_json(path, payload)
    return path


def _stable_id(prefix: str, fields: Sequence[str]) -> str:
    encoded = json.dumps(tuple(fields), separators=(",", ":")).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:32]}"


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value.lower())


def _normalize_league_scope(league_codes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(league_codes, (str, bytes)):
        raise ProductionContractError("league scope must be a sequence of league codes")
    try:
        selected = tuple(league_codes)
    except TypeError as exc:
        raise ProductionContractError("league scope must be a non-empty sequence") from exc
    if not selected:
        raise ProductionContractError("league scope must not be empty")
    if any(not isinstance(code, str) or not code.strip() for code in selected):
        raise ProductionContractError("league scope contains an invalid league code")
    if len(selected) != len(set(selected)):
        raise ProductionContractError("league scope contains duplicate leagues")
    unknown = sorted(set(selected) - set(TOP5_REAL_SHADOW_LEAGUES))
    if unknown:
        raise ProductionContractError(f"league scope contains unknown leagues: {', '.join(unknown)}")
    return tuple(sorted(selected))
