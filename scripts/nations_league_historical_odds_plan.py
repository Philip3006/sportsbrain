"""Create the offline Nations League historical-odds request plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_historical_odds_plan import (
    build_plan,
    verify_timeline,
    write_artifacts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("results/audits"))
    args = parser.parse_args()
    timeline, head_sha = verify_timeline(args.timeline)
    plan = build_plan(timeline, timeline_head_sha=head_sha)
    paths = write_artifacts(plan, args.output_dir)
    print(json.dumps({"status_marker": plan["status_marker"], "timeline_digest": plan["timeline"]["dataset_digest"], "request_plan_digest": plan["request_plan_digest"], "fixtures": plan["fixture_universe"], "artifacts": [str(path) for path in paths]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
