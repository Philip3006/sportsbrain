"""League-owned, disabled Top-5 adapter metadata.

The objects in this module are intentionally not a live registry.  They are
immutable descriptions that can be consumed by offline tests and readiness
reports.  A production caller must still provide every dependency explicitly
and pass the rollout evidence gates from :mod:`production_contracts`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from src.football.production_contracts import (
    DISABLED_TOP5_REGISTRY,
    TOP5_HEALTH_PREFIX,
    TOP5_SHADOW_ARCHIVE_PREFIX,
    TOP5_STAGED_ARTIFACT_PREFIX,
    ActivationMode,
    ArtifactOwner,
    LeagueProductionConfig,
    ProductionContractError,
    SignalTimeContract,
    validate_artifact_ownership,
)


@dataclass(frozen=True)
class Top5LeagueAdapter:
    """Explicit metadata boundary for one future Top-5 league.

    ``model_adapter_slot`` and ``signal_time_slot`` make unbound decisions
    visible to readiness tooling.  They are slots, not registrations.
    """

    config: LeagueProductionConfig
    model_adapter_slot: str = "unbound"
    signal_time_slot: str = "unconfigured"
    health_identity: str = ""

    def __post_init__(self) -> None:
        if not self.health_identity:
            object.__setattr__(self, "health_identity", f"top5_shadow:{self.config.league_code}")

    @property
    def league_code(self) -> str:
        return self.config.league_code

    @property
    def league_identity(self) -> str:
        return self.config.display_name

    @property
    def provider_competition_id(self) -> str:
        mapping = self.config.provider_mapping
        if mapping is None:
            raise ProductionContractError("Top-5 adapter is missing provider mapping")
        return mapping.competition_id

    @property
    def artifact_namespaces(self) -> Mapping[ArtifactOwner, str]:
        prefix = f"{self.config.league_code}/"
        return MappingProxyType(
            {
                ArtifactOwner.SHADOW_ARCHIVE: TOP5_SHADOW_ARCHIVE_PREFIX + prefix,
                ArtifactOwner.STAGED_PUBLIC: TOP5_STAGED_ARTIFACT_PREFIX + prefix,
                ArtifactOwner.HEALTH: TOP5_HEALTH_PREFIX + prefix,
            }
        )

    def validate(self) -> None:
        self.config.assert_disabled()
        if self.config.model_adapter_id != self.model_adapter_slot:
            raise ProductionContractError("model adapter slot differs from disabled config")
        if self.model_adapter_slot != "unbound":
            raise ProductionContractError("Top-5 model adapter slot must remain unbound")
        if not self.signal_time_slot.strip():
            raise ProductionContractError("signal-time slot must be explicit")
        if not self.health_identity.strip():
            raise ProductionContractError("health identity must be explicit")
        for owner, prefix in self.artifact_namespaces.items():
            validate_artifact_ownership(prefix + "placeholder.json", owner)

    def shadow_test_config(
        self,
        signal_time: SignalTimeContract,
        *,
        test_model_adapter_id: str = "offline-dummy-model",
    ) -> LeagueProductionConfig:
        """Return a test-only shadow config without mutating this disabled one."""

        self.validate()
        if not test_model_adapter_id.strip():
            raise ProductionContractError("offline test model adapter identity is required")
        signal_time.validate()
        return replace(
            self.config,
            activation_mode=ActivationMode.SHADOW,
            signal_time=signal_time,
            model_adapter_id=test_model_adapter_id,
        )


TOP5_LEAGUE_ADAPTERS: Mapping[str, Top5LeagueAdapter] = MappingProxyType(
    {
        config.league_code: Top5LeagueAdapter(config=config)
        for config in DISABLED_TOP5_REGISTRY.values()
    }
)


def validate_top5_adapters(
    adapters: Mapping[str, Top5LeagueAdapter] = TOP5_LEAGUE_ADAPTERS,
) -> None:
    """Validate metadata while keeping it outside the active league registry."""

    expected_codes = set(DISABLED_TOP5_REGISTRY)
    if set(adapters) != expected_codes:
        raise ProductionContractError("Top-5 adapter set must contain exactly five disabled leagues")
    for code, adapter in adapters.items():
        if code != adapter.league_code:
            raise ProductionContractError("Top-5 adapter key differs from league identity")
        adapter.validate()


validate_top5_adapters()
