#!/bin/bash
# ============================================================
#   VINTED TRACKER — automated scheduled run (Phase 4.5, Mac/Linux)
# ============================================================
# Runs the sales tracker for every product in tracked_keywords.txt, unattended.
#
# REQUIRES: Chrome already running with the debugging port + logged into Vinted,
# left OPEN. This script does NOT relaunch Chrome — relaunching starts a
# logged-out browser even though the cookies are on disk. Keep the Chrome window
# open on the dedicated machine. See AUTOMATION.md.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

export VINTED_AUTOMATED=1        # never block on a login prompt
export VINTED_TRACK_WORKERS=2    # gentle — avoids Vinted's rate limiting

# Confirm the logged-in debug-Chrome is up; if not, stop (don't corrupt anything).
if ! curl -s -m 3 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  echo "[$(date)] ERROR: Chrome is not running with the debugging port."
  echo "   Run start_scraper.sh, log into Vinted, and LEAVE Chrome open. Then retry."
  exit 1
fi

KEYWORDS_FILE="$SCRIPT_DIR/tracked_keywords.txt"
if [ ! -f "$KEYWORDS_FILE" ]; then
  echo "[$(date)] ERROR: tracked_keywords.txt not found"
  exit 1
fi

echo "[$(date)] === automated tracking run starting ==="
while IFS= read -r kw || [ -n "$kw" ]; do
  kw="$(echo "$kw" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -z "$kw" ] && continue
  case "$kw" in \#*) continue ;; esac
  echo "[$(date)] tracking: $kw"
  python3 track_sales.py "$kw"
  sleep 30
done < "$KEYWORDS_FILE"
echo "[$(date)] === run complete ==="
