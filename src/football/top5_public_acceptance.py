"""Side-effect-free Top-5 public-read acceptance and publication precheck.

This module is the B3 boundary after Builder 1 acceptance and before any
production publication.  It validates a captured/generated public product;
it does not fetch providers, activate runtime state, write a publication
target, deploy a Worker, or mutate betting/ledger state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    inventory_for,
)
from src.notifications.public_serializer import (
    TOP5_PREPUBLICATION_RELEASE_SCHEMA,
    TOP5_PUBLIC_PROVIDER_AUTHORITY,
    PublicFootballCompatibilityError,
    canonical_top5_league,
    serialize_public_product,
    serialize_top5_prepublication_candidate_product,
)

TOP5_PUBLIC_DELIVERY_READY = "TOP5_PUBLIC_DELIVERY_READY"
TOP5_PUBLIC_DELIVERY_BLOCKED = "TOP5_PUBLIC_DELIVERY_BLOCKED"
TOP5_PREPUBLICATION_DELIVERY_READY = "TOP5_PREPUBLICATION_DELIVERY_READY"
TOP5_PREPUBLICATION_DELIVERY_BLOCKED = "TOP5_PREPUBLICATION_DELIVERY_BLOCKED"
TOP5_PUBLICATION_PRECHECK_READY = "TOP5_PUBLICATION_PRECHECK_READY"
TOP5_PUBLICATION_PRECHECK_BLOCKED = "TOP5_PUBLICATION_PRECHECK_BLOCKED"
TOP5_LEAGUES = ("EPL", "BL1", "LL", "SA", "L1")
TOP5_RELEASE_SCHEMA = "top5-public-release-v1"
TOP5_PREPUBLICATION_ARTIFACT_SCHEMA = "top5-prepublication-artifact-v1"
TOP5_PREPUBLICATION_DRY_RUN_SCHEMA = "top5-prepublication-delivery-dry-run-v1"
TOP5_PREPUBLICATION_DRY_RUN_STATUS = "TOP5_PREPUBLICATION_DRY_RUN"
_TOP5_LEAGUE_SET = frozenset(TOP5_LEAGUES)
_CANDIDATE_PROVIDER_MARKERS = frozenset(
    {"therundown", "therundown_experimental", "candidate", "shadow"}
)
_FORBIDDEN_PREPUBLICATION_KEYS = frozenset(
    {
        "activation_id",
        "capability",
        "capability_id",
        "capability_nonce",
        "publication_attestation",
        "publication_authorization_id",
        "published_at",
    }
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _required_text(value: object, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} is required")
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _required_digest(value: object, name: str) -> str:
    result = _required_text(value, name)
    if _SHA_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a hexadecimal digest")
    return result.lower()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _age(now: datetime, captured: datetime, scope: str) -> None:
    if captured > now:
        raise ValueError(f"{scope} is from the future")


def _prepublication_id(
    run_id: str, session_id: str, source_sha: str, runtime_data_sha: str
) -> str:
    return "top5-prepublication-v1:" + _digest(
        {
            "run_id": run_id,
            "session_id": session_id,
            "source_release_sha": source_sha,
            "runtime_data_sha": runtime_data_sha,
        }
    )


def _reject_prepublication_authority(value: object, path: str = "prepublication") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).casefold()
            if name in _FORBIDDEN_PREPUBLICATION_KEYS:
                raise ValueError(f"{path}.{key} is forbidden before publication")
            _reject_prepublication_authority(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_prepublication_authority(item, f"{path}[{index}]")


def _reject_candidate_authority(value: object, path: str = "prepublication") -> None:
    authority_keys = {
        "provider",
        "provider_authority",
        "provider_name",
        "source",
        "selected_provider",
        "active_provider_order",
        "authority",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in authority_keys:
                candidates = (
                    item
                    if isinstance(item, Sequence) and not isinstance(item, (str, bytes))
                    else (item,)
                )
                if any(
                    isinstance(candidate, str)
                    and (
                        candidate.casefold() == "therundown_experimental"
                        or "therundown" in candidate.casefold()
                    )
                    for candidate in candidates
                ):
                    raise ValueError(f"{path}.{key} leaks candidate provider authority")
            _reject_candidate_authority(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_candidate_authority(item, f"{path}[{index}]")


@dataclass(frozen=True)
class Top5PrepublicationArtifactV1:
    """Read-only proof binding equal Worker/static Top-5 candidate payloads."""

    prepublication_id: str
    prepared_at: str
    worker_candidate_payload: Mapping[str, object]
    static_candidate_payload: Mapping[str, object]
    public_product_digest: str
    worker_candidate_payload_digest: str
    static_candidate_payload_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "worker_candidate_payload", _freeze(self.worker_candidate_payload)
        )
        object.__setattr__(
            self, "static_candidate_payload", _freeze(self.static_candidate_payload)
        )

    @classmethod
    def create(
        cls,
        *,
        worker_candidate_payload: Mapping[str, object],
        static_candidate_payload: Mapping[str, object],
        prepared_at: datetime | str,
    ) -> Top5PrepublicationArtifactV1:
        """Project already-prepared payloads and derive only their digests/identity."""
        prepared = _timestamp(prepared_at, "prepublication prepared_at")
        if not isinstance(worker_candidate_payload, Mapping) or not isinstance(
            static_candidate_payload, Mapping
        ):
            raise TypeError("Worker/static candidate payloads must be objects")
        worker_input = deepcopy(dict(worker_candidate_payload))
        static_input = deepcopy(dict(static_candidate_payload))
        _reject_prepublication_authority(worker_input, "worker_candidate_payload")
        _reject_prepublication_authority(static_input, "static_candidate_payload")
        try:
            worker_release = worker_input["top5_release"]
            static_release = static_input["top5_release"]
            if not isinstance(worker_release, Mapping) or not isinstance(
                static_release, Mapping
            ):
                raise TypeError("Top-5 prepublication release must be an object")
            identity_values = (
                "controlled_shadow_run_id",
                "qualification_session_id",
                "source_release_sha",
                "runtime_data_sha",
            )
            for key in identity_values:
                if worker_release.get(key) != static_release.get(key):
                    raise ValueError(f"Worker/static {key} binding differs")
            run_id, session_id, source_sha, runtime_sha = (
                _required_text(worker_release.get(key), key) for key in identity_values
            )
            expected_id = _prepublication_id(
                run_id, session_id, source_sha, runtime_sha
            )
            worker_release = dict(worker_release)
            static_release = dict(static_release)
            worker_input["top5_release"] = worker_release
            static_input["top5_release"] = static_release
            for release in (worker_release, static_release):
                supplied_id = release.get("prepublication_id")
                if supplied_id not in (None, expected_id):
                    raise ValueError("prepublication_id does not match its run binding")
                release["prepublication_id"] = expected_id
            worker = serialize_top5_prepublication_candidate_product(worker_input)
            static = serialize_top5_prepublication_candidate_product(static_input)
        except TypeError as exc:
            raise TypeError(f"prepublication payload rejected: {exc}") from exc
        except (KeyError, PublicFootballCompatibilityError) as exc:
            raise ValueError(f"prepublication payload rejected: {exc}") from exc
        worker_digest = _digest(worker)
        static_digest = _digest(static)
        if worker != static:
            raise ValueError("Worker/static candidate payloads are not equal")
        return cls(
            prepublication_id=expected_id,
            prepared_at=prepared.isoformat(),
            worker_candidate_payload=worker,
            static_candidate_payload=static,
            public_product_digest=worker_digest,
            worker_candidate_payload_digest=worker_digest,
            static_candidate_payload_digest=static_digest,
        )

    @classmethod
    def from_mapping(cls, value: object) -> Top5PrepublicationArtifactV1:
        if not isinstance(value, Mapping):
            raise TypeError("prepublication artifact must be an object")
        expected = {
            "schema_version",
            "prepublication_id",
            "prepared_at",
            "worker_candidate_payload",
            "static_candidate_payload",
            "public_product_digest",
            "worker_candidate_payload_digest",
            "static_candidate_payload_digest",
            "publication_enabled",
            "publication_authorized",
            "capability_consumed",
            "mutation_performed",
            "provider_requests",
        }
        if set(value) != expected:
            raise ValueError("prepublication artifact fields are invalid")
        if value.get("schema_version") != TOP5_PREPUBLICATION_ARTIFACT_SCHEMA:
            raise ValueError("prepublication artifact schema is unsupported")
        if (
            value.get("publication_enabled") is not False
            or value.get("publication_authorized") is not False
            or value.get("capability_consumed") is not False
            or value.get("mutation_performed") is not False
            or value.get("provider_requests") != 0
        ):
            raise ValueError("prepublication artifact claims a forbidden side effect")
        if not isinstance(value.get("worker_candidate_payload"), Mapping) or not isinstance(
            value.get("static_candidate_payload"), Mapping
        ):
            raise TypeError("prepublication candidate payloads must be objects")
        return cls(
            prepublication_id=_required_text(value.get("prepublication_id"), "prepublication_id"),
            prepared_at=_required_text(value.get("prepared_at"), "prepared_at"),
            worker_candidate_payload=dict(value["worker_candidate_payload"]),
            static_candidate_payload=dict(value["static_candidate_payload"]),
            public_product_digest=_required_digest(value.get("public_product_digest"), "public_product_digest"),
            worker_candidate_payload_digest=_required_digest(value.get("worker_candidate_payload_digest"), "worker_candidate_payload_digest"),
            static_candidate_payload_digest=_required_digest(value.get("static_candidate_payload_digest"), "static_candidate_payload_digest"),
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": TOP5_PREPUBLICATION_ARTIFACT_SCHEMA,
            "prepublication_id": self.prepublication_id,
            "prepared_at": self.prepared_at,
            "worker_candidate_payload": _thaw(self.worker_candidate_payload),
            "static_candidate_payload": _thaw(self.static_candidate_payload),
            "public_product_digest": self.public_product_digest,
            "worker_candidate_payload_digest": self.worker_candidate_payload_digest,
            "static_candidate_payload_digest": self.static_candidate_payload_digest,
            "publication_enabled": False,
            "publication_authorized": False,
            "capability_consumed": False,
            "mutation_performed": False,
            "provider_requests": 0,
        }

    def validate(
        self,
        *,
        now: datetime,
        expected_run_id: str | None = None,
        expected_session_id: str | None = None,
        expected_source_release_sha: str | None = None,
        expected_runtime_data_sha: str | None = None,
        expected_model_artifact_hash: str | None = None,
        expected_signal_time_contract_id: str | None = None,
    ) -> dict[str, object]:
        from src.football.top5_final_acceptance import MAX_EVIDENCE_AGE_SECONDS

        checked_now = _timestamp(now, "now")
        prepared = _timestamp(self.prepared_at, "prepublication prepared_at")
        _age(checked_now, prepared, "prepublication artifact")
        if (checked_now - prepared).total_seconds() > MAX_EVIDENCE_AGE_SECONDS:
            raise ValueError("prepublication artifact is stale")
        worker_raw = _thaw(self.worker_candidate_payload)
        static_raw = _thaw(self.static_candidate_payload)
        assert isinstance(worker_raw, dict)
        assert isinstance(static_raw, dict)
        _reject_prepublication_authority(worker_raw, "worker_candidate_payload")
        _reject_prepublication_authority(static_raw, "static_candidate_payload")
        _reject_candidate_authority(worker_raw, "worker_candidate_payload")
        _reject_candidate_authority(static_raw, "static_candidate_payload")
        try:
            worker = serialize_top5_prepublication_candidate_product(worker_raw)
            static = serialize_top5_prepublication_candidate_product(static_raw)
        except TypeError as exc:
            raise TypeError(
                f"prepublication candidate payload rejected: {exc}"
            ) from exc
        except (AssertionError, PublicFootballCompatibilityError, ValueError) as exc:
            raise ValueError(f"prepublication candidate payload rejected: {exc}") from exc
        if worker != worker_raw or static != static_raw:
            raise ValueError("prepublication candidate payload is not canonical")
        worker_digest = _digest(worker)
        static_digest = _digest(static)
        if worker != static:
            raise ValueError("Worker/static candidate payloads are not equal")
        if (
            self.public_product_digest != worker_digest
            or self.worker_candidate_payload_digest != worker_digest
            or self.static_candidate_payload_digest != static_digest
        ):
            raise ValueError("prepublication payload digest mismatch")
        release = worker.get("top5_release")
        records = worker.get("football")
        if not isinstance(release, Mapping):
            raise TypeError("prepublication Top-5 release must be an object")
        if not isinstance(records, list):
            raise TypeError("prepublication outcome records must be a list")
        run_id = _required_text(release.get("controlled_shadow_run_id"), "run_id")
        session_id = _required_text(
            release.get("qualification_session_id"), "session_id"
        )
        source_sha = _required_digest(release.get("source_release_sha"), "source_release_sha")
        runtime_sha = _required_digest(release.get("runtime_data_sha"), "runtime_data_sha")
        model_hash = _required_digest(release.get("model_artifact_hash"), "model_artifact_hash")
        signal_contract = _required_text(
            release.get("signal_time_contract_id"), "signal_time_contract_id"
        )
        if release.get("schema_version") != TOP5_PREPUBLICATION_RELEASE_SCHEMA:
            raise ValueError("Top-5 release is not prepublication")
        if release.get("research_sha") != FROZEN_RESEARCH_SHA:
            raise ValueError("prepublication Research SHA differs from frozen Research")
        if (
            release.get("candidate_id") != M5_CANDIDATE_ID
            or release.get("model_identity") != M5_CANDIDATE_ID
        ):
            raise ValueError("prepublication model identity is not frozen M5")
        if (
            release.get("provider_authority") != TOP5_PUBLIC_PROVIDER_AUTHORITY
            or release.get("source_runtime_consistent") is not True
            or release.get("publication_status") != "PREPARED"
            or release.get("publication_enabled") is not False
            or release.get("publication_authorized") is not False
            or release.get("no_bet") is not True
            or release.get("league_codes") != sorted(TOP5_LEAGUES)
        ):
            raise ValueError("Top-5 prepublication release contract is invalid")
        if self.prepublication_id != _prepublication_id(
            run_id, session_id, source_sha, runtime_sha
        ) or release.get("prepublication_id") != self.prepublication_id:
            raise ValueError("prepublication identity binding mismatch")
        if expected_run_id is not None and run_id != expected_run_id:
            raise ValueError("prepublication run identity mismatch")
        if expected_session_id is not None and session_id != expected_session_id:
            raise ValueError("prepublication session identity mismatch")
        if expected_source_release_sha is not None and source_sha != expected_source_release_sha:
            raise ValueError("prepublication source release binding mismatch")
        if expected_runtime_data_sha is not None and runtime_sha != expected_runtime_data_sha:
            raise ValueError("prepublication runtime data binding mismatch")
        expected_hashes = {
            inventory_for(league, M5_CANDIDATE_ID).model_artifact_hash
            for league in TOP5_LEAGUES
        }
        if len(expected_hashes) != 1 or model_hash not in expected_hashes:
            raise ValueError("prepublication model hash differs from frozen inventory")
        if expected_model_artifact_hash is not None and model_hash != expected_model_artifact_hash:
            raise ValueError("prepublication model hash binding mismatch")
        if expected_signal_time_contract_id is not None and signal_contract != expected_signal_time_contract_id:
            raise ValueError("prepublication Signal-Time contract binding mismatch")
        generated = _timestamp(release.get("generated_at"), "prepublication generated_at")
        _age(checked_now, generated, "prepublication product")
        if (checked_now - generated).total_seconds() > MAX_EVIDENCE_AGE_SECONDS:
            raise ValueError("prepublication product is stale")
        for record in records:
            if not isinstance(record, Mapping):
                raise TypeError("prepublication outcome record must be an object")
            signal_at = record.get("signal_timestamp") or record.get("prediction_timestamp")
            observed = _timestamp(signal_at, "prepublication signal timestamp")
            _age(checked_now, observed, "prepublication signal")
            if (checked_now - observed).total_seconds() > MAX_EVIDENCE_AGE_SECONDS:
                raise ValueError("prepublication signal is stale")
        return {
            "prepublication_id": self.prepublication_id,
            "provider_authority": TOP5_PUBLIC_PROVIDER_AUTHORITY,
            "candidate_id": M5_CANDIDATE_ID,
            "research_sha": FROZEN_RESEARCH_SHA,
            "public_product_digest": worker_digest,
            "worker_candidate_payload_digest": worker_digest,
            "static_candidate_payload_digest": static_digest,
            "run_id": run_id,
            "session_id": session_id,
            "source_release_sha": source_sha,
            "runtime_data_sha": runtime_sha,
            "model_artifact_hash": model_hash,
            "signal_time_contract_id": signal_contract,
        }

    def delivery_dry_run_manifest(self, *, rollback_ready: bool) -> dict[str, object]:
        return {
            "schema_version": TOP5_PREPUBLICATION_DRY_RUN_SCHEMA,
            "status": TOP5_PREPUBLICATION_DRY_RUN_STATUS,
            "prepublication_id": self.prepublication_id,
            "prepared_at": self.prepared_at,
            "public_product_digest": self.public_product_digest,
            "worker_candidate_payload_digest": self.worker_candidate_payload_digest,
            "static_candidate_payload_digest": self.static_candidate_payload_digest,
            "worker_candidate_payloads_equal": (
                dict(self.worker_candidate_payload) == dict(self.static_candidate_payload)
            ),
            "worker_destination": "/signals",
            "static_destination": "docs/data/signals.json",
            "provider_requests": 0,
            "network_requests": 0,
            "publication_enabled": False,
            "publication_authorized": False,
            "capability_consumed": False,
            "mutation_performed": False,
            "rollback_ready": rollback_ready,
        }


def _timestamp(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{field} is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} is malformed") from exc
    elif value is None:
        raise ValueError(f"{field} is missing")
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _reason(code: str, message: str, *, field: str | None = None) -> dict[str, str]:
    result = {"code": code, "message": message}
    if field:
        result["field"] = field
    return result


def _candidate_provider(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return normalized in _CANDIDATE_PROVIDER_MARKERS or "therundown" in normalized


def _top5_records(payload: Mapping[str, object]) -> list[dict[str, object]]:
    records = payload.get("football")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return []
    return [
        dict(record)
        for record in records
        if isinstance(record, Mapping)
        and canonical_top5_league(record.get("league")) in _TOP5_LEAGUE_SET
    ]


def _validate_offline_fixture(
    payload: Mapping[str, object], reasons: list[dict[str, str]]
) -> None:
    records = _top5_records(payload)
    if str(payload.get("fixture_mode", "")).upper() not in {"TEST/OFFLINE", "OFFLINE"}:
        reasons.append(
            _reason(
                "PUBLIC_TEST_DATA_REJECTED",
                "offline acceptance requires explicit TEST/OFFLINE classification",
            )
        )
    if (
        len(records) != 5
        or len({str(canonical_top5_league(record.get("league"))) for record in records})
        != 5
    ):
        reasons.append(
            _reason(
                "PUBLIC_LEAGUE_INCOMPLETE",
                "offline fixture must contain one record for each Top-5 league",
            )
        )
    for record in records:
        if str(canonical_top5_league(record.get("league"))) != record.get("league"):
            reasons.append(
                _reason(
                    "PUBLIC_LEAGUE_NONCANONICAL",
                    "offline fixture contains a non-canonical league identity",
                )
            )
        if str(record.get("evidence_kind", "")).upper() != "TEST_FIXTURE":
            reasons.append(
                _reason(
                    "PUBLIC_TEST_DATA_REJECTED",
                    "offline records must be marked TEST_FIXTURE",
                )
            )
        if record.get("synthetic") is not True:
            reasons.append(
                _reason(
                    "PUBLIC_TEST_DATA_REJECTED",
                    "offline records must be marked synthetic",
                )
            )
        if (
            record.get("publication_enabled") is not False
            or record.get("signal_status") != "SHADOW"
        ):
            reasons.append(
                _reason(
                    "PUBLIC_TEST_DATA_REJECTED",
                    "offline records cannot be publication-ready",
                )
            )


def validate_public_bundle(
    payload: Mapping[str, object],
    *,
    now: datetime,
    offline_fixture: bool = False,
    expected_source_release_sha: str | None = None,
    expected_runtime_data_sha: str | None = None,
) -> dict[str, object]:
    """Validate one public bundle and return machine-readable acceptance evidence."""

    reasons: list[dict[str, str]] = []
    if not isinstance(payload, Mapping):
        reasons.append(
            _reason("PUBLIC_SCHEMA_INVALID", "public bundle must be an object")
        )
        return {"status": TOP5_PUBLIC_DELIVERY_BLOCKED, "reasons": reasons}
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError
        checked_now = now.astimezone(timezone.utc)
    except (AttributeError, ValueError):
        reasons.append(
            _reason("PUBLIC_SCHEMA_INVALID", "acceptance time must be timezone-aware")
        )
        return {"status": TOP5_PUBLIC_DELIVERY_BLOCKED, "reasons": reasons}

    if offline_fixture:
        _validate_offline_fixture(payload, reasons)
        status = (
            TOP5_PUBLIC_DELIVERY_READY if not reasons else TOP5_PUBLIC_DELIVERY_BLOCKED
        )
        return {
            "status": status,
            "production_eligible": False,
            "evidence_classification": "TEST_FIXTURE",
            "leagues": list(TOP5_LEAGUES),
            "record_count": len(_top5_records(payload)),
            "reasons": reasons,
            "bundle_digest": _digest(payload),
        }

    try:
        public = serialize_public_product(dict(payload))
    except (
        AssertionError,
        PublicFootballCompatibilityError,
        TypeError,
        ValueError,
    ) as exc:
        reasons.append(_reason("PUBLIC_SCHEMA_INVALID", str(exc)))
        public = {}

    release = public.get("top5_release") if isinstance(public, Mapping) else None
    if not isinstance(release, Mapping):
        reasons.append(_reason("PUBLIC_SCHEMA_INVALID", "top5_release is missing"))
        release = {}
    if release.get("schema_version") != TOP5_RELEASE_SCHEMA:
        reasons.append(
            _reason("PUBLIC_SCHEMA_INVALID", "unsupported Top-5 release schema")
        )
    if release.get("provider_authority") != TOP5_PUBLIC_PROVIDER_AUTHORITY:
        reasons.append(
            _reason(
                "PUBLIC_CANDIDATE_AUTHORITY_LEAK",
                "production public authority must remain the_odds_api",
            )
        )
    if _candidate_provider(release.get("provider_authority")):
        reasons.append(
            _reason(
                "PUBLIC_CANDIDATE_AUTHORITY_LEAK",
                "candidate provider cannot be production authority",
            )
        )
    if (
        release.get("activation_state") != "CONTROLLED"
        or release.get("publication_status") != "PUBLISHED"
    ):
        reasons.append(
            _reason(
                "PUBLIC_SCHEMA_INVALID", "Top-5 release is not controlled and published"
            )
        )
    if (
        release.get("publication_enabled") is not True
        or release.get("no_bet") is not True
    ):
        reasons.append(
            _reason(
                "PUBLIC_SCHEMA_INVALID",
                "Top-5 release must remain explicitly published and no-bet",
            )
        )
    for field in ("source_release_sha", "runtime_data_sha"):
        if not isinstance(release.get(field), str) or not release[field].strip():
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID", f"Top-5 release {field} is missing"
                )
            )
    if release.get("source_runtime_consistent") is not True:
        reasons.append(
            _reason(
                "PUBLIC_PROVENANCE_INVALID",
                "Top-5 release source/runtime consistency is not confirmed",
            )
        )

    try:
        generated_at = _timestamp(
            release.get("generated_at"), "top5_release.generated_at"
        )
        published_at = _timestamp(
            release.get("published_at"), "top5_release.published_at"
        )
        if (
            generated_at > checked_now
            or published_at > checked_now
            or published_at < generated_at
        ):
            reasons.append(
                _reason(
                    "PUBLIC_ARTIFACT_FUTURE",
                    "Top-5 release timestamp is invalid or from the future",
                )
            )
        max_age = release.get("fallback_max_age_seconds")
        if (
            isinstance(max_age, bool)
            or not isinstance(max_age, (int, float))
            or max_age <= 0
        ):
            raise ValueError("fallback_max_age_seconds is invalid")
        if (checked_now - published_at).total_seconds() > float(max_age):
            reasons.append(_reason("PUBLIC_ARTIFACT_STALE", "Top-5 release is stale"))
    except ValueError as exc:
        reasons.append(_reason("PUBLIC_SCHEMA_INVALID", str(exc)))

    codes = release.get("league_codes")
    canonical_codes = (
        tuple(canonical_top5_league(code) for code in codes)
        if isinstance(codes, Sequence) and not isinstance(codes, (str, bytes))
        else ()
    )
    if tuple(sorted(canonical_codes)) != tuple(sorted(TOP5_LEAGUES)) or len(
        canonical_codes
    ) != len(set(canonical_codes)):
        reasons.append(
            _reason(
                "PUBLIC_LEAGUE_INCOMPLETE",
                "Top-5 release must contain the five canonical leagues exactly once",
            )
        )
    if any(
        _candidate_provider(record.get("provider")) for record in _top5_records(public)
    ):
        reasons.append(
            _reason(
                "PUBLIC_CANDIDATE_AUTHORITY_LEAK",
                "candidate provider appears in a public record",
            )
        )

    records = _top5_records(public)
    if len(records) != 15:
        reasons.append(
            _reason(
                "PUBLIC_LEAGUE_INCOMPLETE",
                "published Top-5 bundle must contain exactly 15 records",
            )
        )
    by_league: dict[str, list[dict[str, object]]] = {
        league: [] for league in TOP5_LEAGUES
    }
    for record in records:
        league = canonical_top5_league(record.get("league"))
        if record.get("league") != league:
            reasons.append(
                _reason(
                    "PUBLIC_LEAGUE_NONCANONICAL",
                    "public records must emit canonical league codes",
                )
            )
            continue
        by_league.setdefault(str(league), []).append(record)
    for league, league_records in by_league.items():
        if len(league_records) != 3:
            reasons.append(
                _reason(
                    "PUBLIC_LEAGUE_INCOMPLETE",
                    f"{league} must contain exactly three outcomes",
                )
            )
        fixtures = [
            value.strip() if isinstance(value, str) else ""
            for value in (record.get("fixture_key") for record in league_records)
        ]
        if len(set(fixtures)) != 1 or not fixtures[0]:
            reasons.append(
                _reason(
                    "PUBLIC_BUNDLE_PARTIAL",
                    f"{league} must contain one fixture identity",
                )
            )
        for record in league_records:
            provenance = record.get("provenance")
            if not isinstance(provenance, Mapping):
                reasons.append(
                    _reason(
                        "PUBLIC_PROVENANCE_INVALID",
                        f"{league} record provenance is missing",
                    )
                )
                continue
            if record.get("provider") != TOP5_PUBLIC_PROVIDER_AUTHORITY:
                reasons.append(
                    _reason(
                        "PUBLIC_CANDIDATE_AUTHORITY_LEAK",
                        f"{league} record provider is not production authority",
                    )
                )
            if record.get("activation_id") != release.get(
                "activation_id"
            ) or provenance.get("activation_id") != release.get("activation_id"):
                reasons.append(
                    _reason(
                        "PUBLIC_PROVENANCE_INVALID",
                        f"{league} activation binding is invalid",
                    )
                )
            if provenance.get("evidence_digest") != record.get("evidence_digest"):
                reasons.append(
                    _reason(
                        "PUBLIC_PROVENANCE_INVALID",
                        f"{league} evidence digest binding is invalid",
                    )
                )
            if record.get("synthetic") is True or str(
                record.get("evidence_kind", "")
            ).upper() in {"SYNTHETIC", "TEST_FIXTURE"}:
                reasons.append(
                    _reason(
                        "PUBLIC_TEST_DATA_REJECTED",
                        "synthetic/test evidence cannot satisfy production public readiness",
                    )
                )
            source_timestamp = record.get("signal_timestamp") or record.get(
                "prediction_timestamp"
            )
            try:
                observed_at = _timestamp(source_timestamp, f"{league}.signal_timestamp")
                if observed_at > checked_now:
                    reasons.append(
                        _reason(
                            "PUBLIC_ARTIFACT_FUTURE",
                            f"{league} signal timestamp is from the future",
                        )
                    )
                max_age = release.get("fallback_max_age_seconds")
                if isinstance(max_age, (int, float)) and (
                    checked_now - observed_at
                ).total_seconds() > float(max_age):
                    reasons.append(
                        _reason("PUBLIC_ARTIFACT_STALE", f"{league} signal is stale")
                    )
            except ValueError as exc:
                reasons.append(_reason("PUBLIC_PROVENANCE_INVALID", str(exc)))
            if str(record.get("stale_state", "")).upper() == "STALE":
                reasons.append(
                    _reason("PUBLIC_ARTIFACT_STALE", f"{league} signal is marked stale")
                )

    if (
        expected_source_release_sha is not None
        and release.get("source_release_sha") != expected_source_release_sha
    ):
        reasons.append(
            _reason(
                "PUBLIC_PROVENANCE_INVALID",
                "source release SHA does not match expected acceptance",
            )
        )
    if (
        expected_runtime_data_sha is not None
        and release.get("runtime_data_sha") != expected_runtime_data_sha
    ):
        reasons.append(
            _reason(
                "PUBLIC_PROVENANCE_INVALID",
                "runtime data SHA does not match expected acceptance",
            )
        )
    if expected_source_release_sha is not None and not release.get(
        "source_release_sha"
    ):
        reasons.append(
            _reason("PUBLIC_PROVENANCE_INVALID", "source release SHA is missing")
        )
    if expected_runtime_data_sha is not None and not release.get("runtime_data_sha"):
        reasons.append(
            _reason("PUBLIC_PROVENANCE_INVALID", "runtime data SHA is missing")
        )

    status = TOP5_PUBLIC_DELIVERY_READY if not reasons else TOP5_PUBLIC_DELIVERY_BLOCKED
    return {
        "status": status,
        "production_eligible": not reasons,
        "evidence_classification": "REAL_OBSERVED" if not reasons else "UNACCEPTED",
        "leagues": list(TOP5_LEAGUES),
        "record_count": len(records),
        "generation_id": release.get("generation_id"),
        "activation_id": release.get("activation_id"),
        "provider_authority": release.get("provider_authority"),
        "source_release_sha": release.get("source_release_sha"),
        "runtime_data_sha": release.get("runtime_data_sha"),
        "source_runtime_consistent": release.get("source_runtime_consistent"),
        "bundle_digest": _digest(public) if public else None,
        "reasons": reasons,
    }


def publication_precheck(
    payload: Mapping[str, object],
    accepted_evidence: Mapping[str, object],
    *,
    now: datetime,
    delivery_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate B1 prepublication acceptance plus a zero-mutation dry run."""
    reasons: list[dict[str, str]] = []
    artifact_result: dict[str, object] = {
        "status": TOP5_PREPUBLICATION_DELIVERY_BLOCKED,
        "reasons": [],
    }
    artifact = None
    try:
        artifact = Top5PrepublicationArtifactV1.from_mapping(payload)
        artifact_result = {
            "status": TOP5_PREPUBLICATION_DELIVERY_READY,
            **artifact.validate(now=now),
        }
    except (TypeError, ValueError) as exc:
        reasons.append(_reason("PUBLIC_PREPUBLICATION_INVALID", str(exc)))

    from src.football.top5_final_acceptance import (
        FINAL_ACCEPTANCE_SCHEMA_VERSION,
        STATUS_VERIFIED,
        canonical_digest,
    )

    manifest_fields = {
        "schema_version",
        "source_main_sha",
        "provider_authority",
        "evidence_provider",
        "candidate_provider",
        "leagues",
        "research_sha",
        "model_identity",
        "b4_proof_id",
        "b4_proof_evidence_digest",
        "b4_headroom_digest",
        "b4_dossier_digest",
        "discovery_event_ids",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "adapter_source_sha",
        "controlled_shadow_digest",
        "capture_digests",
        "source_release_sha",
        "runtime_data_sha",
        "model_artifact_hash",
        "signal_time_contract_id",
        "public_prepublication_id",
        "public_product_digest",
        "worker_candidate_payload_digest",
        "static_candidate_payload_digest",
        "checks",
        "readiness",
        "manifest_digest",
    }
    check_fields = {
        "five_leagues",
        "b4_quota_proof",
        "discovery",
        "controlled_shadow",
        "model_signal_time",
        "public_prepublication_delivery",
        "runtime_provenance",
        "candidate_not_authority",
        "no_bet",
    }
    manifest = accepted_evidence.get("manifest") if isinstance(accepted_evidence, Mapping) else None
    manifest_valid = False
    manifest_evidence_provider = None
    if isinstance(manifest, Mapping):
        try:
            from src.football.top5_durable_activation import (
                DurableActivationError,
                validate_activation_manifest_evidence_provider,
            )

            manifest_evidence_provider = validate_activation_manifest_evidence_provider(
                manifest
            )
        except DurableActivationError:
            manifest_evidence_provider = None
    if (
        isinstance(accepted_evidence, Mapping)
        and set(accepted_evidence) == {"status", "manifest"}
        and accepted_evidence.get("status") == "TOP5_FINAL_ACCEPTANCE_VERIFIED"
        and isinstance(manifest, Mapping)
        and set(manifest) == manifest_fields
    ):
        manifest_body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
        checks = manifest.get("checks")
        checks_valid = (
            isinstance(checks, Mapping)
            and set(checks) == check_fields
            and all(value is True for value in checks.values())
        )
        try:
            digest_valid = manifest.get("manifest_digest") == canonical_digest(manifest_body)
        except ValueError:
            digest_valid = False
        manifest_valid = (
            digest_valid
            and manifest.get("schema_version") == FINAL_ACCEPTANCE_SCHEMA_VERSION
            and manifest.get("readiness") == STATUS_VERIFIED
            and manifest.get("provider_authority") == TOP5_PUBLIC_PROVIDER_AUTHORITY
            and manifest_evidence_provider is not None
            and manifest.get("candidate_provider") == manifest_evidence_provider
            and manifest.get("leagues") == sorted(TOP5_LEAGUES)
            and checks_valid
            and artifact is not None
            and artifact_result.get("status") == TOP5_PREPUBLICATION_DELIVERY_READY
            and manifest.get("public_prepublication_id") == artifact_result.get("prepublication_id")
            and manifest.get("controlled_shadow_run_id") == artifact_result.get("run_id")
            and manifest.get("qualification_session_id") == artifact_result.get("session_id")
            and manifest.get("provider_authority") == artifact_result.get("provider_authority")
            and manifest.get("model_identity") == artifact_result.get("candidate_id")
            and manifest.get("research_sha") == artifact_result.get("research_sha")
            and manifest.get("public_product_digest") == artifact_result.get("public_product_digest")
            and manifest.get("worker_candidate_payload_digest") == artifact_result.get("worker_candidate_payload_digest")
            and manifest.get("static_candidate_payload_digest") == artifact_result.get("static_candidate_payload_digest")
            and manifest.get("source_release_sha") == artifact_result.get("source_release_sha")
            and manifest.get("runtime_data_sha") == artifact_result.get("runtime_data_sha")
            and manifest.get("model_artifact_hash") == artifact_result.get("model_artifact_hash")
            and manifest.get("signal_time_contract_id") == artifact_result.get("signal_time_contract_id")
        )
    if not manifest_valid:
        reasons.append(
            _reason(
                "PUBLIC_ACCEPTANCE_EVIDENCE_INVALID",
                "verified Builder 1 prepublication acceptance is required",
            )
        )

    if delivery_manifest is None:
        reasons.append(
            _reason(
                "PUBLIC_DELIVERY_MANIFEST_MISSING",
                "zero-mutation delivery dry-run manifest is required",
            )
        )
    else:
        expected_dry_run_fields = {
            "schema_version", "status", "prepublication_id", "prepared_at",
            "public_product_digest", "worker_candidate_payload_digest",
            "static_candidate_payload_digest", "worker_candidate_payloads_equal",
            "worker_destination", "static_destination", "provider_requests",
            "network_requests", "publication_enabled", "publication_authorized",
            "capability_consumed", "mutation_performed", "rollback_ready",
        }
        if set(delivery_manifest) != expected_dry_run_fields:
            reasons.append(_reason("PUBLIC_DRY_RUN_INVALID", "dry-run manifest fields are invalid"))
        elif artifact is None:
            reasons.append(_reason("PUBLIC_DRY_RUN_INVALID", "dry-run has no valid prepublication artifact"))
        else:
            expected_dry_run = artifact.delivery_dry_run_manifest(
                rollback_ready=delivery_manifest.get("rollback_ready") is True
            )
            if dict(delivery_manifest) != expected_dry_run or delivery_manifest.get("rollback_ready") is not True:
                reasons.append(
                    _reason(
                        "PUBLIC_DRY_RUN_INVALID",
                        "dry run must prove equal payloads and zero requests, authorization, and mutation",
                    )
                )

    acceptance = {
        **artifact_result,
        "reasons": reasons.copy(),
    }
    status = (
        TOP5_PUBLICATION_PRECHECK_READY
        if not reasons
        else TOP5_PUBLICATION_PRECHECK_BLOCKED
    )
    return {
        "status": status,
        "publication_enabled": False,
        "publication_authorized": False,
        "production_mutation": False,
        "capability_consumed": False,
        "provider_requests": 0,
        "reasons": reasons,
        "acceptance": acceptance,
    }


__all__ = [
    "TOP5_LEAGUES",
    "TOP5_PREPUBLICATION_ARTIFACT_SCHEMA",
    "TOP5_PREPUBLICATION_DELIVERY_BLOCKED",
    "TOP5_PREPUBLICATION_DELIVERY_READY",
    "TOP5_PREPUBLICATION_DRY_RUN_SCHEMA",
    "TOP5_PREPUBLICATION_DRY_RUN_STATUS",
    "TOP5_PUBLICATION_PRECHECK_BLOCKED",
    "TOP5_PUBLICATION_PRECHECK_READY",
    "TOP5_PUBLIC_DELIVERY_BLOCKED",
    "TOP5_PUBLIC_DELIVERY_READY",
    "Top5PrepublicationArtifactV1",
    "publication_precheck",
    "validate_public_bundle",
]
