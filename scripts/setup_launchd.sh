#!/bin/bash
# Setup script: makes SportsBrain cron wrappers executable and loads launchd agents.
# Run once from any directory: bash /Users/philiprassillier/sportsbrain/scripts/setup_launchd.sh

set -e

SPORTSBRAIN_DIR="/Users/philiprassillier/sportsbrain"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "=== SportsBrain launchd setup ==="

# 1. Make shell wrappers executable
echo "[1/3] Setting execute permissions on cron wrappers..."
chmod +x "$SPORTSBRAIN_DIR/scripts/scan_cron.sh"
chmod +x "$SPORTSBRAIN_DIR/scripts/closing_odds_cron.sh"
echo "      OK: scan_cron.sh, closing_odds_cron.sh"

# 2. Ensure results directory exists
mkdir -p "$SPORTSBRAIN_DIR/results"

# 3. Load (or reload) all three launchd agents
echo "[2/3] Loading launchd agents..."

PLISTS=(
    "com.sportsbrain.daily-scan.plist"
    "com.sportsbrain.closing-odds.plist"
    "com.sportsbrain.closing-odds-evening.plist"
)

for PLIST in "${PLISTS[@]}"; do
    PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST"
    if [ ! -f "$PLIST_PATH" ]; then
        echo "      ERROR: $PLIST_PATH not found — skipping"
        continue
    fi
    # Unload first if already loaded (ignore errors if not loaded)
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    echo "      Loaded: $PLIST"
done

# 4. Verify
echo "[3/3] Verifying loaded agents..."
for LABEL in com.sportsbrain.daily-scan com.sportsbrain.closing-odds com.sportsbrain.closing-odds-evening; do
    STATUS=$(launchctl list "$LABEL" 2>/dev/null | grep '"Label"' || echo "NOT FOUND")
    if echo "$STATUS" | grep -q "$LABEL"; then
        echo "      OK: $LABEL is registered"
    else
        echo "      WARNING: $LABEL not found in launchctl list"
    fi
done

echo ""
echo "=== Setup complete ==="
echo ""
echo "Schedule summary (UTC / CET):"
echo "  09:00 CET / 07:00 UTC  — daily scan          (com.sportsbrain.daily-scan)"
echo "  16:00 CET / 14:00 UTC  — closing odds AM      (com.sportsbrain.closing-odds)"
echo "  20:00 CET / 18:00 UTC  — closing odds PM      (com.sportsbrain.closing-odds-evening)"
echo ""
echo "Log files:"
echo "  $SPORTSBRAIN_DIR/results/scan_cron.log"
echo "  $SPORTSBRAIN_DIR/results/closing_odds_cron.log"
echo "  $SPORTSBRAIN_DIR/results/launchd_scan.log"
echo "  $SPORTSBRAIN_DIR/results/launchd_closing_odds.log"
echo "  $SPORTSBRAIN_DIR/results/launchd_closing_odds_evening.log"
echo ""
echo "To check agent status:  launchctl list | grep sportsbrain"
echo "To unload all agents:   launchctl unload ~/Library/LaunchAgents/com.sportsbrain.*.plist"
echo "To test scan now:       bash $SPORTSBRAIN_DIR/scripts/scan_cron.sh"
