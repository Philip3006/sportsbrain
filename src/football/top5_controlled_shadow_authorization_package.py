"""Offline reconciliation and authorization packaging for Top-5 shadows.

This module is deliberately a boundary layer around the reviewed PR #103
network contract and PR #112 candidate eligibility contract.  It prepares a
disabled-by-default package, and it validates a later completed network run;
it does not create CEO authority, enable a transport, issue a receipt, or
change the active Football provider repertoire.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any

from src.football.odds.therundown import THERUNDOWN_BASE_URL
from src.football.odds.therundown_la_liga_capture import (
    SAME_RUN_LL_VALIDATION_SCHEMA,
    validate_same_run_ll_capture,
)
from src.football.provider_cascade.candidate_eligibility import (
    CandidateEligibilityError,
    CandidateProviderEligibilityV1,
)
from src.football.top5_b2_qualification_batch_orchestrator import (
    Builder2FiveLeagueShadowPackageV1,
    build_five_league_shadow_package,
    load_five_league_shadow_package,
)
from src.football.top5_b2_shadow_qualification_intake import (
    INTAKE_CONTRACT_VERSION,
    Builder2QualificationIntakeError,
    Builder2QualificationIntakeManifestV1,
    Builder2QualificationSourceArtifactV1,
    _safe_external_path,
)
from src.football.top5_builder2_qualification_receipt import semantic_digest
from src.football.top5_controlled_shadow_provider_qualification import (
    QUALIFICATION_CONTRACT_VERSION,
    CEOAuthorization,
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
    ProviderQualificationSession,
    ProviderReadinessState,
    QualificationTimingPolicy,
    RealProviderObservation,
)
from src.football.top5_provider_cascade_validation import (
    BudgetDecision,
    CascadeAttempt,
    CascadeEvidence,
    CascadeOutcome,
    CascadeProvenance,
    CascadeQuotaSnapshot,
    CascadeSafety,
    MarketPhase,
    RequestCostClassification,
    evidence_digest,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    NETWORK_SHADOW_SCHEMA_VERSION,
    NetworkShadowContractError,
    NetworkShadowRunStatus,
    TheRundownCanonicalPayloadAdapterV1,
    TheRundownHttpNetworkTransportV1,
    TheRundownNetworkAuthorizationV1,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkParticipantScopeV1,
    TheRundownNetworkRequestScopeV1,
    TheRundownNetworkShadowCaptureV1,
    TheRundownNetworkShadowExecutorV1,
    TheRundownNetworkShadowRunResultV1,
    TheRundownQuotaHeadroomEvidenceV1,
    TheRundownQuotaProofAuthorizationV1,
    TheRundownQuotaProofEvidenceV1,
    TheRundownQuotaProofRequestV1,
    execute_therundown_quota_proof,
)
from src.football.top5_therundown_shadow_canary import TheRundownCanaryTargetV1
from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

CANONICAL_CANDIDATE_PROVIDER = "therundown_experimental"
TOP5_LEAGUE_ORDER = ("EPL", "BL1", "LL", "SA", "L1")
AUTHORIZATION_PACKAGE_SCHEMA_VERSION = "top5-controlled-shadow-authorization-package-v1"
RECONCILIATION_SCHEMA_VERSION = "top5-controlled-shadow-reconciliation-v1"
QUALIFICATION_ARTIFACT_SCHEMA_VERSION = (
    "top5-controlled-shadow-qualification-artifacts-v1"
)
FUTURE_EXECUTION_COMMAND = (
    "python -m src.football.top5_controlled_shadow_authorization_package "
    "--execute-network --package <authorization-package.json> "
    "--authorization <ceo-authorization.json> "
    "--quota-headroom <quota-headroom-evidence.json> "
    "--credential-file /operator-only/top5/therundown.env "
    "--output /operator-only/top5/top5-b2-five-league-shadow-package.json"
)
QUOTA_PROOF_COMMAND = (
    "python -m src.football.top5_controlled_shadow_authorization_package "
    "--execute-quota-proof "
    "--proof-authorization <quota-proof-authorization.json> "
    "--proof-target-evidence /private/tmp/top5-b1-laliga-final-evidence.json "
    "--spend-control-evidence <provider-tier-evidence.json> "
    "--credential-file /operator-only/top5/therundown.env "
    "--output /operator-only/top5/top5-quota-proof.json"
)
DEFAULT_THERUNDOWN_CREDENTIAL_PATH = Path.home() / "sportsbrain" / ".env"
SPEND_CONTROL_EVIDENCE_MAXIMUM_AGE_SECONDS = 86_400
QUOTA_PROOF_CONSUMPTION_SCHEMA_VERSION = "top5-therundown-quota-proof-consumption-v1"
B2_SHADOW_TIMING_KICKOFF_TOLERANCE_SECONDS = 60
B2_SHADOW_TIMING_MINIMUM_LEAD_SECONDS = 0
B2_SHADOW_TIMING_MAXIMUM_LEAD_SECONDS = 10_800


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


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ControlledShadowAuthorizationPackageError(f"{name} must be an object")
    return value


def _timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise ControlledShadowAuthorizationPackageError(
                f"{name} must be an ISO-8601 timestamp"
            ) from exc
    raise ControlledShadowAuthorizationPackageError(
        f"{name} must be an ISO-8601 timestamp"
    )


def _digest_value(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) not in (40, 64):
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be a hexadecimal digest"
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be a hexadecimal digest"
        ) from exc
    return value.lower()


def _price(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be a decimal price"
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be a decimal price"
        ) from exc
    if number <= 1.0 or not isfinite(number):
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be a decimal price"
        )
    return number


def _materialize_b1_ll_artifact(artifact: object) -> Mapping[str, object]:
    if hasattr(artifact, "as_evidence_bundle"):
        artifact = artifact.as_evidence_bundle()  # type: ignore[union-attr]
    if isinstance(artifact, Mapping) and isinstance(
        artifact.get("canonical_b1_evidence_bundle"), Mapping
    ):
        artifact = artifact["canonical_b1_evidence_bundle"]
    return _mapping(artifact, "B1 La Liga artifact")


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ControlledShadowAuthorizationPackageError(f"{name} must be a path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ControlledShadowAuthorizationPackageError(
            f"{name} must be an absolute path"
        )
    return path


def _read_json_file(path_value: object, name: str) -> Mapping[str, object]:
    path = _absolute_path(path_value, name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlledShadowAuthorizationPackageError(
            f"{name} could not be read"
        ) from exc
    return _mapping(raw, name)


def _read_protected_therundown_credential(path_value: object) -> str:
    """Read the existing operator-only .env convention without exposing it."""

    path = _absolute_path(path_value, "credential_file")
    try:
        if path.is_symlink():
            raise ControlledShadowAuthorizationPackageError(
                "credential_file must not be a symlink"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ControlledShadowAuthorizationPackageError(
                "credential_file permissions are too broad"
            )
        lines = path.read_text(encoding="utf-8").splitlines()
    except ControlledShadowAuthorizationPackageError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise ControlledShadowAuthorizationPackageError(
            "credential_file could not be read; THERUNDOWN_API_KEY is unavailable"
        ) from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() not in ("THERUNDOWN_API_KEY", "export THERUNDOWN_API_KEY"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            return value
    raise ControlledShadowAuthorizationPackageError(
        "THERUNDOWN_API_KEY is missing from credential_file"
    )


def quota_proof_consumption_state_path() -> Path:
    """Return the sole external operator-owned quota-proof consumption store."""

    return runtime_state_path(
        "football/top5/quota-proof-consumption",
        require_external=True,
    )


def _quota_proof_consumption_identity(
    authorization: TheRundownQuotaProofAuthorizationV1,
) -> str:
    return _digest(
        {
            "proof_authorization_id": authorization.proof_authorization_id,
            "authorization_digest": authorization.authorization_digest,
        }
    )


def _quota_proof_consumption_marker_path(
    authorization: TheRundownQuotaProofAuthorizationV1,
) -> Path:
    directory = quota_proof_consumption_state_path()
    if not directory.is_absolute():
        raise ControlledShadowAuthorizationPackageError(
            "quota proof consumption store must be an absolute operator path"
        )
    marker_name = "quota-proof-" + _digest(
        {"proof_authorization_id": authorization.proof_authorization_id}
    )
    return directory / f"{marker_name}.json"


def _read_quota_proof_consumption_marker(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlledShadowAuthorizationPackageError(
            "existing quota proof consumption marker is invalid; refusing reuse"
        ) from exc
    marker = _mapping(raw, "quota proof consumption marker")
    required = {
        "schema_version",
        "consumption_identity",
        "proof_authorization_id",
        "authorization_digest",
        "proof_id",
        "provider",
        "provider_event_id",
        "request_shape_digest",
        "proof_target_source_digest",
        "consumed_at",
        "maximum_request_count",
        "retry_count",
    }
    if (
        set(marker) != required
        or marker.get("schema_version") != QUOTA_PROOF_CONSUMPTION_SCHEMA_VERSION
    ):
        raise ControlledShadowAuthorizationPackageError(
            "existing quota proof consumption marker is invalid; refusing reuse"
        )
    for name in (
        "consumption_identity",
        "authorization_digest",
        "request_shape_digest",
        "proof_target_source_digest",
    ):
        _digest_value(marker.get(name), f"quota proof marker {name}")
    _timestamp(marker.get("consumed_at"), "quota proof marker consumed_at")
    if marker.get("maximum_request_count") != 1 or marker.get("retry_count") != 0:
        raise ControlledShadowAuthorizationPackageError(
            "existing quota proof consumption marker has unsafe limits"
        )
    return marker


def _consume_quota_proof_authorization(
    authorization: TheRundownQuotaProofAuthorizationV1,
    *,
    consumed_at: datetime,
) -> Path:
    """Atomically consume one proof authorization before credential/transport use."""

    directory = quota_proof_consumption_state_path()
    if directory.is_symlink():
        raise ControlledShadowAuthorizationPackageError(
            "quota proof consumption store must not be a symlink"
        )
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise ControlledShadowAuthorizationPackageError(
                "quota proof consumption store is not a directory"
            )
        directory.chmod(0o700)
        if stat.S_IMODE(directory.stat().st_mode) & 0o077:
            raise ControlledShadowAuthorizationPackageError(
                "quota proof consumption store permissions are too broad"
            )
    except ControlledShadowAuthorizationPackageError:
        raise
    except OSError as exc:
        raise ControlledShadowAuthorizationPackageError(
            "quota proof consumption store is unavailable"
        ) from exc

    marker_path = _quota_proof_consumption_marker_path(authorization)
    marker = {
        "schema_version": QUOTA_PROOF_CONSUMPTION_SCHEMA_VERSION,
        "consumption_identity": _quota_proof_consumption_identity(authorization),
        "proof_authorization_id": authorization.proof_authorization_id,
        "authorization_digest": authorization.authorization_digest,
        "proof_id": authorization.proof_id,
        "provider": authorization.provider,
        "provider_event_id": authorization.provider_event_id,
        "request_shape_digest": authorization.request_shape_digest,
        "proof_target_source_digest": authorization.proof_target_source_digest,
        "consumed_at": _utc(consumed_at, "quota proof consumed_at").isoformat(),
        "maximum_request_count": 1,
        "retry_count": 0,
    }
    payload = json.dumps(
        marker, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags | nofollow, 0o600)
    except FileExistsError as exc:
        if marker_path.is_symlink():
            raise ControlledShadowAuthorizationPackageError(
                "existing quota proof consumption marker is a symlink; refusing reuse"
            ) from exc
        existing = _read_quota_proof_consumption_marker(marker_path)
        if (
            existing.get("proof_authorization_id")
            != authorization.proof_authorization_id
            or existing.get("authorization_digest")
            != authorization.authorization_digest
        ):
            raise ControlledShadowAuthorizationPackageError(
                "quota proof authorization identity conflicts with consumed state"
            ) from exc
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — proof authorization has already been consumed"
        ) from exc
    except OSError as exc:
        raise ControlledShadowAuthorizationPackageError(
            "quota proof consumption marker could not be created"
        ) from exc

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        marker_path.chmod(0o600)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        # The exclusive marker remains as a deliberate fail-closed tombstone.
        raise ControlledShadowAuthorizationPackageError(
            "quota proof consumption marker could not be committed"
        ) from exc
    return marker_path


def _target_from_payload(raw: object) -> TheRundownCanaryTargetV1:
    item = _mapping(raw, "target")
    return TheRundownCanaryTargetV1(
        provider=str(item.get("provider", "")),
        league=str(item.get("league", "")),
        fixture_key=str(item.get("fixture_key", "")),
        provider_event_id=str(item.get("provider_event_id", "")),
        home_team=str(item.get("home_team", "")),
        away_team=str(item.get("away_team", "")),
        kickoff=_timestamp(item.get("kickoff"), "target kickoff"),
    )


def _targets_from_payload(raw: object) -> tuple[TheRundownCanaryTargetV1, ...]:
    if not isinstance(raw, (tuple, list)):
        raise ControlledShadowAuthorizationPackageError("targets must be an array")
    return tuple(_target_from_payload(item) for item in raw)


def _participants_from_payload(
    raw: object,
) -> tuple[TheRundownNetworkParticipantScopeV1, ...]:
    if not isinstance(raw, (tuple, list)):
        raise ControlledShadowAuthorizationPackageError(
            "participant_scope must be an array"
        )
    values = []
    for item in raw:
        mapping = _mapping(item, "participant scope item")
        values.append(
            TheRundownNetworkParticipantScopeV1(
                fixture_key=str(mapping.get("fixture_key", "")),
                home_participant_id=str(mapping.get("home_participant_id", "")),
                away_participant_id=str(mapping.get("away_participant_id", "")),
            )
        )
    return tuple(values)


def _requests_from_payload(
    raw: object,
) -> tuple[TheRundownNetworkRequestScopeV1, ...]:
    if not isinstance(raw, (tuple, list)):
        raise ControlledShadowAuthorizationPackageError(
            "request_scope must be an array"
        )
    values = []
    for item in raw:
        mapping = _mapping(item, "request scope item")
        values.append(
            TheRundownNetworkRequestScopeV1(
                fixture_key=str(mapping.get("fixture_key", "")),
                request_identity=str(mapping.get("request_identity", "")),
            )
        )
    return tuple(values)


def _configuration_from_payload(
    raw: object,
    *,
    enabled: bool,
    configuration_digest: str | None = None,
) -> TheRundownNetworkConfigurationV1:
    mapping = _mapping(raw, "configuration")
    digest = configuration_digest or mapping.get("configuration_digest")
    if not isinstance(digest, str):
        raise ControlledShadowAuthorizationPackageError(
            "configuration_digest is missing"
        )
    configuration = TheRundownNetworkConfigurationV1(
        targets=_targets_from_payload(mapping.get("targets")),
        participant_scope=_participants_from_payload(mapping.get("participant_scope")),
        request_scope=_requests_from_payload(mapping.get("request_scope")),
        adapter_version=str(mapping.get("adapter_version", "")),
        adapter_source_sha=str(mapping.get("adapter_source_sha", "")),
        maximum_request_count=mapping.get("maximum_request_count"),  # type: ignore[arg-type]
        maximum_datapoints=mapping.get("maximum_datapoints"),  # type: ignore[arg-type]
        maximum_quota_cost_units=mapping.get("maximum_quota_cost_units"),  # type: ignore[arg-type]
        request_quota_cost_units=mapping.get("request_quota_cost_units"),  # type: ignore[arg-type]
        maximum_source_age_seconds=mapping.get("maximum_source_age_seconds"),  # type: ignore[arg-type]
        minimum_interval_seconds=mapping.get("minimum_interval_seconds", 1.1),  # type: ignore[arg-type]
        maximum_retries=mapping.get("maximum_retries", 0),  # type: ignore[arg-type]
        enabled=enabled,
        no_bet=mapping.get("no_bet", True),  # type: ignore[arg-type]
        publication=mapping.get("publication", False),  # type: ignore[arg-type]
        production_activation=mapping.get("production_activation", False),  # type: ignore[arg-type]
        monetary_spend_authorized=mapping.get("monetary_spend_authorized", False),  # type: ignore[arg-type]
        configuration_digest=digest,
    )
    return configuration


def _authorization_from_payload(
    raw: object,
) -> TheRundownNetworkAuthorizationV1:
    mapping = _mapping(raw, "CEO authorization")
    return TheRundownNetworkAuthorizationV1(
        authorization_id=str(mapping.get("authorization_id", "")),
        ceo_authorization_identity=str(mapping.get("ceo_authorization_identity", "")),
        controlled_shadow_run_id=str(mapping.get("controlled_shadow_run_id", "")),
        qualification_session_id=str(mapping.get("qualification_session_id", "")),
        provider=str(mapping.get("provider", "")),
        targets=_targets_from_payload(mapping.get("targets")),
        participant_scope=_participants_from_payload(mapping.get("participant_scope")),
        request_scope=_requests_from_payload(mapping.get("request_scope")),
        adapter_version=str(mapping.get("adapter_version", "")),
        adapter_source_sha=str(mapping.get("adapter_source_sha", "")),
        configuration_digest=str(mapping.get("configuration_digest", "")),
        maximum_request_count=mapping.get("maximum_request_count"),  # type: ignore[arg-type]
        maximum_datapoints=mapping.get("maximum_datapoints"),  # type: ignore[arg-type]
        maximum_quota_cost_units=mapping.get("maximum_quota_cost_units"),  # type: ignore[arg-type]
        request_quota_cost_units=mapping.get("request_quota_cost_units"),  # type: ignore[arg-type]
        maximum_source_age_seconds=mapping.get("maximum_source_age_seconds"),  # type: ignore[arg-type]
        issued_at=_timestamp(mapping.get("issued_at"), "issued_at"),
        expires_at=_timestamp(mapping.get("expires_at"), "expires_at"),
        quota_headroom_evidence_digest=str(
            mapping.get("quota_headroom_evidence_digest", "")
        ),
        minimum_interval_seconds=mapping.get("minimum_interval_seconds", 1.1),  # type: ignore[arg-type]
        maximum_retries=mapping.get("maximum_retries", 0),  # type: ignore[arg-type]
        no_bet=mapping.get("no_bet", True),  # type: ignore[arg-type]
        publication=mapping.get("publication", False),  # type: ignore[arg-type]
        production_activation=mapping.get("production_activation", False),  # type: ignore[arg-type]
        monetary_spend_authorized=mapping.get("monetary_spend_authorized", False),  # type: ignore[arg-type]
        schema_version=str(
            mapping.get("schema_version", NETWORK_SHADOW_SCHEMA_VERSION)
        ),
    )


def _load_execution_inputs(
    package_path: object,
    authorization_path: object,
    *,
    now: datetime,
) -> tuple[
    ControlledShadowAuthorizationPackageV1,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkAuthorizationV1,
]:
    package_payload = _read_json_file(package_path, "authorization package")
    package = ControlledShadowAuthorizationPackageV1(
        configuration_payload=_mapping(
            package_payload.get("configuration"), "package configuration"
        ),
        authorization_template=_mapping(
            package_payload.get("authorization_template"),
            "package authorization_template",
        ),
        package_digest=str(package_payload.get("package_digest", "")),
    )
    package.validate()
    package_configuration = _configuration_from_payload(
        package.configuration_payload,
        enabled=False,
    )
    package_configuration.validate()
    if package_configuration.configuration_digest != package.configuration_payload.get(
        "configuration_digest"
    ):
        raise ControlledShadowAuthorizationPackageError(
            "package configuration digest does not match the disabled configuration"
        )

    authorization_container = _read_json_file(
        authorization_path, "CEO authorization artifact"
    )
    authorization_payload = authorization_container.get("authorization")
    if not isinstance(authorization_payload, Mapping):
        authorization_payload = authorization_container
    supplied_package_digest = authorization_container.get("package_digest")
    if supplied_package_digest is None:
        supplied_package_digest = authorization_container.get(
            "authorization_package_digest"
        )
    if supplied_package_digest is None:
        supplied_package_digest = authorization_payload.get("package_digest")
    if supplied_package_digest != package.package_digest:
        raise ControlledShadowAuthorizationPackageError(
            "CEO authorization is not bound to the exact package digest"
        )
    authorization = _authorization_from_payload(authorization_payload)
    supplied_authorization_digest = authorization_payload.get("authorization_digest")
    if supplied_authorization_digest != authorization.authorization_digest:
        raise ControlledShadowAuthorizationPackageError(
            "CEO authorization digest mismatch"
        )
    execution_configuration = _configuration_from_payload(
        package.configuration_payload,
        enabled=True,
        configuration_digest=authorization.configuration_digest,
    )
    if (
        authorization.configuration_digest
        != replace(
            execution_configuration, configuration_digest=""
        ).computed_configuration_digest
    ):
        raise ControlledShadowAuthorizationPackageError(
            "authorization configuration digest does not match the enabled configuration"
        )
    execution_configuration.validate()
    authorization.validate(execution_configuration, now=now)
    if authorization.provider != CANONICAL_CANDIDATE_PROVIDER:
        raise ControlledShadowAuthorizationPackageError(
            "CEO authorization provider is not canonical"
        )
    return package, execution_configuration, authorization


def _load_quota_proof_authorization(
    path_value: object,
    *,
    now: datetime,
) -> TheRundownQuotaProofAuthorizationV1:
    container = _read_json_file(path_value, "quota proof authorization")
    payload = container.get("authorization")
    if not isinstance(payload, Mapping):
        payload = container.get("proof_authorization")
    if not isinstance(payload, Mapping):
        payload = container
    authorization = TheRundownQuotaProofAuthorizationV1.from_payload(payload)
    outer_digest = container.get("authorization_digest")
    if outer_digest is not None and outer_digest != authorization.authorization_digest:
        raise ControlledShadowAuthorizationPackageError(
            "quota proof authorization digest wrapper mismatch"
        )
    try:
        authorization.validate(now=now)
    except NetworkShadowContractError as exc:
        raise ControlledShadowAuthorizationPackageError(str(exc)) from exc
    return authorization


def _contains_provider_event_id(value: object, provider_event_id: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                key in {"event_id", "provider_event_id"}
                and str(item) == provider_event_id
            )
            or _contains_provider_event_id(item, provider_event_id)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(
            _contains_provider_event_id(item, provider_event_id) for item in value
        )
    return False


def _load_quota_proof_target_evidence(
    path_value: object,
    *,
    authorization: TheRundownQuotaProofAuthorizationV1,
) -> dict[str, object]:
    """Select one previously observed event; never perform discovery."""

    path = _absolute_path(path_value, "quota proof target evidence")
    raw = _read_json_file(path, "quota proof target evidence")
    bundle = _mapping(
        raw.get("canonical_b1_evidence_bundle"),
        "quota proof canonical target evidence",
    )
    original = _mapping(
        raw.get("original_network_capture"),
        "quota proof original target capture",
    )
    repaired = _mapping(
        raw.get("repaired_normalization"),
        "quota proof repaired target normalization",
    )
    provider_event_id = str(repaired.get("provider_event_id", ""))
    if provider_event_id != authorization.provider_event_id:
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_NO_PROOF_TARGET: target event binding mismatch"
        )
    if (
        raw.get("evidence_kind") != ObservationEvidenceKind.REAL_OBSERVED.value
        or bundle.get("capture_status") != "CAPTURED"
        or original.get("provider") != CANONICAL_CANDIDATE_PROVIDER
        or original.get("league") != "LL"
        or original.get("network_evidence_is_original") is not True
    ):
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_NO_PROOF_TARGET: local evidence is not an original real LL capture"
        )
    raw_digests = {
        str(original.get("raw_response_digest", "")),
        str(bundle.get("raw_response_digest", "")),
        str(repaired.get("raw_response_digest", "")),
    }
    if len(raw_digests) != 1 or "" in raw_digests:
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_NO_PROOF_TARGET: local target digests disagree"
        )
    replay = _mapping(raw.get("replay"), "quota proof replay metadata")
    body_path_value = replay.get("source_event_body")
    headers_path_value = replay.get("source_event_headers")
    try:
        body_path = _absolute_path(body_path_value, "quota proof source event body")
        headers_path = _absolute_path(
            headers_path_value, "quota proof source event headers"
        )
        body = _read_json_file(body_path, "quota proof source event body")
        _read_json_file(headers_path, "quota proof source event headers")
    except (ControlledShadowAuthorizationPackageError, TypeError) as exc:
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_NO_PROOF_TARGET: source capture files are unavailable"
        ) from exc
    if not _contains_provider_event_id(body, provider_event_id):
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_NO_PROOF_TARGET: source body does not contain the selected event"
        )
    observed_at = _timestamp(repaired.get("captured_at"), "proof target captured_at")
    source_digest = _digest(raw)
    if source_digest != authorization.proof_target_source_digest:
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_NO_PROOF_TARGET: target source digest mismatch"
        )
    return {
        "provider": CANONICAL_CANDIDATE_PROVIDER,
        "league": "LL",
        "provider_event_id": provider_event_id,
        "source_digest": source_digest,
        "source_path": str(path),
        "observed_at": observed_at,
    }


def _load_quota_headroom(
    path_value: object,
    *,
    package: ControlledShadowAuthorizationPackageV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
    now: datetime,
) -> TheRundownQuotaHeadroomEvidenceV1:
    raise ControlledShadowAuthorizationPackageError(
        "NO_TRUSTWORTHY_PRE_REQUEST_QUOTA_SOURCE: TheRundown exposes "
        "quota remaining only in billed response headers; no provider-native "
        "non-billable account artifact or verifier is configured"
    )


def _load_spend_control_evidence(
    path_value: object,
    *,
    now: datetime,
) -> dict[str, object]:
    """Accept only recent provider evidence proving a hard-capped free tier.

    This is deliberately not quota headroom.  It gates paid-overage exposure
    before the single billed proof request; remaining datapoints must still be
    obtained from that provider response and cannot be supplied by the caller.
    """

    try:
        raw = _read_json_file(path_value, "spend-control evidence")
        headers_raw = raw.get("headers", raw)
        headers = {
            str(key).casefold(): str(value).strip()
            for key, value in _mapping(headers_raw, "spend-control headers").items()
            if str(value).strip()
        }
        if raw.get("provider") not in (None, CANONICAL_CANDIDATE_PROVIDER):
            raise ValueError("provider mismatch")
        if headers.get("x-tier", "").casefold() != "free":
            raise ValueError("account tier is not free")
        if headers.get("x-datapoints-period", "").casefold() != "daily":
            raise ValueError("datapoint period is not daily")
        if int(headers.get("x-datapoints-limit", "-1")) != 20_000:
            raise ValueError("free-tier daily limit is not 20000")
        if int(headers.get("x-rate-limit", "-1")) != 1:
            raise ValueError("provider rate limit is not the bounded one-request rate")
        observed_raw = raw.get("observed_at") or headers.get("date")
        if not observed_raw:
            raise ValueError("observation timestamp is missing")
        if isinstance(observed_raw, str) and "," in observed_raw:
            observed = parsedate_to_datetime(observed_raw)
        else:
            observed = _timestamp(observed_raw, "spend-control observed_at")
        observed = _utc(observed, "spend-control observed_at")
        current = _utc(now, "spend-control validation now")
        if observed > current:
            raise ValueError("observation timestamp is in the future")
        if (
            current - observed
        ).total_seconds() > SPEND_CONTROL_EVIDENCE_MAXIMUM_AGE_SECONDS:
            raise ValueError("spend-control evidence is stale")
        safe = {
            key: headers[key]
            for key in (
                "x-tier",
                "x-datapoints-period",
                "x-datapoints-limit",
                "x-rate-limit",
            )
        }
        if "date" in headers:
            safe["date"] = headers["date"]
        return {
            "provider": CANONICAL_CANDIDATE_PROVIDER,
            "observed_at": observed,
            "account_tier": "free",
            "overage_exposure": "none",
            "digest": _digest(
                {
                    "provider": CANONICAL_CANDIDATE_PROVIDER,
                    "observed_at": observed,
                    "safe_headers": safe,
                }
            ),
            "safe_headers": safe,
        }
    except (ControlledShadowAuthorizationPackageError, TypeError, ValueError) as exc:
        if isinstance(exc, ControlledShadowAuthorizationPackageError) and str(
            exc
        ).startswith("TOP5_B4_QUOTA_PROOF"):
            raise
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — BLOCKED_SPEND_CONTROL: " + str(exc)
        ) from exc


def _build_quota_proof_request(
    package: ControlledShadowAuthorizationPackageV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
) -> TheRundownQuotaProofRequestV1:
    if tuple(target.league for target in configuration.targets) != TOP5_LEAGUE_ORDER:
        raise ControlledShadowAuthorizationPackageError(
            "quota proof requires the exact five-league configuration"
        )
    target = configuration.targets[0]
    request = TheRundownQuotaProofRequestV1(
        proof_id="quota-proof:"
        + _digest(
            {
                "authorization_id": authorization.authorization_id,
                "package_digest": package.package_digest,
                "event": target.provider_event_id,
            }
        )[:32],
        provider=CANONICAL_CANDIDATE_PROVIDER,
        provider_event_id=target.provider_event_id,
        authorization_package_digest=package.package_digest,
        configuration_digest=configuration.configuration_digest,
        authorization_id=authorization.authorization_id,
        controlled_shadow_run_id=authorization.controlled_shadow_run_id,
        qualification_session_id=authorization.qualification_session_id,
        ceo_authorization_identity=authorization.ceo_authorization_identity,
        adapter_version=configuration.adapter_version,
        adapter_source_sha=configuration.adapter_source_sha,
        endpoint=f"{THERUNDOWN_BASE_URL}/events/{target.provider_event_id}",
        query={
            "affiliate_ids": "19,22,23",
            "hide_closed": "true",
            "main_line": "true",
            "market_ids": "1",
        },
        request_shape_digest="0" * 64,
    )
    return replace(request, request_shape_digest=request.computed_request_shape_digest)


def _write_quota_proof(
    path_value: object,
    *,
    request: TheRundownQuotaProofRequestV1,
    evidence: TheRundownQuotaProofEvidenceV1,
    spend_control: Mapping[str, object],
) -> Path:
    path = _absolute_path(path_value, "quota proof output")
    if path.exists():
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — second quota proof output is not allowed"
        )
    payload = {
        "schema_version": "top5-therundown-quota-proof-package-v1",
        "execution_phase": "quota_proof",
        "proof": evidence.as_payload(),
        "request": {
            "proof_id": request.proof_id,
            "provider": request.provider,
            "provider_event_id": request.provider_event_id,
            "authorization_package_digest": request.authorization_package_digest,
            "configuration_digest": request.configuration_digest,
            "authorization_id": request.authorization_id,
            "controlled_shadow_run_id": request.controlled_shadow_run_id,
            "qualification_session_id": request.qualification_session_id,
            "ceo_authorization_identity": request.ceo_authorization_identity,
            "adapter_version": request.adapter_version,
            "adapter_source_sha": request.adapter_source_sha,
            "endpoint": request.endpoint,
            "query": dict(request.query),
            "request_shape_digest": request.request_shape_digest,
            "proof_target_source_digest": request.proof_target_source_digest,
            "maximum_datapoints": request.maximum_datapoints,
            "request_count": request.request_count,
            "retry_count": request.retry_count,
        },
        "spend_control": {
            "provider": spend_control["provider"],
            "account_tier": spend_control["account_tier"],
            "overage_exposure": spend_control["overage_exposure"],
            "observed_at": spend_control["observed_at"],
            "digest": spend_control["digest"],
        },
        "safety": {
            "five_league_requests": 0,
            "receipt_issued": False,
            "authority_changed": False,
            "activation": False,
            "publication": False,
            "betting": False,
            "monetary_spend_authorized": False,
        },
    }
    try:
        atomic_write_json(
            path,
            _jsonable(payload),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        path.chmod(0o600)
    except OSError as exc:
        raise ControlledShadowAuthorizationPackageError(
            "quota proof output could not be written"
        ) from exc
    reloaded = _read_json_file(path, "quota proof output")
    reloaded_evidence = TheRundownQuotaProofEvidenceV1.from_payload(
        reloaded.get("proof")
    )
    reloaded_evidence.validate(
        request=request, now=reloaded_evidence.response_finished_at
    )
    if reloaded_evidence.evidence_digest != evidence.evidence_digest:
        raise ControlledShadowAuthorizationPackageError(
            "quota proof output digest changed during reload"
        )
    return path


def run_guarded_quota_proof(
    proof_authorization_path: object,
    proof_target_evidence_path: object,
    *,
    spend_control_evidence_path: object,
    credential_file: object = DEFAULT_THERUNDOWN_CREDENTIAL_PATH,
    output_path: object,
    clock: Any = None,
    http_client: Any = None,
) -> dict[str, object]:
    """Perform only the one billed proof transaction; never start league execution."""

    clock_fn = clock or (lambda: datetime.now(timezone.utc))
    now = _utc(clock_fn(), "quota proof now")
    proof_authorization = _load_quota_proof_authorization(
        proof_authorization_path,
        now=now,
    )
    target = _load_quota_proof_target_evidence(
        proof_target_evidence_path,
        authorization=proof_authorization,
    )
    spend_control = _load_spend_control_evidence(
        spend_control_evidence_path,
        now=now,
    )
    output = _absolute_path(output_path, "quota proof output")
    if output.exists():
        raise ControlledShadowAuthorizationPackageError(
            "TOP5_B4_QUOTA_PROOF — second quota proof output is not allowed"
        )
    proof_configuration_digest = _digest(
        {
            "schema_version": "top5-therundown-quota-proof-configuration-v1",
            "proof_authorization_digest": proof_authorization.authorization_digest,
            "request_shape_digest": proof_authorization.request_shape_digest,
            "proof_target_source_digest": target["source_digest"],
        }
    )
    request = proof_authorization.request_for_proof(
        proof_configuration_digest=proof_configuration_digest
    )
    _consume_quota_proof_authorization(
        proof_authorization,
        consumed_at=now,
    )
    api_key = _read_protected_therundown_credential(credential_file)
    evidence = execute_therundown_quota_proof(
        request,
        api_key=api_key,
        http_client=http_client,
        now=_utc(clock_fn(), "quota proof response validation now"),
    )
    artifact_path = _write_quota_proof(
        output,
        request=request,
        evidence=evidence,
        spend_control=spend_control,
    )
    return {
        "status": "TOP5_B4_QUOTA_PROOF — QUOTA_CONFIRMED",
        "artifact_path": str(artifact_path),
        "proof_id": evidence.proof_id,
        "proof_authorization_id": proof_authorization.proof_authorization_id,
        "provider": evidence.provider,
        "selected_proof_target": target["provider_event_id"],
        "proof_target_source_digest": target["source_digest"],
        "proof_target_source_path": target["source_path"],
        "request_count": evidence.request_count,
        "billed_datapoints": evidence.billed_datapoints,
        "remaining_datapoints": evidence.remaining_datapoints,
        "quota_limit_datapoints": evidence.quota_limit_datapoints,
        "quota_period": evidence.quota_period,
        "proof_digest": evidence.evidence_digest,
        "response_digest": evidence.response_digest,
        "request_shape_digest": evidence.request_shape_digest,
        "spend_control_digest": spend_control["digest"],
        "five_league_requests": 0,
        "next_step": "STOP_FOR_NEW_CEO_AUTHORIZATION",
        "safety": {
            "receipt_issued": False,
            "authority_changed": False,
            "activation": False,
            "publication": False,
            "betting": False,
            "monetary_spend_authorized": False,
        },
    }


def _validate_b1_ll_artifact_shape(artifact: Mapping[str, object]) -> None:
    """Validate the serialized shape without inventing B1 authority."""

    raw = _materialize_b1_ll_artifact(artifact)
    if raw.get("schema_version") == SAME_RUN_LL_VALIDATION_SCHEMA:
        required = (
            "validation_status",
            "provider_identity",
            "league",
            "canonical_fixture_key",
            "provider_event_id",
            "provider_request_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_id",
            "participant_ids",
            "bookmaker_identity",
            "source_identity",
            "home_odds",
            "draw_odds",
            "away_odds",
            "source_timestamp",
            "captured_at",
            "request_started_at",
            "request_finished_at",
            "adapter_version",
            "adapter_source_sha",
            "raw_response_digest",
            "provider_record_digest",
            "normalized_record_digest",
            "normalized_observations",
            "quota_before",
            "quota_after",
            "datapoint_count",
            "quota_cost_units",
            "validation_digest",
        )
        missing = [name for name in required if name not in raw]
        if missing:
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 validation is missing: " + ", ".join(missing)
            )
        if raw.get("validation_status") != "VALIDATED":
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 validation is not valid"
            )
        if raw.get("provider_identity") != CANONICAL_CANDIDATE_PROVIDER:
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 provider identity is not canonical"
            )
        if raw.get("league") != "LL":
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 league is not LL"
            )
        if not isinstance(raw.get("participant_ids"), Mapping):
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 participant IDs are missing"
            )
        if not isinstance(raw.get("normalized_observations"), list):
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 normalized observations are missing"
            )
        for name, expected in (
            ("network_execution", True),
            ("evidence_kind", ObservationEvidenceKind.REAL_OBSERVED.value),
            ("b1_authority_issued", False),
            ("receipt_issued", False),
            ("no_bet", True),
            ("publication", False),
            ("production_activation", False),
            ("monetary_spend_authorized", False),
        ):
            if raw.get(name) is not expected:
                raise ControlledShadowAuthorizationPackageError(
                    f"same-run B1 safety field {name} is unsafe"
                )
        if _digest_value(
            raw.get("validation_digest"), "same-run B1 validation digest"
        ) != _digest(
            {key: value for key, value in raw.items() if key != "validation_digest"}
        ):
            raise ControlledShadowAuthorizationPackageError(
                "same-run B1 validation digest mismatch"
            )
        return
    if raw.get("schema_version") != "top5-therundown-ll-evidence-bundle-v1":
        raise ControlledShadowAuthorizationPackageError(
            "B1 La Liga artifact schema is unsupported"
        )
    if raw.get("capture_status") != "CAPTURED":
        raise ControlledShadowAuthorizationPackageError(
            "B1 La Liga artifact is not a captured observation"
        )
    if not isinstance(raw.get("b1_bridge_inputs"), Mapping):
        raise ControlledShadowAuthorizationPackageError("B1 bridge inputs are missing")
    if not isinstance(raw.get("bookmaker_observations"), (tuple, list)):
        raise ControlledShadowAuthorizationPackageError(
            "B1 bookmaker observations are missing"
        )
    safety = _mapping(raw.get("safety"), "B1 safety")
    for name, expected in (
        ("candidate_only", True),
        ("quality_eligible", False),
        ("no_bet", True),
        ("no_publication", True),
        ("no_activation", True),
        ("no_spend", True),
    ):
        if safety.get(name) is not expected:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 safety field {name} is unsafe"
            )


def _validate_b1_ll_artifact(
    artifact: object,
    *,
    capture: TheRundownNetworkShadowCaptureV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
    now: datetime,
) -> dict[str, object]:
    """Bind B1's repaired LL bundle to the exact B4 LL capture.

    B1 owns provider-side capture input only.  It must not supply a cascade
    digest or a canonical attestation; those remain properties of the PR-103
    completed run and are checked by ``_validate_capture``.
    """

    raw = dict(_materialize_b1_ll_artifact(artifact))
    if raw.get("schema_version") == SAME_RUN_LL_VALIDATION_SCHEMA:
        try:
            expected = validate_same_run_ll_capture(
                target=capture.target,
                request=capture.request,
                response=capture.response,
                authorization=authorization,
                now=now,
                maximum_source_age_seconds=configuration.maximum_source_age_seconds,
            )
        except Exception as exc:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 same-run LL validation failed: {exc}"
            ) from exc
        if raw != expected:
            raise ControlledShadowAuthorizationPackageError(
                "B1 same-run validation attestation does not match the genuine LL capture"
            )
        return raw
    if raw.get("schema_version") != "top5-therundown-ll-evidence-bundle-v1":
        raise ControlledShadowAuthorizationPackageError(
            "B1 La Liga artifact schema is unsupported"
        )
    if raw.get("capture_status") != "CAPTURED":
        raise ControlledShadowAuthorizationPackageError(
            "B1 La Liga artifact is not a captured observation"
        )
    safety = _mapping(raw.get("safety"), "B1 safety")
    for name, expected in (
        ("candidate_only", True),
        ("quality_eligible", False),
        ("no_bet", True),
        ("no_publication", True),
        ("no_activation", True),
        ("no_spend", True),
    ):
        if safety.get(name) is not expected:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 safety field {name} is unsafe"
            )
    bridge = _mapping(raw.get("b1_bridge_inputs"), "B1 bridge inputs")
    if bridge.get("evidence_kind") != ObservationEvidenceKind.REAL_OBSERVED.value:
        raise ControlledShadowAuthorizationPackageError(
            "B1 synthetic or replay evidence cannot enter reconciliation"
        )
    if bridge.get("market_phase") != "PRE_MATCH" or bridge.get("market_type") != (
        "football:pre_match:1x2"
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact must be pre-match regulation 1X2"
        )
    if bridge.get("network_execution") is not True:
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact must record network execution"
        )
    if bridge.get("cascade_evidence_digest") is not None:
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact cannot self-supply cascade evidence"
        )
    if bridge.get("cascade_evidence_required_from_b4") is not True:
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact must require B4 cascade evidence"
        )
    if bridge.get("capture_attestation_required_from_b4") is not True:
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact must require B4 capture attestation"
        )
    if bridge.get("provider_identity") != CANONICAL_CANDIDATE_PROVIDER:
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact provider identity is not canonical"
        )
    if bridge.get("league") not in ("LL", "ESP1"):
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact is not the canonical La Liga league"
        )

    b1_authorization = _mapping(raw.get("authorization"), "B1 authorization")
    if b1_authorization.get("provider") != CANONICAL_CANDIDATE_PROVIDER:
        raise ControlledShadowAuthorizationPackageError(
            "B1 authorization provider identity is not canonical"
        )
    if b1_authorization.get("league") not in ("LL", "ESP1"):
        raise ControlledShadowAuthorizationPackageError(
            "B1 authorization league is not La Liga"
        )
    for name, expected in (
        ("controlled_shadow_run_id", authorization.controlled_shadow_run_id),
        ("qualification_session_id", authorization.qualification_session_id),
        ("ceo_authorization_id", authorization.authorization_id),
    ):
        if bridge.get(name) != expected:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 authorization binding mismatch: {name}"
            )
    for name, expected in (
        ("controlled_shadow_run_id", authorization.controlled_shadow_run_id),
        ("qualification_session_id", authorization.qualification_session_id),
        ("ceo_authorization_id", authorization.authorization_id),
    ):
        if b1_authorization.get(name) != expected:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 nested authorization mismatch: {name}"
            )
    if _timestamp(
        b1_authorization.get("expires_at"), "B1 authorization expiry"
    ) != _utc(authorization.expires_at, "authorization expiry"):
        raise ControlledShadowAuthorizationPackageError(
            "B1 authorization expiry does not match the run authorization"
        )
    for name, expected in (
        ("no_bet", True),
        ("no_publication", True),
        ("no_activation", True),
        ("no_spend", True),
    ):
        if b1_authorization.get(name) is not expected:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 authorization safety field {name} is unsafe"
            )

    target = capture.target
    request = capture.request
    response = capture.response
    bridge_kickoff = _timestamp(bridge.get("kickoff"), "B1 kickoff")
    derived_fixture_key = make_fixture_key(
        str(bridge.get("league")),
        str(bridge.get("home_team")),
        str(bridge.get("away_team")),
        bridge_kickoff,
    )
    if derived_fixture_key != target.fixture_key:
        raise ControlledShadowAuthorizationPackageError(
            "B1 canonical fixture identity does not match the LL target"
        )
    if (
        not isinstance(bridge.get("fixture_key"), str)
        or not bridge["fixture_key"].strip()
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider fixture identity is missing"
        )
    expected_provider_fixture_key = (
        f"therundown:{bridge.get('league')}:{bridge.get('provider_event_id')}"
    )
    if bridge.get("fixture_key") != expected_provider_fixture_key:
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider fixture identity does not match the provider event"
        )
    exact_fields = (
        (
            "provider_event_id",
            bridge.get("provider_event_id"),
            response.provider_event_id,
        ),
        (
            "provider_request_id",
            bridge.get("provider_request_id"),
            request.request_identity,
        ),
        ("home_team", bridge.get("home_team"), target.home_team),
        ("away_team", bridge.get("away_team"), target.away_team),
        ("adapter_version", bridge.get("adapter_version"), response.adapter_version),
        (
            "adapter_source_sha",
            bridge.get("adapter_source_sha"),
            response.adapter_source_sha,
        ),
        (
            "raw_response_digest",
            bridge.get("raw_response_digest"),
            response.raw_response_digest,
        ),
    )
    for name, actual, expected in exact_fields:
        if actual != expected:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 LL binding mismatch: {name}"
            )
    if raw.get("raw_response_digest") != response.raw_response_digest:
        raise ControlledShadowAuthorizationPackageError(
            "B1 top-level raw response digest does not match the LL capture"
        )
    if raw.get("adapter_version") != response.adapter_version:
        raise ControlledShadowAuthorizationPackageError(
            "B1 top-level adapter version does not match the LL capture"
        )
    if raw.get("adapter_source_sha") != response.adapter_source_sha:
        raise ControlledShadowAuthorizationPackageError(
            "B1 top-level adapter source SHA does not match the LL capture"
        )
    if _timestamp(bridge.get("kickoff"), "B1 kickoff") != _utc(
        target.kickoff, "target kickoff"
    ):
        raise ControlledShadowAuthorizationPackageError("B1 kickoff mismatch")
    participant_ids = _mapping(bridge.get("participant_ids"), "B1 participant IDs")
    if (
        participant_ids.get("home") != request.home_participant_id
        or participant_ids.get("away") != request.away_participant_id
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 participant binding mismatch"
        )
    if not participant_ids.get("draw"):
        raise ControlledShadowAuthorizationPackageError(
            "B1 draw participant binding is missing"
        )
    if _timestamp(bridge.get("captured_at"), "B1 captured_at") != _utc(
        response.captured_at, "response captured_at"
    ):
        raise ControlledShadowAuthorizationPackageError("B1 capture timestamp mismatch")
    source_timestamps = bridge.get("source_timestamps")
    if not isinstance(source_timestamps, (tuple, list)) or not source_timestamps:
        raise ControlledShadowAuthorizationPackageError(
            "B1 source timestamp provenance is missing"
        )
    source_timestamp = _utc(response.source_timestamp, "response source_timestamp")
    if source_timestamp not in {
        _timestamp(value, "B1 source timestamp") for value in source_timestamps
    }:
        raise ControlledShadowAuthorizationPackageError(
            "B1 source timestamp does not match the LL capture"
        )
    if (
        now - source_timestamp
    ).total_seconds() > configuration.maximum_source_age_seconds:
        raise ControlledShadowAuthorizationPackageError(
            "B1 source observation is stale"
        )
    _digest_value(bridge.get("raw_response_digest"), "B1 raw response digest")
    provider_digests = bridge.get("provider_record_digests")
    normalized_digests = bridge.get("normalized_record_digests")
    if not isinstance(provider_digests, (tuple, list)) or not isinstance(
        normalized_digests, (tuple, list)
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider/normalized digest provenance is missing"
        )
    if response.provider_record_digest not in provider_digests:
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider record digest does not match the LL capture"
        )
    if response.normalized_record_digest not in normalized_digests:
        raise ControlledShadowAuthorizationPackageError(
            "B1 normalized record digest does not match the LL capture"
        )

    bookmaker_observations = raw.get("bookmaker_observations")
    if (
        not isinstance(bookmaker_observations, (tuple, list))
        or not bookmaker_observations
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 bookmaker observations are missing"
        )
    matching_bookmaker = False
    for item in bookmaker_observations:
        bookmaker = _mapping(item, "B1 bookmaker observation")
        if not bookmaker.get("bookmaker_id") or not bookmaker.get("bookmaker_name"):
            raise ControlledShadowAuthorizationPackageError(
                "B1 bookmaker identity is incomplete"
            )
        odds = _mapping(bookmaker.get("odds"), "B1 bookmaker odds")
        prices = (
            _price(odds.get("home"), "B1 home odds"),
            _price(odds.get("draw"), "B1 draw odds"),
            _price(odds.get("away"), "B1 away odds"),
        )
        bookmaker_source = _timestamp(
            bookmaker.get("source_timestamp"), "B1 bookmaker source timestamp"
        )
        bookmaker_captured = _timestamp(
            bookmaker.get("captured_at"), "B1 bookmaker captured_at"
        )
        _digest_value(
            bookmaker.get("provider_record_digest"),
            "B1 bookmaker provider record digest",
        )
        _digest_value(
            bookmaker.get("normalized_record_digest"),
            "B1 bookmaker normalized record digest",
        )
        if (
            bookmaker.get("bookmaker_name") == response.bookmaker_identity
            and prices == (response.home_odds, response.draw_odds, response.away_odds)
            and bookmaker_source == source_timestamp
            and bookmaker_captured == _utc(response.captured_at, "response captured_at")
            and bookmaker.get("provider_record_digest")
            == response.provider_record_digest
            and bookmaker.get("normalized_record_digest")
            == response.normalized_record_digest
        ):
            matching_bookmaker = True
    if not matching_bookmaker:
        raise ControlledShadowAuthorizationPackageError(
            "B1 bookmaker/price evidence does not match the LL capture"
        )
    for name in ("quota_before", "quota_after", "rate_limit_state", "quota_evidence"):
        if not isinstance(bridge.get(name), Mapping):
            raise ControlledShadowAuthorizationPackageError(
                f"B1 {name} provenance is missing"
            )
    return raw


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
            "quota_headroom_evidence_digest",
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
        "quota_headroom_evidence_digest": None,
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
    b1_ll_artifact: Mapping[str, object] | None = None
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
        if self.b1_ll_artifact is not None:
            _validate_b1_ll_artifact_shape(self.b1_ll_artifact)

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
            "b1_ll_artifact": (
                dict(self.b1_ll_artifact) if self.b1_ll_artifact is not None else None
            ),
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
            capture,
            now=now,
            maximum_source_age_seconds=configuration.maximum_source_age_seconds,
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
    b1_ll_artifact: object | None = None,
) -> FiveLeagueReconciliationV1:
    """Validate a completed network result into B1/B2 evidence inputs.

    ``b1_ll_artifact`` is the exact mapping returned by B1's repaired
    ``LaLigaCaptureEvidence.as_evidence_bundle()``.  When supplied, it is
    bound to the LL capture in this result; it cannot replace the PR-103
    capture attestation or create qualification authority.
    """

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
    validated_b1_ll_artifact = None
    if b1_ll_artifact is not None:
        validated_b1_ll_artifact = _validate_b1_ll_artifact(
            b1_ll_artifact,
            capture=captures_by_league["LL"],
            configuration=configuration,
            authorization=authorization,
            now=now,
        )
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
        b1_ll_artifact=validated_b1_ll_artifact,
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


def reconcile_controlled_shadow_run_with_b1_ll_artifact(
    result: TheRundownNetworkShadowRunResultV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
    b1_ll_artifact: object,
    *,
    now: datetime,
) -> FiveLeagueReconciliationV1:
    """Run the final offline five-league path with B1's LL input required."""

    if b1_ll_artifact is None:
        raise ControlledShadowAuthorizationPackageError(
            "final five-league reconciliation requires the B1 LL artifact"
        )
    return reconcile_controlled_shadow_run(
        result,
        configuration,
        authorization,
        now=now,
        b1_ll_artifact=b1_ll_artifact,
    )


