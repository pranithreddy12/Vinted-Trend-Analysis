#!/bin/bash
# ============================================================
#   VINTED MARKET INTELLIGENCE — Mac launcher (Phase 3)
# ============================================================

echo "==============================="
echo "   VINTED MARKET INTELLIGENCE"
echo "==============================="

# Always work from the folder this script lives in — so it works no matter
# where you place the Vinted folder (no path editing needed).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_DATA_DIR="$SCRIPT_DIR/vinted_profile"

if [ ! -f "$CHROME" ]; then
  echo "[ERROR] Google Chrome was not found at:"
  echo "  $CHROME"
  echo "Please install Google Chrome, then run this again."
  exit 1
fi

# Chrome must be fully quit so we can relaunch it in debugging mode.
echo "Closing any open Chrome windows..."
pkill -i "Google Chrome"
sleep 2

# Launch Chrome with remote debugging on a dedicated Vinted profile.
"$CHROME" \
  --remote-debugging-port=9222 \
  --user-data-dir="$USER_DATA_DIR" \
  "https://www.vinted.fr" &

sleep 8

echo ""
echo "If Vinted asks you to log in, log in now (solve any captcha)."
echo "Then come back here and press ENTER to start..."
read -r

# Run the Phase 3 engine.
python3 fetch_results.py

echo ""
echo "Finished. Press ENTER to close..."
read -r
