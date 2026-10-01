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
from datetime import datetime, timedelta, timezone
from typing import Any

from src.analysis.nations_league_live_edge import (
    NationsLeagueLiveEdgeError,
    build_edge_analysis,
    validate_edge_analysis,
)
from src.analysis.nations_league_live_market_enrichment import (
    NationsLeagueLiveMarketEnrichmentError,
    select_market_enrichment,
    validate_market_enrichment,
)
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
        "model_release",
    }
)
_PUBLIC_FIXTURE_OPTIONAL_KEYS = frozenset({"edge_analysis"})
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


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLivePublicError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueLivePublicError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise NationsLeagueLivePublicError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _zero_view_updated_at(
    selected: Mapping[str, Mapping[str, Any]], release: Mapping[str, Any]
) -> str:
    """Return the immutable semantic timestamp for an empty current view."""

    timestamps = [_utc(release["training_cutoff"], "training_cutoff")]
    for fixture in selected.values():
        timestamps.extend(
            [
                _utc(fixture["updated_at"], "fixture.updated_at"),
                _utc(fixture["kickoff_utc"], "fixture.kickoff_utc"),
            ]
        )
    return _stamp(max(timestamps))


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NationsLeagueLivePublicError(
            "LIVE public value is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _public_canonical(value: Any) -> str:
    """Match JSON.stringify number spelling for Worker/PWA digest parity."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        try:
            number = float(value)
        except OverflowError as exc:
            raise NationsLeagueLivePublicError(
                "public bundle contains a number outside binary64"
            ) from exc
        return _ecmascript_number(number)
    if isinstance(value, float):
        return _ecmascript_number(value)
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
    raise NationsLeagueLivePublicError("public bundle contains an unsupported value")


def _ecmascript_number(value: float) -> str:
    """Serialize one finite binary64 value using ECMAScript thresholds.

    Python and ECMAScript both use the shortest round-tripping decimal for a
    binary64 value.  Their presentation thresholds differ: ECMAScript uses
    fixed notation for ``1e-6 <= abs(value) < 1e21`` and scientific notation
    outside that interval.  Reformatting Python's shortest representation at
    those boundaries also normalizes signed zero and exponent spelling to the
    native JSON.stringify form.
    """

    if not math.isfinite(value):
        raise NationsLeagueLivePublicError("public bundle contains a non-finite number")
    if value == 0:
        return "0"

    text = repr(float(value))
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    decimal_index = len(whole) + exponent
    digits = digits.rstrip("0")
    absolute = abs(value)

    if 1e-6 <= absolute < 1e21:
        if decimal_index <= 0:
            body = "0." + ("0" * -decimal_index) + digits
        elif decimal_index >= len(digits):
            body = digits + ("0" * (decimal_index - len(digits)))
        else:
            body = digits[:decimal_index] + "." + digits[decimal_index:]
        return sign + body

    scientific_exponent = decimal_index - 1
    coefficient = digits[0]
    if len(digits) > 1:
        coefficient += "." + digits[1:]
    exponent_marker = (
        f"e+{scientific_exponent}"
        if scientific_exponent >= 0
        else f"e{scientific_exponent}"
    )
    return sign + coefficient + exponent_marker


def _public_digest(value: Any) -> str:
    return hashlib.sha256(_public_canonical(value).encode("utf-8")).hexdigest()


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
        record_id = _digest_text(
            row.get("source_prediction_record_id"), "source_prediction_record_id"
        )
        if record_id in index:
            raise NationsLeagueLivePublicError(
                "LIVE evidence binding duplicates a record"
            )
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
    if expected != _digest(
        {
            key: value
            for key, value in binding_manifest.items()
            if key != "binding_digest"
        }
    ):
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


def _source_fixture(
    record: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    edge_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    is_live = record.get("status") == "LIVE"
    if is_live:
        valid = (
            record.get("record_type") == "prediction"
            and record.get("competition") == COMPETITION
            and record.get("model_version") == MODEL_VERSION
            and record.get("model_digest") == FROZEN_ALGORITHM_DIGEST
            and record.get("no_bet") is True
            and record.get("betting_enabled") is False
            and record.get("publication_enabled") is True
            and record.get("ledger_mutation") is False
        )
    else:
        valid = (
            record.get("record_type") == "prediction"
            and record.get("competition") == COMPETITION
            and record.get("model_version") == MODEL_VERSION
            and record.get("model_digest") == FROZEN_ALGORITHM_DIGEST
            and record.get("shadow") is True
            and record.get("no_bet") is True
            and record.get("publication_enabled") is False
            and record.get("ledger_mutation") is False
        )
    if not valid:
        raise NationsLeagueLivePublicError(
            "source record violates frozen evidence safety"
        )
    record_id = _digest_text(record.get("record_id"), "record_id")
    if not is_live:
        if record_id != binding["source_prediction_record_id"]:
            raise NationsLeagueLivePublicError(
                "source record identity differs from binding"
            )
        input_provenance = record.get("input_provenance")
        if (
            not isinstance(input_provenance, Mapping)
            or input_provenance.get("input_snapshot_digest")
            != binding["input_snapshot_digest"]
        ):
            raise NationsLeagueLivePublicError(
                "source record input-state binding differs"
            )
    probabilities = _probabilities(record.get("probabilities"))
    source_identity = record.get("source_identity")
    if source_identity is not None:
        if not isinstance(source_identity, Mapping):
            raise NationsLeagueLivePublicError("source identity is malformed")
        source_home = _required_text(
            source_identity.get("home_team"), "source home_team"
        )
        source_away = _required_text(
            source_identity.get("away_team"), "source away_team"
        )
    else:
        source_home = _required_text(record.get("home_team"), "home_team")
        source_away = _required_text(record.get("away_team"), "away_team")
    identity = record.get("model_identity")
    canonical_identity = record.get("canonical_identity")
    if canonical_identity is not None:
        if not isinstance(canonical_identity, Mapping):
            raise NationsLeagueLivePublicError("canonical identity is malformed")
        canonical_home = _required_text(
            canonical_identity.get("home_team"), "canonical home_team"
        )
        canonical_away = _required_text(
            canonical_identity.get("away_team"), "canonical away_team"
        )
    else:
        canonical_home = source_home
        canonical_away = source_away
    if identity is not None:
        if not isinstance(identity, Mapping):
            raise NationsLeagueLivePublicError("source model identity is malformed")
        if canonical_identity is None:
            canonical_home = _required_text(
                identity.get("home_team"), "canonical home_team"
            )
            canonical_away = _required_text(
                identity.get("away_team"), "canonical away_team"
            )
    prediction_timestamp = _required_text(
        record.get("prediction_timestamp"), "prediction_timestamp"
    )
    edge = edge_override if edge_override is not None else record.get("edge_analysis")
    if edge is None:
        edge_release_id = record.get("model_release_id") or binding.get(
            "model_release", {}
        ).get("release_id")
        edge = build_edge_analysis(
            probabilities,
            fixture_id=_required_text(record.get("fixture_id"), "fixture_id"),
            phase=_required_text(record.get("phase"), "phase"),
            model_release_id=_required_text(edge_release_id, "model_release_id"),
            prediction_record_id=record_id,
            prediction_timestamp=prediction_timestamp,
        )
    try:
        edge = validate_edge_analysis(
            edge,
            fixture_id=_required_text(record.get("fixture_id"), "fixture_id"),
            prediction_record_id=record_id,
        )
    except NationsLeagueLiveEdgeError as exc:
        raise NationsLeagueLivePublicError(str(exc)) from exc
    return {
        "fixture_id": _required_text(record.get("fixture_id"), "fixture_id"),
        "competition": COMPETITION,
        "source_prediction_record_id": record_id,
        "source_identity": {"home_team": source_home, "away_team": source_away},
        "canonical_identity": {
            "home_team": canonical_home,
            "away_team": canonical_away,
        },
        "kickoff_utc": _required_text(record.get("kickoff_utc"), "kickoff_utc"),
        "phase": record.get("phase"),
        "probabilities": probabilities,
        "prediction_cutoff": prediction_timestamp,
        "updated_at": prediction_timestamp,
        "model_release": dict(binding.get("model_release", {})),
        "edge_analysis": edge,
    }


def _release_binding(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RELEASE_KEYS:
        raise NationsLeagueLivePublicError("fixture model release is missing")
    result = dict(value)
    if (
        result.get("model_family") != MODEL_FAMILY
        or result.get("model_version") != MODEL_VERSION
        or result.get("algorithm_digest") != FROZEN_ALGORITHM_DIGEST
    ):
        raise NationsLeagueLivePublicError("fixture model release identity is invalid")
    for field in (
        "release_id",
        "algorithm_digest",
        "training_data_digest",
        "trained_state_digest",
        "binding_digest",
    ):
        _digest_text(result.get(field), f"fixture.model_release.{field}")
    expected_binding = _digest(
        {
            "release_id": result["release_id"],
            "algorithm_digest": result["algorithm_digest"],
            "training_data_digest": result["training_data_digest"],
            "trained_state_digest": result["trained_state_digest"],
        }
    )
    if result["binding_digest"] != expected_binding:
        raise NationsLeagueLivePublicError("fixture model release binding is invalid")
    _required_text(
        result.get("training_cutoff"), "fixture.model_release.training_cutoff"
    )
    return result


def build_live_public_nations_league(
    source_records: Iterable[Mapping[str, Any]],
    *,
    active_release: ModelRelease,
    evidence_binding: Mapping[str, Any],
    as_of: str,
    market_enrichments: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Project immutable source records as a deterministic, non-betting LIVE view.

    A refinement record replaces its initial counterpart for the public fixture
    view.  Both source record IDs remain in ``audit_history``; probabilities are
    copied, never recalculated.
    """

    release = _validate_release(active_release)
    cutoff = _utc(as_of, "as_of")
    bindings = _binding_index(evidence_binding)
    enrichment_rows = list(market_enrichments)
    source_rows = list(source_records)
    records_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in source_rows:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLivePublicError("source record is malformed")
        record_id = _digest_text(raw.get("record_id"), "record_id")
        if record_id in records_by_id:
            raise NationsLeagueLivePublicError("source record is duplicated")
        records_by_id[record_id] = raw
    for enrichment in enrichment_rows:
        if not isinstance(enrichment, Mapping):
            raise NationsLeagueLivePublicError("market enrichment is malformed")
        record_id = _digest_text(
            enrichment.get("prediction_record_id"),
            "market enrichment prediction_record_id",
        )
        record = records_by_id.get(record_id)
        if record is None:
            raise NationsLeagueLivePublicError(
                "market enrichment has no source prediction"
            )
        try:
            validate_market_enrichment(enrichment, record=record)
        except NationsLeagueLiveMarketEnrichmentError as exc:
            raise NationsLeagueLivePublicError(str(exc)) from exc
    parsed: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    for raw in source_rows:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLivePublicError("source record is malformed")
        record = dict(raw)
        record_id = _digest_text(record.get("record_id"), "record_id")
        if record_id in seen_records:
            raise NationsLeagueLivePublicError("source record is duplicated")
        seen_records.add(record_id)
        binding = bindings.get(record_id)
        if binding is None:
            if record.get("status") != "LIVE":
                raise NationsLeagueLivePublicError(
                    "source record is absent from evidence binding"
                )
            generated = {
                "model_family": MODEL_FAMILY,
                "model_version": MODEL_VERSION,
                "algorithm_digest": record.get("algorithm_digest"),
                "release_id": record.get("model_release_id"),
                "training_data_digest": record.get("training_data_digest"),
                "trained_state_digest": record.get("trained_state_digest"),
                "training_cutoff": record.get("training_cutoff"),
            }
            generated["binding_digest"] = _digest(
                {
                    key: generated[key]
                    for key in (
                        "release_id",
                        "algorithm_digest",
                        "training_data_digest",
                        "trained_state_digest",
                    )
                }
            )
            binding = {
                "model_release": generated,
                "source_prediction_record_id": record_id,
            }
        elif "model_release" not in binding:
            binding = dict(binding)
            binding["model_release"] = evidence_binding.get("active_model_release")
        binding["model_release"] = _release_binding(binding.get("model_release"))
        try:
            enrichment = select_market_enrichment(enrichment_rows, record)
        except NationsLeagueLiveMarketEnrichmentError as exc:
            raise NationsLeagueLivePublicError(str(exc)) from exc
        parsed.append(
            _source_fixture(
                record,
                binding,
                edge_override=(enrichment or {}).get("edge_analysis")
                if enrichment is not None
                else None,
            )
        )
    if not set(bindings).issubset(seen_records):
        raise NationsLeagueLivePublicError(
            "LIVE evidence binding coverage is incomplete"
        )
    parsed_by_record_id = {
        fixture["source_prediction_record_id"]: fixture for fixture in parsed
    }
    selected: dict[str, dict[str, Any]] = {}
    audit: dict[str, list[str]] = {}
    for fixture in sorted(
        parsed,
        key=lambda item: (
            item["fixture_id"],
            item["phase"],
            item["source_prediction_record_id"],
        ),
    ):
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
    fixtures = [
        selected[key]
        for key in sorted(selected)
        if _utc(selected[key]["kickoff_utc"], "kickoff_utc") > cutoff
    ]
    audit_history = [
        {"fixture_id": fixture_id, "source_prediction_record_ids": audit[fixture_id]}
        for fixture_id in sorted(audit)
    ]
    if not fixtures:
        audit_history = [
            {
                **entry,
                "source_record_digests": [
                    _digest(parsed_by_record_id[record_id])
                    for record_id in entry["source_prediction_record_ids"]
                ],
            }
            for entry in audit_history
        ]
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
        "audit_history": audit_history,
    }
    payload["updated_at"] = (
        max(fixture["updated_at"] for fixture in fixtures)
        if fixtures
        else _zero_view_updated_at(selected, release)
    )
    payload["public_digest"] = _public_digest(payload)
    return payload