def _b2_observation_digest(observation: RealProviderObservation) -> str:
    payload = observation.as_payload()
    payload.pop("capture_attestation", None)
    return semantic_digest(payload)


def _b2_cascade_for_capture(
    capture: TheRundownNetworkShadowCaptureV1,
) -> CascadeEvidence:
    """Project one validated network capture into the canonical B2 cascade."""

    response = capture.response
    request = capture.request
    target = capture.target
    required_timestamps = (
        response.request_started_at,
        response.request_finished_at,
        response.captured_at,
        response.source_timestamp,
    )
    if any(value is None for value in required_timestamps):
        raise ControlledShadowAuthorizationPackageError(
            "B2 cascade projection requires complete timestamp provenance"
        )
    start = response.request_started_at
    end = response.request_finished_at
    captured = response.captured_at
    if start is None or end is None or captured is None:
        raise ControlledShadowAuthorizationPackageError(
            "B2 cascade projection requires request/capture timestamps"
        )
    attempt = CascadeAttempt(
        league=target.league,
        fixture_key=target.fixture_key,
        home_team=target.home_team,
        away_team=target.away_team,
        kickoff=target.kickoff,
        configured_provider_order=(CANONICAL_CANDIDATE_PROVIDER,),
        provider_attempt_index=0,
        fallback_depth=0,
        provider_identity=CANONICAL_CANDIDATE_PROVIDER,
        network_called=True,
        start_timestamp=start,
        end_timestamp=end,
        capture_timestamp=captured,
        outcome=CascadeOutcome.SUCCESS,
        failure_classification=None,
        market_type="h2h_1x2",
        home_odds=response.home_odds,
        draw_odds=response.draw_odds,
        away_odds=response.away_odds,
        bookmaker_identity=response.bookmaker_identity,
        source_identity=response.source_identity,
        market_phase=MarketPhase.PRE_MATCH,
        source_timestamp=response.source_timestamp,
        request_latency_ms=max(0, round((end - start).total_seconds() * 1000)),
        quota_before=CascadeQuotaSnapshot(
            authenticated=True,
            quota_used=None,
            quota_remaining=response.quota_before,
        ),
        quota_after=CascadeQuotaSnapshot(
            authenticated=True,
            quota_used=None,
            quota_remaining=response.quota_after,
        ),
        preflight_allowed=True,
        budget_decision=BudgetDecision.ALLOWED,
        request_cost_classification=RequestCostClassification.QUOTA_CONSUMING_REQUEST,
        network_request_count=1,
        quota_cost_units=response.quota_cost_units,
        credentials_available=True,
        provider_record_id=response.provider_event_id,
        adapter_version=response.adapter_version,
        raw_record_digest=response.raw_response_digest,
        request_identity=request.request_identity,
        provider_readiness_state=ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
        source_timing_provenance="SOURCE_TIMESTAMP",
    )
    provenance = CascadeProvenance(
        evidence_id=f"b4-cascade:{request.request_identity}",
        artifact_id=f"b4-capture:{response.provider_event_id}",
        artifact_sha=response.raw_response_digest,
        source_sha=response.adapter_source_sha,
        research_sha=FROZEN_RESEARCH_SHA,
        candidate_id="top5-controlled-shadow",
        model_identity="unbound-model-slot",
        generated_at=captured,
    )
    cascade = CascadeEvidence.from_attempts(
        provenance=provenance,
        attempts=(attempt,),
        selected_provider=CANONICAL_CANDIDATE_PROVIDER,
        prediction_input_allowed=True,
        safety=CascadeSafety(True, False, False, False, False, False, False),
    )
    cascade.validate_structural()
    return cascade


