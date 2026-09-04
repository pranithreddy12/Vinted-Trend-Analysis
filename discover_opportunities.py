"""
Phase 6 Layer 2 — opportunity detection over accumulated history.

Layer 1 (seedless category sweeps in track_sales) collects the data; this layer reads the
per-variant daily history and surfaces the HIDDEN OPPORTUNITIES — products that are in demand,
fast-moving, low-competition, AND rising. "Rising" is the new signal: momentum measured from the
`variants_<slug>_<date>.csv` snapshot series, so a product accelerating off a low base ranks above
a big-but-flat one — catching winners early, which is the whole point of autonomous discovery.

HONEST on data: momentum needs ≥2 daily snapshots and gets more reliable the longer history runs.
With one run it's 0 for everything and ranking falls back to the proven demand/velocity/competition
score — still useful, just not yet "rising-aware". It strengthens automatically as Layer 1 collects.
"""

import os
import csv


def snapshot_series(slug: str, max_days: int = 21) -> dict:
    """{variant: [(date, est_sales_30d), …]} in chronological order, from this product/category's
    daily snapshots. Scoped by slug so categories/products never cross-contaminate."""
    import track_sales as ts
    d = ts.TRACK_DIR
    if not os.path.isdir(d):
        return {}
    # Strictly this product's own snapshots, ordered by real date. ts.snapshot_date rejects a
    # different product whose slug merely starts with this one (e.g. slug "stanley_quencher"
    # must NOT absorb "stanley_quencher_rose") — that mixed two products into one series and
    # produced a bogus "+200% rising" on every variant.
    dated = sorted((dt, fn) for fn in os.listdir(d)
                   if (dt := ts.snapshot_date(fn, slug)))[-max_days:]
    series: dict = {}
    for date, fn in dated:
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        series.setdefault(row["variant"], []).append(
                            (date, float(row["est_sales_30d"])))
                    except (ValueError, KeyError):
                        pass
        except Exception:
            pass
    return series


def momentum(series: list) -> float:
    """Relative growth across the snapshot window: least-squares slope of est-sales over time,
    normalised by the mean level → e.g. 0.4 ≈ +40% trend across the window, -0.3 ≈ cooling.
    Returns 0.0 when there's too little history (<2 points) or a flat/zero series."""
    n = len(series)
    if n < 2:
        return 0.0
    ys = [e for _, e in series]
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if not var or not my:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (cov / var) * (n - 1) / my


def discovery_score(variant: dict, mom: float) -> int:
    """Blend the proven demand-first opportunity score (0–100) with rising momentum. Only upward
    momentum is rewarded (we're hunting emerging winners, not fading ones): base carries 70,
    momentum up to 30 → 0–100. Flat/unknown history ⇒ falls back to the base ranking."""
    base = variant.get("score") or 0
    boost = min(max(mom, 0.0), 1.0)
    return round(0.7 * base + 30 * boost)


def _reason(v: dict, mom_pct: int) -> str:
    bits = []
    if mom_pct >= 10:
        bits.append(f"rising +{mom_pct}%")
    elif mom_pct <= -10:
        bits.append(f"cooling {mom_pct}%")
    if v.get("demand_level"):
        bits.append(f"{v['demand_level']} demand")
    md = v.get("median_days_to_sell")
    if md:
        bits.append(f"~{md}d to sell")
    if v.get("competition_level"):
        bits.append(f"{str(v['competition_level']).lower()} competition")
    return " · ".join(bits)


def rank_opportunities(variants: list, slug: str = "", top_n: int = 20,
                       max_days: int = 21) -> list:
    """Attach momentum + discovery_score + a human reason to each variant and return them ranked
    best-first. `variants` is a variant_analysis() result; history comes from the snapshots."""
    series = snapshot_series(slug, max_days)
    ranked = []
    for v in variants:
        pts = series.get(v.get("variant", ""), [])
        mom = momentum(pts)
        out = dict(v)
        # Cap the DISPLAYED percentage. Live-observed: a variant with two near-zero snapshots
        # then its first real sale computes a mathematically valid but absurd slope ("0, 0, 8.7"
        # -> +300%) because dividing by a near-zero mean amplifies tiny absolute moves into huge
        # percentages. That's not real week-over-week growth, it's noise from a small sample
        # crossing zero - showing "+300%" to the client would read as explosive demand when it's
        # really just the first data point after a near-empty ramp-up. discovery_score's boost
        # was already clamped to +/-100%; the number we PRINT needed the same clamp.
        out["momentum_pct"] = round(max(-100.0, min(100.0, mom * 100)))
        out["history_points"] = len(pts)
        out["discovery_score"] = discovery_score(v, mom)
        out["reason"] = _reason(out, out["momentum_pct"])
        out["explanation"] = "; ".join(explain(out))   # data-grounded "why", for the report
        ranked.append(out)
    ranked.sort(key=lambda x: (x["discovery_score"], x.get("est_sales_30d", 0)), reverse=True)
    return ranked[:top_n] if top_n else ranked


