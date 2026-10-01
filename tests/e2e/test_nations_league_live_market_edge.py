from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from src.analysis.nations_league_live_edge import normalize_market_snapshot_input
from src.analysis.nations_league_live_runtime import (
    append_live_store,
    build_fresh_input_state,
    load_active_release,
    run_live_cycle,
)
from src.notifications.nations_league_live_public import (
    build_live_public_nations_league,
    validate_live_public_nations_league,
)
from src.notifications.public_serializer import serialize_public_product
from src.scanner.nations_league_live_market import acquire_live_market_snapshots
from scripts.build_nations_league_live_public import materialize

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "results/audits/nations_league_forward_fixture_manifest.json"
BASE_TIMELINE = ROOT / "results/research/nations_league_fixture_timeline_v1.json"
RESULT_EXTENSION = ROOT / (
    "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
)
REGISTRY = ROOT / "results/audits/continuous_model_lifecycle_registry.json"
CAMPAIGN = ROOT / (
    "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json"
)
BINDING = ROOT / "results/audits/nations_league_v1_1_live_evidence_binding.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _synthetic_manifest() -> tuple[dict, dict]:
    source = next(
        row
        for row in _load(MANIFEST)["fixtures"]
        if row["home_team"] == "France" and row["away_team"] == "Italy"
    )
    fixture = deepcopy(source)
    fixture["fixture_id"] = "uefa-nl:synthetic-market-edge"
    manifest = deepcopy(_load(MANIFEST))
    manifest["fixtures"] = [fixture]
    manifest["manifest_digest"] = _digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    )
    return manifest, fixture


def _provider_event(fixture: dict, *, event_id: str, odds: dict[str, float]) -> dict:
    return {
        "id": event_id,
        "sport_key": "soccer_uefa_nations_league",
        "sport_title": "UEFA Nations League",
        "commence_time": fixture["kickoff_utc"],
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": fixture["home_team"], "price": odds["home"]},
                            {"name": "Draw", "price": odds["draw"]},
                            {"name": fixture["away_team"], "price": odds["away"]},
                        ],
                    }
                ],
            }
        ],
    }


def _capture(
    manifest: dict,
    fixture: dict,
    *,
    as_of: str,
    event_id: str,
    odds: dict[str, float],
    existing_records: list[dict] | None = None,
) -> dict:
    return acquire_live_market_snapshots(
        manifest,
        as_of=as_of,
        existing_records=existing_records or [],
        fetcher=lambda: (
            [_provider_event(fixture, event_id=event_id, odds=odds)],
            1,
            0,
            {
                "method": "GET",
                "url": "https://api.the-odds-api.com/v4/sports/soccer_uefa_nations_league/odds",
            },
        ),
        now=datetime.fromisoformat(as_of.replace("Z", "+00:00")),
    )


