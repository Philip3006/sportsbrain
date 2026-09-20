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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any

from src.football.provider_cascade.candidate_eligibility import (
    CandidateEligibilityError,
    CandidateProviderEligibilityV1,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    NETWORK_SHADOW_SCHEMA_VERSION,
    NetworkShadowContractError,
    NetworkShadowExecutionBlocked,
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
)
from src.football.top5_therundown_shadow_canary import TheRundownCanaryTargetV1

CANONICAL_CANDIDATE_PROVIDER = "therundown_experimental"
TOP5_LEAGUE_ORDER = ("EPL", "BL1", "LL", "SA", "L1")
TOP5_CONTROLLED_SHADOW_REQUEST_COUNT = 5
TOP5_OBSERVED_REQUEST_DATAPOINT_COST = 55
TOP5_CONTROLLED_SHADOW_MAX_DATAPOINTS = (
    TOP5_CONTROLLED_SHADOW_REQUEST_COUNT * TOP5_OBSERVED_REQUEST_DATAPOINT_COST
)
TOP5_CONTROLLED_SHADOW_REQUEST_QUOTA_COST_UNITS = float(
    TOP5_OBSERVED_REQUEST_DATAPOINT_COST
)
TOP5_CONTROLLED_SHADOW_MAX_QUOTA_COST_UNITS = float(
    TOP5_CONTROLLED_SHADOW_MAX_DATAPOINTS
)
TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS = 1.1
TOP5_CONTROLLED_SHADOW_MAX_RETRIES = 0
TOP5_CONTROLLED_SHADOW_MAX_SOURCE_AGE_SECONDS = 300
AUTHORIZATION_PACKAGE_SCHEMA_VERSION = "top5-controlled-shadow-authorization-package-v1"
RECONCILIATION_SCHEMA_VERSION = "top5-controlled-shadow-reconciliation-v1"
QUALIFICATION_ARTIFACT_SCHEMA_VERSION = (
    "top5-controlled-shadow-qualification-artifacts-v1"
)
FUTURE_EXECUTION_COMMAND = (
    "python -m src.football.top5_controlled_shadow_authorization_package "
    "--execute --package <authorization-package.json> "
    "--authorization <ceo-authorization.json> "
    "--b1-ll-artifact <b1-ll-evidence-bundle.json>"
)


def derive_bounded_shadow_budget(
    *,
    observed_datapoint_cost: int,
    available_quota_remaining: int,
    request_count: int = TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
) -> dict[str, object]:
    """Derive the final hard ceiling from observed provider billing semantics."""

    if observed_datapoint_cost <= 0:
        raise ControlledShadowAuthorizationPackageError(
            "observed provider datapoint cost must be positive"
        )
    if request_count != TOP5_CONTROLLED_SHADOW_REQUEST_COUNT:
        raise ControlledShadowAuthorizationPackageError(
            "the controlled shadow scope is exactly five requests"
        )
    if available_quota_remaining < observed_datapoint_cost * request_count:
        raise ControlledShadowAuthorizationPackageError(
            "free-tier quota headroom is below the bounded five-request ceiling"
        )
    total = observed_datapoint_cost * request_count
    return {
        "maximum_request_count": request_count,
        "maximum_datapoints": total,
        "maximum_quota_cost_units": float(total),
        "request_quota_cost_units": float(observed_datapoint_cost),
        "observed_datapoint_cost_per_request": observed_datapoint_cost,
        "available_quota_remaining_before_run": available_quota_remaining,
        "quota_headroom_after_bounded_run": available_quota_remaining - total,
        "minimum_interval_seconds": TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
        "maximum_retries": TOP5_CONTROLLED_SHADOW_MAX_RETRIES,
        "billing_unit": "provider_billed_x_datapoints",
    }


