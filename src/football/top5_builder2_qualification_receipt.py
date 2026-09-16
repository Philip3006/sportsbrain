"""Canonical Builder-2 qualification receipt contract.

This module projects one already accepted PR-#66 qualification result into a
small serialized authority artifact.  It performs no I/O and cannot issue a
provider request, create authorization, bind a model, place a bet, publish,
or activate production.  Builder 1 and Builder 4 consume the receipt; neither
consumer is an issuer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256

from src.football.top5_controlled_shadow_provider_qualification import (
    CEOAuthorization,
    ControlledShadowCaptureAttestation,
    ObservationValidationResult,
    ProviderQualificationReport,
    ProviderQualificationStatus,
    QualificationContractError,
    RealProviderObservation,
)
from src.football.top5_provider_cascade_validation import (
    CascadeEvidence,
    evidence_digest,
)

BUILDER2_QUALIFICATION_RECEIPT_CONTRACT_VERSION = (
    "top5-builder2-qualification-receipt-v1"
)
RECEIPT_SCHEMA_VERSION = BUILDER2_QUALIFICATION_RECEIPT_CONTRACT_VERSION
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_EXPECTED_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "qualification_receipt_id",
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
        "accepted",
        "prediction_input_allowed",
        "no_bet",
        "publication",
        "production_activation",
        "monetary_spend_authorized",
        "failure_codes",
    }
)


class Builder2QualificationReceiptError(QualificationContractError):
    """Malformed, copied, or unsafe qualification receipt."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Builder2QualificationReceiptError(f"{name} must be non-empty text")
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise Builder2QualificationReceiptError(f"{name} must be a hexadecimal digest")
    return value


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def semantic_digest(value: object) -> str:
    """Return a stable semantic digest independent of mapping key order."""

    encoded = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _observation_payload(observation: RealProviderObservation) -> dict[str, object]:
    payload = observation.as_payload()
    payload.pop("capture_attestation", None)
    return payload


def _attestation_payload(
    attestation: ControlledShadowCaptureAttestation,
) -> dict[str, object]:
    return attestation.as_payload()


def _report_payload(report: ProviderQualificationReport) -> dict[str, object]:
    payload = report.as_payload()
    # These collections represent sets of report entries; canonicalize their
    # serialization order without changing configured provider/fixture order.
    for key in ("results", "coverage", "freshness"):
        values = payload.get(key)
        if isinstance(values, list):
            payload[key] = sorted(
                values,
                key=lambda item: json.dumps(
                    _canonical(item), sort_keys=True, separators=(",", ":")
                ),
            )
    if isinstance(payload.get("unresolved"), list):
        payload["unresolved"] = sorted(payload["unresolved"])
    return payload


def _result_payload(
    report_digest: str,
    observation: RealProviderObservation,
    result: ObservationValidationResult,
    observation_digest: str,
) -> dict[str, object]:
    return {
        "qualification_report_digest": report_digest,
        "observation_id": observation.observation_id,
        "observation_digest": observation_digest,
        "result": result.as_payload(),
    }


def _as_observation(
    value: RealProviderObservation | Mapping[str, object],
) -> RealProviderObservation:
    return (
        value
        if isinstance(value, RealProviderObservation)
        else RealProviderObservation.from_payload(value)
    )


def _as_attestation(
    value: ControlledShadowCaptureAttestation | Mapping[str, object],
) -> ControlledShadowCaptureAttestation:
    return (
        value
        if isinstance(value, ControlledShadowCaptureAttestation)
        else ControlledShadowCaptureAttestation.from_payload(value)
    )


def _as_cascade(value: CascadeEvidence | Mapping[str, object]) -> CascadeEvidence:
    return (
        value
        if isinstance(value, CascadeEvidence)
        else CascadeEvidence.from_payload(value)
    )


