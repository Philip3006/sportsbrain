"""Read-only Champions League public publication contract.

This module owns only the downstream public representation of Champions
League records.  It does not select a provider, call a provider, activate a
model, publish data, or write financial state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite

CL_CANONICAL_LEAGUE = "ucl"
CL_COMPETITION_NAME = "UEFA Champions League"
CL_LEAGUE_CODES = frozenset(
    {"ucl", "champions_league", "uefa_champs_league", "soccer_uefa_champs_league"}
)
CL_MARKETS = frozenset({"home", "draw", "away"})
CL_PUBLICATION_SCHEMA = "champions-league-publication-v1"
CL_RELEASE_FIELDS = frozenset(
    {
        "schema_version",
        "competition",
        "league_code",
        "generation_id",
        "activation_state",
        "publication_status",
        "publication_enabled",
        "publication_authorization_id",
        "provider_authority",
        "result_authority",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "prediction_count",
        "fixture_count",
        "generated_at",
        "published_at",
        "stale_after_seconds",
        "no_bet",
    }
)
CL_PROVENANCE_FIELDS = (
    "source",
    "provider",
    "source_sha",
    "research_sha",
    "model_artifact_hash",
    "snapshot_id",
    "snapshot_kind",
    "captured_at",
    "source_age_seconds",
    "evidence_digest",
)
CL_STATES = frozenset({"DISABLED", "SHADOW", "CONTROLLED", "LIVE"})
CL_PUBLICATION_STATUSES = frozenset({"UNPUBLISHED", "PUBLISHED", "FAILED", "BLOCKED"})
CL_RESULT_STATUSES = frozenset({"PENDING", "FINAL", "WON", "LOST", "VOID", "UNKNOWN"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ChampionsLeaguePublicationError(ValueError):
    """Malformed, stale, incomplete, or unsafe CL public evidence."""


def canonical_cl_league(value: object) -> str:
    if not isinstance(value, str):
        raise ChampionsLeaguePublicationError("CL league must be text")
    normalized = value.strip().lower()
    if normalized not in CL_LEAGUE_CODES:
        raise ChampionsLeaguePublicationError(f"unsupported CL league: {value!r}")
    return CL_CANONICAL_LEAGUE


def is_cl_league(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in CL_LEAGUE_CODES


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChampionsLeaguePublicationError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChampionsLeaguePublicationError(f"CL {field} is required")
    return value.strip()


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if not _SHA256_RE.fullmatch(text):
        raise ChampionsLeaguePublicationError(
            f"CL {field} must be a SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChampionsLeaguePublicationError(f"CL {field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ChampionsLeaguePublicationError(f"CL {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ChampionsLeaguePublicationError(f"CL {field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ChampionsLeaguePublicationError(f"CL {field} must be numeric") from exc
    if not isfinite(number) or number < 0:
        raise ChampionsLeaguePublicationError(
            f"CL {field} must be finite and non-negative"
        )
    return number


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChampionsLeaguePublicationError(f"CL {field} must be a positive integer")
    return value


def _state(value: object) -> str:
    normalized = _text(value, "activation_state").upper()
    if normalized not in CL_STATES:
        raise ChampionsLeaguePublicationError(
            f"unsupported CL activation_state: {normalized!r}"
        )
    return normalized


def _publication_status(value: object) -> str:
    normalized = _text(value, "publication_status").upper()
    if normalized not in CL_PUBLICATION_STATUSES:
        raise ChampionsLeaguePublicationError(
            f"unsupported CL publication_status: {normalized!r}"
        )
    return normalized


def project_cl_release(value: object) -> dict[str, object]:
    """Return the public allowlisted CL release envelope and validate it."""
    source = _mapping(value, "champions_league_release")
    result = {key: source[key] for key in CL_RELEASE_FIELDS if key in source}
    validate_cl_release(result)
    result["league_code"] = CL_CANONICAL_LEAGUE
    result["activation_state"] = _state(result["activation_state"])
    result["publication_status"] = _publication_status(result["publication_status"])
    return result


def validate_cl_release(
    value: object,
    *,
    now: datetime | None = None,
    max_age_seconds: float | None = None,
    require_published: bool = False,
) -> dict[str, object]:
    """Validate the CL release envelope without network or filesystem access."""
    release = _mapping(value, "champions_league_release")
    required = {
        "schema_version",
        "competition",
        "league_code",
        "generation_id",
        "activation_state",
        "publication_status",
        "publication_enabled",
        "provider_authority",
        "result_authority",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "prediction_count",
        "fixture_count",
        "generated_at",
        "stale_after_seconds",
        "no_bet",
    }
    missing = sorted(required - set(release))
    if missing:
        raise ChampionsLeaguePublicationError(
            "CL release is incomplete: " + ", ".join(missing)
        )
    if release["schema_version"] != CL_PUBLICATION_SCHEMA:
        raise ChampionsLeaguePublicationError("unsupported CL publication schema")
    if release["competition"] != CL_COMPETITION_NAME:
        raise ChampionsLeaguePublicationError("CL competition name mismatch")
    canonical_cl_league(release["league_code"])
    _text(release["generation_id"], "generation_id")
    state = _state(release["activation_state"])
    status = _publication_status(release["publication_status"])
    if not isinstance(release["publication_enabled"], bool):
        raise ChampionsLeaguePublicationError("CL publication_enabled must be boolean")
    if release["no_bet"] is not True:
        raise ChampionsLeaguePublicationError("CL publication must remain no-bet")
    if not release["provider_authority"] or not release["result_authority"]:
        raise ChampionsLeaguePublicationError("CL authority fields are required")
    _sha(release["source_sha"], "source_sha")
    _sha(release["research_sha"], "research_sha")
    _sha(release["model_artifact_hash"], "model_artifact_hash")
    prediction_count = _count(release["prediction_count"], "prediction_count")
    fixture_count = _count(release["fixture_count"], "fixture_count")
    stale_after = _number(release["stale_after_seconds"], "stale_after_seconds")
    if stale_after <= 0:
        raise ChampionsLeaguePublicationError("CL stale_after_seconds must be positive")
    generated_at = _timestamp(release["generated_at"], "generated_at")
    if now is not None:
        now_utc = now.astimezone(timezone.utc)
        age = (now_utc - generated_at).total_seconds()
        if age < 0:
            raise ChampionsLeaguePublicationError(
                "CL release generated_at is in the future"
            )
        freshness_limit = (
            max_age_seconds if max_age_seconds is not None else stale_after
        )
        if age > freshness_limit:
            raise ChampionsLeaguePublicationError("CL release is stale")
    if release.get("published_at"):
        _timestamp(release["published_at"], "published_at")
    if "publication_authorization_id" in release:
        _text(release["publication_authorization_id"], "publication_authorization_id")
    if release["publication_enabled"]:
        if not release.get("publication_authorization_id"):
            raise ChampionsLeaguePublicationError(
                "published CL release requires publication authorization"
            )
        if status != "PUBLISHED" or state not in {"CONTROLLED", "LIVE"}:
            raise ChampionsLeaguePublicationError(
                "published CL release must be CONTROLLED/LIVE and PUBLISHED"
            )
    elif status == "PUBLISHED":
        raise ChampionsLeaguePublicationError(
            "unenabled CL release cannot claim PUBLISHED status"
        )
    if require_published and not release["publication_enabled"]:
        raise ChampionsLeaguePublicationError("CL publication is not enabled")
    return {
        **release,
        "league_code": CL_CANONICAL_LEAGUE,
        "activation_state": state,
        "publication_status": status,
        "prediction_count": prediction_count,
        "fixture_count": fixture_count,
        "stale_after_seconds": stale_after,
    }


def _validate_cl_record(
    record: Mapping[str, object],
    *,
    now: datetime | None,
    max_age_seconds: float | None,
    release: Mapping[str, object] | None,
) -> tuple[str, str, str]:
    if record.get("sport") != "football":
        raise ChampionsLeaguePublicationError("CL record must have sport=football")
    canonical_cl_league(record.get("league"))
    prediction_id = _text(record.get("prediction_id"), "prediction_id")
    fixture_key = _text(record.get("fixture_key"), "fixture_key")
    _text(record.get("match"), "match")
    _text(record.get("home"), "home")
    _text(record.get("away"), "away")
    _timestamp(record.get("kickoff"), "kickoff")
    _timestamp(record.get("prediction_timestamp"), "prediction_timestamp")
    _text(record.get("model_identity"), "model_identity")
    _text(record.get("source"), "source")
    _text(record.get("provider"), "provider")
    market = _text(record.get("market"), "market").lower()
    if market not in CL_MARKETS:
        raise ChampionsLeaguePublicationError(f"unsupported CL market: {market!r}")
    if not isinstance(record.get("publication_enabled"), bool):
        raise ChampionsLeaguePublicationError(
            "CL record publication_enabled must be boolean"
        )
    if record.get("no_bet") is not True:
        raise ChampionsLeaguePublicationError("CL record must remain no-bet")
    _state(record.get("activation_state"))
    status = _publication_status(record.get("publication_status"))
    signal_status = _text(record.get("signal_status"), "signal_status").upper()
    if signal_status == "ACTIVE":
        raise ChampionsLeaguePublicationError("CL record cannot be actionable")
    result_status = _text(record.get("result_status"), "result_status").upper()
    settlement_status = _text(
        record.get("settlement_status"), "settlement_status"
    ).upper()
    if result_status not in CL_RESULT_STATUSES or settlement_status != result_status:
        raise ChampionsLeaguePublicationError(
            "CL result/settlement representation is invalid"
        )
    stale_state = _text(record.get("stale_state"), "stale_state").upper()
    if stale_state not in {"FRESH", "STALE", "UNKNOWN"}:
        raise ChampionsLeaguePublicationError("CL stale_state is invalid")
    provenance = _mapping(record.get("provenance"), "CL provenance")
    for field in CL_PROVENANCE_FIELDS:
        if field not in provenance:
            raise ChampionsLeaguePublicationError(f"CL provenance missing {field}")
        if field == "source_age_seconds":
            _number(provenance[field], f"provenance.{field}")
        elif field in {
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "evidence_digest",
        }:
            _sha(provenance[field], f"provenance.{field}")
        elif field == "captured_at":
            captured_at = _timestamp(provenance[field], "provenance.captured_at")
            if now is not None:
                age = (now.astimezone(timezone.utc) - captured_at).total_seconds()
                if age < 0:
                    raise ChampionsLeaguePublicationError(
                        "CL provenance captured_at is in the future"
                    )
                if max_age_seconds is not None and age > max_age_seconds:
                    raise ChampionsLeaguePublicationError(
                        "CL record provenance is stale"
                    )
        else:
            _text(provenance[field], f"provenance.{field}")
    if (
        provenance["source"] != record["source"]
        or provenance["provider"] != record["provider"]
    ):
        raise ChampionsLeaguePublicationError(
            "CL record/provenance source binding mismatch"
        )
    if release is not None:
        if (
            record.get("publication_status", "").upper()
            != release["publication_status"]
        ):
            raise ChampionsLeaguePublicationError(
                "CL record/release publication mismatch"
            )
        if record.get("activation_state", "").upper() != release["activation_state"]:
            raise ChampionsLeaguePublicationError(
                "CL record/release activation mismatch"
            )
        if record.get("publication_enabled") is not release["publication_enabled"]:
            raise ChampionsLeaguePublicationError(
                "CL record/release enablement mismatch"
            )
        if record["provider"] != release["provider_authority"]:
            raise ChampionsLeaguePublicationError("CL record/release provider mismatch")
        for field in ("source_sha", "research_sha", "model_artifact_hash"):
            if provenance[field] != release[field]:
                raise ChampionsLeaguePublicationError(f"CL {field} binding mismatch")
    if status == "PUBLISHED" and stale_state != "FRESH":
        raise ChampionsLeaguePublicationError("stale CL records cannot be published")
    if record["publication_enabled"] and status != "PUBLISHED":
        raise ChampionsLeaguePublicationError("enabled CL records must be PUBLISHED")
    return prediction_id, fixture_key, market


def _validate_cl_health(
    health: Mapping[str, object],
    *,
    release: Mapping[str, object],
) -> None:
    candidates: list[Mapping[str, object]] = []
    if isinstance(health.get("football_release"), Mapping):
        candidates.append(health["football_release"])
    if isinstance(health.get("football_releases"), Sequence) and not isinstance(
        health["football_releases"], (str, bytes)
    ):
        candidates.extend(
            item for item in health["football_releases"] if isinstance(item, Mapping)
        )
    cl_health = [item for item in candidates if is_cl_league(item.get("league"))]
    if len(cl_health) != 1:
        raise ChampionsLeaguePublicationError(
            "CL health release is missing or duplicated"
        )
    item = cl_health[0]
    required = (
        "schema_version",
        "league",
        "publication_status",
        "stale_artifact",
        "missing_result_count",
        "settlement_status",
        "source_age_seconds",
        "source",
        "provider",
        "source_sha",
        "activation_state",
        "no_bet",
        "publication_enabled",
        "observed_at",
    )
    for field in required:
        if field not in item:
            raise ChampionsLeaguePublicationError(f"CL health missing {field}")
    if item["schema_version"] != "football-release-health-v1":
        raise ChampionsLeaguePublicationError("unsupported CL health schema")
    if (
        item["source_sha"] != release["source_sha"]
        or item["provider"] != release["provider_authority"]
        or item["publication_status"] != release["publication_status"]
        or item["activation_state"].upper() != release["activation_state"]
        or item["publication_enabled"] is not release["publication_enabled"]
        or item["no_bet"] is not True
    ):
        raise ChampionsLeaguePublicationError("CL health/release provenance mismatch")
    _timestamp(item["observed_at"], "CL health observed_at")


def validate_champions_league_publication(
    payload: object,
    *,
    now: datetime | None = None,
    max_age_seconds: float | None = None,
    require_release: bool = False,
    require_health: bool = False,
    require_published: bool = False,
) -> dict[str, object]:
    """Validate a public product snapshot and return acceptance facts."""
    product = _mapping(payload, "public product")
    raw_records = product.get("football", [])
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes)):
        raise ChampionsLeaguePublicationError("public football records must be a list")
    cl_records = [
        record
        for record in raw_records
        if isinstance(record, Mapping) and is_cl_league(record.get("league"))
    ]
    raw_release = product.get("champions_league_release")
    if require_release and raw_release is None:
        raise ChampionsLeaguePublicationError("CL release envelope is required")
    release = (
        validate_cl_release(
            raw_release,
            now=now,
            max_age_seconds=max_age_seconds,
            require_published=require_published,
        )
        if raw_release is not None
        else None
    )
    effective_max_age_seconds = max_age_seconds
    if now is not None and effective_max_age_seconds is None and release is not None:
        effective_max_age_seconds = float(release["stale_after_seconds"])
    if not cl_records:
        raise ChampionsLeaguePublicationError("CL public records are missing")
    groups: dict[tuple[str, str], set[str]] = {}
    for record in cl_records:
        prediction_id, fixture_key, market = _validate_cl_record(
            record,
            now=now,
            max_age_seconds=effective_max_age_seconds,
            release=release,
        )
        groups.setdefault((prediction_id, fixture_key), set()).add(market)
    if any(markets != CL_MARKETS for markets in groups.values()):
        raise ChampionsLeaguePublicationError(
            "each CL prediction must contain home/draw/away"
        )
    if release is not None:
        if release["prediction_count"] != len(groups):
            raise ChampionsLeaguePublicationError(
                "CL prediction_count does not match records"
            )
        if release["fixture_count"] != len({fixture for _, fixture in groups}):
            raise ChampionsLeaguePublicationError(
                "CL fixture_count does not match records"
            )
        if require_health:
            _validate_cl_health(
                _mapping(product.get("health"), "health"), release=release
            )
    if (
        now is not None
        and effective_max_age_seconds is not None
        and "updated" in product
    ):
        updated = _timestamp(product["updated"], "updated")
        age = (now.astimezone(timezone.utc) - updated).total_seconds()
        if age < 0 or age > effective_max_age_seconds:
            raise ChampionsLeaguePublicationError(
                "CL public product updated timestamp is stale"
            )
    return {
        "schema_version": CL_PUBLICATION_SCHEMA,
        "league_code": CL_CANONICAL_LEAGUE,
        "prediction_count": len(groups),
        "fixture_count": len({fixture for _, fixture in groups}),
        "publication_enabled": bool(release and release["publication_enabled"]),
        "publication_status": release["publication_status"]
        if release
        else "UNPUBLISHED",
    }
