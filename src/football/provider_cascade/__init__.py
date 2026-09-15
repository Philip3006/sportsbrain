"""Top-5 shadow provider cascade public API."""

from src.football.provider_cascade.adapters import (
    AdapterResult,
    ApiFootballAdapter,
    BetfairDelayedAdapter,
    OddsApiIoAdapter,
    ProviderRequest,
    RawProviderResponse,
    TheOddsAPIAdapter,
)
from src.football.provider_cascade.budget import (
    PreflightDecision,
    ProviderBudgetCounters,
    RequestBudgetManager,
)
from src.football.provider_cascade.builder1 import (
    Builder1OddsInput,
    accepted_for_builder1,
)
from src.football.provider_cascade.comparison import (
    ProviderComparisonMetric,
    ProviderComparisonReport,
    compare_provider_results,
)
from src.football.provider_cascade.contracts import (
    DEFAULT_PROVIDER_ORDER,
    MARKET_PREMATCH_1X2,
    CascadeDecisionTrace,
    CascadeResult,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderAttemptTrace,
    ProviderCascadeConfig,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
)
from src.football.provider_cascade.health import ProviderHealth, ProviderHealthRegistry
from src.football.provider_cascade.router import ProviderCascadeRouter

__all__ = [
    "DEFAULT_PROVIDER_ORDER",
    "MARKET_PREMATCH_1X2",
    "AdapterResult",
    "ApiFootballAdapter",
    "BetfairDelayedAdapter",
    "Builder1OddsInput",
    "CascadeDecisionTrace",
    "CascadeResult",
    "NormalizedOddsObservation",
    "ObservationCompleteness",
    "OddsApiIoAdapter",
    "PreflightDecision",
    "ProviderAttemptTrace",
    "ProviderBudgetCounters",
    "ProviderCascadeConfig",
    "ProviderCascadeRouter",
    "ProviderComparisonMetric",
    "ProviderComparisonReport",
    "ProviderConfig",
    "ProviderHealth",
    "ProviderHealthRegistry",
    "ProviderRequest",
    "ProviderState",
    "QuotaSnapshot",
    "RawProviderResponse",
    "RequestBudgetManager",
    "TheOddsAPIAdapter",
    "accepted_for_builder1",
    "compare_provider_results",
]
