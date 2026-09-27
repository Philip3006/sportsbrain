"""Fail-closed public projection for verified Nations League shadow runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from src.runtime.paths import governed_runtime_root

SCHEMA = "nations-league-public-v1"
ARTIFACT_SCHEMA = "nations-league-isports-shadow-v1"
COMPETITION = "UEFA Nations League"
PROVIDER = "isports_api"
PROVIDER_LEAGUE_ID = 146819
EVIDENCE_STATUS = "WEAK_EVIDENCE_SHADOW_ONLY"
LIFECYCLE = "SHADOW_ONLY"
MAX_ARTIFACT_AGE = timedelta(minutes=15)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^unl-shadow-\d{8}T\d{6}Z-[0-9a-f]{12}$")
_OUTCOMES = ("home", "draw", "away")
_PUBLIC_KEYS = frozenset(
    {
        "schema",
        "competition",
        "provider",
        "provider_league_id",
        "evidence_status",
        "lifecycle",
        "no_bet",
        "publication_enabled",
        "captured_at",
        "source_sha",
        "artifact_digest",
        "model_snapshot_digest",
        "fixture_count",
        "fixtures",
        "public_digest",
    }
)
_PUBLIC_FIXTURE_KEYS = frozenset(
    {
        "provider_event_id",
        "competition",
        "kickoff",
        "home",
        "away",
        "captured_at",
        "model",
        "market",
        "source_sha",
        "artifact_digest",
    }
)


class NationsLeaguePublicError(ValueError):
    """An unsupported, stale, incomplete, or unbound public shadow artifact."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NationsLeaguePublicError("artifact is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _public_canonical(value: Any) -> str:
    """Use JSON.stringify-compatible number spelling for cross-runtime digests."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NationsLeaguePublicError("public bundle contains a non-finite number")
        if value == 0:
            return "0"
        # Public probabilities and odds are projected at six decimal places.
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, Mapping):
        return (
            "{"
            + ",".join(
                f"{_public_canonical(str(key))}:{_public_canonical(item)}"
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_public_canonical(item) for item in value) + "]"
    raise NationsLeaguePublicError("public bundle contains an unsupported JSON value")


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeaguePublicError(f"{field} is required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeaguePublicError(f"{field} is malformed") from exc
    if result.tzinfo is None:
        raise NationsLeaguePublicError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


def _probabilities(value: object, field: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(_OUTCOMES):
        raise NationsLeaguePublicError(f"{field} must contain home, draw, away")
    result: dict[str, float] = {}
    for key in _OUTCOMES:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NationsLeaguePublicError(f"{field}.{key} is not numeric")
        number = float(item)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise NationsLeaguePublicError(f"{field}.{key} is outside [0, 1]")
        result[key] = number
    if abs(sum(result.values()) - 1.0) > 1e-6:
        raise NationsLeaguePublicError(f"{field} is not normalized")
    return result


def _public_probabilities(value: object, field: str) -> dict[str, float]:
    values = _probabilities(value, field)
    rounded = {key: round(values[key], 6) for key in ("home", "draw")}
    rounded["away"] = round(1.0 - rounded["home"] - rounded["draw"], 6)
    return _probabilities(rounded, field)


def _validate_public_probabilities(value: object, field: str) -> dict[str, float]:
    values = _probabilities(value, field)
    if any(round(item, 6) != item for item in values.values()):
        raise NationsLeaguePublicError(f"{field} exceeds public precision")
    return values


def _public_digest(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("public_digest", None)
    return hashlib.sha256(_public_canonical(body).encode("utf-8")).hexdigest()


def validate_public_nations_league(
    value: object,
    *,
    now: datetime | None = None,
    max_age: timedelta = MAX_ARTIFACT_AGE,
) -> dict[str, Any]:
    """Validate the already-projected public bundle without private fields."""
    if not isinstance(value, Mapping):
        raise NationsLeaguePublicError("public Nations League bundle is not an object")
    payload = dict(value)
    if set(payload) != _PUBLIC_KEYS:
        raise NationsLeaguePublicError(
            "public Nations League fields are not allowlisted"
        )
    if payload.get("schema") != SCHEMA:
        raise NationsLeaguePublicError("unsupported public Nations League schema")
    if (
        payload.get("competition") != COMPETITION
        or payload.get("provider") != PROVIDER
        or not isinstance(payload.get("provider_league_id"), int)
        or isinstance(payload.get("provider_league_id"), bool)
        or payload.get("provider_league_id") != PROVIDER_LEAGUE_ID
    ):
        raise NationsLeaguePublicError("public competition/provider identity mismatch")
    if (
        payload.get("evidence_status") != EVIDENCE_STATUS
        or payload.get("lifecycle") != LIFECYCLE
        or payload.get("no_bet") is not True
        or payload.get("publication_enabled") is not False
    ):
        raise NationsLeaguePublicError("public shadow safety state is invalid")
    source_sha = payload.get("source_sha")
    if not isinstance(source_sha, str) or not _SOURCE_SHA.fullmatch(source_sha):
        raise NationsLeaguePublicError("public source SHA is malformed")
    for field in ("artifact_digest", "model_snapshot_digest", "public_digest"):
        if not isinstance(payload.get(field), str) or not _SHA256.fullmatch(
            payload[field]
        ):
            raise NationsLeaguePublicError(f"public {field} is malformed")
    if payload["public_digest"] != _public_digest(payload):
        raise NationsLeaguePublicError("public bundle digest mismatch")
    captured = _timestamp(payload.get("captured_at"), "captured_at")
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    age = current - captured
    if age < timedelta(0) or age > max_age:
        raise NationsLeaguePublicError(
            "public Nations League artifact is stale or future-dated"
        )
    fixtures = payload.get("fixtures")
    count = payload.get("fixture_count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or not isinstance(fixtures, list)
        or len(fixtures) != count
    ):
        raise NationsLeaguePublicError("public fixture coverage is incomplete")
    ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, Mapping):
            raise NationsLeaguePublicError("public fixture is malformed")
        if set(fixture) != _PUBLIC_FIXTURE_KEYS:
            raise NationsLeaguePublicError("public fixture fields are not allowlisted")
        event_id = fixture.get("provider_event_id")
        if not isinstance(event_id, str) or not event_id.strip() or event_id in ids:
            raise NationsLeaguePublicError(
                "public fixture identity is missing or duplicated"
            )
        ids.add(event_id)
        if fixture.get("competition") != COMPETITION:
            raise NationsLeaguePublicError("public fixture tournament mismatch")
        kickoff = _timestamp(fixture.get("kickoff"), "fixture kickoff")
        if kickoff <= current:
            raise NationsLeaguePublicError("public fixture has already kicked off")
        for field in ("home", "away"):
            if not isinstance(fixture.get(field), str) or not fixture[field].strip():
                raise NationsLeaguePublicError(f"public fixture {field} is missing")
        model = fixture.get("model")
        market = fixture.get("market")
        if not isinstance(model, Mapping) or not isinstance(market, Mapping):
            raise NationsLeaguePublicError("public model/market record is missing")
        if (
            not isinstance(market.get("bookmaker"), str)
            or not market["bookmaker"].strip()
        ):
            raise NationsLeaguePublicError("public bookmaker provenance is missing")
        if set(model) != {"probabilities", "components"} or set(market) != {
            "bookmaker",
            "odds_decimal",
            "probabilities",
        }:
            raise NationsLeaguePublicError(
                "public model/market fields are not allowlisted"
            )
        if not isinstance(model.get("components"), Mapping) or set(
            model["components"]
        ) != {"raw_dixon_coles", "raw_gbt", "canonical_stacker"}:
            raise NationsLeaguePublicError("public model components are incomplete")
        model_probabilities = _validate_public_probabilities(
            model.get("probabilities"), "model probabilities"
        )
        components = model.get("components")
        if not isinstance(components, Mapping) or set(components) != {
            "raw_dixon_coles",
            "raw_gbt",
            "canonical_stacker",
        }:
            raise NationsLeaguePublicError("public model components are incomplete")
        for name, probabilities in components.items():
            _validate_public_probabilities(probabilities, f"model component {name}")
        if components["canonical_stacker"] != model_probabilities:
            raise NationsLeaguePublicError(
                "public final probabilities differ from canonical stacker"
            )
        odds = market.get("odds_decimal")
        if not isinstance(odds, Mapping) or set(odds) != set(_OUTCOMES):
            raise NationsLeaguePublicError("public market odds are incomplete")
        for outcome in _OUTCOMES:
            item = odds[outcome]
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise NationsLeaguePublicError("public market odds are malformed")
            if not math.isfinite(float(item)) or float(item) <= 1.0:
                raise NationsLeaguePublicError("public market odds must exceed 1")
            if round(float(item), 6) != float(item):
                raise NationsLeaguePublicError(
                    "public market odds exceed public precision"
                )
        _validate_public_probabilities(
            market.get("probabilities"), "market probabilities"
        )
        if (
            fixture.get("source_sha") != source_sha
            or fixture.get("artifact_digest") != payload["artifact_digest"]
        ):
            raise NationsLeaguePublicError("public fixture provenance mismatch")
        if fixture.get("captured_at") != payload.get("captured_at"):
            raise NationsLeaguePublicError("public fixture capture time mismatch")
    return payload


def build_public_nations_league(
    artifact: object,
    *,
    expected_source_sha: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the complete private run artifact and project safe public fields."""
    if not isinstance(artifact, Mapping):
        raise NationsLeaguePublicError("shadow artifact is not an object")
    raw = dict(artifact)
    claimed_digest = raw.pop("artifact_digest", None)
    if not isinstance(claimed_digest, str) or not _SHA256.fullmatch(claimed_digest):
        raise NationsLeaguePublicError("artifact digest is missing or malformed")
    if _digest(raw) != claimed_digest:
        raise NationsLeaguePublicError("artifact digest mismatch")
    source_sha = raw.get("source_sha")
    if (
        not isinstance(expected_source_sha, str)
        or not _SOURCE_SHA.fullmatch(expected_source_sha)
        or source_sha != expected_source_sha
    ):
        raise NationsLeaguePublicError(
            "artifact source SHA does not match the verified release"
        )
    if raw.get("schema") != ARTIFACT_SCHEMA:
        raise NationsLeaguePublicError("unsupported shadow artifact schema")
    _validate_isports_identity(raw)
    if (
        raw.get("shadow") is not True
        or raw.get("no_bet") is not True
        or raw.get("publication") is not False
        or raw.get("ledger_mutation") is not False
        or raw.get("synthetic") is True
        or str(raw.get("evidence_kind", "")).upper() in {"SYNTHETIC", "TEST_FIXTURE"}
    ):
        raise NationsLeaguePublicError(
            "artifact is synthetic or outside shadow-only authority"
        )
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise NationsLeaguePublicError("artifact run identity is not production-shaped")
    captured = _timestamp(raw.get("captured_at"), "artifact captured_at")
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    age = current - captured
    if age < timedelta(0) or age > MAX_ARTIFACT_AGE:
        raise NationsLeaguePublicError("shadow artifact is stale or future-dated")
    _validate_isports_request_provenance(raw, captured_at=captured, current=current)
    snapshot = raw.get("model_snapshot")
    if not isinstance(snapshot, Mapping):
        raise NationsLeaguePublicError("model snapshot provenance is missing")
    files = snapshot.get("files")
    snapshot_digest = snapshot.get("digest")
    if (
        not isinstance(files, Mapping)
        or not files
        or any(
            not isinstance(key, str)
            or not isinstance(item, str)
            or not _SHA256.fullmatch(item)
            for key, item in files.items()
        )
        or not isinstance(snapshot_digest, str)
        or _digest(dict(files)) != snapshot_digest
    ):
        raise NationsLeaguePublicError("model snapshot provenance digest mismatch")
    fixture_count = raw.get("fixture_count")
    covered_count = raw.get("covered_fixture_count")
    fixtures = raw.get("fixtures")
    if (
        not isinstance(fixture_count, int)
        or isinstance(fixture_count, bool)
        or fixture_count < 1
        or isinstance(covered_count, bool)
        or covered_count != fixture_count
        or not isinstance(raw.get("provider_event_count"), int)
        or isinstance(raw.get("provider_event_count"), bool)
        or raw["provider_event_count"] < fixture_count
        or not isinstance(fixtures, list)
        or len(fixtures) != fixture_count
    ):
        raise NationsLeaguePublicError("shadow artifact fixture coverage is incomplete")
    skipped = raw.get("skipped_fixtures")
    if not isinstance(skipped, list) or any(
        not isinstance(item, Mapping)
        or item.get("reason") not in {"non_target_sport_key", "non_target_tournament"}
        for item in skipped
    ):
        raise NationsLeaguePublicError(
            "shadow artifact contains skipped target fixtures"
        )

    public_fixtures: list[dict[str, Any]] = []
    for item in fixtures:
        if not isinstance(item, Mapping) or item.get("tournament") != COMPETITION:
            raise NationsLeaguePublicError("shadow fixture has the wrong tournament")
        if not isinstance(item.get("neutral"), bool):
            raise NationsLeaguePublicError(
                "Nations League venue provenance is missing or malformed"
            )
        model_probabilities = _probabilities(
            item.get("probabilities", {}).get("final_ensemble")
            if isinstance(item.get("probabilities"), Mapping)
            else None,
            "final ensemble probabilities",
        )
        probabilities = item["probabilities"]
        component_names = {
            "raw_dixon_coles": "raw_dixon_coles",
            "raw_gbt": "raw_gbt",
            "canonical_stacker": "canonical_stacker",
        }
        components = {
            output: _public_probabilities(
                probabilities.get(source), f"{output} probabilities"
            )
            for output, source in component_names.items()
        }
        model_probabilities = _public_probabilities(
            model_probabilities, "final ensemble probabilities"
        )
        if components["canonical_stacker"] != model_probabilities:
            raise NationsLeaguePublicError(
                "final model output differs from the canonical stacker"
            )
        market = item.get("market")
        if not isinstance(market, Mapping):
            raise NationsLeaguePublicError("shadow fixture market is missing")
        if (
            not isinstance(market.get("bookmaker"), str)
            or not market["bookmaker"].strip()
        ):
            raise NationsLeaguePublicError(
                "shadow fixture bookmaker provenance is missing"
            )
        odds = market.get("odds_decimal")
        market_probabilities = market.get("margin_free_probabilities")
        market_probabilities = _public_probabilities(
            market_probabilities, "market probabilities"
        )
        if not isinstance(odds, Mapping) or set(odds) != set(_OUTCOMES):
            raise NationsLeaguePublicError("shadow fixture market odds are incomplete")
        for outcome in _OUTCOMES:
            price = odds[outcome]
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(float(price))
                or float(price) <= 1.0
            ):
                raise NationsLeaguePublicError(
                    "shadow fixture market odds are malformed"
                )
        kickoff = _timestamp(item.get("kickoff"), "fixture kickoff")
        fixture_captured = _timestamp(item.get("captured_at"), "fixture captured_at")
        if fixture_captured != captured or kickoff <= current:
            raise NationsLeaguePublicError(
                "fixture time/provenance is stale or mismatched"
            )
        event_id = _isports_match_id(item.get("provider_match_id"))
        home, away = item.get("home_team"), item.get("away_team")
        if not all(
            isinstance(text, str) and text.strip() for text in (event_id, home, away)
        ):
            raise NationsLeaguePublicError("shadow fixture identity is incomplete")
        public_fixtures.append(
            {
                "provider_event_id": event_id,
                "competition": COMPETITION,
                "kickoff": kickoff.isoformat().replace("+00:00", "Z"),
                "home": home,
                "away": away,
                "captured_at": raw["captured_at"],
                "model": {
                    "probabilities": model_probabilities,
                    "components": components,
                },
                "market": {
                    "bookmaker": market.get("bookmaker"),
                    "odds_decimal": {
                        key: round(float(odds[key]), 6) for key in _OUTCOMES
                    },
                    "probabilities": dict(market_probabilities),
                },
                "source_sha": source_sha,
                "artifact_digest": claimed_digest,
            }
        )
    if len({item["provider_event_id"] for item in public_fixtures}) != fixture_count:
        raise NationsLeaguePublicError("shadow artifact fixture IDs are duplicated")
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "competition": COMPETITION,
        "provider": PROVIDER,
        "provider_league_id": PROVIDER_LEAGUE_ID,
        "evidence_status": EVIDENCE_STATUS,
        "lifecycle": LIFECYCLE,
        "no_bet": True,
        "publication_enabled": False,
        "captured_at": raw["captured_at"],
        "source_sha": source_sha,
        "artifact_digest": claimed_digest,
        "model_snapshot_digest": snapshot_digest,
        "fixture_count": fixture_count,
        "fixtures": public_fixtures,
    }
    payload["public_digest"] = _public_digest(payload)
    return validate_public_nations_league(payload, now=current)


