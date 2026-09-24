"""Read-only composition gate for the final Top-5 acceptance package."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256

from src.football.odds.therundown import THERUNDOWN_PROVIDER_NAME
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID
from src.football.top5_therundown_event_discovery import (
    DISCOVERY_EVIDENCE_SCHEMA_VERSION,
    TheRundownB4QuotaProofV1,
    TheRundownEventDiscoveryEvidenceV1,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownQuotaHeadroomEvidenceV1,
)

FINAL_ACCEPTANCE_SCHEMA_VERSION = "top5-final-acceptance-v1"
STATUS_VERIFIED = "TOP5_FINAL_ACCEPTANCE_VERIFIED"
STATUS_BLOCKED = "TOP5_FINAL_ACCEPTANCE_BLOCKED"
ACTIVE_PROVIDER = "the_odds_api"
CANDIDATE_PROVIDER = THERUNDOWN_PROVIDER_NAME
TOP5_LEAGUES = frozenset({"EPL", "BL1", "LL", "SA", "L1"})
MAX_EVIDENCE_AGE_SECONDS = 900
MAX_SHADOW_REQUESTS = 5
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


class Top5FinalAcceptanceError(ValueError):
    """Machine-readable fail-closed acceptance failure."""


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Top5FinalAcceptanceError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Top5FinalAcceptanceError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA_RE.fullmatch(result) is None:
        raise Top5FinalAcceptanceError(f"{name} must be a hexadecimal digest")
    return result.lower()


def _timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Top5FinalAcceptanceError(f"{name} is not ISO-8601") from exc
    else:
        raise Top5FinalAcceptanceError(f"{name} is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5FinalAcceptanceError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return _timestamp(value, "timestamp").isoformat()
    return value


def canonical_digest(value: object) -> str:
    """Return the deterministic digest used by the acceptance manifest."""

    try:
        encoded = json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Top5FinalAcceptanceError(
            "acceptance input is not canonical JSON"
        ) from exc
    return sha256(encoded).hexdigest()


def _exact_leagues(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise Top5FinalAcceptanceError(f"{name} must be a list")
    codes = tuple(str(item).upper() for item in value)
    if len(codes) != len(TOP5_LEAGUES) or set(codes) != TOP5_LEAGUES:
        raise Top5FinalAcceptanceError(
            f"{name} must contain exactly EPL, BL1, LL, SA and L1 once"
        )
    return codes


def _require_false(raw: Mapping[str, object], names: Sequence[str], scope: str) -> None:
    for name in names:
        if raw.get(name) is not False:
            raise Top5FinalAcceptanceError(f"{scope}.{name} must be false")


def _age(now: datetime, captured: datetime, scope: str) -> None:
    if captured > now:
        raise Top5FinalAcceptanceError(f"{scope} is from the future")
    if (now - captured).total_seconds() > MAX_EVIDENCE_AGE_SECONDS:
        raise Top5FinalAcceptanceError(f"{scope} is stale")


def _contains_candidate(value: object, path: str = "public") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).casefold()
            if key_text in {
                "provider",
                "provider_authority",
                "source",
                "selected_provider",
                "active_provider_order",
                "authority",
            }:
                if isinstance(item, str) and item == CANDIDATE_PROVIDER:
                    raise Top5FinalAcceptanceError(
                        f"{path}.{key} exposes the candidate provider as production authority"
                    )
                if (
                    isinstance(item, Sequence)
                    and not isinstance(item, (str, bytes))
                    and CANDIDATE_PROVIDER in item
                ):
                    raise Top5FinalAcceptanceError(
                        f"{path}.{key} exposes the candidate provider as production authority"
                    )
            _contains_candidate(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _contains_candidate(item, f"{path}[{index}]")


def _validate_model_runtime(raw: Mapping[str, object]) -> dict[str, str]:
    _text(raw.get("signal_time_contract_id"), "model_runtime.signal_time_contract_id")
    _text(raw.get("prediction_input_kind"), "model_runtime.prediction_input_kind")
    if (
        raw.get("candidate_id") != M5_CANDIDATE_ID
        or raw.get("model_identity") != M5_CANDIDATE_ID
    ):
        raise Top5FinalAcceptanceError("model runtime is not the frozen M5 candidate")
    if raw.get("research_sha") != FROZEN_RESEARCH_SHA:
        raise Top5FinalAcceptanceError("model runtime research SHA is not frozen")
    if raw.get("prediction_input_kind") != "signal_time":
        raise Top5FinalAcceptanceError("prediction input is not signal-time data")
    if raw.get("closing_used_for_prediction") is not False:
        raise Top5FinalAcceptanceError("closing values cannot be prediction input")
    _require_false(
        raw,
        (
            "production_model_approved",
            "signal_time_approved_for_production",
            "publication_authorized",
        ),
        "model_runtime",
    )
    if raw.get("no_bet") is not True or raw.get("prediction_input_allowed") is not True:
        raise Top5FinalAcceptanceError("model runtime safety contract is invalid")
    return {
        "candidate_id": M5_CANDIDATE_ID,
        "research_sha": FROZEN_RESEARCH_SHA,
        "source_sha": _sha(raw.get("source_sha"), "model_runtime.source_sha"),
        "model_artifact_hash": _sha(
            raw.get("model_artifact_hash"), "model_runtime.model_artifact_hash"
        ),
    }


def _validate_discovery(
    raw_items: object, *, now: datetime
) -> tuple[dict[str, object], ...]:
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise Top5FinalAcceptanceError("discovery evidence must be a list")
    if len(raw_items) != 5:
        raise Top5FinalAcceptanceError(
            "discovery must contain exactly five league records"
        )
    records: list[dict[str, object]] = []
    seen_leagues: set[str] = set()
    seen_events: set[str] = set()
    seen_fixtures: set[str] = set()
    seen_requests: set[str] = set()
    for item in raw_items:
        raw = _mapping(item, "discovery evidence")
        if (
            raw.get("schema_version", DISCOVERY_EVIDENCE_SCHEMA_VERSION)
            != DISCOVERY_EVIDENCE_SCHEMA_VERSION
        ):
            raise Top5FinalAcceptanceError("discovery evidence schema is unsupported")
        try:
            evidence = TheRundownEventDiscoveryEvidenceV1(
                discovery_authorization_id=_text(
                    raw.get("discovery_authorization_id"), "discovery authorization"
                ),
                discovery_authorization_digest=_sha(
                    raw.get("discovery_authorization_digest"),
                    "discovery authorization digest",
                ),
                provider=_text(raw.get("provider"), "discovery provider"),
                league=_text(raw.get("league"), "discovery league"),
                fixture_key=_text(raw.get("fixture_key"), "discovery fixture"),
                home_team=_text(raw.get("home_team"), "discovery home team"),
                away_team=_text(raw.get("away_team"), "discovery away team"),
                home_participant_id=_text(
                    raw.get("home_participant_id"), "discovery home participant"
                ),
                away_participant_id=_text(
                    raw.get("away_participant_id"), "discovery away participant"
                ),
                kickoff=_timestamp(raw.get("kickoff"), "discovery kickoff"),
                provider_event_id=_text(
                    raw.get("provider_event_id"), "discovery event"
                ),
                request_identity=_text(
                    raw.get("request_identity"), "discovery request"
                ),
                request_shape_digest=_sha(
                    raw.get("request_shape_digest"), "discovery request shape"
                ),
                request_started_at=_timestamp(
                    raw.get("request_started_at"), "discovery request start"
                ),
                response_completed_at=_timestamp(
                    raw.get("response_completed_at"), "discovery response completion"
                ),
                raw_response_digest=_sha(
                    raw.get("raw_response_digest"), "discovery raw digest"
                ),
                provider_event_evidence_digest=_sha(
                    raw.get("provider_event_evidence_digest"),
                    "discovery evidence digest",
                ),
                datapoints=raw.get("datapoints"),
                remaining_datapoints=raw.get("remaining_datapoints"),
                retry_count=raw.get("retry_count", 0),
                network_execution=raw.get("network_execution", False),
                qualification_eligible=raw.get("qualification_eligible", False),
                receipt_eligible=raw.get("receipt_eligible", False),
                provider_authority=raw.get("provider_authority", False),
                activation_authorized=raw.get("activation_authorized", False),
                publication_authorized=raw.get("publication_authorized", False),
                ledger_mutated=raw.get("ledger_mutated", False),
                monetary_spend_authorized=raw.get("monetary_spend_authorized", False),
            )
            evidence.validate()
        except Exception as exc:
            raise Top5FinalAcceptanceError(
                f"discovery evidence rejected: {exc}"
            ) from exc
        _age(now, evidence.response_completed_at, f"discovery {evidence.league}")
        if (
            evidence.provider != CANDIDATE_PROVIDER
            or evidence.network_execution is not True
        ):
            raise Top5FinalAcceptanceError("discovery must be real candidate evidence")
        if (
            evidence.provider_event_evidence_digest
            != evidence.computed_provider_event_evidence_digest
        ):
            raise Top5FinalAcceptanceError("discovery evidence digest mismatch")
        if (
            evidence.league in seen_leagues
            or evidence.provider_event_id in seen_events
            or evidence.fixture_key in seen_fixtures
            or evidence.request_identity in seen_requests
        ):
            raise Top5FinalAcceptanceError(
                "discovery contains duplicate league/event/fixture/request identity"
            )
        seen_leagues.add(evidence.league)
        seen_events.add(evidence.provider_event_id)
        seen_fixtures.add(evidence.fixture_key)
        seen_requests.add(evidence.request_identity)
        records.append(dict(raw))
    if seen_leagues != TOP5_LEAGUES:
        raise Top5FinalAcceptanceError(
            "discovery does not cover exactly the five Top-5 leagues"
        )
    return tuple(records)


def verify_final_acceptance(
    bundle: Mapping[str, object],
    *,
    now: datetime,
    expected_source_main_sha: str | None = None,
) -> dict[str, object]:
    """Validate one complete offline acceptance bundle and return its manifest."""

    try:
        from src.football.top5_final_acceptance_public import (
            _validate_public,
            _validate_runtime,
        )
        from src.football.top5_final_acceptance_shadow import (
            _validate_controlled_shadow,
        )

        if bundle.get("schema_version") != FINAL_ACCEPTANCE_SCHEMA_VERSION:
            raise Top5FinalAcceptanceError("unsupported final acceptance schema")
        source_main_sha = _sha(bundle.get("source_main_sha"), "source_main_sha")
        if (
            expected_source_main_sha
            and source_main_sha != expected_source_main_sha.lower()
        ):
            raise Top5FinalAcceptanceError(
                "acceptance bundle is not based on current main"
            )
        now_utc = _timestamp(now, "now")
        model = _validate_model_runtime(
            _mapping(bundle.get("model_runtime"), "model_runtime")
        )
        quota_package = _mapping(
            bundle.get("b4_quota_proof_package"), "B4 quota proof package"
        )
        proof = TheRundownB4QuotaProofV1.from_package(quota_package, now=now_utc)
        proof.validate(now=now_utc)
        headroom = TheRundownQuotaHeadroomEvidenceV1.from_payload(
            bundle.get("b4_quota_headroom")
        )
        headroom.validate(expected_provider=CANDIDATE_PROVIDER, now=now_utc)
        discovery_items = _validate_discovery(
            bundle.get("discovery_evidence"), now=now_utc
        )
        native_provenance = bundle.get("provider_native_discovery_provenance")
        if native_provenance is not None:
            from src.football.top5_provider_native_evidence_bridge import (
                ProviderNativeEvidenceBridgeError,
                validate_native_provenance_against_legacy,
            )

            try:
                validate_native_provenance_against_legacy(
                    native_provenance, discovery_items
                )
            except ProviderNativeEvidenceBridgeError as exc:
                raise Top5FinalAcceptanceError(
                    f"provider-native discovery provenance rejected: {exc}"
                ) from exc
        discovery_by_league = {str(item["league"]): item for item in discovery_items}
        (
            run_id,
            session_id,
            authorization_id,
            adapter_sha,
            capture_digests,
            shadow_digest,
        ) = _validate_controlled_shadow(
            _mapping(bundle.get("controlled_shadow"), "controlled_shadow"),
            now=now_utc,
            discovery=discovery_by_league,
        )
        if (
            headroom.controlled_shadow_run_id != run_id
            or headroom.qualification_session_id != session_id
            or headroom.authorization_id != authorization_id
        ):
            raise Top5FinalAcceptanceError("B4 headroom/run identity binding mismatch")
        public = _validate_public(
            _mapping(bundle.get("public"), "public"),
            now=now_utc,
            run_id=run_id,
            session_id=session_id,
            model=model,
        )
        _validate_runtime(
            _mapping(bundle.get("runtime_evidence"), "runtime_evidence"),
            now=now_utc,
            model=model,
        )
        manifest_body = {
            "schema_version": FINAL_ACCEPTANCE_SCHEMA_VERSION,
            "source_main_sha": source_main_sha,
            "provider_authority": ACTIVE_PROVIDER,
            "candidate_provider": CANDIDATE_PROVIDER,
            "leagues": sorted(TOP5_LEAGUES),
            "research_sha": model["research_sha"],
            "model_identity": model["candidate_id"],
            "b4_proof_id": proof.proof_id,
            "b4_proof_evidence_digest": proof.evidence_digest,
            "b4_headroom_digest": headroom.evidence_digest,
            "discovery_event_ids": {
                league: discovery_by_league[league]["provider_event_id"]
                for league in sorted(TOP5_LEAGUES)
            },
            "controlled_shadow_run_id": run_id,
            "qualification_session_id": session_id,
            "ceo_authorization_id": authorization_id,
            "adapter_source_sha": adapter_sha,
            "controlled_shadow_digest": shadow_digest,
            "capture_digests": list(capture_digests),
            "public_generation_id": public["generation_id"],
            "public_activation_id": public["activation_id"],
            "public_product_digest": public["public_product_digest"],
            "checks": {
                "five_leagues": True,
                "b4_quota_proof": True,
                "discovery": True,
                "controlled_shadow": True,
                "model_signal_time": True,
                "public_delivery": True,
                "runtime_provenance": True,
                "candidate_not_authority": True,
                "no_bet": True,
            },
            "readiness": STATUS_VERIFIED,
        }
        return {
            "status": STATUS_VERIFIED,
            "manifest": {
                **manifest_body,
                "manifest_digest": canonical_digest(manifest_body),
            },
        }
    except Top5FinalAcceptanceError:
        raise
    except Exception as exc:
        raise Top5FinalAcceptanceError(str(exc)) from exc


__all__ = [
    "ACTIVE_PROVIDER",
    "CANDIDATE_PROVIDER",
    "FINAL_ACCEPTANCE_SCHEMA_VERSION",
    "STATUS_BLOCKED",
    "STATUS_VERIFIED",
    "Top5FinalAcceptanceError",
    "canonical_digest",
    "verify_final_acceptance",
]
