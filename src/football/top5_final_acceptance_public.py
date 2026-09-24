"""Public delivery and governed-runtime checks for final acceptance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from scripts.top5_publication_delivery_acceptance import validate_delivery
from src.football.top5_final_acceptance import (
    ACTIVE_PROVIDER,
    CANDIDATE_PROVIDER,
    TOP5_LEAGUES,
    Top5FinalAcceptanceError,
    _age,
    _contains_candidate,
    _exact_leagues,
    _mapping,
    _sha,
    _text,
    _timestamp,
)
from src.football.top5_publisher import ControlledPublicationCapability
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID


def _validate_public(
    raw: Mapping[str, object],
    *,
    now: datetime,
    run_id: str,
    session_id: str,
    model: Mapping[str, str],
) -> dict[str, object]:
    worker = _mapping(raw.get("worker_payload"), "public worker payload")
    static = _mapping(raw.get("static_payload"), "public static payload")
    attestation = _mapping(
        raw.get("publication_attestation"), "publication attestation"
    )
    capability = _mapping(raw.get("capability"), "publication capability")
    manifest = _mapping(raw.get("delivery_manifest"), "delivery manifest")
    try:
        capability_object = ControlledPublicationCapability.from_mapping(capability)
        capability_object.validate()
        delivery = validate_delivery(
            dict(worker),
            dict(static),
            dict(attestation),
            expected_provider=ACTIVE_PROVIDER,
            worker_status=raw.get("worker_status", 200),
            pwa_status=raw.get("pwa_status", 200),
            now=now,
            delivery_manifest=dict(manifest),
        )
    except Exception as exc:
        raise Top5FinalAcceptanceError(f"public delivery rejected: {exc}") from exc
    _contains_candidate(
        {"worker": worker, "static": static, "attestation": attestation}, "public"
    )
    release = _mapping(worker.get("top5_release"), "public release")
    codes = _exact_leagues(release.get("league_codes"), "public release league_codes")
    if (
        release.get("provider_authority") != ACTIVE_PROVIDER
        or release.get("controlled_shadow_run_id") != run_id
        or release.get("qualification_session_id") != session_id
    ):
        raise Top5FinalAcceptanceError("public release authority/run binding mismatch")
    records = worker.get("football")
    if not isinstance(records, list):
        raise Top5FinalAcceptanceError("public football records are missing")
    top5 = [
        record
        for record in records
        if isinstance(record, Mapping)
        and str(record.get("league", "")).upper() in TOP5_LEAGUES
    ]
    if len(top5) != 15:
        raise Top5FinalAcceptanceError(
            "public release must contain exactly 15 Top-5 records"
        )
    counts = {league: 0 for league in TOP5_LEAGUES}
    fixtures: dict[str, set[str]] = {league: set() for league in TOP5_LEAGUES}
    for record in top5:
        league = str(record.get("league", "")).upper()
        counts[league] += 1
        fixture = _text(record.get("fixture_key"), "public fixture_key")
        fixtures[league].add(fixture)
        if (
            record.get("provider") != ACTIVE_PROVIDER
            or record.get("source") == CANDIDATE_PROVIDER
            or record.get("run_id") != run_id
            or record.get("session_id") != session_id
            or record.get("no_bet") is not True
            or record.get("publication_enabled") is not True
            or record.get("closing_used_for_prediction") is not False
        ):
            raise Top5FinalAcceptanceError(
                "public record authority/provenance contract is invalid"
            )
        if (
            record.get("model_identity") != M5_CANDIDATE_ID
            or record.get("research_sha") != FROZEN_RESEARCH_SHA
            or record.get("source_sha") != model["source_sha"]
        ):
            raise Top5FinalAcceptanceError("public record model provenance mismatch")
        provenance = _mapping(record.get("provenance"), "public record provenance")
        if (
            provenance.get("provider") != ACTIVE_PROVIDER
            or provenance.get("source_sha") != model["source_sha"]
            or provenance.get("research_sha") != FROZEN_RESEARCH_SHA
        ):
            raise Top5FinalAcceptanceError("public record provenance mismatch")
    if any(count != 3 for count in counts.values()) or any(
        len(values) != 1 for values in fixtures.values()
    ):
        raise Top5FinalAcceptanceError(
            "public release must contain three records for one fixture per league"
        )
    if set(codes) != TOP5_LEAGUES:
        raise Top5FinalAcceptanceError("public release league set is incomplete")
    return {
        "generation_id": _text(release.get("generation_id"), "public generation ID"),
        "activation_id": _text(release.get("activation_id"), "public activation ID"),
        "public_product_digest": delivery["public_product_digest"],
        "league_codes": list(codes),
        "worker_status": raw.get("worker_status", 200),
        "pwa_status": raw.get("pwa_status", 200),
        "capability_id": capability_object.capability_id,
    }


def _validate_runtime(
    raw: Mapping[str, object], *, now: datetime, model: Mapping[str, str]
) -> None:
    root = _text(raw.get("runtime_root"), "runtime_evidence.runtime_root")
    if not root.startswith("/") or raw.get("runtime_root_role") != "governed-runtime":
        raise Top5FinalAcceptanceError(
            "runtime evidence root is not the governed runtime"
        )
    if raw.get("checkout_clean") is not True or raw.get("publisher_clean") is not True:
        raise Top5FinalAcceptanceError("runtime checkout/publisher is not clean")
    if raw.get("health_authority") not in {"local", "governed"} or raw.get(
        "health_status"
    ) not in {"ok", "degraded"}:
        raise Top5FinalAcceptanceError("runtime health evidence is invalid")
    order = raw.get("active_provider_order")
    if (
        not isinstance(order, Sequence)
        or ACTIVE_PROVIDER not in order
        or CANDIDATE_PROVIDER in order
    ):
        raise Top5FinalAcceptanceError("runtime provider authority evidence is invalid")
    if raw.get("source_release_sha") != model["source_sha"]:
        raise Top5FinalAcceptanceError(
            "runtime source release does not match model source"
        )
    _sha(raw.get("runtime_data_sha"), "runtime_evidence.runtime_data_sha")
    _age(
        now,
        _timestamp(raw.get("captured_at"), "runtime_evidence.captured_at"),
        "runtime evidence",
    )
