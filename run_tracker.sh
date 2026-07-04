#!/bin/bash
# ============================================================
#   VINTED TRACKER — automated scheduled run (Phase 4.5, Mac/Linux)
# ============================================================
# Runs the sales tracker for every product in tracked_keywords.txt, unattended.
# Assumes Chrome is already running with the debugging port + logged-in Vinted
# profile (keep it running; see AUTOMATION.md). Schedule this with cron.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

export VINTED_AUTOMATED=1        # never block on a login prompt
export VINTED_TRACK_WORKERS=2    # gentle — steady-state has few new listings, so
                                 # low concurrency avoids Vinted's rate limiting

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
  sleep 30   # brief gap between products
done < "$KEYWORDS_FILE"
echo "[$(date)] === run complete ==="
