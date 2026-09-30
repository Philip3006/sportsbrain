"""Offline, fail-closed input gate for the frozen Nations League v1.1 runner.

Completeness is an explicit upstream assertion, never inferred from the last match.
No provider, odds, ledger, filesystem or clock access occurs here.
"""

import unicodedata
from copy import deepcopy

from src.analysis.nations_league_competition_state import (
    ALIASES,
    NORMALIZATION_VERSION,
    canonical_team,
)
from src.analysis.nations_league_result_extension import completeness
from src.analysis.nations_league_v1_1 import (
    TRAINING_TIMELINE_DATASET_DIGEST,
    _parse_utc,
    _require_digest,
    build_forward_shadow_prediction,
    fit_causal_elo,
    model_digest,
    sha256_json,
    training_records_from_timeline,
    validate_point_in_time_training,
    validate_shadow_record,
    validate_target_fixture,
)

FROZEN_DIGEST = model_digest()
MODEL_VERSION = "nations_league_v1_1"


def _normalized_identity_key(value):
    """Match the existing alias contract without adding normalization rules."""

    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _identity_binding(source_team, ratings, historical_spellings):
    """Return the sealed source-to-model identity decision for one target team."""

    canonical = canonical_team(source_team)
    explicit_alias = (
        source_team != canonical
        and _normalized_identity_key(source_team) in ALIASES
        and ALIASES[_normalized_identity_key(source_team)] == canonical
    )
    canonical_identity = source_team == canonical
    if explicit_alias:
        resolution = "EXPLICIT_EXISTING_ALIAS"
    elif canonical_identity and canonical in ratings:
        resolution = "IDENTITY"
    else:
        resolution = "UNRESOLVED"
    if (
        canonical.casefold() in historical_spellings
        and historical_spellings[canonical.casefold()] != canonical
    ):
        resolution = "AMBIGUOUS_IDENTITY"
    return {
        "source_team": source_team,
        "canonical_team": canonical,
        "resolution": resolution,
        "normalization_version": NORMALIZATION_VERSION,
    }


def _identity_bindings_digest(bindings):
    return sha256_json(
        {
            "normalization_version": NORMALIZATION_VERSION,
            "bindings": bindings,
        }
    )


def timeline_training(timeline):
    """Verify committed timeline hashes and adapt its 510 safe result records.

    Administrative exceptions remain excluded, not fabricated as played results.
    The frozen model's existing absent-neutral default is preserved.
    """
    return training_records_from_timeline(timeline)