@dataclass(frozen=True)
class Builder2QualificationReceiptV1:
    """Deterministic, validation-only authority receipt for one real observation."""

    schema_version: str
    qualification_receipt_id: str
    qualification_report_identity: str
    qualification_report_digest: str
    qualification_result_digest: str
    qualification_session_id: str
    controlled_shadow_run_id: str
    ceo_authorization_id: str
    fixture_key: str
    provider_identity: str
    provider_event_id: str
    provider_request_id: str
    observation_id: str
    observation_digest: str
    normalized_record_digest: str
    cascade_evidence_digest: str
    capture_attestation_digest: str
    adapter_version: str
    adapter_source_sha: str
    qualification_status: ProviderQualificationStatus | str
    accepted: bool
    prediction_input_allowed: bool
    no_bet: bool
    publication: bool
    production_activation: bool
    monetary_spend_authorized: bool
    failure_codes: tuple[str, ...] = ()
    receipt_digest: str = ""

    @property
    def receipt_schema_version(self) -> str:
        return self.schema_version

    @property
    def semantic_receipt_digest(self) -> str:
        return self.receipt_digest

    def _payload(self, *, include_receipt_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "qualification_receipt_id": self.qualification_receipt_id,
            "qualification_report_identity": self.qualification_report_identity,
            "qualification_report_digest": self.qualification_report_digest,
            "qualification_result_digest": self.qualification_result_digest,
            "qualification_session_id": self.qualification_session_id,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "ceo_authorization_id": self.ceo_authorization_id,
            "fixture_key": self.fixture_key,
            "provider_identity": self.provider_identity,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "capture_attestation_digest": self.capture_attestation_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "qualification_status": ProviderQualificationStatus(
                self.qualification_status
            ).value,
            "accepted": self.accepted,
            "prediction_input_allowed": self.prediction_input_allowed,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
            "failure_codes": list(self.failure_codes),
        }
        if include_receipt_digest:
            payload["receipt_digest"] = self.receipt_digest
        return payload

    def validate(self) -> None:
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise Builder2QualificationReceiptError("unsupported receipt schema")
        for name, value in (
            ("qualification_receipt_id", self.qualification_receipt_id),
            ("qualification_report_identity", self.qualification_report_identity),
            ("qualification_session_id", self.qualification_session_id),
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("ceo_authorization_id", self.ceo_authorization_id),
            ("fixture_key", self.fixture_key),
            ("provider_identity", self.provider_identity),
            ("provider_event_id", self.provider_event_id),
            ("provider_request_id", self.provider_request_id),
            ("observation_id", self.observation_id),
            ("adapter_version", self.adapter_version),
        ):
            _text(value, name)
        for name, value in (
            ("qualification_report_digest", self.qualification_report_digest),
            ("qualification_result_digest", self.qualification_result_digest),
            ("observation_digest", self.observation_digest),
            ("normalized_record_digest", self.normalized_record_digest),
            ("cascade_evidence_digest", self.cascade_evidence_digest),
            ("capture_attestation_digest", self.capture_attestation_digest),
            ("adapter_source_sha", self.adapter_source_sha),
            ("receipt_digest", self.receipt_digest),
        ):
            _sha(value, name)
        try:
            status = ProviderQualificationStatus(self.qualification_status)
        except (TypeError, ValueError) as exc:
            raise Builder2QualificationReceiptError(
                "qualification status is invalid"
            ) from exc
        if status is not ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED:
            raise Builder2QualificationReceiptError(
                "receipt requires REAL_OBSERVATION_VALIDATED"
            )
        if self.qualification_report_identity != (
            f"{self.qualification_session_id}:{status.value}"
        ):
            raise Builder2QualificationReceiptError(
                "qualification report identity is inconsistent"
            )
        if self.qualification_receipt_id != (
            f"b2qr-{self.qualification_result_digest[:24]}"
        ):
            raise Builder2QualificationReceiptError("receipt ID is not deterministic")
        if self.accepted is not True or self.prediction_input_allowed is not True:
            raise Builder2QualificationReceiptError(
                "receipt must be accepted and prediction-input eligible"
            )
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise Builder2QualificationReceiptError(f"unsafe receipt field: {name}")
        if self.failure_codes:
            raise Builder2QualificationReceiptError(
                "prediction-eligible receipts require zero qualification failures"
            )
        expected_digest = semantic_digest(self._payload(include_receipt_digest=False))
        if self.receipt_digest.lower() != expected_digest:
            raise Builder2QualificationReceiptError("receipt digest mismatch")

    @classmethod
    def from_payload(cls, payload: object) -> Builder2QualificationReceiptV1:
        raw = payload if isinstance(payload, Mapping) else {}
        failure_codes = raw.get("failure_codes", ())
        if not isinstance(failure_codes, (list, tuple)):
            failure_codes = ()
        return cls(
            schema_version=raw.get("schema_version", ""),
            qualification_receipt_id=raw.get("qualification_receipt_id", ""),
            qualification_report_identity=raw.get("qualification_report_identity", ""),
            qualification_report_digest=raw.get("qualification_report_digest", ""),
            qualification_result_digest=raw.get("qualification_result_digest", ""),
            qualification_session_id=raw.get("qualification_session_id", ""),
            controlled_shadow_run_id=raw.get("controlled_shadow_run_id", ""),
            ceo_authorization_id=raw.get("ceo_authorization_id", ""),
            fixture_key=raw.get("fixture_key", ""),
            provider_identity=raw.get("provider_identity", ""),
            provider_event_id=raw.get("provider_event_id", ""),
            provider_request_id=raw.get("provider_request_id", ""),
            observation_id=raw.get("observation_id", ""),
            observation_digest=raw.get("observation_digest", ""),
            normalized_record_digest=raw.get("normalized_record_digest", ""),
            cascade_evidence_digest=raw.get("cascade_evidence_digest", ""),
            capture_attestation_digest=raw.get("capture_attestation_digest", ""),
            adapter_version=raw.get("adapter_version", ""),
            adapter_source_sha=raw.get("adapter_source_sha", ""),
            qualification_status=raw.get("qualification_status", ""),
            accepted=raw.get("accepted"),
            prediction_input_allowed=raw.get("prediction_input_allowed"),
            no_bet=raw.get("no_bet"),
            publication=raw.get("publication"),
            production_activation=raw.get("production_activation"),
            monetary_spend_authorized=raw.get("monetary_spend_authorized"),
            failure_codes=tuple(failure_codes),
            receipt_digest=raw.get("receipt_digest", ""),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_receipt_digest=True)


