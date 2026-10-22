"""Look up one product by name: 30-day sales, median price, competition, time to sell, sources, reliability.

  python lookup_product.py "adidas samba" --files client_data/women.csv
  python lookup_product.py "tupperware" --files "client_data/home.csv" --days 30

Same strict rule as the rankings (product_groups.identified_key): a listing joins a product only when
its brand, type/model, colour and size are identified. When several different products match the
name they are reported SEPARATELY, never added together. Listings that match the name but cannot be
identified are counted and shown, not used. Prices are the asking price at sale (Vinted does not
publish the price paid); time to sell is a lower bound for category-sweep files.
"""
import argparse, collections, csv, glob, statistics
from datetime import timedelta

import product_groups as pg
import track_sales as ts
from check_model_sales import reliability


def _stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def matches(query_tokens, title_words, variant):
    """Every query word must be found: a colour word (any language) in the listing's colour, a size or
    capacity in its variant, anything else in the title/product words (singular and plural alike)."""
    stems, vparts = {_stem(w) for w in title_words}, set(variant.split())
    for w in query_tokens:
        col = pg._colour_of(w)
        if not ((col and col in vparts) or w in vparts or _stem(w) in stems):
            return False
    return True


def lookup(query, files, days=30):
    rows = {}
    for pat in files:
        for f in glob.glob(pat):
            for r in csv.DictReader(open(f, encoding="utf-8")):
                rows.setdefault(r["id"], r)
    rows = list(rows.values())
    if not rows:
        return None
    stamps = [ts._parse(r[k]) for r in rows for k in ("first_seen", "last_seen") if r.get(k)]
    now = max(stamps)
    span = (now - min(stamps)).total_seconds() / 86400
    since = now - timedelta(days=days)
    q = [w for w in pg._tokens(query) if w not in pg._STOP]
    if not q:
        return []
    groups, unidentified = collections.defaultdict(list), 0
    for r in rows:
        k = pg.identified_key(r)
        words = pg._tokens(r.get("title", "") + " " + " ".join(k or ()))
        if not matches(q, words, pg.variant_of(r)):
            continue
        if k:
            groups[k].append(r)
        else:
            unidentified += 1
    out = []
    for k, g in groups.items():
        sold = [r for r in g if r["status"] == "sold" and r.get("last_seen") and ts._parse(r["last_seen"]) >= since]
        prices = [float(r["price"]) for r in sold if r.get("price")]
        life = [h for h in (ts._lifespan_hours(r) for r in sold) if h is not None]
        active = sum(1 for r in g if r["status"] == "active")
        level, why = reliability(len(sold), sum(1 for r in sold if r.get("category")), span, days)
        out.append({"product": " ".join(k), "sold": sold, "active": active, "listings": len(g),
                    "median_price": round(statistics.median(prices), 2) if prices else None,
                    "median_days": round(statistics.median(life) / 24, 1) if life else None, "n_timed": len(life),
                    "competition": ts.competition_label(active),
                    "variants": collections.Counter(pg.variant_of(r) for r in sold).most_common(6),
                    "reliability": level, "why": why})
    out.sort(key=lambda p: (-len(p["sold"]), p["active"]))
    return out, unidentified, span, now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query"); ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--days", type=int, default=30); ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args()
    res = lookup(a.query, a.files, a.days)
    if res is None:
        raise SystemExit(f"No data: no listing found in {a.files} (check the file path)")
    if res == []:
        raise SystemExit(f"'{a.query}' has no usable word (only words like 'de', 'la'): give a brand or product name")
    out, unid, span, now = res
    print(f"'{a.query}': data {now - timedelta(days=span):%Y-%m-%d} to {now:%Y-%m-%d} ({span:.1f} days); "
          f"{len(out)} distinct product(s) match; {unid} matching listings could not be identified and are NOT counted")
    if not out:
        print("No identified product matches: no data for this name (nothing is guessed).")
    if len(out) > 1:
        print("Several different products match this name; each is reported on its own, never added together.")
    for p in out[:a.top]:
        print(f"\n== {p['product']}: {len(p['sold'])} confirmed sold in {a.days} days · {p['active']} on sale now "
              f"({p['competition']} competition)"
              + (f" · median asking price at sale €{p['median_price']}" if p["median_price"] is not None else "")
              + (f" · median time to sell {p['median_days']} days (n={p['n_timed']}, lower bound)" if p["n_timed"] >= 5 else " · time to sell n/a (too few dated sales)"))
        print(f"   RELIABILITY: {p['reliability']} - {p['why']}")
        if p["variants"]:
            print("   variants: " + "; ".join(f"{v}: {n}" for v, n in p["variants"]))
        for r in sorted(p["sold"], key=lambda r: r["last_seen"])[:30]:
            print(f"   {r['last_seen'][:10]}  €{r['price']:>7}  {r['title'][:50]}  https://www.vinted.fr/items/{r['id']}")


if __name__ == "__main__":
    t = pg._tokens("adidas samba white")
    assert matches(t, pg._tokens("adidas shoes samba"), "white 37")        # colour is in the variant, not the title
    assert not matches(t, pg._tokens("adidas shoes samba"), "black 37")
    assert matches(pg._tokens("tasse"), pg._tokens("tasses"), "")           # singular finds plural
    assert matches(pg._tokens("blanc 37"), pg._tokens("adidas samba"), "white 37")   # French colour word
    main()
