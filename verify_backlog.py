"""Confirm the sold status of a tracking CSV's unchecked 'disappeared' listings from THIS
machine's Chrome, so a client's files can be verified without his tracker running. The same
page visit reads brand, size, colour and Vinted's category, repairing rows recorded by older
versions (no size, brand guessed from the first title word).

  python verify_backlog.py client_data/women.csv --max 3000

Checks every disappeared listing (repeat products first, then newest) plus already-confirmed
sales that were never repaired; a confirmed sale stays a sale even if its page is gone now. The
CSV is updated in place and saved every 25 checks, so keep a copy of the original and re-run to
continue. Needs the tracker's Chrome running (remote debugging on port 9222). An old sold page
Vinted has deleted comes back 'removed': that lowers confirmed sales, it never invents one.
"""
import argparse
import os

import product_groups as pg
import track_sales as ts

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--max", type=int, default=3000, help="listings to check this run")
a = ap.parse_args()

tracking = ts.load_tracking(a.file)
# confirmed sales already tried once: a page Vinted has deleted can't be repaired, don't re-open it every pass
tried_path = a.file + ".repair_tried"
tried = set(open(tried_path).read().split()) if os.path.exists(tried_path) else set()
sold = [i for i, r in tracking.items() if r["status"] == "sold" and not r.get("category") and i not in tried]
ids = (sold + pg.verification_order(tracking))[: a.max]
print(f"{a.file}: {len(tracking)} listings, checking {len(ids)} "
      f"({min(len(sold), len(ids))} confirmed sales to repair, the rest unverified disappeared)")
ts.verify_disappearances(tracking, a.file, ids)
for i in sold:
    tracking[i]["status"], tracking[i]["sale_confirmed"] = "sold", True
ts.save_tracking(a.file, tracking)
with open(tried_path, "a") as f:
    f.write("".join(i + "\n" for i in sold if i in ids))
