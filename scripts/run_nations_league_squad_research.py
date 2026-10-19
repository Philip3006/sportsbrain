"""Generate the offline Nations League squad/lineup evidence audit."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_squad_research import (
    build_local_research_report,
    write_research_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-main-sha",
        help="Current verified main SHA; defaults to the local origin/main ref.",
    )
    parser.add_argument(
        "--output",
        default="results/audits/nations_league_squad_lineup_research_v1.json",
        help="Research-only JSON report path.",
    )
    args = parser.parse_args()
    source_sha = (
        args.source_main_sha
        or subprocess.check_output(
            ["git", "rev-parse", "origin/main"], cwd=ROOT, text=True
        ).strip()
    )
    if not 40 <= len(source_sha) <= 64 or any(
        char not in "0123456789abcdef" for char in source_sha.lower()
    ):
        parser.error("--source-main-sha must be a Git SHA")
    output_path = (ROOT / args.output).resolve()
    allowed_output_root = (ROOT / "results/audits").resolve()
    if not output_path.is_relative_to(allowed_output_root):
        parser.error("--output must remain under results/audits")
    generated_at = datetime.now(timezone.utc).isoformat()
    report = build_local_research_report(
        ROOT,
        source_main_sha=source_sha.lower(),
        generated_at=generated_at,
    )
    digest = write_research_report(report, output_path)
    print(f"status={report['research_status']}")
    print(f"historical_matches={report['historical_sample']['match_count']}")
    print(
        f"verified_pit_captures={report['historical_sample']['verified_historical_pit_squad_captures']}"
    )
    print(f"report_digest={digest}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
