"""Offline, fail-closed input gate for the frozen Nations League shadow runner.

Completeness is an explicit upstream assertion, never inferred from the last match.
No provider, odds, ledger, filesystem or clock access occurs here.
"""

from copy import deepcopy

from src.analysis.nations_league_competition_state import canonical_team
from src.analysis.nations_league_v1 import (
    _parse_utc,
    _require_digest,
    build_forward_shadow_prediction,
    fit_causal_elo,
    model_digest,
    sha256_json,
    validate_point_in_time_training,
    validate_target_fixture,
)

FROZEN_DIGEST = "f55549e7225f55deac23c7a31b757acf509ad0b4810b93ba8244301d3395a8ee"


def timeline_training(timeline):
    """Verify committed timeline hashes and adapt its 510 safe result records.

    Administrative exceptions remain excluded, not fabricated as played results.
    The frozen model's existing absent-neutral default is preserved.
    """
    if timeline.get("competition") != "UEFA Nations League":
        raise ValueError("wrong competition")
    digest = timeline.get("dataset_digest")
    if digest != sha256_json(
        {k: v for k, v in timeline.items() if k != "dataset_digest"}
    ):
        raise ValueError("timeline digest mismatch")
    rows = []
    for record in timeline["records"]:
        if record.get("record_digest") != sha256_json(
            {k: v for k, v in record.items() if k != "record_digest"}
        ):
            raise ValueError("timeline record digest mismatch")
        if record.get("administrative_exception") is not None:
            continue
        row = {
            k: record[k]
            for k in (
                "fixture_id",
                "edition",
                "home_team",
                "away_team",
                "kickoff_utc",
                "result_safe_available_at",
                "home_score",
                "away_score",
            )
        }
        row.update(
            competition=timeline["competition"],
            evaluation_block=record["validation_period"],
            source_provenance=f"canonical-timeline:{digest}",
            source_digest=record["record_digest"],
        )
        rows.append(row)
    return rows


def build_input_state(fixtures, training_records, *, prediction_cutoff, provenance):
    """Construct exact causal state; report readiness without default team ratings.

    results_verified_through requires source digest/provenance and observed_at <=
    cutoff. It asserts completeness through that cutoff, not merely last-result age.
    Missing proof requires live-result refresh; an older proof is STALE_INPUT.
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
    freshness = "LIVE_RESULT_REFRESH_REQUIRED"
    watermark = provenance.get("results_verified_through")
    if watermark is not None:
        observed = _parse_utc(provenance.get("observed_at"), "observed_at")
        verified = _parse_utc(watermark, "results_verified_through")
        if observed > cutoff or verified > observed:
            raise ValueError("noncausal completeness proof")
        freshness = "READY" if verified == cutoff else "STALE_INPUT"
    targets = deepcopy(list(fixtures))
    readiness = {}
    target_ids = set()
    for fixture in targets:
        validate_target_fixture(fixture)
        if fixture["fixture_id"] in target_ids or fixture["fixture_id"] in ids:
            raise ValueError("duplicate target identity")
        target_ids.add(fixture["fixture_id"])
        if _parse_utc(fixture["kickoff_utc"], "kickoff_utc") <= cutoff:
            raise ValueError("target must be future")
        for field in ("home_team", "away_team"):
            team = fixture[field]
            ambiguous = (
                canonical_team(team) != team
                or spellings.get(team.casefold(), team) != team
            )
            readiness[team] = (
                "AMBIGUOUS_IDENTITY"
                if ambiguous
                else "MISSING_TEAM"
                if team not in ratings
                else freshness
            )
    snapshot = {
        "schema": "nations-league-forward-input-v1",
        "prediction_cutoff": cutoff.isoformat(),
        "model_digest": FROZEN_DIGEST,
        "training_records": rows,
        "elo_state": ratings,
        "fixtures": targets,
        "provenance": deepcopy(provenance),
        "team_readiness": readiness,
        "shadow": True,
        "no_bet": True,
    }
    snapshot["input_snapshot_digest"] = sha256_json(snapshot)
    return snapshot


def predict_from_input_state(snapshot, fixture_id, *, phase):
    """Bind #227 to verified actual input; do not modify previous shadow records."""
    rebuilt = build_input_state(
        snapshot["fixtures"],
        snapshot["training_records"],
        prediction_cutoff=snapshot["prediction_cutoff"],
        provenance=snapshot["provenance"],
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
    return build_forward_shadow_prediction(
        fixture,
        phase=phase,
        prediction_timestamp=snapshot["prediction_cutoff"],
        training_records=snapshot["training_records"],
        input_provenance={
            "timeline_digest": snapshot["provenance"]["source_digest"],
            "fixture_source_digest": fixture["source_digest"],
            "input_snapshot_digest": snapshot["input_snapshot_digest"],
        },
    )
