from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.analysis.nations_league_live_edge import build_market_snapshot
from src.analysis.nations_league_live_market_enrichment import (
    NationsLeagueLiveMarketEnrichmentError,
    append_market_enrichments,
    build_market_enrichment,
    load_market_enrichments,
    record_has_valid_market_edge,
    select_market_enrichment,
    validate_market_enrichment,
)
from src.scanner.nations_league_live_market import (
    acquire_live_market_snapshots,
    prepare_market_preflight,
)

ROOT = Path(__file__).parents[2]
STORE = ROOT / "results/research/nations_league_v1_1_live_prediction_store.jsonl"


def _record() -> dict:
    rows = [
        json.loads(line)
        for line in STORE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return next(row for row in rows if row["phase"] == "refinement")


def _snapshot(record: dict, captured_at: str) -> dict:
    return build_market_snapshot(
        {
            "provider": "isports_api",
            "bookmaker": "Reviewed bookmaker median",
            "captured_at": captured_at,
            "fixture_id": record["fixture_id"],
            "provider_match_id": "isports-recovery-match",
            "odds_decimal": {"home": 2.2, "draw": 3.4, "away": 3.1},
        }
    )


def _manifest_for(record: dict) -> dict:
    body = {
        "schema": "nations-league-future-fixture-manifest-v1",
        "competition": "UEFA Nations League",
        "fixtures": [
            {
                "fixture_id": record["fixture_id"],
                "status": "VERIFIED",
                "home_team": record["home_team"],
                "away_team": record["away_team"],
                "kickoff_utc": record["kickoff_utc"],
            }
        ],
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**body, "manifest_digest": digest}


def _event(record: dict) -> dict:
    return {
        "id": "recovery-event",
        "sport_key": "soccer_uefa_nations_league",
        "sport_title": "UEFA Nations League",
        "commence_time": record["kickoff_utc"],
        "home_team": record["home_team"],
        "away_team": record["away_team"],
        "bookmakers": [
            {
                "key": "reviewed-bookmaker",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": record["home_team"], "price": 2.2},
                            {"name": "Draw", "price": 3.4},
                            {"name": record["away_team"], "price": 3.1},
                        ],
                    }
                ],
            }
        ],
    }


def test_enrichment_binds_later_market_without_mutating_prediction():
    record = _record()
    original = deepcopy(record)
    enrichment = build_market_enrichment(
        record, _snapshot(record, "2026-10-01T16:46:04Z")
    )

    assert record == original
    assert record_has_valid_market_edge(record) is False
    assert enrichment["prediction_record_id"] == record["record_id"]
    assert enrichment["model_release_id"] == record["model_release_id"]
    assert enrichment["captured_at"] == "2026-10-01T16:46:04Z"
    assert enrichment["edge_analysis"]["edge_status"] in {"EDGE_MEASURED", "NO_EDGE"}
    assert enrichment["no_bet"] is True
    assert enrichment["betting_enabled"] is False
    assert enrichment["ledger_mutation"] is False
    assert validate_market_enrichment(enrichment, record=record) == enrichment


def test_capture_batch_emits_recovery_enrichment_for_existing_no_market_record(
    monkeypatch,
):
    record = _record()
    from src.signals import provider_budget

    monkeypatch.setattr(provider_budget, "get_budget_snapshot", dict)
    monkeypatch.setattr(
        provider_budget,
        "odds_api_quota_state",
        lambda: {"requests_remaining": 5},
    )
    batch = acquire_live_market_snapshots(
        _manifest_for(record),
        as_of="2026-10-01T16:46:04Z",
        existing_records=[record],
        fetcher=lambda: ([_event(record)], 1, 0, {"method": "GET"}),
        now=datetime(2026, 10, 1, 16, 46, 4, tzinfo=timezone.utc),
    )

    assert batch["status"] == "READY"
    assert len(batch["snapshots"]) == 1
    assert len(batch["market_enrichments"]) == 1
    enrichment = batch["market_enrichments"][0]
    assert enrichment["prediction_record_id"] == record["record_id"]
    assert (
        enrichment["edge_analysis"]["market_snapshot"]["snapshot_digest"]
        == batch["snapshots"][0]["snapshot_digest"]
    )


def test_enrichment_store_is_append_only_and_exact_repeat_is_idempotent(tmp_path):
    record = _record()
    enrichment = build_market_enrichment(
        record, _snapshot(record, "2026-10-01T16:46:04Z")
    )
    path = tmp_path / "market-enrichment.jsonl"

    first = append_market_enrichments(path, [], [enrichment])
    second = append_market_enrichments(path, first, [enrichment])

    assert second == first
    assert load_market_enrichments(path) == first
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert select_market_enrichment(second, record) == enrichment


def test_existing_valid_enrichment_removes_provider_need_without_reacquisition(
    monkeypatch,
):
    record = _record()
    enrichment = build_market_enrichment(
        record, _snapshot(record, "2026-10-01T16:46:04Z")
    )
    from src.signals import provider_budget

    monkeypatch.setattr(provider_budget, "get_budget_snapshot", dict)
    monkeypatch.setattr(
        provider_budget,
        "odds_api_quota_state",
        lambda: {"requests_remaining": 0},
    )
    batch = prepare_market_preflight(
        _manifest_for(record),
        as_of="2026-10-01T16:46:04Z",
        existing_records=[record],
        existing_enrichments=[enrichment],
    )

    assert batch["needs_provider"] is False
    assert batch["due_fixtures"] == []
    assert batch["total_network_request_count"] == 0


def test_enrichment_rejects_backdating_and_capture_outside_phase():
    record = _record()
    with pytest.raises(NationsLeagueLiveMarketEnrichmentError, match="backdate"):
        build_market_enrichment(record, _snapshot(record, "2026-10-01T16:00:00Z"))
    with pytest.raises(NationsLeagueLiveMarketEnrichmentError, match="outside"):
        build_market_enrichment(record, _snapshot(record, "2026-10-01T18:00:00Z"))


def test_enrichment_substitution_fails_closed():
    record = _record()
    enrichment = build_market_enrichment(
        record, _snapshot(record, "2026-10-01T16:46:04Z")
    )
    tampered = deepcopy(enrichment)
    tampered["snapshot_digest"] = "f" * 64
    with pytest.raises(
        NationsLeagueLiveMarketEnrichmentError, match="snapshot binding"
    ):
        validate_market_enrichment(tampered, record=record)