def _b2_manifest_for_capture(
    capture: TheRundownNetworkShadowCaptureV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
) -> tuple[
    TheRundownNetworkShadowCaptureV1,
    Builder2QualificationIntakeManifestV1,
]:
    """Build one canonical B2 manifest without creating authority."""

    response = capture.response
    request = capture.request
    target = capture.target
    if capture.observation_id is None:
        raise ControlledShadowAuthorizationPackageError(
            "successful capture is missing an observation ID"
        )
    cascade = _b2_cascade_for_capture(capture)
    cascade_digest = evidence_digest(cascade)
    attestation = ControlledShadowCaptureAttestation.from_payload(
        capture.canonical_capture_attestation
    )
    attestation = replace(attestation, cascade_evidence_digest=cascade_digest)
    attestation.validate()
    if response.captured_at is None:
        raise ControlledShadowAuthorizationPackageError(
            "successful capture is missing captured_at"
        )
    observation = RealProviderObservation(
        observation_id=capture.observation_id,
        qualification_session_id=request.qualification_session_id,
        evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
        provider_identity=target.provider,
        provider_event_id=response.provider_event_id,
        provider_request_id=response.provider_request_id,
        league=target.league,
        fixture_key=target.fixture_key,
        home_team=target.home_team,
        away_team=target.away_team,
        kickoff=target.kickoff,
        market_type="h2h_1x2",
        market_phase=MarketPhase.PRE_MATCH.value,
        home_odds=response.home_odds,
        draw_odds=response.draw_odds,
        away_odds=response.away_odds,
        bookmaker_identity=response.bookmaker_identity,
        source_identity=response.source_identity,
        source_timestamp=response.source_timestamp,
        provider_timestamp_provenance=response.provider_timestamp_provenance,
        captured_at=response.captured_at,
        request_started_at=response.request_started_at,
        request_finished_at=response.request_finished_at,
        latency_ms=max(
            0,
            round(
                (
                    response.request_finished_at - response.request_started_at
                ).total_seconds()
                * 1000
            ),
        ),
        adapter_version=response.adapter_version,
        adapter_source_sha=response.adapter_source_sha,
        raw_response_digest=response.raw_response_digest,
        normalized_record_digest=response.normalized_record_digest,
        cascade_evidence=cascade,
        quota_before=response.quota_before,
        quota_after=response.quota_after,
        quota_cost_units=response.quota_cost_units,
        network_request_count=1,
        capture_attestation=attestation,
    )
    observation.validate_structural()
    observation_digest = _b2_observation_digest(observation)
    attestation_payload = attestation.as_payload()
    canonical_response = replace(
        response,
        cascade_evidence_digest=cascade_digest,
    )
    canonical_capture = replace(
        capture,
        response=canonical_response,
        observation_digest=observation_digest,
        capture_attestation_digest=semantic_digest(attestation_payload),
        capture_attestation_input=attestation_payload,
        observation_input=observation.as_payload(),
    )
    canonical_capture.validate()
    eligibility = CandidateProviderEligibilityV1.from_network_capture(
        canonical_capture,
        now=response.captured_at,
        maximum_source_age_seconds=configuration.maximum_source_age_seconds,
    )
    timing_policy = QualificationTimingPolicy(
        maximum_odds_age_seconds=configuration.maximum_source_age_seconds,
        kickoff_tolerance_seconds=B2_SHADOW_TIMING_KICKOFF_TOLERANCE_SECONDS,
        minimum_lead_seconds=B2_SHADOW_TIMING_MINIMUM_LEAD_SECONDS,
        maximum_lead_seconds=B2_SHADOW_TIMING_MAXIMUM_LEAD_SECONDS,
    )
    session = ProviderQualificationSession(
        qualification_session_id=request.qualification_session_id,
        schema_version=QUALIFICATION_CONTRACT_VERSION,
        created_at=response.captured_at,
        provider_identity="top5_cascade",
        league_scope=(target.league,),
        fixture_scope=(target.fixture_key,),
        configured_provider_order=(CANONICAL_CANDIDATE_PROVIDER,),
        adapter_version=response.adapter_version,
        adapter_source_sha=response.adapter_source_sha,
        qualification_state=ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
        network_request_count=1,
        selected_observation_network_request_count=1,
        cascade_network_request_count=1,
        quota_units_observed=response.quota_cost_units,
    )
    b2_authorization = CEOAuthorization(
        authorization_id=request.authorization_id,
        controlled_shadow_run_id=request.controlled_shadow_run_id,
        qualification_session_id=request.qualification_session_id,
        provider_scope=(CANONICAL_CANDIDATE_PROVIDER,),
        league_scope=(target.league,),
        fixture_scope=(target.fixture_key,),
        maximum_network_requests=1,
        monetary_spend_authorized=False,
        issued_at=authorization.issued_at,
        expires_at=authorization.expires_at,
    )
    source_artifact = Builder2QualificationSourceArtifactV1(
        role="network-capture",
        path=(
            "/private/tmp/top5-b4-capture-"
            f"{target.league.lower()}-{_digest(request.request_identity)[:16]}.json"
        ),
        digest=response.raw_response_digest,
    )
    manifest = Builder2QualificationIntakeManifestV1(
        schema_version=INTAKE_CONTRACT_VERSION,
        intake_id=(
            f"top5-{target.league.lower()}-{_digest(request.request_identity)[:16]}"
        ),
        controlled_shadow_run_id=request.controlled_shadow_run_id,
        qualification_session_id=request.qualification_session_id,
        ceo_authorization_id=request.authorization_id,
        fixture_key=target.fixture_key,
        provider_identity=target.provider,
        provider_event_id=response.provider_event_id,
        provider_request_id=response.provider_request_id,
        observation_id=capture.observation_id,
        observation_digest=observation_digest,
        normalized_record_digest=response.normalized_record_digest,
        cascade_evidence_digest=cascade_digest,
        capture_attestation_digest=semantic_digest(attestation_payload),
        adapter_version=response.adapter_version,
        adapter_source_sha=response.adapter_source_sha,
        timing_policy_reference="top5-controlled-shadow-timing-v1",
        readiness_reference="top5-controlled-shadow-provider-readiness-v1",
        source_artifacts=(source_artifact,),
        observation=observation,
        session=session,
        authorization=b2_authorization,
        cascade_evidence=cascade,
        capture_attestation=attestation,
        timing_policy=timing_policy,
        provider_readiness={
            CANONICAL_CANDIDATE_PROVIDER: ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
        },
        candidate_provider_eligibility=eligibility,
    )
    manifest.validate()
    return canonical_capture, manifest