def build_input_state(
    fixtures,
    training_records,
    *,
    prediction_cutoff,
    provenance,
    base_timeline=None,
    result_extension=None,
    completeness_artifact=None,
    include_identity_bindings=True,
):
    """Construct exact causal state; report readiness without default team ratings.

    READY is derived only from the sealed #235 completeness artifact and its
    sealed extension/base bindings.  Caller-supplied watermark fields alone
    can never produce READY.  Missing evidence requires live-result refresh;
    an older valid proof is STALE_INPUT.
    """
    if model_digest() != FROZEN_DIGEST:
        raise ValueError("frozen model digest mismatch")
    cutoff = _parse_utc(prediction_cutoff, "prediction_cutoff")
    _require_digest(provenance.get("source_digest"), "source_digest")
    if (
        not isinstance(provenance.get("source_provenance"), str)
        or not provenance["source_provenance"].strip()
    ):
        raise ValueError("source provenance missing")
    rows = deepcopy(list(training_records))
    seen_input_ids = set()
    seen_input_identities = set()
    for row in rows:
        identity = (row["kickoff_utc"], row["home_team"], row["away_team"])
        if row["fixture_id"] in seen_input_ids or identity in seen_input_identities:
            raise ValueError("duplicate result")
        seen_input_ids.add(row["fixture_id"])
        seen_input_identities.add(identity)
        row["result_safe_available_at"] = _parse_utc(
            row["result_safe_available_at"], "result_safe_available_at"
        ).isoformat()
    base_digest = None
    extension_digest = None
    completeness_digest = None
    completeness_state = "LIVE_RESULT_REFRESH_REQUIRED"
    completeness_value = None
    if all(
        value is not None
        for value in (base_timeline, result_extension, completeness_artifact)
    ):
        base_rows = training_records_from_timeline(base_timeline)
        expected_completeness = completeness(result_extension, cutoff.isoformat())
        if expected_completeness != completeness_artifact:
            raise ValueError("completeness artifact mismatch")
        expected_source_rows = deepcopy(base_rows + result_extension["result_rows"])
        for row in expected_source_rows:
            row["result_safe_available_at"] = _parse_utc(
                row["result_safe_available_at"], "result_safe_available_at"
            ).isoformat()
        expected_rows = validate_point_in_time_training(
            expected_source_rows, prediction_cutoff
        )
        supplied_rows = validate_point_in_time_training(rows, prediction_cutoff)
        for collection in (expected_rows, supplied_rows):
            for row in collection:
                row["result_safe_available_at"] = _parse_utc(
                    row["result_safe_available_at"], "result_safe_available_at"
                ).isoformat()
        if supplied_rows != expected_rows:
            raise ValueError("training rows do not match sealed base/extension")
        base_digest = TRAINING_TIMELINE_DATASET_DIGEST
        extension_digest = result_extension["extension_digest"]
        completeness_digest = completeness_artifact["completeness_digest"]
        completeness_state = completeness_artifact["status"]
        completeness_value = deepcopy(dict(completeness_artifact))
    ids, identities, spellings = set(), set(), {}
    for row in rows:
        identity = (row["kickoff_utc"], row["home_team"], row["away_team"])
        if row["fixture_id"] in ids or identity in identities:
            raise ValueError("duplicate result")
        ids.add(row["fixture_id"])
        identities.add(identity)
        for field in ("home_team", "away_team"):
            team = row[field]
            canonical = canonical_team(team)
            if canonical != team or spellings.get(team.casefold(), team) != team:
                raise ValueError("ambiguous team alias in history")
            spellings[team.casefold()] = team
        for field in ("home_score", "away_score"):
            if type(row.get(field)) is not int or row[field] < 0:
                raise ValueError("invalid result score")
        # Normalize UTC spellings before the frozen runner's stable string sort.
        row["result_safe_available_at"] = _parse_utc(
            row["result_safe_available_at"], "result_safe_available_at"
        ).isoformat()
        if not _parse_utc(row["kickoff_utc"], "kickoff_utc") < _parse_utc(
            row["result_safe_available_at"], "result_safe_available_at"
        ):
            raise ValueError("result cannot be safe before kickoff")
    rows = validate_point_in_time_training(rows, prediction_cutoff)
    ratings = fit_causal_elo(rows, prediction_cutoff)
    freshness = completeness_state
    targets = deepcopy(list(fixtures))
    readiness = {}
    target_ids = set()
    identity_bindings = {}
    for fixture in targets:
        validate_target_fixture(fixture)
        if fixture["fixture_id"] in target_ids or fixture["fixture_id"] in ids:
            raise ValueError("duplicate target identity")
        target_ids.add(fixture["fixture_id"])
        if _parse_utc(fixture["kickoff_utc"], "kickoff_utc") <= cutoff:
            raise ValueError("target must be future")
        for field in ("home_team", "away_team"):
            team = fixture[field]
            binding = _identity_binding(team, ratings, spellings)
            identity_bindings[team] = binding
            canonical = binding["canonical_team"]
            known_identity = binding["resolution"] in {
                "IDENTITY",
                "EXPLICIT_EXISTING_ALIAS",
            }
            conflicting_identity = (
                spellings.get(canonical.casefold(), canonical) != canonical
            )
            readiness[team] = (
                "AMBIGUOUS_IDENTITY"
                if conflicting_identity or (not known_identity and canonical in ratings)
                else "MISSING_TEAM"
                if canonical not in ratings
                else freshness
            )
    snapshot = {
        "schema": "nations-league-forward-input-v1_1",
        "model_version": MODEL_VERSION,
        "prediction_cutoff": cutoff.isoformat(),
        "model_digest": FROZEN_DIGEST,
        "base_timeline_digest": base_digest,
        "result_extension_digest": extension_digest,
        "completeness_digest": completeness_digest,
        "completeness": completeness_value,
        "base_timeline": deepcopy(base_timeline),
        "result_extension": deepcopy(result_extension),
        "results_verified_through": (
            completeness_value.get("results_verified_through")
            if completeness_value
            else None
        ),
        "observed_at": (
            completeness_value.get("observed_at") if completeness_value else None
        ),
        "training_records": rows,
        "elo_state": ratings,
        "fixtures": targets,
        "provenance": deepcopy(provenance),
        "team_readiness": readiness,
        "shadow": True,
        "no_bet": True,
    }
    if include_identity_bindings:
        snapshot["identity_bindings"] = identity_bindings
        snapshot["identity_binding_digest"] = _identity_bindings_digest(
            identity_bindings
        )
    snapshot["input_snapshot_digest"] = sha256_json(snapshot)
    return snapshot


