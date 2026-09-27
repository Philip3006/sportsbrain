"""Offline tests for assembling canonical B1 final-acceptance inputs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
)
from src.football.top5_final_acceptance import (
    FINAL_ACCEPTANCE_SCHEMA_VERSION,
    Top5FinalAcceptanceError,
    verify_final_acceptance,
)
from src.football.top5_final_acceptance_composer import (
    Top5FinalAcceptanceCompositionError,
    compose_top5_final_acceptance_bundle,
)
from src.football.top5_public_acceptance import Top5PrepublicationArtifactV1
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    inventory_for,
)
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    Top5SignalLifecycle,
    create_initial_signal,
)
from tests.football import test_top5_final_acceptance as b1_tests


class _B4Handoff:
    def __init__(self, inputs: dict[str, object]) -> None:
        self.inputs = inputs
        self.calls = 0

    def b1_evidence_inputs(self, *, now):
        self.calls += 1
        return self.inputs


def _lifecycles(now, *, source_sha: str = b1_tests.SOURCE_SHA):
    result = []
    for league in ("BL1", "EPL", "L1", "LL", "SA"):
        fixture = Fixture(
            fixture_key=f"composer-{league.lower()}-fixture",
            league_code=league,
            home_team=f"{league} Home",
            away_team=f"{league} Away",
            kickoff=now + timedelta(hours=24),
        )
        snapshot = MarketSnapshot(
            fixture_key=fixture.fixture_key,
            captured_at=now - timedelta(minutes=1),
            kind=MarketSnapshotKind.SIGNAL_TIME,
            source="the_odds_api:prematch",
            odds={"home": 2.1, "draw": 3.3, "away": 3.6},
            snapshot_id=f"composer-{league.lower()}-snapshot",
        )
        result.append(
            create_initial_signal(
                fixture=fixture,
                snapshot=snapshot,
                now=now,
                market_id="1x2-regulation",
                outcome_id="home",
                candidate_id=M5_CANDIDATE_ID,
                model_identity=M5_CANDIDATE_ID,
                probabilities={"home": 0.48, "draw": 0.28, "away": 0.24},
                source_sha=source_sha,
                research_sha=FROZEN_RESEARCH_SHA,
                model_artifact_hash=inventory_for(
                    league, M5_CANDIDATE_ID
                ).model_artifact_hash
                or "",
                eligibility_decision=True,
                decision_id=f"composer-{league.lower()}-decision",
                decision_reason="validated offline lifecycle fixture",
                contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
            )
        )
    return tuple(result)


def _prepublication_artifact(base_bundle, now, signal_time_contract_id):
    public = base_bundle["public"]
    worker = deepcopy(public["worker_candidate_payload"])
    static = deepcopy(public["static_candidate_payload"])
    for payload in (worker, static):
        payload["top5_release"]["signal_time_contract_id"] = signal_time_contract_id
        for record in payload["football"]:
            record["signal_time_contract_id"] = signal_time_contract_id
            record["provenance"]["signal_time_contract_id"] = signal_time_contract_id
    return Top5PrepublicationArtifactV1.create(
        worker_candidate_payload=worker,
        static_candidate_payload=static,
        prepared_at=now,
    )


def _case(monkeypatch):
    from monitoring.top5 import governed_runtime_evidence
    from src.football import top5_final_acceptance_composer as composer

    base = b1_tests._bundle()
    now = b1_tests.NOW_ACCEPTANCE
    inputs = {
        "source_main_sha": "d" * 40,
        "b4_quota_proof_package": base["b4_quota_proof_package"],
        "b4_quota_headroom": base["b4_quota_headroom"],
        "discovery_evidence": base["discovery_evidence"],
        "provider_native_discovery_provenance": None,
        "controlled_shadow": base["controlled_shadow"],
        "b4_reconciliation": {"preserve": "reconciliation"},
        "b4_qualification": {"preserve": "qualification"},
        "b4_native_authorization": {"preserve": "authorization"},
        "b4_dossier_digest": "e" * 64,
    }
    handoff = _B4Handoff(inputs)
    lifecycles = _lifecycles(now)
    first = lifecycles[0].versions[-1]
    prepublication = _prepublication_artifact(base, now, first.stage_contract_id)
    runtime = deepcopy(base["runtime_evidence"])
    runtime.update(
        {
            "schema_version": governed_runtime_evidence.ARTIFACT_SCHEMA,
            "status": "READY",
        }
    )
    runtime["artifact_digest"] = governed_runtime_evidence._canonical_digest(runtime)
    monkeypatch.setattr(composer, "observe_governed_runtime", lambda: runtime)
    return handoff, inputs, lifecycles, prepublication, runtime, now


def test_composer_builds_exact_verified_v2_bundle_and_preserves_inputs(monkeypatch):
    handoff, inputs, lifecycles, public, runtime, now = _case(monkeypatch)
    b4_before = deepcopy(inputs)
    public_before = public.as_payload()
    runtime_before = deepcopy(runtime)

    bundle = compose_top5_final_acceptance_bundle(
        b4_dossier=handoff,  # type: ignore[arg-type]
        lifecycles=lifecycles,
        prepublication_artifact=public,
        now=now,
    )

    assert handoff.calls == 1
    assert set(bundle) == {
        "schema_version",
        "source_main_sha",
        "model_runtime",
        "b4_quota_proof_package",
        "b4_quota_headroom",
        "discovery_evidence",
        "provider_native_discovery_provenance",
        "controlled_shadow",
        "runtime_evidence",
        "public",
        "b4_reconciliation",
        "b4_qualification",
        "b4_native_authorization",
        "b4_dossier_digest",
    }
    assert bundle["schema_version"] == FINAL_ACCEPTANCE_SCHEMA_VERSION
    assert bundle["source_main_sha"] == "d" * 40
    assert bundle["model_runtime"]["source_sha"] == b1_tests.SOURCE_SHA
    assert bundle["model_runtime"]["source_sha"] != bundle["source_main_sha"]
    assert bundle["model_runtime"]["candidate_id"] == M5_CANDIDATE_ID
    assert (
        bundle["model_runtime"]["signal_time_contract_id"]
        == lifecycles[0].versions[-1].stage_contract_id
    )
    assert bundle["model_runtime"]["closing_used_for_prediction"] is False
    assert bundle["model_runtime"]["production_model_approved"] is False
    assert bundle["model_runtime"]["signal_time_approved_for_production"] is False
    assert bundle["model_runtime"]["publication_authorized"] is False
    assert bundle["model_runtime"]["no_bet"] is True
    assert bundle["b4_quota_proof_package"] == b4_before["b4_quota_proof_package"]
    assert bundle["controlled_shadow"] == b4_before["controlled_shadow"]
    for key in (
        "b4_reconciliation",
        "b4_qualification",
        "b4_native_authorization",
        "b4_dossier_digest",
    ):
        assert bundle[key] is inputs[key]
    assert bundle["runtime_evidence"] is runtime
    assert bundle["public"]["schema_version"] == "top5-prepublication-artifact-v1"
    assert "publication_attestation" not in bundle["public"]
    assert inputs == b4_before
    assert public.as_payload() == public_before
    assert runtime == runtime_before
    assert (
        verify_final_acceptance(bundle, now=now)["status"] == b1_tests.STATUS_VERIFIED
    )


def test_blocked_or_badly_digest_bound_governed_runtime_fails_closed(monkeypatch):
    handoff, _inputs, lifecycles, public, _runtime, now = _case(monkeypatch)
    from src.football import top5_final_acceptance_composer as composer

    monkeypatch.setattr(
        composer, "observe_governed_runtime", lambda: {"status": "BLOCKED"}
    )
    with pytest.raises(Top5FinalAcceptanceCompositionError, match="READY observation"):
        compose_top5_final_acceptance_bundle(
            b4_dossier=handoff,  # type: ignore[arg-type]
            lifecycles=lifecycles,
            prepublication_artifact=public,
            now=now,
        )


def test_inconsistent_lifecycle_source_is_not_rewritten_from_b4(monkeypatch):
    handoff, _inputs, lifecycles, public, _runtime, now = _case(monkeypatch)
    bad = (*lifecycles[:-1], *_lifecycles(now, source_sha="f" * 40)[-1:])
    with pytest.raises(Top5FinalAcceptanceCompositionError, match="share one model"):
        compose_top5_final_acceptance_bundle(
            b4_dossier=handoff,  # type: ignore[arg-type]
            lifecycles=bad,
            prepublication_artifact=public,
            now=now,
        )


def test_closing_snapshot_and_untyped_publication_input_are_rejected(monkeypatch):
    handoff, _inputs, lifecycles, public, _runtime, now = _case(monkeypatch)
    first = lifecycles[0]
    tampered_version = replace(
        first.versions[-1],
        snapshot_kind=MarketSnapshotKind.CLOSING,
        version_digest="",
    )
    withdrawn_like = Top5SignalLifecycle(
        first.lifecycle_id, first.contract, (tampered_version,)
    )
    with pytest.raises(ValueError, match="closing odds"):
        compose_top5_final_acceptance_bundle(
            b4_dossier=handoff,  # type: ignore[arg-type]
            lifecycles=(withdrawn_like, *lifecycles[1:]),
            prepublication_artifact=public,
            now=now,
        )
    with pytest.raises(
        Top5FinalAcceptanceCompositionError, match="typed prepublication"
    ):
        compose_top5_final_acceptance_bundle(
            b4_dossier=handoff,  # type: ignore[arg-type]
            lifecycles=lifecycles,
            prepublication_artifact=public.as_payload(),  # type: ignore[arg-type]
            now=now,
        )


def test_existing_b1_freshness_validators_still_reject_stale_inputs(monkeypatch):
    handoff, _inputs, lifecycles, public, _runtime, now = _case(monkeypatch)
    with pytest.raises((Top5FinalAcceptanceError, ValueError)):
        compose_top5_final_acceptance_bundle(
            b4_dossier=handoff,  # type: ignore[arg-type]
            lifecycles=lifecycles,
            prepublication_artifact=public,
            now=now + timedelta(minutes=20),
        )