def _build_b2_shadow_package(
    result: TheRundownNetworkShadowRunResultV1,
    reconciliation: FiveLeagueReconciliationV1,
    configuration: TheRundownNetworkConfigurationV1,
    authorization: TheRundownNetworkAuthorizationV1,
) -> Builder2FiveLeagueShadowPackageV1:
    """Materialize the exact merged B2 package from a completed B4 result."""

    if reconciliation.receipt_eligible or reconciliation.authority_changed:
        raise ControlledShadowAuthorizationPackageError(
            "B4 reconciliation carries unsafe downstream authority"
        )
    if tuple(capture.target.league for capture in result.captures) != TOP5_LEAGUE_ORDER:
        raise ControlledShadowAuthorizationPackageError(
            "B2 package requires the canonical five-league capture order"
        )
    canonical_captures: list[TheRundownNetworkShadowCaptureV1] = []
    manifests: list[Builder2QualificationIntakeManifestV1] = []
    for capture in result.captures:
        canonical_capture, manifest = _b2_manifest_for_capture(
            capture, configuration, authorization
        )
        canonical_captures.append(canonical_capture)
        manifests.append(manifest)
    canonical_run = replace(result, captures=tuple(canonical_captures))
    canonical_run.validate()
    package = build_five_league_shadow_package(canonical_run, tuple(manifests))
    package.validate()
    return package


