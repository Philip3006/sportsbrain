"""Offline end-to-end coverage for the non-authorizing B1 input materializer."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from monitoring.top5 import governed_runtime_evidence
from src.football.top5_b1_real_input_materializer import (
    Top5B1MaterializationError,
    _materialize_validated_dossier,
)
from src.football.top5_b4_provider_neutral_evidence import (
    TOP5_LEAGUE_ORDER,
    Top5B4ProviderNeutralEvidenceDossierV1,
)
from src.football.top5_final_acceptance import ACTIVE_PROVIDER, STATUS_VERIFIED
from src.football.top5_public_acceptance import TOP5_PUBLICATION_PRECHECK_READY
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from tests.football import test_top5_b4_provider_neutral_evidence as b4_fixtures

SOURCE_MAIN_SHA = "d" * 40
SOURCE_RELEASE_SHA = "a" * 40
RUNTIME_DATA_SHA = "b" * 40
MATERIALIZATION_NOW = b4_fixtures.NOW + timedelta(seconds=1)


def _dossier(*, kickoff_lead: timedelta = timedelta(hours=24)):
    shadow = b4_fixtures._evidence(provider="isports_api")
    discovery_items = []
    captures = []
    capture_by_league = {item.league: item for item in shadow.captures}
    for discovery in shadow.discovery_evidence:
        kickoff = b4_fixtures.NOW + kickoff_lead
        fixture_key = make_fixture_key(
            discovery.league,
            discovery.home_team,
            discovery.away_team,
            kickoff,
        )
        updated = replace(discovery, kickoff=kickoff, fixture_key=fixture_key)
        capture = capture_by_league[discovery.league]
        captures.append(
            replace(
                capture,
                fixture_key=fixture_key,
                discovery_evidence_digest=updated.evidence_digest,
            )
        )
        discovery_items.append(updated)
    shadow = replace(
        shadow,
        source_main_sha=SOURCE_MAIN_SHA,
        discovery_evidence=tuple(discovery_items),
        captures=tuple(captures),
    )
    return Top5B4ProviderNeutralEvidenceDossierV1.build(shadow, now=b4_fixtures.NOW)


def _source_files(repo_root: Path, *, now=b4_fixtures.NOW, consistent: bool = True):
    (repo_root / "docs/data").mkdir(parents=True)
    (repo_root / "docs/data/provenance_meta.json").write_text(
        json.dumps(
            {
                "source_release_sha": SOURCE_RELEASE_SHA,
                "source_ci": {"status": "success", "head_sha": SOURCE_RELEASE_SHA},
            }
        ),
        encoding="utf-8",
    )
    (repo_root / "docs/data/health.json").write_text(
        json.dumps(
            {
                "overall": "degraded",
                "generated_at": now.isoformat(),
                "provenance": {
                    "source_release_sha": SOURCE_RELEASE_SHA,
                    "source_ci": {
                        "status": "success",
                        "head_sha": SOURCE_RELEASE_SHA,
                    },
                    "runtime_data_sha": RUNTIME_DATA_SHA,
                    "source_runtime_consistent": consistent,
                },
            }
        ),
        encoding="utf-8",
    )


def _runtime_artifact(now, runtime_root: Path):
    observed = now - timedelta(seconds=1)
    payload = {
        "schema_version": governed_runtime_evidence.ARTIFACT_SCHEMA,
        "status": "READY",
        "runtime_root": str(runtime_root),
        "runtime_root_role": "governed-runtime",
        "source_release_sha": SOURCE_RELEASE_SHA,
        "source_release_resolution": {
            "source": str(runtime_root / "runtime-state-v1.json"),
            "method": "test fixture matching the governed writer contract",
            "resolved_sha": SOURCE_RELEASE_SHA,
        },
        "runtime_data_sha": RUNTIME_DATA_SHA,
        "runtime_data_resolution": {
            "source": str(runtime_root / "runtime-state-v1.json"),
            "method": "test fixture matching the governed writer contract",
            "resolved_sha": RUNTIME_DATA_SHA,
        },
        "runtime_state_observed_at": observed.isoformat(),
        "active_provider_order": [ACTIVE_PROVIDER],
        "provider_authority": ACTIVE_PROVIDER,
        "checkout_clean": True,
        "publisher_clean": True,
        "cleanliness_checks": {
            "checkout": {"root": "/source", "method": "git status", "result": "clean"},
            "publisher": {
                "root": "/publisher",
                "method": "git status",
                "result": "clean",
            },
        },
        "health_authority": "governed",
        "health_source": "docs/data/health.json",
        "health_status": "degraded",
        "captured_at": now.isoformat(),
        "no_bet": True,
        "publication_enabled": False,
        "activation_state": "DISABLED",
        "scheduler_state": "disabled",
    }
    payload["artifact_digest"] = governed_runtime_evidence._canonical_digest(payload)
    return payload


def test_real_isports_dossier_materializes_verified_b1_and_read_only_b3(
    tmp_path, monkeypatch
):
    from src.football import top5_signal_lifecycle as lifecycle_module

    persisted: list[str] = []
    monkeypatch.setattr(
        lifecycle_module.Top5SignalLifecycleStore,
        "save",
        lambda *_args, **_kwargs: persisted.append("lifecycle"),
    )
    monkeypatch.setattr(
        lifecycle_module.Top5SignalLifecycleSetStore,
        "commit",
        lambda *_args, **_kwargs: persisted.append("lifecycle-set"),
    )
    result, calls, output_dir, _observed = _case_with_composer_observer(
        tmp_path, monkeypatch
    )

    assert result["b1_status"] == STATUS_VERIFIED
    assert result["public_precheck_status"] == TOP5_PUBLICATION_PRECHECK_READY
    assert result["provider_authority"] == ACTIVE_PROVIDER
    assert result["evidence_provider"] == "isports_api"
    assert result["lifecycle_anchor_count"] == 5
    assert result["public_outcome_record_count"] == 15
    assert result["provider_requests"] == 0
    assert result["publication_enabled"] is False
    assert result["publication_authorized"] is False
    assert result["activation_performed"] is False
    assert result["betting"] is False
    assert result["ledger_mutation"] is False
    assert result["scheduler_registered"] is False
    assert len(calls["writes"]) == 1
    assert calls["route_reads"] == 1
    assert calls["writes"][0]["provider_authority"] == ACTIVE_PROVIDER
    assert calls["writes"][0]["active_provider_order"] == [ACTIVE_PROVIDER]
    assert persisted == []

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    lifecycles = json.loads(
        (output_dir / "lifecycle_model_evidence.json").read_text(encoding="utf-8")
    )
    prepublication = json.loads(
        (output_dir / "prepublication_artifact.json").read_text(encoding="utf-8")
    )
    verified = json.loads((output_dir / "b1_verified.json").read_text(encoding="utf-8"))
    dry_run = json.loads(
        (output_dir / "publication_precheck.json").read_text(encoding="utf-8")
    )
    assert summary["b1_status"] == STATUS_VERIFIED
    assert lifecycles["provider_authority"] == ACTIVE_PROVIDER
    assert lifecycles["evidence_provider"] == "isports_api"
    assert len(lifecycles["anchors"]) == 5
    assert all(
        len(lifecycles["leagues"][league]["initial_lifecycles"]) == 3
        for league in TOP5_LEAGUE_ORDER
    )
    assert all(
        lifecycles["leagues"][league]["market_snapshot"]["snapshot_source"].startswith(
            "isports_api:"
        )
        for league in TOP5_LEAGUE_ORDER
    )
    for league in TOP5_LEAGUE_ORDER:
        market = lifecycles["leagues"][league]["market_snapshot"]
        for lifecycle in lifecycles["leagues"][league]["initial_lifecycles"]:
            version = lifecycle["versions"][0]
            assert version["provider_identity"] == ACTIVE_PROVIDER
            assert version["snapshot_source"] == market["snapshot_source"]
            assert version["prediction_generated_at"] == MATERIALIZATION_NOW.isoformat()
            assert version["odds_captured_at"] == market["captured_at"]
            assert version["research_sha"] == verified["manifest"]["research_sha"]
            evidence = version["confidence_metadata"]
            assert evidence["evidence_provider"] == "isports_api"
            assert evidence["source_main_sha"] == SOURCE_MAIN_SHA
            assert (
                evidence["controlled_shadow_run_id"]
                == result["controlled_shadow_run_id"]
            )
            assert (
                evidence["qualification_session_id"]
                == result["qualification_session_id"]
            )
            assert evidence["authorization_id"] == result["authorization_id"]
            assert evidence["provider_fixture_id"]
            assert (
                evidence["market_evidence_digest"] == market["market_evidence_digest"]
            )
    assert verified["status"] == STATUS_VERIFIED
    assert verified["manifest"]["provider_authority"] == ACTIVE_PROVIDER
    assert verified["manifest"]["evidence_provider"] == "isports_api"
    assert verified["manifest"]["ceo_authorization_id"] == result["authorization_id"]
    assert verified["manifest"]["checks"]["public_prepublication_delivery"] is True
    assert prepublication["publication_enabled"] is False
    assert prepublication["publication_authorized"] is False
    assert prepublication["capability_consumed"] is False
    assert prepublication["mutation_performed"] is False
    assert prepublication["provider_requests"] == 0
    assert (
        prepublication["worker_candidate_payload"]
        == prepublication["static_candidate_payload"]
    )
    assert len(prepublication["worker_candidate_payload"]["football"]) == 15
    assert (
        prepublication["worker_candidate_payload"]["top5_release"]["publication_status"]
        == "PREPARED"
    )
    assert all(
        record["provider"] == ACTIVE_PROVIDER and record["source"] == ACTIVE_PROVIDER
        for record in prepublication["worker_candidate_payload"]["football"]
    )
    poisoned = json.loads(json.dumps(prepublication["worker_candidate_payload"]))
    poisoned["football"][0]["provider"] = "therundown_experimental"
    with pytest.raises(ValueError, match="prepublication payload rejected"):
        from src.football.top5_public_acceptance import Top5PrepublicationArtifactV1

        Top5PrepublicationArtifactV1.create(
            worker_candidate_payload=poisoned,
            static_candidate_payload=poisoned,
            prepared_at=b4_fixtures.NOW,
        )
    assert dry_run["status"] == TOP5_PUBLICATION_PRECHECK_READY
    assert dry_run["production_mutation"] is False
    assert dry_run["provider_requests"] == 0
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o400 for path in output_dir.iterdir()
    )
    repeated, _repeat_calls, repeat_dir, _repeat_observed = (
        _case_with_composer_observer(tmp_path / "repeat", monkeypatch)
    )
    assert repeated["prepublication_digest"] == result["prepublication_digest"]
    assert repeated["b1_manifest_digest"] == result["b1_manifest_digest"]
    repeated_public = json.loads(
        (repeat_dir / "prepublication_artifact.json").read_text(encoding="utf-8")
    )
    assert (
        repeated_public["worker_candidate_payload_digest"]
        == prepublication["worker_candidate_payload_digest"]
    )


def _case_with_composer_observer(tmp_path: Path, monkeypatch):
    from src.football import top5_final_acceptance_composer as composer

    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    runtime_root = tmp_path / "runtime"
    repo_root.mkdir(parents=True)
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    observed = _runtime_artifact(MATERIALIZATION_NOW, runtime_root)
    monkeypatch.setattr(composer, "observe_governed_runtime", lambda: observed)
    calls: dict[str, object] = {"writes": [], "route_reads": 0}
    route = {
        "provider_authority": ACTIVE_PROVIDER,
        "activation_mode": "DISABLED",
        "publication": False,
        "betting": False,
        "ledger_mutation": False,
        "recurring_scheduler": False,
    }

    def writer(state):
        assert "observed_at" not in state
        calls["writes"].append(dict(state))
        return runtime_root / "runtime-state-v1.json"

    def route_reader(_root):
        calls["route_reads"] += 1
        return route

    result = _materialize_validated_dossier(
        _dossier(),
        publisher_workspace=publisher_root,
        output_directory=output_parent / "materialized",
        now=b4_fixtures.NOW,
        repo_root=repo_root,
        runtime_root=runtime_root,
        current_source_main_sha=SOURCE_MAIN_SHA,
        writer=writer,
        observer=lambda: observed,
        route_reader=route_reader,
        is_git_clean=lambda _path: (True, "clean"),
        clock=lambda: MATERIALIZATION_NOW,
    )
    return result, calls, output_parent / "materialized", observed


def test_source_runtime_inconsistency_fails_before_any_governed_write(tmp_path):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root, consistent=False)
    writes = []
    with pytest.raises(Top5B1MaterializationError, match="inconsistent"):
        _materialize_validated_dossier(
            _dossier(),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            runtime_root=tmp_path / "runtime",
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: pytest.fail("route must not be read"),
            is_git_clean=lambda _path: (True, "clean"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


def test_initial_window_miss_is_explicit_and_does_not_write_runtime_state(tmp_path):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    writes = []
    with pytest.raises(Top5B1MaterializationError, match="B1_LIFECYCLE_WINDOW_BLOCKED"):
        _materialize_validated_dossier(
            _dossier(kickoff_lead=timedelta(hours=48)),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            runtime_root=tmp_path / "runtime",
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: {
                "provider_authority": ACTIVE_PROVIDER,
                "activation_mode": "DISABLED",
                "publication": False,
                "betting": False,
                "ledger_mutation": False,
                "recurring_scheduler": False,
            },
            is_git_clean=lambda _path: (True, "clean"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


def test_unsafe_route_fails_closed_without_runtime_write_or_artifact(tmp_path):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    writes = []
    with pytest.raises(Top5B1MaterializationError, match="durable route"):
        _materialize_validated_dossier(
            _dossier(),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            runtime_root=tmp_path / "runtime",
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: {
                "provider_authority": ACTIVE_PROVIDER,
                "activation_mode": "ACTIVE",
                "publication": False,
                "betting": False,
                "ledger_mutation": False,
                "recurring_scheduler": False,
            },
            is_git_clean=lambda _path: (True, "clean"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


def test_missing_durable_route_fails_closed_before_runtime_write(tmp_path):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    writes = []
    with pytest.raises(Top5B1MaterializationError, match="durable disabled route"):
        _materialize_validated_dossier(
            _dossier(),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            runtime_root=tmp_path / "runtime",
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: (_ for _ in ()).throw(FileNotFoundError()),
            is_git_clean=lambda _path: (True, "clean"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


@pytest.mark.parametrize("timestamp_case", ("stale", "future"))
def test_stale_or_future_runtime_observation_fails_closed(tmp_path, timestamp_case):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    runtime_root = tmp_path / "runtime"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    observed = _runtime_artifact(b4_fixtures.NOW, runtime_root)
    if timestamp_case == "stale":
        observed["captured_at"] = (b4_fixtures.NOW - timedelta(seconds=901)).isoformat()
    else:
        observed["runtime_state_observed_at"] = (
            b4_fixtures.NOW + timedelta(seconds=1)
        ).isoformat()
    observed["artifact_digest"] = governed_runtime_evidence._canonical_digest(observed)
    writes = []
    route = {
        "provider_authority": ACTIVE_PROVIDER,
        "activation_mode": "DISABLED",
        "publication": False,
        "betting": False,
        "ledger_mutation": False,
        "recurring_scheduler": False,
    }
    with pytest.raises(Top5B1MaterializationError, match="timestamps"):
        _materialize_validated_dossier(
            _dossier(),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            runtime_root=runtime_root,
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            observer=lambda: observed,
            route_reader=lambda _root: route,
            is_git_clean=lambda _path: (True, "clean"),
            clock=lambda: b4_fixtures.NOW,
        )
    assert len(writes) == 1
    assert not (output_parent / "materialized").exists()


@pytest.mark.parametrize("workspace_state", ("missing", "dirty"))
def test_missing_or_dirty_publisher_workspace_fails_closed(tmp_path, workspace_state):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    if workspace_state == "dirty":
        publisher_root.mkdir()
    writes = []
    with pytest.raises(Top5B1MaterializationError, match="publisher workspace"):
        _materialize_validated_dossier(
            _dossier(),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: pytest.fail("route must not be read"),
            is_git_clean=lambda _path: (False, "uncommitted changes"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


@pytest.mark.parametrize("bad_market", ("missing_source", "stale"))
def test_missing_source_provenance_or_stale_b4_market_fails_closed(
    tmp_path, bad_market
):
    dossier = _dossier()
    shadow = dossier.controlled_shadow
    markets = list(shadow.market_evidence)
    target = markets[0]
    if bad_market == "missing_source":
        markets[0] = replace(target, source_provenance="")
    else:
        markets[0] = replace(
            target,
            odds_timestamp=b4_fixtures.NOW - timedelta(seconds=301),
            captured_at=b4_fixtures.NOW - timedelta(seconds=300),
        )
    invalid_dossier = replace(
        dossier,
        controlled_shadow=replace(shadow, market_evidence=tuple(markets)),
    )
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    writes = []
    with pytest.raises(
        Top5B1MaterializationError, match="validated B4 dossier rejected"
    ):
        _materialize_validated_dossier(
            invalid_dossier,
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            current_source_main_sha=SOURCE_MAIN_SHA,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: pytest.fail("route must not be read"),
            is_git_clean=lambda _path: (True, "clean"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


def test_b4_source_sha_mismatch_fails_before_runtime_write(tmp_path):
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root)
    writes = []
    with pytest.raises(Top5B1MaterializationError, match="current main SHA"):
        _materialize_validated_dossier(
            _dossier(),
            publisher_workspace=publisher_root,
            output_directory=output_parent / "materialized",
            now=b4_fixtures.NOW,
            repo_root=repo_root,
            current_source_main_sha="c" * 40,
            writer=lambda state: writes.append(state),
            route_reader=lambda _root: pytest.fail("route must not be read"),
            is_git_clean=lambda _path: (True, "clean"),
        )
    assert writes == []
    assert not (output_parent / "materialized").exists()


def test_operator_cli_loads_private_external_dossier_and_materializes_offline(
    tmp_path, monkeypatch, capsys
):
    from scripts import top5_b1_real_input_materializer as cli
    from src.football import top5_b1_real_input_materializer as materializer
    from src.football import top5_final_acceptance_composer as composer

    now = b4_fixtures.NOW
    repo_root = tmp_path / "source"
    publisher_root = tmp_path / "publisher"
    output_parent = tmp_path / "private"
    runtime_root = tmp_path / "runtime"
    dossier_path = tmp_path / "real-b4-dossier.json"
    repo_root.mkdir()
    publisher_root.mkdir()
    output_parent.mkdir()
    _source_files(repo_root, now=now)
    dossier_path.write_text(
        json.dumps(_dossier().as_payload(now=now)), encoding="utf-8"
    )
    observed = _runtime_artifact(MATERIALIZATION_NOW, runtime_root)
    clock_values = iter((now, MATERIALIZATION_NOW))
    route = {
        "provider_authority": ACTIVE_PROVIDER,
        "activation_mode": "DISABLED",
        "publication": False,
        "betting": False,
        "ledger_mutation": False,
        "recurring_scheduler": False,
    }
    writes = []

    monkeypatch.setattr(materializer, "ROOT", repo_root)
    monkeypatch.setattr(materializer, "_utc_now", lambda: next(clock_values))
    monkeypatch.setattr(materializer, "_current_git_sha", lambda _root: SOURCE_MAIN_SHA)
    monkeypatch.setattr(materializer, "governed_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(
        materializer,
        "write_governed_runtime_state",
        lambda state: (
            writes.append(dict(state)) or runtime_root / "runtime-state-v1.json"
        ),
    )
    monkeypatch.setattr(
        governed_runtime_evidence,
        "observe_governed_runtime",
        lambda: observed,
    )
    monkeypatch.setattr(
        governed_runtime_evidence,
        "_read_route_state",
        lambda _root: route,
    )
    monkeypatch.setattr(
        governed_runtime_evidence,
        "_git_clean",
        lambda _path: (True, "clean"),
    )
    monkeypatch.setattr(composer, "observe_governed_runtime", lambda: observed)

    exit_code = cli.main(
        [
            "--b4-dossier",
            str(dossier_path),
            "--publisher-workspace",
            str(publisher_root),
            "--output-dir",
            str(output_parent / "materialized"),
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert result["b1_status"] == STATUS_VERIFIED
    assert result["public_precheck_status"] == TOP5_PUBLICATION_PRECHECK_READY
    assert result["provider_requests"] == 0
    assert len(writes) == 1