def _validate_isports_identity(raw: Mapping[str, Any]) -> None:
    """Validate the one supported iSports Nations League identity contract."""
    if (
        raw.get("provider") != PROVIDER
        or raw.get("competition") != COMPETITION
        or not isinstance(raw.get("provider_league_id"), int)
        or isinstance(raw.get("provider_league_id"), bool)
        or raw.get("provider_league_id") != PROVIDER_LEAGUE_ID
    ):
        raise NationsLeaguePublicError("iSports competition/provider identity mismatch")


def _validate_isports_request_provenance(
    raw: Mapping[str, Any], *, captured_at: datetime, current: datetime
) -> None:
    """Validate the two known iSports operations without exposing auth data."""
    request_count = raw.get("request_count")
    retry_count = raw.get("retry_count")
    if (
        not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or request_count != 2
        or not isinstance(retry_count, int)
        or isinstance(retry_count, bool)
        or retry_count != 0
    ):
        raise NationsLeaguePublicError("iSports request/retry bounds are unsupported")
    manifest = raw.get("provider_operation_manifest")
    if not isinstance(manifest, list) or len(manifest) != 2:
        raise NationsLeaguePublicError("iSports operation manifest is incomplete")
    expected_operations = (
        (1, "schedule", "/sport/football/schedule/basic"),
        (2, "odds", "/sport/football/odds/european/all"),
    )
    expected_keys = {
        "ordinal",
        "operation",
        "method",
        "path",
        "query",
        "status_code",
        "started_at",
        "completed_at",
        "response_sha256",
    }
    for record, (ordinal, operation, path) in zip(
        manifest, expected_operations, strict=True
    ):
        if not isinstance(record, Mapping) or set(record) != expected_keys:
            raise NationsLeaguePublicError(
                "iSports operation manifest fields are unsupported"
            )
        if (
            not isinstance(record.get("ordinal"), int)
            or isinstance(record.get("ordinal"), bool)
            or record.get("ordinal") != ordinal
            or record.get("operation") != operation
            or record.get("method") != "GET"
            or record.get("path") != path
        ):
            raise NationsLeaguePublicError("iSports operation identity mismatch")
        query = record.get("query")
        if operation == "schedule":
            query_valid = (
                isinstance(query, Mapping)
                and set(query) == {"leagueId"}
                and query.get("leagueId") == str(PROVIDER_LEAGUE_ID)
            )
        else:
            day = query.get("day") if isinstance(query, Mapping) else None
            query_valid = (
                isinstance(query, Mapping)
                and set(query) == {"day"}
                and isinstance(day, (str, int))
                and not isinstance(day, bool)
                and re.fullmatch(r"[0-9]+", str(day)) is not None
            )
        status = record.get("status_code")
        if (
            not query_valid
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not 200 <= status < 300
            or not isinstance(record.get("response_sha256"), str)
            or not _SHA256.fullmatch(record["response_sha256"])
        ):
            raise NationsLeaguePublicError(
                "iSports operation request/provenance is invalid"
            )
        started = _timestamp(record.get("started_at"), "iSports operation started_at")
        completed = _timestamp(
            record.get("completed_at"), "iSports operation completed_at"
        )
        if (
            started > completed
            or completed > captured_at
            or current - completed > MAX_ARTIFACT_AGE
        ):
            raise NationsLeaguePublicError(
                "iSports operation time/provenance is invalid"
            )


def _isports_match_id(value: object) -> str | None:
    """Return the stable native iSports matchId in public string form."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    match_id = str(value).strip()
    return match_id or None


def load_public_nations_league_from_runtime(
    run_id: str,
    *,
    expected_source_sha: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read only a production-shaped immutable run from governed runtime state."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise NationsLeaguePublicError("invalid Nations League run id")
    root = governed_runtime_root().resolve()
    directory = root / "data" / "nations-league-shadow"
    path = directory / f"{run_id}.json"
    try:
        resolved_directory = directory.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        if (
            root not in resolved_directory.parents
            or resolved_path.parent != resolved_directory
            or path.is_symlink()
            or not resolved_path.is_file()
        ):
            raise NationsLeaguePublicError(
                "runtime artifact path is outside the governed Nations League directory"
            )
        artifact = json.loads(resolved_path.read_text(encoding="utf-8"))
    except NationsLeaguePublicError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise NationsLeaguePublicError(
            "verified runtime shadow artifact is unavailable"
        ) from exc
    return build_public_nations_league(
        artifact, expected_source_sha=expected_source_sha, now=now
    )
