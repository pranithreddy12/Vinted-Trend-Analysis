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
    prefix = f"variants_{slug}_" if slug else "variants_"
    files = sorted(
        fn for fn in os.listdir(d)
        if fn.startswith(prefix) and fn.endswith(".csv")
        # without a slug, don't sweep up other products' namespaced snapshots
        and (slug or fn[len("variants_"):-len(".csv")].count("_") == 0)
    )[-max_days:]
    series: dict = {}
    for fn in files:
        date = fn[len(prefix):-len(".csv")]
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
        out["momentum_pct"] = round(mom * 100)
        out["history_points"] = len(pts)
        out["discovery_score"] = discovery_score(v, mom)
        out["reason"] = _reason(out, out["momentum_pct"])
        ranked.append(out)
    ranked.sort(key=lambda x: (x["discovery_score"], x.get("est_sales_30d", 0)), reverse=True)
    return ranked[:top_n] if top_n else ranked


def save_opportunities_report(ranked: list, slug: str = "") -> str:
    path = f"opportunities_{slug}.csv" if slug else "opportunities.csv"
    fields = ["rank", "variant", "product", "ai_product", "discovery_score", "momentum_pct",
              "history_points", "est_sales_30d", "demand_level", "median_days_to_sell",
              "competition", "competition_level", "trend", "avg_price", "reason"]
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


def _demo() -> None:
    """Self-check (no I/O): momentum direction + magnitude, and momentum lifting the rank."""
    assert momentum([("d1", 10), ("d2", 20), ("d3", 30)]) > 0.5, "rising series"
    assert momentum([("d1", 30), ("d2", 20), ("d3", 10)]) < 0, "declining series"
    assert momentum([("d1", 20), ("d2", 20)]) == 0.0, "flat series"
    assert momentum([("d1", 20)]) == 0.0, "single point = unknown"
    v = {"score": 50}
    assert discovery_score(v, 0.5) > discovery_score(v, 0.0), "rising must out-score flat"
    assert discovery_score({"score": 100}, 0.0) == 70, "flat base weight"
    r = _reason({"demand_level": "High", "median_days_to_sell": 3,
                 "competition_level": "Low"}, 40)
    assert "rising +40%" in r and "low competition" in r.lower(), r
    print("discover_opportunities self-check OK:", repr(r))


if __name__ == "__main__":
    _demo()
