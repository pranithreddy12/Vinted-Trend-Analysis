#!/bin/bash
# ============================================================
#   VINTED TRACKER — automated scheduled run (Phase 4.5, Mac/Linux)
# ============================================================
# Runs the sales tracker for every product in tracked_keywords.txt, unattended.
# Auto-launches Chrome with the debugging port if it isn't already running.
# Schedule with cron. See AUTOMATION.md.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

export VINTED_AUTOMATED=1        # never block on a login prompt
export VINTED_TRACK_WORKERS=2    # gentle — avoids Vinted's rate limiting

# Ensure Chrome is up with the debugging port; launch it if not reachable.
if ! curl -s -m 3 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  echo "[$(date)] Chrome/CDP not running — launching..."
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "$CHROME" --remote-debugging-port=9222 \
    --user-data-dir="$SCRIPT_DIR/vinted_profile" \
    "https://www.vinted.fr" >/dev/null 2>&1 &
  sleep 12
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