def _validate_accepted_inputs(
    report: ProviderQualificationReport,
    observation: RealProviderObservation,
    result: ObservationValidationResult,
) -> tuple[ControlledShadowCaptureAttestation, str, str, str]:
    report.validate()
    observation.validate_structural()
    if (
        report.qualification_status
        is not ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    ):
        raise Builder2QualificationReceiptError(
            "only a REAL_OBSERVATION_VALIDATED report may issue a receipt"
        )
    if (
        result not in report.results
        or result.accepted is not True
        or result.real_observed is not True
        or result.status is not ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
        or result.failure_codes
        or result.cascade_errors
    ):
        raise Builder2QualificationReceiptError(
            "the supplied result is not the exact accepted result"
        )
    if observation.observation_id not in report.accepted_observation_ids:
        raise Builder2QualificationReceiptError(
            "observation is not accepted by the supplied report"
        )
    if (
        result.observation_id != observation.observation_id
        or result.provider_identity != observation.provider_identity
        or result.league != observation.league
    ):
        raise Builder2QualificationReceiptError("report/result/observation mismatch")
    if observation.evidence_kind != "REAL_OBSERVED":
        raise Builder2QualificationReceiptError(
            "non-real evidence cannot issue a receipt"
        )
    if observation.capture_attestation is None:
        raise Builder2QualificationReceiptError(
            "controlled capture attestation is required"
        )
    try:
        attestation = _as_attestation(observation.capture_attestation)
        attestation.validate()
        cascade = _as_cascade(observation.cascade_evidence)
        cascade.validate_structural()
    except (QualificationContractError, TypeError, ValueError, AttributeError) as exc:
        raise Builder2QualificationReceiptError(
            "accepted observation provenance is invalid"
        ) from exc
    if cascade.prediction_input_allowed is not True:
        raise Builder2QualificationReceiptError(
            "receipt requires prediction_input_allowed=true in accepted cascade"
        )
    if attestation.qualification_session_id != report.session.qualification_session_id:
        raise Builder2QualificationReceiptError(
            "qualification session binding mismatch"
        )
    if attestation.provider_identity != observation.provider_identity:
        raise Builder2QualificationReceiptError("provider binding mismatch")
    if attestation.fixture_key != observation.fixture_key:
        raise Builder2QualificationReceiptError("fixture binding mismatch")
    if attestation.provider_event_id != observation.provider_event_id:
        raise Builder2QualificationReceiptError("provider event binding mismatch")
    if attestation.provider_request_id != observation.provider_request_id:
        raise Builder2QualificationReceiptError("provider request binding mismatch")
    if attestation.adapter_version != observation.adapter_version:
        raise Builder2QualificationReceiptError("adapter version binding mismatch")
    if attestation.adapter_source_sha.lower() != observation.adapter_source_sha.lower():
        raise Builder2QualificationReceiptError("adapter source binding mismatch")
    if (
        attestation.raw_response_digest.lower()
        != observation.raw_response_digest.lower()
    ):
        raise Builder2QualificationReceiptError("raw digest binding mismatch")
    if (
        attestation.normalized_record_digest.lower()
        != observation.normalized_record_digest.lower()
    ):
        raise Builder2QualificationReceiptError("normalized digest binding mismatch")
    if attestation.cascade_evidence_digest.lower() != evidence_digest(cascade).lower():
        raise Builder2QualificationReceiptError("cascade digest binding mismatch")
    if attestation.captured_at != observation.captured_at:
        raise Builder2QualificationReceiptError("capture time binding mismatch")
    if attestation.ceo_authorization_id == "":
        raise Builder2QualificationReceiptError("CEO authorization binding is required")
    report_digest = semantic_digest(_report_payload(report))
    observation_digest = semantic_digest(_observation_payload(observation))
    attestation_digest = semantic_digest(_attestation_payload(attestation))
    return attestation, report_digest, observation_digest, attestation_digest


