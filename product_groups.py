"""Group tracked listings into EXACT products (brand + product type), with colour/size as variants,
and rank products by VERIFIED sales. Read-only on the tracking CSV.

  python product_groups.py tracking/home.csv --days 30 --min-sold 3 --top 5 --out discovered_products.csv

Rule that prevents mixing: a listing joins a product only if brand (the seller-entered "Marque"
from the catalog), product type, colour AND size/capacity are all identified. Anything else is
'unidentified' and is never counted in any product's figures (what the client was promised
2026-10-20). Placeholder brands ("Inconnu", "Pas de marque"...) are unidentified too.
A product is its brand + the first recognised type word, mapped to one canonical English type
across FR/EN/IT/ES/NL/DE ("veste"/"giacca"/"jacket" -> jacket); when no type word is recognised
it falls back to the first leftover word. Colours and sizes are its variants.
Only status=sold rows (Vinted's item page said Sold) count as sales; disappeared never does.

Time to sell from a CATEGORY sweep is a lower bound: the sweep sees only the newest ~960 listings
per domain, so last_seen is when a listing left that window, not when it sold. Watch-list
keywords follow a listing until it goes, so their time to sell is the real one.

Known limits: two different models of the same brand + type merge (e.g. two Zara jackets);
image recognition (Phase 5) is the later fix.
"""
import argparse, collections, csv, os, re, statistics
from datetime import datetime, timedelta, timezone

import track_sales as ts

_STOP = set("""de du la le les en et pour avec sans neuf neuve neufs tres très bon bonne etat état taille
un une des set lot di il per e da für von mit in the a an of and new nuevo nuova nuovo with size gr t l
femme homme woman women man men donna uomo mujer damen herren kinder enfant fille garçon original
originale tbe nwt nieuw zgan comme tag etiquette étiquette excellent parfait très-bon vintage
talla taglia maat größe grösse small medium large mini petit petite grand grande joli jolie superbe
magnifique""".split())
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


_NO_BRAND = {"pas de marque", "no brand", "sans marque", "inconnu", "inconnue", "unknown",
             "autre", "other", "sin marca", "senza marca", "geen merk", "keine marke",
             "fait main", "handmade", "hecho a mano", "fatto a mano",
             # category words older rows stored as "brand" (first-title-word guess)
             "accessories", "accessoires", "art"}
