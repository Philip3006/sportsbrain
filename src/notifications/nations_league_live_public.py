"""Deterministic LIVE projection for an already-bound Nations League release.

This module is deliberately separate from ``nations_league_public``.  The
older module remains a strict iSports `SHADOW_ONLY` projection.  Here, a LIVE
status is a product-display state only: every output is no-bet, has no ledger
authority, performs no I/O, and reuses immutable source probabilities exactly.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from src.analysis.nations_league_model_lifecycle import (
    FROZEN_ALGORITHM_DIGEST,
    MODEL_FAMILY,
    MODEL_VERSION,
    release_binding_payload,
)
from src.models.lifecycle import ACTIVE, ModelRelease

SCHEMA = "nations-league-live-public-v1"
COMPETITION = "UEFA Nations League"
_OUTCOMES = ("home", "draw", "away")
_PUBLIC_KEYS = frozenset(
    {
        "schema",
        "competition",
        "status",
        "experimental",
        "publication_enabled",
        "no_bet",
        "betting_enabled",
        "ledger_mutation",
        "model_release",
        "fixture_count",
        "fixtures",
        "audit_history",
        "updated_at",
        "public_digest",
    }
)
_PUBLIC_FIXTURE_KEYS = frozenset(
    {
        "fixture_id",
        "competition",
        "source_prediction_record_id",
        "source_identity",
        "canonical_identity",
        "kickoff_utc",
        "phase",
        "probabilities",
        "prediction_cutoff",
        "updated_at",
    }
)
_RELEASE_KEYS = frozenset(
    {
        "model_family",
        "model_version",
        "algorithm_digest",
        "release_id",
        "training_data_digest",
        "trained_state_digest",
        "training_cutoff",
        "binding_digest",
    }
)


class NationsLeagueLivePublicError(ValueError):
    """Source evidence is not eligible for an immutable LIVE projection."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NationsLeagueLivePublicError("LIVE public value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLivePublicError(f"{field} is required")
    return value


