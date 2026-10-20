"""Confirm the sold status of a tracking CSV's unchecked 'disappeared' listings from THIS
machine's Chrome, so a client's files can be verified without his tracker running.

  python verify_backlog.py client_data/women.csv --max 2000
  python verify_backlog.py client_data/smeg_tumbler.csv --all     # watch-list files (check_models)

Only identified products (brand + type + colour + size, see product_groups.py) are checked,
those with the most listings first: unidentified listings are never counted anyway. --all checks
every disappeared listing instead (for watch-list files, read by check_model_sales.py). The CSV is
updated in place and saved every 25 checks, so keep a copy of the original and re-run to
continue. Needs the tracker's Chrome running (remote debugging on port 9222). An old sold page
Vinted has deleted comes back 'removed': that lowers confirmed sales, it never invents one.
"""
import argparse

import product_groups as pg
import track_sales as ts

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--max", type=int, default=2000, help="listings to check this run")
ap.add_argument("--all", action="store_true", help="also unidentified listings (watch-list files)")
a = ap.parse_args()

tracking = ts.load_tracking(a.file)
ids = [i for i in pg.verification_order(tracking) if a.all or pg.identified_key(tracking[i])][: a.max]
print(f"{a.file}: {len(tracking)} listings, checking {len(ids)} unverified disappeared ones")
ts.verify_disappearances(tracking, a.file, ids)
