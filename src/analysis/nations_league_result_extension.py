"""Offline post-base NL evidence and Elo continuation; never predictions."""

from collections.abc import Mapping
from copy import deepcopy
from datetime import timedelta

from src.analysis.nations_league_competition_state import (
    canonical_fixture_id,
    canonical_team,
)
from src.analysis.nations_league_v1_1 import (
    COMPETITION,
    HISTORICAL_LATEST_KICKOFF_UTC,
    MODEL_VERSION,
    TRAINING_TIMELINE_DATASET_DIGEST,
    _parse_utc,
    _require_digest,
    fit_causal_elo,
    model_digest,
    sha256_json,
    training_records_from_timeline,
    validate_point_in_time_training,
)

MODEL_DIGEST = "50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626"


def utc(value):
    return _parse_utc(value, "timestamp")


def seal(payload, field):
    return {**payload, field: sha256_json(payload)}


def check_seal(payload, field):
    if payload.get(field) != sha256_json(
        {k: v for k, v in payload.items() if k != field}
    ):
        raise ValueError(f"{field} mismatch")


def _schedule_carry_forward(extension, cutoff, schedule_manifest):
    """Return a sealed schedule proof for an older, otherwise complete extension.

    The manifest is the only allowed source for proving that no result could
    have become safe between the extension watermark and ``cutoff``.  It is
    deliberately strict: an incomplete or ambiguous schedule cannot turn an
    older result observation into current evidence.
    """

    if not isinstance(schedule_manifest, Mapping):
        raise TypeError("schedule coverage is unavailable")
    if schedule_manifest.get("schema") != "nations-league-future-fixture-manifest-v1":
        raise ValueError("unsupported schedule manifest")
    if schedule_manifest.get("competition") != COMPETITION:
        raise ValueError("schedule competition mismatch")
    expected_digest = schedule_manifest.get("manifest_digest")
    if expected_digest != sha256_json(
        {key: value for key, value in schedule_manifest.items() if key != "manifest_digest"}
    ):
        raise ValueError("schedule manifest digest mismatch")
    fixtures = schedule_manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("schedule coverage is unavailable")
    watermark = utc(extension["results_verified_through"])
    identities = set()
    future = []
    for raw in fixtures:
        if not isinstance(raw, Mapping):
            raise TypeError("schedule fixture is malformed")
        fixture_id = raw.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            raise ValueError("schedule fixture identity is missing")
        kickoff = utc(raw.get("kickoff_utc"))
        home = raw.get("home_team")
        away = raw.get("away_team")
        if (
            not isinstance(home, str)
            or not isinstance(away, str)
            or not home.strip()
            or not away.strip()
            or canonical_team(home) == canonical_team(away)
        ):
            raise ValueError("ambiguous schedule fixture identity")
        identity = (kickoff.isoformat(), canonical_team(home), canonical_team(away))
        if fixture_id in identities or identity in identities:
            raise ValueError("duplicate schedule fixture identity")
        identities.add(fixture_id)
        identities.add(identity)
        if raw.get("status") != "VERIFIED":
            if kickoff > watermark:
                raise ValueError("schedule coverage is unresolved")
            continue
        source_digest = raw.get("source_digest")
        if not isinstance(source_digest, str) or len(source_digest) != 64 or any(
            char not in "0123456789abcdef" for char in source_digest
        ):
            raise ValueError("schedule source digest is missing")
        provenance = raw.get("source_provenance_records")
        if not isinstance(provenance, list) or not provenance:
            raise ValueError("schedule source provenance is missing")
        if kickoff > watermark:
            future.append(kickoff + timedelta(hours=6))
    if not future:
        raise ValueError("schedule coverage is unavailable")
    next_possible = min(future)
    return next_possible