def validate_live_public_nations_league(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a built LIVE projection without accepting hidden authority."""

    if not isinstance(value, Mapping):
        raise NationsLeagueLivePublicError("LIVE public payload is malformed")
    payload = deepcopy(dict(value))
    if set(payload) != _PUBLIC_KEYS:
        raise NationsLeagueLivePublicError("LIVE public fields are not allowlisted")
    digest = payload.pop("public_digest", None)
    if not isinstance(digest, str) or digest != _public_digest(payload):
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
    for field in (
        "release_id",
        "algorithm_digest",
        "training_data_digest",
        "trained_state_digest",
        "binding_digest",
    ):
        _digest_text(model_release.get(field), f"model_release.{field}")
    fixtures = payload.get("fixtures")
    fixture_count = payload.get("fixture_count")
    if (
        type(fixture_count) is not int
        or fixture_count < 0
        or fixture_count != len(fixtures if isinstance(fixtures, list) else [])
    ):
        raise NationsLeagueLivePublicError("LIVE public fixture count is invalid")
    if not isinstance(fixtures, list):
        raise NationsLeagueLivePublicError("LIVE public fixture coverage is malformed")
    audit_history = payload.get("audit_history")
    if not isinstance(audit_history, list) or not audit_history:
        raise NationsLeagueLivePublicError("LIVE audit history is missing")
    if not fixtures:
        return dict(value)
    if fixture_count < 1:
        raise NationsLeagueLivePublicError("LIVE public fixture coverage is incomplete")
    identities: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, Mapping) or set(fixture) not in {
            _PUBLIC_FIXTURE_KEYS,
            _PUBLIC_FIXTURE_KEYS | _PUBLIC_FIXTURE_OPTIONAL_KEYS,
        }:
            raise NationsLeagueLivePublicError(
                "LIVE fixture fields are not allowlisted"
            )
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
        _release_binding(fixture.get("model_release"))
        _probabilities(fixture.get("probabilities"))
        for identity in ("source_identity", "canonical_identity"):
            item = fixture.get(identity)
            if not isinstance(item, Mapping):
                raise NationsLeagueLivePublicError(f"LIVE {identity} is missing")
            _required_text(item.get("home_team"), f"{identity}.home_team")
            _required_text(item.get("away_team"), f"{identity}.away_team")
        if "edge_analysis" in fixture:
            try:
                validate_edge_analysis(
                    fixture["edge_analysis"],
                    fixture_id=fixture_id,
                    prediction_record_id=fixture["source_prediction_record_id"],
                )
            except NationsLeagueLiveEdgeError as exc:
                raise NationsLeagueLivePublicError(str(exc)) from exc
    if updated_at != max(fixture["updated_at"] for fixture in fixtures):
        raise NationsLeagueLivePublicError("LIVE updated_at is not deterministic")
    return dict(value)
