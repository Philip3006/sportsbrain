#!/usr/bin/env python3
"""Build the offline 20-fixture public-web-odds pilot artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.research.nations_league_web_odds_pilot import PHASES, phase_record, summarize

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "results" / "research"
TIMELINE_DIGEST = "2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef"

FIXTURES = [
    {"fixture_id":"uefa-nl:bd37b7f2d3fd6b2c45e4e9ba","edition":"2022/23","group":"A4","tier":"A","home_team":"Poland","away_team":"Wales","kickoff_utc":"2022-06-01T16:00:00Z"},
    {"fixture_id":"uefa-nl:2044d1e9a309ac7908bffd7f","edition":"2022/23","group":"B1","tier":"B","home_team":"Scotland","away_team":"Ukraine","kickoff_utc":"2022-09-21T18:45:00Z"},
    {"fixture_id":"uefa-nl:0f938fccd923f265d246b497","edition":"2022/23","group":"D2","tier":"D","home_team":"Estonia","away_team":"San Marino","kickoff_utc":"2022-06-02T18:45:00Z"},
    {"fixture_id":"uefa-nl:fe74fd2d344963825fcd9436","edition":"2022/23","group":"C4","tier":"C","home_team":"Georgia","away_team":"Gibraltar","kickoff_utc":"2022-06-02T16:00:00Z"},
    {"fixture_id":"uefa-nl:00821582e84e0cad16c98140","edition":"2022/23","group":"C4","tier":"C","home_team":"Bulgaria","away_team":"North Macedonia","kickoff_utc":"2022-06-02T18:45:00Z"},
    {"fixture_id":"uefa-nl:37fd600248ee1982cbdb1a9d","edition":"2022/23","group":"B3","tier":"B","home_team":"Finland","away_team":"Romania","kickoff_utc":"2022-09-23T18:45:00Z"},
    {"fixture_id":"uefa-nl:dc63dcb1f41aba5f1fff14fa","edition":"2022/23","group":"A2","tier":"A","home_team":"Spain","away_team":"Portugal","kickoff_utc":"2022-06-02T18:45:00Z"},
    {"fixture_id":"uefa-nl:e3e7193c685cf4acef6edec7","edition":"2022/23","group":"C2","tier":"C","home_team":"Cyprus","away_team":"Kosovo","kickoff_utc":"2022-06-02T18:45:00Z"},
    {"fixture_id":"uefa-nl:bf312c1f429a0a75f23911f7","edition":"2022/23","group":"D1","tier":"D","home_team":"Latvia","away_team":"Moldova","kickoff_utc":"2022-09-22T18:45:00Z"},
    {"fixture_id":"uefa-nl:32caffd3bcda396490b2dbd2","edition":"2022/23","group":"B4","tier":"B","home_team":"Serbia","away_team":"Norway","kickoff_utc":"2022-06-02T18:45:00Z"},
    {"fixture_id":"uefa-nl:1d869aa7871bdc18c8508729","edition":"2024/25","group":"A4","tier":"A","home_team":"Denmark","away_team":"Switzerland","kickoff_utc":"2024-09-05T18:45:00Z"},
    {"fixture_id":"uefa-nl:ab9e4cfa635c4c330ae17f50","edition":"2024/25","group":"B3","tier":"B","home_team":"Austria","away_team":"Kazakhstan","kickoff_utc":"2024-10-10T18:45:00Z"},
    {"fixture_id":"uefa-nl:a26cd4d0342ad16292627388","edition":"2024/25","group":"C4","tier":"C","home_team":"North Macedonia","away_team":"Latvia","kickoff_utc":"2024-11-14T19:45:00Z"},
    {"fixture_id":"uefa-nl:5c5c437a797d8fde61509929","edition":"2024/25","group":"D1","tier":"D","home_team":"San Marino","away_team":"Liechtenstein","kickoff_utc":"2024-09-05T18:45:00Z"},
    {"fixture_id":"uefa-nl:32adc9d4bfb1c729b8279d8f","edition":"2024/25","group":"C3","tier":"C","home_team":"Belarus","away_team":"Bulgaria","kickoff_utc":"2024-09-05T18:45:00Z"},
    {"fixture_id":"uefa-nl:64b4efc331ea6d24f0803ff8","edition":"2024/25","group":"A2","tier":"A","home_team":"Italy","away_team":"Belgium","kickoff_utc":"2024-10-10T18:45:00Z"},
    {"fixture_id":"uefa-nl:43a3bc11c6c41060c0491c51","edition":"2024/25","group":"D1","tier":"D","home_team":"San Marino","away_team":"Gibraltar","kickoff_utc":"2024-11-15T19:45:00Z"},
    {"fixture_id":"uefa-nl:07dbbcd98364447cd0fe8f5f","edition":"2024/25","group":"C1","tier":"C","home_team":"Azerbaijan","away_team":"Sweden","kickoff_utc":"2024-09-05T16:00:00Z"},
    {"fixture_id":"uefa-nl:d0e0402b237930c814bd8b8c","edition":"2024/25","group":"B2","tier":"B","home_team":"England","away_team":"Greece","kickoff_utc":"2024-10-10T18:45:00Z"},
    {"fixture_id":"uefa-nl:7de05eb0d9766ce853bda88f","edition":"2024/25","group":"A2","tier":"A","home_team":"Belgium","away_team":"Italy","kickoff_utc":"2024-11-14T19:45:00Z"},
]

# These are deliberately untime-stamped: the public pages expose historical
# odds but do not prove an INITIAL/REFINEMENT capture.  They are never promoted
# to target-phase evidence.
OBSERVATIONS = {
    ("uefa-nl:00821582e84e0cad16c98140", "https://tipsterarea.com/match/north-macedonia-bulgaria-uefa-nations-league-group-stage-730350"): {
        "source_name": "TipsterArea",
        "source_url": "https://tipsterarea.com/match/north-macedonia-bulgaria-uefa-nations-league-group-stage-730350",
        "odds_decimal": [2.58, 2.96, 2.89],
        "evidence_status": "UNTIMESTAMPED_HISTORICAL",
        "source_quality": {"direct_url": True, "exact_fixture": True, "timestamp_explicit": False, "aggregate_or_bookmaker": "aggregate"},
        "notes": "Search-visible pre-match 1X2 row; no target-phase capture timestamp.",
    },
    ("uefa-nl:1d869aa7871bdc18c8508729", "https://www.oddsmath.com/football/matches/2024-09-05/"): {
        "source_name": "Odds Math",
        "source_url": "https://www.oddsmath.com/football/matches/2024-09-05/",
        "odds_decimal": [2.685, 3.165, 3.17],
        "evidence_status": "UNTIMESTAMPED_HISTORICAL",
        "source_quality": {"direct_url": True, "exact_fixture": True, "timestamp_explicit": False, "aggregate_or_bookmaker": "aggregate"},
        "notes": "Search-visible historical 1X2 row; page does not prove target-phase capture timing.",
    },
    ("uefa-nl:a26cd4d0342ad16292627388", "https://marbet.com.mk/content/Documents/Marbet%20-%202024-11-14T094832.879.pdf"): {
        "source_name": "Marbet PDF",
        "source_url": "https://marbet.com.mk/content/Documents/Marbet%20-%202024-11-14T094832.879.pdf",
        "odds_decimal": [1.45, 4.20, 8.00],
        "evidence_status": "UNTIMESTAMPED_HISTORICAL",
        "source_quality": {"direct_url": True, "exact_fixture": True, "timestamp_explicit": False, "aggregate_or_bookmaker": "bookmaker"},
        "notes": "PDF filename contains a publication-like timestamp, but no auditable capture-time semantics are established.",
    },
    ("uefa-nl:5c5c437a797d8fde61509929", "https://annabet.com/en/soccerstats/serie_735_UEFA_Nations_League_D%2C222%2Cseason_2024-2025.html"): {
        "source_name": "AnnaBet",
        "source_url": "https://annabet.com/en/soccerstats/serie_735_UEFA_Nations_League_D%2C222%2Cseason_2024-2025.html",
        "odds_decimal": [3.86, 2.91, 2.16],
        "evidence_status": "UNTIMESTAMPED_HISTORICAL",
        "source_quality": {"direct_url": True, "exact_fixture": True, "timestamp_explicit": False, "aggregate_or_bookmaker": "aggregate"},
        "notes": "Historical 1X2 row found; no target-phase timestamp.",
    },
}


def build() -> tuple[dict, dict]:
    records = []
    registry = []
    for fixture in FIXTURES:
        obs = next((value for (fixture_id, _), value in OBSERVATIONS.items() if fixture_id == fixture["fixture_id"]), None)
        if obs:
            registry.append({"fixture_id": fixture["fixture_id"], **obs})
        for phase in PHASES:
            records.append(phase_record(fixture, phase, obs))
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    artifact = {
        "schema": "nations-league-web-odds-pilot-v1",
        "decision": "NL_WEB_ODDS_PILOT_NOT_VIABLE",
        "research_only": True,
        "public_web_searches_performed": True,
        "provider_network_requests": 0,
        "provider_api_requests": 0,
        "credentials_accessed": False,
        "timeline": {"source": "PR #215", "dataset_digest": TIMELINE_DIGEST, "competition": "UEFA Nations League"},
        "pilot": {"fixture_count": len(FIXTURES), "editions": ["2022/23", "2024/25"], "phases": list(PHASES)},
        "generated_at": generated_at,
        "records": records,
        "summary": summarize(records, len(FIXTURES)),
    }
    source_registry = {
        "schema": "nations-league-web-odds-source-registry-v1",
        "generated_at": generated_at,
        "research_only": True,
        "provider_network_requests": 0,
        "credentials_accessed": False,
        "source_records": registry,
        "unusable_source_attempts": [
            {"source_name": "UEFA match pages", "reason": "fixture identity/results available, no public historical 1X2 time series"},
            {"source_name": "OddsPortal/BetExplorer candidate pages", "reason": "no auditable target-phase capture retrieved in ordinary public search"},
            {"source_name": "Flashscore/Soccerway candidate pages", "reason": "fixture pages found or searchable, target-phase odds history not exposed"},
        ],
    }
    return artifact, source_registry


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    artifact, registry = build()
    (OUTPUT / "nations_league_web_odds_pilot_20260930.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
    (OUTPUT / "nations_league_web_odds_sources_20260930.json").write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    summary = artifact["summary"]
    report = f"""# Nations League public-web odds pilot\n\nDecision: **{artifact['decision']}**\n\n- Timeline: PR #215; digest `{TIMELINE_DIGEST}`\n- Fixtures attempted: {summary['attempted_fixtures']}\n- Phase attempts: {summary['attempted_phases']}\n- INITIAL exact/near-target: {summary['initial_exact_or_near']}\n- REFINEMENT exact/near-target: {summary['refinement_exact_or_near']}\n- Closing-only: {summary['closing_only']}\n- Unavailable or unusable: {summary['unavailable_or_unusable']}\n\nThe pilot found a small number of public historical 1X2 rows, but none with an auditable timestamp in the requested INITIAL (T-24h ±2h) or REFINEMENT (T-90m ±30m) windows. Untimestamped rows remain research observations only and are not promoted to prediction evidence.\n\nNo provider API, credentials, production path, publication, betting, ledger, or deployment was used.\n"""
    (OUTPUT / "nations_league_web_odds_pilot_20260930.md").write_text(report)