def build_extension(base, inventory, *, generated_at):
    training_records_from_timeline(base)
    if model_digest() != MODEL_DIGEST:
        raise ValueError("frozen v1.1 model digest changed")
    coverage = inventory["coverage"]
    if coverage["competition"] != COMPETITION:
        raise ValueError("non-NL competition")
    if coverage["interval_start_exclusive"] != HISTORICAL_LATEST_KICKOFF_UTC:
        raise ValueError("wrong post-base boundary")
    expected = inventory["expected_source_match_ids"]
    if (
        len(set(expected)) != len(expected)
        or len(expected) != coverage["expected_count"]
    ):
        raise ValueError("duplicate/incomplete expected inventory")
    source_ids = [row["source_match_id"] for row in inventory["records"]]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("duplicate result identity")
    if set(source_ids) != set(expected):
        raise ValueError("incomplete interval inventory")
    observed = utc(inventory["observed_at"])
    if utc(generated_at) < observed:
        raise ValueError("generation precedes observation")
    if utc(coverage["verified_through"]) > observed:
        raise ValueError("noncausal coverage")
    base_ids = {r["fixture_id"] for r in base["records"]}
    base_keys = {
        (r["kickoff_utc"][:10], r["home_team"], r["away_team"]) for r in base["records"]
    }
    known_teams = {
        r[field] for r in base["records"] for field in ("home_team", "away_team")
    }
    rows, unresolved, seen = [], [], set()
    for original in sorted(inventory["records"], key=lambda r: r["source_match_id"]):
        source = deepcopy(original)
        kickoff = utc(source["kickoff_utc"])
        home, away = (
            canonical_team(source["home_team_source"]),
            canonical_team(source["away_team_source"]),
        )
        if home not in known_teams or away not in known_teams or home == away:
            raise ValueError("ambiguous team identity")
        identity = canonical_fixture_id(
            source["edition"], kickoff.date().isoformat(), home, away
        )
        if (
            identity in base_ids
            or (kickoff.date().isoformat(), home, away) in base_keys
        ):
            raise ValueError("base fixture duplicated")
        if kickoff <= utc(HISTORICAL_LATEST_KICKOFF_UTC) or kickoff > utc(
            coverage["verified_through"]
        ):
            raise ValueError("fixture outside post-base interval")
        if identity in seen:
            raise ValueError("duplicate fixture identity")
        seen.add(identity)
        if source.get("competition", COMPETITION) != COMPETITION:
            raise ValueError("non-NL competition")
        for digest in (
            source["match_page_sha256"],
            source["result_source"]["content_sha256"],
        ):
            _require_digest(digest, "source digest")
        for url in (source["source_url"], source["result_source"]["url"]):
            if not url.startswith("https://www.uefa.com/uefanationsleague/"):
                raise ValueError("authoritative NL source required")
        times = [
            utc(source["observed_at"]),
            utc(source["result_source"]["observed_at"]),
        ]
        if max(times) > observed:
            raise ValueError("source observed after inventory")
        row = {
            "fixture_id": identity,
            "source_fixture_id": source["source_match_id"],
            "competition": COMPETITION,
            "edition": source["edition"],
            "stage": source["stage"],
            "evaluation_block": "NL_" + source["edition"].replace("/", "_"),
            "home_team": home,
            "away_team": away,
            "kickoff_utc": kickoff.isoformat(),
            "source_url": source["source_url"],
            "observed_at": max(times).isoformat(),
            "source_provenance": "official UEFA result index + Finished match metadata; observation-time bound",
            "source_digest": sha256_json(source),
            "source_evidence": source,
        }
        if source["played_status"] != "Finished" or not source.get(
            "reported_full_time_at"
        ):
            unresolved.append({**row, "reason": "played/final completion unresolved"})
            continue
        final = utc(source["reported_full_time_at"])
        if not kickoff < final <= min(times):
            raise ValueError("noncausal final/source evidence")
        if any(
            type(source[k]) is not int or source[k] < 0
            for k in ("home_score", "away_score")
        ):
            raise ValueError("invalid score")
        safe = max(*times, kickoff + timedelta(hours=6))
        rows.append(
            {
                **row,
                "home_score": source["home_score"],
                "away_score": source["away_score"],
                "played_status": "FINAL",
                "result_safe_available_at": safe.isoformat(),
                "result_safe_semantics": "SAFE_BOUND: max(source observation, kickoff + repository 6h buffer); not exact publication time",
            }
        )
    return seal(
        {
            "schema": "nations-league-v1-1-forward-result-extension-v1",
            "model_version": MODEL_VERSION,
            "model_digest": MODEL_DIGEST,
            "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
            "extension_start_exclusive": HISTORICAL_LATEST_KICKOFF_UTC,
            "results_verified_through": coverage["verified_through"],
            "observed_at": inventory["observed_at"],
            "generated_at": generated_at,
            "expected_source_match_ids": sorted(expected),
            "included_fixture_ids": sorted(r["fixture_id"] for r in rows),
            "result_rows": sorted(
                rows, key=lambda r: (r["result_safe_available_at"], r["fixture_id"])
            ),
            "exception_rows": [],
            "unresolved_rows": unresolved,
            "interval_complete": coverage["interval_complete"] is True,
            "duplicate_identities": [],
            "ambiguous_team_identities": [],
            "provenance_digest": sha256_json(inventory),
            "coverage_evidence": deepcopy(coverage),
            "no_bet": True,
            "publication_enabled": False,
        },
        "extension_digest",
    )


