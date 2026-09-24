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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEAGUES = {"EPL", "BL1", "LL", "SA", "L1"}
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


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    codes = {str(code).upper() for code in release["league_codes"]}
    if codes != LEAGUES:
        raise DeliveryAcceptanceError(f"top5_release leagues are not exactly Top-5: {codes}")
    return release


def _validate_payload(
    payload: dict[str, Any],
    *,
    source: str,
    expected_provider: str,
    expected_now: datetime,
) -> dict[str, Any]:
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
    if {str(record["league"]).upper() for record in top5_records} != LEAGUES:
        raise DeliveryAcceptanceError(f"{source}: not all five leagues are visible")
    fixtures_by_league: dict[str, set[str]] = {league: set() for league in LEAGUES}
    for record in top5_records:
        league = str(record["league"]).upper()
        fixture = record.get("fixture_key")
        if not isinstance(fixture, str) or not fixture:
            raise DeliveryAcceptanceError(f"{source}: fixture identity is missing")
        fixtures_by_league[league].add(fixture)
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            raise DeliveryAcceptanceError(f"{source}: provenance is missing")
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
            or provenance.get("activation_id") != release["activation_id"]
            or provenance.get("evidence_digest") != record.get("evidence_digest")
        ):
            raise DeliveryAcceptanceError(f"{source}: record/release binding mismatch")
        if record.get("current_odds") != record.get("odds"):
            raise DeliveryAcceptanceError(f"{source}: current odds schema mismatch")
    if any(len(fixtures) != 1 for fixtures in fixtures_by_league.values()):
        raise DeliveryAcceptanceError(f"{source}: mixed fixture generation detected")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-payload", type=Path, required=True)
    parser.add_argument("--static-payload", type=Path, required=True)
    parser.add_argument("--publication-attestation", type=Path, required=True)
    parser.add_argument("--expected-provider", default="the_odds_api")
    parser.add_argument("--worker-status", type=int, default=200)
    parser.add_argument("--pwa-status", type=int, default=200)
    parser.add_argument("--expected-public-product-digest")
    parser.add_argument("--delivery-manifest", type=Path)
    parser.add_argument("--now", help="ISO-8601 verification time; defaults to now")
    args = parser.parse_args(argv)
    try:
        now = _timestamp(args.now, "now") if args.now else datetime.now(timezone.utc)
        result = validate_delivery(
            _read_json(args.worker_payload),
            _read_json(args.static_payload),
            _read_json(args.publication_attestation),
            expected_provider=args.expected_provider,
            worker_status=args.worker_status,
            pwa_status=args.pwa_status,
            now=now,
            expected_public_product_digest=args.expected_public_product_digest,
            delivery_manifest=(
                _read_json(args.delivery_manifest)
                if args.delivery_manifest
                else None
            ),
        )
    except DeliveryAcceptanceError as exc:
        print(json.dumps({"status": STATUS_BLOCKED, "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
