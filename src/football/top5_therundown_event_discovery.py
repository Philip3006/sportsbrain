"""Non-authorizing TheRundown provider-event discovery gate.

This module solves only the bootstrap problem for the later strict
five-league network authorization.  It resolves provider event IDs from the
same dated league snapshot that is explicitly authorized for discovery.  It
does not issue the real-run authorization, create a receipt, grant provider
authority, or activate any production path.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Protocol

from src.football.odds.therundown import (
    THERUNDOWN_BASE_URL,
    THERUNDOWN_MONEYLINE_MARKET_ID,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
    TheRundownExperimentalAdapter,
)
from src.football.production_contracts import Fixture, _utc
from src.football.provider_cascade.contracts import (
    TOP5_LEAGUE_CODES,
    CascadeTimingPolicy,
    digest_record,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST,
    THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
    TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
    TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
    TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkParticipantScopeV1,
    TheRundownNetworkRequestScopeV1,
)
from src.football.top5_therundown_shadow_canary import (
    TheRundownCanaryTargetV1,
)

DISCOVERY_SCHEMA_VERSION = "top5-therundown-provider-event-discovery-v1"
DISCOVERY_AUTHORIZATION_SCHEMA_VERSION = (
    "top5-therundown-provider-event-discovery-authorization-v1"
)
DISCOVERY_EVIDENCE_SCHEMA_VERSION = (
    "top5-therundown-provider-event-discovery-evidence-v1"
)
CURRENT_TARGET_MANIFEST_SCHEMA_VERSION = "top5-current-discovery-target-manifest-v1"
DISCOVERY_LEAGUE_ORDER = ("EPL", "BL1", "LL", "SA", "L1")
DISCOVERY_KICKOFF_TOLERANCE_SECONDS = 60
DISCOVERY_MINIMUM_COMBINED_HEADROOM = 550
DISCOVERY_QUOTA_PROOF_MAX_AGE_SECONDS = 300
CURRENT_TARGET_MANIFEST_MAX_AGE_SECONDS = 3600
B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION = "top5-therundown-quota-proof-package-v1"
B4_QUOTA_PROOF_SCHEMA_VERSION = "top5-therundown-quota-proof-v1"
B4_QUOTA_PROOF_EXECUTION_PHASE = "quota_proof"
B4_QUOTA_PROOF_MAX_DATAPOINTS = (
    THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST
)
B4_QUOTA_PROOF_SPORT_ID = 3
B4_QUOTA_PROOF_MARKET_IDS = ("1",)
B4_QUOTA_PROOF_AFFILIATE_IDS = ("19",)
B4_QUOTA_PROOF_SNAPSHOT_MAX_OFFSET_DAYS = 1
DISCOVERY_CONSUMPTION_SCHEMA_VERSION = (
    "top5-therundown-provider-event-discovery-consumption-v1"
)
B4_QUOTA_PROOF_RELATIVE_PATH = "football/top5/therundown_b4_quota_proof.json"
DISCOVERY_CONSUMPTION_RELATIVE_PATH = (
    "football/top5/therundown_event_discovery_consumption.json"
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_PREMATCH_STATUSES = frozenset(
    {
        "scheduled",
        "statusscheduled",
        "notstarted",
        "statusnotstarted",
        "prematch",
        "upcoming",
        "statusupcoming",
    }
)
_STATE_REJECT_FLAGS = (
    "closed",
    "completed",
    "in_play",
    "inplay",
    "live",
    "is_closed",
    "is_completed",
    "is_in_play",
    "is_live",
)


class EventDiscoveryContractError(ValueError):
    """Malformed discovery input or non-authorizing evidence."""


class EventDiscoveryExecutionBlocked(EventDiscoveryContractError):
    """Fail-closed refusal during the bounded discovery stage."""

    def __init__(
        self,
        message: str,
        *,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


def _operator_runtime_state_path(relative_path: str) -> Path:
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise EventDiscoveryExecutionBlocked(
            "operator runtime-state account is unavailable"
        ) from exc
    if not account_home.is_absolute():
        raise EventDiscoveryExecutionBlocked("operator runtime-state path is invalid")
    return (
        account_home
        / "Library"
        / "Application Support"
        / "SportsBrain"
        / "runtime-state"
        / relative_path
    )


def b4_quota_proof_state_path() -> Path:
    """Return the canonical operator-owned B4 quota-proof artifact path."""

    return _operator_runtime_state_path(B4_QUOTA_PROOF_RELATIVE_PATH)


def discovery_authorization_consumption_state_path() -> Path:
    """Return the canonical operator-owned discovery replay state path."""

    return _operator_runtime_state_path(DISCOVERY_CONSUMPTION_RELATIVE_PATH)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventDiscoveryContractError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise EventDiscoveryContractError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _utc_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        try:
            return _utc(value, name)
        except Exception as exc:
            raise EventDiscoveryContractError(f"{name} must be timezone-aware") from exc
    if not isinstance(value, str) or not value.strip():
        raise EventDiscoveryContractError(f"{name} must be an ISO timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except (TypeError, ValueError) as exc:
        raise EventDiscoveryContractError(f"{name} must be an ISO timestamp") from exc


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return _utc_datetime(value, "timestamp").isoformat()
    return value


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class TheRundownB4QuotaProofV1:
    """Validated normalized access to the real PR-144 nested proof."""

    proof_id: str
    authorization_id: str
    provider: str
    sport_id: int
    snapshot_date: date
    account_scope: str
    remaining_datapoints: int
    response_started_at: datetime
    response_finished_at: datetime
    quota_reset_at: datetime
    response_digest: str
    evidence_digest: str
    request_shape_digest: str
    quota_used_datapoints: int | None = None
    quota_limit_datapoints: int | None = None
    quota_period: str | None = None
    quota_tier: str | None = None

    @staticmethod
    def _timestamp(raw: Mapping[str, object], name: str) -> datetime:
        value = raw.get(name)
        if not isinstance(value, str):
            raise EventDiscoveryContractError(f"quota proof {name} is invalid")
        return _utc_datetime(value, f"quota proof {name}")

    @classmethod
    def _validate_package(
        cls, package: object, *, now: datetime
    ) -> Mapping[str, object]:
        if not isinstance(package, Mapping):
            raise EventDiscoveryContractError(
                "B4 quota proof package must be an object"
            )
        if package.get("schema_version") != B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION:
            raise EventDiscoveryContractError(
                "unsupported B4 quota proof package schema"
            )
        if package.get("execution_phase") != B4_QUOTA_PROOF_EXECUTION_PHASE:
            raise EventDiscoveryExecutionBlocked(
                "B4 quota proof package execution phase is invalid"
            )
        if "proof" not in package:
            raise EventDiscoveryContractError("B4 quota proof package proof is missing")
        if set(package) != {
            "schema_version",
            "execution_phase",
            "proof",
            "request",
            "spend_control",
            "safety",
        }:
            raise EventDiscoveryContractError(
                "B4 quota proof package shape is not the PR-144 format"
            )
        proof = package.get("proof")
        request = package.get("request")
        spend_control = package.get("spend_control")
        safety = package.get("safety")
        if not isinstance(proof, Mapping):
            raise EventDiscoveryContractError("B4 quota proof package proof is missing")
        if not isinstance(request, Mapping):
            raise EventDiscoveryContractError(
                "B4 quota proof package request is missing"
            )
        if not isinstance(spend_control, Mapping):
            raise EventDiscoveryContractError(
                "B4 quota proof package spend control is invalid"
            )
        if not isinstance(safety, Mapping):
            raise EventDiscoveryContractError(
                "B4 quota proof package safety is invalid"
            )
        if dict(safety) != {
            "five_league_requests": 0,
            "receipt_issued": False,
            "authority_changed": False,
            "activation": False,
            "publication": False,
            "betting": False,
            "monetary_spend_authorized": False,
        }:
            raise EventDiscoveryExecutionBlocked(
                "B4 quota proof package safety metadata is unsafe"
            )
        cls._validate_nested_proof(proof, request, now=now)
        return proof

    @classmethod
    def _validate_nested_proof(
        cls,
        proof: Mapping[str, object],
        request: Mapping[str, object],
        *,
        now: datetime,
    ) -> None:
        proof_keys = {
            "schema_version",
            "execution_phase",
            "proof_id",
            "provider",
            "sport_id",
            "snapshot_date",
            "account_scope",
            "authorization_package_digest",
            "configuration_digest",
            "authorization_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_identity",
            "request_shape_digest",
            "credential_binding_digest",
            "request_started_at",
            "response_finished_at",
            "billed_datapoints",
            "remaining_datapoints",
            "quota_used_datapoints",
            "quota_limit_datapoints",
            "quota_period",
            "quota_reset_at",
            "raw_header_evidence",
            "response_digest",
            "evidence_digest",
            "status_code",
            "request_count",
            "retry_count",
            "no_retry",
        }
        if set(proof) != proof_keys:
            raise EventDiscoveryContractError(
                "B4 nested proof shape is not the PR-144 format"
            )
        if proof.get("schema_version") != B4_QUOTA_PROOF_SCHEMA_VERSION:
            raise EventDiscoveryContractError(
                "unsupported nested B4 quota proof schema"
            )
        if proof.get("execution_phase") != B4_QUOTA_PROOF_EXECUTION_PHASE:
            raise EventDiscoveryExecutionBlocked(
                "nested B4 quota proof execution phase is invalid"
            )
        if proof.get("provider") != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryExecutionBlocked("B4 quota proof provider is invalid")
        if proof.get("sport_id") != B4_QUOTA_PROOF_SPORT_ID:
            raise EventDiscoveryExecutionBlocked("B4 quota proof sport is invalid")
        snapshot_raw = proof.get("snapshot_date")
        if not isinstance(snapshot_raw, str):
            raise EventDiscoveryContractError(
                "quota proof snapshot_date must be YYYY-MM-DD"
            )
        try:
            snapshot_date = date.fromisoformat(snapshot_raw)
        except ValueError as exc:
            raise EventDiscoveryContractError(
                "quota proof snapshot_date must be YYYY-MM-DD"
            ) from exc
        for name in (
            "proof_id",
            "account_scope",
            "authorization_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_identity",
        ):
            _text(proof.get(name), f"quota proof {name}")
        for name in (
            "authorization_package_digest",
            "configuration_digest",
            "request_shape_digest",
            "credential_binding_digest",
            "response_digest",
            "evidence_digest",
        ):
            _sha(proof.get(name), f"quota proof {name}")
        for name in (
            "request_started_at",
            "response_finished_at",
            "quota_reset_at",
        ):
            cls._timestamp(proof, name)
        started = cls._timestamp(proof, "request_started_at")
        finished = cls._timestamp(proof, "response_finished_at")
        reset = cls._timestamp(proof, "quota_reset_at")
        current = _utc_datetime(now, "quota proof validation now")
        current_date = current.date()
        if snapshot_date < current_date or snapshot_date > current_date + timedelta(
            days=B4_QUOTA_PROOF_SNAPSHOT_MAX_OFFSET_DAYS
        ):
            raise EventDiscoveryExecutionBlocked(
                "quota proof snapshot date is historical or outside the bounded window"
            )
        if finished < started or finished > current:
            raise EventDiscoveryExecutionBlocked(
                "quota proof response timestamps are invalid"
            )
        if (current - finished).total_seconds() > DISCOVERY_QUOTA_PROOF_MAX_AGE_SECONDS:
            raise EventDiscoveryExecutionBlocked("quota proof response is stale")
        if reset <= finished:
            raise EventDiscoveryExecutionBlocked(
                "quota proof rate-limit reset period is no longer valid"
            )
        int_fields = (
            "billed_datapoints",
            "remaining_datapoints",
            "quota_used_datapoints",
            "quota_limit_datapoints",
            "status_code",
            "request_count",
            "retry_count",
        )
        for name in int_fields:
            value = proof.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise EventDiscoveryContractError(f"quota proof {name} is invalid")
        if proof["remaining_datapoints"] < DISCOVERY_MINIMUM_COMBINED_HEADROOM:
            raise EventDiscoveryExecutionBlocked(
                "TOP5_PROVIDER_EVENT_ID_DISCOVERY — INSUFFICIENT_COMBINED_HEADROOM"
            )
        if not 200 <= proof["status_code"] < 300:
            raise EventDiscoveryExecutionBlocked("quota proof HTTP response is invalid")
        if not 0 < proof["billed_datapoints"] <= B4_QUOTA_PROOF_MAX_DATAPOINTS:
            raise EventDiscoveryExecutionBlocked(
                "quota proof billed datapoints exceed the per-request cap"
            )
        if proof["quota_limit_datapoints"] <= 0:
            raise EventDiscoveryExecutionBlocked("quota proof quota limit is invalid")
        if (
            proof["quota_used_datapoints"] + proof["remaining_datapoints"]
            != proof["quota_limit_datapoints"]
            or proof["billed_datapoints"] > proof["quota_used_datapoints"]
        ):
            raise EventDiscoveryExecutionBlocked("quota proof quota headers contradict")
        if proof["quota_period"] not in {"daily", "weekly", "monthly"}:
            raise EventDiscoveryExecutionBlocked("quota proof period is unsupported")
        if (
            proof["request_count"] != 1
            or proof["retry_count"] != 0
            or proof.get("no_retry") is not True
        ):
            raise EventDiscoveryExecutionBlocked(
                "quota proof must be exactly one request with zero retries"
            )
        headers = proof.get("raw_header_evidence")
        if not isinstance(headers, Mapping):
            raise EventDiscoveryContractError("quota proof raw headers are invalid")
        required_headers = {
            "x-datapoints",
            "x-datapoints-used",
            "x-datapoints-remaining",
            "x-datapoints-limit",
            "x-datapoints-period",
            "x-datapoints-reset",
            "x-tier",
            "x-rate-limit",
            "x-data-delay-seconds",
        }
        if not required_headers.issubset({str(key).casefold() for key in headers}):
            raise EventDiscoveryExecutionBlocked(
                "quota proof billing/rate/tier evidence is incomplete"
            )
        lowered_headers = {
            str(key).casefold(): str(value).strip() for key, value in headers.items()
        }
        for name, expected in (
            ("x-datapoints", proof["billed_datapoints"]),
            ("x-datapoints-used", proof["quota_used_datapoints"]),
            ("x-datapoints-remaining", proof["remaining_datapoints"]),
            ("x-datapoints-limit", proof["quota_limit_datapoints"]),
        ):
            try:
                actual = int(lowered_headers[name])
            except (KeyError, ValueError) as exc:
                raise EventDiscoveryContractError(
                    f"quota proof header {name} is invalid"
                ) from exc
            if actual != expected:
                raise EventDiscoveryExecutionBlocked(
                    f"quota proof header {name} does not match evidence"
                )
        if lowered_headers["x-datapoints-period"] != proof["quota_period"]:
            raise EventDiscoveryExecutionBlocked(
                "quota proof period header does not match"
            )
        if lowered_headers["x-tier"].casefold() != "free":
            raise EventDiscoveryExecutionBlocked("quota proof tier is not approved")
        for name in ("x-rate-limit", "x-data-delay-seconds"):
            try:
                if int(lowered_headers[name]) < 0:
                    raise ValueError
            except (KeyError, ValueError) as exc:
                raise EventDiscoveryContractError(
                    f"quota proof header {name} is invalid"
                ) from exc
        if (
            _utc_datetime(
                lowered_headers["x-datapoints-reset"], "quota proof header reset"
            )
            != reset
        ):
            raise EventDiscoveryExecutionBlocked(
                "quota proof reset header does not match evidence"
            )
        if proof["evidence_digest"].lower() != _digest(
            {key: value for key, value in proof.items() if key != "evidence_digest"}
        ):
            raise EventDiscoveryContractError("quota proof evidence digest mismatch")

        request_keys = {
            "proof_id",
            "provider",
            "sport_id",
            "snapshot_date",
            "authorization_package_digest",
            "configuration_digest",
            "authorization_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_identity",
            "adapter_version",
            "adapter_source_sha",
            "endpoint",
            "query",
            "request_shape_digest",
            "maximum_datapoints",
            "request_count",
            "retry_count",
        }
        if set(request) != request_keys:
            raise EventDiscoveryContractError(
                "B4 quota proof request shape is not the PR-144 format"
            )
        for name in (
            "proof_id",
            "authorization_package_digest",
            "configuration_digest",
            "authorization_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_identity",
            "adapter_version",
            "adapter_source_sha",
            "request_shape_digest",
        ):
            _text(request.get(name), f"quota proof request {name}")
        for name in (
            "authorization_package_digest",
            "configuration_digest",
            "adapter_source_sha",
            "request_shape_digest",
        ):
            _sha(request.get(name), f"quota proof request {name}")
        if request.get("provider") != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryExecutionBlocked(
                "quota proof request provider is invalid"
            )
        if request.get("sport_id") != B4_QUOTA_PROOF_SPORT_ID:
            raise EventDiscoveryExecutionBlocked("quota proof request sport is invalid")
        if request.get("snapshot_date") != snapshot_date.isoformat():
            raise EventDiscoveryExecutionBlocked(
                "quota proof request snapshot date does not match evidence"
            )
        for name in (
            "proof_id",
            "provider",
            "authorization_package_digest",
            "configuration_digest",
            "authorization_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_identity",
            "request_shape_digest",
        ):
            if request.get(name) != proof.get(name):
                raise EventDiscoveryExecutionBlocked(
                    f"quota proof request binding mismatch: {name}"
                )
        if request.get("endpoint") != (
            f"{THERUNDOWN_BASE_URL}/sports/{B4_QUOTA_PROOF_SPORT_ID}/events/"
            f"{snapshot_date.isoformat()}"
        ):
            raise EventDiscoveryExecutionBlocked("quota proof endpoint is invalid")
        expected_query = {
            "affiliate_ids": ",".join(B4_QUOTA_PROOF_AFFILIATE_IDS),
            "hide_closed": "true",
            "main_line": "true",
            "market_ids": ",".join(B4_QUOTA_PROOF_MARKET_IDS),
        }
        if request.get("query") != expected_query:
            raise EventDiscoveryExecutionBlocked("quota proof request query is invalid")
        if request["request_shape_digest"] != _digest(
            {
                "method": "GET",
                "endpoint": request["endpoint"],
                "query": expected_query,
            }
        ):
            raise EventDiscoveryExecutionBlocked("quota proof request shape is invalid")
        if (
            request["maximum_datapoints"] != B4_QUOTA_PROOF_MAX_DATAPOINTS
            or request["request_count"] != 1
            or request["retry_count"] != 0
        ):
            raise EventDiscoveryExecutionBlocked(
                "quota proof request limits are invalid"
            )

    @classmethod
    def from_package(
        cls, package: object, *, now: datetime
    ) -> TheRundownB4QuotaProofV1:
        proof = cls._validate_package(package, now=now)
        return cls(
            proof_id=proof["proof_id"],
            authorization_id=proof["authorization_id"],
            provider=proof["provider"],
            sport_id=proof["sport_id"],
            snapshot_date=date.fromisoformat(proof["snapshot_date"]),
            account_scope=proof["account_scope"],
            remaining_datapoints=proof["remaining_datapoints"],
            response_started_at=cls._timestamp(proof, "request_started_at"),
            response_finished_at=cls._timestamp(proof, "response_finished_at"),
            quota_reset_at=cls._timestamp(proof, "quota_reset_at"),
            response_digest=proof["response_digest"],
            evidence_digest=proof["evidence_digest"],
            request_shape_digest=proof["request_shape_digest"],
            quota_used_datapoints=proof["quota_used_datapoints"],
            quota_limit_datapoints=proof["quota_limit_datapoints"],
            quota_period=proof["quota_period"],
            quota_tier=str(
                proof["raw_header_evidence"].get("x-tier", "")
            ).strip(),
        )

    def validate(
        self,
        *,
        now: datetime,
        maximum_age_seconds: int = DISCOVERY_QUOTA_PROOF_MAX_AGE_SECONDS,
    ) -> None:
        for name, value in (
            ("proof_id", self.proof_id),
            ("authorization_id", self.authorization_id),
            ("account_scope", self.account_scope),
        ):
            _text(value, name)
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryExecutionBlocked("B4 quota proof provider is invalid")
        if self.sport_id != B4_QUOTA_PROOF_SPORT_ID:
            raise EventDiscoveryExecutionBlocked("B4 quota proof sport is invalid")
        if not isinstance(self.snapshot_date, date) or isinstance(
            self.snapshot_date, datetime
        ):
            raise EventDiscoveryContractError("quota proof snapshot_date is invalid")
        if (
            not isinstance(self.remaining_datapoints, int)
            or isinstance(self.remaining_datapoints, bool)
            or self.remaining_datapoints < DISCOVERY_MINIMUM_COMBINED_HEADROOM
        ):
            raise EventDiscoveryExecutionBlocked(
                "TOP5_PROVIDER_EVENT_ID_DISCOVERY — INSUFFICIENT_COMBINED_HEADROOM"
            )
        _sha(self.response_digest, "quota proof response_digest")
        _sha(self.evidence_digest, "quota proof evidence_digest")
        started = _utc_datetime(
            self.response_started_at, "quota proof request_started_at"
        )
        finished = _utc_datetime(
            self.response_finished_at, "quota proof response_finished_at"
        )
        reset = _utc_datetime(self.quota_reset_at, "quota reset_at")
        current = _utc_datetime(now, "quota proof validation now")
        if (
            self.snapshot_date < current.date()
            or self.snapshot_date
            > current.date() + timedelta(days=B4_QUOTA_PROOF_SNAPSHOT_MAX_OFFSET_DAYS)
        ):
            raise EventDiscoveryExecutionBlocked(
                "quota proof snapshot date is historical or outside the bounded window"
            )
        if started > finished:
            raise EventDiscoveryContractError("quota proof timestamps are not ordered")
        if finished > current:
            raise EventDiscoveryExecutionBlocked("quota proof is from the future")
        if (current - finished).total_seconds() > maximum_age_seconds:
            raise EventDiscoveryExecutionBlocked("quota proof is stale")
        if reset <= current or reset <= finished:
            raise EventDiscoveryExecutionBlocked(
                "quota proof rate-limit reset period is no longer valid"
            )

    @classmethod
    def load_canonical(cls, *, now: datetime) -> TheRundownB4QuotaProofV1:
        path = b4_quota_proof_state_path()
        if not path.is_file() or path.is_symlink():
            raise EventDiscoveryExecutionBlocked(
                "canonical B4 quota proof is unavailable"
            )
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise EventDiscoveryExecutionBlocked(
                "canonical B4 quota proof is invalid"
            ) from exc
        return cls.from_package(raw, now=now)


def _team_key(value: object) -> str:
    import unicodedata

    raw = str(value or "").strip()
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", raw).casefold()
        if not unicodedata.combining(char) and char.isalnum()
    )


def _status_values(record: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("status", "state", "event_status", "phase"):
        value = record.get(key)
        if value is not None and str(value).strip():
            values.append(_team_key(value))
    score = record.get("score")
    if isinstance(score, Mapping):
        for key in ("status", "state", "event_status", "phase"):
            value = score.get(key)
            if value is not None and str(value).strip():
                values.append(_team_key(value))
    return tuple(values)


def _is_true(value: object) -> bool:
    return value is True or (
        isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"}
    )


def _prematch(event: Mapping[str, object]) -> bool:
    statuses = _status_values(event)
    if not statuses or any(status not in _PREMATCH_STATUSES for status in statuses):
        return False
    return not any(_is_true(event.get(key)) for key in _STATE_REJECT_FLAGS)


def _provider_shape(target: TheRundownEventDiscoveryTargetV1) -> dict[str, object]:
    sport_id = THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[target.league]
    return {
        "method": "GET",
        "endpoint": f"{THERUNDOWN_BASE_URL}/sports/{sport_id}/events/{target.kickoff.date().isoformat()}",
        "params": {
            "market_ids": str(THERUNDOWN_MONEYLINE_MARKET_ID),
            "main_line": "true",
            "hide_closed": "true",
            "hide_no_markets": "true",
        },
        "league": target.league,
        "fixture_key": target.fixture_key,
        "request_identity": target.request_identity,
    }


@dataclass(frozen=True)
class TheRundownEventDiscoveryTargetV1:
    """Stable canonical fixture scope before provider identity is discovered.

    Provider participant IDs are intentionally optional here.  They are
    response-side identity, and are bound only after a real provider response
    has matched this canonical league/team/kickoff scope.
    """

    provider: str
    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime
    request_identity: str
    home_participant_id: str | None = None
    away_participant_id: str | None = None

    def validate(self) -> None:
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError("discovery provider identity is invalid")
        if self.league not in TOP5_LEAGUE_CODES:
            raise EventDiscoveryContractError("discovery league is outside Top-5")
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("request_identity", self.request_identity),
        ):
            _text(value, name)
        participant_ids = (self.home_participant_id, self.away_participant_id)
        if any(value is not None for value in participant_ids):
            if any(
                not isinstance(value, str) or not value.strip()
                for value in participant_ids
            ):
                raise EventDiscoveryContractError(
                    "provider participant IDs must be supplied together"
                )
            if self.home_participant_id == self.away_participant_id:
                raise EventDiscoveryContractError("participant IDs must be distinct")
        kickoff = _utc_datetime(self.kickoff, "kickoff")
        try:
            expected = make_fixture_key(
                self.league, self.home_team, self.away_team, kickoff
            )
        except Exception as exc:
            raise EventDiscoveryContractError("fixture scope is malformed") from exc
        if self.fixture_key != expected:
            raise EventDiscoveryContractError(
                "fixture key does not match fixture scope"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = {
            "provider": self.provider,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _utc_datetime(self.kickoff, "kickoff").isoformat(),
            "request_identity": self.request_identity,
        }
        if self.home_participant_id is not None:
            payload["home_participant_id"] = self.home_participant_id
            payload["away_participant_id"] = self.away_participant_id
        return payload


def _manifest_source_is_noncanonical(source_provenance: str) -> bool:
    lowered = source_provenance.casefold()
    return any(
        marker in lowered
        for marker in (
            "synthetic",
            "test_fixture",
            "test-fixture",
            "offline_replay",
            "offline-replay",
            "mock://",
            "fixture://",
        )
    )


@dataclass(frozen=True)
class Top5CurrentDiscoveryTargetManifestV1:
    """Current provider-independent five-league discovery scope.

    This manifest deliberately stops at canonical SportsBrain fixture
    identity.  TheRundown event and participant IDs are not known, guessed,
    or serialized until response-side discovery produces them.
    """

    generated_at: datetime
    observed_at: datetime
    source_provenance: str
    source_release_sha: str
    targets: tuple[TheRundownEventDiscoveryTargetV1, ...]
    runtime_data_sha: str | None = None
    manifest_digest: str = ""

    @property
    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": CURRENT_TARGET_MANIFEST_SCHEMA_VERSION,
            "generated_at": _utc_datetime(
                self.generated_at, "generated_at"
            ).isoformat(),
            "observed_at": _utc_datetime(self.observed_at, "observed_at").isoformat(),
            "source_provenance": self.source_provenance,
            "source_release_sha": self.source_release_sha,
            "runtime_data_sha": self.runtime_data_sha,
            "targets": [target.as_payload() for target in self.targets],
        }

    @property
    def computed_manifest_digest(self) -> str:
        return _digest(self._payload_without_digest)

    def validate(self, *, now: datetime | None = None) -> None:
        current = _utc_datetime(
            now or datetime.now(timezone.utc), "target manifest validation now"
        )
        generated = _utc_datetime(self.generated_at, "generated_at")
        observed = _utc_datetime(self.observed_at, "observed_at")
        if generated < observed:
            raise EventDiscoveryContractError(
                "target manifest generated_at precedes observed_at"
            )
        if generated > current or observed > current:
            raise EventDiscoveryExecutionBlocked(
                "target manifest timestamp is future-dated"
            )
        if (
            current - observed
        ).total_seconds() > CURRENT_TARGET_MANIFEST_MAX_AGE_SECONDS:
            raise EventDiscoveryExecutionBlocked("target manifest source is stale")
        source = _text(self.source_provenance, "source_provenance")
        if _manifest_source_is_noncanonical(source):
            raise EventDiscoveryExecutionBlocked(
                "synthetic or offline source cannot establish current targets"
            )
        _sha(self.source_release_sha, "source_release_sha")
        if self.runtime_data_sha is not None:
            _sha(self.runtime_data_sha, "runtime_data_sha")
        if tuple(target.league for target in self.targets) != DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError(
                "target manifest league order is not canonical"
            )
        for target in self.targets:
            target.validate()
            if (
                target.home_participant_id is not None
                or target.away_participant_id is not None
            ):
                raise EventDiscoveryExecutionBlocked(
                    "target manifest must not contain provider participant IDs"
                )
        if len({target.fixture_key for target in self.targets}) != len(self.targets):
            raise EventDiscoveryContractError(
                "target manifest fixture IDs are duplicated"
            )
        if len({target.request_identity for target in self.targets}) != len(
            self.targets
        ):
            raise EventDiscoveryContractError(
                "target manifest request identities are duplicated"
            )
        _sha(self.manifest_digest, "manifest_digest")
        if self.manifest_digest.lower() != self.computed_manifest_digest:
            raise EventDiscoveryContractError("target manifest digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.generated_at)
        return {
            **self._payload_without_digest,
            "manifest_digest": self.manifest_digest.lower(),
        }

    @classmethod
    def from_payload(
        cls, payload: object, *, now: datetime | None = None
    ) -> Top5CurrentDiscoveryTargetManifestV1:
        if not isinstance(payload, Mapping):
            raise EventDiscoveryContractError("target manifest must be an object")
        expected = {
            "schema_version",
            "generated_at",
            "observed_at",
            "source_provenance",
            "source_release_sha",
            "runtime_data_sha",
            "targets",
            "manifest_digest",
        }
        if set(payload) != expected:
            raise EventDiscoveryContractError("target manifest shape is invalid")
        if payload.get("schema_version") != CURRENT_TARGET_MANIFEST_SCHEMA_VERSION:
            raise EventDiscoveryContractError("target manifest schema is invalid")
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, list):
            raise EventDiscoveryContractError("target manifest targets are invalid")
        targets: list[TheRundownEventDiscoveryTargetV1] = []
        for raw in raw_targets:
            if not isinstance(raw, Mapping):
                raise EventDiscoveryContractError("target manifest target is invalid")
            target_keys = {
                "provider",
                "league",
                "fixture_key",
                "home_team",
                "away_team",
                "kickoff",
                "request_identity",
                "home_participant_id",
                "away_participant_id",
            }
            if set(raw) not in (
                target_keys - {"home_participant_id", "away_participant_id"},
                target_keys,
            ):
                raise EventDiscoveryContractError(
                    "target manifest target shape is invalid"
                )
            targets.append(
                TheRundownEventDiscoveryTargetV1(
                    provider=raw.get("provider", ""),
                    league=raw.get("league", ""),
                    fixture_key=raw.get("fixture_key", ""),
                    home_team=raw.get("home_team", ""),
                    away_team=raw.get("away_team", ""),
                    kickoff=_utc_datetime(raw.get("kickoff"), "target kickoff"),
                    request_identity=raw.get("request_identity", ""),
                    home_participant_id=raw.get("home_participant_id"),
                    away_participant_id=raw.get("away_participant_id"),
                )
            )
        manifest = cls(
            generated_at=_utc_datetime(payload.get("generated_at"), "generated_at"),
            observed_at=_utc_datetime(payload.get("observed_at"), "observed_at"),
            source_provenance=payload.get("source_provenance", ""),
            source_release_sha=payload.get("source_release_sha", ""),
            runtime_data_sha=payload.get("runtime_data_sha"),
            targets=tuple(targets),
            manifest_digest=payload.get("manifest_digest", ""),
        )
        manifest.validate(now=now)
        return manifest


def select_current_top5_discovery_targets(
    fixtures: Sequence[Fixture],
    *,
    now: datetime,
    observed_at: datetime,
    source_provenance: str,
    source_release_sha: str,
    runtime_data_sha: str | None = None,
    minimum_lead_seconds: int = 3600,
    maximum_window_seconds: int = 7 * 24 * 3600,
) -> Top5CurrentDiscoveryTargetManifestV1:
    """Select one deterministic, upcoming canonical fixture per Top-5 league.

    The caller must provide a genuinely current canonical fixture source.  No
    provider lookup, alias, synthetic fixture, or guessed kickoff is created
    by this helper.
    """

    current = _utc_datetime(now, "selection now")
    observed = _utc_datetime(observed_at, "selection observed_at")
    if observed > current:
        raise EventDiscoveryExecutionBlocked("fixture source is future-dated")
    if (current - observed).total_seconds() > CURRENT_TARGET_MANIFEST_MAX_AGE_SECONDS:
        raise EventDiscoveryExecutionBlocked("fixture source is stale")
    if (
        isinstance(minimum_lead_seconds, bool)
        or minimum_lead_seconds <= 0
        or isinstance(maximum_window_seconds, bool)
        or maximum_window_seconds <= minimum_lead_seconds
    ):
        raise EventDiscoveryContractError("fixture selection window is invalid")
    source = _text(source_provenance, "source_provenance")
    if _manifest_source_is_noncanonical(source):
        raise EventDiscoveryExecutionBlocked(
            "synthetic or offline source cannot establish current targets"
        )
    _sha(source_release_sha, "source_release_sha")
    if runtime_data_sha is not None:
        _sha(runtime_data_sha, "runtime_data_sha")
    candidates: dict[str, list[Fixture]] = {
        league: [] for league in DISCOVERY_LEAGUE_ORDER
    }
    for fixture in fixtures:
        if not isinstance(fixture, Fixture):
            raise EventDiscoveryContractError(
                "fixture source contains an invalid record"
            )
        fixture.validate()
        if fixture.league_code not in candidates:
            continue
        expected_key = make_fixture_key(
            fixture.league_code,
            fixture.home_team,
            fixture.away_team,
            fixture.kickoff,
        )
        if fixture.fixture_key != expected_key:
            raise EventDiscoveryExecutionBlocked(
                "fixture source identity is non-canonical"
            )
        lead = (fixture.kickoff - current).total_seconds()
        if minimum_lead_seconds <= lead <= maximum_window_seconds:
            candidates[fixture.league_code].append(fixture)
    targets: list[TheRundownEventDiscoveryTargetV1] = []
    for league in DISCOVERY_LEAGUE_ORDER:
        if not candidates[league]:
            raise EventDiscoveryExecutionBlocked(
                f"current fixture source has no eligible {league} target"
            )
        fixture = min(
            candidates[league], key=lambda item: (item.kickoff, item.fixture_key)
        )
        request_identity = _digest(
            {
                "phase": "top5_event_discovery",
                "provider": THERUNDOWN_PROVIDER_NAME,
                "league": league,
                "fixture_key": fixture.fixture_key,
                "kickoff": fixture.kickoff,
            }
        )
        targets.append(
            TheRundownEventDiscoveryTargetV1(
                provider=THERUNDOWN_PROVIDER_NAME,
                league=league,
                fixture_key=fixture.fixture_key,
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                kickoff=fixture.kickoff,
                request_identity=request_identity,
            )
        )
    draft = Top5CurrentDiscoveryTargetManifestV1(
        generated_at=current,
        observed_at=observed,
        source_provenance=source,
        source_release_sha=source_release_sha.lower(),
        runtime_data_sha=runtime_data_sha.lower() if runtime_data_sha else None,
        targets=tuple(targets),
        manifest_digest="0" * 64,
    )
    manifest = replace(draft, manifest_digest=draft.computed_manifest_digest)
    manifest.validate(now=current)
    return manifest


def load_current_top5_discovery_target_manifest(
    path: str | Path, *, now: datetime
) -> Top5CurrentDiscoveryTargetManifestV1:
    """Load and fully validate one operator-produced current target manifest."""

    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise EventDiscoveryExecutionBlocked(
            "current discovery target manifest is unavailable"
        )
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventDiscoveryContractError(
            "current discovery target manifest is unreadable"
        ) from exc
    return Top5CurrentDiscoveryTargetManifestV1.from_payload(payload, now=now)


@dataclass(frozen=True)
class TheRundownEventDiscoveryAuthorizationV1:
    discovery_authorization_id: str
    ceo_discovery_authorization_identity: str
    provider: str
    targets: tuple[TheRundownEventDiscoveryTargetV1, ...]
    adapter_version: str
    adapter_source_sha: str
    request_shape_digest: str
    quota_proof_id: str
    quota_proof_authorization_id: str
    quota_proof_evidence_digest: str
    quota_proof_response_digest: str
    quota_proof_account_scope: str
    quota_proof_remaining_datapoints: int
    quota_proof_sport_id: int
    quota_proof_snapshot_date: date
    quota_proof_observed_at: datetime
    quota_proof_finished_at: datetime
    quota_proof_reset_at: datetime
    issued_at: datetime
    expires_at: datetime
    maximum_request_count: int = TOP5_CONTROLLED_SHADOW_REQUEST_COUNT
    maximum_datapoints: int = TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET
    maximum_datapoints_per_request: int = THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
    minimum_interval_seconds: float = TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS
    maximum_retries: int = 0
    no_bet: bool = True
    candidate_qualification: bool = False
    production_authority: bool = False
    activation: bool = False
    publication: bool = False
    ledger_mutation: bool = False
    monetary_spend_authorized: bool = False

    @property
    def authorization_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": DISCOVERY_AUTHORIZATION_SCHEMA_VERSION,
            "discovery_authorization_id": self.discovery_authorization_id,
            "ceo_discovery_authorization_identity": self.ceo_discovery_authorization_identity,
            "provider": self.provider,
            "targets": [target.as_payload() for target in self.targets],
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "request_shape_digest": self.request_shape_digest,
            "quota_proof_id": self.quota_proof_id,
            "quota_proof_authorization_id": self.quota_proof_authorization_id,
            "quota_proof_evidence_digest": self.quota_proof_evidence_digest,
            "quota_proof_response_digest": self.quota_proof_response_digest,
            "quota_proof_account_scope": self.quota_proof_account_scope,
            "quota_proof_remaining_datapoints": self.quota_proof_remaining_datapoints,
            "quota_proof_sport_id": self.quota_proof_sport_id,
            "quota_proof_snapshot_date": self.quota_proof_snapshot_date.isoformat(),
            "quota_proof_observed_at": _utc_datetime(
                self.quota_proof_observed_at, "quota_proof_observed_at"
            ).isoformat(),
            "quota_proof_finished_at": _utc_datetime(
                self.quota_proof_finished_at, "quota_proof_finished_at"
            ).isoformat(),
            "quota_proof_reset_at": _utc_datetime(
                self.quota_proof_reset_at, "quota_proof_reset_at"
            ).isoformat(),
            "issued_at": _utc_datetime(self.issued_at, "issued_at").isoformat(),
            "expires_at": _utc_datetime(self.expires_at, "expires_at").isoformat(),
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoints": self.maximum_datapoints,
            "maximum_datapoints_per_request": self.maximum_datapoints_per_request,
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "maximum_retries": self.maximum_retries,
            "no_bet": self.no_bet,
            "candidate_qualification": self.candidate_qualification,
            "production_authority": self.production_authority,
            "activation": self.activation,
            "publication": self.publication,
            "ledger_mutation": self.ledger_mutation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    def validate(self, *, now: datetime | None = None) -> None:
        _text(self.discovery_authorization_id, "discovery_authorization_id")
        _text(
            self.ceo_discovery_authorization_identity,
            "ceo_discovery_authorization_identity",
        )
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError(
                "discovery provider is not candidate-only"
            )
        if len(self.targets) != len(DISCOVERY_LEAGUE_ORDER):
            raise EventDiscoveryContractError("discovery requires exactly five targets")
        if tuple(target.league for target in self.targets) != DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError("discovery league order is not canonical")
        for target in self.targets:
            target.validate()
        if len({target.fixture_key for target in self.targets}) != len(self.targets):
            raise EventDiscoveryContractError(
                "discovery fixture identities must be unique"
            )
        if len({target.request_identity for target in self.targets}) != len(
            self.targets
        ):
            raise EventDiscoveryContractError(
                "discovery request identities must be unique"
            )
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _sha(self.request_shape_digest, "request_shape_digest")
        for name, value in (
            ("quota_proof_id", self.quota_proof_id),
            ("quota_proof_authorization_id", self.quota_proof_authorization_id),
            ("quota_proof_account_scope", self.quota_proof_account_scope),
        ):
            _text(value, name)
        _sha(self.quota_proof_evidence_digest, "quota_proof_evidence_digest")
        _sha(self.quota_proof_response_digest, "quota_proof_response_digest")
        if (
            not isinstance(self.quota_proof_remaining_datapoints, int)
            or isinstance(self.quota_proof_remaining_datapoints, bool)
            or self.quota_proof_remaining_datapoints
            < DISCOVERY_MINIMUM_COMBINED_HEADROOM
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery quota-proof binding is below 550 datapoints"
            )
        if self.quota_proof_sport_id != B4_QUOTA_PROOF_SPORT_ID:
            raise EventDiscoveryExecutionBlocked(
                "discovery quota-proof sport binding is invalid"
            )
        if not isinstance(self.quota_proof_snapshot_date, date) or isinstance(
            self.quota_proof_snapshot_date, datetime
        ):
            raise EventDiscoveryContractError(
                "discovery quota-proof snapshot date is invalid"
            )
        _utc_datetime(self.quota_proof_observed_at, "quota_proof_observed_at")
        _utc_datetime(self.quota_proof_finished_at, "quota_proof_finished_at")
        _utc_datetime(self.quota_proof_reset_at, "quota_proof_reset_at")
        expected_shape_digest = _digest(
            [_provider_shape(target) for target in self.targets]
        )
        if expected_shape_digest != self.request_shape_digest.lower():
            raise EventDiscoveryContractError("request-shape digest mismatch")
        if self.maximum_request_count != TOP5_CONTROLLED_SHADOW_REQUEST_COUNT:
            raise EventDiscoveryExecutionBlocked(
                "discovery request budget must be five"
            )
        if self.maximum_datapoints != TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET:
            raise EventDiscoveryExecutionBlocked(
                "discovery datapoint budget must be 275"
            )
        if (
            self.maximum_datapoints_per_request
            != THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery request datapoint cap must be 55"
            )
        if self.maximum_retries != 0:
            raise EventDiscoveryExecutionBlocked("discovery retries are forbidden")
        if (
            isinstance(self.minimum_interval_seconds, bool)
            or not isinstance(self.minimum_interval_seconds, (int, float))
            or not isfinite(float(self.minimum_interval_seconds))
            or self.minimum_interval_seconds
            < TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery pacing must be at least 1.1 seconds"
            )
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("candidate_qualification", self.candidate_qualification, False),
            ("production_authority", self.production_authority, False),
            ("activation", self.activation, False),
            ("publication", self.publication, False),
            ("ledger_mutation", self.ledger_mutation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise EventDiscoveryExecutionBlocked(f"unsafe discovery flag: {name}")
        issued = _utc_datetime(self.issued_at, "issued_at")
        expires = _utc_datetime(self.expires_at, "expires_at")
        if expires <= issued:
            raise EventDiscoveryContractError("discovery expiry must follow issue time")
        current = _utc_datetime(now or datetime.now(timezone.utc), "discovery now")
        if current < issued or current >= expires:
            raise EventDiscoveryExecutionBlocked("discovery authorization is expired")

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        return {
            **self._payload_without_digest(),
            "authorization_digest": self.authorization_digest,
        }

    def validate_against_quota_proof(
        self, proof: TheRundownB4QuotaProofV1, *, now: datetime
    ) -> None:
        self.validate(now=now)
        proof.validate(now=now)
        for name, actual, expected in (
            ("quota_proof_id", self.quota_proof_id, proof.proof_id),
            (
                "quota_proof_authorization_id",
                self.quota_proof_authorization_id,
                proof.authorization_id,
            ),
            (
                "quota_proof_evidence_digest",
                self.quota_proof_evidence_digest.lower(),
                proof.evidence_digest.lower(),
            ),
            (
                "quota_proof_response_digest",
                self.quota_proof_response_digest.lower(),
                proof.response_digest.lower(),
            ),
            (
                "quota_proof_account_scope",
                self.quota_proof_account_scope,
                proof.account_scope,
            ),
            (
                "quota_proof_remaining_datapoints",
                self.quota_proof_remaining_datapoints,
                proof.remaining_datapoints,
            ),
            (
                "quota_proof_sport_id",
                self.quota_proof_sport_id,
                proof.sport_id,
            ),
            (
                "quota_proof_snapshot_date",
                self.quota_proof_snapshot_date,
                proof.snapshot_date,
            ),
            (
                "quota_proof_observed_at",
                _utc_datetime(self.quota_proof_observed_at, "quota_proof_observed_at"),
                _utc_datetime(proof.response_started_at, "proof request_started_at"),
            ),
            (
                "quota_proof_finished_at",
                _utc_datetime(self.quota_proof_finished_at, "quota_proof_finished_at"),
                _utc_datetime(proof.response_finished_at, "proof response_finished_at"),
            ),
            (
                "quota_proof_reset_at",
                _utc_datetime(self.quota_proof_reset_at, "quota_proof_reset_at"),
                _utc_datetime(proof.quota_reset_at, "proof reset_at"),
            ),
        ):
            if actual != expected:
                raise EventDiscoveryExecutionBlocked(
                    f"discovery quota-proof binding mismatch: {name}"
                )

    def request_shape(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        payload = [_provider_shape(target) for target in self.targets]
        return {"requests": payload}


class TheRundownDiscoveryAuthorizationConsumptionStore:
    """Canonical operator-owned durable one-shot discovery consumption state."""

    _SCHEMA = DISCOVERY_CONSUMPTION_SCHEMA_VERSION

    def __init__(self) -> None:
        self.state_path = discovery_authorization_consumption_state_path()
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")

    @contextmanager
    def _locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_path.parent, 0o700)
        if self.lock_path.is_symlink():
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption lock is a symlink"
            )
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validate_record(record: object) -> None:
        if not isinstance(record, Mapping):
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption record is invalid"
            )
        expected = {
            "discovery_authorization_id",
            "authorization_digest",
            "quota_proof_evidence_digest",
            "consumed_at",
        }
        if set(record) != expected:
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption record shape is invalid"
            )
        _text(record.get("discovery_authorization_id"), "consumption authorization id")
        _sha(record.get("authorization_digest"), "consumption authorization digest")
        _sha(
            record.get("quota_proof_evidence_digest"),
            "consumption quota-proof evidence digest",
        )
        _utc_datetime(record.get("consumed_at"), "consumption timestamp")

    def _read_state(self) -> list[dict[str, object]]:
        if not self.state_path.exists():
            return []
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption state is unavailable"
            )
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption state is invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption state must be an object"
            )
        expected = {"schema", "records", "state_digest"}
        if set(value) != expected or value.get("schema") != self._SCHEMA:
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption state shape is invalid"
            )
        records = value.get("records")
        if not isinstance(records, list):
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption records are invalid"
            )
        for record in records:
            self._validate_record(record)
        if len(
            {str(record.get("discovery_authorization_id")) for record in records}
        ) != len(records):
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption state has duplicate authorization identities"
            )
        state_digest = value.get("state_digest")
        _sha(state_digest, "discovery consumption state digest")
        if state_digest.lower() != self._state_digest(records):
            raise EventDiscoveryExecutionBlocked(
                "discovery consumption state digest mismatch"
            )
        return [dict(record) for record in records]

    def _state_digest(self, records: Sequence[Mapping[str, object]]) -> str:
        return _digest({"schema": self._SCHEMA, "records": list(records)})

    def _write_state(self, records: Sequence[Mapping[str, object]]) -> None:
        payload = {
            "schema": self._SCHEMA,
            "records": [dict(record) for record in records],
            "state_digest": self._state_digest(records),
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", dir=self.state_path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def consume(
        self,
        authorization: TheRundownEventDiscoveryAuthorizationV1,
        *,
        now: datetime,
    ) -> None:
        authorization_id = _text(
            authorization.discovery_authorization_id,
            "discovery authorization id",
        )
        authorization_digest = _sha(
            authorization.authorization_digest,
            "discovery authorization digest",
        )
        quota_digest = _sha(
            authorization.quota_proof_evidence_digest,
            "discovery quota-proof evidence digest",
        )
        with self._locked():
            records = self._read_state()
            for record in records:
                if record["discovery_authorization_id"] == authorization_id:
                    if (
                        record["authorization_digest"] == authorization_digest
                        and record["quota_proof_evidence_digest"] == quota_digest
                    ):
                        raise EventDiscoveryExecutionBlocked(
                            "discovery authorization has already been consumed"
                        )
                    raise EventDiscoveryExecutionBlocked(
                        "discovery authorization identity was modified after consumption"
                    )
            records.append(
                {
                    "discovery_authorization_id": authorization_id,
                    "authorization_digest": authorization_digest,
                    "quota_proof_evidence_digest": quota_digest,
                    "consumed_at": _utc_datetime(now, "consumption now").isoformat(),
                }
            )
            self._write_state(records)


@dataclass(frozen=True)
class TheRundownEventDiscoveryRequestV1:
    authorization: TheRundownEventDiscoveryAuthorizationV1
    target: TheRundownEventDiscoveryTargetV1
    sequence: int

    def validate(self) -> None:
        self.authorization.validate(now=self.authorization.issued_at)
        self.target.validate()
        if self.target.league not in DISCOVERY_LEAGUE_ORDER:
            raise EventDiscoveryContractError("request league is invalid")
        if self.sequence != DISCOVERY_LEAGUE_ORDER.index(self.target.league):
            raise EventDiscoveryExecutionBlocked("discovery request order is invalid")


@dataclass(frozen=True)
class TheRundownEventDiscoveryResponseV1:
    status_code: int
    payload: object
    headers: Mapping[str, str]
    started_at: datetime
    finished_at: datetime
    retry_count: int = 0
    network_execution: bool = False

    def validate(self) -> None:
        if self.status_code != 200:
            raise EventDiscoveryExecutionBlocked("discovery response is not HTTP 200")
        if self.retry_count != 0:
            raise EventDiscoveryExecutionBlocked("discovery response reports a retry")
        started = _utc_datetime(self.started_at, "response started_at")
        finished = _utc_datetime(self.finished_at, "response finished_at")
        if finished < started:
            raise EventDiscoveryContractError("response timestamps are not ordered")
        if not isinstance(self.payload, Mapping):
            raise EventDiscoveryExecutionBlocked("discovery payload is not an object")
        if not isinstance(self.payload.get("events"), list):
            raise EventDiscoveryExecutionBlocked("discovery payload has no events list")
        if not isinstance(self.network_execution, bool):
            raise EventDiscoveryContractError("network_execution must be boolean")


class TheRundownEventDiscoveryTransport(Protocol):
    def execute(
        self, request: TheRundownEventDiscoveryRequestV1
    ) -> TheRundownEventDiscoveryResponseV1: ...


@dataclass(frozen=True)
class TheRundownEventDiscoveryEvidenceV1:
    discovery_authorization_id: str
    discovery_authorization_digest: str
    provider: str
    league: str
    fixture_key: str
    home_team: str
    away_team: str
    home_participant_id: str
    away_participant_id: str
    kickoff: datetime
    provider_event_id: str
    request_identity: str
    request_shape_digest: str
    request_started_at: datetime
    response_completed_at: datetime
    raw_response_digest: str
    provider_event_evidence_digest: str
    datapoints: int
    remaining_datapoints: int
    retry_count: int = 0
    network_execution: bool = False
    qualification_eligible: bool = False
    receipt_eligible: bool = False
    provider_authority: bool = False
    activation_authorized: bool = False
    publication_authorized: bool = False
    ledger_mutated: bool = False
    monetary_spend_authorized: bool = False

    @property
    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": DISCOVERY_EVIDENCE_SCHEMA_VERSION,
            "discovery_authorization_id": self.discovery_authorization_id,
            "discovery_authorization_digest": self.discovery_authorization_digest,
            "provider": self.provider,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
            "kickoff": _utc_datetime(self.kickoff, "kickoff").isoformat(),
            "provider_event_id": self.provider_event_id,
            "request_identity": self.request_identity,
            "request_shape_digest": self.request_shape_digest,
            "request_started_at": _utc_datetime(
                self.request_started_at, "request_started_at"
            ).isoformat(),
            "response_completed_at": _utc_datetime(
                self.response_completed_at, "response_completed_at"
            ).isoformat(),
            "raw_response_digest": self.raw_response_digest,
            "datapoints": self.datapoints,
            "remaining_datapoints": self.remaining_datapoints,
            "retry_count": self.retry_count,
            "network_execution": self.network_execution,
            "qualification_eligible": self.qualification_eligible,
            "receipt_eligible": self.receipt_eligible,
            "provider_authority": self.provider_authority,
            "activation_authorized": self.activation_authorized,
            "publication_authorized": self.publication_authorized,
            "ledger_mutated": self.ledger_mutated,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    @property
    def computed_provider_event_evidence_digest(self) -> str:
        return _digest(self._payload_without_digest)

    def validate(self) -> None:
        _text(self.discovery_authorization_id, "discovery_authorization_id")
        _sha(self.discovery_authorization_digest, "discovery_authorization_digest")
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise EventDiscoveryContractError("evidence provider is invalid")
        if self.league not in TOP5_LEAGUE_CODES:
            raise EventDiscoveryContractError("evidence league is invalid")
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("home_participant_id", self.home_participant_id),
            ("away_participant_id", self.away_participant_id),
            ("provider_event_id", self.provider_event_id),
            ("request_identity", self.request_identity),
            ("raw_response_digest", self.raw_response_digest),
        ):
            _text(value, name)
        _sha(self.request_shape_digest, "request_shape_digest")
        _sha(self.raw_response_digest, "raw_response_digest")
        _sha(self.provider_event_evidence_digest, "provider_event_evidence_digest")
        kickoff = _utc_datetime(self.kickoff, "kickoff")
        try:
            expected_fixture_key = make_fixture_key(
                self.league, self.home_team, self.away_team, kickoff
            )
        except Exception as exc:
            raise EventDiscoveryContractError(
                "evidence fixture scope is malformed"
            ) from exc
        if self.fixture_key != expected_fixture_key:
            raise EventDiscoveryContractError("evidence fixture key mismatch")
        started = _utc_datetime(self.request_started_at, "request_started_at")
        completed = _utc_datetime(self.response_completed_at, "response_completed_at")
        if completed < started:
            raise EventDiscoveryContractError("evidence timestamps are not ordered")
        if (
            self.retry_count != 0
            or self.datapoints < 0
            or self.remaining_datapoints < 0
        ):
            raise EventDiscoveryExecutionBlocked(
                "discovery billing evidence is invalid"
            )
        if self.datapoints > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
            raise EventDiscoveryExecutionBlocked(
                "discovery request datapoint cap exceeded"
            )
        if self.network_execution is not True and self.qualification_eligible:
            raise EventDiscoveryExecutionBlocked("offline discovery cannot qualify")
        for name, value in (
            ("qualification_eligible", self.qualification_eligible),
            ("receipt_eligible", self.receipt_eligible),
            ("provider_authority", self.provider_authority),
            ("activation_authorized", self.activation_authorized),
            ("publication_authorized", self.publication_authorized),
            ("ledger_mutated", self.ledger_mutated),
            ("monetary_spend_authorized", self.monetary_spend_authorized),
        ):
            if value is not False:
                raise EventDiscoveryExecutionBlocked(
                    f"discovery evidence is authorizing: {name}"
                )
        if self.home_participant_id == self.away_participant_id:
            raise EventDiscoveryContractError(
                "evidence participant IDs must be distinct"
            )
        if (
            self.computed_provider_event_evidence_digest
            != self.provider_event_evidence_digest
        ):
            raise EventDiscoveryContractError("provider event evidence digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        payload = dict(self._payload_without_digest)
        payload["provider_event_evidence_digest"] = self.provider_event_evidence_digest
        return payload


def discovery_request_shape_digest(
    targets: Sequence[TheRundownEventDiscoveryTargetV1],
) -> str:
    if tuple(target.league for target in targets) != DISCOVERY_LEAGUE_ORDER:
        raise EventDiscoveryContractError(
            "request shape requires canonical five-league order"
        )
    for target in targets:
        target.validate()
    return _digest([_provider_shape(target) for target in targets])


def _billing(response: TheRundownEventDiscoveryResponseV1) -> tuple[int, int]:
    lowered = {
        str(key).casefold(): str(value).strip()
        for key, value in response.headers.items()
    }

    def integer(name: str) -> int:
        try:
            value = int(lowered[name])
        except (KeyError, ValueError) as exc:
            raise EventDiscoveryExecutionBlocked(
                f"missing or invalid billing header: {name}"
            ) from exc
        if value < 0:
            raise EventDiscoveryExecutionBlocked(f"negative billing header: {name}")
        return value

    datapoints = integer("x-datapoints")
    used = integer("x-datapoints-used")
    remaining = integer("x-datapoints-remaining")
    limit = integer("x-datapoints-limit")
    if used + remaining != limit or datapoints > used:
        raise EventDiscoveryExecutionBlocked(
            "discovery quota counters do not reconcile"
        )
    if datapoints > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
        raise EventDiscoveryExecutionBlocked(
            "discovery request datapoint cap exceeded",
            diagnostic={
                "diagnostic_kind": "rejected_observed_x_datapoints",
                "evidence_status": "rejected_observed",
                "header": "x-datapoints",
                "raw_provider_value": lowered["x-datapoints"],
                "authorized_cap": THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
                "accepted_as_billing": False,
            },
        )
    return datapoints, remaining


def _validated_datapoint_total(current: int, datapoints: int) -> int:
    """Apply both per-request and cumulative discovery billing caps."""

    if datapoints < 0:
        raise EventDiscoveryExecutionBlocked("discovery datapoints cannot be negative")
    if datapoints > THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
        raise EventDiscoveryExecutionBlocked("discovery request datapoint cap exceeded")
    total = current + datapoints
    if total > TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET:
        raise EventDiscoveryExecutionBlocked(
            "discovery cumulative datapoint budget exceeded"
        )
    return total


def _resolve_event_identity(
    target: TheRundownEventDiscoveryTargetV1,
    payload: Mapping[str, object],
    *,
    response_at: datetime,
) -> tuple[str, str, str]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise EventDiscoveryExecutionBlocked("discovery payload has no events list")
    fixture = Fixture(
        target.fixture_key,
        target.league,
        target.home_team,
        target.away_team,
        target.kickoff,
    )
    adapter = TheRundownExperimentalAdapter()
    timing = CascadeTimingPolicy(
        maximum_odds_age_seconds=300,
        kickoff_tolerance_seconds=DISCOVERY_KICKOFF_TOLERANCE_SECONDS,
    )
    event_ids: list[str] = []
    matches: list[Mapping[str, object]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_id = str(event.get("event_id", "")).strip()
        if event_id:
            event_ids.append(event_id)
        if adapter.event_identity(event, fixture, timing_policy=timing) == "match":
            matches.append(event)
    if len(event_ids) != len(set(event_ids)):
        raise EventDiscoveryExecutionBlocked(
            "discovery response has duplicate provider event IDs"
        )
    if len(matches) == 0:
        raise EventDiscoveryExecutionBlocked(
            "discovery response has zero matching events"
        )
    if len(matches) != 1:
        raise EventDiscoveryExecutionBlocked(
            "discovery response has multiple matching events"
        )
    resolved = str(matches[0].get("event_id", "")).strip()
    if not resolved:
        raise EventDiscoveryExecutionBlocked(
            "discovery match has malformed provider event ID"
        )
    teams = matches[0].get("teams")
    if (
        not isinstance(teams, list)
        or len(teams) != 2
        or any(not isinstance(team, Mapping) for team in teams)
    ):
        raise EventDiscoveryExecutionBlocked(
            "discovery match has malformed participants"
        )
    typed_teams = tuple(team for team in teams if isinstance(team, Mapping))
    home_marked = tuple(team for team in typed_teams if team.get("is_home") is True)
    away_marked = tuple(team for team in typed_teams if team.get("is_away") is True)
    if home_marked or away_marked:
        if len(home_marked) != 1 or len(away_marked) != 1:
            raise EventDiscoveryExecutionBlocked(
                "discovery participant markers are ambiguous"
            )
        home_team = home_marked[0]
        away_team = away_marked[0]
    else:
        # TheRundown's documented unmarked order is [away, home].
        away_team, home_team = typed_teams
    home_participant_id = str(home_team.get("team_id", "")).strip()
    away_participant_id = str(away_team.get("team_id", "")).strip()
    if not home_participant_id or not away_participant_id:
        raise EventDiscoveryExecutionBlocked(
            "discovery match has missing provider participant IDs"
        )
    if home_participant_id in {"0", "None"} or away_participant_id in {"0", "None"}:
        raise EventDiscoveryExecutionBlocked(
            "discovery match has invalid provider participant IDs"
        )
    if home_participant_id == away_participant_id:
        raise EventDiscoveryExecutionBlocked(
            "discovery match has duplicate provider participant IDs"
        )
    if target.home_participant_id is not None and (
        home_participant_id != target.home_participant_id
        or away_participant_id != target.away_participant_id
    ):
        raise EventDiscoveryExecutionBlocked("discovery participant identity mismatch")
    if target.kickoff <= response_at:
        raise EventDiscoveryExecutionBlocked(
            "discovery fixture is stale or already started"
        )
    return resolved, home_participant_id, away_participant_id


def _resolve_event_id(
    target: TheRundownEventDiscoveryTargetV1,
    payload: Mapping[str, object],
    *,
    response_at: datetime,
) -> str:
    """Compatibility wrapper returning only the provider event ID."""

    return _resolve_event_identity(target, payload, response_at=response_at)[0]


def discover_five_league_events(
    authorization: TheRundownEventDiscoveryAuthorizationV1,
    *,
    transport: TheRundownEventDiscoveryTransport,
    now: datetime,
    pacer: Callable[[float], None] | None = None,
) -> tuple[TheRundownEventDiscoveryEvidenceV1, ...]:
    """Resolve exactly five IDs; any failure emits no partial artifact set."""

    now = _utc_datetime(now, "discovery now")
    authorization.validate(now=now)
    quota_proof = TheRundownB4QuotaProofV1.load_canonical(now=now)
    authorization.validate_against_quota_proof(quota_proof, now=now)
    TheRundownDiscoveryAuthorizationConsumptionStore().consume(authorization, now=now)
    wait = pacer or (lambda _seconds: None)
    artifacts: list[TheRundownEventDiscoveryEvidenceV1] = []
    datapoints_total = 0
    for index, target in enumerate(authorization.targets):
        if index:
            wait(authorization.minimum_interval_seconds)
        request = TheRundownEventDiscoveryRequestV1(authorization, target, index)
        request.validate()
        try:
            response = transport.execute(request)
        except Exception as exc:
            raise EventDiscoveryExecutionBlocked(
                f"{target.league}: discovery transport failure"
            ) from exc
        response.validate()
        if response.network_execution is not True:
            # Offline fake transport is permitted for tests, but its evidence
            # remains explicitly non-authorizing and cannot be mistaken for a run.
            network_execution = False
        else:
            network_execution = True
        if _utc_datetime(response.finished_at, "response finished_at") > now:
            raise EventDiscoveryExecutionBlocked(
                "discovery response is from the future"
            )
        datapoints, remaining = _billing(response)
        datapoints_total = _validated_datapoint_total(datapoints_total, datapoints)
        event_id, home_participant_id, away_participant_id = _resolve_event_identity(
            target, response.payload, response_at=response.finished_at
        )
        raw_digest = digest_record(response.payload)
        evidence_payload = {
            "schema_version": DISCOVERY_EVIDENCE_SCHEMA_VERSION,
            "discovery_authorization_id": authorization.discovery_authorization_id,
            "discovery_authorization_digest": authorization.authorization_digest,
            "provider": target.provider,
            "league": target.league,
            "fixture_key": target.fixture_key,
            "home_team": target.home_team,
            "away_team": target.away_team,
            "home_participant_id": home_participant_id,
            "away_participant_id": away_participant_id,
            "kickoff": target.kickoff,
            "provider_event_id": event_id,
            "request_identity": target.request_identity,
            "request_shape_digest": authorization.request_shape_digest,
            "request_started_at": response.started_at,
            "response_completed_at": response.finished_at,
            "raw_response_digest": raw_digest,
            "datapoints": datapoints,
            "remaining_datapoints": remaining,
            "retry_count": 0,
            "network_execution": network_execution,
            "qualification_eligible": False,
            "receipt_eligible": False,
            "provider_authority": False,
            "activation_authorized": False,
            "publication_authorized": False,
            "ledger_mutated": False,
            "monetary_spend_authorized": False,
        }
        evidence = TheRundownEventDiscoveryEvidenceV1(
            discovery_authorization_id=authorization.discovery_authorization_id,
            discovery_authorization_digest=authorization.authorization_digest,
            provider=target.provider,
            league=target.league,
            fixture_key=target.fixture_key,
            home_team=target.home_team,
            away_team=target.away_team,
            home_participant_id=home_participant_id,
            away_participant_id=away_participant_id,
            kickoff=target.kickoff,
            provider_event_id=event_id,
            request_identity=target.request_identity,
            request_shape_digest=authorization.request_shape_digest,
            request_started_at=response.started_at,
            response_completed_at=response.finished_at,
            raw_response_digest=raw_digest,
            provider_event_evidence_digest=_digest(evidence_payload),
            datapoints=datapoints,
            remaining_datapoints=remaining,
            network_execution=network_execution,
        )
        evidence.validate()
        artifacts.append(evidence)
    if len(artifacts) != TOP5_CONTROLLED_SHADOW_REQUEST_COUNT:
        raise EventDiscoveryExecutionBlocked("discovery did not resolve five events")
    return tuple(artifacts)


def materialize_prebound_network_configuration(
    authorization: TheRundownEventDiscoveryAuthorizationV1,
    evidence: Sequence[TheRundownEventDiscoveryEvidenceV1],
    *,
    enabled: bool = False,
) -> TheRundownNetworkConfigurationV1:
    """Convert all five non-authorizing artifacts into strict run config."""

    if enabled is not False:
        raise EventDiscoveryExecutionBlocked(
            "discovery cannot enable controlled network execution"
        )
    authorization.validate(now=authorization.issued_at)
    if len(evidence) != len(DISCOVERY_LEAGUE_ORDER):
        raise EventDiscoveryContractError(
            "exactly five discovery artifacts are required"
        )
    if len({item.league for item in evidence}) != len(DISCOVERY_LEAGUE_ORDER):
        raise EventDiscoveryContractError(
            "discovery artifacts contain duplicate leagues"
        )
    by_league = {item.league: item for item in evidence}
    if tuple(by_league) != DISCOVERY_LEAGUE_ORDER:
        raise EventDiscoveryContractError(
            "discovery artifacts are incomplete or unordered"
        )
    targets: list[TheRundownCanaryTargetV1] = []
    for target in authorization.targets:
        item = by_league[target.league]
        item.validate()
        if item.discovery_authorization_id != authorization.discovery_authorization_id:
            raise EventDiscoveryContractError(
                "discovery authorization binding mismatch"
            )
        if item.discovery_authorization_digest != authorization.authorization_digest:
            raise EventDiscoveryContractError("discovery authorization digest mismatch")
        target_participants_match = target.home_participant_id is None or (
            item.home_participant_id == target.home_participant_id
            and item.away_participant_id == target.away_participant_id
        )
        if (
            item.fixture_key != target.fixture_key
            or item.request_identity != target.request_identity
            or item.provider != target.provider
            or item.home_team != target.home_team
            or item.away_team != target.away_team
            or not target_participants_match
            or _utc_datetime(item.kickoff, "evidence kickoff")
            != _utc_datetime(target.kickoff, "target kickoff")
        ):
            raise EventDiscoveryContractError(
                "discovery fixture/request binding mismatch"
            )
        if item.provider_event_id in {
            candidate.provider_event_id
            for candidate in evidence
            if candidate is not item
        }:
            raise EventDiscoveryContractError(
                "discovery provider event IDs must be unique"
            )
        targets.append(
            TheRundownCanaryTargetV1(
                provider=target.provider,
                league=target.league,
                fixture_key=target.fixture_key,
                provider_event_id=item.provider_event_id,
                home_team=target.home_team,
                away_team=target.away_team,
                kickoff=target.kickoff,
            )
        )
    participants = tuple(
        TheRundownNetworkParticipantScopeV1(
            fixture_key=item.fixture_key,
            home_participant_id=item.home_participant_id,
            away_participant_id=item.away_participant_id,
        )
        for target in authorization.targets
        for item in (by_league[target.league],)
    )
    requests = tuple(
        TheRundownNetworkRequestScopeV1(
            fixture_key=target.fixture_key,
            request_identity=target.request_identity,
        )
        for target in authorization.targets
    )
    draft = TheRundownNetworkConfigurationV1(
        targets=tuple(targets),
        participant_scope=participants,
        request_scope=requests,
        adapter_version=authorization.adapter_version,
        adapter_source_sha=authorization.adapter_source_sha,
        maximum_request_count=TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
        maximum_datapoints=TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
        maximum_quota_cost_units=float(TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET),
        request_quota_cost_units=float(THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST),
        maximum_source_age_seconds=300,
        minimum_interval_seconds=authorization.minimum_interval_seconds,
        maximum_retries=0,
        enabled=enabled,
        no_bet=True,
        publication=False,
        production_activation=False,
        monetary_spend_authorized=False,
        configuration_digest="0" * 64,
    )
    configuration = replace(
        draft, configuration_digest=draft.computed_configuration_digest
    )
    configuration.validate()
    return configuration


__all__ = [
    "B4_QUOTA_PROOF_AFFILIATE_IDS",
    "B4_QUOTA_PROOF_EXECUTION_PHASE",
    "B4_QUOTA_PROOF_MARKET_IDS",
    "B4_QUOTA_PROOF_MAX_DATAPOINTS",
    "B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION",
    "B4_QUOTA_PROOF_SCHEMA_VERSION",
    "B4_QUOTA_PROOF_SNAPSHOT_MAX_OFFSET_DAYS",
    "B4_QUOTA_PROOF_SPORT_ID",
    "CURRENT_TARGET_MANIFEST_MAX_AGE_SECONDS",
    "CURRENT_TARGET_MANIFEST_SCHEMA_VERSION",
    "DISCOVERY_AUTHORIZATION_SCHEMA_VERSION",
    "DISCOVERY_CONSUMPTION_SCHEMA_VERSION",
    "DISCOVERY_EVIDENCE_SCHEMA_VERSION",
    "DISCOVERY_MINIMUM_COMBINED_HEADROOM",
    "DISCOVERY_SCHEMA_VERSION",
    "EventDiscoveryContractError",
    "EventDiscoveryExecutionBlocked",
    "TheRundownB4QuotaProofV1",
    "TheRundownDiscoveryAuthorizationConsumptionStore",
    "TheRundownEventDiscoveryAuthorizationV1",
    "TheRundownEventDiscoveryEvidenceV1",
    "TheRundownEventDiscoveryRequestV1",
    "TheRundownEventDiscoveryResponseV1",
    "TheRundownEventDiscoveryTargetV1",
    "TheRundownEventDiscoveryTransport",
    "Top5CurrentDiscoveryTargetManifestV1",
    "b4_quota_proof_state_path",
    "discover_five_league_events",
    "discovery_authorization_consumption_state_path",
    "discovery_request_shape_digest",
    "load_current_top5_discovery_target_manifest",
    "materialize_prebound_network_configuration",
    "select_current_top5_discovery_targets",
]