_TYPES = {
    "jacket": ("veste", "blouson", "blazer", "jacket", "giacca", "chaqueta", "jack", "jacke", "doudoune", "puffer"),
    "coat": ("manteau", "coat", "cappotto", "abrigo", "mantel", "trench"),
    "dress": ("robe", "dress", "vestito", "abito", "vestido", "jurk", "kleid"),
    "tshirt": ("tshirt", "tee", "maglietta", "camiseta"),
    "shirt": ("chemise", "chemisier", "blouse", "blusa", "bluse", "polo", "camicia", "camisa", "hemd", "overhemd"),
    "top": ("top", "haut", "body", "débardeur", "debardeur", "crop", "canotta", "bustier"),
    "sweater": ("pull", "pullover", "sweat", "sweatshirt", "hoodie", "maglione", "felpa", "maglia", "jersey",
                "sudadera", "trui", "cardigan", "gilet"),
    "jeans": ("jean", "jeans", "vaquero", "vaqueros", "denim"),
    "trousers": ("pantalon", "trousers", "pants", "pantaloni", "pantalone", "jogging", "pantalón", "broek", "hose", "legging", "leggings"),
    "skirt": ("jupe", "skirt", "gonna", "falda", "rok", "rock"),
    "shorts": ("short", "shorts", "bermuda"),
    "shoes": ("chaussure", "chaussures", "basket", "baskets", "sneaker", "sneakers", "ballerine", "scarpe", "zapatillas", "zapatos", "schoenen", "schuhe",
              "escarpins", "mocassins"),
    "boots": ("bottes", "bottines", "boots", "stivali", "botas", "laarzen", "stiefel"),
    "sandals": ("sandales", "sandals", "sandali", "sandalias"),
    "bag": ("sac", "bag", "borsa", "bolso", "tas", "tasche", "pochette", "handbag", "cabas"),
    "bottle": ("gourde", "bouteille", "bottle", "borraccia", "botella", "fles", "flasche", "tumbler"),
    "mug": ("tasse", "mug", "tazza", "taza", "beker", "becher", "gobelet"),
    "plush": ("peluche", "doudou", "plush", "knuffel", "kuscheltier"),
    "candle": ("bougie", "candle", "candela", "vela", "kaars", "kerze"),
    "vase": ("vase", "vaso", "jarrón", "jarron"),
    "lamp": ("lampe", "lamp", "lampada", "lámpara", "lampara"),
    "cushion": ("coussin", "cushion", "cuscino", "cojín", "cojin", "kussen", "kissen"),
    "frame": ("cadre", "frame", "cornice", "marco", "lijst", "rahmen"),
    "glass": ("verre", "glass", "bicchiere", "copa"),
    "box": ("boîte", "boite", "box", "scatola", "caja", "doos", "kiste", "coffret"),
    "bedding": ("parure", "couette", "drap", "biancheria", "bettwäsche", "dekbedovertrek"),
    "cover": ("housse", "funda", "cover", "fodera"),
    "rug": ("tapis", "tappeto", "alfombra", "rug", "teppich", "vloerkleed"),
    "poster": ("affiche", "poster", "tableau", "quadro", "cuadro"),
    "mould": ("moule", "stampo", "molde", "mould", "mold"),
    "plate": ("assiette", "plat", "plate", "piatto", "plato", "teller", "bord"),
    "curtain": ("rideau", "tenda", "cortina", "curtain", "gordijn", "vorhang"),
    "pencil case": ("trousse", "astuccio", "estuche"),
    "figurine": ("figura", "figurine", "statuette", "statue", "figur"),
    "belt": ("ceinture", "cintura", "cinturón", "cinturon", "belt", "gürtel", "riem"),
    "earrings": ("boucles", "orecchini", "pendientes", "earrings", "oorbellen", "ohrringe"),
    "necklace": ("collier", "collana", "collar", "necklace", "ketting", "halskette"),
    "jumpsuit": ("combinaison", "jumpsuit", "combi", "mono"),
    "cap": ("casquette", "cap", "gorra", "cappellino"),
    "sunglasses": ("lunettes", "sunglasses", "occhiali", "gafas"),
    "perfume": ("parfum", "profumo", "perfume"),
    "set": ("ensemble", "completo", "conjunto"),
}
_TYPE_OF = {w: t for t, ws in _TYPES.items() for w in ws}


def _type_of(w):
    """Canonical type of a title word; a trailing plural 's' is tried too ('assiettes')."""
    return _TYPE_OF.get(w) or (_TYPE_OF.get(w[:-1]) if len(w) > 3 and w.endswith("s") else None)


def product_key(row: dict):
    """(brand, canonical type) or None when unidentified. See module docstring."""
    brand = (row.get("brand") or "").strip().lower()
    if not brand or brand in _NO_BRAND or _type_of(brand) or brand in _STOP or _colour_of(brand):
        return None  # old rows guessed brand from the first title word ("Vintage", "Mug", "Blanc")
    btoks = set(_tokens(brand))
    title = (row.get("title") or "").lower().replace("t-shirt", "tshirt").replace("t shirt", "tshirt")
    toks = [w for w in _tokens(title)
            if w not in btoks and w not in _STOP and not _colour_of(w) and not _SIZE_RE.match(w)]
    toks = [w for w in toks if len(w) > 1]
    typ = next((t for t in map(_type_of, toks) if t), None)
    return (brand, typ or toks[0]) if toks else None


def _variant_parts(row: dict) -> tuple:
    """(colour, size or capacity); '' where not identified. Colour: catalog field, else title.
    Size: catalog 'Taille' (not the 'Autre' placeholder), else a capacity in the title."""
    col = ts.normalize_color(row.get("color", "")) or next(
        (_colour_of(w) for w in _tokens(row.get("title", "")) if _colour_of(w)), "")
    size = (row.get("size") or "").strip()
    if size.lower() in ("autre", "other", "altro", "otro", "andere"):
        size = ""
    title = re.sub(r"(\d)\s+(ml|cl|l|oz)\b", r"\1\2", (row.get("title") or "").lower())  # "500 ml" -> "500ml"
    cap = next((c for c in (ts.canonical_capacity(t) for t in title.split()) if c), "")
    return col, size or cap


def variant_of(row: dict) -> str:
    return " ".join(x for x in _variant_parts(row) if x) or "-"


def identified_key(row: dict):
    """product_key, but only when colour AND size/capacity are identified too; else None."""
    k = product_key(row)
    return k if k and all(_variant_parts(row)) else None


