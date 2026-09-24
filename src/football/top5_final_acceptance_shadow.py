"""Controlled-shadow checks for the final Top-5 acceptance gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from math import isfinite

from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
)
from src.football.top5_final_acceptance import (
    CANDIDATE_PROVIDER,
    MAX_SHADOW_REQUESTS,
    TOP5_LEAGUES,
    Top5FinalAcceptanceError,
    _age,
    _mapping,
    _require_false,
    _sha,
    _text,
    _timestamp,
    canonical_digest,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    NETWORK_RUN_SCHEMA_VERSION,
    TheRundownNetworkResponseV1,
)


def _capture_parts(
    raw: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    if isinstance(raw.get("capture"), Mapping):
        return (
            _mapping(raw.get("target"), "capture target"),
            _mapping(raw.get("request"), "capture request"),
            _mapping(raw.get("capture"), "capture"),
        )
    observation = _mapping(raw.get("observation_input"), "capture observation")
    participant_scope = _mapping(raw.get("participant_scope"), "participant scope")
    target = {
        "provider": raw.get("provider"),
        "league": raw.get("league"),
        "fixture_key": raw.get("fixture_key"),
        "provider_event_id": raw.get("provider_event_id"),
        "home_team": raw.get("home_team") or observation.get("home_team"),
        "away_team": raw.get("away_team") or observation.get("away_team"),
        "kickoff": observation.get("kickoff"),
    }
    request = {
        "controlled_shadow_run_id": None,
        "qualification_session_id": None,
        "authorization_id": None,
        "ceo_authorization_identity": raw.get("ceo_authorization_identity"),
        "configuration_digest": raw.get("configuration_digest"),
        "request_identity": raw.get("request_identity"),
        "home_participant_id": participant_scope.get("home_participant_id"),
        "away_participant_id": participant_scope.get("away_participant_id"),
    }
    return target, request, raw


def _validate_controlled_shadow(
    raw: Mapping[str, object],
    *,
    now: datetime,
    discovery: Mapping[str, Mapping[str, object]],
) -> tuple[str, str, str, str, tuple[str, ...], str]:
    run = _mapping(raw.get("shadow_run", raw), "controlled shadow run")
    if run.get("schema_version") != NETWORK_RUN_SCHEMA_VERSION:
        raise Top5FinalAcceptanceError("controlled shadow run schema is unsupported")
    if run.get("status") != "COMPLETED_NETWORK":
        raise Top5FinalAcceptanceError(
            "controlled shadow run is not complete network evidence"
        )
    run_id = _text(run.get("controlled_shadow_run_id"), "controlled shadow run ID")
    session_id = _text(run.get("qualification_session_id"), "qualification session ID")
    authorization_id = _text(
        run.get("authorization_id"), "controlled shadow authorization ID"
    )
    captures = run.get("captures")
    if (
        not isinstance(captures, Sequence)
        or isinstance(captures, (str, bytes))
        or len(captures) != MAX_SHADOW_REQUESTS
    ):
        raise Top5FinalAcceptanceError(
            "controlled shadow must contain exactly five captures"
        )
    if run.get("request_count") != MAX_SHADOW_REQUESTS or run.get("failures") not in (
        [],
        (),
    ):
        raise Top5FinalAcceptanceError(
            "controlled shadow request/failure accounting is invalid"
        )
    _require_false(
        run,
        (
            "receipt_eligible",
            "authority_changed",
            "publication",
            "production_activation",
            "monetary_spend_authorized",
        ),
        "controlled shadow",
    )
    leagues: set[str] = set()
    total_datapoints = 0
    total_quota = 0.0
    adapter_sha: str | None = None
    capture_digests: list[str] = []
    for item in captures:
        target, request, capture = _capture_parts(
            _mapping(item, "controlled shadow capture")
        )
        observation = _mapping(capture.get("observation_input"), "capture observation")
        response = TheRundownNetworkResponseV1.from_payload(capture.get("response"))
        if response.outcome not in ("success", "SUCCESS"):
            raise Top5FinalAcceptanceError("controlled shadow response did not succeed")
        if response.http_status != 200 or response.provider != CANDIDATE_PROVIDER:
            raise Top5FinalAcceptanceError(
                "controlled shadow response provider/status is invalid"
            )
        if (
            response.evidence_kind != ObservationEvidenceKind.REAL_OBSERVED
            and str(response.evidence_kind)
            != ObservationEvidenceKind.REAL_OBSERVED.value
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow evidence is not REAL_OBSERVED"
            )
        if (
            capture.get("evidence_kind") != ObservationEvidenceKind.REAL_OBSERVED.value
            or capture.get("network_execution") is not True
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow capture is not network REAL_OBSERVED"
            )
        if (
            capture.get("candidate_only") is not True
            or capture.get("receipt_eligible") is not False
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow capture carries unsafe authority"
            )
        safety = _mapping(capture.get("safety"), "controlled shadow capture safety")
        if safety.get("no_bet") is not True:
            raise Top5FinalAcceptanceError(
                "controlled shadow capture must remain no-bet"
            )
        _require_false(
            safety,
            (
                "publication",
                "production_activation",
                "monetary_spend_authorized",
                "authority_changed",
                "scheduler_registered",
                "ledger_mutated",
            ),
            "controlled shadow capture safety",
        )
        league = _text(target.get("league"), "capture league")
        if league not in TOP5_LEAGUES or league in leagues:
            raise Top5FinalAcceptanceError(
                "controlled shadow league coverage is invalid"
            )
        fixture = _text(target.get("fixture_key"), "capture fixture")
        kickoff = _timestamp(target.get("kickoff"), "capture kickoff")
        if fixture != make_fixture_key(
            league,
            _text(target.get("home_team"), "capture home team"),
            _text(target.get("away_team"), "capture away team"),
            kickoff,
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow fixture binding is invalid"
            )
        if response.fixture_key != fixture or response.provider_event_id != _text(
            target.get("provider_event_id"), "capture event"
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow fixture/event binding mismatch"
            )
        if response.home_team != target.get(
            "home_team"
        ) or response.away_team != target.get("away_team"):
            raise Top5FinalAcceptanceError(
                "controlled shadow participant names mismatch"
            )
        scope = _mapping(capture.get("participant_scope"), "capture participant scope")
        if response.home_participant_id != scope.get(
            "home_participant_id"
        ) or response.away_participant_id != scope.get("away_participant_id"):
            raise Top5FinalAcceptanceError("controlled shadow participant IDs mismatch")
        request_identity = _text(
            capture.get("request_identity"), "capture request identity"
        )
        if response.provider_request_id != request_identity or request.get(
            "request_identity"
        ) not in (None, request_identity):
            raise Top5FinalAcceptanceError("controlled shadow request binding mismatch")
        source = _timestamp(response.source_timestamp, "response source timestamp")
        captured = _timestamp(response.captured_at, "response capture timestamp")
        finished = _timestamp(response.request_finished_at, "response finish timestamp")
        started = _timestamp(response.request_started_at, "response start timestamp")
        if (
            started > finished
            or finished > captured
            or source > captured
            or any(
                value <= 1.0
                for value in (
                    response.home_odds,
                    response.draw_odds,
                    response.away_odds,
                )
                if isinstance(value, (int, float))
            )
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow timing or 1X2 values are invalid"
            )
        if (
            response.home_odds is None
            or response.draw_odds is None
            or response.away_odds is None
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow complete regulation 1X2 is missing"
            )
        _age(now, captured, f"controlled shadow {league}")
        if (
            response.provider_delay_seconds is None
            or not isfinite(float(response.provider_delay_seconds))
            or response.provider_delay_seconds < 0
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow provider delay is invalid"
            )
        for name, value in (
            ("raw_response_digest", response.raw_response_digest),
            ("provider_record_digest", response.provider_record_digest),
            ("normalized_record_digest", response.normalized_record_digest),
            ("cascade_evidence_digest", response.cascade_evidence_digest),
            ("adapter_source_sha", response.adapter_source_sha),
        ):
            _sha(value, f"controlled shadow {name}")
        _text(response.bookmaker_identity, "controlled shadow bookmaker")
        if (
            response.retry_count != 0
            or response.network_execution is not True
            or response.no_bet is not True
            or response.publication is not False
            or response.production_activation is not False
            or response.monetary_spend_authorized is not False
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow safety/retry contract is invalid"
            )
        if (
            response.quota_before is None
            or response.quota_after is None
            or response.quota_after < 0
            or response.datapoint_count <= 0
            or response.quota_cost_units != response.datapoint_count
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow quota accounting is invalid"
            )
        attestation_raw = _mapping(
            capture.get("capture_attestation_input"), "capture attestation"
        )
        try:
            attestation = ControlledShadowCaptureAttestation.from_payload(
                attestation_raw
            )
            attestation.validate()
        except Exception as exc:
            raise Top5FinalAcceptanceError(
                f"controlled shadow attestation rejected: {exc}"
            ) from exc
        attestation_digest = _sha(
            capture.get("capture_attestation_digest"), "capture attestation digest"
        )
        if attestation_digest != canonical_digest(attestation_raw):
            raise Top5FinalAcceptanceError("capture attestation digest mismatch")
        if (
            attestation.controlled_shadow_run_id != run_id
            or attestation.qualification_session_id != session_id
            or attestation.ceo_authorization_id != authorization_id
            or attestation.provider_identity != CANDIDATE_PROVIDER
            or attestation.fixture_key != fixture
            or attestation.provider_event_id != response.provider_event_id
            or attestation.provider_request_id != response.provider_request_id
        ):
            raise Top5FinalAcceptanceError(
                "capture attestation identity binding mismatch"
            )
        if (
            attestation.adapter_source_sha.lower()
            != response.adapter_source_sha.lower()
            or attestation.raw_response_digest != response.raw_response_digest
            or attestation.normalized_record_digest != response.normalized_record_digest
            or attestation.cascade_evidence_digest != response.cascade_evidence_digest
        ):
            raise Top5FinalAcceptanceError(
                "capture attestation digest binding mismatch"
            )
        obs_digest = _sha(capture.get("observation_digest"), "observation digest")
        observation_without_digest = {
            key: value
            for key, value in observation.items()
            if key != "observation_digest"
        }
        if obs_digest != canonical_digest(observation_without_digest):
            raise Top5FinalAcceptanceError("observation digest mismatch")
        if (
            observation.get("evidence_kind")
            != ObservationEvidenceKind.REAL_OBSERVED.value
            or observation.get("synthetic_reconstruction") is not False
            or observation.get("network_request_count") != 1
            or observation.get("no_bet") is not True
            or observation.get("publication_enabled") is not False
            or observation.get("production_activation") is not False
            or observation.get("ledger_mutated") is not False
            or observation.get("sealed_data_accessed") is not False
            or observation.get("research_mutated") is not False
        ):
            raise Top5FinalAcceptanceError("observation input is synthetic or unsafe")
        if (
            observation.get("fixture_key") != fixture
            or observation.get("provider_identity") != CANDIDATE_PROVIDER
            or observation.get("provider_event_id") != response.provider_event_id
            or observation.get("provider_request_id") != response.provider_request_id
        ):
            raise Top5FinalAcceptanceError("observation identity binding mismatch")
        discovery_item = discovery.get(league)
        if (
            discovery_item is None
            or discovery_item.get("provider_event_id") != response.provider_event_id
            or discovery_item.get("fixture_key") != fixture
            or discovery_item.get("request_identity") != request_identity
        ):
            raise Top5FinalAcceptanceError(
                "controlled shadow does not match frozen discovery"
            )
        leagues.add(league)
        total_datapoints += response.datapoint_count
        total_quota += float(response.quota_cost_units)
        adapter_sha = adapter_sha or response.adapter_source_sha
        if adapter_sha.lower() != response.adapter_source_sha.lower():
            raise Top5FinalAcceptanceError("adapter source SHA drift across captures")
        capture_digests.append(canonical_digest(capture))
    if (
        leagues != TOP5_LEAGUES
        or run.get("datapoint_count") != total_datapoints
        or float(run.get("quota_cost_units", -1)) != total_quota
    ):
        raise Top5FinalAcceptanceError(
            "controlled shadow aggregate accounting is inconsistent"
        )
    return (
        run_id,
        session_id,
        authorization_id,
        adapter_sha or "",
        tuple(capture_digests),
        canonical_digest(run),
    )
