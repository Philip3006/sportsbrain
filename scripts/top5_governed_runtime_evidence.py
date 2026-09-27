#!/usr/bin/env python3
"""Emit one fresh, read-only governed Top-5 runtime observation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring.top5.governed_runtime_evidence import observe_governed_runtime


def main() -> int:
    payload = observe_governed_runtime()
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload.get("status") == "READY" else 2


if __name__ == "__main__":
    sys.exit(main())
