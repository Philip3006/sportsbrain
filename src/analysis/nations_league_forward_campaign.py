"""Governance and accounting for real Nations League forward-shadow evidence.

The frozen prediction, settlement, and metric contracts live in
``nations_league_v1``.  This module is deliberately a small envelope around
those contracts: it binds one campaign to a fixture manifest and immutable
evaluation policy, accounts for missed windows explicitly, and excludes
``SYNTHETIC_ONLY`` records from real-campaign reporting.

No function here contacts a provider, issues authority, promotes a model, or
rewrites a prediction or settlement.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from src.analysis.nations_league_v1 import (
    COMPETITION,
    INITIAL_WINDOW,
    REFINEMENT_WINDOW,
    calculate_forward_metrics,
    model_digest,
    sha256_json,
    validate_shadow_record,
)

FORWARD_CAMPAIGN_SCHEMA_VERSION = "nations-league-forward-evidence-campaign-v1"
FORWARD_SUMMARY_SCHEMA_VERSION = "nations-league-forward-evidence-summary-v1"
SHADOW_ONLY = "SHADOW_ONLY"
NO_BET = True

EVIDENCE_REAL = "REAL_OBSERVED"
EVIDENCE_SYNTHETIC = "SYNTHETIC_ONLY"
EVIDENCE_CLASSES = frozenset({EVIDENCE_REAL, EVIDENCE_SYNTHETIC})

NO_FORWARD_EVIDENCE = "NO_FORWARD_EVIDENCE"
INSUFFICIENT_FORWARD_EVIDENCE = "INSUFFICIENT_FORWARD_EVIDENCE"
FORWARD_EVIDENCE_ACCUMULATING = "FORWARD_EVIDENCE_ACCUMULATING"
PROMOTION_REVIEW_ELIGIBLE = "PROMOTION_REVIEW_ELIGIBLE"
EVIDENCE_STATES = frozenset(
    {
        NO_FORWARD_EVIDENCE,
        INSUFFICIENT_FORWARD_EVIDENCE,
        FORWARD_EVIDENCE_ACCUMULATING,
        PROMOTION_REVIEW_ELIGIBLE,
    }
)

_EXCEPTIONS = frozenset({"administrative", "cancelled"})


class ForwardCampaignError(ValueError):
    """Raised when campaign evidence would violate an immutable boundary."""


def _utc(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ForwardCampaignError(f"{field} must be a non-empty ISO timestamp")
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardCampaignError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ForwardCampaignError(f"{field} must carry UTC timezone")


def _digest(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ForwardCampaignError(f"{field} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class CapturePolicy:
    """The immutable capture contract for one lifecycle phase."""

    phase: str
    minimum_lead_seconds: int
    maximum_lead_seconds: int
    maximum_snapshot_age_seconds: int = 900

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "minimum_lead_seconds": self.minimum_lead_seconds,
            "maximum_lead_seconds": self.maximum_lead_seconds,
            "maximum_snapshot_age_seconds": self.maximum_snapshot_age_seconds,
        }


INITIAL_CAPTURE_POLICY = CapturePolicy(
    "initial",
    int(INITIAL_WINDOW[0].total_seconds()),
    int(INITIAL_WINDOW[1].total_seconds()),
)
REFINEMENT_CAPTURE_POLICY = CapturePolicy(
    "refinement",
    int(REFINEMENT_WINDOW[0].total_seconds()),
    int(REFINEMENT_WINDOW[1].total_seconds()),
)


def _canonical_manifest(
    manifest: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in manifest:
        row = deepcopy(dict(source))
        fixture_id = row.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise ForwardCampaignError("fixture manifest requires fixture_id")
        if fixture_id in seen:
            raise ForwardCampaignError("fixture manifest contains duplicate fixture_id")
        seen.add(fixture_id)
        for key in ("initial_eligible", "refinement_eligible"):
            if not isinstance(row.get(key), bool):
                raise ForwardCampaignError(f"fixture manifest requires boolean {key}")
        exception = row.get("exception")
        if exception is not None and exception not in _EXCEPTIONS:
            raise ForwardCampaignError("unsupported fixture manifest exception")
        if exception is not None and (
            row["initial_eligible"] or row["refinement_eligible"]
        ):
            raise ForwardCampaignError("exception fixture cannot be eligible")
        rows.append(row)
    return tuple(sorted(rows, key=lambda item: item["fixture_id"]))


@dataclass(frozen=True)
class ForwardEvidenceCampaign:
    """Immutable campaign contract; records are kept in the artifact envelope."""

    campaign_id: str
    competition: str
    edition: str
    model_digest_value: str
    campaign_start: str
    fixture_manifest: tuple[dict[str, Any], ...]
    initial_policy: CapturePolicy
    refinement_policy: CapturePolicy
    promotion_criteria_digest: str | None
    no_bet: bool
    signal_status: str
    evaluation_contract_digest: str

    def contract_payload(self) -> dict[str, Any]:
        return {
            "schema_version": FORWARD_CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "competition": self.competition,
            "edition": self.edition,
            "model_digest": self.model_digest_value,
            "campaign_start": self.campaign_start,
            "fixture_manifest": list(self.fixture_manifest),
            "fixture_manifest_digest": sha256_json(list(self.fixture_manifest)),
            "initial_policy": self.initial_policy.to_payload(),
            "refinement_policy": self.refinement_policy.to_payload(),
            "promotion_criteria_digest": self.promotion_criteria_digest,
            "no_bet": self.no_bet,
            "signal_status": self.signal_status,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.contract_payload()
        payload["evaluation_contract_digest"] = self.evaluation_contract_digest
        return payload


@dataclass(frozen=True)
class ForwardCampaignArtifact:
    campaign: ForwardEvidenceCampaign
    records: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": FORWARD_CAMPAIGN_SCHEMA_VERSION,
            "campaign": self.campaign.to_payload(),
            "records": [deepcopy(record) for record in self.records],
        }


def _validate_policy(
    policy: CapturePolicy,
    expected_phase: str,
    expected_window: tuple[timedelta, timedelta],
) -> None:
    if policy.phase != expected_phase:
        raise ForwardCampaignError(f"{expected_phase} policy phase mismatch")
    if policy.minimum_lead_seconds != int(expected_window[0].total_seconds()):
        raise ForwardCampaignError(f"{expected_phase} policy minimum is not frozen")
    if policy.maximum_lead_seconds != int(expected_window[1].total_seconds()):
        raise ForwardCampaignError(f"{expected_phase} policy maximum is not frozen")
    if policy.maximum_snapshot_age_seconds != 900:
        raise ForwardCampaignError(f"{expected_phase} freshness policy is not frozen")


def _validate_campaign(campaign: ForwardEvidenceCampaign) -> None:
    if not campaign.campaign_id or not campaign.edition:
        raise ForwardCampaignError("campaign identity is incomplete")
    if campaign.competition != COMPETITION:
        raise ForwardCampaignError("unsupported campaign competition")
    _digest(campaign.model_digest_value, "model_digest")
    if campaign.model_digest_value != model_digest():
        raise ForwardCampaignError("campaign model digest is not the frozen model")
    _utc(campaign.campaign_start, "campaign_start")
    _validate_policy(campaign.initial_policy, "initial", INITIAL_WINDOW)
    _validate_policy(campaign.refinement_policy, "refinement", REFINEMENT_WINDOW)
    if campaign.promotion_criteria_digest is not None:
        _digest(campaign.promotion_criteria_digest, "promotion_criteria_digest")
    if campaign.no_bet is not True or campaign.signal_status != SHADOW_ONLY:
        raise ForwardCampaignError("campaign is not shadow/no-bet")
    manifest = _canonical_manifest(campaign.fixture_manifest)
    if manifest != campaign.fixture_manifest:
        raise ForwardCampaignError("fixture manifest is not canonical")
    _digest(campaign.evaluation_contract_digest, "evaluation_contract_digest")
    if sha256_json(campaign.contract_payload()) != campaign.evaluation_contract_digest:
        raise ForwardCampaignError("evaluation contract digest mismatch")


def create_forward_campaign(
    *,
    campaign_id: str,
    edition: str,
    campaign_start: str,
    fixture_manifest: Iterable[Mapping[str, Any]],
    promotion_criteria_digest: str | None = None,
) -> ForwardCampaignArtifact:
    """Create a new empty campaign bound to the current frozen model."""

    canonical_manifest = _canonical_manifest(fixture_manifest)
    if promotion_criteria_digest is not None:
        _digest(promotion_criteria_digest, "promotion_criteria_digest")
    campaign = ForwardEvidenceCampaign(
        campaign_id=campaign_id,
        competition=COMPETITION,
        edition=edition,
        model_digest_value=model_digest(),
        campaign_start=campaign_start,
        fixture_manifest=canonical_manifest,
        initial_policy=INITIAL_CAPTURE_POLICY,
        refinement_policy=REFINEMENT_CAPTURE_POLICY,
        promotion_criteria_digest=promotion_criteria_digest,
        no_bet=NO_BET,
        signal_status=SHADOW_ONLY,
        evaluation_contract_digest="0" * 64,
    )
    digest = sha256_json(campaign.contract_payload())
    campaign = ForwardEvidenceCampaign(
        **{**campaign.__dict__, "evaluation_contract_digest": digest}
    )
    artifact = ForwardCampaignArtifact(campaign=campaign)
    validate_forward_campaign(artifact)
    return artifact


def validate_forward_campaign(artifact: ForwardCampaignArtifact) -> None:
    _validate_campaign(artifact.campaign)
    prediction_ids: set[str] = set()
    phases: set[tuple[str, str]] = set()
    settlement_ids: set[str] = set()
    settled_prediction_ids: set[str] = set()
    fixture_ids = {row["fixture_id"] for row in artifact.campaign.fixture_manifest}
    for record in artifact.records:
        evidence_class = record.get("evidence_class")
        if evidence_class not in EVIDENCE_CLASSES:
            raise ForwardCampaignError("record evidence_class is not approved")
        if record.get("campaign_id") != artifact.campaign.campaign_id:
            raise ForwardCampaignError("record campaign binding mismatch")
        if (
            record.get("campaign_contract_digest")
            != artifact.campaign.evaluation_contract_digest
        ):
            raise ForwardCampaignError("record campaign contract binding mismatch")
        if record.get("model_digest") != artifact.campaign.model_digest_value:
            raise ForwardCampaignError("record model digest mismatch")
        if record.get("record_type", "prediction") == "prediction":
            validate_shadow_record(record)
            if record.get("fixture_id") not in fixture_ids:
                raise ForwardCampaignError(
                    "prediction fixture is outside campaign manifest"
                )
            identity = (record["fixture_id"], record["phase"])
            if record["record_id"] in prediction_ids or identity in phases:
                raise ForwardCampaignError("campaign predictions are append-only")
            prediction_ids.add(record["record_id"])
            phases.add(identity)
            continue
        if record.get("record_type") != "settlement":
            raise ForwardCampaignError("unsupported campaign record type")
        settlement_id = record.get("settlement_id")
        prediction_id = record.get("prediction_record_id")
        if not isinstance(settlement_id, str) or settlement_id in settlement_ids:
            raise ForwardCampaignError("campaign settlements are append-only")
        if (
            prediction_id not in prediction_ids
            or prediction_id in settled_prediction_ids
        ):
            raise ForwardCampaignError("settlement does not bind one prediction")
        settlement_ids.add(settlement_id)
        settled_prediction_ids.add(prediction_id)


def _with_campaign(
    record: Mapping[str, Any], artifact: ForwardCampaignArtifact, evidence_class: str
) -> dict[str, Any]:
    if evidence_class not in EVIDENCE_CLASSES:
        raise ForwardCampaignError(
            "evidence_class must be REAL_OBSERVED or SYNTHETIC_ONLY"
        )
    copy = deepcopy(dict(record))
    if "evidence_class" in copy and copy["evidence_class"] != evidence_class:
        raise ForwardCampaignError("evidence class cannot be rewritten")
    copy["evidence_class"] = evidence_class
    copy["campaign_id"] = artifact.campaign.campaign_id
    copy["campaign_contract_digest"] = artifact.campaign.evaluation_contract_digest
    return copy


def append_forward_prediction(
    artifact: ForwardCampaignArtifact,
    prediction: Mapping[str, Any],
    *,
    evidence_class: str,
) -> ForwardCampaignArtifact:
    """Append one validated prediction without replacing prior evidence."""

    validate_forward_campaign(artifact)
    record = _with_campaign(prediction, artifact, evidence_class)
    validate_shadow_record(record)
    if record.get("model_digest") != artifact.campaign.model_digest_value:
        raise ForwardCampaignError("prediction model digest differs from campaign")
    if any(
        item.get("record_type", "prediction") == "prediction"
        and (
            item.get("record_id") == record.get("record_id")
            or (item.get("fixture_id"), item.get("phase"))
            == (record.get("fixture_id"), record.get("phase"))
        )
        for item in artifact.records
    ):
        raise ForwardCampaignError("campaign predictions are append-only")
    result = ForwardCampaignArtifact(artifact.campaign, artifact.records + (record,))
    validate_forward_campaign(result)
    return result


def append_forward_settlement(
    artifact: ForwardCampaignArtifact,
    settlement: Mapping[str, Any],
) -> ForwardCampaignArtifact:
    """Append a settlement bound to the original prediction evidence class."""

    validate_forward_campaign(artifact)
    if settlement.get("record_type") != "settlement":
        raise ForwardCampaignError("only settlement records may be appended")
    prediction_id = settlement.get("prediction_record_id")
    prediction = next(
        (
            item
            for item in artifact.records
            if item.get("record_type", "prediction") == "prediction"
            and item.get("record_id") == prediction_id
        ),
        None,
    )
    if prediction is None:
        raise ForwardCampaignError("settlement references an unknown prediction")
    record = _with_campaign(settlement, artifact, prediction["evidence_class"])
    if any(
        item.get("record_type") == "settlement"
        and item.get("prediction_record_id") == prediction_id
        for item in artifact.records
    ):
        raise ForwardCampaignError("campaign settlements are append-only")
    result = ForwardCampaignArtifact(artifact.campaign, artifact.records + (record,))
    validate_forward_campaign(result)
    return result


def update_forward_campaign(
    artifact: ForwardCampaignArtifact,
    *,
    model_digest_value: str | None = None,
    initial_policy: CapturePolicy | None = None,
    refinement_policy: CapturePolicy | None = None,
    promotion_criteria_digest: str | None = None,
) -> ForwardCampaignArtifact:
    """Reject contract changes once any prediction has been recorded.

    This is intentionally stricter than the minimum real-prediction rule: a
    test fixture cannot create a path where a later contract edit looks like a
    production campaign mutation.
    """

    validate_forward_campaign(artifact)
    if artifact.records:
        raise ForwardCampaignError("campaign evaluation contract is frozen")
    campaign = artifact.campaign
    updated = ForwardEvidenceCampaign(
        **{
            **campaign.__dict__,
            "model_digest_value": model_digest_value or campaign.model_digest_value,
            "initial_policy": initial_policy or campaign.initial_policy,
            "refinement_policy": refinement_policy or campaign.refinement_policy,
            "promotion_criteria_digest": (
                campaign.promotion_criteria_digest
                if promotion_criteria_digest is None
                else promotion_criteria_digest
            ),
            "evaluation_contract_digest": "0" * 64,
        }
    )
    updated = ForwardEvidenceCampaign(
        **{
            **updated.__dict__,
            "evaluation_contract_digest": sha256_json(updated.contract_payload()),
        }
    )
    result = ForwardCampaignArtifact(updated)
    validate_forward_campaign(result)
    return result


def _empty_metrics() -> dict[str, Any]:
    return {
        "sample_count": 0,
        "brier_score": None,
        "log_loss": None,
        "accuracy": None,
        "calibration": {"ece": None, "by_outcome": {}},
    }


def _criteria_state(
    *,
    real_prediction_count: int,
    promotion_criteria_digest: str | None,
    criteria_evaluation: Mapping[str, Any] | None,
) -> str:
    if real_prediction_count == 0:
        return NO_FORWARD_EVIDENCE
    if criteria_evaluation is None:
        return FORWARD_EVIDENCE_ACCUMULATING
    if promotion_criteria_digest is None:
        raise ForwardCampaignError("criteria evaluation is not bound to campaign")
    if criteria_evaluation.get("criteria_digest") != promotion_criteria_digest:
        raise ForwardCampaignError("promotion criteria digest mismatch")
    state = criteria_evaluation.get("state")
    if state not in {INSUFFICIENT_FORWARD_EVIDENCE, PROMOTION_REVIEW_ELIGIBLE}:
        raise ForwardCampaignError("criteria evaluation state is unsupported")
    if criteria_evaluation.get("frozen") is not True:
        raise ForwardCampaignError("promotion criteria must be explicitly frozen")
    if (
        state == PROMOTION_REVIEW_ELIGIBLE
        and criteria_evaluation.get("human_review_required") is not True
    ):
        raise ForwardCampaignError("promotion review state requires human review")
    return state


def build_forward_evidence_summary(
    artifact: ForwardCampaignArtifact,
    *,
    criteria_evaluation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic completeness and real-only forward metrics."""

    validate_forward_campaign(artifact)
    predictions = [
        row
        for row in artifact.records
        if row.get("record_type", "prediction") == "prediction"
    ]
    real_predictions = [
        row for row in predictions if row["evidence_class"] == EVIDENCE_REAL
    ]
    real_records = [
        row for row in artifact.records if row["evidence_class"] == EVIDENCE_REAL
    ]
    settlements = {
        row["prediction_record_id"]: row
        for row in real_records
        if row.get("record_type") == "settlement"
    }
    manifest = artifact.campaign.fixture_manifest
    eligible = [row for row in manifest if row.get("exception") is None]
    initial_eligible = [row for row in eligible if row["initial_eligible"]]
    refinement_eligible = [row for row in eligible if row["refinement_eligible"]]
    captured_initial = {
        row["fixture_id"] for row in real_predictions if row["phase"] == "initial"
    }
    captured_refinement = {
        row["fixture_id"] for row in real_predictions if row["phase"] == "refinement"
    }
    metrics = calculate_forward_metrics(real_records)
    stage_metrics = metrics.get("lifecycle_stage_breakdown", {})
    summary_body: dict[str, Any] = {
        "schema_version": FORWARD_SUMMARY_SCHEMA_VERSION,
        "campaign": artifact.campaign.to_payload(),
        "evidence_state": _criteria_state(
            real_prediction_count=len(real_predictions),
            promotion_criteria_digest=artifact.campaign.promotion_criteria_digest,
            criteria_evaluation=criteria_evaluation,
        ),
        "completeness": {
            "eligible_fixtures": len(eligible),
            "initial_eligible": len(initial_eligible),
            "initial_captured": len(captured_initial),
            "initial_missed": len(initial_eligible) - len(captured_initial),
            "refinement_eligible": len(refinement_eligible),
            "refinement_captured": len(captured_refinement),
            "refinement_missed": len(refinement_eligible) - len(captured_refinement),
            "settled": len(settlements),
            "unsettled": len(real_predictions) - len(settlements),
            "administrative_exceptions": sum(
                row.get("exception") == "administrative" for row in manifest
            ),
            "cancelled_exceptions": sum(
                row.get("exception") == "cancelled" for row in manifest
            ),
            "synthetic_predictions_excluded": len(predictions) - len(real_predictions),
        },
        "metrics": {
            "overall": metrics["overall"],
            "INITIAL": stage_metrics.get("initial", _empty_metrics()),
            "REFINEMENT": stage_metrics.get("refinement", _empty_metrics()),
            "accuracy_is_secondary": True,
        },
        "safety": {
            "no_bet": True,
            "signal_status": SHADOW_ONLY,
            "automatic_promotion": False,
            "provider_authority_changed": False,
            "publication": False,
            "ledger_mutation": False,
        },
    }
    summary_body["summary_digest"] = sha256_json(summary_body)
    return summary_body