def _write_and_reload_b2_shadow_package(
    path_value: object, package: Builder2FiveLeagueShadowPackageV1
) -> Path:
    try:
        path = _safe_external_path(path_value, "output")
    except Builder2QualificationIntakeError as exc:
        raise ControlledShadowAuthorizationPackageError(str(exc)) from exc
    package_payload = package.as_payload()
    if path.exists():
        existing = load_five_league_shadow_package(path)
        if existing.package_digest != package.package_digest:
            raise ControlledShadowAuthorizationPackageError(
                "output contains a conflicting canonical B2 shadow package"
            )
        return path
    try:
        atomic_write_json(
            path,
            _jsonable(package_payload),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        path.chmod(0o600)
    except OSError as exc:
        raise ControlledShadowAuthorizationPackageError(
            "canonical B2 shadow package could not be written"
        ) from exc
    reloaded = load_five_league_shadow_package(path)
    if (
        reloaded.package_id != package.package_id
        or reloaded.package_digest != package.package_digest
    ):
        raise ControlledShadowAuthorizationPackageError(
            "written B2 shadow package did not survive canonical reload"
        )
    return path


def run_guarded_network_execution(
    package_path: object,
    authorization_path: object,
    *,
    quota_headroom_path: object,
    credential_file: object = DEFAULT_THERUNDOWN_CREDENTIAL_PATH,
    output_path: object,
    endpoint: str = THERUNDOWN_BASE_URL,
    clock: Any = None,
    pacer: Any = None,
    transport: Any = None,
) -> dict[str, object]:
    """Execute one explicitly enabled, fully bound network shadow.

    ``transport`` is an internal injected seam for offline tests only. The
    canonical operator command leaves it unset and therefore uses exactly the
    reviewed ``TheRundownHttpNetworkTransportV1`` implementation.
    """

    clock_fn = clock or (lambda: datetime.now(timezone.utc))
    now = _utc(clock_fn(), "execution now")
    package, configuration, authorization = _load_execution_inputs(
        package_path,
        authorization_path,
        now=now,
    )
    quota_headroom = _load_quota_headroom(
        quota_headroom_path,
        package=package,
        configuration=configuration,
        authorization=authorization,
        now=now,
    )
    api_key = _read_protected_therundown_credential(credential_file)
    adapter = TheRundownCanonicalPayloadAdapterV1(
        adapter_version=configuration.adapter_version,
        adapter_source_sha=configuration.adapter_source_sha,
        maximum_source_age_seconds=configuration.maximum_source_age_seconds,
    )
    if transport is None:
        transport = TheRundownHttpNetworkTransportV1(
            endpoint=endpoint,
            api_key=api_key,
            adapter=adapter,
            clock=clock_fn,
        )
    same_run_b1_validation: dict[str, object] | None = None

    def validate_b1_after_capture(capture: TheRundownNetworkShadowCaptureV1) -> None:
        nonlocal same_run_b1_validation
        if capture.target.league != "LL":
            return
        same_run_b1_validation = validate_same_run_ll_capture(
            target=capture.target,
            request=capture.request,
            response=capture.response,
            authorization=authorization,
            now=_utc(clock_fn(), "B1 same-run validation now"),
            maximum_source_age_seconds=configuration.maximum_source_age_seconds,
        )

    result = TheRundownNetworkShadowExecutorV1(
        clock=clock_fn,
        pacer=pacer,
        allow_live_network=True,
        fail_closed_immediately=True,
    ).run(
        configuration,
        authorization,
        transport=transport,
        quota_headroom=quota_headroom,
        post_capture_validator=validate_b1_after_capture,
    )
    if (
        result.status is not NetworkShadowRunStatus.COMPLETED_NETWORK
        or not result.all_five_succeeded
    ):
        raise ControlledShadowAuthorizationPackageError(
            "network shadow did not complete all five leagues: "
            + "; ".join(result.failures)
        )
    if same_run_b1_validation is None:
        raise ControlledShadowAuthorizationPackageError(
            "B1 same-run validation did not run for the LL capture"
        )
    reconciliation = reconcile_controlled_shadow_run_with_b1_ll_artifact(
        result,
        configuration,
        authorization,
        same_run_b1_validation,
        now=_utc(clock_fn(), "reconciliation now"),
    )
    b2_package = _build_b2_shadow_package(
        result,
        reconciliation,
        configuration,
        authorization=authorization,
    )
    artifact_path = _write_and_reload_b2_shadow_package(output_path, b2_package)
    reloaded_package = load_five_league_shadow_package(artifact_path)
    return {
        "status": reloaded_package.shadow_run.status.value,
        "artifact_path": str(artifact_path),
        "schema_version": reloaded_package.schema_version,
        "package_id": reloaded_package.package_id,
        "package_digest": reloaded_package.package_digest,
        "provider": CANONICAL_CANDIDATE_PROVIDER,
        "authorization_digest": authorization.authorization_digest,
        "configuration_digest": configuration.configuration_digest,
        "controlled_shadow_run_id": reloaded_package.shadow_run.controlled_shadow_run_id,
        "qualification_session_id": reloaded_package.shadow_run.qualification_session_id,
        "authorization_id": reloaded_package.shadow_run.authorization_id,
        "request_count": reloaded_package.shadow_run.request_count,
        "datapoint_count": reloaded_package.shadow_run.datapoint_count,
        "quota_cost_units": reloaded_package.shadow_run.quota_cost_units,
        "capture_count": len(reloaded_package.shadow_run.captures),
        "manifest_count": len(reloaded_package.manifests),
        "reconciliation_digest": reconciliation.reconciliation_digest,
        "network_calls": reloaded_package.shadow_run.request_count,
        "safety": {
            "receipt_issued": False,
            "authority_changed": False,
            "publication": False,
            "production_activation": False,
            "betting": False,
            "monetary_spend_authorized": False,
        },
    }


def run_guarded_network_preflight(
    package_path: object,
    authorization_path: object,
    *,
    quota_headroom_path: object,
    credential_file: object = DEFAULT_THERUNDOWN_CREDENTIAL_PATH,
    clock: Any = None,
) -> dict[str, object]:
    """Validate the complete execution envelope while making zero calls."""

    clock_fn = clock or (lambda: datetime.now(timezone.utc))
    now = _utc(clock_fn(), "preflight now")
    package, configuration, authorization = _load_execution_inputs(
        package_path,
        authorization_path,
        now=now,
    )
    quota_headroom = _load_quota_headroom(
        quota_headroom_path,
        package=package,
        configuration=configuration,
        authorization=authorization,
        now=now,
    )
    _read_protected_therundown_credential(credential_file)
    return {
        "status": "DRY_RUN_READY",
        "network_calls": 0,
        "provider": authorization.provider,
        "leagues": list(TOP5_LEAGUE_ORDER),
        "package_digest": package.package_digest,
        "configuration_digest": configuration.configuration_digest,
        "authorization_digest": authorization.authorization_digest,
        "controlled_shadow_run_id": authorization.controlled_shadow_run_id,
        "qualification_session_id": authorization.qualification_session_id,
        "authorization_id": authorization.authorization_id,
        "maximum_request_count": authorization.maximum_request_count,
        "maximum_datapoints": authorization.maximum_datapoints,
        "maximum_quota_cost_units": authorization.maximum_quota_cost_units,
        "minimum_interval_seconds": authorization.minimum_interval_seconds,
        "maximum_retries": authorization.maximum_retries,
        "quota_headroom_evidence_digest": quota_headroom.evidence_digest,
        "observed_remaining_datapoints": quota_headroom.observed_remaining_datapoints,
        "execution_enabled": False,
        "provider_requests": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded TheRundown Top-5 controlled shadow; default is zero-network dry-run."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute-network", action="store_true")
    mode.add_argument("--execute-quota-proof", action="store_true")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--proof-authorization", type=Path)
    parser.add_argument("--proof-target-evidence", type=Path)
    parser.add_argument("--quota-headroom", type=Path)
    parser.add_argument("--spend-control-evidence", type=Path)
    parser.add_argument(
        "--credential-file",
        type=Path,
        default=DEFAULT_THERUNDOWN_CREDENTIAL_PATH,
        help="absolute operator-only .env containing THERUNDOWN_API_KEY",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--endpoint", default=THERUNDOWN_BASE_URL)
    args = parser.parse_args(argv)
    try:
        if args.execute_quota_proof:
            if args.output is None:
                parser.error("--output is required with --execute-quota-proof")
            if args.proof_authorization is None:
                parser.error(
                    "--proof-authorization is required with --execute-quota-proof"
                )
            if args.proof_target_evidence is None:
                parser.error(
                    "--proof-target-evidence is required with --execute-quota-proof"
                )
            if args.spend_control_evidence is None:
                parser.error(
                    "--spend-control-evidence is required with --execute-quota-proof"
                )
            summary = run_guarded_quota_proof(
                args.proof_authorization,
                args.proof_target_evidence,
                spend_control_evidence_path=args.spend_control_evidence,
                credential_file=args.credential_file,
                output_path=args.output,
            )
        elif args.execute_network:
            if args.package is None or args.authorization is None:
                parser.error(
                    "--package and --authorization are required with --execute-network"
                )
            if args.output is None:
                parser.error("--output is required with --execute-network")
            if args.quota_headroom is None:
                parser.error("--quota-headroom is required with --execute-network")
            summary = run_guarded_network_execution(
                args.package,
                args.authorization,
                quota_headroom_path=args.quota_headroom,
                credential_file=args.credential_file,
                output_path=args.output,
                endpoint=args.endpoint,
            )
        else:
            if args.package is None or args.authorization is None:
                parser.error(
                    "--package and --authorization are required with --preflight"
                )
            if args.quota_headroom is None:
                parser.error("--quota-headroom is required with --preflight")
            summary = run_guarded_network_preflight(
                args.package,
                args.authorization,
                quota_headroom_path=args.quota_headroom,
                credential_file=args.credential_file,
            )
        print(json.dumps(_jsonable(summary), indent=2, sort_keys=True))
        return 0
    except (
        ControlledShadowAuthorizationPackageError,
        NetworkShadowContractError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 2


__all__ = [
    "AUTHORIZATION_PACKAGE_SCHEMA_VERSION",
    "CANONICAL_CANDIDATE_PROVIDER",
    "FUTURE_EXECUTION_COMMAND",
    "QUOTA_PROOF_COMMAND",
    "RECONCILIATION_SCHEMA_VERSION",
    "TOP5_LEAGUE_ORDER",
    "ControlledShadowAuthorizationPackageError",
    "ControlledShadowAuthorizationPackageV1",
    "FiveLeagueReconciliationV1",
    "QualificationReadyArtifactsV1",
    "prepare_authorization_package",
    "reconcile_controlled_shadow_run",
    "reconcile_controlled_shadow_run_with_b1_ll_artifact",
    "run_guarded_network_execution",
    "run_guarded_network_preflight",
    "run_guarded_quota_proof",
]


if __name__ == "__main__":
    raise SystemExit(main())
