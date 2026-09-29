from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.nations_league_historical_odds_backfill import (
    EXPECTED_TIMELINE_DIGEST,
    BackfillError,
    BackfillExecutor,
    ExecutionManifest,
    ExecutionMode,
    HistoricalRequest,
    JoinedObservationStore,
    LoadedPlan,
    PreflightContext,
    PreflightError,
    ProviderResponse,
    RawResponseStore,
    ResumeSafetyError,
    join_snapshot,
    load_plan,
)

PLAN_PATH = Path(
    "results/audits/nations_league_historical_odds_request_plan_20260929.json"
)


def _plan():
    return load_plan(PLAN_PATH)


def _context(plan, mode=ExecutionMode.DRY_RUN, *, credits=2_000, **overrides):
    values = {
        "historical_entitlement": True,
        "available_credits": credits,
        "quota_reset_at": "2026-10-01T00:00:00Z",
        "requested_mode": mode,
        "expected_plan_digest": plan.file_digest,
        "expected_timeline_digest": EXPECTED_TIMELINE_DIGEST,
        "safety_buffer_credits": 100,
    }
    values.update(overrides)
    return PreflightContext(**values)


def _fixture_target(plan, index=0):
    return plan.fixture_targets[index]


def _mini_plan():
    source_plan = _plan()
    target = dict(_fixture_target(source_plan))
    phases = {
        "PREDICTION_ONLY": {
            "phases": [
                {
                    "phase": "INITIAL",
                    "requested_historical_snapshot_timestamps": [
                        target["initial_target_utc"]
                    ],
                    "estimated_credits": 10,
                },
                {
                    "phase": "REFINEMENT",
                    "requested_historical_snapshot_timestamps": [
                        target["refinement_target_utc"]
                    ],
                    "estimated_credits": 10,
                },
            ],
            "estimated_credits": 20,
        },
        "FULL_RESEARCH": {
            "phases": [
                {
                    "phase": "INITIAL",
                    "requested_historical_snapshot_timestamps": [
                        target["initial_target_utc"]
                    ],
                    "estimated_credits": 10,
                },
                {
                    "phase": "REFINEMENT",
                    "requested_historical_snapshot_timestamps": [
                        target["refinement_target_utc"]
                    ],
                    "estimated_credits": 10,
                },
                {
                    "phase": "CLOSING_BENCHMARK",
                    "requested_historical_snapshot_timestamps": [
                        target["closing_boundary_utc"]
                    ],
                    "estimated_credits": 10,
                },
            ],
            "estimated_credits": 30,
        },
    }
    raw = {
        "provider": {
            "name": "the_odds_api",
            "sport_key": "soccer_uefa_nations_league",
            "region": "eu",
            "market": "h2h",
            "estimated_credits_per_unique_request": 10,
        },
        "source": {"timeline_dataset_digest": EXPECTED_TIMELINE_DIGEST},
        "plans": phases,
    }
    return LoadedPlan(Path("mini-plan.json"), "mini-plan-digest", raw, (target,))


def _response_for(target, request, *, snapshot=None, payload_overrides=None):
    kickoff = datetime.fromisoformat(target["kickoff_utc"].replace("Z", "+00:00"))
    if snapshot is None:
        snapshot = (
            kickoff - timedelta(hours=24)
            if request.phase == "INITIAL"
            else kickoff - timedelta(minutes=90)
            if request.phase == "REFINEMENT"
            else kickoff - timedelta(minutes=5)
        )
    event = {
        "id": "provider-event-1",
        "commence_time": target["kickoff_utc"],
        "home_team": target["home_team"],
        "away_team": target["away_team"],
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-09-29T00:00:00Z",
                        "outcomes": [
                            {"name": target["home_team"], "price": 2.1},
                            {"name": "Draw", "price": 3.2},
                            {"name": target["away_team"], "price": 3.6},
                        ],
                    }
                ],
            }
        ],
    }
    payload = {
        "timestamp": snapshot.isoformat().replace("+00:00", "Z"),
        "next_timestamp": target["kickoff_utc"]
        if request.phase == "CLOSING_BENCHMARK"
        else None,
        "data": [event],
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return ProviderResponse(
        status_code=200,
        payload=payload,
        received_at="2026-09-29T00:00:00Z",
        headers={"content-type": "application/json", "x-api-key": "must-not-persist"},
    )


def test_dry_run_reproduces_both_plan_costs_and_has_zero_side_effects():
    plan = _plan()
    first = BackfillExecutor(plan).dry_run(_context(plan))
    second = BackfillExecutor(plan).dry_run(_context(plan))

    assert first == second
    assert first["network_requests"] == 0
    assert first["credential_accesses"] == 0
    assert first["plans"]["PREDICTION_ONLY"]["unique_http_requests"] == 118
    assert first["plans"]["PREDICTION_ONLY"]["estimated_credits"] == 1_180
    assert first["plans"]["FULL_RESEARCH"]["unique_http_requests"] == 177
    assert first["plans"]["FULL_RESEARCH"]["estimated_credits"] == 1_770


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"historical_entitlement": False}, "entitlement"),
        ({"available_credits": 1_000}, "insufficient credits"),
        ({"expected_plan_digest": "wrong"}, "plan digest"),
        ({"expected_timeline_digest": "wrong"}, "timeline digest"),
        ({"quota_reset_at": "not-a-time"}, "quota reset"),
    ],
)
def test_preflight_guards_fail_before_any_credential_or_transport_access(
    overrides, match
):
    plan = _plan()
    calls = {"credential": 0, "transport": 0}

    def credential():
        calls["credential"] += 1
        raise AssertionError("credential must not be read")

    class Transport:
        def fetch(self, request, credential):
            calls["transport"] += 1
            raise AssertionError("transport must not be called")

    with pytest.raises(PreflightError, match=match):
        BackfillExecutor(plan).execute(
            _context(plan, ExecutionMode.PREDICTION_ONLY, **overrides),
            transport=Transport(),
            credential_provider=credential,
            manifest=ExecutionManifest(Path("/tmp/unused-manifest.json")),
            raw_store=RawResponseStore(Path("/tmp/unused-raw")),
            joined_store=JoinedObservationStore(Path("/tmp/unused-joined")),
        )
    assert calls == {"credential": 0, "transport": 0}


