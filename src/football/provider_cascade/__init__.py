"""Top-5 shadow provider cascade public API."""

from src.football.provider_cascade.adapters import (
    AdapterResult,
    ProviderRequest,
    RawProviderResponse,
    TheOddsAPIAdapter,
    resolve_provider_identity,
)
from src.football.provider_cascade.budget import (
    PreflightDecision,
    ProviderBudgetCounters,
    RequestBudgetManager,
)
from src.football.provider_cascade.builder1 import (
    Builder1OddsInput,
    accepted_for_builder1,
    builder2_evidence_payload,
)
from src.football.provider_cascade.candidate_eligibility import (
    CANDIDATE_PROVIDER_IDENTITIES,
    CANDIDATE_PROVIDER_REPERTOIRE,
    CandidateEligibilityError,
    CandidateProviderEligibilityV1,
)
from src.football.provider_cascade.comparison import (
    ProviderComparisonMetric,
    ProviderComparisonReport,
    compare_provider_results,
)
from src.football.provider_cascade.contracts import (
    BUILDER2_VALIDATION_CONTRACT_VERSION,
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
    DECOMMISSIONED_FOOTBALL_PROVIDERS,
    DEFAULT_PROVIDER_ORDER,
    FOOTBALL_PROVIDER_REPERTOIRE,
    MARKET_PREMATCH_1X2,
    CascadeDecisionTrace,
    CascadeResult,
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderAttemptTrace,
    ProviderCascadeConfig,
    ProviderConfig,
    ProviderIdentityResolution,
    ProviderIdentityResolutionState,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
    TransportCapability,
)
from src.football.provider_cascade.health import ProviderHealth, ProviderHealthRegistry
from src.football.provider_cascade.preparation import (
    PREPARATION_BLOCKED,
    PREPARATION_READY,
    PREPARATION_SCHEMA_VERSION,
    ControlledShadowRunPreparationV1,
    PreparationAction,
    PreparationContractError,
    ProviderPreparationManifest,
    ProviderReadinessEvidence,
    build_controlled_shadow_preparation,
    load_preparation,
    preparation_from_input_payload,
    write_preparation,
)
from src.football.provider_cascade.readiness import (
    DEFAULT_QUOTA_FRESHNESS_SECONDS,
    QUOTA_STATE_SCHEMA_VERSION,
    RELEASE_PREFLIGHT_BLOCKED,
    RELEASE_PREFLIGHT_READY,
    PersistedProviderQuotaState,
    ProviderReadinessRecord,
    ProviderReadinessView,
    QuotaStateStore,
    ReleasePreflightResult,
    build_provider_readiness_view,
    next_month_start,
    quota_reset_revalidation_eligible,
    release_day_preflight,
)
from src.football.provider_cascade.router import ProviderCascadeRouter

