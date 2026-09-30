from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_live_release import (
    build_nations_league_release,
    historical_live_projection,
)
from src.models.live_model_lifecycle import ModelLifecycleError
from src.notifications.nations_league_live_public import (
    NationsLeagueLivePublicError,
    build_live_public_nations_league,
    validate_live_public_nations_league,
)
from src.notifications.nations_league_public import validate_public_nations_league
from src.notifications.public_serializer import serialize_public_product

ROOT = Path(__file__).resolve().parents[2]


def state():
    return json.loads(
        (
            ROOT
            / "results/research/nations_league_v1_1_input_state_20260930T200124Z.json"
        ).read_text()
    )


def source_record():
    return json.loads(
        (
            ROOT
            / "results/research/nations_league_v1_1_forward_shadow_store_alias_20260930T200124Z.jsonl"
        )
        .read_text()
        .splitlines()[0]
    )


def test_live_release_is_causal_and_public_adapter_preserves_immutable_capture():
    snap, release, receipts = build_nations_league_release(
        state(), created_at="2026-09-30T20:01:24Z"
    )
    assert snap["training_rows"] == 566
    assert receipts["validation"]["sample_threshold_required"] is False
    original = source_record()
    before = deepcopy(original)
    reference = historical_live_projection(original, release)
    public = build_live_public_nations_league(
        reference, release, generated_at="2026-09-30T20:01:25Z"
    )
    assert validate_live_public_nations_league(public)["lifecycle"] == "LIVE_MODEL"
    assert validate_public_nations_league(public)["lifecycle"] == "LIVE_MODEL"
    assert (
        serialize_public_product({"nations_league": public})["nations_league"] == public
    )
    assert original == before


def test_stale_or_nonready_input_cannot_build_live_release():
    invalid = state()
    invalid["completeness"]["status"] = "STALE_INPUT"
    with pytest.raises(ModelLifecycleError):
        build_nations_league_release(invalid, created_at="2026-09-30T20:01:24Z")


def test_new_valid_state_has_a_new_release_and_tampered_public_payload_fails():
    first_state = state()
    _, first, _ = build_nations_league_release(
        first_state, created_at="2026-09-30T20:01:24Z"
    )
    changed = deepcopy(first_state)
    changed["elo_state"]["Austria"] += 1
    # A caller must recompute and validate a new input state before this can be used.
    with pytest.raises(ModelLifecycleError):
        build_nations_league_release(changed, created_at="2026-09-30T20:02:24Z")
    public = build_live_public_nations_league(
        historical_live_projection(source_record(), first),
        first,
        generated_at="2026-09-30T20:01:25Z",
    )
    public["no_bet"] = False
    with pytest.raises(NationsLeagueLivePublicError):
        validate_live_public_nations_league(public)
