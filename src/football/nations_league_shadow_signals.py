"""Deterministic, offline value-signal candidates for the frozen NL snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from src.betting.gates import gate_for
from src.betting.value_detector import BetSignal, detect_value
from src.config import MIN_EDGE
from src.notifications.nations_league_public import (
    NationsLeaguePublicError,
    _public_digest,
    validate_public_nations_league,
)

EXPECTED_PUBLIC_DIGEST = (
    "59f5aa67e18c89177e24824473b36040d5508094871cadaee43ba9a1478b125a"
)
EXPECTED_CAPTURED_AT = "2026-09-28T00:31:25.441175+00:00"
EXPECTED_FIXTURE_COUNT = 46
EXPECTED_SCHEMA = "nations-league-public-v1"
EXPECTED_COMPETITION = "UEFA Nations League"
EXPECTED_PROVIDER = "isports_api"
SOURCE_FIXTURE_PATH = "tests/fixtures/nations_league_public_incident_20260928.json"
DETECTOR_SOURCE_COMMIT_SHA = "aad04fbedb495621882411513c30c6ac723de366"
EVALUATION_BANKROLL_EUR = 1000.0
MARKETS = ("home", "draw", "away")
ARTIFACT_SCHEMA = "sportsbrain-nations-league-shadow-signal-candidates-v1"


class ShadowCandidateError(ValueError):
    """The frozen source or a value-detector input is not the agreed contract."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ShadowCandidateError("source timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_frozen_public_bundle(value: object) -> dict[str, Any]:
    """Require the exact incident bundle, then validate it at its own time."""
    if not isinstance(value, Mapping):
        raise ShadowCandidateError("frozen public bundle must be an object")
    bundle = dict(value)
    actual_digest = _public_digest(bundle)
    if (
        bundle.get("public_digest") != EXPECTED_PUBLIC_DIGEST
        or actual_digest != EXPECTED_PUBLIC_DIGEST
    ):
        raise ShadowCandidateError(
            "frozen public bundle digest does not match the incident snapshot"
        )
    if (
        bundle.get("schema") != EXPECTED_SCHEMA
        or bundle.get("competition") != EXPECTED_COMPETITION
        or bundle.get("provider") != EXPECTED_PROVIDER
        or bundle.get("fixture_count") != EXPECTED_FIXTURE_COUNT
        or not isinstance(bundle.get("fixtures"), list)
        or len(bundle["fixtures"]) != EXPECTED_FIXTURE_COUNT
        or bundle.get("captured_at") != EXPECTED_CAPTURED_AT
        or bundle.get("lifecycle") != "SHADOW_ONLY"
        or bundle.get("evidence_status") != "WEAK_EVIDENCE_SHADOW_ONLY"
        or bundle.get("no_bet") is not True
        or bundle.get("publication_enabled") is not False
    ):
        raise ShadowCandidateError(
            "frozen public bundle does not match the agreed 46-fixture contract"
        )

    # Validate against capture-time, never today's clock or current kickoff status.
    reference_time = _timestamp(EXPECTED_CAPTURED_AT) + timedelta(seconds=1)
    try:
        return validate_public_nations_league(bundle, now=reference_time)
    except NationsLeaguePublicError as exc:
        raise ShadowCandidateError(
            f"frozen public bundle failed canonical validation: {exc}"
        ) from exc


def _probability_map(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(MARKETS):
        raise ShadowCandidateError(f"{label} must contain exactly home/draw/away")
    probabilities: dict[str, float] = {}
    for market in MARKETS:
        raw = value[market]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ShadowCandidateError(f"{label}.{market} is not numeric")
        probability = float(raw)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ShadowCandidateError(f"{label}.{market} is outside [0, 1]")
        probabilities[market] = probability
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-6):
        raise ShadowCandidateError(f"{label} does not sum to one")
    return probabilities


