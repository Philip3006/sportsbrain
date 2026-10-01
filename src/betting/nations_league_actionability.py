"""Canonical Nations League bet-time quote to value-signal projection.

The immutable LIVE prediction and market-edge objects remain explicitly
``no_bet`` evidence.  This module creates a separate, short-lived projection
only from a fresh iSports quote and the existing football value detector.  It
does not change a prediction record, lifecycle phase, provider authority, or
ledger state.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from src.analysis.nations_league_live_edge import (
    NationsLeagueLiveEdgeError,
    build_edge_analysis,
    validate_edge_analysis,
    validate_market_snapshot,
)
from src.betting.gates import gate_for
from src.betting.signal_contract import is_actionable_value_signal
from src.betting.value_detector import detect_value
from src.notifications.nations_league_live_public import (
    NationsLeagueLivePublicError,
    _public_canonical,
    validate_live_public_nations_league,
)

SCHEMA = "nations-league-actionable-value-signals-v1"
COMPETITION = "UEFA Nations League"
PRODUCTION_PROVIDER = "the_odds_api"
EVIDENCE_PROVIDER = "isports_api"
PHASE = "refinement"
OUTCOMES = ("home", "draw", "away")
MAX_ODDS_AGE = timedelta(minutes=30)
_DIGEST = "0123456789abcdef"


class NationsLeagueActionabilityError(ValueError):
    """A quote or projection is not safe for the canonical value path."""


def _canonical(value: Any) -> bytes:
    try:
        # The projection is also verified by the Worker.  Reuse the reviewed
        # ECMAScript-compatible canonicalizer so 100.0/100 and other numeric
        # spellings produce the same digest in Python and JavaScript.
        return _public_canonical(value).encode("utf-8")
    except (TypeError, ValueError, NationsLeagueLivePublicError) as exc:
        raise NationsLeagueActionabilityError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueActionabilityError(f"{field} is required")
    return value.strip()


def _utc(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueActionabilityError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise NationsLeagueActionabilityError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha(value: object, field: str) -> str:
    result = _text(value, field)
    if len(result) != 64 or any(char not in _DIGEST for char in result):
        raise NationsLeagueActionabilityError(f"{field} must be a SHA-256 digest")
    return result


def _signal_body(signal: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in signal.items() if key != "signal_id"}


def _projection_body(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "artifact_digest"}


def _validate_snapshot(
    snapshot: Mapping[str, Any], *, now: datetime, fixture_id: str
) -> dict[str, Any]:
    try:
        normalized = validate_market_snapshot(snapshot)
    except (NationsLeagueLiveEdgeError, TypeError, ValueError) as exc:
        raise NationsLeagueActionabilityError(
            "bet-time quote is not canonical"
        ) from exc
    if normalized.get("provider") != EVIDENCE_PROVIDER:
        raise NationsLeagueActionabilityError("bet-time quote provider is not iSports")
    if normalized.get("fixture_id") != fixture_id:
        raise NationsLeagueActionabilityError(
            "bet-time quote fixture identity mismatch"
        )
    captured = _utc(normalized.get("captured_at"), "snapshot.captured_at")
    if captured > now:
        raise NationsLeagueActionabilityError("bet-time quote is future-dated")
    if now - captured > MAX_ODDS_AGE:
        raise NationsLeagueActionabilityError("bet-time quote is stale")
    return normalized


def _signal_id_body(
    *,
    fixture_id: str,
    phase: str,
    prediction_record_id: str,
    model_release_id: str,
    outcome: str,
) -> dict[str, str]:
    return {
        "fixture_id": fixture_id,
        "phase": phase,
        "prediction_record_id": prediction_record_id,
        "model_release_id": model_release_id,
        "outcome": outcome,
    }


_SAFE_REQUEST_PROVENANCE_KEYS = frozenset(
    {"provider", "provider_operation_manifest", "provider_rate_evidence"}
)


def _safe_request_provenance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retain only credential-free transport evidence in the public artifact."""

    source = dict(value or {})
    unexpected = set(source) - _SAFE_REQUEST_PROVENANCE_KEYS
    if unexpected:
        raise NationsLeagueActionabilityError(
            "request provenance contains unsupported fields"
        )
    if source.get("provider") != EVIDENCE_PROVIDER:
        raise NationsLeagueActionabilityError(
            "request provenance provider is not iSports"
        )
    operations = source.get("provider_operation_manifest")
    rate_evidence = source.get("provider_rate_evidence")
    if not isinstance(operations, list) or not isinstance(rate_evidence, list):
        raise NationsLeagueActionabilityError("request provenance is incomplete")
    try:
        # Round-trip through canonical JSON to reject arbitrary objects and
        # retain only JSON data returned by the reviewed transport descriptor.
        return json.loads(_canonical(source).decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NationsLeagueActionabilityError(
            "request provenance is not safe JSON"
        ) from exc


def _build_signal(
    fixture: Mapping[str, Any], snapshot: Mapping[str, Any], outcome: str
) -> dict[str, Any]:
    fixture_id = _text(fixture.get("fixture_id"), "fixture.fixture_id")
    record_id = _sha(
        fixture.get("source_prediction_record_id"), "source_prediction_record_id"
    )
    release = fixture.get("model_release")
    if not isinstance(release, Mapping):
        raise NationsLeagueActionabilityError("fixture model release is missing")
    release_id = _sha(release.get("release_id"), "model_release.release_id")
    identity = fixture.get("canonical_identity")
    if not isinstance(identity, Mapping):
        raise NationsLeagueActionabilityError("fixture canonical identity is missing")
    home = _text(identity.get("home_team"), "canonical_identity.home_team")
    away = _text(identity.get("away_team"), "canonical_identity.away_team")
    probabilities = fixture.get("probabilities")
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(OUTCOMES):
        raise NationsLeagueActionabilityError("fixture probabilities are incomplete")
    model_probabilities = {key: float(probabilities[key]) for key in OUTCOMES}
    if any(
        not math.isfinite(item) or not 0 <= item <= 1
        for item in model_probabilities.values()
    ):
        raise NationsLeagueActionabilityError("fixture probabilities are invalid")
    if abs(sum(model_probabilities.values()) - 1.0) > 1e-6:
        raise NationsLeagueActionabilityError(
            "fixture probabilities are not normalized"
        )
    odds = snapshot["odds_decimal"]
    detector_signals = detect_value(
        home,
        away,
        (
            model_probabilities["away"],
            model_probabilities["draw"],
            model_probabilities["home"],
        ),
        (float(odds["home"]), float(odds["draw"]), float(odds["away"])),
        bankroll=1000.0,
        min_edge=gate_for("football", "1x2").min_edge,
        match_id=fixture_id,
    )
    selected = next((item for item in detector_signals if item.market == outcome), None)
    if selected is None:
        raise NationsLeagueActionabilityError(
            "outcome does not satisfy canonical minimum edge"
        )
    gate = gate_for("football", "1x2")
    if selected.ev > gate.max_ev:
        raise NationsLeagueActionabilityError("outcome exceeds canonical MAX_EV")
    market_prob = float(selected.fair_prob)
    current_odds = float(selected.decimal_odds)
    captured_at = _text(snapshot.get("captured_at"), "snapshot.captured_at")
    signal = {
        "signal_id": "pending",
        "signal_status": "ACTIVE",
        "shadow": False,
        "is_shadow": False,
        "unsupported": False,
        "edge_lost": False,
        "stale": False,
        "no_bet_flag": False,
        "current_odds": current_odds,
        "current_ev_pct": round(float(selected.ev) * 100.0, 6),
        "odds_ts": captured_at,
        "event_status": "PREMATCH",
        "sport": "football",
        "match": f"{home} vs {away}",
        "market": outcome,
        "fixture_key": fixture_id,
        "league": "unl",
        "kickoff": _text(fixture.get("kickoff_utc"), "fixture.kickoff_utc"),
        "model_prob": round(model_probabilities[outcome] * 100.0, 6),
        "fair_prob": round(market_prob * 100.0, 6),
        "odds": current_odds,
        "ev_pct": round(float(selected.ev) * 100.0, 6),
        "confidence": selected.confidence,
        "stake_eur": 0.0,
        "source": "nations_league_bet_time_quote",
        "is_nations_league_value": True,
        "phase": PHASE,
        "prediction_record_id": record_id,
        "model_release_id": release_id,
        "provider_event_id": _text(
            snapshot.get("provider_match_id"), "snapshot.provider_match_id"
        ),
        "bookmaker": _text(snapshot.get("bookmaker"), "snapshot.bookmaker"),
        "quote_snapshot_digest": _sha(
            snapshot.get("snapshot_digest"), "snapshot.snapshot_digest"
        ),
        "quote_captured_at": captured_at,
    }
    identity_body = _signal_id_body(
        fixture_id=fixture_id,
        phase=PHASE,
        prediction_record_id=record_id,
        model_release_id=release_id,
        outcome=outcome,
    )
    signal["signal_id"] = f"nl:value:{_digest(identity_body)}"
    return signal


def _build_quote_evidence(
    fixture: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    actionable_signal_count: int,
) -> dict[str, Any]:
    """Keep fresh quote/value measurements even when no signal is actionable.

    This is display-only evidence.  It deliberately remains separate from
    ``signals`` so a no-bet evaluation cannot become a betting authority.
    """

    fixture_id = _text(fixture.get("fixture_id"), "fixture.fixture_id")
    record_id = _sha(
        fixture.get("source_prediction_record_id"), "source_prediction_record_id"
    )
    release = fixture.get("model_release")
    if not isinstance(release, Mapping):
        raise NationsLeagueActionabilityError("fixture model release is missing")
    release_id = _sha(release.get("release_id"), "model_release.release_id")
    model_probabilities = fixture.get("probabilities")
    if not isinstance(model_probabilities, Mapping):
        raise NationsLeagueActionabilityError("fixture probabilities are incomplete")
    try:
        edge = build_edge_analysis(
            model_probabilities,
            fixture_id=fixture_id,
            phase=PHASE,
            model_release_id=release_id,
            prediction_record_id=record_id,
            prediction_timestamp=snapshot["captured_at"],
            market_snapshots=[snapshot],
        )
        validate_edge_analysis(
            edge, fixture_id=fixture_id, prediction_record_id=record_id
        )
    except (NationsLeagueLiveEdgeError, TypeError, ValueError) as exc:
        raise NationsLeagueActionabilityError(
            "fresh quote value evidence is not canonical"
        ) from exc
    return {
        "fixture_id": fixture_id,
        "phase": PHASE,
        "match": f"{_text(fixture['canonical_identity']['home_team'], 'home_team')}"
        f" vs {_text(fixture['canonical_identity']['away_team'], 'away_team')}",
        "kickoff": _text(fixture.get("kickoff_utc"), "fixture.kickoff_utc"),
        "prediction_record_id": record_id,
        "model_release_id": release_id,
        "provider_event_id": _text(
            snapshot.get("provider_match_id"), "snapshot.provider_match_id"
        ),
        "bookmaker": _text(snapshot.get("bookmaker"), "snapshot.bookmaker"),
        "quote_snapshot_digest": _sha(
            snapshot.get("snapshot_digest"), "snapshot.snapshot_digest"
        ),
        "quote_captured_at": _text(snapshot.get("captured_at"), "snapshot.captured_at"),
        "actionable_signal_count": actionable_signal_count,
        "no_bet": True,
        "no_bet_reason": (
            "ACTIONABLE_SIGNAL_AVAILABLE"
            if actionable_signal_count
            else "NO_CANONICAL_ACTIONABLE_OUTCOME"
        ),
        "edge_analysis": edge,
    }


def build_nations_league_actionable_projection(
    nations_league: Mapping[str, Any],
    snapshots: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    request_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build separate actionable signals from trusted NL state and fresh quotes."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        public = validate_live_public_nations_league(nations_league)
    except (NationsLeagueLivePublicError, TypeError, ValueError) as exc:
        raise NationsLeagueActionabilityError(
            "trusted LIVE Nations League state is invalid"
        ) from exc
    fixture_rows = [
        row
        for row in public["fixtures"]
        if row.get("phase") == PHASE
        and _utc(row.get("kickoff_utc"), "fixture.kickoff_utc") > current
    ]
    if not fixture_rows:
        raise NationsLeagueActionabilityError(
            "no current refinement fixtures are available"
        )
    snapshots_by_fixture: dict[str, Mapping[str, Any]] = {}
    for raw in snapshots:
        fixture_id = _text(raw.get("fixture_id"), "snapshot.fixture_id")
        if fixture_id in snapshots_by_fixture:
            raise NationsLeagueActionabilityError("duplicate bet-time quote fixture")
        snapshots_by_fixture[fixture_id] = raw
    signals: list[dict[str, Any]] = []
    quote_evidence: list[dict[str, Any]] = []
    quote_digests: list[str] = []
    for fixture in sorted(fixture_rows, key=lambda item: item["fixture_id"]):
        fixture_id = fixture["fixture_id"]
        snapshot_raw = snapshots_by_fixture.get(fixture_id)
        if snapshot_raw is None:
            raise NationsLeagueActionabilityError(
                "bet-time quote coverage is incomplete"
            )
        snapshot = _validate_snapshot(snapshot_raw, now=current, fixture_id=fixture_id)
        quote_digests.append(snapshot["snapshot_digest"])
        fixture_signal_count = 0
        for outcome in OUTCOMES:
            try:
                signal = _build_signal(fixture, snapshot, outcome)
            except NationsLeagueActionabilityError as exc:
                if "minimum edge" in str(exc) or "MAX_EV" in str(exc):
                    continue
                raise
            valid, reason = is_actionable_value_signal(
                signal, bankroll=1000.0, active_bet_count=0
            )
            if not valid:
                raise NationsLeagueActionabilityError(
                    f"derived signal is not canonical-actionable: {reason}"
                )
            signals.append(signal)
            fixture_signal_count += 1
        quote_evidence.append(
            _build_quote_evidence(
                fixture,
                snapshot,
                actionable_signal_count=fixture_signal_count,
            )
        )
    provenance = _safe_request_provenance(request_provenance)
    provenance["request_count"] = 2
    provenance["retry_count"] = 0
    request_count = provenance.get("request_count", 2)
    retry_count = provenance.get("retry_count", 0)
    if request_count != 2 or retry_count != 0:
        raise NationsLeagueActionabilityError(
            "bet-time quote must use exactly two requests and zero retries"
        )
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "competition": COMPETITION,
        "provider_authority": PRODUCTION_PROVIDER,
        "evidence_provider": EVIDENCE_PROVIDER,
        "candidate_provider": EVIDENCE_PROVIDER,
        "evidence_status": "DERIVED_ACTIONABILITY_ONLY",
        "source_evidence_no_bet": True,
        "actionability_enabled": True,
        "publication_enabled": False,
        "production_activation": False,
        "ledger_mutation": False,
        "phase": PHASE,
        "quote_captured_at": max(
            (signal["quote_captured_at"] for signal in signals), default=_stamp(current)
        ),
        "quote_count": len(quote_digests),
        "request_count": request_count,
        "retry_count": retry_count,
        "quote_snapshot_digests": sorted(set(quote_digests)),
        "request_provenance": provenance,
        "quote_evidence": quote_evidence,
        "signals": sorted(signals, key=lambda item: item["signal_id"]),
    }
    body["artifact_digest"] = _digest(body)
    return body


def validate_nations_league_actionable_projection(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the public projection and its signal/authority boundaries."""

    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise NationsLeagueActionabilityError(
            "invalid Nations League actionable schema"
        )
    candidate = dict(value)
    digest = candidate.pop("artifact_digest", None)
    if not isinstance(digest, str) or digest != _digest(candidate):
        raise NationsLeagueActionabilityError("actionable projection digest mismatch")
    if (
        candidate.get("competition") != COMPETITION
        or candidate.get("provider_authority") != PRODUCTION_PROVIDER
        or candidate.get("evidence_provider") != EVIDENCE_PROVIDER
        or candidate.get("candidate_provider") != EVIDENCE_PROVIDER
        or candidate.get("evidence_status") != "DERIVED_ACTIONABILITY_ONLY"
        or candidate.get("source_evidence_no_bet") is not True
        or candidate.get("actionability_enabled") is not True
        or candidate.get("publication_enabled") is not False
        or candidate.get("production_activation") is not False
        or candidate.get("ledger_mutation") is not False
        or candidate.get("phase") != PHASE
        or candidate.get("request_count") != 2
        or candidate.get("retry_count") != 0
    ):
        raise NationsLeagueActionabilityError(
            "actionable projection safety binding is invalid"
        )
    signals = candidate.get("signals")
    if not isinstance(signals, list):
        raise NationsLeagueActionabilityError("actionable signals are missing")
    seen: set[str] = set()
    for signal in signals:
        if not isinstance(signal, Mapping):
            raise NationsLeagueActionabilityError("actionable signal is malformed")
        sid = _text(signal.get("signal_id"), "signal_id")
        if sid in seen or not sid.startswith("nl:value:"):
            raise NationsLeagueActionabilityError(
                "actionable signal identity is invalid"
            )
        seen.add(sid)
        if (
            signal.get("signal_status") != "ACTIVE"
            or signal.get("shadow") is not False
            or signal.get("is_shadow") is not False
            or signal.get("unsupported") is not False
            or signal.get("edge_lost") is not False
            or signal.get("stale") is not False
            or signal.get("no_bet_flag") is not False
            or signal.get("is_nations_league_value") is not True
            or signal.get("source") != "nations_league_bet_time_quote"
            or signal.get("phase") != PHASE
            or signal.get("sport") != "football"
        ):
            raise NationsLeagueActionabilityError(
                "actionable signal safety binding is invalid"
            )
        _utc(signal.get("odds_ts"), "signal.odds_ts")
        _utc(signal.get("quote_captured_at"), "signal.quote_captured_at")
        _text(signal.get("fixture_key"), "signal.fixture_key")
        _sha(signal.get("quote_snapshot_digest"), "signal.quote_snapshot_digest")
        _sha(signal.get("prediction_record_id"), "signal.prediction_record_id")
        _sha(signal.get("model_release_id"), "signal.model_release_id")
    signal_counts: dict[str, int] = {}
    for signal in signals:
        fixture_id = _text(signal.get("fixture_key"), "signal.fixture_key")
        signal_counts[fixture_id] = signal_counts.get(fixture_id, 0) + 1
    quote_evidence = candidate.get("quote_evidence")
    if not isinstance(quote_evidence, list) or not quote_evidence:
        raise NationsLeagueActionabilityError("fresh quote evidence is missing")
    evidence_ids: set[str] = set()
    for evidence in quote_evidence:
        if not isinstance(evidence, Mapping):
            raise NationsLeagueActionabilityError("fresh quote evidence is malformed")
        fixture_id = _text(evidence.get("fixture_id"), "quote_evidence.fixture_id")
        if fixture_id in evidence_ids:
            raise NationsLeagueActionabilityError("duplicate fresh quote evidence")
        evidence_ids.add(fixture_id)
        if evidence.get("phase") != PHASE or evidence.get("no_bet") is not True:
            raise NationsLeagueActionabilityError(
                "fresh quote evidence safety binding is invalid"
            )
        if evidence.get("no_bet_reason") not in {
            "ACTIONABLE_SIGNAL_AVAILABLE",
            "NO_CANONICAL_ACTIONABLE_OUTCOME",
        }:
            raise NationsLeagueActionabilityError(
                "fresh quote no-bet reason is invalid"
            )
        count = evidence.get("actionable_signal_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise NationsLeagueActionabilityError(
                "fresh quote actionable count is invalid"
            )
        if count != signal_counts.get(fixture_id, 0):
            raise NationsLeagueActionabilityError(
                "fresh quote actionable count does not match canonical signals"
            )
        if (count == 0) != (
            evidence["no_bet_reason"] == "NO_CANONICAL_ACTIONABLE_OUTCOME"
        ):
            raise NationsLeagueActionabilityError(
                "fresh quote no-bet reason does not match count"
            )
        record_id = _sha(
            evidence.get("prediction_record_id"), "quote_evidence.prediction_record_id"
        )
        _sha(evidence.get("model_release_id"), "quote_evidence.model_release_id")
        _text(evidence.get("provider_event_id"), "quote_evidence.provider_event_id")
        _text(evidence.get("bookmaker"), "quote_evidence.bookmaker")
        _sha(
            evidence.get("quote_snapshot_digest"),
            "quote_evidence.quote_snapshot_digest",
        )
        _utc(evidence.get("quote_captured_at"), "quote_evidence.quote_captured_at")
        try:
            edge = validate_edge_analysis(
                evidence.get("edge_analysis"),
                fixture_id=fixture_id,
                prediction_record_id=record_id,
            )
        except (NationsLeagueLiveEdgeError, TypeError, ValueError) as exc:
            raise NationsLeagueActionabilityError(
                "fresh quote edge evidence is invalid"
            ) from exc
        snapshot = edge.get("market_snapshot")
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("snapshot_digest") != evidence["quote_snapshot_digest"]
        ):
            raise NationsLeagueActionabilityError(
                "fresh quote snapshot binding is invalid"
            )
        if snapshot.get("provider") != EVIDENCE_PROVIDER:
            raise NationsLeagueActionabilityError(
                "fresh quote evidence provider is invalid"
            )
    return dict(value)