def completeness(extension, prediction_cutoff, *, schedule_manifest=None):
    check_seal(extension, "extension_digest")
    cutoff = utc(prediction_cutoff)
    if (
        extension["model_digest"] != MODEL_DIGEST
        or extension["base_timeline_digest"] != TRAINING_TIMELINE_DATASET_DIGEST
    ):
        raise ValueError("frozen contract mismatch")
    if (
        utc(extension["observed_at"]) > cutoff
        or utc(extension["results_verified_through"]) > cutoff
    ):
        raise ValueError("noncausal completeness proof")
    validate_point_in_time_training(extension["result_rows"], prediction_cutoff)
    for row in extension["result_rows"]:
        evidence = row["source_evidence"]
        if row["source_digest"] != sha256_json(evidence):
            raise ValueError("result source digest mismatch")
        if any(row[field] != evidence[field] for field in ("home_score", "away_score")):
            raise ValueError("result/source score mismatch")
        if (
            row["home_team"] != canonical_team(evidence["home_team_source"])
            or row["away_team"] != canonical_team(evidence["away_team_source"])
            or utc(row["kickoff_utc"]) != utc(evidence["kickoff_utc"])
            or row["source_fixture_id"] != evidence["source_match_id"]
        ):
            raise ValueError("result/source identity mismatch")
        if utc(row["result_safe_available_at"]) < max(
            utc(evidence["observed_at"]),
            utc(evidence["result_source"]["observed_at"]),
            utc(row["kickoff_utc"]) + timedelta(hours=6),
        ):
            raise ValueError("result-safe bound backdated")
    accounted = (
        extension["result_rows"]
        + extension["exception_rows"]
        + extension["unresolved_rows"]
    )
    ids = [r["source_fixture_id"] for r in accounted]
    if len(ids) != len(set(ids)) or set(ids) != set(
        extension["expected_source_match_ids"]
    ):
        raise ValueError("incomplete/duplicate extension accounting")
    if extension["included_fixture_ids"] != sorted(
        r["fixture_id"] for r in extension["result_rows"]
    ):
        raise ValueError("included fixture identity mismatch")
    if extension["duplicate_identities"]:
        raise ValueError("duplicate identity accounting")
    identity_status = (
        "AMBIGUOUS_IDENTITY"
        if extension["ambiguous_team_identities"]
        else "UNAMBIGUOUS"
    )
    coverage_mode = None
    next_possible_result_safe_at = None
    schedule_manifest_digest = None
    if (
        identity_status == "UNAMBIGUOUS"
        and not extension["unresolved_rows"]
        and extension["interval_complete"]
        and utc(extension["results_verified_through"]) != cutoff
        and schedule_manifest is not None
    ):
        try:
            next_possible = _schedule_carry_forward(
                extension, cutoff, schedule_manifest
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("schedule carry-forward proof is invalid") from exc
        coverage_mode = "SCHEDULE_CAUSAL_CARRY_FORWARD"
        next_possible_result_safe_at = next_possible.isoformat()
        schedule_manifest_digest = schedule_manifest["manifest_digest"]
        if cutoff > next_possible:
            coverage_mode = "DIRECT_OFFICIAL_OBSERVATION_REQUIRED"
    status = (
        "AMBIGUOUS_IDENTITY"
        if identity_status != "UNAMBIGUOUS"
        else "LIVE_RESULT_REFRESH_REQUIRED"
        if extension["unresolved_rows"] or not extension["interval_complete"]
        else "READY"
        if utc(extension["results_verified_through"]) == cutoff
        or coverage_mode == "SCHEDULE_CAUSAL_CARRY_FORWARD"
        else "LIVE_RESULT_REFRESH_REQUIRED"
        if coverage_mode == "DIRECT_OFFICIAL_OBSERVATION_REQUIRED"
        else "STALE_INPUT"
    )
    proof = {
            "schema": "nations-league-v1-1-result-completeness-v1",
            "model_version": MODEL_VERSION,
            "model_digest": MODEL_DIGEST,
            "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
            "extension_digest": extension["extension_digest"],
            "prediction_cutoff": cutoff.isoformat(),
            "results_verified_through": extension["results_verified_through"],
            "observed_at": extension["observed_at"],
            "source_provenance": "reviewed official post-base inventory",
            "expected_count": len(extension["expected_source_match_ids"]),
            "verified_count": len(extension["result_rows"]),
            "unresolved_count": len(extension["unresolved_rows"]),
            "exception_count": len(extension["exception_rows"]),
            "identity_status": identity_status,
            "provenance_digest": extension["provenance_digest"],
            "status": status,
            "no_bet": True,
            "publication_enabled": False,
    }
    if schedule_manifest is not None:
        proof.update(
            {
                "coverage_mode": coverage_mode or "DIRECT_OFFICIAL_OBSERVATION",
                "next_possible_result_safe_at": next_possible_result_safe_at,
                "schedule_manifest_digest": schedule_manifest_digest,
            }
        )
    return seal(proof, "completeness_digest")


def elo_continuation(base, extension, prediction_cutoff, *, schedule_manifest=None):
    proof = completeness(
        extension, prediction_cutoff, schedule_manifest=schedule_manifest
    )
    if (
        proof["unresolved_count"]
        or not extension["interval_complete"]
        or proof["identity_status"] != "UNAMBIGUOUS"
    ):
        raise ValueError("incomplete continuation evidence")
    rows = validate_point_in_time_training(
        training_records_from_timeline(base) + extension["result_rows"],
        prediction_cutoff,
    )
    state = fit_causal_elo(rows, prediction_cutoff)
    return seal(
        {
            "schema": "nations-league-v1-1-elo-continuation-proof-v1",
            "model_digest": MODEL_DIGEST,
            "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
            "extension_digest": extension["extension_digest"],
            "prediction_cutoff": prediction_cutoff,
            "training_count": len(rows),
            "elo_state": state,
            "elo_state_digest": sha256_json(state),
            "prediction_generated": False,
            "completeness_state": proof["status"],
        },
        "continuation_digest",
    )


def validate_append_only_successor(previous, successor):
    """Existing observed rows are immutable; new rows may only extend coverage."""
    check_seal(previous, "extension_digest")
    check_seal(successor, "extension_digest")
    if utc(successor["results_verified_through"]) < utc(
        previous["results_verified_through"]
    ):
        raise ValueError("coverage cannot move backwards")
    for collection in ("result_rows", "exception_rows", "unresolved_rows"):
        later = {row["fixture_id"]: row for row in successor[collection]}
        for row in previous[collection]:
            if later.get(row["fixture_id"]) != row:
                raise ValueError("extension is append-only; prior evidence changed")
