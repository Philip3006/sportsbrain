"""Result-only causal continuation; no target predictions are generated."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_result_extension import (
    MODEL_DIGEST,
    build_extension,
    completeness,
    elo_continuation,
    seal,
    validate_append_only_successor,
)
from src.analysis.nations_league_v1_1 import model_digest, sha256_json

ROOT = Path(__file__).resolve().parents[2]
GENERATED = "2026-09-30T17:15:00Z"
CUTOFF = "2026-09-30T20:00:00Z"


def inputs():
    base = json.loads(
        (ROOT / "results/research/nations_league_fixture_timeline_v1.json").read_text()
    )
    source = json.loads(
        (
            ROOT
            / "data/research/nations_league/post_base_official_results_20260930.json"
        ).read_text()
    )
    return base, source


def schedule_manifest():
    return json.loads(
        (ROOT / "results/audits/nations_league_forward_fixture_manifest.json").read_text()
    )


def extension():
    return build_extension(*inputs(), generated_at=GENERATED)


def reseal(value):
    return seal(
        {k: v for k, v in value.items() if k != "extension_digest"}, "extension_digest"
    )


def test_official_inventory_and_frozen_digests():
    base, source = inputs()
    before = sha256_json(base)
    result = build_extension(base, source, generated_at=GENERATED)
    assert len(result["result_rows"]) == 56
    assert len([r for r in result["result_rows"] if r["edition"] == "2024/25"]) == 4
    assert len([r for r in result["result_rows"] if r["edition"] == "2026/27"]) == 52
    assert result["exception_rows"] == result["unresolved_rows"] == []
    assert model_digest() == MODEL_DIGEST
    assert result["base_timeline_digest"] == base["dataset_digest"]
    assert sha256_json(base) == before
    assert all(
        r["result_safe_available_at"] >= r["observed_at"] for r in result["result_rows"]
    )


def test_repeatable_digests_and_elo_state():
    a, b = extension(), extension()
    assert a == b
    assert completeness(a, CUTOFF) == completeness(b, CUTOFF)
    first = elo_continuation(inputs()[0], a, CUTOFF)
    assert first == elo_continuation(inputs()[0], b, CUTOFF)
    assert first["training_count"] == 566
    assert first["prediction_generated"] is False


@pytest.mark.parametrize(
    "failure", ["non_nl", "duplicate", "missing", "ambiguous", "base_duplicate"]
)
def test_invalid_extension_source_rejected(failure):
    base, source = inputs()
    if failure == "non_nl":
        source["records"][0]["competition"] = "World Cup qualification"
    elif failure == "duplicate":
        source["records"].append(deepcopy(source["records"][0]))
    elif failure == "missing":
        source["records"].pop()
    elif failure == "ambiguous":
        source["records"][0]["home_team_source"] = "unknown ambiguous team"
    else:
        row = next(
            r for r in base["records"] if r.get("administrative_exception") is None
        )
        source["records"][0].update(
            home_team_source=row["home_team"],
            away_team_source=row["away_team"],
            kickoff_utc=row["kickoff_utc"],
            edition=row["edition"],
        )
    with pytest.raises(ValueError):
        build_extension(base, source, generated_at=GENERATED)


@pytest.mark.parametrize("safe", [CUTOFF, "2026-09-30T20:00:01Z"])
def test_safe_at_or_after_cutoff_rejected(safe):
    value = extension()
    value["result_rows"][0]["result_safe_available_at"] = safe
    with pytest.raises(ValueError, match="strictly before"):
        completeness(reseal(value), CUTOFF)


def test_unresolved_and_incomplete_interval_block_ready():
    base, source = inputs()
    source["records"][0]["played_status"] = "Unknown"
    value = build_extension(base, source, generated_at=GENERATED)
    assert completeness(value, CUTOFF)["status"] == "LIVE_RESULT_REFRESH_REQUIRED"
    with pytest.raises(ValueError, match="incomplete"):
        elo_continuation(base, value, CUTOFF)
    source = inputs()[1]
    source["coverage"]["interval_complete"] = False
    assert (
        completeness(build_extension(base, source, generated_at=GENERATED), CUTOFF)[
            "status"
        ]
        == "LIVE_RESULT_REFRESH_REQUIRED"
    )


def test_stale_future_cutoff_and_causal_snapshot_ready():
    value = extension()
    assert completeness(value, CUTOFF)["status"] == "STALE_INPUT"
    assert completeness(value, value["results_verified_through"])["status"] == "READY"
    with pytest.raises(ValueError, match="noncausal"):
        completeness(value, "2026-09-30T16:00:00Z")


@pytest.mark.parametrize(
    "cutoff",
    [
        "2026-10-01T14:30:00Z",
        "2026-10-01T17:15:00Z",
        "2026-10-01T22:00:00Z",
    ],
)
def test_schedule_causal_carry_forward_keeps_older_complete_extension_ready(cutoff):
    proof = completeness(extension(), cutoff, schedule_manifest=schedule_manifest())
    assert proof["status"] == "READY"
    assert proof["coverage_mode"] == "SCHEDULE_CAUSAL_CARRY_FORWARD"
    assert proof["next_possible_result_safe_at"] == "2026-10-01T22:00:00+00:00"
    assert proof["schedule_manifest_digest"] == schedule_manifest()["manifest_digest"]


def test_schedule_carry_forward_requires_new_results_after_safe_horizon():
    proof = completeness(
        extension(), "2026-10-01T22:15:00Z", schedule_manifest=schedule_manifest()
    )
    assert proof["status"] == "LIVE_RESULT_REFRESH_REQUIRED"
    assert proof["coverage_mode"] == "DIRECT_OFFICIAL_OBSERVATION_REQUIRED"


def test_schedule_carry_forward_rejects_tampered_or_unresolved_schedule():
    manifest = schedule_manifest()
    tampered = deepcopy(manifest)
    tampered["fixtures"][0]["home_team"] = "Unknown Team"
    with pytest.raises(ValueError, match="schedule carry-forward"):
        completeness(extension(), "2026-10-01T17:15:00Z", schedule_manifest=tampered)
    unresolved = deepcopy(manifest)
    unresolved["fixtures"][0]["status"] = "UNRESOLVED"
    unresolved["manifest_digest"] = sha256_json(
        {key: value for key, value in unresolved.items() if key != "manifest_digest"}
    )
    with pytest.raises(ValueError, match="schedule carry-forward"):
        completeness(
            extension(), "2026-10-01T17:15:00Z", schedule_manifest=unresolved
        )


def test_removal_or_changed_result_changes_state_or_fails():
    base, source = inputs()
    before = extension()
    source["records"][-1]["home_score"] += 1
    after = build_extension(base, source, generated_at=GENERATED)
    assert after["extension_digest"] != before["extension_digest"]
    assert (
        elo_continuation(base, after, CUTOFF)["elo_state_digest"]
        != elo_continuation(base, before, CUTOFF)["elo_state_digest"]
    )
    with pytest.raises(ValueError, match="append-only"):
        validate_append_only_successor(before, after)
    removed = deepcopy(before)
    removed["result_rows"].pop()
    with pytest.raises(ValueError, match="accounting"):
        completeness(reseal(removed), CUTOFF)


def test_append_only_replay_and_tamper():
    value = extension()
    validate_append_only_successor(value, value)
    value["result_rows"][0]["home_score"] += 1
    with pytest.raises(ValueError, match="digest mismatch"):
        completeness(value, CUTOFF)


def test_materialized_artifacts_reproduce_exactly():
    saved = json.loads(
        (
            ROOT / "results/research/nations_league_v1_1_result_extension_20260930.json"
        ).read_text()
    )
    rebuilt = build_extension(*inputs(), generated_at=saved["generated_at"])
    assert saved == rebuilt
    for filename, actual in (
        (
            "nations_league_v1_1_result_completeness_20260930T200000Z.json",
            completeness(saved, CUTOFF),
        ),
        (
            "nations_league_v1_1_elo_continuation_20260930T200000Z.json",
            elo_continuation(inputs()[0], saved, CUTOFF),
        ),
    ):
        assert json.loads((ROOT / "results/audits" / filename).read_text()) == actual


def test_backdated_or_source_inconsistent_result_rejected():
    value = extension()
    value["result_rows"][0]["result_safe_available_at"] = "2026-03-27T12:00:00Z"
    with pytest.raises(ValueError, match="backdated"):
        completeness(reseal(value), CUTOFF)
    value = extension()
    value["result_rows"][0]["home_score"] += 1
    with pytest.raises(ValueError, match="score mismatch"):
        completeness(reseal(value), CUTOFF)