def issue_builder2_qualification_receipt(
    report: ProviderQualificationReport,
    observation: RealProviderObservation | Mapping[str, object],
    result: ObservationValidationResult,
) -> Builder2QualificationReceiptV1:
    """Issue one receipt from an already accepted PR-#66 qualification result."""

    normalized_observation = _as_observation(observation)
    attestation, report_digest, observation_digest, attestation_digest = (
        _validate_accepted_inputs(report, normalized_observation, result)
    )
    result_digest = semantic_digest(
        _result_payload(
            report_digest, normalized_observation, result, observation_digest
        )
    )
    status = ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    receipt = Builder2QualificationReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        qualification_receipt_id=f"b2qr-{result_digest[:24]}",
        qualification_report_identity=(
            f"{report.session.qualification_session_id}:{status.value}"
        ),
        qualification_report_digest=report_digest,
        qualification_result_digest=result_digest,
        qualification_session_id=report.session.qualification_session_id,
        controlled_shadow_run_id=attestation.controlled_shadow_run_id,
        ceo_authorization_id=attestation.ceo_authorization_id,
        fixture_key=normalized_observation.fixture_key,
        provider_identity=normalized_observation.provider_identity,
        provider_event_id=normalized_observation.provider_event_id,
        provider_request_id=normalized_observation.provider_request_id,
        observation_id=normalized_observation.observation_id,
        observation_digest=observation_digest,
        normalized_record_digest=normalized_observation.normalized_record_digest,
        cascade_evidence_digest=attestation.cascade_evidence_digest,
        capture_attestation_digest=attestation_digest,
        adapter_version=normalized_observation.adapter_version,
        adapter_source_sha=normalized_observation.adapter_source_sha,
        qualification_status=status,
        accepted=True,
        prediction_input_allowed=True,
        no_bet=True,
        publication=False,
        production_activation=False,
        monetary_spend_authorized=False,
        failure_codes=(),
    )
    receipt = replace(
        receipt,
        receipt_digest=semantic_digest(receipt._payload(include_receipt_digest=False)),
    )
    receipt.validate()
    return receipt