def verification_order(tracking: dict) -> list:
    """Disappeared-but-unverified listing ids, products with the MOST listings first (repeat
    products are the only ones that can ever be ranked), then newest disappearance first."""
    size = collections.Counter(k for k in (identified_key(r) for r in tracking.values()) if k)
    ids = [i for i, r in tracking.items() if r["status"] == "disappeared"]
    return sorted(ids, key=lambda i: (-size.get(identified_key(tracking[i]), 0),
                                      "".join(chr(255 - ord(c)) for c in tracking[i].get("disappeared_at", ""))))


def rank_products(rows, days=30, min_sold=3, now=None):
    now = now or max((ts._parse(r["last_seen"]) for r in rows if r.get("last_seen")),
                     default=datetime.now(timezone.utc))
    since = now - timedelta(days=days)
    groups, unidentified = collections.defaultdict(list), 0
    for r in rows:
        k = identified_key(r)
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
        active = sum(1 for r in g if r["status"] == "active")
        per_month = len(sold) * 30 / days
        median_days = (statistics.median(life) / 24) if life else None
        opp = ts.compute_variant_opportunity(per_month, median_days)
        comp = ts.competition_label(active)
        out.append({
            "product": f"{brand} {typ}", "brand": brand, "type": typ,
            "confirmed_sold_30d": len(sold),
            "est_sales_per_month": round(per_month, 1),
            "median_sold_price": round(statistics.median(prices), 2) if prices else "",
            "median_hours_to_sell": round(statistics.median(life), 1) if life else "",
            "n_with_posting_date": len(life),
            "active_listings": active,
            "median_days_to_sell": round(median_days, 1) if median_days is not None else "",
            "competition_level": comp,
            "score": opp["score"],
            "verdict": opp["verdict"],
            "passes_alert_rule": per_month >= 30 and comp != "High" and opp["score"] >= 70,
            "unverified_disappeared": sum(1 for r in g if r["status"] == "disappeared"),
            "variants": "; ".join(f"{v}: {n} sold" for v, n in collections.Counter(variant_of(r) for r in sold).most_common(6)),
            "source_listings": " ".join("https://www.vinted.fr/items/" + r["id"] for r in sold[:3]),
        })
    out.sort(key=lambda p: (-p["confirmed_sold_30d"], p["active_listings"]))
    return out, len(groups), unidentified


def _sweep_files(path="tracked_keywords.txt"):
    """tracking CSVs of the 'cat:<id> <label>' category sweeps in the watch-list (that exist)."""
    try:
        lines = [l.strip() for l in open(path, encoding="utf-8")]
    except OSError:
        return []
    labels = [l.split(None, 1)[1] for l in lines if l.startswith("cat:") and len(l.split(None, 1)) == 2]
    files = [os.path.join(ts.TRACK_DIR, ts._slug(lb) + ".csv") for lb in labels]
    return [f for f in files if os.path.exists(f)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-sold", type=int, default=3)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    files = a.files or _sweep_files()
    if not files:
        print("no category sweep files found (tracked_keywords.txt cat: lines / tracking/*.csv); nothing to do")
        return
    rows = [r for f in files for r in csv.DictReader(open(f, encoding="utf-8"))]
    prods, ngroups, unid = rank_products(rows, a.days, a.min_sold)
    print(f"{len(rows)} listings -> {ngroups} products (brand + type + colour + size identified); {unid} unidentified (not counted); "
          f"{len(prods)} products with >= {a.min_sold} confirmed sales in {a.days} days")
    for p in prods[: a.top]:
        print(f"\n{p['product']}: {p['confirmed_sold_30d']} confirmed sold · {p['active_listings']} active · "
              f"median €{p['median_sold_price']} · {p['median_hours_to_sell']} h to sell (n={p['n_with_posting_date']})"
              f" · competition {p['competition_level']} · score {p['score']}"
              f"{' · ALERT' if p['passes_alert_rule'] else ''}"
              f"\n   variants: {p['variants']}\n   e.g. {p['source_listings']}")
    print(f"\n{sum(p['passes_alert_rule'] for p in prods)} of {len(prods)} products pass the alert rule "
          "(>=30 confirmed sales/30d, competition not High, score >=70)")
    print("note: for category-sweep files, time to sell is a lower bound (see module docstring)")
    if a.out:  # written even when empty, so a quiet run still leaves a (header-only) file
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(prods[0].keys()) if prods else ["product"])
            w.writeheader(); w.writerows(prods)
        print("\nsaved", a.out)


if __name__ == "__main__":
    main()
