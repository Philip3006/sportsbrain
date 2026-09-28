"""Read-only composition gate for the final Top-5 acceptance package."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256

from src.football.odds.therundown import THERUNDOWN_PROVIDER_NAME
from src.football.top5_b4_provider_neutral_evidence import (
    ProviderNeutralB4EvidenceError,
    Top5B4ProviderNeutralEvidenceDossierV1,
    canonical_evidence_digest,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID
from src.football.top5_therundown_event_discovery import (
    DISCOVERY_EVIDENCE_SCHEMA_VERSION,
    TheRundownB4QuotaProofV1,
    TheRundownEventDiscoveryEvidenceV1,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownQuotaHeadroomEvidenceV1,
)

FINAL_ACCEPTANCE_SCHEMA_VERSION = "top5-final-acceptance-v2"
STATUS_VERIFIED = "TOP5_FINAL_ACCEPTANCE_VERIFIED"
STATUS_BLOCKED = "TOP5_FINAL_ACCEPTANCE_BLOCKED"
ACTIVE_PROVIDER = "the_odds_api"
CANDIDATE_PROVIDER = THERUNDOWN_PROVIDER_NAME
TOP5_LEAGUES = frozenset({"EPL", "BL1", "LL", "SA", "L1"})
MAX_EVIDENCE_AGE_SECONDS = 900
MAX_SHADOW_REQUESTS = 5
NEUTRAL_B4_DOSSIER_KEY = "b4_provider_neutral_dossier"
_B4_LOGICAL_INPUT_KEYS = (
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
_CANDIDATE_EVIDENCE_PROVIDERS = frozenset({CANDIDATE_PROVIDER, "isports_api"})
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


def _validated_neutral_dossier(
    bundle: Mapping[str, object], *, now: datetime, source_main_sha: str
) -> tuple[Top5B4ProviderNeutralEvidenceDossierV1, str] | None:
    raw_dossier = bundle.get(NEUTRAL_B4_DOSSIER_KEY)
    if raw_dossier is None:
        return None
    try:
        if isinstance(raw_dossier, Top5B4ProviderNeutralEvidenceDossierV1):
            dossier = raw_dossier
            dossier.validate(now=now)
        else:
            dossier = Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
                raw_dossier, now=now
            )
    except (ProviderNeutralB4EvidenceError, TypeError, ValueError) as exc:
        raise Top5FinalAcceptanceError(
            f"provider-neutral B4 dossier rejected: {exc}"
        ) from exc

    if dossier.source_main_sha != source_main_sha:
        raise Top5FinalAcceptanceError(
            "provider-neutral B4 source-main SHA does not match the B1 handoff"
        )
    try:
        from src.football.top5_durable_activation import (
            validate_activation_evidence_provider,
        )

        evidence_provider = validate_activation_evidence_provider(
            dossier.controlled_shadow.provider_identity
        )
    except Exception as exc:
        raise Top5FinalAcceptanceError(
            f"provider-neutral B4 evidence identity is not activation-compatible: {exc}"
        ) from exc

    if bundle.get("provider_authority") != ACTIVE_PROVIDER:
        raise Top5FinalAcceptanceError(
            "B1 production provider authority must remain the_odds_api"
        )
    if bundle.get("evidence_provider") != evidence_provider:
        raise Top5FinalAcceptanceError(
            "B1 evidence provider identity does not match the validated B4 dossier"
        )
    if bundle.get("candidate_provider") != evidence_provider:
        raise Top5FinalAcceptanceError(
            "B1 candidate_provider must equal evidence_provider"
        )

    try:
        handoff = dossier.b1_evidence_inputs(now=now)
    except (ProviderNeutralB4EvidenceError, TypeError, ValueError) as exc:
        raise Top5FinalAcceptanceError(
            f"provider-neutral B4 logical handoff rejected: {exc}"
        ) from exc
    for key in _B4_LOGICAL_INPUT_KEYS:
        if key not in bundle or _canonical(bundle[key]) != _canonical(handoff[key]):
            raise Top5FinalAcceptanceError(
                f"B1 {key} does not match the validated provider-neutral B4 dossier"
            )
    return dossier, evidence_provider


def _reject_candidate_active_order(
    runtime: Mapping[str, object], *, evidence_provider: str
) -> None:
    order = runtime.get("active_provider_order")
    if not isinstance(order, Sequence) or isinstance(order, (str, bytes)):
        return  # The governed-runtime validator reports the canonical shape error.
    forbidden = _CANDIDATE_EVIDENCE_PROVIDERS | {evidence_provider}
    if any(
        isinstance(provider, str)
        and any(
            provider == candidate or provider.startswith(f"{candidate}:")
            for candidate in forbidden
        )
        for provider in order
    ):
        raise Top5FinalAcceptanceError(
            "candidate provider authority cannot appear in active provider order"
        )


def _validate_model_runtime(raw: Mapping[str, object]) -> dict[str, str]:
    signal_time_contract_id = _text(
        raw.get("signal_time_contract_id"), "model_runtime.signal_time_contract_id"
    )
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
        "signal_time_contract_id": signal_time_contract_id,
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
            _validate_prepublication_public,
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
        neutral = _validated_neutral_dossier(
            bundle, now=now_utc, source_main_sha=source_main_sha
        )
        model = _validate_model_runtime(
            _mapping(bundle.get("model_runtime"), "model_runtime")
        )
        if neutral is not None:
            neutral_dossier, evidence_provider = neutral
            if (
                "provider_authority" in bundle
                and bundle.get("provider_authority") != ACTIVE_PROVIDER
            ):
                raise Top5FinalAcceptanceError(
                    "B1 production provider authority must remain the_odds_api"
                )
            run = neutral_dossier.controlled_shadow
            discovery_by_league = {
                item.league: {
                    "provider_event_id": item.provider_fixture_id,
                    "provider_fixture_id": item.provider_fixture_id,
                    "fixture_key": item.fixture_key,
                }
                for item in run.discovery_evidence
            }
            run_id = run.controlled_shadow_run_id
            session_id = run.qualification_session_id
            authorization_id = run.authorization_id
            adapter_sha = run.adapter_source_sha
            capture_digests = tuple(item.evidence_digest for item in run.captures)
            shadow_digest = canonical_evidence_digest(run.as_payload(now=now_utc))
            # Neutral readiness has no provider-billing proof identifier.
            proof_id = None
            proof_digest = None
            headroom_digest = canonical_digest(bundle["b4_quota_headroom"])
            b4_dossier_digest = neutral_dossier.dossier_digest
        else:
            authority_value = bundle.get("provider_authority", ACTIVE_PROVIDER)
            if authority_value != ACTIVE_PROVIDER:
                raise Top5FinalAcceptanceError(
                    "B1 production provider authority must remain the_odds_api"
                )
            evidence_provider = bundle.get("evidence_provider", CANDIDATE_PROVIDER)
            candidate_provider = bundle.get("candidate_provider", evidence_provider)
            if evidence_provider != CANDIDATE_PROVIDER:
                raise Top5FinalAcceptanceError(
                    "legacy B4 evidence provider identity is unsupported"
                )
            if candidate_provider != evidence_provider:
                raise Top5FinalAcceptanceError(
                    "B1 candidate_provider must equal evidence_provider"
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
            discovery_by_league = {
                str(item["league"]): item for item in discovery_items
            }
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
                raise Top5FinalAcceptanceError(
                    "B4 headroom/run identity binding mismatch"
                )
            proof_id = proof.proof_id
            proof_digest = proof.evidence_digest
            headroom_digest = headroom.evidence_digest
            b4_dossier_digest = bundle.get("b4_dossier_digest")

        runtime_raw = _mapping(bundle.get("runtime_evidence"), "runtime_evidence")
        _reject_candidate_active_order(
            runtime_raw, evidence_provider=str(evidence_provider)
        )
        runtime_data_sha = _validate_runtime(
            runtime_raw,
            now=now_utc,
            model=model,
        )
        public = _validate_prepublication_public(
            _mapping(bundle.get("public"), "public"),
            now=now_utc,
            run_id=run_id,
            session_id=session_id,
            model=model,
            runtime_data_sha=runtime_data_sha,
        )
        manifest_body = {
            "schema_version": FINAL_ACCEPTANCE_SCHEMA_VERSION,
            "source_main_sha": source_main_sha,
            "provider_authority": ACTIVE_PROVIDER,
            "evidence_provider": evidence_provider,
            "candidate_provider": evidence_provider,
            "leagues": sorted(TOP5_LEAGUES),
            "research_sha": model["research_sha"],
            "model_identity": model["candidate_id"],
            "b4_proof_id": proof_id,
            "b4_proof_evidence_digest": proof_digest,
            "b4_headroom_digest": headroom_digest,
            "b4_dossier_digest": b4_dossier_digest,
            "discovery_event_ids": {
                league: discovery_by_league[league].get(
                    "provider_event_id",
                    discovery_by_league[league].get("provider_fixture_id"),
                )
                for league in sorted(TOP5_LEAGUES)
            },
            "controlled_shadow_run_id": run_id,
            "qualification_session_id": session_id,
            "ceo_authorization_id": authorization_id,
            "adapter_source_sha": adapter_sha,
            "controlled_shadow_digest": shadow_digest,
            "capture_digests": list(capture_digests),
            "source_release_sha": model["source_sha"],
            "runtime_data_sha": runtime_data_sha,
            "model_artifact_hash": model["model_artifact_hash"],
            "signal_time_contract_id": model["signal_time_contract_id"],
            "public_prepublication_id": public["prepublication_id"],
            "public_product_digest": public["public_product_digest"],
            "worker_candidate_payload_digest": public[
                "worker_candidate_payload_digest"
            ],
            "static_candidate_payload_digest": public[
                "static_candidate_payload_digest"
            ],
            "checks": {
                "five_leagues": True,
                # Retained for downstream v1 consumers; neutral input means B4 readiness validated.
                "b4_quota_proof": True,
                "discovery": True,
                "controlled_shadow": True,
                "model_signal_time": True,
                "public_prepublication_delivery": True,
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
