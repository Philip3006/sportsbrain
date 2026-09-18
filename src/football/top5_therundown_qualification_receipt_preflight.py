"""Offline B1 qualification-to-Builder-2 receipt preflight.

This module is a read-only compatibility seam.  It consumes a complete B1
qualification envelope and independently supplied controlled-shadow bindings,
then projects the exact per-observation fields that the canonical Builder-2
receipt issuer needs.  It never issues a receipt, changes provider authority,
or activates any downstream consumer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite

from src.football.production_contracts import ProductionContractError
from src.football.provider_cascade.contracts import MARKET_PREMATCH_1X2
from src.football.top5_builder2_qualification_receipt import (
    RECEIPT_SCHEMA_VERSION,
    semantic_digest,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    TOP5_LEAGUES,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_qualification import (
    EVIDENCE_SCHEMA_VERSION,
    THERUNDOWN_PROVIDER_IDENTITY,
    TheRundownQualificationReport,
    TheRundownQualificationStatus,
    evaluate_therundown_qualification,
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_PREMATCH_1X2 = MARKET_PREMATCH_1X2
_PREMATCH = "PRE_MATCH"
_REAL_OBSERVED = "REAL_OBSERVED"
_RECEIPT_STATUS = "REAL_OBSERVATION_VALIDATED"
_AUTHORIZATION_FIELDS = frozenset(
    {
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "provider_identity",
        "canonical_league",
        "fixture_key",
        "provider_event_id",
        "provider_request_id",
        "provider_scope",
        "league_scope",
        "fixture_scope",
        "network_execution",
        "no_bet",
        "publication_enabled",
        "monetary_spend_authorized",
    }
)
_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "controlled_shadow_run_id",
        "ceo_authorization_id",
        "qualification_session_id",
        "provider_identity",
        "fixture_key",
        "provider_event_id",
        "provider_request_id",
        "adapter_version",
        "adapter_source_sha",
        "cascade_evidence_digest",
        "raw_response_digest",
        "normalized_record_digest",
        "captured_at",
        "network_execution",
        "no_bet",
        "publication",
        "monetary_spend_authorized",
    }
)
_BINDING_FIELDS = (
    "qualification_report_identity",
    "qualification_report_digest",
    "qualification_result_digest",
    "qualification_status",
    "qualification_session_id",
    "controlled_shadow_run_id",
    "ceo_authorization_id",
    "fixture_key",
    "provider_identity",
    "provider_event_id",
    "provider_request_id",
    "observation_id",
    "observation_digest",
    "normalized_record_digest",
    "cascade_evidence_digest",
    "capture_attestation_digest",
    "adapter_version",
    "adapter_source_sha",
)


class QualificationReceiptPreflightError(ProductionContractError):
    """Malformed or incomplete input at the offline receipt seam."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationReceiptPreflightError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str, *, source: bool = False) -> str:
    value = _text(value, name)
    matcher = _SHA_RE if source else _SHA256_RE
    if matcher.fullmatch(value) is None:
        raise QualificationReceiptPreflightError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QualificationReceiptPreflightError(
                f"{name} must be a valid timestamp"
            ) from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise QualificationReceiptPreflightError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return _datetime(value, "timestamp").isoformat()
    return value


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise QualificationReceiptPreflightError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QualificationReceiptPreflightError(f"{name} must be numeric") from exc
    if not isfinite(number) or number < minimum:
        raise QualificationReceiptPreflightError(f"{name} is outside the allowed range")
    return number


