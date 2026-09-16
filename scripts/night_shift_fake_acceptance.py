"""Run the disposable Builder 5 FakeExecutor acceptance scenario."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.nightshift.acceptance import run_fake_acceptance

if __name__ == "__main__":
    print(json.dumps(run_fake_acceptance(), sort_keys=True, indent=2))
