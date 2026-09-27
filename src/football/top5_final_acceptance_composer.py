"""Canonical read-only assembler for the Top-5 B1 final acceptance bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from monitoring.top5.governed_runtime_evidence import (
    observe_governed_runtime,
    verify_artifact_digest,
)
from src.football.top5_final_acceptance import (
    FINAL_ACCEPTANCE_SCHEMA_VERSION,
    TOP5_LEAGUES,
    Top5FinalAcceptanceError,
    verify_final_acceptance,
)
from src.football.top5_provider_native_evidence_bridge import (
    Top5B4EvidenceDossierV1,
)
from src.football.top5_public_acceptance import Top5PrepublicationArtifactV1
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    CandidateReadiness,
    inventory_for,
)
from src.football.top5_signal_lifecycle import (
    SignalLifecycleStage,
    Top5SignalLifecycle,
    Top5SignalLifecycleContract,
    Top5SignalLifecycleVersion,
)

_B4_INPUT_KEYS = (
    "source_main_sha",
    "b4_quota_proof_package",
    "b4_quota_headroom",
    "discovery_evidence",
    "provider_native_discovery_provenance",
    "controlled_shadow",
    "b4_reconciliation",
    "b4_qualification",
    "b4_native_authorization",
    "b4_dossier_digest",
)


class Top5FinalAcceptanceCompositionError(ValueError):
    """Raised when canonical typed inputs cannot compose into B1 acceptance."""


def _model_runtime(
    lifecycles: Sequence[Top5SignalLifecycle],
) -> dict[str, object]:
    if (
        not isinstance(lifecycles, Sequence)
        or isinstance(lifecycles, (str, bytes))
        or len(lifecycles) != len(TOP5_LEAGUES)
    ):
        raise Top5FinalAcceptanceCompositionError(
            "model input must contain exactly five Top-5 signal lifecycles"
        )

    versions: dict[str, Top5SignalLifecycleVersion] = {}
    contracts: dict[str, Top5SignalLifecycleContract] = {}
    for lifecycle in lifecycles:
        if not isinstance(lifecycle, Top5SignalLifecycle):
            raise Top5FinalAcceptanceCompositionError(
                "model input must use typed Top-5 signal lifecycles"
            )
        lifecycle.validate()
        version = lifecycle.versions[-1]
        version.validate()
        league = version.league_code.upper()
        if league in versions or league not in TOP5_LEAGUES:
            raise Top5FinalAcceptanceCompositionError(
                "model lifecycle set has a duplicate or unsupported league"
            )
        if version.stage is SignalLifecycleStage.WITHDRAWN:
            raise Top5FinalAcceptanceCompositionError(
                "withdrawn lifecycle cannot identify the acceptance model"
            )
        contract = lifecycle.contract
        contract.validate()
        if (
            version.lifecycle_contract_id != contract.contract_id
            or version.stage_contract_id != contract.stage_contract_id(version.stage)
        ):
            raise Top5FinalAcceptanceCompositionError(
                "lifecycle version is not bound to its typed contract"
            )
        if (
            version.candidate_id != M5_CANDIDATE_ID
            or version.model_identity != M5_CANDIDATE_ID
            or version.research_sha != FROZEN_RESEARCH_SHA
            or version.snapshot_kind.value != "signal_time"
            or contract.no_closing_odds is not True
            or contract.signal_time_approved_for_production is not False
            or version.eligibility_decision is not True
            or version.withdrawal_authorized is not False
            or version.no_bet is not True
            or version.publication_enabled is not False
            or version.activation_enabled is not False
        ):
            raise Top5FinalAcceptanceCompositionError(
                "lifecycle model identity or non-authorizing safety state is invalid"
            )
        inventory = inventory_for(league, M5_CANDIDATE_ID)
        if (
            inventory.readiness is not CandidateReadiness.AVAILABLE_FOR_SHADOW
            or inventory.production_enabled is not False
            or inventory.model_artifact_hash != version.model_artifact_hash
        ):
            raise Top5FinalAcceptanceCompositionError(
                "lifecycle model artifact differs from frozen shadow inventory"
            )
        versions[league] = version
        contracts[league] = contract

    if set(versions) != TOP5_LEAGUES:
        raise Top5FinalAcceptanceCompositionError(
            "model lifecycles must cover exactly the five Top-5 leagues"
        )
    ordered = [versions[league] for league in sorted(TOP5_LEAGUES)]
    identity_fields = (
        "candidate_id",
        "model_identity",
        "research_sha",
        "source_sha",
        "model_artifact_hash",
        "lifecycle_contract_id",
        "stage_contract_id",
        "stage",
    )
    first = ordered[0]
    if any(
        any(getattr(version, name) != getattr(first, name) for name in identity_fields)
        for version in ordered[1:]
    ):
        raise Top5FinalAcceptanceCompositionError(
            "five league lifecycles do not share one model and Signal-Time identity"
        )
    if any(
        contract.as_payload() != contracts[min(TOP5_LEAGUES)].as_payload()
        for contract in contracts.values()
    ):
        raise Top5FinalAcceptanceCompositionError(
            "five league lifecycles do not share one timing contract"
        )
    canonical_league = min(TOP5_LEAGUES)
    canonical_inventory = inventory_for(canonical_league, M5_CANDIDATE_ID)

    return {
        "candidate_id": first.candidate_id,
        "model_identity": first.model_identity,
        "research_sha": first.research_sha,
        "source_sha": first.source_sha,
        "model_artifact_hash": first.model_artifact_hash,
        "signal_time_contract_id": first.stage_contract_id,
        "prediction_input_kind": "signal_time",
        "closing_used_for_prediction": False,
        "production_model_approved": canonical_inventory.production_enabled,
        "signal_time_approved_for_production": contracts[
            canonical_league
        ].signal_time_approved_for_production,
        "publication_authorized": any(
            version.publication_enabled for version in ordered
        ),
        "no_bet": all(version.no_bet for version in ordered),
        "prediction_input_allowed": all(
            version.eligibility_decision for version in ordered
        ),
    }


def compose_top5_final_acceptance_bundle(
    *,
    b4_dossier: Top5B4EvidenceDossierV1,
    lifecycles: Sequence[Top5SignalLifecycle],
    prepublication_artifact: Top5PrepublicationArtifactV1,
    now: datetime,
    expected_source_main_sha: str | None = None,
) -> dict[str, object]:
    """Compose and verify one canonical B1 v2 bundle without creating evidence."""

    b4_inputs = b4_dossier.b1_evidence_inputs(now=now)
    if not isinstance(b4_inputs, Mapping) or set(b4_inputs) != set(_B4_INPUT_KEYS):
        raise Top5FinalAcceptanceCompositionError(
            "B4 dossier returned an unexpected B1 evidence input shape"
        )

    model_runtime = _model_runtime(lifecycles)
    runtime_evidence = observe_governed_runtime()
    if (
        not isinstance(runtime_evidence, dict)
        or runtime_evidence.get("status") != "READY"
        or not verify_artifact_digest(runtime_evidence)
    ):
        raise Top5FinalAcceptanceCompositionError(
            "governed runtime evidence is not a valid READY observation"
        )

    shadow = b4_inputs["controlled_shadow"]
    if not isinstance(shadow, Mapping):
        raise Top5FinalAcceptanceCompositionError(
            "B4 controlled-shadow handoff is not an object"
        )
    run_id = shadow.get("controlled_shadow_run_id")
    session_id = shadow.get("qualification_session_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise Top5FinalAcceptanceCompositionError(
            "B4 controlled-shadow run identity is missing"
        )
    if not isinstance(session_id, str) or not session_id.strip():
        raise Top5FinalAcceptanceCompositionError(
            "B4 qualification-session identity is missing"
        )
    if not isinstance(prepublication_artifact, Top5PrepublicationArtifactV1):
        raise Top5FinalAcceptanceCompositionError(
            "public input must be a typed prepublication artifact"
        )
    prepublication_artifact.validate(
        now=now,
        expected_run_id=run_id,
        expected_session_id=session_id,
        expected_source_release_sha=str(model_runtime["source_sha"]),
        expected_runtime_data_sha=str(runtime_evidence["runtime_data_sha"]),
        expected_model_artifact_hash=str(model_runtime["model_artifact_hash"]),
        expected_signal_time_contract_id=str(model_runtime["signal_time_contract_id"]),
    )

    bundle: dict[str, object] = {
        "schema_version": FINAL_ACCEPTANCE_SCHEMA_VERSION,
        **{key: b4_inputs[key] for key in _B4_INPUT_KEYS},
        "model_runtime": model_runtime,
        "runtime_evidence": runtime_evidence,
        "public": prepublication_artifact.as_payload(),
    }
    try:
        verify_final_acceptance(
            bundle,
            now=now,
            expected_source_main_sha=expected_source_main_sha,
        )
    except Top5FinalAcceptanceError:
        raise
    except Exception as exc:
        raise Top5FinalAcceptanceCompositionError(str(exc)) from exc
    return bundle


__all__ = [
    "Top5FinalAcceptanceCompositionError",
    "compose_top5_final_acceptance_bundle",
]