# The execution harness validates observations using contracts from
# ``top5_controlled_shadow_provider_qualification``.  Importing it eagerly
# here creates a package-initialization cycle when that contract imports the
# candidate-eligibility submodule through this package.  Keep the historical
# public API, but load the harness only after package initialization has
# completed.
_EXECUTION_HARNESS_EXPORTS = frozenset(
    {
        "AUTHORIZATION_SCHEMA_VERSION",
        "CAPTURE_ATTESTATION_SCHEMA_VERSION",
        "EXECUTION_BANNERS",
        "ControlledShadowCaptureAttestationV1",
        "ControlledShadowExecutionHarness",
        "ControlledShadowExecutionResult",
        "ControlledShadowRunAuthorizationV1",
        "FakeControlledShadowTransport",
        "HarnessContractError",
        "HarnessExecutionBlocked",
        "InMemoryControlledShadowRuntimeState",
        "NetworkCapableProviderTransport",
        "ProviderAction",
        "ProviderTransportRequest",
        "ProviderTransportResponse",
        "RealProviderTransport",
        "RunValidationReport",
    }
)
def __getattr__(name: str):
    if name in _EXECUTION_HARNESS_EXPORTS:
        from src.football.provider_cascade import execution_harness

        value = getattr(execution_harness, name)
        globals()[name] = value
        return value
    if name == "ConfiguredNetworkProviderTransport":
        from src.football.provider_cascade.real_transport_bridge import (
            ConfiguredNetworkProviderTransport,
        )

        globals()[name] = ConfiguredNetworkProviderTransport
        return ConfiguredNetworkProviderTransport
    if name in {
        "Builder2QualificationReceiptError",
        "Builder2QualificationReceiptV1",
        "validate_builder1_qualification_receipt",
        "validate_builder4_qualification_receipt",
    }:
        from src.football import top5_builder2_qualification_receipt

        value = getattr(top5_builder2_qualification_receipt, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AUTHORIZATION_SCHEMA_VERSION",
    "BUILDER2_VALIDATION_CONTRACT_VERSION",
    "CANDIDATE_ONLY_PROVIDER_IDENTITIES",
    "CANDIDATE_PROVIDER_IDENTITIES",
    "CANDIDATE_PROVIDER_REPERTOIRE",
    "CAPTURE_ATTESTATION_SCHEMA_VERSION",
    "DECOMMISSIONED_FOOTBALL_PROVIDERS",
    "DEFAULT_PROVIDER_ORDER",
    "DEFAULT_QUOTA_FRESHNESS_SECONDS",
    "EXECUTION_BANNERS",
    "FOOTBALL_PROVIDER_REPERTOIRE",
    "MARKET_PREMATCH_1X2",
    "PREPARATION_BLOCKED",
    "PREPARATION_READY",
    "PREPARATION_SCHEMA_VERSION",
    "QUOTA_STATE_SCHEMA_VERSION",
    "RELEASE_PREFLIGHT_BLOCKED",
    "RELEASE_PREFLIGHT_READY",
    "AdapterResult",
    "Builder1OddsInput",
    "Builder2QualificationReceiptError",
    "Builder2QualificationReceiptV1",
    "CandidateEligibilityError",
    "CandidateProviderEligibilityV1",
    "CascadeDecisionTrace",
    "CascadeResult",
    "CascadeTimingPolicy",
    "ConfiguredNetworkProviderTransport",
    "ControlledShadowCaptureAttestationV1",
    "ControlledShadowExecutionHarness",
    "ControlledShadowExecutionResult",
    "ControlledShadowRunAuthorizationV1",
    "ControlledShadowRunPreparationV1",
    "FakeControlledShadowTransport",
    "HarnessContractError",
    "HarnessExecutionBlocked",
    "InMemoryControlledShadowRuntimeState",
    "NetworkAuthorizationContract",
    "NetworkCapableProviderTransport",
    "NormalizedOddsObservation",
    "ObservationCompleteness",
    "PersistedProviderQuotaState",
    "PreflightDecision",
    "PreparationAction",
    "PreparationContractError",
    "ProviderAction",
    "ProviderAttemptTrace",
    "ProviderBudgetCounters",
    "ProviderCascadeConfig",
    "ProviderCascadeRouter",
    "ProviderComparisonMetric",
    "ProviderComparisonReport",
    "ProviderConfig",
    "ProviderHealth",
    "ProviderHealthRegistry",
    "ProviderIdentityResolution",
    "ProviderIdentityResolutionState",
    "ProviderPreparationManifest",
    "ProviderReadinessEvidence",
    "ProviderReadinessRecord",
    "ProviderReadinessView",
    "ProviderRequest",
    "ProviderState",
    "ProviderTransportRequest",
    "ProviderTransportResponse",
    "QuotaSnapshot",
    "QuotaStateStore",
    "RawProviderResponse",
    "RealProviderTransport",
    "ReleasePreflightResult",
    "RequestBudgetManager",
    "RunValidationReport",
    "TheOddsAPIAdapter",
    "TimingProvenance",
    "TransportCapability",
    "accepted_for_builder1",
    "build_controlled_shadow_preparation",
    "build_provider_readiness_view",
    "builder2_evidence_payload",
    "compare_provider_results",
    "load_preparation",
    "next_month_start",
    "preparation_from_input_payload",
    "quota_reset_revalidation_eligible",
    "release_day_preflight",
    "resolve_provider_identity",
    "validate_builder1_qualification_receipt",
    "validate_builder4_qualification_receipt",
    "write_preparation",
]
