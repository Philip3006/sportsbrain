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


def _neutral_prepublication_artifact(
    base_bundle, now, signal_time_contract_id, run_id, session_id
):
    public = base_bundle["public"]
    worker = deepcopy(public["worker_candidate_payload"])
    static = deepcopy(public["static_candidate_payload"])
    for payload in (worker, static):
        release = payload["top5_release"]
        release.pop("prepublication_id", None)
        release["signal_time_contract_id"] = signal_time_contract_id
        release["controlled_shadow_run_id"] = run_id
        release["qualification_session_id"] = session_id
        for record in payload["football"]:
            record["signal_time_contract_id"] = signal_time_contract_id
            record["run_id"] = run_id
            record["session_id"] = session_id
            record["controlled_shadow_run_id"] = run_id
            record["qualification_session_id"] = session_id
            record["provenance"]["signal_time_contract_id"] = signal_time_contract_id
            record["provenance"]["controlled_shadow_run_id"] = run_id
            record["provenance"]["qualification_session_id"] = session_id
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
    manifest = verify_final_acceptance(bundle, now=now)["manifest"]
    assert manifest["provider_authority"] == "the_odds_api"
    assert manifest["evidence_provider"] == "therundown_experimental"
    assert manifest["candidate_provider"] == manifest["evidence_provider"]


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


def _neutral_case(monkeypatch):
    from monitoring.top5 import governed_runtime_evidence
    from src.football import top5_final_acceptance_composer as composer
    from src.football.top5_b4_provider_neutral_evidence import (
        Top5B4ProviderNeutralEvidenceDossierV1,
    )
    from tests.football import test_top5_b4_provider_neutral_evidence as neutral_tests

    base = b1_tests._bundle()
    now = b1_tests.NOW_ACCEPTANCE
    monkeypatch.setattr(neutral_tests, "NOW", now)
    neutral_shadow = neutral_tests._evidence(provider="isports_api")
    neutral_dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(
        neutral_shadow, now=now
    )
    lifecycles = _lifecycles(now)
    contract_id = lifecycles[0].versions[-1].stage_contract_id
    public = _neutral_prepublication_artifact(
        base,
        now,
        contract_id,
        neutral_shadow.controlled_shadow_run_id,
        neutral_shadow.qualification_session_id,
    )
    runtime = deepcopy(base["runtime_evidence"])
    runtime.update(
        {
            "schema_version": governed_runtime_evidence.ARTIFACT_SCHEMA,
            "status": "READY",
            "runtime_state_observed_at": now.isoformat(),
            "captured_at": now.isoformat(),
            "active_provider_order": ["the_odds_api"],
        }
    )
    runtime["artifact_digest"] = governed_runtime_evidence._canonical_digest(runtime)
    monkeypatch.setattr(composer, "observe_governed_runtime", lambda: runtime)
    bundle = compose_top5_final_acceptance_bundle(
        b4_dossier=neutral_dossier,
        lifecycles=lifecycles,
        prepublication_artifact=public,
        now=now,
    )
    return bundle, neutral_dossier, public, now


