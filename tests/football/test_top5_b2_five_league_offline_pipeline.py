"""Offline regressions for the canonical B4 -> B2 five-league pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.football.top5_b2_qualification_batch_orchestrator import (
    Builder2FiveLeagueReceiptPackageV1,
    Builder2QualificationBatchError,
    build_five_league_shadow_package,
    consume_five_league_shadow_package,
    run_five_league_receipt_pipeline,
    write_five_league_receipt_package,
)
from src.football.top5_controlled_shadow_provider_qualification import TOP5_LEAGUES
from tests.football.test_top5_b2_five_league_receipt import (
    _canonical_run_and_manifests,
)


def _package():
    run, manifests = _canonical_run_and_manifests()
    return build_five_league_shadow_package(run, manifests)


def _raw_package() -> dict[str, object]:
    return json.loads(json.dumps(_package().as_payload()))


def _write_raw_package(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "b4-five-league-package.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_one_shot_produces_five_receipts_and_one_non_authorizing_dossier(tmp_path):
    source = _package()
    input_path = _write_raw_package(tmp_path, source.as_payload())
    output_path = tmp_path / "b2-five-league-receipts.json"

    result = run_five_league_receipt_pipeline(input_path, output_json=output_path)

    assert len(result.receipts) == 5
    assert tuple(binding.league for binding in result.dossier.bindings) == TOP5_LEAGUES
    assert (
        tuple(receipt.qualification_status.value for receipt in result.receipts)
        == ("REAL_OBSERVATION_VALIDATED",) * 5
    )
    assert result.dossier.authority_granted is False
    assert result.dossier.provider_authority_selected is False
    assert result.dossier.publication is False
    assert result.dossier.production_activation is False
    assert result.dossier.betting is False
    assert result.safety["provider_authority_granted"] is False
    assert output_path.is_file()
    assert (
        Builder2FiveLeagueReceiptPackageV1.from_payload(
            json.loads(output_path.read_text(encoding="utf-8"))
        ).as_payload()
        == result.as_payload()
    )


def test_same_package_is_deterministic_and_output_is_idempotent(tmp_path):
    source = _package()
    first = consume_five_league_shadow_package(source)
    second = consume_five_league_shadow_package(source)
    output_path = tmp_path / "b2-five-league-receipts.json"

    write_five_league_receipt_package(first, output_json=output_path)
    first_bytes = output_path.read_bytes()
    write_five_league_receipt_package(second, output_json=output_path)

    assert first.as_payload() == second.as_payload()
    assert first_bytes == output_path.read_bytes()
    assert first.package_id == second.package_id
    assert first.dossier.dossier_id == second.dossier.dossier_id
    assert [item.qualification_receipt_id for item in first.receipts] == [
        item.qualification_receipt_id for item in second.receipts
    ]


def test_invalid_package_fails_before_any_output_is_committed(tmp_path):
    raw = _raw_package()
    raw["capture_envelopes"].pop()
    input_path = _write_raw_package(tmp_path, raw)
    output_path = tmp_path / "must-not-exist.json"

    with pytest.raises(Builder2QualificationBatchError):
        run_five_league_receipt_pipeline(input_path, output_json=output_path)

    assert not output_path.exists()


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda raw: raw["capture_envelopes"].__setitem__(
                -1, raw["capture_envelopes"][0]
            ),
            id="duplicate-league",
        ),
        pytest.param(
            lambda raw: raw["manifests"].pop(),
            id="missing-league-manifest",
        ),
        pytest.param(
            lambda raw: raw["capture_envelopes"][0]["capture"].__setitem__(
                "evidence_kind", "TEST_FIXTURE"
            ),
            id="synthetic-test-fixture",
        ),
        pytest.param(
            lambda raw: raw["capture_envelopes"][0]["target"].__setitem__(
                "provider", "the_odds_api"
            ),
            id="incorrect-provider",
        ),
        pytest.param(
            lambda raw: raw["manifests"][0].__setitem__(
                "ceo_authorization_id", "mixed-ceo-authorization"
            ),
            id="mixed-authorization-ids",
        ),
        pytest.param(
            lambda raw: raw["shadow_run"].__setitem__(
                "datapoint_count", raw["shadow_run"]["datapoint_count"] + 1
            ),
            id="quota-contradiction",
        ),
        pytest.param(
            lambda raw: raw["capture_envelopes"][0]["capture"]["response"].__setitem__(
                "captured_at", "2030-01-01T00:00:00+00:00"
            ),
            id="stale-or-altered-observation",
        ),
        pytest.param(
            lambda raw: raw["manifests"][2]["observation"].__setitem__(
                "fixture_key", "altered-fixture"
            ),
            id="altered-observation",
        ),
    ],
)
def test_package_identity_and_evidence_tampering_fails_closed(tamper, tmp_path):
    raw = _raw_package()
    tamper(raw)
    input_path = _write_raw_package(tmp_path, raw)

    with pytest.raises(Builder2QualificationBatchError):
        run_five_league_receipt_pipeline(input_path)


def test_conflicting_existing_output_is_not_overwritten(tmp_path):
    output_path = tmp_path / "b2-five-league-receipts.json"
    output_path.write_text(json.dumps({"attacker": True}), encoding="utf-8")

    with pytest.raises(Builder2QualificationBatchError):
        write_five_league_receipt_package(
            consume_five_league_shadow_package(_package()),
            output_json=output_path,
        )

    assert json.loads(output_path.read_text(encoding="utf-8")) == {"attacker": True}


def test_offline_pipeline_has_no_network_or_authority_side_effects():
    result = consume_five_league_shadow_package(_package())

    assert result.safety["no_provider_calls"] is True
    assert result.safety["provider_network_execution"] is False
    assert result.dossier.authority_granted is False
    assert result.dossier.provider_authority_selected is False
    assert result.dossier.production_activation is False
    assert result.dossier.publication is False
    assert result.dossier.betting is False