def save_opportunities_report(ranked: list, slug: str = "") -> str:
    path = f"opportunities_{slug}.csv" if slug else "opportunities.csv"
    fields = ["rank", "variant", "product", "ai_product", "discovery_score", "momentum_pct",
              "history_points", "est_sales_30d", "demand_level", "median_days_to_sell",
              "competition", "competition_level", "trend", "avg_price", "explanation"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i, v in enumerate(ranked, 1):
            row = {k: v.get(k, "") for k in fields}
            row["rank"] = i
            w.writerow(row)
    return path


def print_top(ranked: list, n: int = 10) -> None:
    print("\n🔥 TOP OPPORTUNITIES (discovery-ranked)")
    print("─" * 64)
    for i, v in enumerate(ranked[:n], 1):
        name = v.get("ai_product") or v.get("product") or v.get("variant")
        print(f"{i:2}. [{v['discovery_score']:3}] {name}  — {v['reason']}")
    if ranked and all(v.get("history_points", 0) < 2 for v in ranked):
        print("  ⏳ momentum builds as daily history accumulates (needs ≥2 days of snapshots)")


def explain(v: dict) -> list:
    """Plain-language reasons a product is an opportunity, grounded in the collected data — the
    "why" the client wants instead of a bare score (e.g. "sales up 45% recently", "~100
    sales/month", "typically sells in ~24h", "low competition"). Data-driven, not AI-written
    (a richer AI narrative is the AI-Insights phase); every line comes straight from a metric."""
    bits = []
    mom = v.get("momentum_pct", 0)
    if mom >= 10:
        bits.append(f"sales up {mom}% recently")
    elif mom <= -10:
        bits.append(f"sales cooling {abs(mom)}% recently")
    est = v.get("est_sales_30d")
    if est:
        bits.append(f"~{round(est)} sales/month")
    md = v.get("median_days_to_sell")
    if md:
        bits.append(f"typically sells in ~{round(md * 24)}h" if md < 2
                    else f"typically sells in ~{md} days")
    cl = v.get("competition_level")
    if cl:
        n = v.get("competition")
        bits.append(f"{str(cl).lower()} competition" + (f" ({n} listings)" if n else ""))
    off = v.get("offers")
    if off:
        bits.append(f"{off} recent buyer offers")
    return bits


def alerts(ranked: list, min_score: int = 70, min_confirmed_sales: int = 30) -> list:
    """Smart alerts — the actionable subset of the ranking: products worth buying to resell NOW.
    An item alerts when its discovery score clears the bar, it has real demand, it isn't already
    saturated, AND demand has been sufficiently DEMONSTRATED — not just a lucky handful of sales.

    Client feedback (2026-09-04): "30 confirmed recent sales per month (rather than 30 in
    total); this is an additional rule." Supersedes the original 2026-08-22 ask (40 confirmed
    sales, no time window) — min_confirmed_sales is now a floor on est_sales_30d (VERIFIED
    confirmed sales in the trailing 30 days, per the 2026-09-04 scoring fix — not a cumulative
    since-tracking-began count, and not an extrapolated rate). A product that sold 40 times over
    6 months but has gone quiet no longer clears this; a product with 30+ real sales in the last
    30 days does.

    Tagged RISING when momentum is strong, else HOT. (True 'brand-new product' novelty needs a
    first-seen set, a small add once history exists — see Phase 6 notes.)"""
    out = []
    for v in ranked:
        strong_demand = v.get("demand_level") in ("High", "Medium")
        not_saturated = str(v.get("competition_level", "")).lower() in ("low", "medium", "")
        demonstrated = v.get("est_sales_30d", 0) >= min_confirmed_sales
        if (v.get("discovery_score", 0) >= min_score and strong_demand and not_saturated
                and demonstrated):
            out.append({**v, "alert": "RISING" if v.get("momentum_pct", 0) >= 30 else "HOT"})
    return out


def print_alerts(alerted: list) -> None:
    if not alerted:
        return
    print("\n🔔 SMART ALERTS — why these products deserve attention")
    print("─" * 64)
    for v in alerted:
        name = v.get("ai_product") or v.get("product") or v.get("variant")
        print(f"  [{v['alert']}] {name}")
        for line in explain(v):
            print(f"       • {line}")


def _demo() -> None:
    """Self-check (no I/O): momentum direction + magnitude, and momentum lifting the rank."""
    assert momentum([("d1", 10), ("d2", 20), ("d3", 30)]) > 0.5, "rising series"
    assert momentum([("d1", 30), ("d2", 20), ("d3", 10)]) < 0, "declining series"
    assert momentum([("d1", 20), ("d2", 20)]) == 0.0, "flat series"
    assert momentum([("d1", 20)]) == 0.0, "single point = unknown"
    # REGRESSION: live-observed "0, 0, 8.7" series computed +300% momentum — mathematically
    # valid (dividing by a near-zero mean amplifies a tiny move) but absurd to SHOW a client as
    # "rising +300%" when it's really just the first sale after a near-empty ramp-up. The raw
    # momentum() value can still be large (only the displayed % is capped, in rank_opportunities);
    # this asserts the underlying pathology is real so the cap has something to guard against.
    assert momentum([("d1", 0.0), ("d2", 0.0), ("d3", 8.7)]) > 2.0, "near-zero-baseline pathology"
    _orig_series = snapshot_series
    globals()["snapshot_series"] = lambda slug, max_days=21: {
        "x": [("d1", 0.0), ("d2", 0.0), ("d3", 8.7)]}
    try:
        ranked = rank_opportunities(
            [{"variant": "x", "score": 10, "est_sales_30d": 8.7}], slug="whatever")
        assert ranked[0]["momentum_pct"] == 100, \
            f"display must be capped at 100, got {ranked[0]['momentum_pct']}"
    finally:
        globals()["snapshot_series"] = _orig_series
    v = {"score": 50}
    assert discovery_score(v, 0.5) > discovery_score(v, 0.0), "rising must out-score flat"
    assert discovery_score({"score": 100}, 0.0) == 70, "flat base weight"
    r = _reason({"demand_level": "High", "median_days_to_sell": 3,
                 "competition_level": "Low"}, 40)
    assert "rising +40%" in r and "low competition" in r.lower(), r
    # Alerts: a high-score, in-demand, un-saturated, sufficiently-DEMONSTRATED item fires
    # (RISING when momentum strong); a saturated one does not.
    hot = {"discovery_score": 80, "demand_level": "High", "competition_level": "Low",
           "momentum_pct": 45, "product": "x", "reason": "", "est_sales_30d": 35}
    sat = {"discovery_score": 80, "demand_level": "High", "competition_level": "High",
           "momentum_pct": 45, "product": "y", "reason": "", "est_sales_30d": 35}
    al = alerts([hot, sat])
    assert len(al) == 1 and al[0]["alert"] == "RISING", al
    assert alerts([sat]) == [], "saturated item must not alert"
    # Client feedback (2026-09-04): "30 confirmed recent sales per month (rather than 30 in
    # total)" — the floor must read the trailing-30-day figure, not a lifetime total. A variant
    # with a huge cumulative history (sold_tracked) but few sales in the last 30 days must NOT
    # clear it.
    flukey = {**hot, "est_sales_30d": 2, "sold_tracked": 500}
    assert alerts([flukey]) == [], "a big lifetime total but few RECENT sales must not clear the floor"
    assert alerts([flukey], min_confirmed_sales=2) == [flukey | {"alert": "RISING"}], \
        "the floor must be the thing gating it, not something else"
    # explain(): plain-language, data-grounded reasons matching the client's examples.
    ex = explain({"momentum_pct": 45, "est_sales_30d": 100, "median_days_to_sell": 1,
                  "competition_level": "Low", "competition": 12})
    assert "sales up 45% recently" in ex and "~100 sales/month" in ex, ex
    assert "typically sells in ~24h" in ex, ex          # <2 days rendered in hours
    # REGRESSION: a slug must never absorb another product whose slug starts with it.
    # "stanley_quencher" once swallowed "stanley_quencher_rose" snapshots, mixing two products
    # into one series and faking "+200% rising" on every variant.
    import track_sales as ts
    assert ts.snapshot_date("variants_stanley_quencher_rose_2026-07-31.csv",
                            "stanley_quencher") is None, "slug prefix collision must be rejected"
    assert ts.snapshot_date("variants_stanley_quencher_2026-08-22.csv",
                            "stanley_quencher") == "2026-08-22"
    assert ts.snapshot_date("variants_2026-07-04.csv", "") == "2026-07-04"      # legacy
    assert ts.snapshot_date("variants_stanley_quencher_2026-08-22.csv", "") is None
    print("discover_opportunities self-check OK:", ex)


if __name__ == "__main__":
    _demo()