def _compare_expected(
    receipt: Builder2QualificationReceiptV1,
    expected_bindings: Mapping[str, object] | None,
) -> None:
    if expected_bindings is None:
        return
    unknown = set(expected_bindings) - _EXPECTED_BINDING_FIELDS
    if unknown:
        raise Builder2QualificationReceiptError(
            f"unknown receipt binding fields: {sorted(unknown)}"
        )
    for field, expected in expected_bindings.items():
        actual = getattr(receipt, field)
        if isinstance(actual, Enum):
            actual = actual.value
        if actual != expected:
            raise Builder2QualificationReceiptError(
                f"receipt binding mismatch: {field}"
            )


def validate_builder2_qualification_receipt(
    receipt: Builder2QualificationReceiptV1 | Mapping[str, object],
    *,
    expected_observation: RealProviderObservation | Mapping[str, object] | None = None,
    expected_report: ProviderQualificationReport | None = None,
    expected_result: ObservationValidationResult | None = None,
    expected_authorization: CEOAuthorization | None = None,
    expected_cascade_evidence: CascadeEvidence | Mapping[str, object] | None = None,
    expected_capture_attestation: (
        ControlledShadowCaptureAttestation | Mapping[str, object] | None
    ) = None,
    expected_bindings: Mapping[str, object] | None = None,
) -> Builder2QualificationReceiptV1:
    """Validate a receipt and optional exact consumer context without side effects."""

    normalized = (
        receipt
        if isinstance(receipt, Builder2QualificationReceiptV1)
        else Builder2QualificationReceiptV1.from_payload(receipt)
    )
    normalized.validate()
    _compare_expected(normalized, expected_bindings)
    if expected_observation is not None:
        observation = _as_observation(expected_observation)
        observation.validate_structural()
        expected_cascade = _as_cascade(observation.cascade_evidence)
        expected_attestation = (
            _as_attestation(observation.capture_attestation)
            if observation.capture_attestation is not None
            else None
        )
        bindings = {
            "qualification_session_id": observation.qualification_session_id,
            "fixture_key": observation.fixture_key,
            "provider_identity": observation.provider_identity,
            "provider_event_id": observation.provider_event_id,
            "provider_request_id": observation.provider_request_id,
            "observation_id": observation.observation_id,
            "observation_digest": semantic_digest(_observation_payload(observation)),
            "normalized_record_digest": observation.normalized_record_digest,
            "cascade_evidence_digest": evidence_digest(expected_cascade),
            "adapter_version": observation.adapter_version,
            "adapter_source_sha": observation.adapter_source_sha,
        }
        if expected_attestation is None:
            raise Builder2QualificationReceiptError(
                "expected real observation lacks capture attestation"
            )
        expected_attestation.validate()
        bindings.update(
            {
                "controlled_shadow_run_id": expected_attestation.controlled_shadow_run_id,
                "ceo_authorization_id": expected_attestation.ceo_authorization_id,
                "capture_attestation_digest": semantic_digest(
                    _attestation_payload(expected_attestation)
                ),
            }
        )
        _compare_expected(normalized, bindings)
        if expected_authorization is not None:
            expected_authorization.validate()
            _compare_expected(
                normalized,
                {
                    "controlled_shadow_run_id": expected_authorization.controlled_shadow_run_id,
                    "qualification_session_id": expected_authorization.qualification_session_id,
                    "ceo_authorization_id": expected_authorization.authorization_id,
                },
            )
            if expected_authorization.allows(observation) is not None:
                raise Builder2QualificationReceiptError(
                    "receipt observation is outside expected CEO authorization"
                )
    if expected_report is not None:
        expected_report.validate()
        _compare_expected(
            normalized,
            {
                "qualification_report_digest": semantic_digest(
                    _report_payload(expected_report)
                ),
                "qualification_report_identity": (
                    f"{expected_report.session.qualification_session_id}:"
                    f"{ProviderQualificationStatus(expected_report.qualification_status).value}"
                ),
            },
        )
    if expected_result is not None:
        if expected_observation is None:
            raise Builder2QualificationReceiptError(
                "expected_result requires expected_observation"
            )
        observation = _as_observation(expected_observation)
        report_digest = (
            semantic_digest(_report_payload(expected_report))
            if expected_report is not None
            else normalized.qualification_report_digest
        )
        observation_digest = semantic_digest(_observation_payload(observation))
        expected_result_digest = semantic_digest(
            _result_payload(
                report_digest, observation, expected_result, observation_digest
            )
        )
        _compare_expected(
            normalized, {"qualification_result_digest": expected_result_digest}
        )
    if expected_cascade_evidence is not None:
        _compare_expected(
            normalized,
            {
                "cascade_evidence_digest": evidence_digest(
                    _as_cascade(expected_cascade_evidence)
                )
            },
        )
    if expected_capture_attestation is not None:
        attestation = _as_attestation(expected_capture_attestation)
        attestation.validate()
        _compare_expected(
            normalized,
            {
                "capture_attestation_digest": semantic_digest(
                    _attestation_payload(attestation)
                )
            },
        )
    if expected_authorization is not None and expected_observation is None:
        expected_authorization.validate()
        _compare_expected(
            normalized,
            {
                "controlled_shadow_run_id": expected_authorization.controlled_shadow_run_id,
                "qualification_session_id": expected_authorization.qualification_session_id,
                "ceo_authorization_id": expected_authorization.authorization_id,
            },
        )
    return normalized


