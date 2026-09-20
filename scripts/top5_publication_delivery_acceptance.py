#!/usr/bin/env python3
"""Read-only post-publication acceptance check for the Top-5 delivery path.

The command consumes captured Worker and static JSON responses.  It performs
no network request and writes no runtime, scheduler, ledger, Cloudflare, or
publication state.  A caller must provide the separate publication attestation
created by the already-governed publication gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEAGUES = {"EPL", "BL1", "LL", "SA", "L1"}
ACTIVE_PROVIDER = "the_odds_api"
CANDIDATE_PROVIDER = "therundown_experimental"
STATUS_VERIFIED = "TOP5_DELIVERY_VERIFIED"
STATUS_BLOCKED = "TOP5_DELIVERY_BLOCKED"


class DeliveryAcceptanceError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryAcceptanceError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DeliveryAcceptanceError(f"payload is not an object: {path}")
    return value


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAcceptanceError(f"{field} is missing")
    return value.strip()


def _exact_leagues(value: object, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DeliveryAcceptanceError(f"{field} must be a list")
    normalized = [
        code.strip().upper() if isinstance(code, str) else ""
        for code in value
    ]
    if (
        len(normalized) != len(LEAGUES)
        or len(set(normalized)) != len(normalized)
        or set(normalized) != LEAGUES
    ):
        raise DeliveryAcceptanceError(
            f"{field} must contain EPL, BL1, LL, SA, and L1 exactly once"
        )
    return normalized


def _assert_no_candidate_provider(value: object, *, path: str = "") -> None:
    provider_keys = {
        "provider",
        "provider_authority",
        "result_authority",
        "source",
        "selected_provider",
        "active_provider_order",
        "observed_providers",
        "fallback_provider",
        "allowed_fallback_providers",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in provider_keys:
                values = child if isinstance(child, Sequence) and not isinstance(child, (str, bytes)) else (child,)
                if any(
                    isinstance(item, str)
                    and item.casefold() == CANDIDATE_PROVIDER.casefold()
                    for item in values
                ):
                    raise DeliveryAcceptanceError(
                        f"candidate provider is present in production authority: {child_path}"
                    )
            _assert_no_candidate_provider(child, path=child_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _assert_no_candidate_provider(child, path=f"{path}[{index}]")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAcceptanceError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryAcceptanceError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise DeliveryAcceptanceError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _release(payload: dict[str, Any]) -> dict[str, Any]:
    release = payload.get("top5_release")
    if not isinstance(release, dict):
        raise DeliveryAcceptanceError("top5_release is missing")
    required = (
        "schema_version",
        "generation_id",
        "activation_state",
        "activation_id",
        "publication_status",
        "publication_enabled",
        "publication_authorization_id",
        "provider_authority",
        "result_authority",
        "candidate_id",
        "model_identity",
        "evidence_digest",
        "evidence_digests",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "league_codes",
        "generated_at",
        "published_at",
        "fallback_max_age_seconds",
        "no_bet",
    )
    missing = [key for key in required if not release.get(key)]
    if missing:
        raise DeliveryAcceptanceError(
            "top5_release missing: " + ", ".join(missing)
        )
    if (
        release["schema_version"] != "top5-public-release-v1"
        or release["activation_state"] != "CONTROLLED"
        or release["publication_status"] != "PUBLISHED"
        or release["publication_enabled"] is not True
        or release["no_bet"] is not True
    ):
        raise DeliveryAcceptanceError("top5_release is not a published controlled no-bet release")
    _exact_leagues(release["league_codes"], "top5_release.league_codes")
    evidence_digests = release["evidence_digests"]
    if not isinstance(evidence_digests, Mapping) or set(evidence_digests) != LEAGUES:
        raise DeliveryAcceptanceError("top5_release evidence_digests must cover Top-5 exactly")
    for league, digest in evidence_digests.items():
        _required_text(digest, f"top5_release.evidence_digests[{league}]")
    _assert_no_candidate_provider(release)
    return release


def _validate_payload(
    payload: dict[str, Any],
    *,
    source: str,
    expected_provider: str,
    expected_now: datetime,
    require_health_provenance: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise DeliveryAcceptanceError(f"{source}: payload must be an object")
    release = _release(payload)
    if release["provider_authority"] != expected_provider:
        raise DeliveryAcceptanceError(
            f"{source}: unexpected provider authority {release['provider_authority']!r}"
        )
    generated_at = _timestamp(release["generated_at"], f"{source}.generated_at")
    published_at = _timestamp(release["published_at"], f"{source}.published_at")
    max_age = release["fallback_max_age_seconds"]
    if not isinstance(max_age, (int, float)) or max_age <= 0:
        raise DeliveryAcceptanceError(f"{source}: invalid fallback age")
    if (expected_now - published_at).total_seconds() > max_age:
        raise DeliveryAcceptanceError(f"{source}: Top-5 release is stale")
    if generated_at > expected_now or published_at > expected_now:
        raise DeliveryAcceptanceError(f"{source}: Top-5 release is from the future")

    records = payload.get("football")
    if not isinstance(records, list):
        raise DeliveryAcceptanceError(f"{source}: football array is missing")
    top5_records = [
        record
        for record in records
        if isinstance(record, dict)
        and str(record.get("league", "")).upper() in LEAGUES
    ]
    if len(top5_records) != len(LEAGUES) * 3:
        raise DeliveryAcceptanceError(f"{source}: exactly 15 Top-5 records are required")
    by_league: dict[str, list[dict[str, Any]]] = {league: [] for league in LEAGUES}
    for record in top5_records:
        by_league[str(record["league"]).upper()].append(record)
    if set(by_league) != LEAGUES or any(len(items) != 3 for items in by_league.values()):
        raise DeliveryAcceptanceError(f"{source}: exactly three records per league are required")
    fixtures_by_league: dict[str, set[str]] = {league: set() for league in LEAGUES}
    shared_bindings: dict[str, str] | None = None
    for record in top5_records:
        league = str(record["league"]).upper()
        fixture = record.get("fixture_key")
        if not isinstance(fixture, str) or not fixture:
            raise DeliveryAcceptanceError(f"{source}: fixture identity is missing")
        fixtures_by_league[league].add(fixture)
        provenance = record.get("provenance")
        if not isinstance(provenance, Mapping):
            raise DeliveryAcceptanceError(f"{source}: provenance is missing")
        for field in (
            "source",
            "provider",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "snapshot_id",
            "snapshot_kind",
            "captured_at",
            "activation_id",
            "evidence_digest",
            "controlled_shadow_run_id",
            "qualification_session_id",
        ):
            _required_text(provenance.get(field), f"{source}.provenance.{field}")
        _timestamp(provenance["captured_at"], f"{source}.provenance.captured_at")
        _assert_no_candidate_provider(record)
        if (
            record.get("sport") != "football"
            or record.get("activation_state") != "CONTROLLED"
            or record.get("signal_status") != "CONTROLLED"
            or record.get("publication_status") != "PUBLISHED"
            or record.get("publication_enabled") is not True
            or record.get("no_bet") is not True
            or record.get("activation_id") != release["activation_id"]
            or record.get("provider") != expected_provider
            or record.get("run_id") != release.get("controlled_shadow_run_id")
            or record.get("session_id") != release.get("qualification_session_id")
            or record.get("model_identity") != release.get("model_identity")
            or provenance.get("source") != expected_provider
            or provenance.get("provider") != expected_provider
            or provenance.get("activation_id") != release["activation_id"]
            or provenance.get("evidence_digest") != record.get("evidence_digest")
            or provenance.get("controlled_shadow_run_id")
            != release.get("controlled_shadow_run_id")
            or provenance.get("qualification_session_id")
            != release.get("qualification_session_id")
            or provenance.get("snapshot_kind") != "signal_time"
            or provenance.get("evidence_digest")
            != release["evidence_digests"].get(league)
        ):
            raise DeliveryAcceptanceError(f"{source}: record/release binding mismatch")
        if record.get("current_odds") != record.get("odds"):
            raise DeliveryAcceptanceError(f"{source}: current odds schema mismatch")
        bindings = {
            "source_sha": str(provenance["source_sha"]),
            "research_sha": str(provenance["research_sha"]),
            "model_artifact_hash": str(provenance["model_artifact_hash"]),
            "model_identity": str(record["model_identity"]),
            "activation_id": str(record["activation_id"]),
            "controlled_shadow_run_id": str(record["run_id"]),
            "qualification_session_id": str(record["session_id"]),
        }
        if shared_bindings is None:
            shared_bindings = bindings
        elif bindings != shared_bindings:
            raise DeliveryAcceptanceError(f"{source}: record provenance binding drift")
    if any(len(fixtures) != 1 for fixtures in fixtures_by_league.values()):
        raise DeliveryAcceptanceError(f"{source}: mixed fixture generation detected")
    if require_health_provenance:
        health = payload.get("health")
        if not isinstance(health, Mapping):
            raise DeliveryAcceptanceError(f"{source}: governed health provenance is missing")
        health_bindings = {
            "activation_id": health.get("top5_activation_id"),
            "candidate_id": health.get("top5_candidate_id"),
            "model_identity": health.get("top5_model_identity"),
            "source_sha": health.get("top5_source_sha"),
            "research_sha": health.get("top5_research_sha"),
            "model_artifact_hash": health.get("top5_model_artifact_hash"),
            "provider_authority": health.get("top5_provider_authority"),
            "result_authority": health.get("top5_result_authority"),
            "evidence_digests": health.get("top5_evidence_digests"),
        }
        if any(
            not isinstance(value, (Mapping, str)) or value in (None, "")
            for value in health_bindings.values()
        ):
            raise DeliveryAcceptanceError(f"{source}: health provenance is incomplete")
        if (
            health_bindings["activation_id"] != release["activation_id"]
            or health_bindings["candidate_id"] != release["candidate_id"]
            or health_bindings["model_identity"] != release["model_identity"]
            or health_bindings["provider_authority"] != expected_provider
            or health_bindings["result_authority"] != release["result_authority"]
            or health_bindings["evidence_digests"] != release["evidence_digests"]
            or shared_bindings is None
            or health_bindings["source_sha"] != shared_bindings["source_sha"]
            or health_bindings["research_sha"] != shared_bindings["research_sha"]
            or health_bindings["model_artifact_hash"]
            != shared_bindings["model_artifact_hash"]
        ):
            raise DeliveryAcceptanceError(f"{source}: health/release provenance mismatch")
        if health.get("top5_leagues") is not None:
            _exact_leagues(health["top5_leagues"], f"{source}.health.top5_leagues")
        if health.get("activation_state") not in (None, "controlled"):
            raise DeliveryAcceptanceError(f"{source}: health activation state mismatch")
        if health.get("publication_status") not in (None, "PUBLISHED"):
            raise DeliveryAcceptanceError(f"{source}: health publication state mismatch")
        health_no_bet = health.get("no_bet", release["no_bet"])
        if health.get("publication_enabled") is not True or health_no_bet is not True:
            raise DeliveryAcceptanceError(f"{source}: health safety state mismatch")
    return release



def validate_delivery(
    worker_payload: dict[str, Any],
    static_payload: dict[str, Any],
    attestation: dict[str, Any],
    *,
    expected_provider: str = "the_odds_api",
    worker_status: int = 200,
    pwa_status: int = 200,
    now: datetime | None = None,
    expected_public_product_digest: str | None = None,
    delivery_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if expected_provider != ACTIVE_PROVIDER:
        raise DeliveryAcceptanceError(
            f"production acceptance requires provider authority {ACTIVE_PROVIDER!r}"
        )
    if worker_status != 200 or pwa_status != 200:
        raise DeliveryAcceptanceError("Worker and PWA must both return HTTP 200")
    if attestation.get("publication_authorized") is not True:
        raise DeliveryAcceptanceError("separate publication authorization is missing")
    expected_now = now or datetime.now(timezone.utc)
    worker_release = _validate_payload(
        worker_payload,
        source="worker",
        expected_provider=expected_provider,
        expected_now=expected_now,
    )
    static_release = _validate_payload(
        static_payload,
        source="static",
        expected_provider=expected_provider,
        expected_now=expected_now,
    )
    for key in ("generation_id", "activation_id", "provider_authority"):
        if worker_release.get(key) != static_release.get(key):
            raise DeliveryAcceptanceError(f"Worker/static {key} mismatch")
        if attestation.get(key) != worker_release.get(key):
            raise DeliveryAcceptanceError(f"attestation {key} mismatch")
    worker_digest = _canonical_digest(worker_payload)
    static_digest = _canonical_digest(static_payload)
    if worker_digest != static_digest:
        raise DeliveryAcceptanceError("Worker/static public product digest mismatch")
    if expected_public_product_digest and worker_digest != expected_public_product_digest:
        raise DeliveryAcceptanceError("public product digest does not match delivery plan")
    if delivery_manifest is not None:
        if delivery_manifest.get("public_product_digest") != worker_digest:
            raise DeliveryAcceptanceError("delivery manifest public product digest mismatch")
        if delivery_manifest.get("static_payload_digest") != worker_digest:
            raise DeliveryAcceptanceError("delivery manifest static payload mismatch")
        if delivery_manifest.get("worker_payload_digest") != worker_digest:
            raise DeliveryAcceptanceError("delivery manifest Worker payload mismatch")
        if delivery_manifest.get("generation_id") != worker_release["generation_id"]:
            raise DeliveryAcceptanceError("delivery manifest generation mismatch")
        if delivery_manifest.get("activation_id") != worker_release["activation_id"]:
            raise DeliveryAcceptanceError("delivery manifest activation mismatch")
    return {
        "status": STATUS_VERIFIED,
        "generation_id": worker_release["generation_id"],
        "activation_id": worker_release["activation_id"],
        "provider_authority": worker_release["provider_authority"],
        "leagues": sorted(LEAGUES),
        "worker_status": worker_status,
        "pwa_status": pwa_status,
        "publication_authorized": True,
        "public_product_digest": worker_digest,
    }


def validate_production_acceptance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Keep the CLI/API seam while loading the strict evaluator on demand."""
    from src.football.top5_production_acceptance import (
        validate_production_acceptance as _validate_production_acceptance,
    )

    return _validate_production_acceptance(*args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-payload", type=Path, required=True)
    parser.add_argument("--static-payload", type=Path, required=True)
    parser.add_argument("--publication-attestation", type=Path, required=True)
    parser.add_argument("--capability-token", type=Path, required=True)
    parser.add_argument("--runtime-evidence", type=Path, required=True)
    parser.add_argument("--expected-runtime-root", required=True)
    parser.add_argument("--expected-provider", default=ACTIVE_PROVIDER)
    parser.add_argument("--worker-status", type=int, required=True)
    parser.add_argument("--pwa-status", type=int, required=True)
    parser.add_argument("--delivery-manifest", type=Path, required=True)
    parser.add_argument("--max-runtime-age-seconds", type=float, default=900)
    parser.add_argument("--now", help="ISO-8601 verification time; defaults to now")
    args = parser.parse_args(argv)
    try:
        now = _timestamp(args.now, "now") if args.now else datetime.now(timezone.utc)
        if args.expected_provider != ACTIVE_PROVIDER:
            raise DeliveryAcceptanceError(
                f"production acceptance requires provider authority {ACTIVE_PROVIDER!r}"
            )
        result = validate_production_acceptance(
            _read_json(args.worker_payload),
            _read_json(args.static_payload),
            _read_json(args.publication_attestation),
            _read_json(args.capability_token),
            _read_json(args.runtime_evidence),
            expected_runtime_root=args.expected_runtime_root,
            worker_status=args.worker_status,
            pwa_status=args.pwa_status,
            now=now,
            delivery_manifest=_read_json(args.delivery_manifest),
            max_runtime_age_seconds=args.max_runtime_age_seconds,
        )
    except DeliveryAcceptanceError as exc:
        print(json.dumps({"status": STATUS_BLOCKED, "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