def predict_from_input_state(snapshot, fixture_id, *, phase):
    """Bind #227 to verified actual input; do not modify previous shadow records."""
    legacy_snapshot = "identity_bindings" not in snapshot
    rebuilt = build_input_state(
        snapshot["fixtures"],
        snapshot["training_records"],
        prediction_cutoff=snapshot["prediction_cutoff"],
        provenance=snapshot["provenance"],
        base_timeline=snapshot.get("base_timeline"),
        result_extension=snapshot.get("result_extension"),
        completeness_artifact=snapshot.get("completeness"),
        include_identity_bindings=not legacy_snapshot,
    )
    if rebuilt != snapshot:
        raise ValueError("input snapshot mismatch")
    fixture = next(
        (f for f in snapshot["fixtures"] if f["fixture_id"] == fixture_id), None
    )
    if fixture is None:
        raise ValueError("unknown fixture")
    if any(
        snapshot["team_readiness"][fixture[k]] != "READY"
        for k in ("home_team", "away_team")
    ):
        raise ValueError("forward input is not READY")
    model_fixture = deepcopy(fixture)
    binding = None
    if not legacy_snapshot:
        binding = snapshot["identity_bindings"].get(fixture["home_team"])
        away_binding = snapshot["identity_bindings"].get(fixture["away_team"])
        if binding is None or away_binding is None:
            raise ValueError("missing sealed identity binding")
        if binding["resolution"] not in {
            "IDENTITY",
            "EXPLICIT_EXISTING_ALIAS",
        } or away_binding["resolution"] not in {
            "IDENTITY",
            "EXPLICIT_EXISTING_ALIAS",
        }:
            raise ValueError("sealed identity binding is not READY")
        model_fixture["home_team"] = binding["canonical_team"]
        model_fixture["away_team"] = away_binding["canonical_team"]
    record = build_forward_shadow_prediction(
        model_fixture,
        phase=phase,
        prediction_timestamp=snapshot["prediction_cutoff"],
        training_records=snapshot["training_records"],
        input_provenance={
            "timeline_digest": snapshot["base_timeline_digest"],
            "fixture_source_digest": fixture["source_digest"],
            "input_snapshot_digest": snapshot["input_snapshot_digest"],
        },
    )
    if not legacy_snapshot:
        record["home_team"] = fixture["home_team"]
        record["away_team"] = fixture["away_team"]
        record["model_identity"] = {
            "home_team": binding["canonical_team"],
            "away_team": away_binding["canonical_team"],
            "normalization_version": NORMALIZATION_VERSION,
            "identity_binding_digest": snapshot["identity_binding_digest"],
        }
        record["source_evidence"] = [
            *record["source_evidence"],
            "sealed_identity_binding",
        ]
        validate_shadow_record(record)
    return record
