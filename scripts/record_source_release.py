"""Record source release provenance after ci_gates passes on main.

Called from .github/workflows/ci_gates.yml on push to main only.
Writes docs/data/provenance_meta.json with the SHA and CI run identity
that just passed all gates.

Usage (in CI):
    python3 scripts/record_source_release.py

Reads from environment:
    GITHUB_SHA        — required: the commit that just passed CI
    GITHUB_RUN_ID     — required: the workflow run ID
    GITHUB_WORKFLOW   — optional: workflow filename (default: ci_gates.yml)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_META = ROOT / "docs" / "data" / "provenance_meta.json"


def main() -> int:
    sha = os.environ.get("GITHUB_SHA", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    workflow = os.environ.get("GITHUB_WORKFLOW", "ci_gates.yml").strip()

    if not sha:
        print("[record-source-release] FAIL-CLOSED: GITHUB_SHA not set", file=sys.stderr)
        return 1
    if not run_id:
        print("[record-source-release] FAIL-CLOSED: GITHUB_RUN_ID not set", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schema_version": "1",
        "source_release_sha": sha,
        "source_ci": {
            "run_id": run_id,
            "workflow": workflow,
            "status": "success",
        },
        "recorded_at": now,
    }

    PROVENANCE_META.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROVENANCE_META.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, PROVENANCE_META)

    print(f"[record-source-release] source_release_sha={sha[:12]} run_id={run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