def test_provider_neutral_isports_dossier_composes_and_binds_distinct_authority(
    monkeypatch,
):
    from src.football.top5_durable_activation import (
        validate_activation_manifest_evidence_provider,
    )
    from src.football.top5_public_acceptance import publication_precheck

    bundle, dossier, public, now = _neutral_case(monkeypatch)
    result = verify_final_acceptance(bundle, now=now)
    manifest = result["manifest"]

    assert result["status"] == b1_tests.STATUS_VERIFIED
    assert bundle["b4_provider_neutral_dossier"]["schema_version"] == (
        "top5-b4-provider-neutral-dossier-v1"
    )
    assert manifest["provider_authority"] == "the_odds_api"
    assert manifest["evidence_provider"] == "isports_api"
    assert manifest["candidate_provider"] == manifest["evidence_provider"]
    assert manifest["b4_proof_id"] is None
    assert manifest["b4_proof_evidence_digest"] is None
    assert manifest["b4_dossier_digest"] == dossier.dossier_digest
    assert validate_activation_manifest_evidence_provider(manifest) == "isports_api"

    prepublication = Top5PrepublicationArtifactV1.from_mapping(bundle["public"])
    publication = publication_precheck(
        bundle["public"],
        result,
        now=now,
        delivery_manifest=prepublication.delivery_dry_run_manifest(rollback_ready=True),
    )
    assert publication["status"] == "TOP5_PUBLICATION_PRECHECK_READY"
    assert publication["production_mutation"] is False
    assert publication["provider_requests"] == 0
    assert public.as_payload() == bundle["public"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda b: b.update(evidence_provider="unknown_provider"), "identity"),
        (
            lambda b: b.update(candidate_provider="therundown_experimental"),
            "candidate_provider",
        ),
        (lambda b: b.update(provider_authority="isports_api"), "authority"),
        (
            lambda b: b.update(provider_authority="isports_api:main:prematch_1x2"),
            "authority",
        ),
        (
            lambda b: b["runtime_evidence"].update(
                active_provider_order=["the_odds_api", "isports_api"]
            ),
            "active provider order",
        ),
        (
            lambda b: b["discovery_evidence"][0].update(
                provider_identity="the_odds_api"
            ),
            "discovery_evidence",
        ),
        (
            lambda b: b["b4_quota_proof_package"].update(readiness_status="BLOCKED"),
            "b4_quota_proof_package",
        ),
        (
            lambda b: b["b4_quota_headroom"].update(provider_identity="the_odds_api"),
            "b4_quota_headroom",
        ),
        (
            lambda b: b["b4_reconciliation"].update(provider_identity="the_odds_api"),
            "b4_reconciliation",
        ),
        (
            lambda b: b["b4_qualification"].update(provider_identity="the_odds_api"),
            "b4_qualification",
        ),
        (
            lambda b: b["b4_native_authorization"].update(
                provider_identity="the_odds_api"
            ),
            "b4_native_authorization",
        ),
        (
            lambda b: b["b4_provider_neutral_dossier"]["controlled_shadow"][
                "market_evidence"
            ][0].update(provider_identity="the_odds_api"),
            "dossier rejected",
        ),
        (
            lambda b: b["b4_provider_neutral_dossier"].update(dossier_digest="0" * 64),
            "dossier rejected",
        ),
    ),
)
def test_provider_neutral_b1_rejects_identity_authority_and_projection_tampering(
    monkeypatch, mutate, message
):
    bundle, _dossier, _public, now = _neutral_case(monkeypatch)
    mutate(bundle)
    with pytest.raises(Top5FinalAcceptanceError, match=message):
        verify_final_acceptance(bundle, now=now)


def test_provider_neutral_b1_rejects_dossier_source_main_mismatch(monkeypatch):
    bundle, _dossier, _public, now = _neutral_case(monkeypatch)
    bundle["source_main_sha"] = "f" * 40
    with pytest.raises(Top5FinalAcceptanceError, match="source-main SHA"):
        verify_final_acceptance(bundle, now=now)


def test_provider_neutral_manifest_evidence_substitution_is_not_digest_compatible(
    monkeypatch,
):
    from src.football.top5_durable_activation import (
        validate_activation_manifest_evidence_provider,
    )

    bundle, _dossier, _public, now = _neutral_case(monkeypatch)
    manifest = verify_final_acceptance(bundle, now=now)["manifest"]
    manifest["candidate_provider"] = "therundown_experimental"
    with pytest.raises(ValueError, match="identities do not match"):
        validate_activation_manifest_evidence_provider(manifest)


def test_provider_neutral_the_odds_identity_exposes_merged_activation_allowlist_gap(
    monkeypatch,
):
    from src.football.top5_b4_provider_neutral_evidence import (
        Top5B4ProviderNeutralEvidenceDossierV1,
    )
    from tests.football import test_top5_b4_provider_neutral_evidence as neutral_tests

    now = b1_tests.NOW_ACCEPTANCE
    monkeypatch.setattr(neutral_tests, "NOW", now)
    dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(
        neutral_tests._evidence(provider="the_odds_api"), now=now
    )
    base, _valid_dossier, _public, _now = _neutral_case(monkeypatch)
    base["b4_provider_neutral_dossier"] = dossier.as_payload(now=now)
    with pytest.raises(Top5FinalAcceptanceError, match="activation-compatible"):
        verify_final_acceptance(base, now=now)