def _require_final_shadow_budget(
    configuration: TheRundownNetworkConfigurationV1,
) -> None:
    expected = derive_bounded_shadow_budget(
        observed_datapoint_cost=TOP5_OBSERVED_REQUEST_DATAPOINT_COST,
        available_quota_remaining=TOP5_OBSERVED_REQUEST_DATAPOINT_COST
        * TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
    )
    fields = (
        ("maximum_request_count", configuration.maximum_request_count),
        ("maximum_datapoints", configuration.maximum_datapoints),
        ("maximum_quota_cost_units", configuration.maximum_quota_cost_units),
        ("request_quota_cost_units", configuration.request_quota_cost_units),
        ("minimum_interval_seconds", configuration.minimum_interval_seconds),
        ("maximum_retries", configuration.maximum_retries),
        ("maximum_source_age_seconds", configuration.maximum_source_age_seconds),
    )
    for name, actual in fields:
        expected_value = expected.get(name)
        if name == "maximum_source_age_seconds":
            expected_value = TOP5_CONTROLLED_SHADOW_MAX_SOURCE_AGE_SECONDS
        if actual != expected_value:
            raise ControlledShadowAuthorizationPackageError(
                f"final provider billing budget mismatch: {name}"
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
    if isinstance(artifact, (str, Path)):
        try:
            artifact = json.loads(Path(artifact).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlledShadowAuthorizationPackageError(
                "B1 La Liga artifact file cannot be read as JSON"
            ) from exc
    if hasattr(artifact, "as_evidence_bundle"):
        artifact = artifact.as_evidence_bundle()  # type: ignore[union-attr]
    outer = _mapping(artifact, "B1 La Liga artifact")
    if outer.get("schema_version") == "top5-b1-laliga-final-evidence-v1":
        if outer.get("evidence_kind") != ObservationEvidenceKind.REAL_OBSERVED.value:
            raise ControlledShadowAuthorizationPackageError(
                "B1 outer artifact must be REAL_OBSERVED"
            )
        replay = _mapping(outer.get("replay"), "B1 replay")
        if replay.get("network_called") is not False:
            raise ControlledShadowAuthorizationPackageError(
                "offline B1 replay must not make a network call"
            )
        reconciliation = _mapping(outer.get("b4_reconciliation"), "B4 reconciliation")
        for name, expected in (
            ("activation_or_publication_enabled", False),
            ("provider_authority_changed", False),
            ("receipt_or_authority_issued", False),
            ("candidate_provider_eligibility_input_ready", True),
            ("complete_regulation_1x2_present", True),
            ("required_bookmaker_observations_present", True),
        ):
            if reconciliation.get(name) is not expected:
                raise ControlledShadowAuthorizationPackageError(
                    f"B4 outer reconciliation safety field {name} is invalid"
                )
        outer = _mapping(
            outer.get("canonical_b1_evidence_bundle"), "B1 canonical evidence bundle"
        )
    return outer


def _validate_b1_ll_artifact_shape(artifact: Mapping[str, object]) -> None:
    """Validate the serialized shape without inventing B1 authority."""

    raw = _materialize_b1_ll_artifact(artifact)
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


def _derive_b1_fixture_identity(
    bridge: Mapping[str, object],
) -> dict[str, object]:
    """Keep B1's provider key separate from SportsBrain's canonical key."""

    provider_fixture_key = bridge.get("fixture_key")
    if not isinstance(provider_fixture_key, str) or not provider_fixture_key.strip():
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider fixture identity is missing"
        )
    league = bridge.get("league")
    if league not in ("LL", "ESP1"):
        raise ControlledShadowAuthorizationPackageError(
            "B1 fixture identity has an unsupported league"
        )
    home_team = bridge.get("home_team")
    away_team = bridge.get("away_team")
    if not isinstance(home_team, str) or not home_team.strip():
        raise ControlledShadowAuthorizationPackageError(
            "B1 home participant identity is missing"
        )
    if not isinstance(away_team, str) or not away_team.strip():
        raise ControlledShadowAuthorizationPackageError(
            "B1 away participant identity is missing"
        )
    kickoff = _timestamp(bridge.get("kickoff"), "B1 kickoff")
    canonical_fixture_key = make_fixture_key("LL", home_team, away_team, kickoff)
    return {
        "provider_fixture_key": provider_fixture_key,
        "canonical_fixture_key": canonical_fixture_key,
        "canonical_derivation": "make_fixture_key(league,home_team,away_team,kickoff)",
        "provider_event_id": bridge.get("provider_event_id"),
        "league": "LL",
        "home_team": home_team,
        "away_team": away_team,
        "kickoff": kickoff.isoformat(),
    }


def validate_canonical_b1_ll_artifact(
    artifact: object,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Validate the supplied repaired B1 LL artifact without issuing authority.

    The checked-in/run-specific B1 artifact carries the historical LL capture's
    own authorization binding.  This preflight validator therefore proves the
    artifact's evidence, billing, and safety boundary independently.  The
    later five-league run must still bind its newly captured LL slot to its own
    CEO authorization, run ID, session ID, and configuration digest through
    ``_validate_b1_ll_artifact``.
    """

    raw = dict(_materialize_b1_ll_artifact(artifact))
    _validate_b1_ll_artifact_shape(raw)
    bridge = _mapping(raw.get("b1_bridge_inputs"), "B1 bridge inputs")
    if bridge.get("evidence_kind") != ObservationEvidenceKind.REAL_OBSERVED.value:
        raise ControlledShadowAuthorizationPackageError(
            "B1 synthetic or replay evidence cannot enter the offline preflight"
        )
    if bridge.get("network_execution") is not True:
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact does not prove network execution"
        )
    if bridge.get("provider_identity") != CANONICAL_CANDIDATE_PROVIDER:
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider identity is not canonical"
        )
    if bridge.get("league") not in ("LL", "ESP1"):
        raise ControlledShadowAuthorizationPackageError("B1 artifact is not La Liga")
    if bridge.get("market_phase") != "PRE_MATCH" or bridge.get("market_type") != (
        "football:pre_match:1x2"
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 artifact must be prematch regulation 1X2"
        )
    _digest_value(raw.get("raw_response_digest"), "B1 raw response digest")
    _digest_value(bridge.get("raw_response_digest"), "B1 bridge raw response digest")
    if raw.get("raw_response_digest") != bridge.get("raw_response_digest"):
        raise ControlledShadowAuthorizationPackageError(
            "B1 raw response digest is inconsistent"
        )
    for name in ("adapter_version", "adapter_source_sha"):
        if not isinstance(raw.get(name), str) or not str(raw.get(name)).strip():
            raise ControlledShadowAuthorizationPackageError(
                f"B1 {name} provenance is missing"
            )
    participant_ids = _mapping(bridge.get("participant_ids"), "B1 participant IDs")
    for name in ("home", "away", "draw"):
        if not str(participant_ids.get(name, "")).strip():
            raise ControlledShadowAuthorizationPackageError(
                "B1 participant binding is incomplete"
            )
    source_timestamps = bridge.get("source_timestamps")
    if not isinstance(source_timestamps, (tuple, list)) or not source_timestamps:
        raise ControlledShadowAuthorizationPackageError(
            "B1 source timestamp provenance is missing"
        )
    source_timestamp = _timestamp(source_timestamps[0], "B1 source timestamp")
    captured_at = _timestamp(bridge.get("captured_at"), "B1 captured_at")
    validation_now = _utc(now, "B1 preflight now") if now is not None else captured_at
    if source_timestamp > captured_at or captured_at > validation_now:
        raise ControlledShadowAuthorizationPackageError(
            "B1 timestamp provenance is not ordered"
        )
    if (
        validation_now - source_timestamp
    ).total_seconds() > TOP5_CONTROLLED_SHADOW_MAX_SOURCE_AGE_SECONDS:
        raise ControlledShadowAuthorizationPackageError(
            "B1 source observation is stale"
        )
    quota_evidence = _mapping(bridge.get("quota_evidence"), "B1 quota evidence")
    datapoints = quota_evidence.get("x-datapoints")
    used_after = quota_evidence.get("x-datapoints-used")
    remaining_after = quota_evidence.get("x-datapoints-remaining")
    limit = quota_evidence.get("x-datapoints-limit")
    for name, value in (
        ("x-datapoints", datapoints),
        ("x-datapoints-used", used_after),
        ("x-datapoints-remaining", remaining_after),
        ("x-datapoints-limit", limit),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ControlledShadowAuthorizationPackageError(
                f"B1 billing provenance is missing or invalid: {name}"
            )
    if used_after + remaining_after != limit or used_after < datapoints:
        raise ControlledShadowAuthorizationPackageError(
            "B1 billing counters do not reconcile"
        )
    bookmaker_observations = raw.get("bookmaker_observations")
    if (
        not isinstance(bookmaker_observations, (tuple, list))
        or not bookmaker_observations
    ):
        raise ControlledShadowAuthorizationPackageError(
            "B1 bookmaker observations are missing"
        )
    fixture_identity = _derive_b1_fixture_identity(bridge)
    for item in bookmaker_observations:
        bookmaker = _mapping(item, "B1 bookmaker observation")
        if (
            not str(bookmaker.get("bookmaker_id", "")).strip()
            or not str(bookmaker.get("bookmaker_name", "")).strip()
        ):
            raise ControlledShadowAuthorizationPackageError(
                "B1 bookmaker identity is incomplete"
            )
        odds = _mapping(bookmaker.get("odds"), "B1 bookmaker odds")
        _price(odds.get("home"), "B1 home odds")
        _price(odds.get("draw"), "B1 draw odds")
        _price(odds.get("away"), "B1 away odds")
    return {
        "schema_version": raw.get("schema_version"),
        "provider": CANONICAL_CANDIDATE_PROVIDER,
        "league": "LL",
        "evidence_kind": bridge.get("evidence_kind"),
        "network_execution": True,
        "provider_fixture_key": fixture_identity["provider_fixture_key"],
        "canonical_fixture_key": fixture_identity["canonical_fixture_key"],
        "fixture_identity_provenance": fixture_identity["canonical_derivation"],
        "fixture_key": fixture_identity["provider_fixture_key"],
        "provider_event_id": bridge.get("provider_event_id"),
        "provider_request_id": bridge.get("provider_request_id"),
        "bookmaker_count": len(bookmaker_observations),
        "source_timestamp": source_timestamp.isoformat(),
        "captured_at": captured_at.isoformat(),
        "datapoint_count": datapoints,
        "quota_cost_units": float(datapoints),
        "quota_used_before": used_after - datapoints,
        "quota_used_after": used_after,
        "quota_remaining_after": remaining_after,
        "quota_limit": limit,
        "adapter_version": raw.get("adapter_version"),
        "adapter_source_sha": raw.get("adapter_source_sha"),
        "raw_response_digest": raw.get("raw_response_digest"),
        "candidate_only": True,
        "receipt_or_authority_issued": False,
    }


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
    fixture_identity = _derive_b1_fixture_identity(bridge)
    exact_fields = (
        (
            "canonical_fixture_key",
            fixture_identity["canonical_fixture_key"],
            target.fixture_key,
        ),
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
    for digest in provider_digests:
        _digest_value(digest, "B1 provider record digest")
    for digest in normalized_digests:
        _digest_value(digest, "B1 normalized record digest")
    if response.provider_record_digest not in provider_digests:
        raise ControlledShadowAuthorizationPackageError(
            "B1 provider record digest does not match the LL capture"
        )
    # B1's historical normalized digests are bound to its original provider
    # fixture key.  B4 must retain them as provenance while independently
    # digesting the canonical SportsBrain fixture key; conflating the two
    # would either lose B1 history or falsify the B4 canonical digest.

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
            and bookmaker.get("normalized_record_digest") in normalized_digests
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
    _require_final_shadow_budget(configuration)
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
    b1_ll_artifact: Mapping[str, object] | None = None
    b1_fixture_identity: Mapping[str, object] | None = None
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
        if self.b1_fixture_identity is not None:
            identity = _mapping(self.b1_fixture_identity, "B1 fixture identity")
            for name in (
                "provider_fixture_key",
                "canonical_fixture_key",
                "canonical_derivation",
            ):
                if not str(identity.get(name, "")).strip():
                    raise ControlledShadowAuthorizationPackageError(
                        f"B1 fixture identity is missing: {name}"
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
            "b1_ll_artifact": (
                dict(self.b1_ll_artifact) if self.b1_ll_artifact is not None else None
            ),
            "b1_fixture_identity": (
                dict(self.b1_fixture_identity)
                if self.b1_fixture_identity is not None
                else None
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
    b1_fixture_identity = None
    if b1_ll_artifact is not None:
        validated_b1_ll_artifact = _validate_b1_ll_artifact(
            b1_ll_artifact,
            capture=captures_by_league["LL"],
            configuration=configuration,
            authorization=authorization,
            now=now,
        )
        b1_fixture_identity = _derive_b1_fixture_identity(
            _mapping(
                validated_b1_ll_artifact.get("b1_bridge_inputs"),
                "B1 bridge inputs",
            )
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
        b1_fixture_identity=b1_fixture_identity,
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


def _cli_mapping(value: object, name: str) -> Mapping[str, object]:
    return _mapping(value, name)


def _target_from_payload(value: object) -> TheRundownCanaryTargetV1:
    raw = _cli_mapping(value, "target")
    return TheRundownCanaryTargetV1(
        provider=raw.get("provider", ""),
        league=raw.get("league", ""),
        fixture_key=raw.get("fixture_key", ""),
        provider_event_id=raw.get("provider_event_id", ""),
        home_team=raw.get("home_team", ""),
        away_team=raw.get("away_team", ""),
        kickoff=_timestamp(raw.get("kickoff"), "target kickoff"),
    )


def _configuration_from_payload(
    value: object,
) -> TheRundownNetworkConfigurationV1:
    raw = _cli_mapping(value, "configuration")
    targets_raw = raw.get("targets")
    participants_raw = raw.get("participant_scope")
    requests_raw = raw.get("request_scope")
    if not isinstance(targets_raw, (tuple, list)):
        raise ControlledShadowAuthorizationPackageError(
            "configuration targets are missing"
        )
    if not isinstance(participants_raw, (tuple, list)):
        raise ControlledShadowAuthorizationPackageError(
            "configuration participant scope is missing"
        )
    if not isinstance(requests_raw, (tuple, list)):
        raise ControlledShadowAuthorizationPackageError(
            "configuration request scope is missing"
        )
    participants = tuple(
        TheRundownNetworkParticipantScopeV1(
            fixture_key=_cli_mapping(item, "participant scope").get("fixture_key", ""),
            home_participant_id=_cli_mapping(item, "participant scope").get(
                "home_participant_id", ""
            ),
            away_participant_id=_cli_mapping(item, "participant scope").get(
                "away_participant_id", ""
            ),
        )
        for item in participants_raw
    )
    requests = tuple(
        TheRundownNetworkRequestScopeV1(
            fixture_key=_cli_mapping(item, "request scope").get("fixture_key", ""),
            request_identity=_cli_mapping(item, "request scope").get(
                "request_identity", ""
            ),
        )
        for item in requests_raw
    )
    return TheRundownNetworkConfigurationV1(
        targets=tuple(_target_from_payload(item) for item in targets_raw),
        participant_scope=participants,
        request_scope=requests,
        adapter_version=raw.get("adapter_version", ""),
        adapter_source_sha=raw.get("adapter_source_sha", ""),
        maximum_request_count=raw.get("maximum_request_count", 0),
        maximum_datapoints=raw.get("maximum_datapoints", 0),
        maximum_quota_cost_units=raw.get("maximum_quota_cost_units", 0.0),
        request_quota_cost_units=raw.get("request_quota_cost_units", 0.0),
        maximum_source_age_seconds=raw.get("maximum_source_age_seconds", 0),
        minimum_interval_seconds=raw.get("minimum_interval_seconds", 0.0),
        maximum_retries=raw.get("maximum_retries", 0),
        enabled=raw.get("enabled", False),
        no_bet=raw.get("no_bet", True),
        publication=raw.get("publication", False),
        production_activation=raw.get("production_activation", False),
        monetary_spend_authorized=raw.get("monetary_spend_authorized", False),
        configuration_digest=raw.get("configuration_digest", ""),
    )


def _authorization_from_payload(
    value: object,
) -> TheRundownNetworkAuthorizationV1:
    raw = _cli_mapping(value, "authorization")
    targets_raw = raw.get("targets")
    participants_raw = raw.get("participant_scope")
    requests_raw = raw.get("request_scope")
    if not all(
        isinstance(items, (tuple, list))
        for items in (targets_raw, participants_raw, requests_raw)
    ):
        raise ControlledShadowAuthorizationPackageError(
            "authorization scope is incomplete"
        )
    participants = tuple(
        TheRundownNetworkParticipantScopeV1(
            fixture_key=_cli_mapping(item, "authorization participant scope").get(
                "fixture_key", ""
            ),
            home_participant_id=_cli_mapping(
                item, "authorization participant scope"
            ).get("home_participant_id", ""),
            away_participant_id=_cli_mapping(
                item, "authorization participant scope"
            ).get("away_participant_id", ""),
        )
        for item in participants_raw
    )
    requests = tuple(
        TheRundownNetworkRequestScopeV1(
            fixture_key=_cli_mapping(item, "authorization request scope").get(
                "fixture_key", ""
            ),
            request_identity=_cli_mapping(item, "authorization request scope").get(
                "request_identity", ""
            ),
        )
        for item in requests_raw
    )
    return TheRundownNetworkAuthorizationV1(
        authorization_id=raw.get("authorization_id", ""),
        ceo_authorization_identity=raw.get("ceo_authorization_identity", ""),
        controlled_shadow_run_id=raw.get("controlled_shadow_run_id", ""),
        qualification_session_id=raw.get("qualification_session_id", ""),
        provider=raw.get("provider", ""),
        targets=tuple(_target_from_payload(item) for item in targets_raw),
        participant_scope=participants,
        request_scope=requests,
        adapter_version=raw.get("adapter_version", ""),
        adapter_source_sha=raw.get("adapter_source_sha", ""),
        configuration_digest=raw.get("configuration_digest", ""),
        maximum_request_count=raw.get("maximum_request_count", 0),
        maximum_datapoints=raw.get("maximum_datapoints", 0),
        maximum_quota_cost_units=raw.get("maximum_quota_cost_units", 0.0),
        request_quota_cost_units=raw.get("request_quota_cost_units", 0.0),
        maximum_source_age_seconds=raw.get("maximum_source_age_seconds", 0),
        issued_at=_timestamp(raw.get("issued_at"), "authorization issued_at"),
        expires_at=_timestamp(raw.get("expires_at"), "authorization expires_at"),
        minimum_interval_seconds=raw.get("minimum_interval_seconds", 0.0),
        maximum_retries=raw.get("maximum_retries", 0),
        no_bet=raw.get("no_bet", True),
        publication=raw.get("publication", False),
        production_activation=raw.get("production_activation", False),
        monetary_spend_authorized=raw.get("monetary_spend_authorized", False),
        schema_version=raw.get("schema_version", NETWORK_SHADOW_SCHEMA_VERSION),
    )


def _read_cli_json(path: str, name: str) -> Mapping[str, object]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlledShadowAuthorizationPackageError(
            f"{name} cannot be read as JSON"
        ) from exc
    return _cli_mapping(raw, name)


def _load_cli_inputs(
    package_path: str,
    authorization_path: str,
    b1_path: str,
) -> tuple[
    ControlledShadowAuthorizationPackageV1,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkAuthorizationV1,
]:
    package_raw = _read_cli_json(package_path, "authorization package")
    package = ControlledShadowAuthorizationPackageV1(
        configuration_payload=_cli_mapping(
            package_raw.get("configuration"), "package configuration"
        ),
        authorization_template=_cli_mapping(
            package_raw.get("authorization_template"), "authorization template"
        ),
        package_digest=package_raw.get("package_digest", ""),
        network_execution_enabled=package_raw.get("network_execution_enabled", False),
        receipt_issuer_present=package_raw.get("receipt_issuer_present", False),
        active_provider_authority=package_raw.get("active_provider_authority", False),
        scheduler_registered=package_raw.get("scheduler_registered", False),
        publication=package_raw.get("publication", False),
        production_activation=package_raw.get("production_activation", False),
        betting=package_raw.get("betting", False),
        monetary_spend_authorized=package_raw.get("monetary_spend_authorized", False),
    )
    package.validate()
    packaged_configuration = _configuration_from_payload(package.configuration_payload)
    packaged_configuration.validate()
    _require_canonical_targets(packaged_configuration)
    _require_final_shadow_budget(packaged_configuration)
    execution_configuration = replace(
        packaged_configuration,
        enabled=True,
        configuration_digest="",
    )
    execution_configuration = replace(
        execution_configuration,
        configuration_digest=execution_configuration.computed_configuration_digest,
    )
    authorization = _authorization_from_payload(
        _read_cli_json(authorization_path, "CEO authorization")
    )
    # The package remains disabled.  The explicit execution flag is the only
    # place where the separately authorized enabled digest is accepted.
    authorization.validate(execution_configuration)
    validate_canonical_b1_ll_artifact(b1_path)
    return package, packaged_configuration, execution_configuration, authorization


def main(argv: Sequence[str] | None = None) -> int:
    """Guarded CLI: preflight is offline; execution requires every explicit gate."""

    parser = argparse.ArgumentParser(
        description="Controlled-shadow package preflight or explicitly authorized execution"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--package", required=True)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--b1-ll-artifact", required=True)
    parser.add_argument("--endpoint")
    args = parser.parse_args(argv)
    try:
        _, packaged_configuration, execution_configuration, authorization = (
            _load_cli_inputs(
                args.package,
                args.authorization,
                args.b1_ll_artifact,
            )
        )
        if args.preflight:
            print(
                json.dumps(
                    {
                        "status": "PREFLIGHT_READY",
                        "network_calls": 0,
                        "network_enabled_by_default": packaged_configuration.enabled,
                        "provider": CANONICAL_CANDIDATE_PROVIDER,
                        "leagues": list(TOP5_LEAGUE_ORDER),
                        "maximum_request_count": execution_configuration.maximum_request_count,
                        "maximum_datapoints": execution_configuration.maximum_datapoints,
                        "maximum_quota_cost_units": execution_configuration.maximum_quota_cost_units,
                        "maximum_retries": execution_configuration.maximum_retries,
                    },
                    sort_keys=True,
                )
            )
            return 0
        endpoint = args.endpoint or os.environ.get(
            "THERUNDOWN_ENDPOINT", "https://therundown.io/api/v2"
        )
        api_key = os.environ.get("THERUNDOWN_API_KEY")
        if not api_key:
            raise NetworkShadowExecutionBlocked(
                "THERUNDOWN_API_KEY is required only for explicit execution"
            )
        transport = TheRundownHttpNetworkTransportV1(
            endpoint=endpoint,
            api_key=api_key,
            adapter=TheRundownCanonicalPayloadAdapterV1(
                adapter_version=execution_configuration.adapter_version,
                adapter_source_sha=execution_configuration.adapter_source_sha,
            ),
        )
        executor = TheRundownNetworkShadowExecutorV1(allow_live_network=True)
        result = executor.run(
            execution_configuration,
            authorization,
            transport=transport,
        )
        reconciliation = reconcile_controlled_shadow_run_with_b1_ll_artifact(
            result,
            execution_configuration,
            authorization,
            args.b1_ll_artifact,
            now=datetime.now(timezone.utc),
        )
        print(
            json.dumps(
                {
                    "status": "CONTROLLED_SHADOW_RECONCILED",
                    "network_calls": len(transport.calls),
                    "request_count": result.request_count,
                    "datapoint_count": result.datapoint_count,
                    "quota_cost_units": result.quota_cost_units,
                    "reconciliation_digest": reconciliation.reconciliation_digest,
                    "receipt_eligible": False,
                    "authority_changed": False,
                    "publication": False,
                    "production_activation": False,
                    "betting": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        ControlledShadowAuthorizationPackageError,
        NetworkShadowContractError,
    ) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTHORIZATION_PACKAGE_SCHEMA_VERSION",
    "CANONICAL_CANDIDATE_PROVIDER",
    "FUTURE_EXECUTION_COMMAND",
    "RECONCILIATION_SCHEMA_VERSION",
    "TOP5_CONTROLLED_SHADOW_MAX_DATAPOINTS",
    "TOP5_CONTROLLED_SHADOW_MAX_QUOTA_COST_UNITS",
    "TOP5_CONTROLLED_SHADOW_MAX_RETRIES",
    "TOP5_CONTROLLED_SHADOW_MAX_SOURCE_AGE_SECONDS",
    "TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS",
    "TOP5_CONTROLLED_SHADOW_REQUEST_COUNT",
    "TOP5_CONTROLLED_SHADOW_REQUEST_QUOTA_COST_UNITS",
    "TOP5_LEAGUE_ORDER",
    "TOP5_OBSERVED_REQUEST_DATAPOINT_COST",
    "ControlledShadowAuthorizationPackageError",
    "ControlledShadowAuthorizationPackageV1",
    "FiveLeagueReconciliationV1",
    "QualificationReadyArtifactsV1",
    "derive_bounded_shadow_budget",
    "prepare_authorization_package",
    "reconcile_controlled_shadow_run",
    "reconcile_controlled_shadow_run_with_b1_ll_artifact",
    "validate_canonical_b1_ll_artifact",
]
