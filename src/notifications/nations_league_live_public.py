"""Stable, non-financial public projection for a live Nations League release."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from src.models.live_model_lifecycle import validate_release

SCHEMA = "nations-league-live-public-v1"
_SHA = set("0123456789abcdef")


class NationsLeagueLivePublicError(ValueError):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _sha(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in _SHA for c in value)
    ):
        raise NationsLeagueLivePublicError(f"{field} must be a SHA-256 digest")
    return value


def build_live_public_nations_league(
    reference: Mapping[str, Any], release: Mapping[str, Any], *, generated_at: str
) -> dict[str, Any]:
    active = validate_release(release)
    if active["model_family"] != "nations_league_v1_1":
        raise NationsLeagueLivePublicError("non-Nations-League release")
    if (
        reference.get("publication_eligible") is not True
        or reference.get("no_bet") is not True
    ):
        raise NationsLeagueLivePublicError("live reference lacks safe public binding")
    if (
        reference.get("active_release_id") != active["release_id"]
        or reference.get("state_digest") != active["state_digest"]
    ):
        raise NationsLeagueLivePublicError("reference does not bind active release")
    for field in ("source_record_digest", "algorithm_digest", "state_digest"):
        _sha(reference.get(field), field)
    payload = {
        "schema": SCHEMA,
        "lifecycle": "LIVE_MODEL",
        "model_family": active["model_family"],
        "release_id": active["release_id"],
        "algorithm_digest": active["algorithm_digest"],
        "state_digest": active["state_digest"],
        "source_record_id": reference.get("source_record_id"),
        "source_record_digest": reference["source_record_digest"],
        "source_lifecycle": reference.get("source_lifecycle"),
        "generated_at": generated_at,
        "no_bet": True,
        "betting_authorized": False,
        "publication_eligible": True,
    }
    if (
        not isinstance(payload["source_record_id"], str)
        or not payload["source_record_id"]
        or payload["source_lifecycle"] not in {"INITIAL", "REFINEMENT"}
    ):
        raise NationsLeagueLivePublicError("source record binding is invalid")
    payload["public_digest"] = _digest(payload)
    return payload


def validate_live_public_nations_league(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "lifecycle",
        "model_family",
        "release_id",
        "algorithm_digest",
        "state_digest",
        "source_record_id",
        "source_record_digest",
        "source_lifecycle",
        "generated_at",
        "no_bet",
        "betting_authorized",
        "publication_eligible",
        "public_digest",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise NationsLeagueLivePublicError("live public fields are not allowlisted")
    body = dict(value)
    digest = body.pop("public_digest")
    if (
        body.get("schema") != SCHEMA
        or body.get("lifecycle") != "LIVE_MODEL"
        or body.get("model_family") != "nations_league_v1_1"
    ):
        raise NationsLeagueLivePublicError("unsupported live public contract")
    if (
        body.get("no_bet") is not True
        or body.get("betting_authorized") is not False
        or body.get("publication_eligible") is not True
    ):
        raise NationsLeagueLivePublicError("live public financial boundary is invalid")
    for field in (
        "release_id",
        "algorithm_digest",
        "state_digest",
        "source_record_digest",
    ):
        _sha(body.get(field), field)
    if (
        not isinstance(body.get("source_record_id"), str)
        or not body["source_record_id"]
    ):
        raise NationsLeagueLivePublicError("source record identity is invalid")
    if (
        body.get("source_lifecycle") not in {"INITIAL", "REFINEMENT"}
        or _digest(body) != digest
    ):
        raise NationsLeagueLivePublicError("live public digest or stage is invalid")
    return dict(value)