def test_canonical_request_identity_is_phase_and_timestamp_bound():
    initial = HistoricalRequest("INITIAL", "2022-06-10T18:45:00Z")
    refinement = HistoricalRequest("REFINEMENT", "2022-06-10T18:45:00Z")
    assert initial.request_identifier != refinement.request_identifier
    assert (
        initial.request_identifier
        == HistoricalRequest("INITIAL", "2022-06-10T18:45:00Z").request_identifier
    )


def test_join_accepts_complete_market_and_uses_canonical_shin_probabilities():
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])
    joined = join_snapshot(
        request, _response_for(target, request), plan.fixture_targets
    )
    item = next(
        item
        for item in joined["fixtures"]
        if item["fixture_id"] == target["fixture_id"]
    )
    assert item["market_state"] == "matched"
    assert item["prediction_input"] is True
    assert item["bookmaker_market_observations"][0]["market_key"] == "h2h"
    assert sum(
        item["bookmaker_market_observations"][0]["shin_fair_probabilities"].values()
    ) == pytest.approx(1.0)


def test_join_fails_closed_on_identity_ambiguity_and_missing_market():
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])
    response = _response_for(target, request)
    event = response.payload["data"][0]
    response.payload["data"].append(dict(event))
    joined = join_snapshot(request, response, plan.fixture_targets)
    item = next(
        item
        for item in joined["fixtures"]
        if item["fixture_id"] == target["fixture_id"]
    )
    assert item["market_state"] == "ambiguous"

    response = _response_for(target, request)
    response.payload["data"][0]["bookmakers"] = []
    joined = join_snapshot(request, response, plan.fixture_targets)
    item = next(
        item
        for item in joined["fixtures"]
        if item["fixture_id"] == target["fixture_id"]
    )
    assert item["market_state"] == "missing"
    assert item["missing_or_ambiguous_reason"] == "missing_complete_h2h_market"


def test_malformed_provider_payload_fails_closed():
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])
    with pytest.raises(BackfillError, match="valid event list"):
        join_snapshot(
            request,
            ProviderResponse(
                status_code=200,
                payload={"unexpected": "shape"},
                received_at="2026-09-29T00:00:00Z",
                headers={},
                provider_snapshot_timestamp="2022-06-10T18:45:00Z",
            ),
            plan.fixture_targets,
        )


def test_temporal_leakage_is_rejected_and_closing_is_research_only():
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])
    kickoff = datetime.fromisoformat(target["kickoff_utc"].replace("Z", "+00:00"))
    too_late = _response_for(target, request, snapshot=kickoff - timedelta(minutes=30))
    joined = join_snapshot(request, too_late, plan.fixture_targets)
    item = next(
        item
        for item in joined["fixtures"]
        if item["fixture_id"] == target["fixture_id"]
    )
    assert item["market_state"] == "ambiguous"
    assert item["missing_or_ambiguous_reason"] == "outside_initial_window"

    closing = HistoricalRequest("CLOSING_BENCHMARK", target["closing_boundary_utc"])
    joined = join_snapshot(
        closing, _response_for(target, closing), plan.fixture_targets
    )
    assert joined["closing_prediction_input_forbidden"] is True
    item = next(
        item
        for item in joined["fixtures"]
        if item["fixture_id"] == target["fixture_id"]
    )
    assert item["prediction_input"] is False
    assert item["research_classification"] == "RESEARCH_BENCHMARK_ONLY"

    ambiguous = _response_for(
        target, closing, payload_overrides={"next_timestamp": None}
    )
    joined = join_snapshot(closing, ambiguous, plan.fixture_targets)
    item = next(
        item
        for item in joined["fixtures"]
        if item["fixture_id"] == target["fixture_id"]
    )
    assert item["market_state"] == "ambiguous"