def _bound_fixture_model(
    fixture: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, float]]:
    model = fixture.get("model")
    if not isinstance(model, Mapping):
        raise ShadowCandidateError("fixture model is missing")
    components = model.get("components")
    if not isinstance(components, Mapping):
        raise ShadowCandidateError("fixture model components are missing")
    final = _probability_map(model.get("probabilities"), "model probabilities")
    stacker = _probability_map(components.get("canonical_stacker"), "canonical stacker")
    if final != stacker:
        raise ShadowCandidateError(
            "final model probabilities differ from canonical stacker"
        )
    raw_dc = _probability_map(
        components.get("raw_dixon_coles"), "raw Dixon-Coles probabilities"
    )
    return final, raw_dc


def _candidate_id(source_digest: str, provider_event_id: str, market: str) -> str:
    key = f"{source_digest}\0{provider_event_id}\0{market}".encode()
    return "nl-shadow-candidate:" + _sha256_bytes(key)


def _candidate_from_signal(
    signal: BetSignal,
    *,
    provider_event_id: str,
    source_digest: str,
) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(source_digest, provider_event_id, signal.market),
        "provider_event_id": provider_event_id,
        "market": signal.market,
        "model_prob": signal.model_prob,
        "fair_prob": signal.fair_prob,
        "decimal_odds": signal.decimal_odds,
        "ev": signal.ev,
        "ev_percent": signal.ev * 100.0,
        "kelly_fraction": signal.kelly_f,
        "confidence": signal.confidence,
        "theoretical_stake_eur": signal.theoretical_stake_eur,
        "stake_label": "THEORETICAL_EVALUATION_ONLY",
        "evaluation_bankroll_eur": EVALUATION_BANKROLL_EUR,
        "shadow": True,
        "signal_status": "SHADOW_ONLY",
        "no_bet": True,
        "no_bet_flag": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "source_snapshot_digest": source_digest,
    }


def detect_fixture_candidates(
    fixture: Mapping[str, Any],
    *,
    source_digest: str = EXPECTED_PUBLIC_DIGEST,
) -> list[dict[str, Any]]:
    """Run the canonical detector for one frozen public fixture."""
    event_id = fixture.get("provider_event_id")
    home = fixture.get("home")
    away = fixture.get("away")
    if not all(
        isinstance(item, str) and item.strip() for item in (event_id, home, away)
    ):
        raise ShadowCandidateError("fixture identity is incomplete")
    model, raw_dc = _bound_fixture_model(fixture)
    market = fixture.get("market")
    if not isinstance(market, Mapping):
        raise ShadowCandidateError("fixture market is missing")
    odds_map = market.get("odds_decimal")
    if not isinstance(odds_map, Mapping) or set(odds_map) != set(MARKETS):
        raise ShadowCandidateError("fixture market odds are incomplete")
    odds: dict[str, float] = {}
    for side in MARKETS:
        raw = odds_map[side]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ShadowCandidateError(f"market odds {side} is not numeric")
        price = float(raw)
        if not math.isfinite(price) or price <= 1.0:
            raise ShadowCandidateError(f"market odds {side} must be finite and above 1")
        odds[side] = price

    detected = detect_value(
        str(home),
        str(away),
        np.asarray([model["away"], model["draw"], model["home"]], dtype=float),
        (odds["home"], odds["draw"], odds["away"]),
        bankroll=EVALUATION_BANKROLL_EUR,
        min_edge=MIN_EDGE,
        match_id=str(event_id),
        dc_probs={
            "p_home": raw_dc["home"],
            "p_draw": raw_dc["draw"],
            "p_away": raw_dc["away"],
        },
    )
    max_ev = gate_for("football").max_ev
    accepted = [signal for signal in detected if signal.ev <= max_ev]
    return [
        _candidate_from_signal(
            signal,
            provider_event_id=str(event_id),
            source_digest=source_digest,
        )
        for signal in accepted
    ]


def _source_file_hash(relative_path: str) -> str:
    path = Path(__file__).resolve().parents[2] / relative_path
    return _sha256_bytes(path.read_bytes())


