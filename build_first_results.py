"""Merge every opportunities_*.csv (+ confirmed sale prices from variant_report_*.csv) into one
ranking: first_results_ranking.csv. Run:  python build_first_results.py [top_n]"""
import csv, glob, os, sys

top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
rows = []
for f in glob.glob("opportunities_*.csv"):
    slug = f[len("opportunities_"):-len(".csv")]
    vr = {}
    p = f"variant_report_{slug}.csv"
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as fh:
            vr = {r["variant"]: r for r in csv.DictReader(fh)}
    with open(f, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            v = vr.get(r["variant"], {})
            rows.append({
                "tracked_list": slug, "product": r["product"],
                "discovery_score": r["discovery_score"], "est_sales_30d": r["est_sales_30d"],
                "median_days_to_sell": r["median_days_to_sell"], "asking_price_avg": r["avg_price"],
                "confirmed_sale_price_median": v.get("sold_median_price", ""),
                "confirmed_sales_count": v.get("sold_tracked", ""), "demand": r["demand_level"],
                "competition": f'{r["competition_level"]} ({r["competition"]})',
                "momentum_pct": r["momentum_pct"], "days_of_history": r["history_points"],
                "why": r["explanation"]})

if not rows:
    sys.exit("No opportunities_*.csv found here - run this from the folder containing them (src).")
rows.sort(key=lambda r: -float(r["discovery_score"] or 0))
rows = rows[:top_n]
with open("first_results_ranking.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print(f"Wrote first_results_ranking.csv ({len(rows)} rows) - open it in Excel.")