def test_manifest_suppresses_completed_requests_after_restart(tmp_path):
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])

    manifest = ExecutionManifest(tmp_path / "execution.jsonl")
    manifest.append(
        {
            "request_identifier": request.request_identifier,
            "execution_status": "completed",
        }
    )
    assert manifest.state(request.request_identifier)["execution_status"] == "completed"


def test_uncertain_transport_attempt_blocks_paid_duplicate_retry(tmp_path):
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])
    manifest = ExecutionManifest(tmp_path / "execution.jsonl")
    manifest.append(
        {
            "request_identifier": request.request_identifier,
            "execution_status": "uncertain",
        }
    )
    with pytest.raises(ResumeSafetyError):
        if manifest.state(request.request_identifier):
            raise ResumeSafetyError("unresolved prior attempt")


def test_execute_resumes_completed_requests_and_blocks_uncertain_retry(tmp_path):
    plan = _mini_plan()
    context = _context(
        plan,
        ExecutionMode.PREDICTION_ONLY,
        credits=100,
        expected_plan_digest="mini-plan-digest",
        safety_buffer_credits=1,
    )
    manifest_path = tmp_path / "execution.jsonl"
    calls = []

    class Transport:
        def fetch(self, request, credential):
            calls.append(request.request_identifier)
            return _response_for(plan.fixture_targets[0], request)

    executor = BackfillExecutor(plan)
    first = executor.execute(
        context,
        transport=Transport(),
        credential_provider=lambda: "opaque-runtime-secret",
        manifest=ExecutionManifest(manifest_path),
        raw_store=RawResponseStore(tmp_path / "raw"),
        joined_store=JoinedObservationStore(tmp_path / "joined"),
    )
    assert first["network_requests"] == 2
    assert len(calls) == 2

    second = executor.execute(
        context,
        transport=Transport(),
        credential_provider=lambda: "must-not-be-read-for-completed-run",
        manifest=ExecutionManifest(manifest_path),
        raw_store=RawResponseStore(tmp_path / "raw"),
        joined_store=JoinedObservationStore(tmp_path / "joined"),
    )
    assert second["network_requests"] == 0
    assert second["skipped_completed_requests"] == 2
    assert len(calls) == 2

    partial_manifest = tmp_path / "partial.jsonl"
    partial_calls = []

    class FailingTransport:
        def fetch(self, request, credential):
            partial_calls.append(request.request_identifier)
            if len(partial_calls) == 2:
                raise TimeoutError("simulated transport loss")
            return _response_for(plan.fixture_targets[0], request)

    with pytest.raises(BackfillError, match="resume is blocked"):
        executor.execute(
            context,
            transport=FailingTransport(),
            credential_provider=lambda: "opaque-runtime-secret",
            manifest=ExecutionManifest(partial_manifest),
            raw_store=RawResponseStore(tmp_path / "partial-raw"),
            joined_store=JoinedObservationStore(tmp_path / "partial-joined"),
        )

    retry_calls = []

    class RetryTransport:
        def fetch(self, request, credential):
            retry_calls.append(request.request_identifier)
            return _response_for(plan.fixture_targets[0], request)

    with pytest.raises(ResumeSafetyError):
        executor.execute(
            context,
            transport=RetryTransport(),
            credential_provider=lambda: "must-not-be-read-after-uncertain-request",
            manifest=ExecutionManifest(partial_manifest),
            raw_store=RawResponseStore(tmp_path / "partial-raw"),
            joined_store=JoinedObservationStore(tmp_path / "partial-joined"),
        )
    assert retry_calls == []


def test_raw_response_store_is_write_once_and_filters_secret_headers(tmp_path):
    plan = _plan()
    target = _fixture_target(plan)
    request = HistoricalRequest("INITIAL", target["initial_target_utc"])
    response = _response_for(target, request)
    store = RawResponseStore(tmp_path / "raw")
    path = store.write_once(request, response, "digest-1")
    text = path.read_text(encoding="utf-8")
    assert "x-api-key" not in text
    assert "must-not-persist" not in text
    assert store.write_once(request, response, "digest-1") == path
    with pytest.raises(BackfillError, match="immutable raw"):
        store.write_once(request, response, "digest-2")
