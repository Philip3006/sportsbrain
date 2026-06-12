#!/bin/bash
# Auto-retrain cron wrapper — invoked by launchd every 12h during WM 2026.
# Fetches fresh martj42 CSV (force=True bypasses cache), retrains DC + LGBM
# when new WM 2026 matches are present.
set -euo pipefail

cd "$(dirname "$0")/.."

TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "===== $TS auto_retrain start ====="

/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    scripts/auto_retrain.py 2>&1

echo "===== $TS auto_retrain done ====="