def _state(value: object, name: str, fields: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationReceiptPreflightError(f"{name} is required")
    for field in fields:
        if field not in value:
            raise QualificationReceiptPreflightError(f"{name}.{field} is required")
        if (
            isinstance(value[field], bool)
            or not isinstance(value[field], int)
            or value[field] < 0
        ):
            raise QualificationReceiptPreflightError(f"{name}.{field} is invalid")
    return value


def _validate_quota(evidence: Mapping[str, object]) -> float:
    before = _state(
        evidence.get("quota_state_before"),
        "quota_state_before",
        ("used", "remaining"),
    )
    after = _state(
        evidence.get("quota_state_after"),
        "quota_state_after",
        ("used", "remaining"),
    )
    rate = _state(
        evidence.get("rate_limit_state"),
        "rate_limit_state",
        ("rate_limit", "rate_remaining"),
    )
    if after["used"] < before["used"] or after["remaining"] > before["remaining"]:
        raise QualificationReceiptPreflightError("quota state is not monotonic")
    if rate["rate_remaining"] > rate["rate_limit"]:
        raise QualificationReceiptPreflightError("rate-limit state is inconsistent")
    cost = _number(evidence.get("quota_cost_units"), "quota_cost_units")
    if float(after["used"] - before["used"]) != cost:
        raise QualificationReceiptPreflightError(
            "quota cost does not match usage delta"
        )
    return cost


def _validate_authorization(
    evidence: Mapping[str, object],
) -> Mapping[str, object]:
    raw = evidence.get("authorization_metadata")
    if not isinstance(raw, Mapping) or set(raw) != _AUTHORIZATION_FIELDS:
        raise QualificationReceiptPreflightError("authorization metadata is incomplete")
    expected = {
        "provider_identity": evidence.get("provider_identity"),
        "canonical_league": evidence.get("canonical_league"),
        "fixture_key": evidence.get("fixture_key"),
        "provider_event_id": evidence.get("provider_event_id"),
        "provider_request_id": evidence.get("provider_request_id"),
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise QualificationReceiptPreflightError("authorization binding mismatch")
    for name in (
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
    ):
        _text(raw.get(name), name)
    scopes = {
        "provider_scope": evidence.get("provider_identity"),
        "league_scope": evidence.get("canonical_league"),
        "fixture_scope": evidence.get("fixture_key"),
    }
    for field, expected_value in scopes.items():
        value = raw.get(field)
        if (
            isinstance(value, (str, bytes))
            or not isinstance(value, Sequence)
            or not value
            or expected_value not in value
        ):
            raise QualificationReceiptPreflightError(f"authorization {field} is unsafe")
    if raw.get("network_execution") is not True:
        raise QualificationReceiptPreflightError(
            "real evidence requires network execution"
        )
    if raw.get("no_bet") is not True or raw.get("publication_enabled") is not False:
        raise QualificationReceiptPreflightError(
            "authorization safety flags are unsafe"
        )
    if raw.get("monetary_spend_authorized") is not False:
        raise QualificationReceiptPreflightError("monetary spend is forbidden")
    return raw


def _validate_attestation(
    evidence: Mapping[str, object],
    binding: Mapping[str, object],
    authorization: Mapping[str, object],
) -> tuple[Mapping[str, object], str, str]:
    if not isinstance(binding, Mapping):
        raise QualificationReceiptPreflightError("shadow binding is invalid")
    if set(binding) != {
        "observation_digest",
        "cascade_evidence_digest",
        "capture_attestation",
    }:
        raise QualificationReceiptPreflightError("shadow binding schema is incomplete")
    observation_digest = _sha(binding.get("observation_digest"), "observation_digest")
    cascade_digest = _sha(
        binding.get("cascade_evidence_digest"), "cascade_evidence_digest"
    )
    raw = binding.get("capture_attestation")
    if not isinstance(raw, Mapping) or set(raw) != _ATTESTATION_FIELDS:
        raise QualificationReceiptPreflightError("capture attestation is incomplete")
    if raw.get("schema_version") != CAPTURE_ATTESTATION_CONTRACT_VERSION:
        raise QualificationReceiptPreflightError(
            "unsupported capture attestation schema"
        )
    for field in (
        "controlled_shadow_run_id",
        "ceo_authorization_id",
        "qualification_session_id",
        "provider_identity",
        "fixture_key",
        "provider_event_id",
        "provider_request_id",
        "adapter_version",
    ):
        _text(raw.get(field), f"capture_attestation.{field}")
    bindings = {
        "controlled_shadow_run_id": authorization["controlled_shadow_run_id"],
        "ceo_authorization_id": authorization["ceo_authorization_id"],
        "qualification_session_id": authorization["qualification_session_id"],
        "provider_identity": evidence["provider_identity"],
        "fixture_key": evidence["fixture_key"],
        "provider_event_id": evidence["provider_event_id"],
        "provider_request_id": evidence["provider_request_id"],
        "adapter_version": evidence["adapter_version"],
    }
    if any(raw.get(field) != value for field, value in bindings.items()):
        raise QualificationReceiptPreflightError("capture attestation binding mismatch")
    if (
        raw.get("adapter_source_sha", "").lower()
        != str(evidence["adapter_source_sha"]).lower()
    ):
        raise QualificationReceiptPreflightError("capture adapter source mismatch")
    if raw.get("cascade_evidence_digest", "").lower() != cascade_digest:
        raise QualificationReceiptPreflightError("capture cascade digest mismatch")
    if (
        raw.get("raw_response_digest", "").lower()
        != str(evidence["raw_record_digest"]).lower()
    ):
        raise QualificationReceiptPreflightError("capture raw digest mismatch")
    if (
        raw.get("normalized_record_digest", "").lower()
        != str(evidence["normalized_record_digest"]).lower()
    ):
        raise QualificationReceiptPreflightError("capture normalized digest mismatch")
    if _datetime(
        raw.get("captured_at"), "capture_attestation.captured_at"
    ) != _datetime(evidence.get("captured_at"), "captured_at"):
        raise QualificationReceiptPreflightError("capture time binding mismatch")
    if raw.get("network_execution") is not True:
        raise QualificationReceiptPreflightError(
            "capture attestation is not independent real evidence"
        )
    if raw.get("no_bet") is not True or raw.get("publication") is not False:
        raise QualificationReceiptPreflightError("capture safety flags are unsafe")
    if raw.get("monetary_spend_authorized") is not False:
        raise QualificationReceiptPreflightError("capture monetary spend is forbidden")
    capture_digest = _digest(raw)
    return raw, observation_digest, capture_digest


def _validate_evidence(
    evidence: Mapping[str, object],
    *,
    maximum_odds_age_seconds: int,
) -> Mapping[str, object]:
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise QualificationReceiptPreflightError(
            "unsupported qualification evidence schema"
        )
    if evidence.get("provider_identity") != THERUNDOWN_PROVIDER_IDENTITY:
        raise QualificationReceiptPreflightError("provider identity mismatch")
    if evidence.get("evidence_kind") != _REAL_OBSERVED:
        raise QualificationReceiptPreflightError(
            "non-real evidence cannot enter preflight"
        )
    if evidence.get("synthetic_reconstruction") is not False:
        raise QualificationReceiptPreflightError(
            "synthetic evidence cannot enter preflight"
        )
    if evidence.get("network_request_count") != 1:
        raise QualificationReceiptPreflightError("unsafe network request count")
    league = _text(evidence.get("canonical_league"), "canonical_league")
    if league not in TOP5_LEAGUES:
        raise QualificationReceiptPreflightError("league is outside the Top-5 scope")
    _text(evidence.get("evidence_id"), "evidence_id")
    _text(evidence.get("fixture_key"), "fixture_key")
    _text(evidence.get("provider_event_id"), "provider_event_id")
    _text(evidence.get("provider_request_id"), "provider_request_id")
    home = _text(evidence.get("home_team"), "home_team")
    away = _text(evidence.get("away_team"), "away_team")
    kickoff = _datetime(evidence.get("kickoff"), "kickoff")
    if (
        evidence.get("fixture_observed") is not True
        or evidence.get("home_away_identity_verified") is not True
    ):
        raise QualificationReceiptPreflightError(
            "fixture or participant identity is unverified"
        )
    if (
        evidence["fixture_key"].casefold()
        != make_fixture_key(league, home, away, kickoff).casefold()
    ):
        raise QualificationReceiptPreflightError("fixture participant binding mismatch")
    if (
        evidence.get("market_type") != _PREMATCH_1X2
        or evidence.get("market_phase") != _PREMATCH
    ):
        raise QualificationReceiptPreflightError("pre-match regulation 1X2 is required")
    odds = evidence.get("odds")
    if not isinstance(odds, Mapping) or any(
        _number(odds.get(outcome), f"odds.{outcome}", minimum=1.0000001) <= 1.0
        for outcome in ("home", "draw", "away")
    ):
        raise QualificationReceiptPreflightError("complete 1X2 prices are required")
    _text(evidence.get("bookmaker_identity"), "bookmaker_identity")
    source = _datetime(evidence.get("source_timestamp"), "source_timestamp")
    captured = _datetime(evidence.get("captured_at"), "captured_at")
    if evidence.get("source_timing_provenance") != "SOURCE_TIMESTAMP":
        raise QualificationReceiptPreflightError(
            "source timestamp provenance is required"
        )
    age = (captured - source).total_seconds()
    if age < 0 or age > maximum_odds_age_seconds or captured >= kickoff:
        raise QualificationReceiptPreflightError("odds are stale or not pre-match")
    _text(evidence.get("source_provenance"), "source_provenance")
    _sha(evidence.get("raw_record_digest"), "raw_record_digest")
    _sha(evidence.get("provider_record_digest"), "provider_record_digest")
    _sha(evidence.get("normalized_record_digest"), "normalized_record_digest")
    _text(evidence.get("adapter_version"), "adapter_version")
    _sha(evidence.get("adapter_source_sha"), "adapter_source_sha", source=True)
    if evidence.get("provider_status") != "AVAILABLE" or evidence.get(
        "failure_codes"
    ) not in ([], ()):
        raise QualificationReceiptPreflightError(
            "provider failure cannot enter preflight"
        )
    _validate_quota(evidence)
    _validate_authorization(evidence)
    return evidence


@dataclass(frozen=True)
class Builder2QualificationReceiptPreflightV1:
    """One complete, non-issued Builder-2 receipt input projection."""

    evidence_id: str
    receipt_input: Mapping[str, object]
    source_evidence: Mapping[str, object]

    @property
    def receipt_eligible(self) -> bool:
        return True

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_eligible": True,
            "issuer_present": False,
            "evidence_id": self.evidence_id,
            "required_binding_fields": list(_BINDING_FIELDS),
            "receipt_input": dict(self.receipt_input),
            "source_evidence": dict(self.source_evidence),
            "receipt_issuer_fields": ["qualification_receipt_id", "receipt_digest"],
            "authority_changed": False,
            "activation": False,
            "publication": False,
            "no_bet": True,
        }


def build_builder2_qualification_receipt_preflight(
    qualification_report: TheRundownQualificationReport,
    qualification_envelope: Mapping[str, object],
    *,
    shadow_bindings: Mapping[str, Mapping[str, object]],
) -> tuple[Builder2QualificationReceiptPreflightV1, ...]:
    """Project accepted B1 evidence into exact per-observation receipt inputs.

    ``shadow_bindings`` is independently supplied and must contain one binding
    for every accepted B1 evidence ID.  This function never calls the canonical
    Builder-2 issuer; the issuer remains the only component allowed to create a
    receipt.
    """

    if not isinstance(qualification_envelope, Mapping):
        raise QualificationReceiptPreflightError("qualification envelope is required")
    if not isinstance(shadow_bindings, Mapping):
        raise QualificationReceiptPreflightError("shadow bindings are required")
    if qualification_report.provider_identity != THERUNDOWN_PROVIDER_IDENTITY:
        raise QualificationReceiptPreflightError("qualification provider mismatch")
    if (
        qualification_report.status
        is not TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    ):
        raise QualificationReceiptPreflightError("all five leagues must qualify")
    if len(qualification_report.leagues) != len(TOP5_LEAGUES) or {
        league.league for league in qualification_report.leagues
    } != set(TOP5_LEAGUES):
        raise QualificationReceiptPreflightError("all five Top-5 leagues are required")
    if any(
        league.status is not TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
        or not league.qualified_evidence_ids
        for league in qualification_report.leagues
    ):
        raise QualificationReceiptPreflightError(
            "every Top-5 league must have accepted evidence"
        )
    recomputed = evaluate_therundown_qualification(
        qualification_envelope,
        maximum_odds_age_seconds=qualification_report.maximum_odds_age_seconds,
    )
    if recomputed.as_payload() != qualification_report.as_payload():
        raise QualificationReceiptPreflightError(
            "qualification report is not reproducible"
        )
    evidence = qualification_envelope.get("evidence")
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise QualificationReceiptPreflightError(
            "qualification evidence must be a sequence"
        )
    evidence_by_id: dict[str, Mapping[str, object]] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            raise QualificationReceiptPreflightError(
                "qualification evidence item is invalid"
            )
        evidence_id = _text(item.get("evidence_id"), "evidence_id")
        if evidence_id in evidence_by_id:
            raise QualificationReceiptPreflightError("duplicate evidence identity")
        evidence_by_id[evidence_id] = item
    accepted_ids = tuple(
        evidence_id
        for league in qualification_report.leagues
        for evidence_id in league.qualified_evidence_ids
    )
    if len(accepted_ids) != len(set(accepted_ids)) or not accepted_ids:
        raise QualificationReceiptPreflightError(
            "accepted evidence identities are ambiguous"
        )
    if not set(accepted_ids).issubset(evidence_by_id):
        raise QualificationReceiptPreflightError(
            "accepted evidence identity is missing from the envelope"
        )
    if set(shadow_bindings) != set(accepted_ids):
        raise QualificationReceiptPreflightError(
            "every accepted observation needs one attestation"
        )
    report_digest = semantic_digest(qualification_report.as_payload())
    preflight: list[Builder2QualificationReceiptPreflightV1] = []
    common_run: str | None = None
    common_session: str | None = None
    common_ceo: str | None = None
    for evidence_id in accepted_ids:
        item = _validate_evidence(
            evidence_by_id[evidence_id],
            maximum_odds_age_seconds=qualification_report.maximum_odds_age_seconds,
        )
        authorization = _validate_authorization(item)
        attestation, observation_digest, capture_digest = _validate_attestation(
            item, shadow_bindings[evidence_id], authorization
        )
        run_id = _text(
            authorization["controlled_shadow_run_id"], "controlled_shadow_run_id"
        )
        session_id = _text(
            authorization["qualification_session_id"], "qualification_session_id"
        )
        ceo_id = _text(authorization["ceo_authorization_id"], "ceo_authorization_id")
        if common_run is None:
            common_run, common_session, common_ceo = run_id, session_id, ceo_id
        elif (run_id, session_id, ceo_id) != (common_run, common_session, common_ceo):
            raise QualificationReceiptPreflightError(
                "shadow run/session/authorization mismatch"
            )
        result_payload = {
            "observation_id": item["observation_id"],
            "provider_identity": item["provider_identity"],
            "league": item["canonical_league"],
            "status": _RECEIPT_STATUS,
            "accepted": True,
            "real_observed": True,
            "failure_codes": [],
            "cascade_errors": [],
            "cascade_network_request_count": item["network_request_count"],
            "cascade_quota_units": item["quota_cost_units"],
        }
        result_digest = semantic_digest(
            {
                "qualification_report_digest": report_digest,
                "observation_id": item["observation_id"],
                "observation_digest": observation_digest,
                "result": result_payload,
            }
        )
        receipt_input = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "qualification_report_identity": f"{session_id}:{_RECEIPT_STATUS}",
            "qualification_report_digest": report_digest,
            "qualification_result_digest": result_digest,
            "qualification_status": _RECEIPT_STATUS,
            "qualification_session_id": session_id,
            "controlled_shadow_run_id": run_id,
            "ceo_authorization_id": ceo_id,
            "fixture_key": item["fixture_key"],
            "provider_identity": item["provider_identity"],
            "provider_event_id": item["provider_event_id"],
            "provider_request_id": item["provider_request_id"],
            "observation_id": item["observation_id"],
            "observation_digest": observation_digest,
            "normalized_record_digest": item["normalized_record_digest"],
            "cascade_evidence_digest": shadow_bindings[evidence_id][
                "cascade_evidence_digest"
            ],
            "capture_attestation_digest": capture_digest,
            "adapter_version": item["adapter_version"],
            "adapter_source_sha": item["adapter_source_sha"],
            "accepted": True,
            "prediction_input_allowed": True,
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
            "failure_codes": [],
        }
        source_evidence = {
            "evidence": dict(item),
            "authorization_metadata": dict(authorization),
            "capture_attestation": dict(attestation),
            "bookmaker_identity": item["bookmaker_identity"],
            "odds": dict(item["odds"]),
            "source_timestamp": item["source_timestamp"],
            "captured_at": item["captured_at"],
            "quota_state_before": dict(item["quota_state_before"]),
            "quota_state_after": dict(item["quota_state_after"]),
            "rate_limit_state": dict(item["rate_limit_state"]),
            "cascade_evidence_digest": shadow_bindings[evidence_id][
                "cascade_evidence_digest"
            ],
        }
        preflight.append(
            Builder2QualificationReceiptPreflightV1(
                evidence_id=evidence_id,
                receipt_input=receipt_input,
                source_evidence=source_evidence,
            )
        )
    return tuple(preflight)


__all__ = [
    "Builder2QualificationReceiptPreflightV1",
    "QualificationReceiptPreflightError",
    "build_builder2_qualification_receipt_preflight",
]
