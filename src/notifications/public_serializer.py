"""
P0C-001 — Public product serialization boundary.

ALL public static artifacts (docs/data/signals.json, signals_philip.json) and
the Worker public GET /signals.json endpoint MUST derive from
serialize_public_product(). Private financial and identity fields are excluded
by an explicit ALLOWLIST — not a blacklist.

Architecture: public_payload = serialize_public_product(full_internal_snapshot)

The full internal/private snapshot is still written to Cloudflare KV for
Worker POST /pending-bet validation (bankroll cap, open bet count). The public
serializer is applied ONLY at the static-file write boundary and at the Worker
GET response boundary.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any


class PublicFootballCompatibilityError(ValueError):
    """Malformed or unsafe league-neutral football publication input."""


# ── Top-level public-product allowlist ─────────────────────────────────────
# Only keys in this set are permitted in the public payload.
# Excluded top-level: bankroll_state, open_bets, settled_bets, history (P&L),
# portfolio, wm_stats — all contain private financial state.
# meta and tennis_stats are included but reconstructed (see below).
_PUBLIC_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "updated",
        "build_info",
        "schedule",
        "all_odds",
        "model_tips",
        "model_evals",
        "football",
        "tennis",
        "top_elo",
        "wm_results",
        "odds_history",
        "health",
    }
)

# Keys that MUST NOT appear anywhere in a public payload (recursive check).
# Used by assert_no_private_fields() as the security assertion gate.
FORBIDDEN_PRIVATE_KEYS: frozenset[str] = frozenset(
    {
        # Financial state
        "bankroll",
        "bankroll_state",
        "open_bets",
        "pending_bets",
        "settled_bets",
        "bet_history",
        "ledger",
        "ledger_rows",
        "stake_history",
        "pnl_history",
        # Identity / ownership
        "user",
        "user_id",
        "default_user",
        "owner",
        # Credentials
        "auth_token",
        "token",
        "master_token",
        "api_token",
        # Private financial aggregates (from wm_stats/tennis_stats/portfolio)
        "total_staked",
        "total_pnl",
    }
)

_PUBLIC_FOOTBALL_STATES = frozenset({"disabled", "shadow", "controlled", "live"})
_PUBLIC_FOOTBALL_EVIDENCE_KINDS = frozenset(
    {
        "SYNTHETIC",
        "TEST_FIXTURE",
        "INJECTED",
        "OFFLINE_REPLAY",
        "RUNTIME_DERIVED",
        "REAL_OBSERVED",
    }
)
_PUBLIC_FOOTBALL_PROVENANCE_FIELDS = frozenset(
    {
        "source",
        "provider",
        "provider_name",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "feature_schema_hash",
        "snapshot_id",
        "snapshot_kind",
        "captured_at",
        "source_age_seconds",
        "evidence_kind",
        "observation_mode",
        "prediction_time_source",
        "prediction_time_snapshot_id",
        "prediction_time_captured_at",
    }
)

_CHAMPIONS_LEAGUE_KEYS = frozenset(
    {
        "ucl",
        "cl",
        "championsleague",
        "champions_league",
        "uefa_champions_league",
        "uefa_champs_league",
        "soccer_uefa_champions_league",
        "soccer_uefa_champs_league",
    }
)

_COMPETITION_CONTEXT_KEYS = (
    "competition_context",
    "competition_metadata",
    "competition",
)

# Context is diagnostic metadata, not an unbounded payload escape hatch.  A
# small deterministic limit keeps replayed/provider-supplied context from
# exhausting serializer memory or inflating public artifacts.
_MAX_PUBLIC_CONTEXT_DEPTH = 6
_MAX_PUBLIC_CONTEXT_ITEMS = 128
_MAX_PUBLIC_CONTEXT_STRING_LENGTH = 4096


def _normalized_key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    raise PublicFootballCompatibilityError(f"public football {field} must be boolean")


def _public_json_value(value: object, field: str, *, depth: int = 0) -> object:
    """Copy bounded JSON context without allowing arbitrary object leakage."""
    if depth > _MAX_PUBLIC_CONTEXT_DEPTH:
        raise PublicFootballCompatibilityError(
            f"public football {field} exceeds context depth limit"
        )
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > _MAX_PUBLIC_CONTEXT_STRING_LENGTH:
            raise PublicFootballCompatibilityError(
                f"public football {field} exceeds context string limit"
            )
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise PublicFootballCompatibilityError(
                f"public football {field} contains a non-finite number"
            )
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_PUBLIC_CONTEXT_ITEMS:
            raise PublicFootballCompatibilityError(
                f"public football {field} exceeds context item limit"
            )
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not key.strip():
                raise PublicFootballCompatibilityError(
                    f"public football {field} contains an invalid key"
                )
            result[key] = _public_json_value(nested, f"{field}.{key}", depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_PUBLIC_CONTEXT_ITEMS:
            raise PublicFootballCompatibilityError(
                f"public football {field} exceeds context item limit"
            )
        return [
            _public_json_value(item, f"{field}[]", depth=depth + 1)
            for item in value
        ]
    raise PublicFootballCompatibilityError(
        f"public football {field} must be JSON-compatible"
    )


def _context_mapping(*sources: Mapping[str, object]) -> dict[str, object]:
    """Merge explicit competition/fixture/result context mappings in priority order."""
    result: dict[str, object] = {}
    for source in reversed(sources):
        for key in _COMPETITION_CONTEXT_KEYS:
            nested = source.get(key)
            if isinstance(nested, Mapping):
                public_nested = _public_json_value(nested, f"{key}")
                if not isinstance(public_nested, dict):
                    raise PublicFootballCompatibilityError(
                        f"public football {key} must be an object"
                    )
                if "competition_id" not in public_nested:
                    for alias in ("id", "code"):
                        if public_nested.get(alias) not in (None, ""):
                            public_nested["competition_id"] = public_nested[alias]
                            break
                if "competition_identity" not in public_nested:
                    for alias in ("identity", "competition_id", "code", "id"):
                        if public_nested.get(alias) not in (None, ""):
                            public_nested["competition_identity"] = public_nested[alias]
                            break
                result.update(public_nested)
    return result


def _evidence_state(
    *sources: Mapping[str, object],
) -> tuple[str | None, bool, bool, bool]:
    """Return (kind, synthetic, injected, real_observed) with contradictions rejected."""
    raw_kind = _first_value(
        *sources,
        keys=("evidence_kind", "marker", "observation_mode"),
    )
    if raw_kind is None:
        origin = _first_value(*sources, keys=("evidence_origin",))
        if _normalized_key(origin) in {
            "synthetic",
            "test_fixture",
            "testfixture",
            "injected",
            "injected_evidence",
            "offline_replay",
            "offlinereplay",
            "real_observed",
            "real",
            "observed",
        }:
            raw_kind = origin
    kind = _normalized_key(raw_kind).upper() if raw_kind is not None else ""
    aliases = {
        "TESTFIXTURE": "TEST_FIXTURE",
        "OFFLINEREPLAY": "OFFLINE_REPLAY",
        "REAL": "REAL_OBSERVED",
        "OBSERVED": "REAL_OBSERVED",
        "INJECTED_EVIDENCE": "INJECTED",
    }
    kind = aliases.get(kind, kind)
    if kind and kind not in _PUBLIC_FOOTBALL_EVIDENCE_KINDS:
        raise PublicFootballCompatibilityError(
            f"unsupported public football evidence kind: {kind!r}"
        )
    # An explicit envelope boolean is authoritative over a marker copied from
    # nested metadata.  This matters for replay/injection adapters, where the
    # payload can retain a historical marker while the current envelope has
    # already classified the evidence.
    explicit_synthetic = next(
        (
            source["synthetic"]
            for source in sources
            if isinstance(source.get("synthetic"), bool)
        ),
        None,
    )
    explicit_injected = next(
        (
            source[key]
            for source in sources
            for key in ("injected", "injected_evidence")
            if isinstance(source.get(key), bool)
        ),
        None,
    )
    explicit_real_observed = next(
        (
            source["real_observed"]
            for source in sources
            if isinstance(source.get("real_observed"), bool)
        ),
        None,
    )
    synthetic = (
        explicit_synthetic
        if explicit_synthetic is not None
        else kind in {"SYNTHETIC", "TEST_FIXTURE"}
    )
    injected = (
        explicit_injected
        if explicit_injected is not None
        else kind in {"INJECTED", "OFFLINE_REPLAY"}
    )
    real_observed = (
        explicit_real_observed
        if explicit_real_observed is not None
        else kind == "REAL_OBSERVED"
    )
    if (synthetic or injected) and real_observed:
        raise PublicFootballCompatibilityError(
            "synthetic football evidence (including injected evidence) cannot claim real observation"
        )
    if not kind:
        if synthetic:
            kind = "SYNTHETIC"
        elif injected:
            kind = "INJECTED"
        elif real_observed:
            kind = "REAL_OBSERVED"
    return kind or None, synthetic, injected, real_observed


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _first_value(*sources: Mapping[str, object], keys: Sequence[str]) -> object | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicFootballCompatibilityError(f"public football {field} is required")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_number(value: object, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PublicFootballCompatibilityError(
            f"public football {field} is not numeric"
        ) from exc
    if not isfinite(number):
        raise PublicFootballCompatibilityError(f"public football {field} is not finite")
    return number


def _optional_count(value: object, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise PublicFootballCompatibilityError(
            f"public football {field} must be an integer"
        )
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise PublicFootballCompatibilityError(
            f"public football {field} must be an integer"
        ) from exc
    if count < 0:
        raise PublicFootballCompatibilityError(
            f"public football {field} must be non-negative"
        )
    return count


def _outcome_values(value: object) -> dict[str, float]:
    source = _mapping(value)
    aliases = {
        "home": ("home", "h2h_home", "1"),
        "draw": ("draw", "h2h_draw", "x"),
        "away": ("away", "h2h_away", "2"),
    }
    result: dict[str, float] = {}
    for outcome, names in aliases.items():
        raw = next((source[name] for name in names if name in source), None)
        number = _optional_number(raw, f"{outcome} probability")
        if number is None:
            continue
        if not 0 <= number <= 1:
            raise PublicFootballCompatibilityError(
                f"public football {outcome} probability must be in [0, 1]"
            )
        result[outcome] = number
    if not result:
        raise PublicFootballCompatibilityError(
            "public football prediction requires probabilities"
        )
    return result


def _market_values(value: object) -> dict[str, float]:
    source = _mapping(value)
    aliases = {
        "home": ("home", "h2h_home", "1"),
        "draw": ("draw", "h2h_draw", "x"),
        "away": ("away", "h2h_away", "2"),
    }
    result: dict[str, float] = {}
    for outcome, names in aliases.items():
        raw = next((source[name] for name in names if name in source), None)
        number = _optional_number(raw, f"{outcome} odds")
        if number is None:
            continue
        if number <= 1:
            raise PublicFootballCompatibilityError(
                f"public football {outcome} odds must be greater than 1"
            )
        result[outcome] = number
    return result


def _stable_public_signal_id(prediction_id: str, outcome: str) -> str:
    return hashlib.sha256(
        f"football-public-signal-v1:{prediction_id}:{outcome}".encode()
    ).hexdigest()[:16]


def _normalize_public_state(*sources: Mapping[str, object]) -> str:
    raw = _first_value(
        *sources,
        keys=(
            "activation_state",
            "activation_mode",
            "shadow_state",
            "shadow_runtime_state",
            "state",
        ),
    )
    normalized = str(raw or "disabled").strip().lower().replace("-", "_")
    aliases = {
        "off": "disabled",
        "readiness": "disabled",
        "real_shadow": "shadow",
        "shadow_only": "shadow",
        "inactive": "disabled",
        "controlled_activation": "controlled",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _PUBLIC_FOOTBALL_STATES:
        raise PublicFootballCompatibilityError(
            f"unsupported public football activation state: {normalized!r}"
        )
    return normalized


def _is_published_publication_status(value: object) -> bool:
    return isinstance(value, str) and value.strip().upper() == "PUBLISHED"


def _validate_synthetic_boundary(
    record: Mapping[str, object],
    provenance: Mapping[str, object],
    state: str,
    *additional_sources: Mapping[str, object],
) -> bool:
    sources = (record, provenance, *additional_sources)
    evidence_kind, synthetic, injected, _real_observed = _evidence_state(*sources)
    synthetic_or_injected = synthetic or injected
    if not synthetic_or_injected:
        return False
    forbidden_true = (
        "real_observed",
        "provider_approved",
        "model_approved",
        "signal_time_approved",
        "production_activation",
        "live_activation",
    )
    if any(source.get(key) is True for source in sources for key in forbidden_true):
        raise PublicFootballCompatibilityError(
            "synthetic football evidence (including injected evidence) cannot claim real observation, approval, or activation"
        )
    if state == "live" or any(
        source.get("publication_enabled") is True
        or _is_published_publication_status(source.get("publication_status"))
        for source in sources
    ):
        raise PublicFootballCompatibilityError(
            "synthetic football evidence (including injected evidence) must remain disabled or shadow and unpublished"
        )
    return synthetic_or_injected


def _is_champions_league(*sources: Mapping[str, object]) -> bool:
    values: list[object] = []
    for source in sources:
        values.extend(
            source.get(key)
            for key in (
                "competition",
                "competition_id",
                "competition_identity",
                "competition_code",
                "league_code",
                "league",
            )
            if source.get(key) is not None
        )
        for key in _COMPETITION_CONTEXT_KEYS:
            nested = source.get(key)
            if isinstance(nested, Mapping):
                values.extend(
                    nested.get(name)
                    for name in (
                        "id",
                        "competition_id",
                        "identity",
                        "competition_identity",
                        "code",
                        "competition_code",
                        "name",
                        "display_name",
                        "league",
                    )
                    if nested.get(name) is not None
                )
    return any(_normalized_key(value) in _CHAMPIONS_LEAGUE_KEYS for value in values)


def _validate_champions_league_boundary(
    is_champions_league: bool,
    state: str,
    *sources: Mapping[str, object],
) -> None:
    """Keep UCL compatibility records shadow-only and out of publication."""
    if not is_champions_league:
        return
    requested_publication = any(
        source.get("publication_enabled") is True
        or source.get("publication") is True
        or source.get("publication_success") is True
        or _is_published_publication_status(source.get("publication_status"))
        for source in sources
    )
    requested_live = state in {"controlled", "live"} or any(
        _normalized_key(source.get("activation_state")) in {"controlled", "live"}
        or _normalized_key(source.get("activation_mode")) in {"controlled", "live"}
        or _normalized_key(source.get("shadow_state")) in {"controlled", "live"}
        or _normalized_key(source.get("shadow_runtime_state")) in {"controlled", "live"}
        or source.get("live_activation") is True
        or source.get("production_activation") is True
        for source in sources
    )
    requested_bet = any(
        source.get("no_bet") is False or source.get("no_bet_flag") is False
        for source in sources
    )
    externally_visible = any(
        source.get("externally_visible") is True
        or source.get("publicly_visible") is True
        or source.get("external_visibility") is True
        or source.get("public") is True
        or _normalized_key(source.get("visibility")) in {"public", "external", "published"}
        for source in sources
    )
    if requested_publication or requested_live or requested_bet or externally_visible:
        raise PublicFootballCompatibilityError(
            "Champions League compatibility output must remain shadow, no-bet, and unpublished"
        )


def _public_provenance(
    record: Mapping[str, object],
    provenance: Mapping[str, object],
    *,
    snapshot_id: str,
    snapshot_kind: str,
    source_age_seconds: float | None,
    additional_sources: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    result: dict[str, object] = {}
    sources = (record, provenance, *additional_sources)
    for key in sorted(_PUBLIC_FOOTBALL_PROVENANCE_FIELDS):
        value = _first_value(*sources, keys=(key,))
        if value is not None and value != "":
            if key == "source_age_seconds":
                value = _optional_number(value, key)
            elif not isinstance(value, (str, int, float, bool)):
                continue
            result[key] = value
    if snapshot_id:
        result["snapshot_id"] = snapshot_id
    if snapshot_kind:
        result["snapshot_kind"] = snapshot_kind
    if source_age_seconds is not None:
        result["source_age_seconds"] = source_age_seconds
    return result


def _optional_context_value(
    *sources: Mapping[str, object],
    keys: Sequence[str],
) -> object | None:
    """Resolve a context field from an explicit nested context then legacy fields."""
    return _first_value(*sources, keys=keys)


def _context_payload(
    record: Mapping[str, object],
    artifact: Mapping[str, object],
    fixture: Mapping[str, object],
    provenance: Mapping[str, object],
    health: Mapping[str, object],
    result: Mapping[str, object],
    *,
    league: str,
    fixture_key: str,
    prediction_id: str,
    model_identity: str,
    generated_at: str,
    snapshot_id: str,
    snapshot_kind: str,
    signal_timestamp: str,
    source: str,
    source_age_seconds: float | None,
    stale_state: str,
    state: str,
    result_status: str,
) -> dict[str, object]:
    """Build the additive, bounded observability context for a football record."""
    competition_context = _context_mapping(record, artifact, fixture, provenance)
    context_sources = (competition_context, artifact, record, fixture, provenance)
    result_sources = (result, health, record, artifact, provenance)

    competition_id = _optional_text(
        _optional_context_value(
            *context_sources,
            keys=(
                "competition",
                "competition_id",
                "competition_code",
                "competition_identity",
                "league_code",
                "league",
            ),
        )
    ) or league
    competition_identity = _optional_text(
        _optional_context_value(
            *context_sources,
            keys=(
                "competition_identity",
                "competition_id",
                "competition_code",
            ),
        )
    ) or competition_id
    competition_name = _optional_text(
        _optional_context_value(
            *context_sources,
            keys=("competition_name", "display_name", "name"),
        )
    )
    season = _optional_text(
        _optional_context_value(
            *context_sources,
            keys=(
                "season",
                "season_id",
                "season_label",
                "competition_season",
            ),
        )
    )
    historical_format_era = _optional_text(
        _optional_context_value(
            *context_sources,
            keys=(
                "historical_format_era",
                "format_era",
                "competition_format_era",
            ),
        )
    )
    stage = _optional_text(
        _optional_context_value(
            *context_sources,
        keys=("stage", "competition_stage", "stage_name", "phase"),
        )
    )
    round_raw = _optional_context_value(
        *context_sources,
        keys=("round", "round_name", "round_number", "round_of"),
    )
    if isinstance(round_raw, bool):
        raise PublicFootballCompatibilityError("public football round must be text or integer")
    round_name: object = (
        round_raw
        if isinstance(round_raw, int)
        else _optional_text(round_raw)
    )
    leg = _optional_context_value(
        *context_sources,
        keys=("leg", "leg_number", "leg_index", "tie_leg"),
    )
    if leg is not None and (isinstance(leg, bool) or not isinstance(leg, (str, int))):
        raise PublicFootballCompatibilityError("public football leg must be text or integer")
    aggregate_raw = _optional_context_value(
        *context_sources,
        keys=("aggregate_context", "aggregate", "aggregate_score", "tie_context"),
    )
    aggregate_context = (
        _public_json_value(aggregate_raw, "aggregate_context")
        if aggregate_raw is not None
        else None
    )
    neutral_site = _optional_bool(
        _optional_context_value(
            *context_sources,
            keys=(
                "neutral_site",
                "neutral_venue",
                "is_neutral",
                "venue_neutral",
                "is_neutral_site",
                "neutral",
            ),
        ),
        "neutral_site",
    )

    model_artifact_hash = _optional_text(
        _first_value(
            artifact,
            record,
            provenance,
            keys=(
                "model_artifact_hash",
                "model_artifact_sha",
                "model_artifact_sha256",
                "model_sha",
                "artifact_hash",
                "artifact_sha",
                "artifact_sha256",
                "prediction_artifact_sha",
            ),
        )
    )
    prediction_time_raw = _optional_context_value(
        *context_sources,
        keys=(
            "prediction_time_provenance",
            "prediction_provenance",
            "prediction_time",
            "time_provenance",
        ),
    )
    if prediction_time_raw is None:
        prediction_time_provenance: object = {
            "prediction_timestamp": generated_at,
            "source": source,
            "snapshot_id": snapshot_id,
            "snapshot_kind": snapshot_kind,
            "captured_at": signal_timestamp or None,
            "evidence_kind": _first_value(
                record,
                provenance,
                artifact,
                keys=("evidence_kind", "marker", "observation_mode"),
            ),
        }
    else:
        prediction_time_provenance = _public_json_value(
            prediction_time_raw, "prediction_time_provenance"
        )
    prediction_time_details = _mapping(prediction_time_provenance)
    prediction_time_source = _optional_text(
        _first_value(
            prediction_time_details,
            *context_sources,
            keys=("prediction_time_source", "time_provenance_source", "source"),
        )
    )
    prediction_time_snapshot_id = _optional_text(
        _first_value(
            prediction_time_details,
            *context_sources,
            keys=(
                "prediction_time_snapshot_id",
                "prediction_snapshot_id",
                "snapshot_id",
            ),
        )
    ) or snapshot_id
    prediction_time_captured_at = _optional_text(
        _first_value(
            prediction_time_details,
            *context_sources,
            keys=(
                "prediction_time_captured_at",
                "prediction_captured_at",
                "captured_at",
            ),
        )
    ) or (signal_timestamp or None)

    fixture_id = _optional_text(
        _first_value(
            fixture,
            record,
            artifact,
            provenance,
            keys=("fixture_id", "provider_fixture_id", "provider_event_id", "event_id"),
        )
    )
    provider_fixture_id = _optional_text(
        _first_value(
            fixture,
            record,
            artifact,
            provenance,
            keys=("provider_fixture_id", "provider_event_id", "event_id"),
        )
    )
    fixture_identity_raw = _first_value(
        record,
        artifact,
        fixture,
        provenance,
        keys=("fixture_identity",),
    )
    fixture_identity = (
        _public_json_value(fixture_identity_raw, "fixture_identity")
        if fixture_identity_raw is not None
        else {}
    )
    if not isinstance(fixture_identity, dict):
        raise PublicFootballCompatibilityError("public football fixture_identity must be an object")
    fixture_identity.update(
        {
            "fixture_key": fixture_key,
            "fixture_id": fixture_id,
            "provider_fixture_id": provider_fixture_id,
        }
    )

    result_id = _optional_text(
        _first_value(
            *result_sources,
            keys=("result_id", "provider_result_id", "result_key"),
        )
    )
    provider_result_id = _optional_text(
        _first_value(
            *result_sources,
            keys=("provider_result_id", "result_id", "provider_event_id"),
        )
    )
    result_source = _optional_text(
        _first_value(
            *result_sources,
            keys=("result_source", "result_provider", "provider_result_source"),
        )
    )
    result_timestamp = _optional_text(
        _first_value(*result_sources, keys=("result_timestamp", "result_at", "settled_at"))
    )
    result_artifact_hash = _optional_text(
        _first_value(
            *result_sources,
            keys=(
                "result_artifact_hash",
                "result_artifact_sha",
                "result_artifact_sha256",
                "result_hash",
                "attachment_sha",
            ),
        )
    )
    result_identity_raw = _first_value(
        record,
        artifact,
        result,
        provenance,
        keys=("result_identity",),
    )
    result_identity = (
        _public_json_value(result_identity_raw, "result_identity")
        if result_identity_raw is not None
        else {}
    )
    if not isinstance(result_identity, dict):
        raise PublicFootballCompatibilityError("public football result_identity must be an object")
    result_identity.update(
        {
            "result_id": result_id,
            "provider_result_id": provider_result_id,
            "result_source": result_source,
            "result_timestamp": result_timestamp,
            "result_status": result_status,
        }
    )
    result_lineage_raw = _first_value(
        record,
        artifact,
        result,
        provenance,
        keys=("result_lineage",),
    )
    result_lineage_value = (
        _public_json_value(result_lineage_raw, "result_lineage")
        if result_lineage_raw is not None
        else {}
    )
    if not isinstance(result_lineage_value, dict):
        raise PublicFootballCompatibilityError("public football result_lineage must be an object")
    result_lineage = result_lineage_value
    # Canonical identity fields are always regenerated from the current
    # envelope so a stale attachment cannot be attributed to another
    # prediction or fixture.
    result_lineage.update(
        {
            "prediction_id": prediction_id,
            "fixture_key": fixture_key,
            "result_id": result_id,
            "provider_result_id": provider_result_id,
            "result_status": result_status,
            "result_source": result_source,
        }
    )

    freshness = {
        "state": stale_state,
        "stale": stale_state == "STALE" if stale_state != "UNKNOWN" else None,
        "source_age_seconds": source_age_seconds,
    }
    explicit_freshness = _first_value(
        health,
        record,
        provenance,
        keys=("freshness",),
    )
    if explicit_freshness is not None:
        freshness = _public_json_value(explicit_freshness, "freshness")
        if not isinstance(freshness, dict):
            freshness = {"state": freshness}
        explicit_state = _normalized_key(freshness.get("state"))
        if explicit_state in {"fresh", "current"}:
            freshness["state"] = "FRESH"
        elif explicit_state in {"stale", "expired"}:
            freshness["state"] = "STALE"
        freshness.setdefault("state", stale_state)
        explicit_age = _optional_number(
            freshness.get("source_age_seconds"), "freshness.source_age_seconds"
        )
        if explicit_age is not None and explicit_age < 0:
            raise PublicFootballCompatibilityError(
                "public football freshness.source_age_seconds must be non-negative"
            )
        freshness["source_age_seconds"] = (
            explicit_age if explicit_age is not None else source_age_seconds
        )
        if "stale" not in freshness:
            freshness["stale"] = (
                freshness["state"] == "STALE"
                if freshness["state"] in {"FRESH", "STALE"}
                else None
            )

    evidence_kind, synthetic, injected, real_observed = _evidence_state(
        record, provenance, artifact, fixture, health, result
    )
    context: dict[str, object] = {
        # Canonical names are retained alongside the legacy ``league`` field;
        # aliases below make the envelope consumable by existing football
        # readers without requiring a second competition architecture.
        "competition": competition_identity,
        "competition_id": competition_id,
        "competition_identity": competition_identity,
        "season": season,
        "season_id": season,
        "historical_format_era": historical_format_era,
        "format_era": historical_format_era,
        "stage": stage,
        "round": round_name,
        "round_name": round_name,
        "leg": leg,
        "leg_number": leg,
        "aggregate_context": aggregate_context,
        "aggregate": aggregate_context,
        "neutral_site": neutral_site,
        "neutral_venue": neutral_site,
        "venue_context": (
            "neutral" if neutral_site is True else "standard_home" if neutral_site is False else "unknown"
        ),
        "home_advantage_applicable": (
            False if neutral_site is True else True if neutral_site is False else None
        ),
        "model_artifact_hash": model_artifact_hash,
        "model_artifact_sha": model_artifact_hash,
        "model_artifact_sha256": model_artifact_hash,
        "artifact_hash": model_artifact_hash,
        "artifact_sha": model_artifact_hash,
        "artifact_sha256": model_artifact_hash,
        "prediction_artifact_sha": model_artifact_hash,
        "model_identity": model_identity,
        "prediction_timestamp": generated_at,
        "signal_timestamp": signal_timestamp,
        "prediction_time_provenance": prediction_time_provenance,
        "prediction_provenance": prediction_time_provenance,
        "prediction_time_source": prediction_time_source,
        "prediction_time_snapshot_id": prediction_time_snapshot_id,
        "prediction_time_captured_at": prediction_time_captured_at,
        "fixture_id": fixture_id,
        "provider_fixture_id": provider_fixture_id,
        "fixture_identity": fixture_identity,
        "result_id": result_id,
        "provider_result_id": provider_result_id,
        "result_source": result_source,
        "result_timestamp": result_timestamp,
        "result_identity": result_identity,
        "result_lineage": result_lineage,
        "result_artifact_hash": result_artifact_hash,
        "result_artifact_sha": result_artifact_hash,
        "result_artifact_sha256": result_artifact_hash,
        "freshness": freshness,
        "freshness_state": stale_state,
        "freshness_status": stale_state,
        "freshness_age_seconds": source_age_seconds,
        "odds_age_seconds": source_age_seconds,
        "shadow_state": state.upper(),
        "shadow_only": state in {"disabled", "shadow"},
        "inactive": state in {"disabled", "shadow"},
        "externally_visible": False if _is_champions_league(
            record, artifact, fixture, provenance, competition_context
        ) else None,
        "publicly_visible": False if _is_champions_league(
            record, artifact, fixture, provenance, competition_context
        ) else None,
        "evidence_kind": evidence_kind,
        "synthetic": synthetic,
        "injected": injected,
        "real_observed": real_observed,
    }
    if competition_name is not None:
        context["competition_name"] = competition_name
    context["competition_context"] = {
        "competition_id": competition_id,
        "competition_identity": competition_identity,
        "competition_name": competition_name,
        "season": season,
        "historical_format_era": historical_format_era,
        "stage": stage,
        "round": round_name,
        "leg": leg,
        "aggregate_context": aggregate_context,
        "neutral_site": neutral_site,
    }
    return context


def map_prediction_to_public_football_signals(
    record: Mapping[str, object],
) -> list[dict[str, object]]:
    """Map one league-neutral prediction envelope to existing PWA football signals.

    The mapper is deliberately an adapter, not a publisher.  It accepts a
    prediction artifact plus optional fixture/provenance/health mappings and
    emits the legacy ``football`` list shape used by the PWA.  It never calls a
    provider, enables activation, or asserts evidence not present in the input.
    """
    if not isinstance(record, Mapping):
        raise PublicFootballCompatibilityError(
            "public football prediction must be an object"
        )
    artifact = _mapping(
        record.get("prediction_artifact") or record.get("prediction") or record
    )
    fixture = _mapping(record.get("fixture"))
    provenance = _mapping(record.get("provenance"))
    health = _mapping(record.get("health"))
    result = _mapping(record.get("result"))

    league = _required_text(
        _first_value(artifact, record, provenance, keys=("league_code", "league")),
        "league",
    )
    fixture_key = _required_text(
        _first_value(artifact, record, fixture, keys=("fixture_key", "fixture_id")),
        "fixture_key",
    )
    prediction_id = _required_text(
        _first_value(artifact, record, keys=("prediction_id", "id")),
        "prediction_id",
    )
    model_identity = _required_text(
        _first_value(
            artifact,
            record,
            provenance,
            keys=("model_identity", "model_adapter_id", "model_version"),
        ),
        "model_identity",
    )
    generated_at = _required_text(
        _first_value(
            artifact,
            record,
            keys=("prediction_timestamp", "generated_at", "captured_at"),
        ),
        "prediction_timestamp",
    )
    probabilities = _outcome_values(
        _first_value(artifact, record, keys=("probabilities", "model_probabilities"))
    )
    state = _normalize_public_state(record, artifact, provenance)
    is_champions_league = _is_champions_league(
        record, artifact, fixture, provenance
    )
    _validate_synthetic_boundary(record, provenance, state, artifact, health, result)
    evidence_kind, synthetic, injected, real_observed = _evidence_state(
        record, provenance, artifact, fixture, health, result
    )
    _validate_champions_league_boundary(
        is_champions_league,
        state,
        record,
        artifact,
        fixture,
        provenance,
        health,
        result,
    )

    fixture_key_text = fixture_key
    home = (
        _optional_text(
            _first_value(fixture, record, keys=("home_team", "home", "home_name"))
        )
        or ""
    )
    away = (
        _optional_text(
            _first_value(fixture, record, keys=("away_team", "away", "away_name"))
        )
        or ""
    )
    match = _optional_text(_first_value(fixture, record, keys=("match", "label")))
    if not match and home and away:
        match = f"{home} vs {away}"
    match = match or fixture_key_text

    snapshot_id = (
        _optional_text(
            _first_value(
                artifact, record, provenance, keys=("snapshot_id", "signal_snapshot_id")
            )
        )
        or ""
    )
    snapshot_kind = (
        str(
            _first_value(artifact, record, provenance, keys=("snapshot_kind",))
            or "unknown"
        )
        .strip()
        .lower()
    )
    signal_timestamp = _optional_text(
        _first_value(
            record,
            artifact,
            provenance,
            keys=("signal_timestamp", "snapshot_captured_at", "captured_at"),
        )
    )
    freshness_record = _mapping(
        _first_value(health, record, provenance, result, keys=("freshness",))
    )
    source_age_seconds = _optional_number(
        _first_value(
            freshness_record,
            health,
            record,
            provenance,
            result,
            keys=("source_age_seconds", "odds_age_seconds", "freshness_age_seconds", "age_seconds"),
        ),
        "source_age_seconds",
    )
    if source_age_seconds is not None and source_age_seconds < 0:
        raise PublicFootballCompatibilityError(
            "public football source_age_seconds must be non-negative"
        )
    freshness_raw = _first_value(
        freshness_record,
        health,
        record,
        provenance,
        result,
        keys=("freshness_state", "freshness_status", "state"),
    )
    stale = None
    if isinstance(freshness_raw, str):
        freshness_state = _normalized_key(freshness_raw)
        if freshness_state in {"fresh", "current"}:
            stale = False
        elif freshness_state in {"stale", "expired"}:
            stale = True
    if stale is None:
        stale_raw = _first_value(
            freshness_record,
            health,
            record,
            provenance,
            result,
            keys=("stale", "stale_artifact"),
        )
        stale = stale_raw if isinstance(stale_raw, bool) else None
    stale_state = "STALE" if stale is True else "FRESH" if stale is False else "UNKNOWN"

    publication_enabled = (
        _first_value(
            record, artifact, health, keys=("publication_enabled", "publication")
        )
        is True
    )
    publication_status = (
        str(
            _first_value(record, artifact, health, keys=("publication_status",))
            or ("PUBLISHED" if publication_enabled else "UNPUBLISHED")
        )
        .strip()
        .upper()
    )
    if publication_status not in {"UNPUBLISHED", "PUBLISHED", "FAILED", "BLOCKED"}:
        raise PublicFootballCompatibilityError(
            f"unsupported public football publication status: {publication_status!r}"
        )
    if publication_status == "PUBLISHED":
        publication_enabled = True
    publication_failure = _first_value(
        record, artifact, health, keys=("publication_failure", "publication_error")
    )
    if publication_failure:
        publication_status = "FAILED"
        publication_enabled = False

    no_bet_raw = _first_value(record, artifact, health, keys=("no_bet", "no_bet_flag"))
    no_bet = no_bet_raw if isinstance(no_bet_raw, bool) else state != "live"
    signal_status = (
        "ACTIVE"
        if state == "live" and publication_enabled and not no_bet
        else state.upper()
    )
    result_status = (
        str(
            _first_value(
                result,
                record,
                artifact,
                health,
                keys=("result_status", "settlement_status", "status"),
            )
            or "PENDING"
        )
        .strip()
        .upper()
    )
    source = _optional_text(
        _first_value(
            record, artifact, provenance, keys=("source", "provider", "provider_name")
        )
    )
    model_version = (
        _optional_text(
            _first_value(
                record,
                artifact,
                provenance,
                keys=("model_version", "model_artifact_id"),
            )
        )
        or ""
    )
    kickoff = _optional_text(
        _first_value(fixture, record, keys=("kickoff", "commence_time"))
    )
    odds = (
        _market_values(_first_value(record, artifact, keys=("odds", "market_odds")))
        if _first_value(record, artifact, keys=("odds", "market_odds")) is not None
        else {}
    )

    fair_probabilities: dict[str, float] = {}
    if odds:
        inverse = {key: 1 / value for key, value in odds.items() if value > 1}
        total = sum(inverse.values())
        if total > 0:
            fair_probabilities = {key: value / total for key, value in inverse.items()}

    public_provenance = _public_provenance(
        record,
        provenance,
        snapshot_id=snapshot_id,
        snapshot_kind=snapshot_kind,
        source_age_seconds=source_age_seconds,
        additional_sources=(artifact, health, result),
    )
    if evidence_kind is not None:
        public_provenance["evidence_kind"] = evidence_kind
    if injected:
        public_provenance["injected"] = True
    if real_observed:
        public_provenance["real_observed"] = True
    context = _context_payload(
        record,
        artifact,
        fixture,
        provenance,
        health,
        result,
        league=league,
        fixture_key=fixture_key_text,
        prediction_id=prediction_id,
        model_identity=model_identity,
        generated_at=generated_at,
        snapshot_id=snapshot_id,
        snapshot_kind=snapshot_kind,
        signal_timestamp=signal_timestamp or "",
        source=source or "",
        source_age_seconds=source_age_seconds,
        stale_state=stale_state,
        state=state,
        result_status=result_status,
    )
    rich_context_supplied = is_champions_league or any(
        source.get(key) is not None
        for source in (record, artifact, fixture, provenance, health, result)
        for key in (
            "competition_id",
            "competition_identity",
            "competition_context",
            "competition",
            "season",
            "season_id",
            "historical_format_era",
            "format_era",
            "stage",
            "round",
            "leg",
            "aggregate_context",
            "neutral_site",
            "model_artifact_hash",
            "model_artifact_sha",
            "model_artifact_sha256",
            "prediction_time_provenance",
            "prediction_provenance",
            "prediction_time",
            "fixture_identity",
            "result_identity",
            "result_lineage",
            "freshness",
        )
    )
    common: dict[str, object] = {
        "sport": "football",
        "league": league,
        "fixture_key": fixture_key_text,
        "match": match,
        "home": home,
        "away": away,
        "kickoff": kickoff or "",
        "prediction_id": prediction_id,
        "model_identity": model_identity,
        "model_version": model_version,
        "prediction_timestamp": generated_at,
        "signal_timestamp": signal_timestamp or "",
        "signal_snapshot_id": snapshot_id,
        "snapshot_kind": snapshot_kind,
        "source": source or "",
        "source_age_seconds": source_age_seconds,
        "stale_state": stale_state,
        "activation_state": state.upper(),
        "activation_mode": state,
        "publication_status": publication_status,
        "publication_enabled": publication_enabled,
        "no_bet": no_bet,
        "signal_status": signal_status,
        "result_status": result_status,
        "settlement_status": result_status,
        "run_id": _optional_text(
            _first_value(record, artifact, health, keys=("run_id",))
        )
        or "",
        "session_id": _optional_text(
            _first_value(record, artifact, health, keys=("session_id",))
        )
        or "",
        "provenance": public_provenance,
        "model_prob": 0.0,
        "fair_prob": 0.0,
        "odds": 0.0,
        "ev_pct": 0.0,
        "stake_eur": 0.0,
        "stake_pct": 0.0,
        "confidence": "N/A",
        "n_models_agree": 0,
        "no_bet_flag": record.get("no_bet_flag") is True,
    }
    if is_champions_league:
        # The compatibility record remains inspectable for shadow evaluation,
        # but cannot become an actionable or externally visible signal.
        common.update(
            {
                **context,
                "activation_state": "SHADOW" if state != "disabled" else "DISABLED",
                "activation_mode": "shadow" if state != "disabled" else "disabled",
                "publication_status": "UNPUBLISHED",
                "publication_enabled": False,
                "no_bet": True,
                "no_bet_flag": True,
                "signal_status": "SHADOW" if state != "disabled" else "DISABLED",
                "externally_visible": False,
                "shadow_only": True,
                "shadow_state": "SHADOW" if state != "disabled" else "DISABLED",
            }
        )
    elif rich_context_supplied:
        # Preserve the legacy Top-5/Tennis-facing record shape unless a
        # caller explicitly supplied the additive context contract.
        common.update(context)
    if synthetic or injected:
        common.update(
            {
                "synthetic": synthetic,
                "evidence_kind": evidence_kind or ("SYNTHETIC" if synthetic else "INJECTED"),
                "injected": injected,
                "real_observed": False,
                "model_approved": False,
                "signal_time_approved": False,
                "production_activation": False,
            }
        )

    output: list[dict[str, object]] = []
    for outcome in ("home", "draw", "away"):
        if outcome not in probabilities:
            continue
        item = dict(common)
        item["signal_id"] = _stable_public_signal_id(prediction_id, outcome)
        item["market"] = outcome
        item["model_prob"] = round(probabilities[outcome] * 100, 4)
        item["fair_prob"] = round(fair_probabilities.get(outcome, 0.0) * 100, 4)
        item["odds"] = round(odds.get(outcome, 0.0), 4)
        if item["odds"] and item["model_prob"]:
            item["ev_pct"] = round(probabilities[outcome] * item["odds"] * 100 - 100, 4)
        if signal_timestamp:
            item["odds_ts"] = signal_timestamp
        output.append(item)
    assert_no_private_fields(output)
    return output


def build_public_football_release_health(
    record: Mapping[str, object],
) -> dict[str, object]:
    """Project one release/evidence health record into the public health schema."""
    if not isinstance(record, Mapping):
        raise PublicFootballCompatibilityError(
            "football release health must be an object"
        )
    provenance = _mapping(record.get("provenance"))
    league = _required_text(
        _first_value(record, provenance, keys=("league", "league_code")),
        "health league",
    )
    model_identity = _required_text(
        _first_value(record, provenance, keys=("model_identity", "model_adapter_id")),
        "health model_identity",
    )
    state = _normalize_public_state(record, provenance)
    result_record = _mapping(record.get("result"))
    is_champions_league = _is_champions_league(record, provenance)
    _validate_synthetic_boundary(record, provenance, state, result_record)
    _validate_champions_league_boundary(
        is_champions_league, state, record, provenance, result_record
    )
    publication_status = (
        str(
            _first_value(record, keys=("publication_status",))
            or (
                "PUBLISHED"
                if record.get("publication_success") is True
                else "UNPUBLISHED"
            )
        )
        .strip()
        .upper()
    )
    if publication_status not in {"UNPUBLISHED", "PUBLISHED", "FAILED", "BLOCKED"}:
        raise PublicFootballCompatibilityError(
            "unsupported football health publication status"
        )
    result: dict[str, object] = {
        "schema_version": "football-release-health-v1",
        "league": league,
        "model_identity": model_identity,
        "model_version": _optional_text(
            _first_value(record, provenance, keys=("model_version",))
        )
        or "",
        "run_id": _optional_text(_first_value(record, keys=("run_id",))) or "",
        "session_id": _optional_text(_first_value(record, keys=("session_id",))) or "",
        "prediction_count": _optional_count(
            record.get("prediction_count"), "prediction_count"
        ),
        "publication_status": publication_status,
        "publication_success": record.get("publication_success")
        if isinstance(record.get("publication_success"), bool)
        else None,
        "publication_failure": record.get("publication_failure")
        if isinstance(record.get("publication_failure"), bool)
        else publication_status == "FAILED",
        "stale_artifact": record.get("stale_artifact")
        if isinstance(record.get("stale_artifact"), bool)
        else None,
        "stale_artifact_age_seconds": _optional_number(
            record.get("stale_artifact_age_seconds"), "stale_artifact_age_seconds"
        ),
        "missing_result_count": _optional_count(
            record.get("missing_result_count"), "missing_result_count"
        ),
        "settlement_status": _optional_text(
            _first_value(record, keys=("settlement_status", "result_status"))
        )
        or "UNKNOWN",
        "source_age_seconds": _optional_number(
            _first_value(record, provenance, keys=("source_age_seconds",)),
            "source_age_seconds",
        ),
        "source": _optional_text(
            _first_value(
                record, provenance, keys=("source", "provider", "provider_name")
            )
        )
        or "",
        "provider": _optional_text(
            _first_value(record, provenance, keys=("provider", "provider_name"))
        )
        or "",
        "source_sha": _optional_text(
            _first_value(record, provenance, keys=("source_sha",))
        )
        or "",
        "rollback_state": _optional_text(_first_value(record, keys=("rollback_state",)))
        or ("DISABLED" if state == "disabled" else "UNKNOWN"),
        "activation_state": state.upper(),
        "no_bet": record.get("no_bet")
        if isinstance(record.get("no_bet"), bool)
        else state != "live",
        "publication_enabled": record.get("publication_enabled")
        if isinstance(record.get("publication_enabled"), bool)
        else publication_status == "PUBLISHED",
        "observed_at": _optional_text(
            _first_value(record, keys=("observed_at", "generated_at"))
        )
        or "",
    }
    for age_field in ("source_age_seconds", "stale_artifact_age_seconds"):
        age = result[age_field]
        if isinstance(age, (int, float)) and age < 0:
            raise PublicFootballCompatibilityError(
                f"public football {age_field} must be non-negative"
            )
    health_freshness_record = _mapping(
        _first_value(record, provenance, result_record, keys=("freshness",))
    )
    freshness_age = _optional_number(
        _first_value(
            health_freshness_record,
            keys=("source_age_seconds", "odds_age_seconds", "age_seconds"),
        ),
        "source_age_seconds",
    )
    if freshness_age is not None:
        result["source_age_seconds"] = freshness_age
    if (
        isinstance(result["source_age_seconds"], (int, float))
        and result["source_age_seconds"] < 0
    ):
        raise PublicFootballCompatibilityError(
            "public football source_age_seconds must be non-negative"
        )
    if result["stale_artifact"] is None:
        freshness_state = _normalized_key(
            _first_value(health_freshness_record, keys=("state", "status"))
        )
        if freshness_state in {"fresh", "current"}:
            result["stale_artifact"] = False
        elif freshness_state in {"stale", "expired"}:
            result["stale_artifact"] = True
    rich_context_supplied = any(
        source.get(key) is not None
        for source in (record, provenance, result_record)
        for key in (
            "competition_id",
            "competition_identity",
            "competition_context",
            "competition",
            "season",
            "season_id",
            "historical_format_era",
            "format_era",
            "stage",
            "round",
            "leg",
            "aggregate_context",
            "neutral_site",
            "model_artifact_hash",
            "model_artifact_sha",
            "model_artifact_sha256",
            "prediction_timestamp",
            "prediction_time_provenance",
            "prediction_provenance",
            "prediction_time",
            "fixture_identity",
            "result_identity",
            "result_lineage",
            "freshness",
            "shadow_state",
            "injected",
            "evidence_kind",
        )
    )
    if rich_context_supplied:
        health_source = _optional_text(
            _first_value(record, provenance, keys=("source", "provider", "provider_name"))
        ) or ""
        health_stale = result["stale_artifact"]
        health_stale_state = (
            "STALE"
            if health_stale is True
            else "FRESH"
            if health_stale is False
            else "UNKNOWN"
        )
        health_context = _context_payload(
            record,
            {},
            _mapping(record.get("fixture")),
            provenance,
            record,
            result_record,
            league=league,
            fixture_key=_optional_text(
                _first_value(record, provenance, keys=("fixture_key", "fixture_id"))
            ) or "",
            prediction_id=_optional_text(
                _first_value(record, result_record, keys=("prediction_id",))
            ) or "",
            model_identity=model_identity,
            generated_at=_optional_text(
                _first_value(record, keys=("prediction_timestamp", "observed_at", "generated_at"))
            ) or "",
            snapshot_id=_optional_text(
                _first_value(record, provenance, keys=("snapshot_id", "signal_snapshot_id"))
            ) or "",
            snapshot_kind=_optional_text(
                _first_value(record, provenance, keys=("snapshot_kind",))
            ) or "unknown",
            signal_timestamp=_optional_text(
                _first_value(record, provenance, keys=("prediction_time_captured_at", "captured_at"))
            ) or "",
            source=health_source,
            source_age_seconds=result["source_age_seconds"]
            if isinstance(result["source_age_seconds"], (int, float))
            else None,
            stale_state=health_stale_state,
            state=state,
            result_status=str(result["settlement_status"]),
        )
        result.update(health_context)
        if is_champions_league:
            result.update(
                {
                    "activation_state": "SHADOW" if state != "disabled" else "DISABLED",
                    "no_bet": True,
                    "publication_enabled": False,
                    "publication_status": "UNPUBLISHED",
                    "shadow_state": "SHADOW" if state != "disabled" else "DISABLED",
                    "shadow_only": True,
                    "externally_visible": False,
                }
            )
    assert_no_private_fields(result)
    return result


def serialize_public_football_records(records: object) -> object:
    """Normalize marked prediction envelopes while preserving legacy signals."""
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return records
    output: list[object] = []
    for record in records:
        if isinstance(record, Mapping) and (
            record.get("record_type") == "prediction_artifact"
            or "prediction_artifact" in record
            or ("prediction" in record and "market" not in record)
        ):
            output.extend(map_prediction_to_public_football_signals(record))
        else:
            output.append(record)
    return output


def _public_meta(meta: dict | None) -> dict:
    """Return a public-safe meta object — operational flags only, no identity."""
    if not isinstance(meta, dict):
        return {}
    return {"stale_odds": bool(meta.get("stale_odds", False))}


def _public_tennis_stats(ts: dict | None) -> dict:
    """Return public-safe tennis_stats — operational context only, no financial stats.

    ts["stats"] contains total_staked, total_pnl, roi_pct — these are private.
    Only the tournament/gate operational fields are public.
    """
    if not isinstance(ts, dict):
        return {}
    result: dict = {}
    for key in ("updated", "active_tournaments", "live_gate_status"):
        if key in ts:
            result[key] = ts[key]
    return result


def serialize_public_product(snapshot: dict | None) -> dict:
    """Return a public-safe product payload derived from a full internal snapshot.

    Implements an explicit ALLOWLIST: every top-level key must be on the list.
    Nested objects with mixed public/private fields (meta, tennis_stats) are
    reconstructed using their own allowlists.

    Fail-closed: assert_no_private_fields() is called on the result before
    returning. If any forbidden key survived (e.g. nested inside an approved
    container), an AssertionError is raised rather than leaking private data.

    Private state excluded:
      - bankroll_state (free, staked, exposure_pct, max_win, pnl_closed,
        published_at)
      - open_bets (active bet list)
      - settled_bets (bet history)
      - history (per-day P&L derived from ledger)
      - portfolio (per-market P&L derived from ledger)
      - wm_stats (WM betting performance stats: drawdown, CLV, ROI …)
      - meta.user / meta.default_user (default-user identity)
      - tennis_stats.stats (total_staked, total_pnl, roi_pct …)
    """
    if not isinstance(snapshot, dict):
        return {}
    pub: dict = {}
    for key in _PUBLIC_TOP_LEVEL_KEYS:
        if key in snapshot:
            pub[key] = snapshot[key]
    if "football" in pub:
        pub["football"] = serialize_public_football_records(pub["football"])
    if isinstance(snapshot.get("health"), Mapping):
        public_health = dict(snapshot["health"])
        if "football_release" in public_health:
            public_health["football_release"] = build_public_football_release_health(
                _mapping(public_health["football_release"])
            )
        if isinstance(
            public_health.get("football_releases"), Sequence
        ) and not isinstance(public_health["football_releases"], (str, bytes)):
            public_health["football_releases"] = [
                build_public_football_release_health(_mapping(item))
                for item in public_health["football_releases"]
            ]
        pub["health"] = public_health
    # Reconstruct objects that contain a mix of public and private fields.
    if "meta" in snapshot:
        pub["meta"] = _public_meta(snapshot["meta"])
    if "tennis_stats" in snapshot:
        pub["tennis_stats"] = _public_tennis_stats(snapshot["tennis_stats"])
    # Fail-closed gate: any forbidden key nested inside an approved container
    # raises AssertionError here rather than reaching the caller.
    assert_no_private_fields(pub)
    return pub


def assert_no_private_fields(obj: Any, _path: str = "root") -> None:
    """Recursively assert that no forbidden private key appears in a public payload.

    Raises AssertionError with the offending path when a private key is found.
    Walks dicts, lists, and nested combinations to any depth.

    Used as the deterministic security gate in tests/privacy/test_public_serializer.py
    and can be called at static-file write time to enforce the invariant.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_PRIVATE_KEYS:
                raise AssertionError(
                    f"P0C-001 PRIVACY VIOLATION — forbidden key '{k}' found at {_path}.{k}"
                )
            assert_no_private_fields(v, _path=f"{_path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            assert_no_private_fields(item, _path=f"{_path}[{i}]")