def _digest_text(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise NationsLeagueLivePublicError(f"{field} must be a SHA-256 digest")
    return text


def _probabilities(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(_OUTCOMES):
        raise NationsLeagueLivePublicError("source probabilities are incomplete")
    result: dict[str, float] = {}
    for outcome in _OUTCOMES:
        item = value[outcome]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NationsLeagueLivePublicError("source probabilities are not numeric")
        number = float(item)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise NationsLeagueLivePublicError("source probability is outside [0, 1]")
        result[outcome] = number
    if abs(sum(result.values()) - 1.0) > 1e-9:
        raise NationsLeagueLivePublicError("source probabilities are not normalized")
    return result


def _binding_index(binding_manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if binding_manifest.get("schema") != "nations-league-live-evidence-binding-v1":
        raise NationsLeagueLivePublicError("unsupported LIVE evidence binding schema")
    rows = binding_manifest.get("source_records")
    if not isinstance(rows, list) or not rows:
        raise NationsLeagueLivePublicError("LIVE evidence binding contains no records")
    index: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLivePublicError("LIVE evidence binding row is malformed")
        row = dict(raw)
        record_id = _digest_text(row.get("source_prediction_record_id"), "source_prediction_record_id")
        if record_id in index:
            raise NationsLeagueLivePublicError("LIVE evidence binding duplicates a record")
        for field in (
            "input_snapshot_digest",
            "training_data_digest",
            "trained_state_digest",
        ):
            _digest_text(row.get(field), field)
        if row.get("algorithm_digest") != FROZEN_ALGORITHM_DIGEST:
            raise NationsLeagueLivePublicError("LIVE evidence has the wrong algorithm")
        index[record_id] = row
    expected = binding_manifest.get("binding_digest")
    if expected != _digest({key: value for key, value in binding_manifest.items() if key != "binding_digest"}):
        raise NationsLeagueLivePublicError("LIVE evidence binding digest mismatch")
    return index


def _validate_release(release: ModelRelease) -> dict[str, Any]:
    if (
        not isinstance(release, ModelRelease)
        or release.status != ACTIVE
        or release.snapshot.model_family != MODEL_FAMILY
        or release.snapshot.algorithm_version != MODEL_VERSION
        or release.snapshot.algorithm_digest != FROZEN_ALGORITHM_DIGEST
    ):
        raise NationsLeagueLivePublicError("active Nations League release is required")
    return release_binding_payload(release)


def _source_fixture(record: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    if (
        record.get("record_type") != "prediction"
        or record.get("competition") != COMPETITION
        or record.get("model_version") != MODEL_VERSION
        or record.get("model_digest") != FROZEN_ALGORITHM_DIGEST
        or record.get("shadow") is not True
        or record.get("no_bet") is not True
        or record.get("publication_enabled") is not False
        or record.get("ledger_mutation") is not False
    ):
        raise NationsLeagueLivePublicError("source record violates frozen evidence safety")
    record_id = _digest_text(record.get("record_id"), "record_id")
    if record_id != binding["source_prediction_record_id"]:
        raise NationsLeagueLivePublicError("source record identity differs from binding")
    input_provenance = record.get("input_provenance")
    if not isinstance(input_provenance, Mapping) or input_provenance.get(
        "input_snapshot_digest"
    ) != binding["input_snapshot_digest"]:
        raise NationsLeagueLivePublicError("source record input-state binding differs")
    probabilities = _probabilities(record.get("probabilities"))
    source_home = _required_text(record.get("home_team"), "home_team")
    source_away = _required_text(record.get("away_team"), "away_team")
    identity = record.get("model_identity")
    canonical_home = source_home
    canonical_away = source_away
    if identity is not None:
        if not isinstance(identity, Mapping):
            raise NationsLeagueLivePublicError("source model identity is malformed")
        canonical_home = _required_text(identity.get("home_team"), "canonical home_team")
        canonical_away = _required_text(identity.get("away_team"), "canonical away_team")
    return {
        "fixture_id": _required_text(record.get("fixture_id"), "fixture_id"),
        "competition": COMPETITION,
        "source_prediction_record_id": record_id,
        "source_identity": {"home_team": source_home, "away_team": source_away},
        "canonical_identity": {"home_team": canonical_home, "away_team": canonical_away},
        "kickoff_utc": _required_text(record.get("kickoff_utc"), "kickoff_utc"),
        "phase": record.get("phase"),
        "probabilities": probabilities,
        "prediction_cutoff": _required_text(
            record.get("prediction_timestamp"), "prediction_timestamp"
        ),
        "updated_at": _required_text(record.get("prediction_timestamp"), "prediction_timestamp"),
    }


def build_live_public_nations_league(
    source_records: Iterable[Mapping[str, Any]],
    *,
    active_release: ModelRelease,
    evidence_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Project immutable source records as a deterministic, non-betting LIVE view.

    A refinement record replaces its initial counterpart for the public fixture
    view.  Both source record IDs remain in ``audit_history``; probabilities are
    copied, never recalculated.
    """

    release = _validate_release(active_release)
    bindings = _binding_index(evidence_binding)
    parsed: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    for raw in source_records:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLivePublicError("source record is malformed")
        record = dict(raw)
        record_id = _digest_text(record.get("record_id"), "record_id")
        if record_id in seen_records:
            raise NationsLeagueLivePublicError("source record is duplicated")
        seen_records.add(record_id)
        binding = bindings.get(record_id)
        if binding is None:
            raise NationsLeagueLivePublicError("source record is absent from evidence binding")
        if (
            binding["training_data_digest"] != release["training_data_digest"]
            or binding["trained_state_digest"] != release["trained_state_digest"]
        ):
            raise NationsLeagueLivePublicError("source record does not bind to active state")
        parsed.append(_source_fixture(record, binding))
    if set(bindings) != seen_records:
        raise NationsLeagueLivePublicError("LIVE evidence binding coverage is incomplete")
    selected: dict[str, dict[str, Any]] = {}
    audit: dict[str, list[str]] = {}
    for fixture in sorted(parsed, key=lambda item: (item["fixture_id"], item["phase"], item["source_prediction_record_id"])):
        phase = fixture["phase"]
        if phase not in {"initial", "refinement"}:
            raise NationsLeagueLivePublicError("source lifecycle phase is unsupported")
        fixture_id = fixture["fixture_id"]
        audit.setdefault(fixture_id, []).append(fixture["source_prediction_record_id"])
        current = selected.get(fixture_id)
        if current is None or (current["phase"] == "initial" and phase == "refinement"):
            selected[fixture_id] = fixture
        elif current["phase"] == phase:
            raise NationsLeagueLivePublicError("duplicate source lifecycle record")
    fixtures = [selected[key] for key in sorted(selected)]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "competition": COMPETITION,
        "status": "LIVE",
        "experimental": True,
        "publication_enabled": True,
        "no_bet": True,
        "betting_enabled": False,
        "ledger_mutation": False,
        "model_release": release,
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
        "audit_history": [
            {"fixture_id": fixture_id, "source_prediction_record_ids": audit[fixture_id]}
            for fixture_id in sorted(audit)
        ],
    }
    payload["updated_at"] = max(
        fixture["updated_at"] for fixture in fixtures
    ) if fixtures else release["training_cutoff"]
    payload["public_digest"] = _digest(payload)
    return payload


def validate_live_public_nations_league(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a built LIVE projection without accepting hidden authority."""

    if not isinstance(value, Mapping):
        raise NationsLeagueLivePublicError("LIVE public payload is malformed")
    payload = deepcopy(dict(value))
    if set(payload) != _PUBLIC_KEYS:
        raise NationsLeagueLivePublicError("LIVE public fields are not allowlisted")
    digest = payload.pop("public_digest", None)
    if not isinstance(digest, str) or digest != _digest(payload):
        raise NationsLeagueLivePublicError("LIVE public digest mismatch")
    if (
        payload.get("schema") != SCHEMA
        or payload.get("competition") != COMPETITION
        or payload.get("status") != "LIVE"
        or payload.get("publication_enabled") is not True
        or payload.get("no_bet") is not True
        or payload.get("betting_enabled") is not False
        or payload.get("ledger_mutation") is not False
        or payload.get("experimental") is not True
    ):
        raise NationsLeagueLivePublicError("LIVE public safety contract is invalid")
    updated_at = _required_text(payload.get("updated_at"), "updated_at")
    model_release = payload.get("model_release")
    if not isinstance(model_release, Mapping) or set(model_release) != _RELEASE_KEYS:
        raise NationsLeagueLivePublicError("LIVE model release is missing")
    if (
        model_release.get("model_family") != MODEL_FAMILY
        or model_release.get("model_version") != MODEL_VERSION
        or model_release.get("algorithm_digest") != FROZEN_ALGORITHM_DIGEST
    ):
        raise NationsLeagueLivePublicError("LIVE model release identity is invalid")
    for field in ("release_id", "algorithm_digest", "training_data_digest", "trained_state_digest", "binding_digest"):
        _digest_text(model_release.get(field), f"model_release.{field}")
    if payload.get("fixture_count") != len(payload.get("fixtures", [])):
        raise NationsLeagueLivePublicError("LIVE public fixture count is invalid")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise NationsLeagueLivePublicError("LIVE public fixture coverage is incomplete")
    identities: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, Mapping) or set(fixture) != _PUBLIC_FIXTURE_KEYS:
            raise NationsLeagueLivePublicError("LIVE fixture fields are not allowlisted")
        fixture_id = _required_text(fixture.get("fixture_id"), "fixture_id")
        if fixture_id in identities:
            raise NationsLeagueLivePublicError("LIVE fixture identity is duplicated")
        identities.add(fixture_id)
        if fixture.get("competition") != COMPETITION:
            raise NationsLeagueLivePublicError("LIVE fixture competition is invalid")
        if fixture.get("phase") not in {"initial", "refinement"}:
            raise NationsLeagueLivePublicError("LIVE fixture phase is invalid")
        _required_text(fixture.get("kickoff_utc"), "kickoff_utc")
        _required_text(fixture.get("prediction_cutoff"), "prediction_cutoff")
        _required_text(fixture.get("updated_at"), "updated_at")
        _probabilities(fixture.get("probabilities"))
        for identity in ("source_identity", "canonical_identity"):
            item = fixture.get(identity)
            if not isinstance(item, Mapping):
                raise NationsLeagueLivePublicError(f"LIVE {identity} is missing")
            _required_text(item.get("home_team"), f"{identity}.home_team")
            _required_text(item.get("away_team"), f"{identity}.away_team")
    if updated_at != max(fixture["updated_at"] for fixture in fixtures):
        raise NationsLeagueLivePublicError("LIVE updated_at is not deterministic")
    return dict(value)
