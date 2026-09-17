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
    }
)


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
    raw = _first_value(*sources, keys=("activation_state", "activation_mode", "state"))
    normalized = str(raw or "disabled").strip().lower().replace("-", "_")
    aliases = {
        "off": "disabled",
        "readiness": "disabled",
        "real_shadow": "shadow",
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
    evidence_kind = (
        str(_first_value(*sources, keys=("evidence_kind", "marker")) or "")
        .strip()
        .upper()
    )
    synthetic = any(
        source.get("synthetic") is True for source in sources
    ) or evidence_kind in {
        "SYNTHETIC",
        "TEST_FIXTURE",
    }
    if evidence_kind and evidence_kind not in _PUBLIC_FOOTBALL_EVIDENCE_KINDS:
        raise PublicFootballCompatibilityError(
            f"unsupported public football evidence kind: {evidence_kind!r}"
        )
    if not synthetic:
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
            "synthetic football evidence cannot claim real observation, approval, or activation"
        )
    if state == "live" or any(
        source.get("publication_enabled") is True
        or _is_published_publication_status(source.get("publication_status"))
        for source in sources
    ):
        raise PublicFootballCompatibilityError(
            "synthetic football evidence must remain disabled or shadow and unpublished"
        )
    return True


def _public_provenance(
    record: Mapping[str, object],
    provenance: Mapping[str, object],
    *,
    snapshot_id: str,
    snapshot_kind: str,
    source_age_seconds: float | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in sorted(_PUBLIC_FOOTBALL_PROVENANCE_FIELDS):
        value = _first_value(record, provenance, keys=(key,))
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
    synthetic = _validate_synthetic_boundary(
        record, provenance, state, artifact, health
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
    source_age_seconds = _optional_number(
        _first_value(health, record, provenance, keys=("source_age_seconds",)),
        "source_age_seconds",
    )
    stale_raw = _first_value(
        health, record, provenance, keys=("stale", "stale_artifact")
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
                record, artifact, health, keys=("result_status", "settlement_status")
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
    if synthetic:
        common.update(
            {
                "synthetic": True,
                "evidence_kind": "SYNTHETIC",
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
    _validate_synthetic_boundary(record, provenance, state)
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
