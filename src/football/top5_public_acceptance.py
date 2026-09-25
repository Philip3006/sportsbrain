"""Side-effect-free Top-5 public-read acceptance and publication precheck.

This module is the B3 boundary after Builder 1 acceptance and before any
production publication.  It validates a captured/generated public product;
it does not fetch providers, activate runtime state, write a publication
target, deploy a Worker, or mutate betting/ledger state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from src.notifications.public_serializer import (
    TOP5_PUBLIC_PROVIDER_AUTHORITY,
    PublicFootballCompatibilityError,
    canonical_top5_league,
    serialize_public_product,
)

TOP5_PUBLIC_DELIVERY_READY = "TOP5_PUBLIC_DELIVERY_READY"
TOP5_PUBLIC_DELIVERY_BLOCKED = "TOP5_PUBLIC_DELIVERY_BLOCKED"
TOP5_PUBLICATION_PRECHECK_READY = "TOP5_PUBLICATION_PRECHECK_READY"
TOP5_PUBLICATION_PRECHECK_BLOCKED = "TOP5_PUBLICATION_PRECHECK_BLOCKED"
TOP5_LEAGUES = ("EPL", "BL1", "LL", "SA", "L1")
TOP5_RELEASE_SCHEMA = "top5-public-release-v1"
_TOP5_LEAGUE_SET = frozenset(TOP5_LEAGUES)
_CANDIDATE_PROVIDER_MARKERS = frozenset(
    {"therundown", "therundown_experimental", "candidate", "shadow"}
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is malformed") from exc
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
    """Bind public delivery to Builder 1 acceptance without duplicating it."""

    acceptance = validate_public_bundle(payload, now=now)
    reasons: list[dict[str, str]] = []
    if (
        isinstance(accepted_evidence, Mapping)
        and accepted_evidence.get("status") == "TOP5_FINAL_ACCEPTANCE_VERIFIED"
    ):
        from src.football.top5_final_acceptance import (
            FINAL_ACCEPTANCE_SCHEMA_VERSION,
            STATUS_VERIFIED,
            canonical_digest,
        )

        manifest = accepted_evidence.get("manifest")
        manifest_fields = {
            "schema_version",
            "source_main_sha",
            "provider_authority",
            "candidate_provider",
            "leagues",
            "research_sha",
            "model_identity",
            "b4_proof_id",
            "b4_proof_evidence_digest",
            "b4_headroom_digest",
            "discovery_event_ids",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_authorization_id",
            "adapter_source_sha",
            "controlled_shadow_digest",
            "capture_digests",
            "public_generation_id",
            "public_activation_id",
            "public_product_digest",
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
            "public_delivery",
            "runtime_provenance",
            "candidate_not_authority",
            "no_bet",
        }
        manifest_valid = False
        if (
            set(accepted_evidence) == {"status", "manifest"}
            and isinstance(manifest, Mapping)
            and set(manifest) == manifest_fields
        ):
            manifest_body = {
                str(key): value
                for key, value in manifest.items()
                if key != "manifest_digest"
            }
            checks = manifest.get("checks")
            checks_valid = (
                isinstance(checks, Mapping)
                and set(checks) == check_fields
                and all(value is True for value in checks.values())
                and checks.get("candidate_not_authority") is True
            )
            manifest_valid = (
                acceptance.get("status") == TOP5_PUBLIC_DELIVERY_READY
                and manifest.get("schema_version") == FINAL_ACCEPTANCE_SCHEMA_VERSION
                and manifest.get("readiness") == STATUS_VERIFIED
                and manifest.get("provider_authority") == TOP5_PUBLIC_PROVIDER_AUTHORITY
                and manifest.get("candidate_provider") == "therundown_experimental"
                and manifest.get("leagues") == sorted(TOP5_LEAGUES)
                and checks_valid
                and manifest.get("manifest_digest") == canonical_digest(manifest_body)
                and manifest.get("public_generation_id")
                == acceptance.get("generation_id")
                and manifest.get("public_activation_id")
                == acceptance.get("activation_id")
                and manifest.get("public_product_digest")
                == acceptance.get("bundle_digest")
            )
        if manifest_valid and isinstance(manifest, Mapping):
            accepted_evidence = {
                "schema_version": FINAL_ACCEPTANCE_SCHEMA_VERSION,
                "status": "ACCEPTED",
                "publication_ready": True,
                "provider_authority": manifest["provider_authority"],
                "generation_id": acceptance["generation_id"],
                "activation_id": acceptance["activation_id"],
                "source_release_sha": acceptance["source_release_sha"],
                "runtime_data_sha": acceptance["runtime_data_sha"],
                "evidence_digest": manifest["manifest_digest"],
            }
        else:
            accepted_evidence = {
                "schema_version": FINAL_ACCEPTANCE_SCHEMA_VERSION,
                "status": "INVALID_B1_ACCEPTANCE",
                "publication_ready": False,
            }
    required = {
        "schema_version": "top5-final-acceptance-v1",
        "status": "ACCEPTED",
        "publication_ready": True,
        "provider_authority": TOP5_PUBLIC_PROVIDER_AUTHORITY,
    }
    accepted_shape = isinstance(accepted_evidence, Mapping)
    if not accepted_shape:
        reasons.append(
            _reason(
                "PUBLIC_ACCEPTANCE_EVIDENCE_MISSING",
                "Builder 1 acceptance manifest is missing",
            )
        )
    else:
        for key, expected in required.items():
            if accepted_evidence.get(key) != expected:
                reasons.append(
                    _reason(
                        "PUBLIC_ACCEPTANCE_EVIDENCE_INVALID",
                        f"Builder 1 acceptance field {key} is not accepted",
                    )
                )
        for key in (
            "generation_id",
            "activation_id",
            "source_release_sha",
            "runtime_data_sha",
            "evidence_digest",
        ):
            if (
                not isinstance(accepted_evidence.get(key), str)
                or not accepted_evidence[key]
            ):
                reasons.append(
                    _reason(
                        "PUBLIC_ACCEPTANCE_EVIDENCE_INVALID",
                        f"Builder 1 acceptance field {key} is missing",
                    )
                )
        if (
            isinstance(accepted_evidence.get("source_release_sha"), str)
            and accepted_evidence.get("source_release_sha")
            and isinstance(accepted_evidence.get("runtime_data_sha"), str)
            and accepted_evidence.get("runtime_data_sha")
        ):
            acceptance = validate_public_bundle(
                payload,
                now=now,
                expected_source_release_sha=accepted_evidence["source_release_sha"],
                expected_runtime_data_sha=accepted_evidence["runtime_data_sha"],
            )
        if acceptance.get("generation_id") and accepted_evidence.get(
            "generation_id"
        ) != acceptance.get("generation_id"):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "Builder 1 generation binding does not match public bundle",
                )
            )
        if acceptance.get("activation_id") and accepted_evidence.get(
            "activation_id"
        ) != acceptance.get("activation_id"):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "Builder 1 activation binding does not match public bundle",
                )
            )
        if acceptance.get("source_release_sha") and accepted_evidence.get(
            "source_release_sha"
        ) != acceptance.get("source_release_sha"):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "Builder 1 source release binding does not match public bundle",
                )
            )
        if acceptance.get("runtime_data_sha") and accepted_evidence.get(
            "runtime_data_sha"
        ) != acceptance.get("runtime_data_sha"):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "Builder 1 runtime data binding does not match public bundle",
                )
            )
    if delivery_manifest is None:
        reasons.append(
            _reason(
                "PUBLIC_DELIVERY_MANIFEST_MISSING",
                "dry-run delivery manifest is required for publication precheck",
            )
        )
    else:
        if delivery_manifest.get("public_product_digest") != acceptance.get(
            "bundle_digest"
        ):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "delivery manifest digest does not match public bundle",
                )
            )
        if delivery_manifest.get("generation_id") != acceptance.get("generation_id"):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "delivery manifest generation does not match public bundle",
                )
            )
        if delivery_manifest.get("activation_id") != acceptance.get("activation_id"):
            reasons.append(
                _reason(
                    "PUBLIC_PROVENANCE_INVALID",
                    "delivery manifest activation does not match public bundle",
                )
            )
        for field, code, message in (
            (
                "static_payload_digest",
                "PUBLIC_BUNDLE_PARTIAL",
                "static delivery digest does not match public bundle",
            ),
            (
                "worker_payload_digest",
                "PUBLIC_BUNDLE_PARTIAL",
                "Worker delivery digest does not match public bundle",
            ),
        ):
            if delivery_manifest.get(field) != acceptance.get("bundle_digest"):
                reasons.append(_reason(code, message))
        if delivery_manifest.get("dry_run_status") not in {
            "TOP5_DELIVERY_DRY_RUN",
            "TOP5_DELIVERY_IDEMPOTENT",
        }:
            reasons.append(
                _reason(
                    "PUBLIC_DRY_RUN_REQUIRED",
                    "delivery manifest does not prove a successful dry run",
                )
            )
        if delivery_manifest.get("rollback_ready") is not True:
            reasons.append(
                _reason(
                    "PUBLIC_ROLLBACK_UNAVAILABLE",
                    "delivery manifest does not preserve rollback state",
                )
            )
    reasons.extend(acceptance.get("reasons") or [])
    status = (
        TOP5_PUBLICATION_PRECHECK_READY
        if not reasons
        else TOP5_PUBLICATION_PRECHECK_BLOCKED
    )
    return {
        "status": status,
        "publication_enabled": False,
        "production_mutation": False,
        "provider_requests": 0,
        "reasons": reasons,
        "acceptance": acceptance,
    }


__all__ = [
    "TOP5_LEAGUES",
    "TOP5_PUBLICATION_PRECHECK_BLOCKED",
    "TOP5_PUBLICATION_PRECHECK_READY",
    "TOP5_PUBLIC_DELIVERY_BLOCKED",
    "TOP5_PUBLIC_DELIVERY_READY",
    "publication_precheck",
    "validate_public_bundle",
]
