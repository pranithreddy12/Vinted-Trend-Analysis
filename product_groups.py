"""Group tracked listings into EXACT products (brand + product type), with colour/size as variants,
and rank products by VERIFIED sales. Read-only on the tracking CSV.

  python product_groups.py tracking/home.csv --days 30 --min-sold 3 --top 5 --out discovered_products.csv

Rule that prevents mixing: a listing joins a product only if it has a brand (the seller-entered
"Marque" from the catalog) AND at least one product-type word left after removing brand, colour,
size, condition and filler words. Anything else is 'unidentified' and is never counted in any
product's figures. A product is its brand + first 3 type words; colours and sizes are its variants.
Only status=sold rows (Vinted's item page said Sold) count as sales; disappeared never does.

Known limits: titles are free text and in several languages, so one real product can split
("veste"/"jacket") and two products with the same brand + type words can merge; image
recognition (Phase 5) is the later fix.
"""
import argparse, collections, csv, re, statistics
from datetime import datetime, timedelta, timezone

import track_sales as ts

_STOP = set("""de du la le les en et pour avec sans neuf neuve neufs tres très bon bonne etat état taille
un une des set lot di il per e da für von mit in the a an of and new nuevo nuova nuovo with size gr t l
femme homme woman women man men donna uomo mujer damen herren kinder enfant fille garçon original
originale tbe nwt nieuw zgan comme tag etiquette étiquette excellent parfait très-bon vintage
talla taglia maat größe grösse small medium large""".split())
_SIZE_RE = re.compile(r"^(xxs|xs|s|m|l|xl|xxl|xxxl|\d{1,3}(?:[.,]\d+)?(?:cm|mm|ml|cl|l|oz|kg|g)?)$")
_WORD_RE = re.compile(r"[a-zà-ÿ0-9]+")
# French/Italian/Spanish feminine or plural colour forms the shared COLOR_BUCKETS lacks
_EXTRA_COLORS = {"blanche": "white", "blanches": "white", "noire": "black", "noires": "black",
                 "bleue": "blue", "bleues": "blue", "verte": "green", "vertes": "green",
                 "grise": "grey", "grises": "grey", "rouges": "red", "violette": "purple",
                 "dorée": "gold", "dorées": "gold", "argentée": "silver", "marron": "brown",
                 "bianca": "white", "nera": "black", "rossa": "red", "azul": "blue", "negro": "black",
                 "blanco": "white", "rojo": "red", "verde": "green"}


def _colour_of(w):
    return ts.COLOR_BUCKETS.get(w) or _EXTRA_COLORS.get(w)


def _tokens(s: str) -> list:
    return _WORD_RE.findall((s or "").lower())


def product_key(row: dict):
    """(brand, type) or None when unidentified. See module docstring."""
    brand = (row.get("brand") or "").strip().lower()
    if not brand or brand in ("pas de marque", "no brand", "sans marque"):
        return None
    btoks = set(_tokens(brand))
    toks = [w for w in _tokens(row.get("title", ""))
            if w not in btoks and w not in _STOP and not _colour_of(w) and not _SIZE_RE.match(w)]
    toks = [w for w in toks if len(w) > 1]
    return (brand, " ".join(toks[:3])) if toks else None


def variant_of(row: dict) -> str:
    col = ts.normalize_color(row.get("color", "")) or next(
        (_colour_of(w) for w in _tokens(row.get("title", "")) if _colour_of(w)), "")
    size = (row.get("size") or "").strip()
    cap = next((c for c in (ts.canonical_capacity(t) for t in (row.get("title", "").lower().split())) if c), "")
    return " ".join(x for x in (col, size or cap) if x) or "-"


def verification_order(tracking: dict) -> list:
    """Disappeared-but-unverified listing ids, products with the MOST listings first (repeat
    products are the only ones that can ever be ranked), then newest disappearance first."""
    size = collections.Counter(k for k in (product_key(r) for r in tracking.values()) if k)
    ids = [i for i, r in tracking.items() if r["status"] == "disappeared"]
    return sorted(ids, key=lambda i: (-size.get(product_key(tracking[i]), 0),
                                      "".join(chr(255 - ord(c)) for c in tracking[i].get("disappeared_at", ""))))


def rank_products(rows, days=30, min_sold=3, now=None):
    now = now or max((ts._parse(r["last_seen"]) for r in rows if r.get("last_seen")),
                     default=datetime.now(timezone.utc))
    since = now - timedelta(days=days)
    groups, unidentified = collections.defaultdict(list), 0
    for r in rows:
        k = product_key(r)
        if k:
            groups[k].append(r)
        else:
            unidentified += 1
    out = []
    for (brand, typ), g in groups.items():
        sold = [r for r in g if r["status"] == "sold" and r.get("last_seen") and ts._parse(r["last_seen"]) >= since]
        if len(sold) < min_sold:
            continue
        prices = [float(r["price"]) for r in sold if r.get("price")]
        life = [v for v in (ts._lifespan_hours(r) for r in sold) if v is not None]
        out.append({
            "product": f"{brand} {typ}", "brand": brand, "type": typ,
            "confirmed_sold_30d": len(sold),
            "est_sales_per_month": round(len(sold) * 30 / days, 1),
            "median_sold_price": round(statistics.median(prices), 2) if prices else "",
            "median_hours_to_sell": round(statistics.median(life), 1) if life else "",
            "n_with_posting_date": len(life),
            "active_listings": sum(1 for r in g if r["status"] == "active"),
            "unverified_disappeared": sum(1 for r in g if r["status"] == "disappeared"),
            "variants": "; ".join(f"{v} x{n}" for v, n in collections.Counter(variant_of(r) for r in g).most_common(6)),
            "source_listings": " ".join("https://www.vinted.fr/items/" + r["id"] for r in sold[:3]),
        })
    out.sort(key=lambda p: (-p["confirmed_sold_30d"], p["active_listings"]))
    return out, len(groups), unidentified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-sold", type=int, default=3)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rows = [r for f in a.files for r in csv.DictReader(open(f, encoding="utf-8"))]
    prods, ngroups, unid = rank_products(rows, a.days, a.min_sold)
    print(f"{len(rows)} listings -> {ngroups} products with a brand + type; {unid} unidentified (not counted); "
          f"{len(prods)} products with >= {a.min_sold} confirmed sales in {a.days} days")
    for p in prods[: a.top]:
        print(f"\n{p['product']}: {p['confirmed_sold_30d']} confirmed sold · {p['active_listings']} active · "
              f"median €{p['median_sold_price']} · {p['median_hours_to_sell']} h to sell (n={p['n_with_posting_date']})"
              f"\n   variants: {p['variants']}\n   e.g. {p['source_listings']}")
    if a.out and prods:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(prods[0].keys()))
            w.writeheader(); w.writerows(prods)
        print("\nsaved", a.out)


if __name__ == "__main__":
    main()
