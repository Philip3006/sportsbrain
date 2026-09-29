#!/usr/bin/env python3
"""Freeze the audited Nations League subset from a local results pickle.

Offline only. The path is explicit, the upstream cache SHA must match the
already audited value, and no downloader is imported or called.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.nations_league_competition_state import canonical_digest
from src.analysis.nations_league_validation import load_local_results, select_historical_matches

AUDITED_CACHE_SHA256 = "6b47d79b84891306d8bb2ce4c0abec810b11818c6e4c3fad9fa2415c228b7f2d"
UPSTREAM_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"


def freeze(source_path: Path, output_path: Path, committed_at: str | None = None) -> dict:
    matches, source_sha = load_local_results(source_path)
    if source_sha != AUDITED_CACHE_SHA256:
        raise SystemExit("Refusing a cache that does not match the audited 512-match source digest")
    selected = select_historical_matches(matches)
    rows = [
        {
            "date": row["date"].date().isoformat(),
            "home_team": str(row["home_team"]),
            "away_team": str(row["away_team"]),
            "home_score": int(row["home_score"]),
            "away_score": int(row["away_score"]),
            "neutral": bool(row["neutral"]),
            "edition": str(row["edition"]),
            "validation_period": str(row["validation_period"]),
        }
        for _, row in selected.iterrows()
    ]
    payload = {
        "schema_version": "uefa-nl-canonical-result-source-v1",
        "competition": "UEFA Nations League",
        "normalization_version": "sportsbrain-nl-team-aliases-v1",
        "upstream_url": UPSTREAM_URL,
        "upstream_cache_sha256": source_sha,
        "snapshot_committed_at": committed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "upstream_cache_retrieved_at": None,
        "upstream_retrieval_note": "Original retrieval timestamp was not available in the ignored local cache metadata.",
        "matches": rows,
    }
    output = {**payload, "snapshot_digest": canonical_digest(payload)}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    temp.write_text(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(output_path)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pickle", type=Path, required=True, help="Explicit local, already-audited pickle path")
    parser.add_argument("--output", type=Path, required=True, help="Canonical JSON source snapshot output")
    parser.add_argument("--committed-at", help="Optional fixed UTC timestamp for deterministic regeneration")
    args = parser.parse_args()
    output = freeze(args.source_pickle, args.output, args.committed_at)
    print(json.dumps({"matches": len(output["matches"]), "source_cache_sha256": output["upstream_cache_sha256"],
                      "snapshot_digest": output["snapshot_digest"], "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
