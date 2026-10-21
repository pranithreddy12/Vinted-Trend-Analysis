"""Confirm the sold status of a tracking CSV's unchecked 'disappeared' listings from THIS
machine's Chrome, so a client's files can be verified without his tracker running. The same
page visit reads brand, size, colour and Vinted's category, repairing rows recorded by older
versions (no size, brand guessed from the first title word).

  python verify_backlog.py client_data/women.csv --max 3000 [--priority 'baskets|sneaker']

Checks every disappeared listing (repeat products first, then newest) plus already-confirmed
sales that were never repaired; a confirmed sale stays a sale even if its page is gone now. The
CSV is updated in place and saved every 25 checks, so keep a copy of the original and re-run to
continue. Needs the tracker's Chrome running (remote debugging on port 9222). An old sold page
Vinted has deleted comes back 'removed': that lowers confirmed sales, it never invents one.
"""
import argparse
import os
import re
import sys

import product_groups as pg
import track_sales as ts

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--max", type=int, default=3000, help="listings to check this run")
ap.add_argument("--priority", help="regex on the title: matching listings are checked first (the client's categories)")
a = ap.parse_args()

tracking = ts.load_tracking(a.file)
# confirmed sales stay sales: remember them before checking, and restore any an interrupted earlier
# run left marked otherwise (a re-opened sale whose page is gone is checkpointed as "removed" mid-run)
sold_path = a.file + ".sold_ids"
if os.path.exists(sold_path):
    for i in open(sold_path).read().split():
        if i in tracking and tracking[i]["status"] != "sold":
            tracking[i]["status"], tracking[i]["sale_confirmed"] = "sold", True
with open(sold_path, "w") as f:
    f.write("".join(i + "\n" for i, r in tracking.items() if r["status"] == "sold"))
# confirmed sales already tried once: a page Vinted has deleted can't be repaired, don't re-open it every pass
tried_path = a.file + ".repair_tried"
tried = set(open(tried_path).read().split()) if os.path.exists(tried_path) else set()
# also re-open checked sales of clothing/shoes still missing a size (read before the size_familiar fix)
needs_size = lambda r: not r.get("size") and any(w in (r.get("category_path") or "") for w in ("Vêtements", "Chaussures"))
sold = [i for i, r in tracking.items()
        if r["status"] == "sold" and (not r.get("category") or needs_size(r)) and i not in tried]
order = pg.verification_order(tracking)
if a.priority:
    rx = re.compile(a.priority, re.I)
    order = [i for i in order if rx.search(tracking[i]["title"])] + [i for i in order if not rx.search(tracking[i]["title"])]
ids = (sold + order)[: a.max]
print(f"{a.file}: {len(tracking)} listings, checking {len(ids)} "
      f"({min(len(sold), len(ids))} confirmed sales to repair, the rest unverified disappeared)")
ts.verify_disappearances(tracking, a.file, ids)
# only a page Vinted has really deleted is "tried"; one that didn't load (session expired, Chrome
# down) must be retried next pass
gone = [i for i in sold if i in ids and tracking[i]["status"] == "removed"]
for i in sold:
    tracking[i]["status"], tracking[i]["sale_confirmed"] = "sold", True
ts.save_tracking(a.file, tracking)
with open(tried_path, "a") as f:
    f.write("".join(i + "\n" for i in gone))
# A logged-out session can't read Sold: stop (exit 5) instead of grinding through pages it can't use.
if ts._LOGGED_OUT_PAGES[0] >= max(25, len(ids) // 5):
    print(f"STOP: {ts._LOGGED_OUT_PAGES[0]} of {len(ids)} pages were logged out - log in again in the tracker Chrome")
    sys.exit(5)