def _node_validates_worker_and_pwa(payload_path: Path, *, now: str) -> None:
    worker = (ROOT / "cloudflare/worker.js").as_posix()
    app = (ROOT / "docs/js/app.js").as_posix()
    script = f"""
import {{ readFileSync }} from 'node:fs';
import {{ pathToFileURL }} from 'node:url';
import vm from 'node:vm';
import {{ webcrypto }} from 'node:crypto';

const payload = JSON.parse(readFileSync(process.argv[1], 'utf8')).nations_league;
const worker = await import(pathToFileURL({json.dumps(worker)}).href);
if (!(await worker.validatePublicNationsLeagueDigest(payload))) throw new Error('Worker rejected LIVE payload');

const appSource = readFileSync({json.dumps(app)}, 'utf8');
const start = appSource.indexOf('function _canonicalNationsLeagueJson(');
const end = appSource.indexOf('\\nfunction _top5LifecycleError', start);
if (start < 0 || end < 0) throw new Error('PWA validator seam missing');
const context = {{ crypto: webcrypto, TextEncoder, JSON, Object, Array, Uint8Array, String, Date, Number, Math }};
vm.createContext(context);
vm.runInContext(appSource.slice(start, end) + '\\nglobalThis.valid = _validNationsLeaguePublicPayload;', context);
if (!(await context.valid(payload, Date.parse({json.dumps(now)})))) throw new Error('PWA rejected LIVE payload');
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script, str(payload_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_live_prediction(manifest: dict, *, as_of: str, market_batch: dict, existing=None) -> dict:
    state = build_fresh_input_state(
        manifest,
        _load(BASE_TIMELINE),
        _load(RESULT_EXTENSION),
        prediction_cutoff=as_of,
    )
    release = load_active_release(REGISTRY)
    result = run_live_cycle(
        manifest,
        state,
        release,
        as_of=as_of,
        existing_records=existing or [],
        execute=True,
        market_snapshots=normalize_market_snapshot_input(
            market_batch, fixtures=state["fixtures"]
        ),
    )
    assert result["status"] == "MATERIALIZED"
    assert result["appended_count"] == 1
    return result["appended_records"][0]


def test_real_offline_market_to_worker_and_pwa_edge_chain(tmp_path):
    manifest, fixture = _synthetic_manifest()
    as_of = "2026-10-01T18:45:00Z"
    batch = _capture(
        manifest,
        fixture,
        as_of=as_of,
        event_id="synthetic-odds-event-initial",
        odds={"home": 2.3, "draw": 3.2, "away": 3.0},
    )

    assert batch["status"] == "READY"
    assert batch["request_count"] == 1
    assert batch["retry_count"] == 0
    assert batch["provider"] == "the_odds_api"
    snapshot = batch["snapshots"][0]
    assert snapshot["bookmaker"] == "pinnacle"
    assert snapshot["captured_at"] == as_of
    assert len(snapshot["snapshot_digest"]) == 64
    assert abs(sum(snapshot["margin_free_probabilities"].values()) - 1.0) < 1e-9

    record = _run_live_prediction(manifest, as_of=as_of, market_batch=batch)
    edge = record["edge_analysis"]
    assert edge["fixture_id"] == fixture["fixture_id"]
    assert edge["market_snapshot"]["captured_at"] <= record["prediction_timestamp"]
    assert edge["edge_status"] == "EDGE_MEASURED"
    assert set(edge["outcomes"]) == {"home", "draw", "away"}
    assert all("probability_edge" in outcome and "ev" in outcome for outcome in edge["outcomes"].values())
    assert record["no_bet"] is True
    assert record["betting_enabled"] is False
    assert record["ledger_mutation"] is False

    campaign_records = _load(CAMPAIGN)["records"]
    public = build_live_public_nations_league(
        [*campaign_records, record],
        active_release=load_active_release(REGISTRY),
        evidence_binding=_load(BINDING),
        as_of="2026-10-01T18:45:01Z",
    )
    assert validate_live_public_nations_league(public)["public_digest"] == public["public_digest"]
    serialized = serialize_public_product({"nations_league": public})
    assert serialized["nations_league"] == public
    public_fixture = next(item for item in public["fixtures"] if item["fixture_id"] == fixture["fixture_id"])
    assert public_fixture["edge_analysis"]["edge_digest"] == edge["edge_digest"]

    store_path = tmp_path / "live.jsonl"
    append_live_store(store_path, [], [record])
    materialized_path = tmp_path / "materialized-signals.json"
    materialized = materialize(
        campaign=CAMPAIGN,
        registry=REGISTRY,
        binding=BINDING,
        inputs=[ROOT / "docs/data/signals.json"],
        store=store_path,
        output=materialized_path,
        as_of="2026-10-01T18:45:01Z",
    )
    assert materialized["public_digest"] == public["public_digest"]

    payload_path = tmp_path / "signals.json"
    payload_path.write_text(
        json.dumps({"nations_league": materialized}), encoding="utf-8"
    )
    _node_validates_worker_and_pwa(payload_path, now="2026-10-01T18:45:02Z")


def test_initial_and_refinement_are_distinct_and_initial_remains_recoverable():
    manifest, fixture = _synthetic_manifest()
    initial_as_of = "2026-10-01T18:45:00Z"
    initial_batch = _capture(
        manifest,
        fixture,
        as_of=initial_as_of,
        event_id="synthetic-odds-event-initial",
        odds={"home": 2.3, "draw": 3.2, "away": 3.0},
    )
    initial = _run_live_prediction(
        manifest,
        as_of=initial_as_of,
        market_batch=initial_batch,
    )
    refinement_as_of = "2026-10-02T17:15:00Z"
    refinement_batch = _capture(
        manifest,
        fixture,
        as_of=refinement_as_of,
        event_id="synthetic-odds-event-refinement",
        odds={"home": 2.0, "draw": 3.4, "away": 3.4},
        existing_records=[initial],
    )
    refinement = _run_live_prediction(
        manifest,
        as_of=refinement_as_of,
        market_batch=refinement_batch,
        existing=[initial],
    )

    assert initial["phase"] == "initial"
    assert refinement["phase"] == "refinement"
    assert initial["edge_analysis"]["edge_digest"] != refinement["edge_analysis"]["edge_digest"]
    assert initial["edge_analysis"]["market_snapshot"]["snapshot_digest"] != refinement["edge_analysis"]["market_snapshot"]["snapshot_digest"]
    initial_edge_digest = initial["edge_analysis"]["edge_digest"]
    public = build_live_public_nations_league(
        [*_load(CAMPAIGN)["records"], initial, refinement],
        active_release=load_active_release(REGISTRY),
        evidence_binding=_load(BINDING),
        as_of="2026-10-02T17:15:01Z",
    )
    current = next(item for item in public["fixtures"] if item["fixture_id"] == fixture["fixture_id"])
    history = next(item for item in public["audit_history"] if item["fixture_id"] == fixture["fixture_id"])
    assert current["phase"] == "refinement"
    assert current["edge_analysis"]["edge_digest"] == refinement["edge_analysis"]["edge_digest"]
    assert initial["edge_analysis"]["edge_digest"] == initial_edge_digest
    assert history["source_prediction_record_ids"] == [
        initial["record_id"],
        refinement["record_id"],
    ]


def test_provider_failure_materializes_only_safe_no_market_edge():
    manifest, fixture = _synthetic_manifest()
    batch = acquire_live_market_snapshots(
        manifest,
        as_of="2026-10-01T18:45:00Z",
        fetcher=lambda: (_ for _ in ()).throw(RuntimeError("secret must not escape")),
        now=datetime.fromisoformat("2026-10-01T18:45:00+00:00"),
    )
    assert batch["snapshots"] == []
    assert batch["request_count"] == 0
    assert "secret" not in json.dumps(batch)
    record = _run_live_prediction(manifest, as_of="2026-10-01T18:45:00Z", market_batch=batch)
    assert record["edge_analysis"]["edge_status"] == "NO_MARKET_SNAPSHOT"
    assert record["edge_analysis"]["market_snapshot"] is None
    assert record["no_bet"] is True
    assert record["betting_enabled"] is False
    assert record["ledger_mutation"] is False
