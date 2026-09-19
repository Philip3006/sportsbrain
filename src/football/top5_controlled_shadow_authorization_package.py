"""Offline reconciliation and authorization packaging for Top-5 shadows.

This module is deliberately a boundary layer around the reviewed PR #103
network contract and PR #112 candidate eligibility contract.  It prepares a
disabled-by-default package, and it validates a later completed network run;
it does not create CEO authority, enable a transport, issue a receipt, or
change the active Football provider repertoire.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from src.football.provider_cascade.candidate_eligibility import (
    CandidateEligibilityError,
    CandidateProviderEligibilityV1,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
)
from src.football.top5_therundown_network_shadow import (
    NETWORK_SHADOW_SCHEMA_VERSION,
    NetworkShadowContractError,
    NetworkShadowRunStatus,
    TheRundownNetworkAuthorizationV1,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkShadowCaptureV1,
    TheRundownNetworkShadowRunResultV1,
)

CANONICAL_CANDIDATE_PROVIDER = "therundown_experimental"
TOP5_LEAGUE_ORDER = ("EPL", "BL1", "LL", "SA", "L1")
AUTHORIZATION_PACKAGE_SCHEMA_VERSION = "top5-controlled-shadow-authorization-package-v1"
RECONCILIATION_SCHEMA_VERSION = "top5-controlled-shadow-reconciliation-v1"
QUALIFICATION_ARTIFACT_SCHEMA_VERSION = (
    "top5-controlled-shadow-qualification-artifacts-v1"
)
FUTURE_EXECUTION_COMMAND = (
    "python -m src.football.top5_controlled_shadow_authorization_package "
    "--execute --package <authorization-package.json> "
    "--authorization <ceo-authorization.json>"
)


class ControlledShadowAuthorizationPackageError(NetworkShadowContractError):
    """The offline package or a completed run is not safe to consume."""


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _require_canonical_targets(
    configuration: TheRundownNetworkConfigurationV1,
) -> None:
    providers = {target.provider for target in configuration.targets}
    if providers != {CANONICAL_CANDIDATE_PROVIDER}:
        raise ControlledShadowAuthorizationPackageError(
            "authorization package requires the canonical "
            "therundown_experimental provider identity"
        )
    if {target.league for target in configuration.targets} != set(TOP5_LEAGUE_ORDER):
        raise ControlledShadowAuthorizationPackageError(
            "authorization package requires exactly the five canonical leagues"
        )


def _scope_payload(
    configuration: TheRundownNetworkConfigurationV1,
) -> dict[str, object]:
    return {
        "targets": [target.as_payload() for target in configuration.targets],
        "participant_scope": [
            item.as_payload() for item in configuration.participant_scope
        ],
        "request_scope": [item.as_payload() for item in configuration.request_scope],
    }


@dataclass(frozen=True)
class ControlledShadowAuthorizationPackageV1:
    """A non-executable package template for one future CEO-authorized run."""

    configuration_payload: Mapping[str, object]
    authorization_template: Mapping[str, object]
    package_digest: str
    network_execution_enabled: bool = False
    receipt_issuer_present: bool = False
    active_provider_authority: bool = False
    scheduler_registered: bool = False
    publication: bool = False
    production_activation: bool = False
    betting: bool = False
    monetary_spend_authorized: bool = False

    @property
    def future_execution_command(self) -> str:
        return FUTURE_EXECUTION_COMMAND

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": AUTHORIZATION_PACKAGE_SCHEMA_VERSION,
            "configuration": dict(self.configuration_payload),
            "authorization_template": dict(self.authorization_template),
            "future_execution_command": self.future_execution_command,
            "network_execution_enabled": False,
            "receipt_issuer_present": False,
            "active_provider_authority": False,
            "scheduler_registered": False,
            "publication": False,
            "production_activation": False,
            "betting": False,
            "monetary_spend_authorized": False,
        }

    def validate(self) -> None:
        if self.package_digest != _digest(self._payload_without_digest()):
            raise ControlledShadowAuthorizationPackageError(
                "authorization package digest mismatch"
            )
        if any(
            value is not False
            for value in (
                self.network_execution_enabled,
                self.receipt_issuer_present,
                self.active_provider_authority,
                self.scheduler_registered,
                self.publication,
                self.production_activation,
                self.betting,
                self.monetary_spend_authorized,
            )
        ):
            raise ControlledShadowAuthorizationPackageError(
                "authorization package contains an unsafe capability"
            )
        configuration = self.configuration_payload
        if configuration.get("enabled") is not False:
            raise ControlledShadowAuthorizationPackageError(
                "authorization package must remain disabled by default"
            )
        if configuration.get("provider") != CANONICAL_CANDIDATE_PROVIDER:
            raise ControlledShadowAuthorizationPackageError(
                "authorization package provider identity is not canonical"
            )
        required = {
            "ceo_authorization_identity",
            "authorization_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "issued_at",
            "expires_at",
        }
        if set(required) - set(self.authorization_template):
            raise ControlledShadowAuthorizationPackageError(
                "authorization template is missing CEO/run/expiry fields"
            )
        if any(self.authorization_template[name] is not None for name in required):
            raise ControlledShadowAuthorizationPackageError(
                "offline template must not self-create CEO authorization"
            )
        for name in (
            "maximum_request_count",
            "maximum_datapoints",
            "maximum_quota_cost_units",
            "request_quota_cost_units",
            "adapter_source_sha",
            "configuration_digest",
        ):
            if name not in self.authorization_template:
                raise ControlledShadowAuthorizationPackageError(
                    f"authorization template is missing {name}"
                )
        if self.authorization_template.get("maximum_retries") != 0:
            raise ControlledShadowAuthorizationPackageError(
                "authorization template permits retries"
            )
        for name, expected in (
            ("no_bet", True),
            ("publication", False),
            ("production_activation", False),
            ("monetary_spend_authorized", False),
        ):
            if self.authorization_template.get(name) is not expected:
                raise ControlledShadowAuthorizationPackageError(
                    f"authorization template safety field {name} is unsafe"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._payload_without_digest(),
            "package_digest": self.package_digest,
        }


def prepare_authorization_package(
    configuration: TheRundownNetworkConfigurationV1,
) -> ControlledShadowAuthorizationPackageV1:
    """Prepare a disabled template without creating any authority."""

    configuration.validate()
    _require_canonical_targets(configuration)
    if configuration.enabled is not False:
        raise ControlledShadowAuthorizationPackageError(
            "package preparation requires a disabled configuration"
        )
    scope = _scope_payload(configuration)
    configuration_payload = {
        "schema_version": NETWORK_SHADOW_SCHEMA_VERSION,
        "provider": CANONICAL_CANDIDATE_PROVIDER,
        **scope,
        "adapter_version": configuration.adapter_version,
        "adapter_source_sha": configuration.adapter_source_sha,
        "maximum_request_count": configuration.maximum_request_count,
        "maximum_datapoints": configuration.maximum_datapoints,
        "maximum_quota_cost_units": configuration.maximum_quota_cost_units,
        "request_quota_cost_units": configuration.request_quota_cost_units,
        "maximum_source_age_seconds": configuration.maximum_source_age_seconds,
        "minimum_interval_seconds": configuration.minimum_interval_seconds,
        "maximum_retries": configuration.maximum_retries,
        "enabled": False,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
        "configuration_digest": configuration.configuration_digest,
    }
    authorization_template = {
        "schema_version": NETWORK_SHADOW_SCHEMA_VERSION,
        "authorization_id": None,
        "ceo_authorization_identity": None,
        "controlled_shadow_run_id": None,
        "qualification_session_id": None,
        "provider": CANONICAL_CANDIDATE_PROVIDER,
        **scope,
        "adapter_version": configuration.adapter_version,
        "adapter_source_sha": configuration.adapter_source_sha,
        "configuration_digest": configuration.configuration_digest,
        "maximum_request_count": configuration.maximum_request_count,
        "maximum_datapoints": configuration.maximum_datapoints,
        "maximum_quota_cost_units": configuration.maximum_quota_cost_units,
        "request_quota_cost_units": configuration.request_quota_cost_units,
        "maximum_source_age_seconds": configuration.maximum_source_age_seconds,
        "issued_at": None,
        "expires_at": None,
        "minimum_interval_seconds": configuration.minimum_interval_seconds,
        "maximum_retries": 0,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
        "requires_caller_supplied_ceo_authorization": True,
    }
    package = ControlledShadowAuthorizationPackageV1(
        configuration_payload=configuration_payload,
        authorization_template=authorization_template,
        package_digest="",
    )
    package = ControlledShadowAuthorizationPackageV1(
        configuration_payload=configuration_payload,
        authorization_template=authorization_template,
        package_digest=_digest(package._payload_without_digest()),
    )
    package.validate()
    return package


@dataclass(frozen=True)
class QualificationReadyArtifactsV1:
    """Evidence inputs for B1/B2; never a receipt or authority object."""

    provider: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    configuration_digest: str
    capture_attestations: tuple[Mapping[str, object], ...]
    candidate_eligibilities: tuple[Mapping[str, object], ...]
    qualification_inputs: tuple[Mapping[str, object], ...]
    builder2_receipt_inputs: tuple[Mapping[str, object], ...]
    cascade_evidence_available: bool = False
    qualification_status: str = "PENDING_BUILDER2_VALIDATION"
    receipt_eligible: bool = False
    receipt_issuer_present: bool = False
    authority_changed: bool = False
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False

    def validate(self) -> None:
        if self.provider != CANONICAL_CANDIDATE_PROVIDER:
            raise ControlledShadowAuthorizationPackageError(
                "qualification artifacts provider identity is not canonical"
            )
        if len(self.capture_attestations) != len(TOP5_LEAGUE_ORDER):
            raise ControlledShadowAuthorizationPackageError(
                "qualification artifacts must contain five attestations"
            )
        expected_count = len(TOP5_LEAGUE_ORDER)
        if any(
            len(items) != expected_count
            for items in (
                self.candidate_eligibilities,
                self.qualification_inputs,
                self.builder2_receipt_inputs,
            )
        ):
            raise ControlledShadowAuthorizationPackageError(
                "qualification artifacts must contain five items per evidence type"
            )
        if (
            self.cascade_evidence_available is not False
            or self.receipt_eligible is not False
            or self.receipt_issuer_present is not False
            or self.authority_changed is not False
            or self.publication is not False
            or self.production_activation is not False
            or self.monetary_spend_authorized is not False
        ):
            raise ControlledShadowAuthorizationPackageError(
                "qualification artifact safety boundary is unsafe"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
            "provider": self.provider,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "configuration_digest": self.configuration_digest,
            "capture_attestations": [dict(item) for item in self.capture_attestations],
            "candidate_eligibilities": [
                dict(item) for item in self.candidate_eligibilities
            ],
            "qualification_inputs": [dict(item) for item in self.qualification_inputs],
            "builder2_receipt_inputs": [
                dict(item) for item in self.builder2_receipt_inputs
            ],
            "cascade_evidence_available": False,
            "qualification_status": self.qualification_status,
            "receipt_eligible": False,
            "receipt_issuer_present": False,
            "authority_changed": False,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
        }


@dataclass(frozen=True)
class FiveLeagueReconciliationV1:
    """Validated, ordered five-league evidence package for downstream B2."""

    provider: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    authorization_digest: str
    configuration_digest: str
    adapter_version: str
    adapter_source_sha: str
    leagues: tuple[str, ...]
    fixture_keys: tuple[str, ...]
    provider_event_ids: tuple[str, ...]
    provider_request_ids: tuple[str, ...]
    request_count: int
    datapoint_count: int
    quota_cost_units: float
    artifacts: QualificationReadyArtifactsV1
    reconciliation_digest: str
    receipt_eligible: bool = False
    authority_changed: bool = False
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "provider": self.provider,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "configuration_digest": self.configuration_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "leagues": list(self.leagues),
            "fixture_keys": list(self.fixture_keys),
            "provider_event_ids": list(self.provider_event_ids),
            "provider_request_ids": list(self.provider_request_ids),
            "request_count": self.request_count,
            "datapoint_count": self.datapoint_count,
            "quota_cost_units": self.quota_cost_units,
            "artifacts": self.artifacts.as_payload(),
            "receipt_eligible": False,
            "authority_changed": False,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
        }

    def validate(self) -> None:
        if self.provider != CANONICAL_CANDIDATE_PROVIDER:
            raise ControlledShadowAuthorizationPackageError(
                "reconciliation provider identity is not canonical"
            )
        if self.leagues != TOP5_LEAGUE_ORDER:
            raise ControlledShadowAuthorizationPackageError(
                "reconciliation league order or coverage is invalid"
            )
        self.artifacts.validate()
        if self.reconciliation_digest != _digest(self._payload_without_digest()):
            raise ControlledShadowAuthorizationPackageError(
                "reconciliation digest mismatch"
            )
        if any(
            value is not False
            for value in (
                self.receipt_eligible,
                self.authority_changed,
                self.publication,
                self.production_activation,
                self.monetary_spend_authorized,
            )
        ):
            raise ControlledShadowAuthorizationPackageError(
                "reconciliation contains an unsafe capability"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._payload_without_digest(),
            "reconciliation_digest": self.reconciliation_digest,
        }


def _validate_capture(
    capture: TheRundownNetworkShadowCaptureV1,
    *,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
    now: datetime,
) -> CandidateProviderEligibilityV1:
    if capture.evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED:
        raise ControlledShadowAuthorizationPackageError(
            "synthetic or replay evidence cannot enter reconciliation"
        )
    if capture.network_execution is not True:
        raise ControlledShadowAuthorizationPackageError(
            "reconciliation requires network execution evidence"
        )
    if capture.candidate_only is not True or capture.receipt_eligible is not False:
        raise ControlledShadowAuthorizationPackageError(
            "capture authority flags are unsafe"
        )
    target = capture.target
    request = capture.request
    response = capture.response
    if target.provider != CANONICAL_CANDIDATE_PROVIDER:
        raise ControlledShadowAuthorizationPackageError(
            "capture provider is not the canonical candidate"
        )
    if response.evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED:
        raise ControlledShadowAuthorizationPackageError(
            "response evidence kind does not match network capture"
        )
    if response.network_execution is not True:
        raise ControlledShadowAuthorizationPackageError(
            "response network execution does not match capture"
        )
    if response.provider != target.provider:
        raise ControlledShadowAuthorizationPackageError("provider mismatch")
    if response.league != target.league:
        raise ControlledShadowAuthorizationPackageError("league mismatch")
    if response.fixture_key != target.fixture_key:
        raise ControlledShadowAuthorizationPackageError("fixture mismatch")
    if response.provider_event_id != target.provider_event_id:
        raise ControlledShadowAuthorizationPackageError("event mismatch")
    if response.provider_request_id != request.request_identity:
        raise ControlledShadowAuthorizationPackageError("request mismatch")
    if (
        response.home_participant_id != request.home_participant_id
        or response.away_participant_id != request.away_participant_id
    ):
        raise ControlledShadowAuthorizationPackageError("participant mismatch")
    if (
        request.controlled_shadow_run_id != authorization.controlled_shadow_run_id
        or request.qualification_session_id != authorization.qualification_session_id
        or request.authorization_id != authorization.authorization_id
    ):
        raise ControlledShadowAuthorizationPackageError(
            "run/session/authorization binding mismatch"
        )
    if request.configuration_digest != configuration.configuration_digest:
        raise ControlledShadowAuthorizationPackageError("configuration digest mismatch")
    try:
        attestation = ControlledShadowCaptureAttestation.from_payload(
            capture.canonical_capture_attestation
        )
        attestation.validate()
    except Exception as exc:
        raise ControlledShadowAuthorizationPackageError(
            f"invalid canonical capture attestation: {exc}"
        ) from exc
    expected_attestation = {
        "controlled_shadow_run_id": request.controlled_shadow_run_id,
        "ceo_authorization_id": request.authorization_id,
        "qualification_session_id": request.qualification_session_id,
        "provider_identity": target.provider,
        "fixture_key": target.fixture_key,
        "provider_event_id": response.provider_event_id,
        "provider_request_id": response.provider_request_id,
        "adapter_version": response.adapter_version,
        "adapter_source_sha": response.adapter_source_sha,
        "cascade_evidence_digest": response.cascade_evidence_digest,
        "raw_response_digest": response.raw_response_digest,
        "normalized_record_digest": response.normalized_record_digest,
        "network_execution": True,
        "no_bet": True,
        "publication": False,
        "monetary_spend_authorized": False,
    }
    attestation_payload = attestation.as_payload()
    for name, expected in expected_attestation.items():
        if attestation_payload.get(name) != expected:
            raise ControlledShadowAuthorizationPackageError(
                f"capture attestation binding mismatch: {name}"
            )
    try:
        candidate = CandidateProviderEligibilityV1.from_network_capture(
            capture, now=now
        )
    except CandidateEligibilityError as exc:
        raise ControlledShadowAuthorizationPackageError(
            f"candidate eligibility rejected: {exc}"
        ) from exc
    if candidate.maximum_source_age_seconds != configuration.maximum_source_age_seconds:
        raise ControlledShadowAuthorizationPackageError(
            "candidate source-age contract does not match configuration"
        )
    return candidate


def reconcile_controlled_shadow_run(
    result: TheRundownNetworkShadowRunResultV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
    *,
    now: datetime,
) -> FiveLeagueReconciliationV1:
    """Validate a completed network result into B1/B2 evidence inputs."""

    now = _utc(now, "reconciliation now")
    try:
        configuration.validate()
        authorization.validate(configuration, now=now)
        result.validate()
    except Exception as exc:
        raise ControlledShadowAuthorizationPackageError(str(exc)) from exc
    _require_canonical_targets(configuration)
    if authorization.provider != CANONICAL_CANDIDATE_PROVIDER:
        raise ControlledShadowAuthorizationPackageError(
            "authorization provider is not canonical"
        )
    if result.status is not NetworkShadowRunStatus.COMPLETED_NETWORK:
        raise ControlledShadowAuthorizationPackageError(
            "only a completed network run can be reconciled"
        )
    if not result.all_five_succeeded:
        raise ControlledShadowAuthorizationPackageError(
            "five-league run is incomplete or contains failures"
        )
    if (
        result.controlled_shadow_run_id != authorization.controlled_shadow_run_id
        or result.qualification_session_id != authorization.qualification_session_id
        or result.authorization_id != authorization.authorization_id
    ):
        raise ControlledShadowAuthorizationPackageError(
            "run result authority binding mismatch"
        )
    captures_by_league: dict[str, TheRundownNetworkShadowCaptureV1] = {}
    candidates: dict[str, CandidateProviderEligibilityV1] = {}
    fixture_keys: list[str] = []
    event_ids: list[str] = []
    request_ids: list[str] = []
    for capture in result.captures:
        league = capture.target.league
        if league in captures_by_league:
            raise ControlledShadowAuthorizationPackageError(
                "duplicate league observation"
            )
        captures_by_league[league] = capture
        candidate = _validate_capture(
            capture,
            configuration=configuration,
            authorization=authorization,
            now=now,
        )
        candidates[league] = candidate
        fixture_keys.append(candidate.fixture_key)
        event_ids.append(candidate.provider_event_id)
        request_ids.append(candidate.request_identity)
    if tuple(captures_by_league) != TOP5_LEAGUE_ORDER:
        raise ControlledShadowAuthorizationPackageError(
            "completed run must preserve the canonical five-league order"
        )
    if len(set(fixture_keys)) != len(fixture_keys):
        raise ControlledShadowAuthorizationPackageError("duplicate fixture identity")
    if len(set(event_ids)) != len(event_ids):
        raise ControlledShadowAuthorizationPackageError("duplicate provider event")
    if len(set(request_ids)) != len(request_ids):
        raise ControlledShadowAuthorizationPackageError("duplicate request identity")
    if result.request_count != len(result.captures):
        raise ControlledShadowAuthorizationPackageError(
            "request count does not match five captures"
        )
    datapoint_count = sum(
        capture.response.datapoint_count for capture in result.captures
    )
    quota_cost_units = sum(
        capture.response.quota_cost_units for capture in result.captures
    )
    if result.datapoint_count != datapoint_count:
        raise ControlledShadowAuthorizationPackageError(
            "datapoint evidence does not reconcile"
        )
    if result.quota_cost_units != quota_cost_units:
        raise ControlledShadowAuthorizationPackageError(
            "quota evidence does not reconcile"
        )
    ordered_captures = tuple(captures_by_league[league] for league in TOP5_LEAGUE_ORDER)
    ordered_candidates = tuple(candidates[league] for league in TOP5_LEAGUE_ORDER)
    artifacts = QualificationReadyArtifactsV1(
        provider=CANONICAL_CANDIDATE_PROVIDER,
        controlled_shadow_run_id=authorization.controlled_shadow_run_id,
        qualification_session_id=authorization.qualification_session_id,
        authorization_id=authorization.authorization_id,
        configuration_digest=configuration.configuration_digest,
        capture_attestations=tuple(
            dict(capture.canonical_capture_attestation) for capture in ordered_captures
        ),
        candidate_eligibilities=tuple(
            candidate.as_payload() for candidate in ordered_candidates
        ),
        qualification_inputs=tuple(
            dict(capture.qualification_input) for capture in ordered_captures
        ),
        builder2_receipt_inputs=tuple(
            dict(capture.builder2_receipt_input) for capture in ordered_captures
        ),
    )
    artifacts.validate()
    reconciliation = FiveLeagueReconciliationV1(
        provider=CANONICAL_CANDIDATE_PROVIDER,
        controlled_shadow_run_id=authorization.controlled_shadow_run_id,
        qualification_session_id=authorization.qualification_session_id,
        authorization_id=authorization.authorization_id,
        authorization_digest=authorization.authorization_digest,
        configuration_digest=configuration.configuration_digest,
        adapter_version=configuration.adapter_version,
        adapter_source_sha=configuration.adapter_source_sha,
        leagues=TOP5_LEAGUE_ORDER,
        fixture_keys=tuple(fixture_keys),
        provider_event_ids=tuple(event_ids),
        provider_request_ids=tuple(request_ids),
        request_count=result.request_count,
        datapoint_count=result.datapoint_count,
        quota_cost_units=result.quota_cost_units,
        artifacts=artifacts,
        reconciliation_digest="",
    )
    reconciliation = FiveLeagueReconciliationV1(
        **{
            **reconciliation.__dict__,
            "reconciliation_digest": _digest(reconciliation._payload_without_digest()),
        }
    )
    reconciliation.validate()
    return reconciliation


__all__ = [
    "AUTHORIZATION_PACKAGE_SCHEMA_VERSION",
    "CANONICAL_CANDIDATE_PROVIDER",
    "FUTURE_EXECUTION_COMMAND",
    "RECONCILIATION_SCHEMA_VERSION",
    "TOP5_LEAGUE_ORDER",
    "ControlledShadowAuthorizationPackageError",
    "ControlledShadowAuthorizationPackageV1",
    "FiveLeagueReconciliationV1",
    "QualificationReadyArtifactsV1",
    "prepare_authorization_package",
    "reconcile_controlled_shadow_run",
]