def render_forward_operator_view(summary: Mapping[str, Any]) -> str:
    """Render a concise deterministic operator view without changing evidence."""

    completeness = summary["completeness"]
    metrics = summary["metrics"]
    lines = [
        "# Nations League forward-shadow evidence",
        "",
        f"- Evidence state: `{summary['evidence_state']}`",
        f"- Eligible fixtures: {completeness['eligible_fixtures']}",
        f"- INITIAL: captured {completeness['initial_captured']}, missed {completeness['initial_missed']}",
        f"- REFINEMENT: captured {completeness['refinement_captured']}, missed {completeness['refinement_missed']}",
        f"- Settled: {completeness['settled']}; unsettled: {completeness['unsettled']}",
        f"- Real settled samples: {metrics['overall']['sample_count']}",
        f"- INITIAL Brier/log-loss/ECE: {metrics['INITIAL']['brier_score']} / {metrics['INITIAL']['log_loss']} / {metrics['INITIAL']['calibration']['ece']}",
        f"- REFINEMENT Brier/log-loss/ECE: {metrics['REFINEMENT']['brier_score']} / {metrics['REFINEMENT']['log_loss']} / {metrics['REFINEMENT']['calibration']['ece']}",
        "- Safety: `no_bet=true`, `SHADOW_ONLY`, automatic promotion disabled",
    ]
    return "\n".join(lines) + "\n"


def serialize_forward_summary(summary: Mapping[str, Any]) -> str:
    """Return canonical JSON for a machine-readable summary."""

    if summary.get("summary_digest") != sha256_json(
        {k: v for k, v in summary.items() if k != "summary_digest"}
    ):
        raise ForwardCampaignError("summary digest mismatch")
    return json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
