"""No-network preparation contract for a future Top-5 controlled shadow run.

This module plans a future run from caller-supplied state.  It deliberately
does not import provider adapters, transports, credentials, Builder 2 receipt
issuance, the ledger, runtime storage, or production activation code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    DEFAULT_PROVIDER_ORDER,
    MARKET_PREMATCH_1X2,
    TOP5_LEAGUE_CODES,
    CascadeTimingPolicy,
    ProviderIdentityResolutionState,
    QuotaSnapshot,
)
from src.football.top5_shadow_provider_redundancy import (
    ProviderReadinessState,
)
from src.utils.atomic_io import atomic_write_json

PREPARATION_SCHEMA_VERSION = "controlled-shadow-run-preparation-v1"
PREPARATION_ID_PREFIX = "top5-controlled-shadow-preparation:"
PREPARATION_READY = "READY_FOR_CEO_CONTROLLED_SHADOW_AUTHORIZATION"
PREPARATION_BLOCKED = "BLOCKED"
SUPPORTED_PREPARATION_PROVIDERS = DEFAULT_PROVIDER_ORDER
UNKNOWN = "UNKNOWN"
_STORAGE_SCOPE = "caller_selected_external_or_test_path_only"


class PreparationContractError(ProductionContractError):
    """Raised when preparation input or a serialized manifest is unsafe."""


_READY_READINESS = frozenset(
    {
        ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
        ProviderReadinessState.REAL_OBSERVATION_VALIDATED,
    }
)


@dataclass(frozen=True)
class _ProviderSpec:
    display_name: str
    odds_action: str
    discovery_action: str | None
    source_timestamp_capability: str
    delayed_data_semantics: str | None
    bookmaker_constraint: str
    expected_failures: tuple[str, ...]
    prerequisites: tuple[str, ...]
    supports_inline_identity_discovery: bool = False


_PROVIDER_SPECS = MappingProxyType(
    {
        "the_odds_api": _ProviderSpec(
            display_name="The Odds API",
            odds_action="GET /sports/{sport_key}/odds",
            discovery_action=None,
            source_timestamp_capability="bookmaker.last_update or event commence_time",
            delayed_data_semantics=None,
            bookmaker_constraint="configured bookmaker with complete h2h prices",
            expected_failures=(
                "CREDENTIAL_MISSING",
                "QUOTA_EXHAUSTED",
                "RATE_LIMITED",
                "AUTH_FAILED",
                "UNSUPPORTED_FIXTURE",
                "UNSUPPORTED_MARKET",
                "MALFORMED",
                "STALE",
            ),
            prerequisites=(
                "credential presence must be explicitly true",
                "known non-exhausted provider quota",
                "exact fixture identity in the sport-level odds response",
                "complete pre-match h2h 1X2 response",
            ),
            supports_inline_identity_discovery=True,
        ),
    }
)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreparationContractError(f"{name} is required")
    return value.strip()


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise PreparationContractError(f"{name} must be boolean")
    return value


def _strict_optional_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise PreparationContractError(f"{name} must be boolean or null")
    return value


def _strict_nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PreparationContractError(f"{name} must be a non-negative integer")
    return value


def _strict_nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise PreparationContractError(f"{name} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PreparationContractError(f"{name} must be a non-negative number") from exc
    if not isfinite(number) or number < 0:
        raise PreparationContractError(f"{name} must be a non-negative number")
    return number


def _parse_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if not isinstance(value, str):
        raise PreparationContractError(f"{name} must be a timezone-aware ISO timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except (TypeError, ValueError) as exc:
        raise PreparationContractError(
            f"{name} must be a timezone-aware ISO timestamp"
        ) from exc


def _fixture_from_input(value: Fixture | Mapping[str, object]) -> Fixture:
    if isinstance(value, Fixture):
        fixture = value
    elif isinstance(value, Mapping):
        kickoff_value = value.get("kickoff", value.get("kickoff_utc"))
        fixture = Fixture(
            fixture_key=str(value.get("fixture_key", "")),
            league_code=str(value.get("league_code", value.get("league", ""))),
            home_team=str(value.get("home_team", "")),
            away_team=str(value.get("away_team", "")),
            kickoff=_parse_datetime(kickoff_value, "fixture kickoff"),
        )
    else:
        raise PreparationContractError("fixture identity must be a Fixture or mapping")
    try:
        fixture.validate()
    except (TypeError, ValueError, ProductionContractError) as exc:
        raise PreparationContractError("fixture identity is invalid") from exc
    if fixture.league_code not in TOP5_LEAGUE_CODES:
        raise PreparationContractError(
            "fixture league must be one of the Top-5 leagues"
        )
    return fixture


def _timing_from_input(
    value: CascadeTimingPolicy | Mapping[str, object],
) -> CascadeTimingPolicy:
    if isinstance(value, CascadeTimingPolicy):
        timing = value
    elif isinstance(value, Mapping):
        if (
            "maximum_odds_age_seconds" not in value
            or "kickoff_tolerance_seconds" not in value
        ):
            raise PreparationContractError("timing policy must be supplied explicitly")
        timing = CascadeTimingPolicy(
            maximum_odds_age_seconds=value["maximum_odds_age_seconds"],  # type: ignore[arg-type]
            kickoff_tolerance_seconds=value["kickoff_tolerance_seconds"],  # type: ignore[arg-type]
        )
    else:
        raise PreparationContractError("timing policy must be a mapping or contract")
    try:
        timing.validate()
    except (TypeError, ValueError, ProductionContractError) as exc:
        raise PreparationContractError("timing policy is invalid") from exc
    return timing


def _timing_payload(timing: CascadeTimingPolicy) -> dict[str, int]:
    return {
        "maximum_odds_age_seconds": timing.maximum_odds_age_seconds,
        "kickoff_tolerance_seconds": timing.kickoff_tolerance_seconds,
    }


def _fixture_payload(fixture: Fixture) -> dict[str, object]:
    return {
        "fixture_key": fixture.fixture_key,
        "league_code": fixture.league_code,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "kickoff": fixture.kickoff.isoformat(),
    }


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identity_state(value: object, name: str) -> str | None:
    if value is None:
        return None
    if value == UNKNOWN:
        return None
    try:
        return ProviderIdentityResolutionState(value).value
    except (TypeError, ValueError):
        raise PreparationContractError(f"{name} is unknown") from None


def _readiness_state(value: object, name: str) -> str | None:
    if value is None:
        return None
    if value == UNKNOWN:
        return None
    try:
        return ProviderReadinessState(value).value
    except (TypeError, ValueError):
        try:
            return ProviderReadinessState[str(value)].value
        except (KeyError, TypeError, ValueError):
            raise PreparationContractError(f"{name} is unknown") from None


def _quota_from_input(value: object) -> QuotaSnapshot | None:
    if value is None:
        return None
    if isinstance(value, QuotaSnapshot):
        return value
    if not isinstance(value, Mapping):
        raise PreparationContractError(
            "provider quota/readiness evidence must be a mapping"
        )

    def optional_int(name: str) -> int | None:
        raw = value.get(name)
        if raw is None:
            return None
        return _strict_nonnegative_int(raw, f"quota.{name}")

    def optional_time(name: str) -> datetime | None:
        raw = value.get(name)
        return None if raw is None else _parse_datetime(raw, f"quota.{name}")

    try:
        return QuotaSnapshot(
            used=optional_int("used"),
            remaining=optional_int("remaining"),
            reset_at=optional_time("reset_at"),
            rate_limit=optional_int("rate_limit"),
            rate_remaining=optional_int("rate_remaining"),
            rate_reset_at=optional_time("rate_reset_at"),
        )
    except (TypeError, ValueError, ProductionContractError) as exc:
        raise PreparationContractError(
            "provider quota/readiness evidence is malformed"
        ) from exc


@dataclass(frozen=True)
class ProviderReadinessEvidence:
    """Caller-supplied, secret-free state for one provider path."""

    readiness_state: str | None = None
    identity_state: str | None = None
    fixture_id_known: bool | None = None
    provider_fixture_id: str | None = None
    discovery_required: bool | None = None
    quota: QuotaSnapshot | None = None
    quota_cost_units_per_request: float = 1.0
    state_supplied: bool = True
    pagination_risk: str | None = None
    body_error_taxonomy: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: ProviderReadinessEvidence | Mapping[str, object],
        *,
        supplied: bool = True,
    ) -> ProviderReadinessEvidence:
        if isinstance(value, ProviderReadinessEvidence):
            return value
        if not isinstance(value, Mapping):
            raise PreparationContractError(
                "provider readiness evidence must be a mapping"
            )
        provider_fixture_id = value.get(
            "provider_fixture_id", value.get("fixture_id", value.get("market_id"))
        )
        if provider_fixture_id in (None, ""):
            normalized_fixture_id = None
        elif isinstance(provider_fixture_id, (int, str)) and not isinstance(
            provider_fixture_id, bool
        ):
            normalized_fixture_id = str(provider_fixture_id).strip() or None
        else:
            raise PreparationContractError("provider fixture ID must be text or null")
        fixture_id_known = (
            _strict_optional_bool(value["fixture_id_known"], "fixture_id_known")
            if "fixture_id_known" in value
            else (normalized_fixture_id is not None)
        )
        identity_state = _identity_state(value.get("identity_state"), "identity_state")
        discovery_required = (
            _strict_optional_bool(value["discovery_required"], "discovery_required")
            if "discovery_required" in value
            else (
                identity_state
                == ProviderIdentityResolutionState.DISCOVERY_REQUIRED.value
                if identity_state is not None
                else None
            )
        )
        cost_value = value.get(
            "quota_cost_units_per_request",
            value.get("request_cost_units", value.get("request_cost", 1.0)),
        )
        cost = _strict_nonnegative_number(cost_value, "quota_cost_units_per_request")
        pagination_risk = value.get("pagination_risk")
        if pagination_risk is not None and not isinstance(pagination_risk, str):
            raise PreparationContractError("pagination_risk must be text or null")
        taxonomy = value.get("body_error_taxonomy", ())
        if isinstance(taxonomy, str) or not isinstance(taxonomy, Sequence):
            raise PreparationContractError("body_error_taxonomy must be a sequence")
        body_errors = tuple(
            _required_text(item, "body_error_taxonomy item") for item in taxonomy
        )
        return cls(
            readiness_state=_readiness_state(
                value.get("readiness_state"), "readiness_state"
            ),
            identity_state=identity_state,
            fixture_id_known=fixture_id_known,
            provider_fixture_id=normalized_fixture_id,
            discovery_required=discovery_required,
            quota=_quota_from_input(
                value.get("quota", value.get("quota_state", value.get("initial_quota")))
            ),
            quota_cost_units_per_request=cost,
            state_supplied=supplied,
            pagination_risk=pagination_risk.strip()
            if isinstance(pagination_risk, str)
            else None,
            body_error_taxonomy=body_errors,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "readiness_state": self.readiness_state,
            "identity_state": self.identity_state,
            "fixture_id_known": self.fixture_id_known,
            "provider_fixture_id": self.provider_fixture_id,
            "discovery_required": self.discovery_required,
            "quota": self.quota.as_payload() if self.quota else None,
            "quota_cost_units_per_request": self.quota_cost_units_per_request,
            "state_supplied": self.state_supplied,
            "pagination_risk": self.pagination_risk,
            "body_error_taxonomy": list(self.body_error_taxonomy),
        }


@dataclass(frozen=True)
class PreparationAction:
    """One planned action; it is never dispatched by this package."""

    sequence: int
    provider: str
    action_class: str
    endpoint_or_action: str
    network_request_count: int
    quota_cost_units: float
    condition: str

    def validate(self) -> None:
        if self.sequence < 0 or self.network_request_count < 0:
            raise PreparationContractError("preparation action counts are invalid")
        _required_text(self.provider, "preparation action provider")
        _required_text(self.action_class, "preparation action class")
        _required_text(self.endpoint_or_action, "preparation action endpoint")
        _required_text(self.condition, "preparation action condition")
        _strict_nonnegative_number(
            self.quota_cost_units, "preparation action quota cost"
        )
        if self.network_request_count not in (0, 1):
            raise PreparationContractError(
                "preparation actions are bounded to one request"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "sequence": self.sequence,
            "provider": self.provider,
            "action_class": self.action_class,
            "endpoint_or_action": self.endpoint_or_action,
            "network_request_count": self.network_request_count,
            "quota_cost_units": self.quota_cost_units,
            "condition": self.condition,
        }

    @classmethod
    def from_payload(cls, value: object) -> PreparationAction:
        if not isinstance(value, Mapping):
            raise PreparationContractError("preparation action must be a mapping")
        action = cls(
            sequence=_strict_nonnegative_int(value.get("sequence"), "action.sequence"),
            provider=_required_text(value.get("provider"), "action.provider"),
            action_class=_required_text(
                value.get("action_class"), "action.action_class"
            ),
            endpoint_or_action=_required_text(
                value.get("endpoint_or_action"), "action.endpoint_or_action"
            ),
            network_request_count=_strict_nonnegative_int(
                value.get("network_request_count"), "action.network_request_count"
            ),
            quota_cost_units=_strict_nonnegative_number(
                value.get("quota_cost_units"), "action.quota_cost_units"
            ),
            condition=_required_text(value.get("condition"), "action.condition"),
        )
        action.validate()
        return action


@dataclass(frozen=True)
class ProviderPreparationManifest:
    """The complete no-network plan for one candidate provider."""

    provider: str
    display_name: str
    configured_cascade_position: int | None
    enabled: bool
    candidate_state: str
    credential_required: bool
    credential_present: bool | None
    provider_fixture_id_known: bool | None
    provider_fixture_id: str | None
    fixture_discovery_required: bool | None
    identity_state: str
    readiness_state: str
    expected_endpoint_action_class: str
    expected_network_request_count: int
    expected_quota_cost_units: float
    maximum_allowed_request_count: int
    maximum_allowed_quota_cost_units: float
    current_known_quota: Mapping[str, object]
    source_timestamp_capability: str
    delayed_data_semantics: str | None
    bookmaker_constraint: str
    expected_failure_classifications: tuple[str, ...]
    prerequisites: tuple[str, ...]
    executable: bool
    reason_executable_or_blocked: str
    pagination_risk: str | None
    body_error_taxonomy: tuple[str, ...]
    planned_actions: tuple[PreparationAction, ...] = ()
    monetary_spend: float = 0.0
    betting: bool = False
    publication: bool = False
    production_activation: bool = False

    def validate(self) -> None:
        if self.provider not in SUPPORTED_PREPARATION_PROVIDERS:
            raise PreparationContractError(
                "provider manifest contains an unsupported provider"
            )
        if (
            self.configured_cascade_position is not None
            and self.configured_cascade_position < 0
        ):
            raise PreparationContractError("provider cascade position is invalid")
        if self.enabled != (self.configured_cascade_position is not None):
            raise PreparationContractError(
                "provider enabled flag is inconsistent with configured position"
            )
        if not isinstance(self.enabled, bool) or not isinstance(
            self.credential_required, bool
        ):
            raise PreparationContractError(
                "provider manifest boolean fields are invalid"
            )
        if self.credential_present is not None and not isinstance(
            self.credential_present, bool
        ):
            raise PreparationContractError("credential_present must be boolean or null")
        if self.provider_fixture_id_known is not None and not isinstance(
            self.provider_fixture_id_known, bool
        ):
            raise PreparationContractError(
                "provider_fixture_id_known must be boolean or null"
            )
        if self.fixture_discovery_required is not None and not isinstance(
            self.fixture_discovery_required, bool
        ):
            raise PreparationContractError(
                "fixture_discovery_required must be boolean or null"
            )
        for name, value in (
            ("display_name", self.display_name),
            ("candidate_state", self.candidate_state),
            ("identity_state", self.identity_state),
            ("readiness_state", self.readiness_state),
            ("expected_endpoint_action_class", self.expected_endpoint_action_class),
            ("source_timestamp_capability", self.source_timestamp_capability),
            ("bookmaker_constraint", self.bookmaker_constraint),
            ("reason_executable_or_blocked", self.reason_executable_or_blocked),
        ):
            _required_text(value, f"provider manifest {name}")
        _strict_nonnegative_int(
            self.expected_network_request_count, "expected_network_request_count"
        )
        _strict_nonnegative_int(
            self.maximum_allowed_request_count, "maximum_allowed_request_count"
        )
        _strict_nonnegative_number(
            self.expected_quota_cost_units, "expected_quota_cost_units"
        )
        _strict_nonnegative_number(
            self.maximum_allowed_quota_cost_units, "maximum_allowed_quota_cost_units"
        )
        if self.expected_network_request_count > self.maximum_allowed_request_count:
            raise PreparationContractError("provider plan exceeds its request maximum")
        if (
            self.expected_quota_cost_units
            > self.maximum_allowed_quota_cost_units + 1e-9
        ):
            raise PreparationContractError(
                "provider plan exceeds its quota-cost maximum"
            )
        try:
            ProviderIdentityResolutionState(self.identity_state)
        except (TypeError, ValueError):
            if self.identity_state != UNKNOWN:
                raise PreparationContractError(
                    "provider identity state is invalid"
                ) from None
        try:
            ProviderReadinessState(self.readiness_state)
        except (TypeError, ValueError):
            if self.readiness_state != UNKNOWN:
                raise PreparationContractError(
                    "provider readiness state is invalid"
                ) from None
        if self.current_known_quota is None or not isinstance(
            self.current_known_quota, Mapping
        ):
            raise PreparationContractError("current known quota must be a mapping")
        _quota_from_input(self.current_known_quota)
        if self.pagination_risk is not None and not isinstance(
            self.pagination_risk, str
        ):
            raise PreparationContractError("pagination_risk must be text or null")
        for item in self.body_error_taxonomy:
            _required_text(item, "body error taxonomy item")
        for action in self.planned_actions:
            action.validate()
        if tuple(action.sequence for action in self.planned_actions) != tuple(
            range(len(self.planned_actions))
        ):
            raise PreparationContractError(
                "provider planned action sequence is not contiguous"
            )
        if (
            tuple(
                action.provider
                for action in self.planned_actions
                if action.network_request_count
            )
            != (self.provider,) * self.expected_network_request_count
        ):
            raise PreparationContractError("planned actions are not bound to provider")
        if (
            sum(action.network_request_count for action in self.planned_actions)
            != self.expected_network_request_count
        ):
            raise PreparationContractError(
                "planned action request count does not match manifest"
            )
        if (
            abs(
                sum(action.quota_cost_units for action in self.planned_actions)
                - self.expected_quota_cost_units
            )
            > 1e-9
        ):
            raise PreparationContractError(
                "planned action quota cost does not match manifest"
            )
        if (
            not isinstance(self.monetary_spend, (int, float))
            or self.monetary_spend != 0
        ):
            raise PreparationContractError("preparation monetary spend must be zero")
        if self.betting or self.publication or self.production_activation:
            raise PreparationContractError(
                "provider preparation cannot enable production actions"
            )
        if self.executable and self.expected_network_request_count <= 0:
            raise PreparationContractError(
                "executable provider requires a planned request"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "configured_cascade_position": self.configured_cascade_position,
            "enabled": self.enabled,
            "candidate_state": self.candidate_state,
            "credential_required": self.credential_required,
            "credential_present": self.credential_present,
            "provider_fixture_id_known": self.provider_fixture_id_known,
            "provider_fixture_id": self.provider_fixture_id,
            "fixture_discovery_required": self.fixture_discovery_required,
            "identity_state": self.identity_state,
            "readiness_state": self.readiness_state,
            "expected_endpoint_action_class": self.expected_endpoint_action_class,
            "expected_network_request_count": self.expected_network_request_count,
            "expected_quota_cost_units": self.expected_quota_cost_units,
            "maximum_allowed_request_count": self.maximum_allowed_request_count,
            "maximum_allowed_quota_cost_units": self.maximum_allowed_quota_cost_units,
            "current_known_quota": dict(self.current_known_quota),
            "source_timestamp_capability": self.source_timestamp_capability,
            "delayed_data_semantics": self.delayed_data_semantics,
            "bookmaker_constraint": self.bookmaker_constraint,
            "expected_failure_classifications": list(
                self.expected_failure_classifications
            ),
            "prerequisites": list(self.prerequisites),
            "executable": self.executable,
            "reason_executable_or_blocked": self.reason_executable_or_blocked,
            "pagination_risk": self.pagination_risk,
            "body_error_taxonomy": list(self.body_error_taxonomy),
            "planned_actions": [action.as_payload() for action in self.planned_actions],
            "monetary_spend": self.monetary_spend,
            "betting": self.betting,
            "publication": self.publication,
            "production_activation": self.production_activation,
        }

    @classmethod
    def from_payload(cls, value: object) -> ProviderPreparationManifest:
        if not isinstance(value, Mapping):
            raise PreparationContractError("provider manifest must be a mapping")
        raw_actions = value.get("planned_actions", ())
        if isinstance(raw_actions, (str, bytes)) or not isinstance(
            raw_actions, Sequence
        ):
            raise PreparationContractError("planned_actions must be a sequence")
        raw_failures = value.get("expected_failure_classifications", ())
        raw_prerequisites = value.get("prerequisites", ())
        if isinstance(raw_failures, (str, bytes)) or not isinstance(
            raw_failures, Sequence
        ):
            raise PreparationContractError(
                "expected failure classifications must be a sequence"
            )
        if isinstance(raw_prerequisites, (str, bytes)) or not isinstance(
            raw_prerequisites, Sequence
        ):
            raise PreparationContractError("prerequisites must be a sequence")
        raw_body_errors = value.get("body_error_taxonomy", ())
        if isinstance(raw_body_errors, (str, bytes)) or not isinstance(
            raw_body_errors, Sequence
        ):
            raise PreparationContractError("body_error_taxonomy must be a sequence")
        provider = _required_text(value.get("provider"), "provider manifest provider")
        manifest = cls(
            provider=provider,
            display_name=_required_text(value.get("display_name"), "display_name"),
            configured_cascade_position=(
                None
                if value.get("configured_cascade_position") is None
                else _strict_nonnegative_int(
                    value.get("configured_cascade_position"),
                    "configured_cascade_position",
                )
            ),
            enabled=_strict_bool(value.get("enabled"), "enabled"),
            candidate_state=_required_text(
                value.get("candidate_state"), "candidate_state"
            ),
            credential_required=_strict_bool(
                value.get("credential_required"), "credential_required"
            ),
            credential_present=_strict_optional_bool(
                value.get("credential_present"), "credential_present"
            ),
            provider_fixture_id_known=_strict_optional_bool(
                value.get("provider_fixture_id_known"), "provider_fixture_id_known"
            ),
            provider_fixture_id=(
                None
                if value.get("provider_fixture_id") is None
                else _required_text(
                    value.get("provider_fixture_id"), "provider_fixture_id"
                )
            ),
            fixture_discovery_required=_strict_optional_bool(
                value.get("fixture_discovery_required"), "fixture_discovery_required"
            ),
            identity_state=_required_text(
                value.get("identity_state"), "identity_state"
            ),
            readiness_state=_required_text(
                value.get("readiness_state"), "readiness_state"
            ),
            expected_endpoint_action_class=_required_text(
                value.get("expected_endpoint_action_class"),
                "expected_endpoint_action_class",
            ),
            expected_network_request_count=_strict_nonnegative_int(
                value.get("expected_network_request_count"),
                "expected_network_request_count",
            ),
            expected_quota_cost_units=_strict_nonnegative_number(
                value.get("expected_quota_cost_units"), "expected_quota_cost_units"
            ),
            maximum_allowed_request_count=_strict_nonnegative_int(
                value.get("maximum_allowed_request_count"),
                "maximum_allowed_request_count",
            ),
            maximum_allowed_quota_cost_units=_strict_nonnegative_number(
                value.get("maximum_allowed_quota_cost_units"),
                "maximum_allowed_quota_cost_units",
            ),
            current_known_quota=value.get("current_known_quota"),
            source_timestamp_capability=_required_text(
                value.get("source_timestamp_capability"), "source_timestamp_capability"
            ),
            delayed_data_semantics=(
                None
                if value.get("delayed_data_semantics") is None
                else _required_text(
                    value.get("delayed_data_semantics"), "delayed_data_semantics"
                )
            ),
            bookmaker_constraint=_required_text(
                value.get("bookmaker_constraint"), "bookmaker_constraint"
            ),
            expected_failure_classifications=tuple(
                _required_text(item, "expected failure classification")
                for item in raw_failures
            ),
            prerequisites=tuple(
                _required_text(item, "provider prerequisite")
                for item in raw_prerequisites
            ),
            executable=_strict_bool(value.get("executable"), "executable"),
            reason_executable_or_blocked=_required_text(
                value.get("reason_executable_or_blocked"),
                "reason_executable_or_blocked",
            ),
            pagination_risk=(
                None
                if value.get("pagination_risk") is None
                else _required_text(value.get("pagination_risk"), "pagination_risk")
            ),
            body_error_taxonomy=tuple(
                _required_text(item, "body error taxonomy item")
                for item in raw_body_errors
            ),
            planned_actions=tuple(
                PreparationAction.from_payload(item) for item in raw_actions
            ),
            monetary_spend=_strict_nonnegative_number(
                value.get("monetary_spend"), "monetary_spend"
            ),
            betting=_strict_bool(value.get("betting"), "betting"),
            publication=_strict_bool(value.get("publication"), "publication"),
            production_activation=_strict_bool(
                value.get("production_activation"), "production_activation"
            ),
        )
        manifest.validate()
        return manifest


@dataclass(frozen=True)
class ControlledShadowRunPreparationV1:
    """Deterministic, non-authorizing plan for a future controlled shadow run."""

    schema_version: str
    preparation_id: str
    preparation_status: str
    fixture_identity: Mapping[str, object]
    league: str
    kickoff: datetime
    timing_policy: Mapping[str, int]
    timing_policy_digest: str
    timing_policy_reference: str
    configured_provider_order: tuple[str, ...]
    maximum_total_network_requests: int
    maximum_total_quota_cost_units: float
    per_provider_maximums: Mapping[str, Mapping[str, object]]
    providers: tuple[ProviderPreparationManifest, ...]
    expected_sequential_execution_plan: tuple[PreparationAction, ...]
    known_blockers: tuple[str, ...]
    ceo_decisions_required: tuple[str, ...]
    authorization_required: bool = True
    authorization_status: str = "NOT_AUTHORIZED"
    execution_allowed: bool = False
    builder2_receipt_required_before_capture: bool = False
    builder2_receipt_issuer_exposed: bool = False
    no_network_guarantee: bool = True
    discovery_calls_separated_from_odds: bool = True
    monetary_spend: float = 0.0
    no_bet: bool = True
    betting: bool = False
    publication: bool = False
    production_activation: bool = False
    sealed_data_access: bool = False
    zero_spend_invariant: bool = True
    preparation_digest: str = ""

    def _digest_payload(self) -> dict[str, object]:
        payload = self.as_payload(validate=False, include_digest=False)
        payload.pop("preparation_id", None)
        return payload

    def validate(self, *, verify_digest: bool = True) -> None:
        if self.schema_version != PREPARATION_SCHEMA_VERSION:
            raise PreparationContractError("unsupported preparation schema version")
        if self.league not in TOP5_LEAGUE_CODES:
            raise PreparationContractError("preparation league is outside Top-5")
        _utc(self.kickoff, "preparation kickoff")
        if not isinstance(self.fixture_identity, Mapping):
            raise PreparationContractError("fixture identity must be a mapping")
        fixture = _fixture_from_input(self.fixture_identity)
        if fixture.league_code != self.league or fixture.kickoff != self.kickoff:
            raise PreparationContractError("fixture identity fields are inconsistent")
        if not isinstance(self.timing_policy, Mapping) or set(self.timing_policy) != {
            "maximum_odds_age_seconds",
            "kickoff_tolerance_seconds",
        }:
            raise PreparationContractError("timing policy contains unexpected fields")
        timing = _timing_from_input(self.timing_policy)
        expected_timing_digest = _digest(_timing_payload(timing))
        if self.timing_policy_digest != expected_timing_digest:
            raise PreparationContractError("timing policy digest mismatch")
        if (
            self.timing_policy_reference
            != f"timing-policy:{expected_timing_digest[:32]}"
        ):
            raise PreparationContractError("timing policy reference mismatch")
        if (
            not self.configured_provider_order
            or len(set(self.configured_provider_order))
            != len(self.configured_provider_order)
            or any(
                provider not in SUPPORTED_PREPARATION_PROVIDERS
                for provider in self.configured_provider_order
            )
        ):
            raise PreparationContractError("configured provider order is invalid")
        _strict_nonnegative_int(
            self.maximum_total_network_requests, "maximum_total_network_requests"
        )
        _strict_nonnegative_number(
            self.maximum_total_quota_cost_units, "maximum_total_quota_cost_units"
        )
        if len(self.providers) != len(SUPPORTED_PREPARATION_PROVIDERS):
            raise PreparationContractError(
                "preparation must include the canonical provider entry"
            )
        if (
            tuple(item.provider for item in self.providers)
            != SUPPORTED_PREPARATION_PROVIDERS
        ):
            raise PreparationContractError(
                "provider entries are not in canonical order"
            )
        configured_positions = {
            provider: index
            for index, provider in enumerate(self.configured_provider_order)
        }
        for provider in self.providers:
            provider.validate()
            if provider.configured_cascade_position != configured_positions.get(
                provider.provider
            ):
                raise PreparationContractError(
                    "provider configured position is inconsistent"
                )
        if not isinstance(self.per_provider_maximums, Mapping):
            raise PreparationContractError("per-provider maximums must be a mapping")
        if set(self.per_provider_maximums) != set(SUPPORTED_PREPARATION_PROVIDERS):
            raise PreparationContractError(
                "per-provider maximums must cover all providers"
            )
        for provider in SUPPORTED_PREPARATION_PROVIDERS:
            limits = self.per_provider_maximums[provider]
            if not isinstance(limits, Mapping):
                raise PreparationContractError("per-provider maximum is malformed")
            _strict_nonnegative_int(
                limits.get("network_requests"), "per-provider network maximum"
            )
            _strict_nonnegative_number(
                limits.get("quota_cost_units"), "per-provider quota maximum"
            )
        for index, action in enumerate(self.expected_sequential_execution_plan):
            action.validate()
            if action.sequence != index:
                raise PreparationContractError(
                    "execution plan sequence is not contiguous"
                )
            if action.provider not in self.configured_provider_order:
                raise PreparationContractError(
                    "execution plan contains an unconfigured provider"
                )
        planned_requests = sum(
            action.network_request_count
            for action in self.expected_sequential_execution_plan
        )
        planned_cost = sum(
            action.quota_cost_units
            for action in self.expected_sequential_execution_plan
        )
        if planned_requests > self.maximum_total_network_requests:
            raise PreparationContractError(
                "execution plan exceeds total network budget"
            )
        if planned_cost > self.maximum_total_quota_cost_units + 1e-9:
            raise PreparationContractError(
                "execution plan exceeds total quota-cost budget"
            )
        if self.preparation_status not in {PREPARATION_READY, PREPARATION_BLOCKED}:
            raise PreparationContractError("preparation status is unknown")
        if (
            self.authorization_status != "NOT_AUTHORIZED"
            or self.authorization_required is not True
        ):
            raise PreparationContractError("preparation cannot represent authorization")
        if self.execution_allowed or self.builder2_receipt_required_before_capture:
            raise PreparationContractError(
                "preparation cannot authorize execution or require B2 before capture"
            )
        if self.builder2_receipt_issuer_exposed is not False:
            raise PreparationContractError(
                "preparation cannot expose a Builder-2 issuer"
            )
        if (
            not self.no_network_guarantee
            or not self.discovery_calls_separated_from_odds
        ):
            raise PreparationContractError("preparation network guarantees are invalid")
        if (
            not self.zero_spend_invariant
            or self.monetary_spend != 0
            or not self.no_bet
            or self.betting
            or self.publication
            or self.production_activation
            or self.sealed_data_access
        ):
            raise PreparationContractError("preparation violates a safety invariant")
        executable = any(
            provider.executable and provider.expected_network_request_count > 0
            for provider in self.providers
            if provider.enabled
        )
        if (self.preparation_status == PREPARATION_READY) != executable:
            raise PreparationContractError(
                "preparation status does not match executable paths"
            )
        if not self.known_blockers and self.preparation_status == PREPARATION_BLOCKED:
            raise PreparationContractError("blocked preparation must record a blocker")
        if not _required_text(self.preparation_id, "preparation_id").startswith(
            PREPARATION_ID_PREFIX
        ):
            raise PreparationContractError("preparation ID namespace is invalid")
        if verify_digest:
            expected_digest = _digest(self._digest_payload())
            if self.preparation_digest != expected_digest:
                raise PreparationContractError("preparation digest mismatch")
            if self.preparation_id != f"{PREPARATION_ID_PREFIX}{expected_digest[:32]}":
                raise PreparationContractError(
                    "preparation ID is not derived from its digest"
                )

    def as_payload(
        self, *, validate: bool = True, include_digest: bool = True
    ) -> dict[str, object]:
        if validate:
            self.validate()
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "preparation_id": self.preparation_id,
            "preparation_status": self.preparation_status,
            "fixture_identity": dict(self.fixture_identity),
            "league": self.league,
            "kickoff": self.kickoff.isoformat(),
            "market_type": MARKET_PREMATCH_1X2,
            "timing_policy": dict(self.timing_policy),
            "timing_policy_digest": self.timing_policy_digest,
            "timing_policy_reference": self.timing_policy_reference,
            "configured_provider_order": list(self.configured_provider_order),
            "maximum_total_network_requests": self.maximum_total_network_requests,
            "maximum_total_quota_cost_units": self.maximum_total_quota_cost_units,
            "per_provider_maximums": {
                provider: dict(limits)
                for provider, limits in self.per_provider_maximums.items()
            },
            "providers": [provider.as_payload() for provider in self.providers],
            "expected_sequential_execution_plan": [
                action.as_payload()
                for action in self.expected_sequential_execution_plan
            ],
            "known_blockers": list(self.known_blockers),
            "ceo_decisions_required": list(self.ceo_decisions_required),
            "authorization_required": self.authorization_required,
            "authorization_status": self.authorization_status,
            "execution_allowed": self.execution_allowed,
            "builder2_receipt_required_before_capture": self.builder2_receipt_required_before_capture,
            "builder2_receipt_issuer_exposed": self.builder2_receipt_issuer_exposed,
            "no_network_guarantee": self.no_network_guarantee,
            "discovery_calls_separated_from_odds": self.discovery_calls_separated_from_odds,
            "monetary_spend": self.monetary_spend,
            "zero_spend_invariant": self.zero_spend_invariant,
            "no_bet": self.no_bet,
            "betting": self.betting,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "sealed_data_access": self.sealed_data_access,
            "storage_scope": _STORAGE_SCOPE,
        }
        if include_digest:
            payload["preparation_digest"] = self.preparation_digest
        return payload

    @classmethod
    def from_payload(
        cls, value: Mapping[str, object]
    ) -> ControlledShadowRunPreparationV1:
        if not isinstance(value, Mapping):
            raise PreparationContractError("preparation artifact must be a mapping")
        if value.get("market_type") != MARKET_PREMATCH_1X2:
            raise PreparationContractError("preparation market type is invalid")
        if value.get("storage_scope") != _STORAGE_SCOPE:
            raise PreparationContractError("preparation storage scope is invalid")
        raw_order = value.get("configured_provider_order")
        if isinstance(raw_order, (str, bytes)) or not isinstance(raw_order, Sequence):
            raise PreparationContractError(
                "configured provider order must be a sequence"
            )
        raw_providers = value.get("providers")
        if isinstance(raw_providers, (str, bytes)) or not isinstance(
            raw_providers, Sequence
        ):
            raise PreparationContractError("providers must be a sequence")
        raw_plan = value.get("expected_sequential_execution_plan")
        if isinstance(raw_plan, (str, bytes)) or not isinstance(raw_plan, Sequence):
            raise PreparationContractError("execution plan must be a sequence")
        raw_per_provider_maximums = value.get("per_provider_maximums")
        if not isinstance(raw_per_provider_maximums, Mapping):
            raise PreparationContractError("per-provider maximums must be a mapping")
        raw_blockers = value.get("known_blockers", ())
        if isinstance(raw_blockers, (str, bytes)) or not isinstance(
            raw_blockers, Sequence
        ):
            raise PreparationContractError("known_blockers must be a sequence")
        raw_decisions = value.get("ceo_decisions_required", ())
        if isinstance(raw_decisions, (str, bytes)) or not isinstance(
            raw_decisions, Sequence
        ):
            raise PreparationContractError("ceo_decisions_required must be a sequence")
        fixture_identity = value.get("fixture_identity")
        if not isinstance(fixture_identity, Mapping):
            raise PreparationContractError("fixture identity must be a mapping")
        manifest = cls(
            schema_version=_required_text(
                value.get("schema_version"), "schema_version"
            ),
            preparation_id=_required_text(
                value.get("preparation_id"), "preparation_id"
            ),
            preparation_status=_required_text(
                value.get("preparation_status"), "preparation_status"
            ),
            fixture_identity=dict(fixture_identity),
            league=_required_text(value.get("league"), "league"),
            kickoff=_parse_datetime(value.get("kickoff"), "kickoff"),
            timing_policy=value.get("timing_policy"),
            timing_policy_digest=_required_text(
                value.get("timing_policy_digest"), "timing_policy_digest"
            ),
            timing_policy_reference=_required_text(
                value.get("timing_policy_reference"), "timing_policy_reference"
            ),
            configured_provider_order=tuple(
                _required_text(item, "configured provider") for item in raw_order
            ),
            maximum_total_network_requests=_strict_nonnegative_int(
                value.get("maximum_total_network_requests"),
                "maximum_total_network_requests",
            ),
            maximum_total_quota_cost_units=_strict_nonnegative_number(
                value.get("maximum_total_quota_cost_units"),
                "maximum_total_quota_cost_units",
            ),
            per_provider_maximums=dict(raw_per_provider_maximums),
            providers=tuple(
                ProviderPreparationManifest.from_payload(item) for item in raw_providers
            ),
            expected_sequential_execution_plan=tuple(
                PreparationAction.from_payload(item) for item in raw_plan
            ),
            known_blockers=tuple(
                _required_text(item, "known blocker") for item in raw_blockers
            ),
            ceo_decisions_required=tuple(
                _required_text(item, "CEO decision") for item in raw_decisions
            ),
            authorization_required=_strict_bool(
                value.get("authorization_required"), "authorization_required"
            ),
            authorization_status=_required_text(
                value.get("authorization_status"), "authorization_status"
            ),
            execution_allowed=_strict_bool(
                value.get("execution_allowed"), "execution_allowed"
            ),
            builder2_receipt_required_before_capture=_strict_bool(
                value.get("builder2_receipt_required_before_capture"),
                "builder2_receipt_required_before_capture",
            ),
            builder2_receipt_issuer_exposed=_strict_bool(
                value.get("builder2_receipt_issuer_exposed"),
                "builder2_receipt_issuer_exposed",
            ),
            no_network_guarantee=_strict_bool(
                value.get("no_network_guarantee"), "no_network_guarantee"
            ),
            discovery_calls_separated_from_odds=_strict_bool(
                value.get("discovery_calls_separated_from_odds"),
                "discovery_calls_separated_from_odds",
            ),
            monetary_spend=_strict_nonnegative_number(
                value.get("monetary_spend"), "monetary_spend"
            ),
            no_bet=_strict_bool(value.get("no_bet"), "no_bet"),
            betting=_strict_bool(value.get("betting"), "betting"),
            publication=_strict_bool(value.get("publication"), "publication"),
            production_activation=_strict_bool(
                value.get("production_activation"), "production_activation"
            ),
            sealed_data_access=_strict_bool(
                value.get("sealed_data_access"), "sealed_data_access"
            ),
            zero_spend_invariant=_strict_bool(
                value.get("zero_spend_invariant"), "zero_spend_invariant"
            ),
            preparation_digest=_required_text(
                value.get("preparation_digest"), "preparation_digest"
            ),
        )
        manifest.validate()
        return manifest


def _parse_provider_order(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PreparationContractError("provider order must be a sequence")
    order = tuple(_required_text(item, "provider order item") for item in value)
    if not order:
        raise PreparationContractError("provider order must not be empty")
    if len(set(order)) != len(order):
        raise PreparationContractError("provider order contains a duplicate")
    unsupported = sorted(set(order) - set(SUPPORTED_PREPARATION_PROVIDERS))
    if unsupported:
        raise PreparationContractError(
            f"provider order contains unsupported provider: {', '.join(unsupported)}"
        )
    return order


def _parse_limits(
    value: Mapping[str, object] | None,
    *,
    name: str,
) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PreparationContractError(f"{name} must be a mapping")
    unknown = sorted(set(value) - set(SUPPORTED_PREPARATION_PROVIDERS))
    if unknown:
        raise PreparationContractError(f"{name} contains unsupported provider")
    result: dict[str, object] = {}
    for provider, raw in value.items():
        result[str(provider)] = raw
    return result


def _provider_state_map(
    value: Mapping[str, object] | None,
) -> dict[str, ProviderReadinessEvidence]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PreparationContractError("provider readiness state must be a mapping")
    unknown = sorted(set(value) - set(SUPPORTED_PREPARATION_PROVIDERS))
    if unknown:
        raise PreparationContractError(
            "provider readiness state contains unsupported provider"
        )
    return {
        provider: ProviderReadinessEvidence.from_mapping(raw)
        for provider, raw in value.items()
    }


def _credentials(value: Mapping[str, object] | None) -> dict[str, bool | None]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PreparationContractError("credential presence must be a mapping")
    unknown = sorted(set(value) - set(SUPPORTED_PREPARATION_PROVIDERS))
    if unknown:
        raise PreparationContractError(
            "credential presence contains unsupported provider"
        )
    return {
        provider: _strict_optional_bool(raw, f"credential presence for {provider}")
        for provider, raw in value.items()
    }


def _max_request_for_provider(
    provider: str,
    raw: object,
    default: int,
) -> int:
    if raw is None:
        return default
    if isinstance(raw, Mapping):
        raw = raw.get("network_requests", raw.get("request_count"))
    return _strict_nonnegative_int(raw, f"maximum request count for {provider}")


def _max_cost_for_provider(
    provider: str,
    raw: object,
    default: float,
) -> float:
    if raw is None:
        return default
    if isinstance(raw, Mapping):
        raw = raw.get("quota_cost_units", raw.get("cost_units"))
    return _strict_nonnegative_number(raw, f"maximum quota cost for {provider}")


def _quota_payload(quota: QuotaSnapshot | None) -> dict[str, object]:
    return (
        quota.as_payload()
        if quota
        else {
            "used": None,
            "remaining": None,
            "reset_at": None,
            "rate_limit": None,
            "rate_remaining": None,
            "rate_reset_at": None,
        }
    )


def _provider_manifest(
    provider: str,
    order: tuple[str, ...],
    credential_present: bool | None,
    evidence: ProviderReadinessEvidence,
    max_request_raw: object,
    max_cost_raw: object,
    used_requests: int,
    used_cost: float,
    total_request_budget: int,
    total_cost_budget: float,
) -> tuple[ProviderPreparationManifest, int, float, list[str]]:
    spec = _PROVIDER_SPECS[provider]
    configured = provider in order
    position = order.index(provider) if configured else None
    failures = list(spec.expected_failures)
    blockers: list[str] = []
    identity = evidence.identity_state or UNKNOWN
    readiness = evidence.readiness_state or UNKNOWN
    fixture_known = evidence.fixture_id_known
    discovery_required = evidence.discovery_required
    raw_request_count = 0
    raw_cost = 0.0
    actions: list[tuple[str, str, str]] = []

    if not configured:
        reason = "not in configured cascade order; no request planned"
        manifest = ProviderPreparationManifest(
            provider=provider,
            display_name=spec.display_name,
            configured_cascade_position=None,
            enabled=False,
            candidate_state="NOT_CONFIGURED",
            credential_required=True,
            credential_present=credential_present,
            provider_fixture_id_known=fixture_known,
            provider_fixture_id=evidence.provider_fixture_id,
            fixture_discovery_required=discovery_required,
            identity_state=identity,
            readiness_state=readiness,
            expected_endpoint_action_class="NOT_CONFIGURED",
            expected_network_request_count=0,
            expected_quota_cost_units=0.0,
            maximum_allowed_request_count=0,
            maximum_allowed_quota_cost_units=0.0,
            current_known_quota=_quota_payload(evidence.quota),
            source_timestamp_capability=spec.source_timestamp_capability,
            delayed_data_semantics=spec.delayed_data_semantics,
            bookmaker_constraint=spec.bookmaker_constraint,
            expected_failure_classifications=tuple(failures),
            prerequisites=spec.prerequisites,
            executable=False,
            reason_executable_or_blocked=reason,
            pagination_risk=evidence.pagination_risk,
            body_error_taxonomy=evidence.body_error_taxonomy,
            monetary_spend=0.0,
        )
        manifest.validate()
        return manifest, used_requests, used_cost, blockers

    if not evidence.state_supplied:
        blockers.append("provider readiness, fixture, and quota state is unknown")
    if credential_present is None:
        blockers.append(
            "credential presence is unknown; no credential value is accepted"
        )
    elif credential_present is False:
        blockers.append("credential is explicitly absent; no network request planned")
        failures.append("CREDENTIAL_MISSING")
    if readiness == UNKNOWN:
        blockers.append("provider readiness state is unknown")
    elif readiness not in {state.value for state in _READY_READINESS}:
        blockers.append(f"provider readiness is not observation-ready: {readiness}")
    if identity == UNKNOWN or fixture_known is None or discovery_required is None:
        blockers.append("provider fixture/discovery identity state is unknown")
    else:
        if identity == ProviderIdentityResolutionState.RESOLVED.value:
            if not fixture_known or not evidence.provider_fixture_id:
                raise PreparationContractError(
                    f"{provider} resolved identity must include a provider fixture ID"
                )
            if discovery_required:
                raise PreparationContractError(
                    f"{provider} resolved identity cannot still require discovery"
                )
            actions.append(
                (
                    "ODDS",
                    spec.odds_action,
                    "exact provider fixture identity is already resolved",
                )
            )
        elif identity == ProviderIdentityResolutionState.DISCOVERY_REQUIRED.value:
            if fixture_known or evidence.provider_fixture_id:
                raise PreparationContractError(
                    f"{provider} discovery-required identity cannot include a known fixture ID"
                )
            if not discovery_required:
                raise PreparationContractError(
                    f"{provider} discovery-required identity must mark discovery_required"
                )
            if spec.discovery_action is None:
                actions.append(
                    (
                        "ODDS_AND_EVENT_DISCOVERY",
                        spec.odds_action,
                        "sport-level response must resolve the exact fixture before any promotion",
                    )
                )
            else:
                actions.append(
                    (
                        "FIXTURE_DISCOVERY",
                        spec.discovery_action,
                        "only if discovery returns one exact fixture identity",
                    )
                )
                actions.append(
                    (
                        "ODDS",
                        spec.odds_action,
                        "only if discovery resolves uniquely; otherwise fail closed",
                    )
                )
        elif identity == ProviderIdentityResolutionState.UNRESOLVED.value:
            blockers.append(
                "provider fixture identity is unresolved; no discovery is invented"
            )
            failures.append("UNRESOLVED")
        elif identity == ProviderIdentityResolutionState.AMBIGUOUS.value:
            blockers.append(
                "provider fixture identity is ambiguous; no odds request planned"
            )
            failures.append("AMBIGUOUS")
        else:
            raise PreparationContractError(f"{provider} identity state is invalid")
    if provider == "the_odds_api" and evidence.pagination_risk:
        blockers.append("pagination risk is not applicable to the sport-level endpoint")
    pagination_risk = evidence.pagination_risk
    body_error_taxonomy = evidence.body_error_taxonomy

    raw_request_count = len(actions)
    raw_cost = raw_request_count * evidence.quota_cost_units_per_request
    if not actions and not blockers:
        blockers.append("no safe provider action could be planned")
    quota_remaining = evidence.quota.remaining if evidence.quota else None
    if quota_remaining is None:
        blockers.append(
            "known quota/readiness remaining capacity is unknown; no free availability assumed"
        )
    elif raw_cost > quota_remaining:
        blockers.append(
            f"known quota is insufficient for planned cost ({quota_remaining} remaining < {raw_cost:g})"
        )
        failures.append("QUOTA_EXHAUSTED")
        if provider == "the_odds_api" and quota_remaining == 0:
            if evidence.quota is not None and evidence.quota.used == 500:
                blockers.append(
                    "The Odds API used=500/remaining=0; zero network requests are planned"
                )
            else:
                blockers.append(
                    "The Odds API remaining quota is zero; zero network requests are planned"
                )
    if provider == "the_odds_api" and quota_remaining == 0:
        failures.append("QUOTA_EXHAUSTED")

    default_max_requests = raw_request_count
    default_max_cost = raw_cost
    max_requests = _max_request_for_provider(
        provider, max_request_raw, default_max_requests
    )
    max_cost = _max_cost_for_provider(provider, max_cost_raw, default_max_cost)
    if raw_request_count > max_requests:
        blockers.append("planned request count exceeds the provider request maximum")
    if raw_cost > max_cost + 1e-9:
        blockers.append("planned quota cost exceeds the provider quota-cost maximum")
    if not blockers and raw_request_count > 0:
        if used_requests + raw_request_count > total_request_budget:
            blockers.append("planned request count exceeds the global request budget")
        if used_cost + raw_cost > total_cost_budget + 1e-9:
            blockers.append("planned quota cost exceeds the global quota-cost budget")

    executable = not blockers and raw_request_count > 0
    planned_actions: tuple[PreparationAction, ...] = ()
    if executable:
        planned_actions = tuple(
            PreparationAction(
                sequence=index,
                provider=provider,
                action_class=action_class,
                endpoint_or_action=endpoint,
                network_request_count=1,
                quota_cost_units=evidence.quota_cost_units_per_request,
                condition=condition,
            )
            for index, (action_class, endpoint, condition) in enumerate(actions)
        )
        used_requests += raw_request_count
        used_cost += raw_cost
    else:
        raw_request_count = 0
        raw_cost = 0.0
    unique_failures = tuple(dict.fromkeys(failures))
    reason = (
        "executable plan prepared; later execution still requires an explicit CEO authorization artifact/context for this specific run"
        if executable
        else "not executable: " + "; ".join(dict.fromkeys(blockers))
    )
    manifest = ProviderPreparationManifest(
        provider=provider,
        display_name=spec.display_name,
        configured_cascade_position=position,
        enabled=True,
        candidate_state="CANDIDATE_ONLY",
        credential_required=True,
        credential_present=credential_present,
        provider_fixture_id_known=fixture_known,
        provider_fixture_id=evidence.provider_fixture_id,
        fixture_discovery_required=discovery_required,
        identity_state=identity,
        readiness_state=readiness,
        expected_endpoint_action_class=(
            "DISCOVERY_THEN_ODDS"
            if len(actions) == 2
            else (actions[0][0] if actions else "PRECHECK_ONLY")
        ),
        expected_network_request_count=raw_request_count,
        expected_quota_cost_units=raw_cost,
        maximum_allowed_request_count=max_requests,
        maximum_allowed_quota_cost_units=max_cost,
        current_known_quota=_quota_payload(evidence.quota),
        source_timestamp_capability=spec.source_timestamp_capability,
        delayed_data_semantics=spec.delayed_data_semantics,
        bookmaker_constraint=spec.bookmaker_constraint,
        expected_failure_classifications=unique_failures,
        prerequisites=spec.prerequisites,
        executable=executable,
        reason_executable_or_blocked=reason,
        pagination_risk=pagination_risk,
        body_error_taxonomy=body_error_taxonomy,
        planned_actions=planned_actions,
        monetary_spend=0.0,
    )
    manifest.validate()
    if blockers:
        return (
            manifest,
            used_requests,
            used_cost,
            [f"{provider}: {item}" for item in dict.fromkeys(blockers)],
        )
    return manifest, used_requests, used_cost, []


def build_controlled_shadow_preparation(
    fixture: Fixture | Mapping[str, object],
    timing_policy: CascadeTimingPolicy | Mapping[str, object],
    provider_order: Sequence[str],
    credential_presence: Mapping[str, object] | None,
    provider_readiness: Mapping[str, object] | None,
    maximum_total_network_requests: int,
    maximum_total_quota_cost_units: float,
    *,
    per_provider_maximum_requests: Mapping[str, object] | None = None,
    per_provider_maximum_quota_cost_units: Mapping[str, object] | None = None,
    monetary_spend: float = 0.0,
    betting: bool = False,
    publication: bool = False,
    production_activation: bool = False,
    sealed_data_access: bool = False,
) -> ControlledShadowRunPreparationV1:
    """Build a deterministic plan without calling any provider or transport."""

    target = _fixture_from_input(fixture)
    timing = _timing_from_input(timing_policy)
    order = _parse_provider_order(provider_order)
    credentials = _credentials(credential_presence)
    states = _provider_state_map(provider_readiness)
    total_requests = _strict_nonnegative_int(
        maximum_total_network_requests, "maximum_total_network_requests"
    )
    total_cost = _strict_nonnegative_number(
        maximum_total_quota_cost_units, "maximum_total_quota_cost_units"
    )
    spend = _strict_nonnegative_number(monetary_spend, "monetary_spend")
    if spend != 0:
        raise PreparationContractError("preparation monetary spend must be zero")
    for name, flag in (
        ("betting", betting),
        ("publication", publication),
        ("production_activation", production_activation),
        ("sealed_data_access", sealed_data_access),
    ):
        if not isinstance(flag, bool):
            raise PreparationContractError(f"{name} must be boolean")
        if flag:
            raise PreparationContractError(f"{name} must remain disabled")
    request_limits = _parse_limits(
        per_provider_maximum_requests, name="per-provider request maximums"
    )
    cost_limits = _parse_limits(
        per_provider_maximum_quota_cost_units, name="per-provider quota maximums"
    )

    used_requests = 0
    used_cost = 0.0
    manifests: list[ProviderPreparationManifest] = []
    blockers: list[str] = []
    for provider in SUPPORTED_PREPARATION_PROVIDERS:
        evidence = states.get(provider)
        if evidence is None:
            evidence = ProviderReadinessEvidence(state_supplied=False)
        manifest, used_requests, used_cost, provider_blockers = _provider_manifest(
            provider,
            order,
            credentials.get(provider),
            evidence,
            request_limits.get(provider),
            cost_limits.get(provider),
            used_requests,
            used_cost,
            total_requests,
            total_cost,
        )
        manifests.append(manifest)
        blockers.extend(provider_blockers)

    plan: list[PreparationAction] = []
    for provider in order:
        manifest = next(item for item in manifests if item.provider == provider)
        if manifest.executable:
            for action in manifest.planned_actions:
                plan.append(replace(action, sequence=len(plan)))
        else:
            plan.append(
                PreparationAction(
                    sequence=len(plan),
                    provider=provider,
                    action_class="PRECHECK_ONLY",
                    endpoint_or_action="no network dispatch",
                    network_request_count=0,
                    quota_cost_units=0.0,
                    condition=manifest.reason_executable_or_blocked,
                )
            )
    executable = any(item.executable for item in manifests if item.enabled)
    status = PREPARATION_READY if executable else PREPARATION_BLOCKED
    if status == PREPARATION_BLOCKED and not blockers:
        blockers.append("no configured provider has all technical prerequisites")
    decisions = [
        "explicit CEO authorization artifact/context for this specific future controlled shadow run",
    ]
    if status == PREPARATION_BLOCKED:
        decisions.append(
            "resolve all preparation blockers before requesting authorization"
        )
    timing_payload = _timing_payload(timing)
    timing_digest = _digest(timing_payload)
    per_provider_maximums = {
        item.provider: {
            "network_requests": item.maximum_allowed_request_count,
            "quota_cost_units": item.maximum_allowed_quota_cost_units,
        }
        for item in manifests
    }
    base = ControlledShadowRunPreparationV1(
        schema_version=PREPARATION_SCHEMA_VERSION,
        preparation_id="",
        preparation_status=status,
        fixture_identity=_fixture_payload(target),
        league=target.league_code,
        kickoff=target.kickoff,
        timing_policy=timing_payload,
        timing_policy_digest=timing_digest,
        timing_policy_reference=f"timing-policy:{timing_digest[:32]}",
        configured_provider_order=order,
        maximum_total_network_requests=total_requests,
        maximum_total_quota_cost_units=total_cost,
        per_provider_maximums=per_provider_maximums,
        providers=tuple(manifests),
        expected_sequential_execution_plan=tuple(plan),
        known_blockers=tuple(dict.fromkeys(blockers)),
        ceo_decisions_required=tuple(dict.fromkeys(decisions)),
        monetary_spend=0.0,
        no_bet=True,
        betting=False,
        publication=False,
        production_activation=False,
        sealed_data_access=False,
    )
    preparation_digest = _digest(base._digest_payload())
    result = replace(
        base,
        preparation_id=f"{PREPARATION_ID_PREFIX}{preparation_digest[:32]}",
        preparation_digest=preparation_digest,
    )
    result.validate()
    return result


def preparation_from_input_payload(
    value: Mapping[str, object],
) -> ControlledShadowRunPreparationV1:
    """Parse the CLI input manifest; no Builder-2 receipt is accepted or needed."""

    if not isinstance(value, Mapping):
        raise PreparationContractError("preparation input must be a mapping")
    forbidden = {
        "builder2_receipt",
        "qualification_receipt",
        "ceo_authorization",
        "authorization_artifact",
    }
    if forbidden.intersection(value):
        raise PreparationContractError(
            "authorization and Builder-2 receipt are not preparation inputs"
        )
    fixture = value.get("fixture", value.get("fixture_identity"))
    timing = value.get("timing_policy")
    if fixture is None or timing is None:
        raise PreparationContractError("fixture and timing_policy are required")
    return build_controlled_shadow_preparation(
        fixture,
        timing,
        value.get("provider_order", value.get("configured_provider_order")),  # type: ignore[arg-type]
        value.get("credential_presence"),  # type: ignore[arg-type]
        value.get("provider_readiness", value.get("known_provider_state")),  # type: ignore[arg-type]
        value.get("maximum_total_network_requests"),  # type: ignore[arg-type]
        value.get("maximum_total_quota_cost_units"),  # type: ignore[arg-type]
        per_provider_maximum_requests=value.get("per_provider_maximum_requests"),  # type: ignore[arg-type]
        per_provider_maximum_quota_cost_units=value.get(
            "per_provider_maximum_quota_cost_units"
        ),  # type: ignore[arg-type]
        monetary_spend=value.get("monetary_spend", 0.0),  # type: ignore[arg-type]
        betting=value.get("betting", False),  # type: ignore[arg-type]
        publication=value.get("publication", False),  # type: ignore[arg-type]
        production_activation=value.get("production_activation", False),  # type: ignore[arg-type]
        sealed_data_access=value.get("sealed_data_access", False),  # type: ignore[arg-type]
    )


def write_preparation(
    preparation: ControlledShadowRunPreparationV1, output_path: str | Path
) -> Path:
    """Write only to a caller-selected external or test path."""

    preparation.validate()
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        raise PreparationContractError("preparation output path must be absolute")
    resolved = path.resolve()
    active_root = Path(__file__).resolve().parents[3]
    if resolved == active_root or active_root in resolved.parents:
        raise PreparationContractError(
            "preparation output cannot be inside the active checkout"
        )
    lowered = str(resolved).lower()
    if any(
        token in lowered
        for token in ("/ledger", "docs/data", "/src/", "/scripts/", "/tests/")
    ):
        raise PreparationContractError(
            "preparation output path is not an external preparation location"
        )
    atomic_write_json(path, preparation.as_payload(), indent=2, sort_keys=True)
    return path


def load_preparation(path: str | Path) -> ControlledShadowRunPreparationV1:
    """Load and validate a preparation artifact without executing anything."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise PreparationContractError("preparation input path must be absolute")
    try:
        payload = json.loads(candidate.read_text())
    except (OSError, ValueError) as exc:
        raise PreparationContractError("preparation artifact is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise PreparationContractError("preparation artifact must be a mapping")
    return ControlledShadowRunPreparationV1.from_payload(payload)


__all__ = [
    "PREPARATION_BLOCKED",
    "PREPARATION_ID_PREFIX",
    "PREPARATION_READY",
    "PREPARATION_SCHEMA_VERSION",
    "SUPPORTED_PREPARATION_PROVIDERS",
    "ControlledShadowRunPreparationV1",
    "PreparationAction",
    "PreparationContractError",
    "ProviderPreparationManifest",
    "ProviderReadinessEvidence",
    "build_controlled_shadow_preparation",
    "load_preparation",
    "preparation_from_input_payload",
    "write_preparation",
]
