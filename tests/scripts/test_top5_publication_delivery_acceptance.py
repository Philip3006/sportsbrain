"""Offline tests for the strict final Top-5 production acceptance harness."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from scripts.top5_publication_delivery_acceptance import (
    DeliveryAcceptanceError,
    validate_production_acceptance,
)
from tests.football.test_top5_public_delivery_executor import _context


def _valid_context() -> dict[str, object]:
    artifact, _current, plan, attestation, capability = _context()
    worker = json.loads(plan.worker_payload)
    static = json.loads(plan.static_payload)
    source_sha = worker["football"][0]["provenance"]["source_sha"]
    runtime = {
        "schema_version": "top5-runtime-acceptance-v1",
        "runtime_root": "/runtime/sportsbrain-current",
        "runtime_role": "governed-runtime",
        "checkout_clean": True,
        "publisher_clean": True,
        "health_authority": "governed-runtime",
        "health_status": "ok",
        "active_provider_order": ["the_odds_api"],
        "captured_at": "2026-09-20T12:00:00+00:00",
        "source_release_sha": source_sha,
        "runtime_data_sha": "f" * 64,
        "source_runtime_consistent": True,
    }
    return {
        "worker": worker,
        "static": static,
        "attestation": attestation.as_payload(),
        "capability": capability,
        "runtime": runtime,
        "manifest": plan.manifest(),
        "artifact": artifact,
    }


def _verify(context: dict[str, object], **overrides: object) -> dict[str, object]:
    values = {
        "worker_payload": context["worker"],
        "static_payload": context["static"],
        "attestation": context["attestation"],
        "capability": context["capability"],
        "runtime_evidence": context["runtime"],
        "expected_runtime_root": "/runtime/sportsbrain-current",
        "worker_status": 200,
        "pwa_status": 200,
        "now": datetime.fromisoformat("2026-09-20T12:01:00+00:00"),
        "delivery_manifest": context["manifest"],
    }
    values.update(overrides)
    return validate_production_acceptance(**values)


def test_valid_production_evidence_passes_without_side_effects():
    result = _verify(_valid_context())

    assert result["status"] == "TOP5_PRODUCTION_ACCEPTANCE_VERIFIED"
    assert result["provider_authority"] == "the_odds_api"
    assert result["leagues"] == ["BL1", "EPL", "L1", "LL", "SA"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_status", 503),
        ("pwa_status", 503),
    ],
)
def test_endpoint_unavailable_fails_closed(field, value):
    with pytest.raises(DeliveryAcceptanceError):
        _verify(_valid_context(), **{field: value})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda context: context["static"]["top5_release"].update(
            {"generation_id": "top5-generation-v1:other"}
        ),
        lambda context: context["static"]["top5_release"].update(
            {"league_codes": ["EPL", "BL1", "LL", "SA"]}
        ),
        lambda context: context["static"]["top5_release"].update(
            {"league_codes": ["EPL", "BL1", "LL", "SA", "SA"]}
        ),
        lambda context: context["static"]["top5_release"].update(
            {"league_codes": ["EPL", "BL1", "LL", "SA", "L1", "UCL"]}
        ),
        lambda context: context["static"]["top5_release"].update(
            {"provider_authority": "unexpected_provider"}
        ),
        lambda context: context["static"]["football"].pop(),
        lambda context: context["static"]["football"][0].update(
            {"fixture_key": "EPL:fixture:other"}
        ),
        lambda context: context["static"]["football"][0]["provenance"].pop(
            "source_sha"
        ),
        lambda context: context["static"]["football"][0].update(
            {"evidence_digest": "e" * 64}
        ),
        lambda context: context["attestation"].update(
            {"activation_id": "activation:other"}
        ),
        lambda context: context["runtime"].update(
            {"source_runtime_consistent": False}
        ),
    ],
)
def test_malformed_or_mismatched_evidence_fails_closed(mutate):
    context = _valid_context()
    mutate(context)

    with pytest.raises(DeliveryAcceptanceError):
        _verify(context)


def test_manifest_digest_mismatch_fails_closed():
    context = _valid_context()
    context["manifest"]["public_product_digest"] = "0" * 64

    with pytest.raises(DeliveryAcceptanceError):
        _verify(context)


def test_malformed_payload_fails_closed():
    context = _valid_context()
    context["worker"] = []

    with pytest.raises(DeliveryAcceptanceError):
        _verify(context)


def test_stale_generation_fails_closed():
    context = _valid_context()

    with pytest.raises(DeliveryAcceptanceError):
        _verify(
            context,
            now=datetime.fromisoformat("2026-09-21T12:01:00+00:00"),
        )


def test_candidate_provider_cannot_appear_in_public_authority_or_runtime_routing():
    context = _valid_context()
    context["worker"]["top5_release"]["provider_authority"] = "therundown_experimental"

    with pytest.raises(DeliveryAcceptanceError):
        _verify(context)

    context = _valid_context()
    context["runtime"]["active_provider_order"] = ["the_odds_api", "therundown_experimental"]
    with pytest.raises(DeliveryAcceptanceError):
        _verify(context)


def test_missing_capability_or_runtime_root_mismatch_fails_closed():
    context = _valid_context()
    context["capability"] = {}
    with pytest.raises(DeliveryAcceptanceError):
        _verify(context)

    context = _valid_context()
    with pytest.raises(DeliveryAcceptanceError):
        _verify(context, expected_runtime_root="/runtime/obsolete-checkout")


def test_runtime_evidence_is_fresh_and_health_is_attributable():
    context = _valid_context()
    context["runtime"]["captured_at"] = (
        "2026-09-20T11:50:00+00:00"
    )
    assert _verify(context)["runtime_root"] == "/runtime/sportsbrain-current"