def validate_builder1_qualification_receipt(
    receipt: Builder2QualificationReceiptV1 | Mapping[str, object],
    **kwargs: object,
) -> Builder2QualificationReceiptV1:
    """Builder-1 seam: require an already valid prediction-input receipt."""

    return validate_builder2_qualification_receipt(receipt, **kwargs)  # type: ignore[arg-type]


def validate_builder4_qualification_receipt(
    receipt: Builder2QualificationReceiptV1 | Mapping[str, object],
    **kwargs: object,
) -> Builder2QualificationReceiptV1:
    """Builder-4 seam: validate binding to cascade evidence; never issue."""

    return validate_builder2_qualification_receipt(receipt, **kwargs)  # type: ignore[arg-type]


# Compatibility name for the local authority type that Builder 4 replaces;
# the canonical issuer and validation behavior remain the V1 contract above.
Builder2ValidationReceipt = Builder2QualificationReceiptV1


__all__ = [
    "BUILDER2_QUALIFICATION_RECEIPT_CONTRACT_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "Builder2QualificationReceiptError",
    "Builder2QualificationReceiptV1",
    "Builder2ValidationReceipt",
    "issue_builder2_qualification_receipt",
    "semantic_digest",
    "validate_builder1_qualification_receipt",
    "validate_builder2_qualification_receipt",
    "validate_builder4_qualification_receipt",
]
