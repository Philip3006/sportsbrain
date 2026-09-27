from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data import isports_api
from src.scanner import nations_league_isports_shadow as shadow
from src.scanner.nations_league_shadow import _canonical_json

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


class Response:
    def __init__(self, data, *, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {
            "content-type": "application/json",
            "x-ratelimit-remaining": "73",
            "set-cookie": "must-not-persist",
        }
        self.content = json.dumps(
            {"code": 0, "message": "success", "data": data}
        ).encode()
        self._payload = {"code": 0, "message": "success", "data": data}

    def json(self):
        return self._payload


def _schedule(
    match_id="nl-1", *, neutral=True, kickoff="2026-10-05T18:45:00Z", **overrides
):
    import pandas as pd

    row = {
        "matchId": match_id,
        "leagueId": "146819",
        "leagueType": 2,
        "leagueName": "UEFA Nations League",
        "matchTime": int(pd.Timestamp(kickoff).timestamp()),
        "status": 0,
        "homeName": "England",
        "awayName": "France",
        "neutral": neutral,
    }
    row.update(overrides)
    return row


def _odds(
    match_id="nl-1",
    *,
    home="England",
    away="France",
    kickoff="2026-10-05T18:45:00Z",
    count=3,
):
    import pandas as pd

    changed = int(pd.Timestamp("2026-09-27T11:55:00Z").timestamp())
    details = []
    for index in range(count):
        details.append(
            {
                "changeTime": changed + index,
                "oddsDetail": [
                    f"{100 + index},Bookmaker {index},2.4,3.1,3.2,{2.4 + index / 100},3.1,3.2"
                ],
            }
        )
    return {
        "matchId": match_id,
        "matchTime": int(pd.Timestamp(kickoff).timestamp()),
        "leagueName": "UEFA Nations League",
        "homeName": home,
        "awayName": away,
        "odds": details,
    }


def _prediction(**kwargs):
    return {
        "provider_event_id": kwargs["event"]["id"],
        "kickoff": kwargs["event"]["commence_time"],
        "captured_at": kwargs["captured_at"].isoformat(),
        "home_team": kwargs["event"]["home_team"],
        "away_team": kwargs["event"]["away_team"],
        "neutral": kwargs["neutral"],
        "market": {
            "bookmaker": kwargs["odds"]["bookmaker"],
            "odds_decimal": {
                "home": kwargs["odds"]["home"],
                "draw": kwargs["odds"]["draw"],
                "away": kwargs["odds"]["away"],
            },
            "margin_free_probabilities": {"home": 0.4, "draw": 0.3, "away": 0.3},
        },
        "probabilities": {
            "raw_dixon_coles": {"home": 0.4, "draw": 0.3, "away": 0.3},
            "raw_gbt": {"home": 0.4, "draw": 0.3, "away": 0.3},
            "canonical_stacker": {"home": 0.4, "draw": 0.3, "away": 0.3},
            "final_ensemble": {"home": 0.4, "draw": 0.3, "away": 0.3},
            "market_anchored": None,
        },
        "market_anchor_status": "not_applied_unbound_to_frozen_stacker_contract",
        "model_vs_market_edge_percentage_points": {"home": 0, "draw": 0, "away": 0},
    }


def _patch_offline_runtime(monkeypatch):
    monkeypatch.setattr(
        shadow,
        "load_frozen_snapshot",
        lambda _path: SimpleNamespace(
            provenance=lambda: {
                "identity": "wm2026-test",
                "digest": "a" * 64,
                "files": {},
            }
        ),
    )
    monkeypatch.setattr(
        shadow,
        "load_cached_history",
        lambda *args, **kwargs: (object(), {"sha256": "b" * 64}),
    )
    monkeypatch.setattr(shadow, "current_source_sha", lambda _root: "c" * 40)
    monkeypatch.setattr(shadow, "predict_fixture", _prediction)


def test_single_operation_helper_disables_redirects_and_redacts_key():
    seen = {}

    def transport(url, *, params, timeout, allow_redirects):
        seen.update(
            url=url,
            params=dict(params),
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        return Response([{"matchId": "1"}])

    operation = isports_api.request_once(
        api_key="secret-value",
        operation_kind="schedule",
        ordinal=1,
        endpoint_path=isports_api.SCHEDULE_PATH,
        query={"leagueId": "146819"},
        transport=transport,
    )
    assert seen["url"] == "https://api.isportsapi.com/sport/football/schedule/basic"
    assert seen["params"] == {"leagueId": "146819", "api_key": "secret-value"}
    assert seen["allow_redirects"] is False
    assert operation.manifest_entry["query"] == {"leagueId": "146819"}
    assert operation.manifest_entry["status_code"] == 200
    assert set(operation.manifest_entry) == {
        "ordinal",
        "operation",
        "method",
        "path",
        "query",
        "status_code",
        "started_at",
        "completed_at",
        "response_sha256",
    }
    assert "secret-value" not in json.dumps(operation.manifest_entry)


def test_latest_valid_quote_per_bookmaker_and_component_median():
    record = _odds(count=151)
    record["odds"].append(
        {
            "changeTime": 1790512200,
            "oddsDetail": ["100,Bookmaker 0,2,3,4,2.8,3.1,3.2"],
        }
    )
    quotes = shadow._bookmaker_quotes(record)
    assert len(quotes) == 151
    newest = next(row for row in quotes if row["company_id"] == "100")
    assert newest["odds_decimal"]["home"] == 2.8
    market = shadow._market_for_fixture(record, match_id="nl-1")
    assert market["bookmaker_count"] == 151
    assert (
        market["aggregation"]
        == "latest_valid_quote_per_bookmaker_then_componentwise_median"
    )


def test_schedule_uses_target_window_and_preserves_neutral():
    eligible, excluded = shadow._schedule_fixtures(
        [
            _schedule("target", neutral=True),
            _schedule("outside", kickoff="2026-11-18T00:00:00Z"),
        ],
        captured_at=NOW,
    )
    assert [row["provider_match_id"] for row in eligible] == ["target"]
    assert eligible[0]["neutral"] is True
    assert excluded == [
        {"provider_match_id": "outside", "reason": "outside_active_window"}
    ]


def test_one_run_has_two_bulk_requests_complete_reconciliation_and_immutable_artifact(
    tmp_path, monkeypatch
):
    _patch_offline_runtime(monkeypatch)
    schedule = [
        _schedule("nl-1", neutral=True),
        _schedule("nl-2", neutral=False, homeName="Spain", awayName="Italy"),
    ]
    odds_rows = [
        _odds("nl-1"),
        _odds("nl-2", home="Spain", away="Italy"),
    ]
    responses = [Response(schedule), Response(odds_rows)]
    calls = []

    def transport(url, *, params, timeout, allow_redirects):
        calls.append((url, dict(params), allow_redirects))
        return responses.pop(0)

    path = tmp_path / "capture.json"
    artifact, written = shadow.run_isports_shadow_scan(
        api_key="test-secret",
        transport=transport,
        source_root=ROOT,
        output_path=path,
        now=NOW,
    )
    assert written == path
    assert len(calls) == 2
    assert calls[0][0].endswith(isports_api.SCHEDULE_PATH)
    assert calls[0][1]["leagueId"] == "146819"
    assert calls[1][0].endswith(isports_api.EUROPEAN_ODDS_PATH)
    assert calls[1][1]["day"] == "52"
    assert all(call[2] is False for call in calls)
    assert artifact["provider"] == "isports_api"
    assert artifact["provider_league_id"] == 146819
    assert artifact["request_count"] == 2
    assert artifact["retry_count"] == 0
    assert [
        entry["operation"] for entry in artifact["provider_operation_manifest"]
    ] == [
        "schedule",
        "odds",
    ]
    assert [entry["query"] for entry in artifact["provider_operation_manifest"]] == [
        {"leagueId": "146819"},
        {"day": 52},
    ]
    assert all(
        set(entry)
        == {
            "ordinal",
            "operation",
            "method",
            "path",
            "query",
            "status_code",
            "started_at",
            "completed_at",
            "response_sha256",
        }
        for entry in artifact["provider_operation_manifest"]
    )
    assert artifact["coverage"]["complete"] is True
    assert artifact["coverage"]["eligible_match_ids"] == ["nl-1", "nl-2"]
    assert artifact["fixtures"][0]["neutral"] is True
    assert artifact["fixtures"][1]["neutral"] is False
    assert all("provider_event_id" not in row for row in artifact["fixtures"])
    assert [row["provider_match_id"] for row in artifact["fixtures"]] == [
        "nl-1",
        "nl-2",
    ]
    serialized = path.read_text()
    assert "test-secret" not in serialized
    body = copy.deepcopy(artifact)
    digest = body.pop("artifact_digest")
    assert digest == shadow._sha256_bytes(_canonical_json(body))
    assert json.loads(serialized)["artifact_digest"] == digest


def test_missing_odds_for_any_eligible_fixture_fails_closed_without_artifact(
    tmp_path, monkeypatch
):
    _patch_offline_runtime(monkeypatch)
    responses = [
        Response(
            [_schedule("one"), _schedule("two", homeName="Spain", awayName="Italy")]
        ),
        Response([_odds("one")]),
    ]
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    output = tmp_path / "must-not-exist.json"
    with pytest.raises(
        shadow.NationsLeagueIsportsError, match="coverage gap: missing matchId two"
    ):
        shadow.run_isports_shadow_scan(
            api_key="test-secret",
            transport=transport,
            source_root=ROOT,
            output_path=output,
            now=NOW,
        )
    assert len(calls) == 2
    assert not output.exists()


def test_missing_history_preflight_spends_no_provider_requests_or_credentials(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        shadow,
        "load_frozen_snapshot",
        lambda _path: SimpleNamespace(provenance=dict),
    )

    def missing_history(*args, **kwargs):
        from src.scanner.nations_league_shadow import NationsLeagueShadowError

        raise NationsLeagueShadowError("local international-results cache is missing")

    monkeypatch.setattr(shadow, "load_cached_history", missing_history)
    monkeypatch.setattr(
        shadow,
        "load_isports_api_key",
        lambda: pytest.fail("credential must not be accessed before offline preflight"),
    )
    with pytest.raises(
        shadow.NationsLeagueIsportsError,
        match="offline shadow preflight failed: local international-results cache is missing",
    ) as exc_info:
        shadow.run_isports_shadow_scan(
            transport=lambda *args, **kwargs: calls.append((args, kwargs)),
            source_root=ROOT,
            now=NOW,
        )
    assert exc_info.value.request_count == 0
    assert calls == []


def test_odds_identity_mismatch_fails_closed():
    fixture = shadow._schedule_fixtures([_schedule("same")], captured_at=NOW)[0][0]
    with pytest.raises(shadow.NationsLeagueIsportsError, match="homeName disagrees"):
        shadow._odds_records_by_match([_odds("same", home="Portugal")], [fixture])
