"""Confirmed sales for exact products, straight from tracking/*.csv (read-only except the log).

One product:
  python check_model_sales.py --files tracking/smeg_tumbler.csv --brand smeg --color cream --cap 0.5l --hours 48
List of products (any number, e.g. the whole watch-list), results appended to model_checks_log.csv
so each re-run shows the evolution:
  python check_model_sales.py --list model_checks.txt --hours 168

model_checks.txt, one product per line, '#' comments allowed, fields separated by ';':
  name ; tracking glob ; brand ; colour ; capacity ; line
  Stanley Quencher H2.0 40oz pink ; tracking/stanley_*.csv ; stanley ; pink ; 40oz ; Quencher
  Smeg cream 500ml ; tracking/smeg_tumbler.csv ; smeg ; cream ; 0.5l ;

Counts rows Vinted confirmed as Sold (status=sold) whose last_seen (~ time of sale) is within
--hours, with the LISTING price they were last seen at. Vinted does not publish the final price
paid, so this is asking price at sale, not a realised price.
"""
import argparse, csv, glob, os, statistics
from datetime import datetime, timedelta, timezone

import fetch_results as fr
import track_sales as ts

LOG = "model_checks_log.csv"


def reliability(sold, verified, span_days, window_days):
    """(level, why): how far a sales figure can be trusted. Low = too few sales, or the data does not
    cover the window; High = 20+ sales, window covered and 80%+ of sales read on the item page."""
    if span_days < window_days:
        return "Low", f"data covers only {span_days:.0f} days of the {window_days:.0f}-day window: the figure is a minimum"
    if sold < 5:
        return "Low", f"only {sold} confirmed sales"
    if sold < 20 or verified < 0.8 * sold:
        return "Medium", f"{sold} confirmed sales, {verified} read on the item page"
    return "High", f"{sold} confirmed sales, {verified} read on the item page, window covered"


def check(files, brand, color="", cap="", line="", hours=48):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows, seen, stamps = [], set(), []  # stamps: every sighting in the files, not only this product's
    for f in glob.glob(files):
        for r in csv.DictReader(open(f, encoding="utf-8")):
            stamps += [ts._parse(r[k]) for k in ("first_seen", "last_seen") if r.get(k)]
            if r["id"] in seen or brand.lower() not in (r["title"] + " " + r.get("brand", "")).lower():
                continue
            seen.add(r["id"])
            if fr.is_off_product_type(r["title"], f"{brand} tumbler", r.get("category", "")):
                continue  # e.g. a Smeg kettle in a Smeg bottle file (no-op for other brands)
            # A row checked on its Vinted page (has a category) has the real colour/size: rebuild the
            # variant from them; the stored one came from the title by older code.
            v = ((r.get("variant") or "").lower() if not r.get("category") else "") or ts.build_variant(
                r["title"], r.get("brand", ""), r.get("color", ""), r.get("size", "")).lower()
            if (cap and cap.lower() not in v) or (color and ts.normalize_color(color) not in v):
                continue
            if line and ts.detect_model(r["title"]).lower() != line.lower():
                continue
            rows.append((r, v))
    sold = [(r, v) for r, v in rows if r["status"] == "sold" and r["last_seen"]
            and ts._parse(r["last_seen"]) >= since]
    prices = [float(r["price"]) for r, _ in sold if r.get("price")]
    life = [h for h in (ts._lifespan_hours(r) for r, _ in sold) if h is not None]
    span = (max(stamps) - min(stamps)).total_seconds() / 86400 if stamps else 0
    level, why = reliability(len(sold), sum(1 for r, _ in sold if r.get("category")), span, hours / 24)
    return {
        "span_days": span, "reliability": level, "why": why,
        "tracked": len(rows), "active": sum(1 for r, _ in rows if r["status"] == "active"),
        "sold": len(sold), "median_price": round(statistics.median(prices), 2) if prices else "",
        "min_price": min(prices) if prices else "", "max_price": max(prices) if prices else "",
        "median_hours_to_sell": round(statistics.median(life), 1) if life else "", "n_timed": len(life),
        "sold_rows": sold,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", help="file with one product per line (see module docstring)")
    ap.add_argument("--files"); ap.add_argument("--brand")
    ap.add_argument("--color", default=""); ap.add_argument("--cap", default="")
    ap.add_argument("--line", default="", help="product line, e.g. Quencher")
    ap.add_argument("--hours", type=float, default=48)
    a = ap.parse_args()

    jobs = []
    if a.list:
        for ln in open(a.list, encoding="utf-8"):
            p = [x.strip() for x in ln.split(";")]
            if ln.strip() and not ln.lstrip().startswith("#") and len(p) >= 3:
                p += [""] * (6 - len(p))
                jobs.append((p[0], p[1], p[2], p[3], p[4], p[5]))
    elif a.files and a.brand:
        jobs.append((f"{a.brand} {a.cap} {a.color}".strip(), a.files, a.brand, a.color, a.cap, a.line))
    else:
        ap.error("give --list, or --files and --brand")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    new_log = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as lf:
        w = csv.writer(lf)
        if new_log:
            w.writerow(["checked_at_utc", "product", "window_hours", "tracked", "active_now",
                        "confirmed_sold", "median_price", "min_price", "max_price", "median_hours_to_sell"])
        for name, files, brand, color, cap, line in jobs:
            c = check(files, brand, color, cap, line, a.hours)
            print(f"{name}: {c['tracked']} tracked · {c['active']} active · "
                  f"{c['sold']} confirmed sold in {a.hours:g}h"
                  + (f" · listing price median €{c['median_price']} (€{c['min_price']}–€{c['max_price']})"
                     if c["sold"] else "")
                  + (f" · median time to sell {c['median_hours_to_sell']} h ({c['median_hours_to_sell'] / 24:.1f} days,"
                     f" {c['n_timed']} sales with a posting date)" if c["n_timed"] else ""))
            print(f"    RELIABILITY: {c['reliability']} - {c['why']} (data {c['span_days']:.1f} days; sales are the "
                  f"asking price at sale; time to sell is a lower bound for category-sweep files)")
            for r, v in sorted(c["sold_rows"], key=lambda x: x[0]["last_seen"]):
                print(f"    {r['last_seen']}  €{r['price']:>7}  {v or '-':<12} {r['title'][:55]}  https://www.vinted.fr/items/{r['id']}")
            w.writerow([stamp, name, a.hours, c["tracked"], c["active"], c["sold"],
                        c["median_price"], c["min_price"], c["max_price"], c["median_hours_to_sell"]])


if __name__ == "__main__":
    assert reliability(30, 28, 25, 30)[0] == "Low"      # data shorter than the window
    assert reliability(3, 3, 40, 30)[0] == "Low"        # too few sales
    assert reliability(12, 12, 40, 30)[0] == "Medium"
    assert reliability(25, 10, 40, 30)[0] == "Medium"   # most sales not read on the page
    assert reliability(25, 24, 40, 30)[0] == "High"
    main()
