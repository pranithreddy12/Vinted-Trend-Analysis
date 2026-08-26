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
# Cap per-run item-page work so ONE product with a big/first-time catalog can't turn a
# scheduled 6h-cadence run into a many-hour marathon (live-observed: a 1,551-item first-time
# enrichment alone ran well over an hour). Uncapped work resumes automatically on the NEXT
# run — nothing is lost, it just spreads across cycles instead of blocking everything after it.
export VINTED_MAX_ENRICH=300
export VINTED_MAX_VERIFY=300
# Cross-border tracking (Phase 4 delivery, 2026-07-08): your Vinted account's shipping
# zone — France plus the 9 countries buyers reach you from. FR-only tracking was
# missing ~45% of the listings actually visible in this zone. Remove domains here if
# you only want to track a subset.
export VINTED_DOMAINS="fr,be,lu,nl,de,at,es,pt,it,ie"

# Data reliability: verify a disappearance against the real Vinted page before counting it
# as a sale. ON by default — no extra cost (just item-page loads, same as publish-time
# enrichment), and it fixed a ~10x sale overcount found on live data.
export VINTED_VERIFY_SOLD=1
# Opportunity ranking + alerts (Phase 6): pure computation on data already collected, no
# extra cost. ON by default so opportunities_<slug>.csv builds up from the first run.
export VINTED_DISCOVER=1

# ---- Optional AI features (OFF by default — each spends your own Anthropic budget) ----
# Cleared explicitly (not left unset) so a stray export/setx from earlier manual testing on
# this machine can never silently turn AI spend on in an automated run.
unset VINTED_VISION VINTED_VISION_PROVIDER VINTED_DEDUP VINTED_REFERENCE
# Uncomment to enable during continuous collection. Cost scales with NEW distinct products,
# not total listings, and VINTED_DEDUP cuts it further by identifying each product once.
# export VINTED_VISION=1                    # AI product identification (Stage A)
# export VINTED_VISION_PROVIDER=anthropic   # (needs ANTHROPIC_API_KEY in the environment)
# export VINTED_DEDUP=1                     # reuse each product across sellers — big AI-cost cut
# export VINTED_REFERENCE=1                 # reference lookup for generic items (Stage B)

# Confirm the logged-in debug-Chrome is up. If it is not — live-tested 2026-08-25/26: Chrome
# can die or get closed (even by accident) partway through a run, and without a per-product
# recheck every remaining product failed instantly with no recovery attempt for the rest of
# the run. Try ONE relaunch on the same profile before giving up (a clean relaunch — no pkill
# of other Chrome windows — preserves the saved login: verified live). Returns 0 if Chrome is
# up (after relaunching if needed), 1 if it's still unreachable.
ensure_chrome() {
  curl -s -m 3 http://127.0.0.1:9222/json/version >/dev/null 2>&1 && return 0
  echo "[$(date)] Chrome debug port unreachable — attempting one relaunch..."
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "$CHROME" --remote-debugging-port=9222 --user-data-dir="$SCRIPT_DIR/vinted_profile" "https://www.vinted.fr" >/dev/null 2>&1 &
  sleep 12
  if curl -s -m 3 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    echo "[$(date)] Chrome relaunched successfully — continuing."
    return 0
  fi
  return 1
}

if ! ensure_chrome; then
  echo "[$(date)] ERROR: Chrome still not running with the debugging port after a relaunch attempt."
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

  # Re-check Chrome before EVERY product, not just once at the top (see ensure_chrome above) —
  # a single closure/crash now costs at most one product instead of the whole rest of the run.
  if ! ensure_chrome; then
    echo "[$(date)] Chrome still unreachable — skipping \"$kw\" this run."
    sleep 30
    continue
  fi

  if [ "${kw#cat:}" != "$kw" ]; then
    # Seedless category sweep (Phase 6 Layer 1): "cat:<id> <label>" collects a WHOLE category
    # with no search keyword, feeding the same tracking/history pipeline.
    rest="${kw#cat:}"
    cat_id="${rest%%[[:space:]]*}"
    cat_name="$(printf '%s' "$rest" | sed 's/^[^[:space:]]*[[:space:]]*//')"
    [ -z "$cat_name" ] && cat_name="category $cat_id"
    echo "[$(date)] sweeping category: $cat_id ($cat_name)"
    VINTED_CATALOG_ID="$cat_id" VINTED_CATEGORY_NAME="$cat_name" python3 track_sales.py
  else
    echo "[$(date)] tracking: $kw"
    python3 track_sales.py "$kw"
  fi
  sleep 30
done < "$KEYWORDS_FILE"
echo "[$(date)] === run complete ==="
