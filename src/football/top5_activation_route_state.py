"""Durable external route-state records for a future controlled Top-5 canary.

The store is atomic, owner-only, locked and digest validated. It records and
restores route configuration exactly, but is not wired to a live production
route consumer on current main; callers must not interpret a file read-back as
production activation or verified production rollback.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from src.football.production_contracts import (
    ProductionContractError,
    RuntimeStateBinding,
    RuntimeStateOwner,
    _utc,
)
from src.football.top5_activation_authorization import (
    Top5ActivationExecutionBindingV1,
    VerifiedTop5ActivationAuthorizationV1,
)
from src.football.top5_controlled_shadow_provider_qualification import TOP5_LEAGUES
from src.football.top5_durable_activation import _canonical_bytes, _sha
from src.football.top5_production_activation import current_top5_routing_snapshot
from src.football.top5_signal_lifecycle import DEFAULT_SIGNAL_LIFECYCLE_CONTRACT
from src.runtime.paths import ROOT, runtime_state_path

TOP5_ROUTE_STATE_SCHEMA = "top5-production-route-state-v1"
TOP5_ROUTE_STORE_SCHEMA = "top5-production-route-store-v1"
TOP5_ROUTE_STATE_PATH = "football/top5/controlled_activation/route-state-v1.json"
TOP5_ROUTE_CONSUMER_BLOCKER = "NO_LIVE_TOP5_PRODUCTION_ROUTE_STATE_CONSUMER"


class Top5RouteStateError(ProductionContractError):
    """Invalid durable route record or unsafe transition."""


def _digest_text(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise Top5RouteStateError(f"{name} must be a SHA-256 digest")
    if any(char not in "0123456789abcdef" for char in value):
        raise Top5RouteStateError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _false_safety_fields(payload: Mapping[str, object]) -> None:
    for field in (
        "publication",
        "recurring_scheduler",
        "betting",
        "ledger_mutation",
    ):
        if payload.get(field) is not False:
            raise Top5RouteStateError(f"{field} must remain false")


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(self.fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(self.fd)
            self.fd = None
            raise Top5RouteStateError("route lock must be a user-owned regular file")
        os.fchmod(self.fd, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class DurableTop5ProductionRouteStateStore:
    """External digest-checked route records; no scheduler/provider side effect."""

    def __init__(self, path: Path | None = None) -> None:
        RuntimeStateBinding(
            owner=RuntimeStateOwner.TOP5_SHADOW,
            relative_path=TOP5_ROUTE_STATE_PATH,
            external_required=True,
        ).validate()
        self.path = Path(
            path or runtime_state_path(TOP5_ROUTE_STATE_PATH, require_external=True)
        )
        resolved = self.path.resolve()
        if resolved == ROOT.resolve() or ROOT.resolve() in resolved.parents:
            raise Top5RouteStateError("route state must remain outside the checkout")
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _locked(self) -> _FileLock:
        return _FileLock(self.lock_path)

    @staticmethod
    def _disabled_route() -> dict[str, object]:
        baseline = current_top5_routing_snapshot(
            snapshot_id="top5-disabled-route-code-baseline"
        )
        return {
            "schema_version": TOP5_ROUTE_STATE_SCHEMA,
            "activation_mode": "DISABLED",
            "activation_id": None,
            "active_league": None,
            "active_fixture": None,
            "lifecycle_contract_id": None,
            "lifecycle_stage": None,
            "model_identity": None,
            "model_artifact_hash": None,
            "source_sha": None,
            "research_sha": None,
            "provider_authority": "the_odds_api",
            "activation_plan_digest": None,
            "signed_authorization_digest": None,
            "pre_activation_snapshot_digest": baseline.snapshot_digest,
            "current_configuration_digest": baseline.provider_config_digest,
            "last_transition_time": None,
            "publication": False,
            "recurring_scheduler": False,
            "betting": False,
            "ledger_mutation": False,
        }

    def _initial(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": TOP5_ROUTE_STORE_SCHEMA,
            "revision": 0,
            "current_route": self._disabled_route(),
            "records": {},
            "consumed_nonces": {},
            "audit_history": [],
        }
        body["state_digest"] = _sha(body)
        return body

    @staticmethod
    def _validate_route(route: object) -> None:
        if not isinstance(route, dict):
            raise Top5RouteStateError("route state must be an object")
        expected = {
            "schema_version",
            "activation_mode",
            "activation_id",
            "active_league",
            "active_fixture",
            "lifecycle_contract_id",
            "lifecycle_stage",
            "model_identity",
            "model_artifact_hash",
            "source_sha",
            "research_sha",
            "provider_authority",
            "activation_plan_digest",
            "signed_authorization_digest",
            "pre_activation_snapshot_digest",
            "current_configuration_digest",
            "last_transition_time",
            "publication",
            "recurring_scheduler",
            "betting",
            "ledger_mutation",
        }
        if (
            set(route) != expected
            or route.get("schema_version") != TOP5_ROUTE_STATE_SCHEMA
        ):
            raise Top5RouteStateError("route state schema fields are invalid")
        if route.get("provider_authority") != "the_odds_api":
            raise Top5RouteStateError("route authority must remain the_odds_api")
        _false_safety_fields(route)
        for name in ("pre_activation_snapshot_digest", "current_configuration_digest"):
            _digest_text(route.get(name), name)
        mode = route.get("activation_mode")
        if mode == "DISABLED":
            if (
                route.get("active_league") is not None
                or route.get("active_fixture") is not None
            ):
                raise Top5RouteStateError(
                    "disabled route cannot have an active fixture"
                )
        elif mode == "CONTROLLED_ONE_SHOT_EXECUTING":
            required = (
                "activation_id",
                "active_league",
                "active_fixture",
                "lifecycle_contract_id",
                "lifecycle_stage",
                "model_identity",
                "model_artifact_hash",
                "source_sha",
                "research_sha",
                "activation_plan_digest",
                "signed_authorization_digest",
            )
            if any(
                not isinstance(route.get(name), str) or not route[name]
                for name in required
            ):
                raise Top5RouteStateError("executing route binding is incomplete")
            if route.get("active_league") not in TOP5_LEAGUES:
                raise Top5RouteStateError(
                    "executing route league is not canonical Top-5"
                )
            if route.get("lifecycle_stage") not in {"INITIAL", "REFINEMENT"}:
                raise Top5RouteStateError("executing route lifecycle stage is invalid")
            if (
                route.get("lifecycle_contract_id")
                != DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id
            ):
                raise Top5RouteStateError(
                    "executing route lifecycle contract is non-canonical"
                )
            _digest_text(route.get("activation_plan_digest"), "activation plan digest")
            _digest_text(
                route.get("signed_authorization_digest"), "signed authorization digest"
            )
            _digest_text(route.get("model_artifact_hash"), "model artifact hash")
            for name in ("source_sha", "research_sha"):
                value = route.get(name)
                if (
                    not isinstance(value, str)
                    or len(value) not in (40, 64)
                    or any(
                        character not in "0123456789abcdefABCDEF" for character in value
                    )
                ):
                    raise Top5RouteStateError(f"{name} is not a source SHA")
        else:
            raise Top5RouteStateError("route activation mode is unsupported")

    def _read_unlocked(self) -> dict[str, object]:
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return self._initial()
        except OSError as exc:
            raise Top5RouteStateError("route state cannot be opened safely") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid()
            ):
                raise Top5RouteStateError(
                    "route state must be an owner-only user-owned regular file"
                )
            if metadata.st_size > 10_000_000:
                raise Top5RouteStateError("route state exceeds the safe size limit")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = json.loads(stream.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Top5RouteStateError("route state is unreadable or malformed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "revision",
            "current_route",
            "records",
            "consumed_nonces",
            "audit_history",
            "state_digest",
        }:
            raise Top5RouteStateError("route store envelope is malformed")
        claimed = raw.pop("state_digest")
        if raw.get("schema_version") != TOP5_ROUTE_STORE_SCHEMA or _sha(raw) != claimed:
            raise Top5RouteStateError("route store digest/schema mismatch")
        self._validate_route(raw.get("current_route"))
        if not isinstance(raw.get("records"), dict) or not isinstance(
            raw.get("consumed_nonces"), dict
        ):
            raise Top5RouteStateError("route records/nonce index is malformed")
        if not isinstance(raw.get("audit_history"), list):
            raise Top5RouteStateError("route audit history is malformed")
        for activation_id, record in raw["records"].items():
            self._validate_record(activation_id, record)
        for nonce, authorization_digest in raw["consumed_nonces"].items():
            if (
                not isinstance(nonce, str)
                or not nonce
                or not isinstance(authorization_digest, str)
            ):
                raise Top5RouteStateError(
                    "consumed authorization nonce index is malformed"
                )
            _digest_text(authorization_digest, "consumed authorization digest")
        raw["state_digest"] = claimed
        return raw

    @staticmethod
    def _validate_record(activation_id: object, record: object) -> None:
        expected = {
            "schema_version",
            "activation_id",
            "activation_plan_digest",
            "execution_binding",
            "status",
            "created_at",
            "updated_at",
            "pre_activation_snapshot",
            "pre_activation_snapshot_digest",
            "signed_authorization_digest",
            "authorization_nonce",
            "publication",
            "recurring_scheduler",
            "betting",
            "ledger_mutation",
            "rollback_readback_digest",
            "record_digest",
        }
        if (
            not isinstance(activation_id, str)
            or not isinstance(record, dict)
            or set(record) != expected
        ):
            raise Top5RouteStateError("activation route record schema is malformed")
        if (
            record.get("schema_version") != TOP5_ROUTE_STORE_SCHEMA
            or record.get("activation_id") != activation_id
        ):
            raise Top5RouteStateError("activation route record identity is invalid")
        body = {key: value for key, value in record.items() if key != "record_digest"}
        if _sha(body) != record.get("record_digest"):
            raise Top5RouteStateError("activation route record digest mismatch")
        _digest_text(record.get("activation_plan_digest"), "activation plan digest")
        snapshot = record.get("pre_activation_snapshot")
        if not isinstance(snapshot, dict) or _sha(snapshot) != record.get(
            "pre_activation_snapshot_digest"
        ):
            raise Top5RouteStateError("saved pre-activation snapshot digest mismatch")
        DurableTop5ProductionRouteStateStore._validate_route(snapshot)
        binding = record.get("execution_binding")
        if not isinstance(binding, dict) or _sha(binding) != record.get(
            "activation_plan_digest"
        ):
            raise Top5RouteStateError("execution binding digest mismatch")
        if record.get("status") not in {
            "PREPARED",
            "AUTHORIZED",
            "EXECUTING",
            "ROLLBACK",
            "ROLLED_BACK",
        }:
            raise Top5RouteStateError("activation route record status is invalid")
        _false_safety_fields(record)
        if record.get("status") in {
            "AUTHORIZED",
            "EXECUTING",
            "ROLLBACK",
            "ROLLED_BACK",
        }:
            _digest_text(
                record.get("signed_authorization_digest"), "signed authorization digest"
            )
            if (
                not isinstance(record.get("authorization_nonce"), str)
                or not record["authorization_nonce"]
            ):
                raise Top5RouteStateError("authorized route record nonce is missing")
        if record.get("status") == "ROLLED_BACK":
            _digest_text(
                record.get("rollback_readback_digest"), "rollback read-back digest"
            )

    def _write_unlocked(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise Top5RouteStateError("route state must not be a symlink")
        body = {key: value for key, value in state.items() if key != "state_digest"}
        body["state_digest"] = _sha(body)
        data = _canonical_bytes(body)
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self) -> dict[str, object]:
        with self._locked():
            state = self._read_unlocked()
            return json.loads(json.dumps(state))

    def prepare(
        self,
        binding: Top5ActivationExecutionBindingV1,
        *,
        now: datetime,
    ) -> dict[str, object]:
        now_utc = _utc(now, "now")
        if binding.provider_authority != "the_odds_api" or binding.retry_budget != 0:
            raise Top5RouteStateError("prepared route violates provider/retry contract")
        if binding.activation_league not in TOP5_LEAGUES:
            raise Top5RouteStateError("prepared route league is not canonical Top-5")
        if (
            binding.lifecycle_contract_id
            != DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id
            or binding.lifecycle_stage not in {"INITIAL", "REFINEMENT"}
        ):
            raise Top5RouteStateError(
                "prepared route lifecycle binding is non-canonical"
            )
        for name in (
            "durable_plan_digest",
            "five_league_evidence_digest",
            "b1_acceptance_manifest_digest",
            "model_artifact_hash",
            "pre_activation_routing_configuration_digest",
            "rollback_snapshot_digest",
        ):
            _digest_text(getattr(binding, name), name)
        for name in ("source_sha", "research_sha"):
            value = getattr(binding, name)
            if (
                not isinstance(value, str)
                or len(value) not in (40, 64)
                or any(character not in "0123456789abcdefABCDEF" for character in value)
            ):
                raise Top5RouteStateError(f"{name} is not a source SHA")
        with self._locked():
            state = self._read_unlocked()
            current = state["current_route"]
            self._validate_route(current)
            if binding.pre_activation_routing_configuration_digest != current.get(
                "current_configuration_digest"
            ) or binding.rollback_snapshot_digest != current.get(
                "pre_activation_snapshot_digest"
            ):
                raise Top5RouteStateError(
                    "prepared plan does not match the disabled route baseline"
                )
            prior = state["records"].get(binding.activation_id)
            if prior is not None:
                if (
                    prior.get("activation_plan_digest")
                    != binding.activation_plan_digest
                ):
                    raise Top5RouteStateError(
                        "activation ID is already bound to another plan"
                    )
                if prior.get("status") == "PREPARED":
                    return json.loads(json.dumps(prior))
                raise Top5RouteStateError("activation has already left PREPARED")
            if (
                current.get("activation_mode") != "DISABLED"
                or current.get("active_league") is not None
            ):
                raise Top5RouteStateError("another Top-5 route is not disabled")
            if any(
                item.get("status") in {"PREPARED", "AUTHORIZED", "EXECUTING", "ACTIVE"}
                for item in state["records"].values()
            ):
                raise Top5RouteStateError("another activation is pending or active")
            baseline = json.loads(json.dumps(current))
            record = {
                "schema_version": TOP5_ROUTE_STORE_SCHEMA,
                "activation_id": binding.activation_id,
                "activation_plan_digest": binding.activation_plan_digest,
                "execution_binding": binding._core_payload(),
                "status": "PREPARED",
                "created_at": now_utc.isoformat(),
                "updated_at": now_utc.isoformat(),
                "pre_activation_snapshot": baseline,
                "pre_activation_snapshot_digest": _sha(baseline),
                "signed_authorization_digest": None,
                "authorization_nonce": None,
                "publication": False,
                "recurring_scheduler": False,
                "betting": False,
                "ledger_mutation": False,
                "rollback_readback_digest": None,
            }
            record["record_digest"] = _sha(record)
            records = dict(state["records"])
            records[binding.activation_id] = record
            history = list(state["audit_history"])
            history.append(
                {
                    "transition": "PREPARED",
                    "activation_id": binding.activation_id,
                    "activation_plan_digest": binding.activation_plan_digest,
                    "at": now_utc.isoformat(),
                }
            )
            state.update(
                {
                    "records": records,
                    "audit_history": history,
                    "revision": int(state["revision"]) + 1,
                }
            )
            self._write_unlocked(state)
            return json.loads(json.dumps(record))

    def authorize(
        self,
        binding: Top5ActivationExecutionBindingV1,
        authorization: VerifiedTop5ActivationAuthorizationV1,
        *,
        now: datetime,
    ) -> dict[str, object]:
        now_utc = _utc(now, "now")
        if not isinstance(authorization, VerifiedTop5ActivationAuthorizationV1):
            raise Top5RouteStateError(
                "authorization must be verifier-produced Ed25519 evidence"
            )
        try:
            authorization.assert_valid_at(now_utc)
        except ProductionContractError as exc:
            raise Top5RouteStateError(str(exc)) from exc
        for field, expected in binding.expected_signed_claims().items():
            if authorization.payload.get(field) != expected:
                raise Top5RouteStateError(
                    f"verified authorization binding mismatch: {field}"
                )
        _digest_text(authorization.authorization_digest, "authorization_digest")
        with self._locked():
            state = self._read_unlocked()
            records = dict(state["records"])
            record = records.get(binding.activation_id)
            if (
                not isinstance(record, dict)
                or record.get("activation_plan_digest")
                != binding.activation_plan_digest
            ):
                raise Top5RouteStateError("exact PREPARED route plan is missing")
            if record.get("status") == "AUTHORIZED":
                if (
                    record.get("signed_authorization_digest")
                    == authorization.authorization_digest
                ):
                    return json.loads(json.dumps(record))
                raise Top5RouteStateError(
                    "activation is authorized by a different signature"
                )
            if record.get("status") != "PREPARED":
                raise Top5RouteStateError("activation is not PREPARED")
            nonce_index = dict(state["consumed_nonces"])
            if authorization.nonce in nonce_index:
                raise Top5RouteStateError("authorization nonce replay detected")
            nonce_index[authorization.nonce] = authorization.authorization_digest
            record = dict(record)
            record.update(
                {
                    "status": "AUTHORIZED",
                    "signed_authorization_digest": authorization.authorization_digest,
                    "authorization_nonce": authorization.nonce,
                    "updated_at": now_utc.isoformat(),
                }
            )
            record["record_digest"] = _sha(
                {key: value for key, value in record.items() if key != "record_digest"}
            )
            records[binding.activation_id] = record
            history = list(state["audit_history"])
            history.append(
                {
                    "transition": "AUTHORIZED",
                    "activation_id": binding.activation_id,
                    "activation_plan_digest": binding.activation_plan_digest,
                    "signed_authorization_digest": authorization.authorization_digest,
                    "at": now_utc.isoformat(),
                }
            )
            state.update(
                {
                    "records": records,
                    "consumed_nonces": nonce_index,
                    "audit_history": history,
                    "revision": int(state["revision"]) + 1,
                }
            )
            self._write_unlocked(state)
            return json.loads(json.dumps(record))

    def begin_execution(
        self,
        binding: Top5ActivationExecutionBindingV1,
        authorization: VerifiedTop5ActivationAuthorizationV1,
        *,
        now: datetime,
    ) -> dict[str, object]:
        """Persist EXECUTING only; this is not a live routing operation."""
        now_utc = _utc(now, "now")
        if not isinstance(authorization, VerifiedTop5ActivationAuthorizationV1):
            raise Top5RouteStateError(
                "authorization must be verifier-produced Ed25519 evidence"
            )
        try:
            authorization.assert_valid_at(now_utc)
        except ProductionContractError as exc:
            raise Top5RouteStateError(str(exc)) from exc
        for field, expected in binding.expected_signed_claims().items():
            if authorization.payload.get(field) != expected:
                raise Top5RouteStateError(
                    f"verified authorization binding mismatch: {field}"
                )
        with self._locked():
            state = self._read_unlocked()
            record = state["records"].get(binding.activation_id)
            if (
                not isinstance(record, dict)
                or record.get("status") != "AUTHORIZED"
                or record.get("activation_plan_digest")
                != binding.activation_plan_digest
                or record.get("signed_authorization_digest")
                != authorization.authorization_digest
            ):
                raise Top5RouteStateError(
                    "exact signed AUTHORIZED route plan is missing"
                )
            current = state["current_route"]
            if (
                current.get("activation_mode") != "DISABLED"
                or current.get("active_league") is not None
            ):
                raise Top5RouteStateError(
                    "production route baseline changed after preparation"
                )
            bound_configuration = _sha(
                {
                    "activation_plan_digest": binding.activation_plan_digest,
                    "provider_authority": "the_odds_api",
                    "activation_league": binding.activation_league,
                    "fixture_key": binding.fixture_key,
                    "lifecycle_contract_id": binding.lifecycle_contract_id,
                    "lifecycle_stage": binding.lifecycle_stage,
                    "model_identity": binding.model_identity,
                    "model_artifact_hash": binding.model_artifact_hash,
                    "source_sha": binding.source_sha,
                    "research_sha": binding.research_sha,
                }
            )
            executing = dict(current)
            executing.update(
                {
                    "activation_mode": "CONTROLLED_ONE_SHOT_EXECUTING",
                    "activation_id": binding.activation_id,
                    "active_league": binding.activation_league,
                    "active_fixture": binding.fixture_key,
                    "lifecycle_contract_id": binding.lifecycle_contract_id,
                    "lifecycle_stage": binding.lifecycle_stage,
                    "model_identity": binding.model_identity,
                    "model_artifact_hash": binding.model_artifact_hash,
                    "source_sha": binding.source_sha,
                    "research_sha": binding.research_sha,
                    "provider_authority": "the_odds_api",
                    "activation_plan_digest": binding.activation_plan_digest,
                    "signed_authorization_digest": authorization.authorization_digest,
                    "current_configuration_digest": bound_configuration,
                    "last_transition_time": now_utc.isoformat(),
                }
            )
            self._validate_route(executing)
            record = dict(record)
            record.update({"status": "EXECUTING", "updated_at": now_utc.isoformat()})
            record["record_digest"] = _sha(
                {key: value for key, value in record.items() if key != "record_digest"}
            )
            records = dict(state["records"])
            records[binding.activation_id] = record
            history = list(state["audit_history"])
            history.append(
                {
                    "transition": "EXECUTING",
                    "activation_id": binding.activation_id,
                    "at": now_utc.isoformat(),
                }
            )
            state.update(
                {
                    "current_route": executing,
                    "records": records,
                    "audit_history": history,
                    "revision": int(state["revision"]) + 1,
                }
            )
            self._write_unlocked(state)
            return json.loads(json.dumps(record))

    def mark_production_verified(self, *_args: object, **_kwargs: object) -> None:
        """Do not synthesize the missing live provider/model/health evidence."""
        raise Top5RouteStateError(TOP5_ROUTE_CONSUMER_BLOCKER)

    def rollback(
        self,
        activation_id: str,
        activation_plan_digest: str,
        *,
        now: datetime,
    ) -> dict[str, object]:
        """Restore the saved route record and verify durable read-back only.

        This is explicitly not a production rollback until a live route
        consumer integrates this store and its value is reread through that
        consumer.
        """
        now_utc = _utc(now, "now")
        _digest_text(activation_plan_digest, "activation_plan_digest")
        with self._locked():
            state = self._read_unlocked()
            records = dict(state["records"])
            record = records.get(activation_id)
            if (
                not isinstance(record, dict)
                or record.get("activation_plan_digest") != activation_plan_digest
            ):
                raise Top5RouteStateError("rollback requires the exact activation plan")
            if record.get("status") == "ROLLED_BACK":
                return json.loads(json.dumps(record))
            if record.get("status") not in {"EXECUTING", "ROLLBACK"}:
                raise Top5RouteStateError("route is not in a rollback-capable state")
            baseline = record.get("pre_activation_snapshot")
            if not isinstance(baseline, dict) or _sha(baseline) != record.get(
                "pre_activation_snapshot_digest"
            ):
                raise Top5RouteStateError(
                    "saved pre-activation route snapshot is invalid"
                )
            record = dict(record)
            record.update({"status": "ROLLBACK", "updated_at": now_utc.isoformat()})
            record["record_digest"] = _sha(
                {key: value for key, value in record.items() if key != "record_digest"}
            )
            records[activation_id] = record
            history = list(state["audit_history"])
            history.append(
                {
                    "transition": "ROLLBACK",
                    "activation_id": activation_id,
                    "at": now_utc.isoformat(),
                }
            )
            state.update(
                {
                    "records": records,
                    "audit_history": history,
                    "revision": int(state["revision"]) + 1,
                }
            )
            self._write_unlocked(state)

            restored = json.loads(json.dumps(baseline))
            self._validate_route(restored)
            record = dict(record)
            record.update(
                {
                    "status": "ROLLED_BACK",
                    "updated_at": now_utc.isoformat(),
                    "rollback_readback_digest": _sha(restored),
                }
            )
            record["record_digest"] = _sha(
                {key: value for key, value in record.items() if key != "record_digest"}
            )
            records[activation_id] = record
            history.append(
                {
                    "transition": "ROLLED_BACK",
                    "activation_id": activation_id,
                    "at": now_utc.isoformat(),
                    "restored_snapshot_digest": _sha(restored),
                }
            )
            state.update(
                {
                    "current_route": restored,
                    "records": records,
                    "audit_history": history,
                    "revision": int(state["revision"]) + 1,
                }
            )
            self._write_unlocked(state)
            reread = self._read_unlocked()
            if reread.get("current_route") != restored:
                raise Top5RouteStateError(
                    "durable route-state rollback read-back mismatch"
                )
            return json.loads(json.dumps(record))


__all__ = [
    "TOP5_ROUTE_CONSUMER_BLOCKER",
    "TOP5_ROUTE_STATE_PATH",
    "TOP5_ROUTE_STATE_SCHEMA",
    "TOP5_ROUTE_STORE_SCHEMA",
    "DurableTop5ProductionRouteStateStore",
    "Top5RouteStateError",
]