def build_shadow_candidate_artifact(
    bundle: object,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build immutable shadow candidates, binding them to the exact frozen bundle."""
    source = validate_frozen_public_bundle(bundle)
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise ShadowCandidateError(
            "candidate generation timestamp must include a timezone"
        )
    generated_at_text = generated.astimezone(timezone.utc).isoformat()

    fixture_records: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    for fixture in source["fixtures"]:
        final, raw_dc = _bound_fixture_model(fixture)
        market = fixture["market"]
        market_probabilities = _probability_map(
            market["probabilities"], "market probabilities"
        )
        odds = {side: float(market["odds_decimal"][side]) for side in MARKETS}
        candidates = detect_fixture_candidates(
            fixture, source_digest=source["public_digest"]
        )
        record = {
            "provider_event_id": fixture["provider_event_id"],
            "kickoff": fixture["kickoff"],
            "home": fixture["home"],
            "away": fixture["away"],
            "final_model_probabilities": final,
            "raw_dixon_coles_probabilities": raw_dc,
            "market_odds_decimal": odds,
            "market_probabilities": market_probabilities,
            "detected_candidates": candidates,
        }
        fixture_records.append(record)
        all_candidates.extend(candidates)

    all_candidates.sort(
        key=lambda item: (-item["ev"], item["provider_event_id"], item["market"])
    )
    market_counts = Counter(candidate["market"] for candidate in all_candidates)
    confidence_counts = Counter(candidate["confidence"] for candidate in all_candidates)
    evs = [candidate["ev"] for candidate in all_candidates]
    fixture_signal_count = sum(
        bool(record["detected_candidates"]) for record in fixture_records
    )
    implementation_files = {
        path: _source_file_hash(path)
        for path in (
            "src/betting/value_detector.py",
            "src/betting/gates.py",
            "src/betting/odds_utils.py",
            "src/betting/kelly.py",
            "src/config.py",
        )
    }
    artifact: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "generated_from": SOURCE_FIXTURE_PATH,
        "provenance": {
            "source_public_digest": source["public_digest"],
            "source_captured_at": source["captured_at"],
            "source_source_sha": source["source_sha"],
            "source_artifact_digest": source["artifact_digest"],
            "source_model_snapshot_digest": source["model_snapshot_digest"],
            "detector_implementation": "src.betting.value_detector.detect_value",
            "detector_source_commit_sha": DETECTOR_SOURCE_COMMIT_SHA,
            "detector_source_files_sha256": implementation_files,
            "min_edge": MIN_EDGE,
            "max_ev": gate_for("football").max_ev,
            "candidate_generation_timestamp": generated_at_text,
        },
        "safety": {
            "shadow": True,
            "no_bet": True,
            "publication_enabled": False,
            "ledger_mutation": False,
            "provider_requests": 0,
            "results_consumed": False,
            "live_scores_consumed": False,
        },
        "evaluation_bankroll_eur": EVALUATION_BANKROLL_EUR,
        "stake_handling": "THEORETICAL_EVALUATION_ONLY",
        "fixture_count": len(fixture_records),
        "fixtures": fixture_records,
        "candidate_table": all_candidates,
        "summary": {
            "total_fixtures": len(fixture_records),
            "fixtures_with_candidates": fixture_signal_count,
            "total_candidates": len(all_candidates),
            "candidate_count_by_market": {
                market: market_counts.get(market, 0) for market in MARKETS
            },
            "candidate_count_by_confidence": {
                confidence: confidence_counts.get(confidence, 0)
                for confidence in ("HIGH", "MEDIUM", "LOW")
            },
            "mean_ev": statistics.fmean(evs) if evs else None,
            "median_ev": statistics.median(evs) if evs else None,
            "maximum_ev": max(evs) if evs else None,
            "minimum_accepted_ev": min(evs) if evs else None,
            "fixtures_without_candidates": len(fixture_records) - fixture_signal_count,
        },
    }
    artifact["artifact_digest"] = _sha256_bytes(_canonical_json(artifact))
    return artifact
