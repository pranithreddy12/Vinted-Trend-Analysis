"""Group tracked listings into EXACT products (brand + product type), with colour/size as variants,
and rank products by VERIFIED sales. Read-only on the tracking CSV.

  python product_groups.py tracking/home.csv --days 30 --min-sold 3 --top 5 --out discovered_products.csv

Rule that prevents mixing: a listing joins a product only when it is fully identified for its
family (Vinted's category path decides the family); anything else is 'unidentified' and is never
counted in any product's figures (what the client was promised 2026-10-20). All families need a
real brand (the seller-entered "Marque"); placeholder brands ("Inconnu", "Pas de marque"...) are
unidentified too. shoes: brand + model (first two title words that are not type/colour/size words)
with colour AND size. face care: brand + type + product name, with a volume ("50 ml"). pans: brand
with a diameter ("28 cm"). kitchenware: brand + type. everything else (clothing...): brand + type
with colour AND size/capacity.
The type comes from Vinted's own category when the row has one
(read from the item page during sold verification, e.g. "Chaussures de course" -> shoes); else
from the title: the first recognised type word, mapped to one canonical English type
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

import fetch_results as fr
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
                 "blanco": "white", "rojo": "red", "verde": "green",
                 "negra": "black", "negras": "black", "negros": "black", "blanca": "white", "blancas": "white",
                 "blancos": "white", "roja": "red", "rojas": "red", "rojos": "red", "azules": "blue", "verdes": "green",
                 "grises": "grey", "nere": "black", "neri": "black", "bianche": "white", "bianchi": "white",
                 "rosse": "red", "rossi": "red", "schwarz": "black", "schwarze": "black", "weiss": "white",
                 "weiße": "white", "zwart": "black", "zwarte": "black", "wit": "white", "witte": "white"}


def _colour_of(w):
    return ts.COLOR_BUCKETS.get(w) or _EXTRA_COLORS.get(w)


def _tokens(s: str) -> list:
    return _WORD_RE.findall((s or "").lower())


_NO_BRAND = {"pas de marque", "no brand", "sans marque", "inconnu", "inconnue", "unknown",
             "autre", "other", "sin marca", "senza marca", "geen merk", "keine marke",
             "fait main", "handmade", "hecho a mano", "fatto a mano",
             "indépendante", "marque indépendante", "independent", "independiente", "indipendente",
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
    "pan": ("poêle", "poele", "casserole", "sauteuse", "wok", "pan", "padella", "sartén", "sarten", "pfanne", "koekenpan"),
}
_TYPE_OF = {w: t for t, ws in _TYPES.items() for w in ws}


def _type_of(w):
    """Canonical type of a title word; a trailing plural 's' is tried too ('assiettes')."""
    return _TYPE_OF.get(w) or (_TYPE_OF.get(w[:-1]) if len(w) > 3 and w.endswith("s") else None)


_BRAND_ALIAS = {"nike air": "nike", "nike sb": "nike", "adidas originals": "adidas", "adidas performance": "adidas", "adidas stan smith": "adidas"}


def _real_brand(b: str) -> bool:
    return bool(b) and b not in _NO_BRAND and not _type_of(b) and b not in _STOP and not _colour_of(b)


def _brand_of(row: dict) -> str:
    """Lower-cased brand, or '' when there is none or it is a guess. Old rows guessed brand from
    the first title word ("Vintage", "Mug", "Blanc"); those words are never brands here.
    A brand read from the photo by the AI (row['ai_brand'], see attach_ai) is used only when the
    listing has no usable brand of its own."""
    brand = (row.get("brand") or "").strip().lower()
    if not _real_brand(brand):
        brand = (row.get("ai_brand") or "").strip().lower()
        if not _real_brand(brand):
            return ""
    return _BRAND_ALIAS.get(brand, brand)


def product_key(row: dict):
    """(brand, canonical type) or None when unidentified. See module docstring."""
    brand = _brand_of(row)
    if not brand:
        return None
    cat = _tokens(row.get("category"))  # Vinted's own category (item page), when known: no guessing
    if cat:
        return brand, next((t for t in map(_type_of, cat) if t), None) or " ".join(cat)
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


def _family(row: dict) -> str:
    """shoes | face | pan | kitchen | other: decides which parts a listing needs to be identified.
    Vinted's category path (or deepest category) when the row has one, else the title's type."""
    path = (row.get("category_path") or row.get("category") or "").lower()
    if path:
        if any(w in path for w in ("chaussure", "basket", "sneaker")):
            return "shoes"
        if "visage" in path or "soin" in path:
            return "face"
        if any(w in path for w in ("poêle", "poele", "casserole", "sauteuse", "wok")):
            return "pan"
        if "cuisine" in path:
            return "kitchen"
    k = product_key(row)
    return k[1] if k and k[1] in ("shoes", "pan") else "other"


# Words that describe a product but are not its model name ("Nike running noir" has no model).
_MODEL_FILLER = {"running", "sport", "sports", "trainers", "trainer", "runner", "course", "gym", "fitness",
                 "training", "ml", "cl", "oz", "kg", "cm", "mm",
                 # materials and shapes: "Bottes Zara cuir" names no model
                 "cuir", "daim", "toile", "simili", "leather", "suede", "cuero", "pelle", "leder", "leer",
                 "montantes", "basses", "low", "high", "mid", "plateforme", "compensées",
                 # generic "sports/running/walking shoes" words in other languages: not a model
                 "deportivas", "deportiva", "corsa", "walking", "hiking", "trail", "ballo", "danza", "dance",
                 "sportschuhe", "laufschuhe", "hardloopschoenen", "tennis", "football", "foot", "basket", "shoes"}


def _model_of(row: dict, exclude: str = "", numbers: bool = False) -> str:
    """First two title words left after removing brand, filler, colours, sizes, numbers, type words
    and single letters, in title order; '' when none (the listing names no model).
    numbers=True (shoes) keeps a model number written right after a model word ("Air Max 90",
    "Pegasus 40"), unless it is the listing's own size: otherwise 90/95 or 40/41 would merge."""
    btoks = set(_tokens(_brand_of(row)))
    size = re.sub(r"\D", "", row.get("size") or "")
    title = (row.get("title") or "").lower().replace("t-shirt", "tshirt").replace("t shirt", "tshirt")
    out, prev_kept = [], False
    for w in _tokens(title):
        if numbers and w.isdigit() and len(w) <= 3 and w != size and prev_kept:
            out.append(w)
            prev_kept = False
            continue
        keep = (w not in btoks and w not in _STOP and w not in _MODEL_FILLER and w != exclude
                and not _colour_of(w) and not _SIZE_RE.match(w) and not any(c.isdigit() for c in w)
                and not _type_of(w) and len(w) > 1)
        if keep:
            if sum(not x.isdigit() for x in out) == 2:
                break
            out.append(w)
        prev_kept = keep
    if not out and row.get("ai_model"):  # the title names no model: the one the AI read from the photo
        return _model_of({**row, "title": row["ai_model"], "ai_model": ""}, exclude, numbers)
    return " ".join(out)


def attach_ai(rows, cache_dir=None) -> int:
    """Adds row['ai_brand'] / row['ai_model'] from the photo-AI cache (phase5_cache/vision_*.json), only
    for high/medium confidence and never for accessories. identified_key uses them ONLY where the listing
    has no usable brand / no model of its own, and every other part (colour, size, capacity) must still
    come from the listing. Returns how many rows got an AI identity (0 when the AI was never switched on)."""
    import glob, json
    ident = {}
    for f in glob.glob(os.path.join(cache_dir or os.environ.get("VINTED_CLIP_CACHE", "phase5_cache"), "vision_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                ident.update(json.load(fh))
        except (OSError, ValueError):
            pass
    n = 0
    for r in rows:
        v = ident.get(str(r.get("id")))
        if isinstance(v, dict) and not v.get("is_accessory") and v.get("confidence") in ("high", "medium"):
            r["ai_brand"] = v.get("brand") or ""
            r["ai_model"] = v.get("product_line") or v.get("official_name") or ""
            n += 1
    return n


# Words that praise or date a listing without naming the product ("Jellycat retired rare").
_MODEL_NOISE = {"retired", "rare", "edition", "limited", "limitee", "limitée", "collection", "collector",
                "authentic", "authentique", "official", "officiel", "officielle", "boxed", "occasion",
                "excellent", "tag", "tags", "label", "etiquette", "livraison", "envoi", "vendu", "reserve",
                "réservé", "bundle", "lot", "pack", "dupe", "cadeau", "gift", "ans", "mois", "jusqu", "bis",
                "monat", "jahre", "months", "years", "month", "year", "age", "baby", "bebe", "bébé",
                "couleur", "color", "colour", "colore", "farbe", "soft", "toy", "jouet", "neuf", "neuve", "tbe", "new"}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _name_tokens(row: dict, btoks: set) -> list:
    """Title words that can be part of a model name: no brand, colour, size, number, type, filler."""
    title = (row.get("title") or "").lower().replace("t-shirt", "tshirt").replace("t shirt", "tshirt")
    return [_stem(w) for w in _tokens(title)
            if len(w) > 2 and w not in btoks and w not in _STOP and w not in _MODEL_FILLER and w not in _MODEL_NOISE
            and not _colour_of(w) and not _SIZE_RE.match(w) and not any(c.isdigit() for c in w) and not _type_of(w)]


def model_namer(rows, min_count: int = 8, min_rows: int = 30, default_brand: str = ""):
    """Returns f(row) -> the MODEL NAME the title gives, for brands that have >= min_rows listings.
    f(row) is None when the row's brand is too small to learn from (caller keeps its old grouping)
    and '' when the title names no model (the listing is not counted as a product).
    A model is the commonest word, or pair of adjacent words (either order), that this brand's titles
    repeat in >= min_count listings: "Malachy Dragon", "Amuseables Croissant", "Samba". A pair beats
    a single word, so "Sky Dragon" and "Snow Dragon" stay two products. Learned per brand from the
    file itself, so it works for any brand without a hand-made list."""
    def brand_of(r):  # a row with no usable brand still belongs to the file's own brand when its title says so
        return _brand_of(r) or (default_brand if default_brand and default_brand in (r.get("title") or "").lower() else "")

    by_brand = collections.defaultdict(list)
    for r in rows:
        b = brand_of(r)
        if b:
            by_brand[b].append(_name_tokens(r, set(_tokens(b))))
    vocab = {}
    for b, cands in by_brand.items():
        if len(cands) < min_rows:
            continue
        uni, bi, shown = collections.Counter(), collections.Counter(), collections.defaultdict(collections.Counter)
        for c in cands:
            uni.update(set(c))
            for p in {(x, y) for x, y in zip(c, c[1:]) if x != y}:
                key = tuple(sorted(p))
                bi[key] += 1
                shown[key][" ".join(p)] += 1
        vocab[b] = (uni, bi, shown)

    def namer(row):
        b = brand_of(row)
        if b not in vocab:
            return None
        uni, bi, shown = vocab[b]
        c = _name_tokens(row, set(_tokens(b)))
        pairs = [tuple(sorted(p)) for p in {(x, y) for x, y in zip(c, c[1:]) if x != y}]
        pairs = [p for p in pairs if bi[p] >= min_count]
        if pairs:
            return shown[max(pairs, key=lambda p: bi[p])].most_common(1)[0][0].title()
        words = [w for w in set(c) if uni[w] >= min_count]
        return max(words, key=lambda w: uni[w]).title() if words else ""
    namer.active = bool(vocab)  # False when no brand has enough listings to learn from
    return namer


def _volume_ml(title: str) -> int:
    return int(round(fr.title_capacity_ml(title)))


_DIAM_RE = re.compile(r"(?<!\d)(1\d|2\d|3\d|40)\s?cm\b")


def _diameter_cm(title: str) -> str:
    m = _DIAM_RE.search((title or "").lower())
    return f"{m.group(1)}cm" if m else ""


def variant_of(row: dict) -> str:
    fam = _family(row)
    if fam == "shoes":
        return " ".join(x for x in _variant_parts(row) if x) or "-"
    if fam == "face":
        return f"{_volume_ml(row.get('title', ''))}ml"
    if fam == "pan":
        return _diameter_cm(row.get("title", "")) or "-"
    return " ".join(x for x in _variant_parts(row) if x) or "-"


# Stanley sells several product LINES and limited-edition collaborations under one brand: a Quencher is not a
# ProTour, and a LoveShackFancy is another (dearer) product. Checked in this order on the title with spaces,
# hyphens and underscores removed ("Pro Tour" = "protour"); a title naming none has no line.
_STANLEY_LINES = (
    ("collab", ("loveshack", "loveshake", "barbie", "oliviarodrigo", "labubu", "lululemon", "starbucks")),
    ("protour", ("protour",)), ("aerolight", ("aerolight",)), ("flip straw", ("flipstraw", "flipjet")),
    ("iceflow", ("iceflow",)), ("adventure", ("adventure",)), ("go", ("gotumbler", "goquencher")),
    ("classic", ("classic", "legendary")), ("quencher", ("quencher", "flowstate", "h2.0", "h20", "h2o")))


# things sold FOR a Stanley (bag, lid, sleeve) or several at once: never a sale of one tumbler
_STANLEY_NOT_A_TUMBLER = re.compile(r"tas voor|tasche|deckel|ersatz|couvercle de rechange|replacement|sleeve|pochette|housse"
                                    r"|[ée]tui|holder|porte-gobelet|\bbags?\b|\bsacs?\b|\blotes?\b|lot de|set de 2|\b2x\b|\bx2\b|\bduo\b")


def _stanley_line(title: str) -> str:
    low = (title or "").lower()
    if _STANLEY_NOT_A_TUMBLER.search(low):
        return "accessory"
    t = re.sub(r"[\s_\-]", "", low)
    return next((name for name, words in _STANLEY_LINES if any(w in t for w in words)), "")


def identified_key(row: dict):
    """The product key when the listing is fully identified for its family, else None (unidentified,
    never counted). shoes: brand + model, with colour AND size. face care: brand + type + product name,
    with a volume. pan: brand + diameter. kitchen: brand + type. other (clothing...): brand + type,
    with colour AND size/capacity."""
    k = product_key(row)
    if not k:
        return None
    brand, typ = k
    title, fam = row.get("title", ""), _family(row)
    if brand == "stanley" and _stanley_line(title):
        # a Stanley with a named line is one product whichever Vinted category the seller picked
        # (Gourdes / Verres / Tasses / even kitchen tools): brand + line, with colour AND capacity
        return (brand, "tumbler", _stanley_line(title)) if all(_variant_parts(row)) else None
    if fam == "shoes":
        model = _model_of(row, numbers=True)
        return (brand, "shoes", model) if model and all(_variant_parts(row)) else None
    if fam == "face":
        model = _model_of(row, typ)
        return (brand, typ, model) if model and _volume_ml(title) > 0 else None
    if fam == "pan":
        return (brand, "pan") if _diameter_cm(title) else None
    if fam == "kitchen":
        return k
    return k if all(_variant_parts(row)) else None


def verification_order(tracking: dict) -> list:
    """Disappeared-but-unverified listing ids, products with the MOST listings first (repeat
    products are the only ones that can ever be ranked), then newest disappearance first."""
    size = collections.Counter(k for k in (identified_key(r) for r in tracking.values()) if k)
    ids = [i for i, r in tracking.items() if r["status"] == "disappeared"]
    return sorted(ids, key=lambda i: (-size.get(identified_key(tracking[i]), 0),
                                      "".join(chr(255 - ord(c)) for c in tracking[i].get("disappeared_at", ""))))


def rank_brands(rows, days=30, min_sold=3, now=None, page_brands_only=False):
    """Brands ranked by confirmed sales in the last `days` (client 2026-10-20: ranking of the
    brands that sell best in the category over the last 30 days, by trend). Needs only a real
    brand, so far more listings count than for exact products. trend = sales in the recent half
    of the window vs the earlier half (None when the earlier half had none: 'new').
    page_brands_only: count only rows whose brand was read from the item page (they have a
    category) - needed for files from versions that stored the first title word as brand.
    active_listings then counts only page-checked actives (usually none): measure competition live."""
    now = now or max((ts._parse(r["last_seen"]) for r in rows if r.get("last_seen")),
                     default=datetime.now(timezone.utc))
    since, mid = now - timedelta(days=days), now - timedelta(days=days / 2)
    groups = collections.defaultdict(list)
    for r in rows:
        b = _brand_of(r)
        if b and (r.get("category") or not page_brands_only):
            groups[b].append(r)
    out = []
    for brand, g in groups.items():
        sold = [r for r in g if r["status"] == "sold" and r.get("last_seen") and ts._parse(r["last_seen"]) >= since]
        if len(sold) < min_sold:
            continue
        recent = sum(1 for r in sold if ts._parse(r["last_seen"]) >= mid)
        earlier = len(sold) - recent
        # a sweep file stamps sales on the few days it was swept: with < 5 distinct dates a trend would be invented
        dated = len({r["last_seen"][:10] for r in sold}) >= 5
        prices = [float(r["price"]) for r in sold if r.get("price")]
        life = [v for v in (ts._lifespan_hours(r) for r in sold) if v is not None]
        active = [r for r in g if r["status"] == "active"]
        offers = [int(float(r["offers"])) for r in active if r.get("offers") not in ("", None)]
        types = collections.Counter(k[1] for k in map(product_key, sold) if k)
        out.append({
            "brand": brand,
            "confirmed_sold_30d": len(sold),
            "trend_pct": round((recent - earlier) * 100 / earlier) if earlier and dated else None,
            "trend_dated": dated,
            "sold_recent_half": recent, "sold_earlier_half": earlier,
            "median_sold_price": round(statistics.median(prices), 2) if prices else "",
            "median_days_to_sell": round(statistics.median(life) / 24, 1) if life else "",
            "n_with_posting_date": len(life),
            "active_listings": len(active),
            "median_offers_active": statistics.median(offers) if offers else "",
            "max_offers_active": max(offers) if offers else "",
            "n_offers_measured": len(offers),
            "top_products": "; ".join(f"{t}: {n} sold" for t, n in types.most_common(5)),
            "source_listings": " ".join("https://www.vinted.fr/items/" + r["id"] for r in sold[:3]),
        })
    out.sort(key=lambda b: (-b["confirmed_sold_30d"], b["active_listings"]))
    return out


def rank_products(rows, days=30, min_sold=3, now=None):
    now = now or max((ts._parse(r["last_seen"]) for r in rows if r.get("last_seen")),
                     default=datetime.now(timezone.utc))
    since = now - timedelta(days=days)
    # the file may cover fewer days than the window (a tracker started 19 days ago): a monthly estimate
    # divides by the days actually covered, as the client reports do, not by the full window
    firsts = [ts._parse(r["first_seen"]) for r in rows if r.get("first_seen")]
    covered = min(days, max(1.0, (now - min(firsts)).total_seconds() / 86400)) if firsts else days
    groups, unidentified, ai_ids = collections.defaultdict(list), 0, set()
    for r in rows:
        k = identified_key(r)
        if k:
            groups[k].append(r)
            # identified only thanks to the photo AI (the listing's own brand/title was not enough)?
            if (r.get("ai_brand") or r.get("ai_model")) and not identified_key({**r, "ai_brand": "", "ai_model": ""}):
                ai_ids.add(r["id"])
        else:
            unidentified += 1
    out = []
    for k, g in groups.items():
        brand, typ = k[0], " ".join(k[1:])
        sold = [r for r in g if r["status"] == "sold" and r.get("last_seen") and ts._parse(r["last_seen"]) >= since]
        if len(sold) < min_sold:
            continue
        prices = [float(r["price"]) for r in sold if r.get("price")]
        life = [v for v in (ts._lifespan_hours(r) for r in sold) if v is not None]
        active = sum(1 for r in g if r["status"] == "active")
        per_month = len(sold) * 30 / covered
        median_days = (statistics.median(life) / 24) if life else None
        opp = ts.compute_variant_opportunity(per_month, median_days)
        comp = ts.competition_label(active)
        out.append({
            "product": " ".join(k), "brand": brand, "type": typ,
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
            "ai_identified_listings": sum(1 for r in g if r["id"] in ai_ids),
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
    ap.add_argument("--by", choices=("product", "brand"), default="product")
    ap.add_argument("--sort", choices=("sales", "trend"), default="sales", help="brand ranking order")
    ap.add_argument("--page-brands-only", action="store_true",
                    help="brand ranking: only brands read from the item page (old files guessed brand from the title)")
    ap.add_argument("--category", action="append", default=[],
                    help="keep rows whose category path contains this text (repeatable, OR)")
    a = ap.parse_args()
    files = a.files or _sweep_files()
    if not files:
        print("no category sweep files found (tracked_keywords.txt cat: lines / tracking/*.csv); nothing to do")
        return
    rows = [r for f in files for r in csv.DictReader(open(f, encoding="utf-8"))]
    n_ai = attach_ai(rows)
    if n_ai:
        print(f"photo AI: {n_ai} listings have an AI identity; it fills a missing brand or model only (counted as 'AI-identified')")
    if a.category:
        want = [c.lower() for c in a.category]
        total = len(rows)
        rows = [r for r in rows if any(c in (r.get("category_path") or r.get("category") or "").lower() for c in want)]
        print(f"category filter {a.category}: kept {len(rows)} of {total} rows")
    if a.by == "brand":
        brands = rank_brands(rows, a.days, a.min_sold, page_brands_only=a.page_brands_only)
        if a.sort == "trend":  # 'new' (no sales in the earlier half) first, then by % change
            brands.sort(key=lambda b: (b["trend_pct"] is not None, -(b["trend_pct"] or 0), -b["confirmed_sold_30d"]))
        stamps = [ts._parse(r["last_seen"]) for r in rows if r.get("last_seen")]
        if stamps:
            print(f"data window: {min(stamps):%Y-%m-%d} to {max(stamps):%Y-%m-%d}; a sale is dated by the last sweep that saw the listing")
        print(f"{len(rows)} listings -> {len(brands)} brands with >= {a.min_sold} confirmed sales in {a.days} days")
        for i, b in enumerate(brands[: a.top], 1):
            tr = "n/a: sale dates too coarse" if not b["trend_dated"] else "new" if b["trend_pct"] is None else f"{b['trend_pct']:+d}%"
            tts = f"{b['median_days_to_sell']} days to sell (n={b['n_with_posting_date']})" if b["n_with_posting_date"] >= 5 else "time to sell n/a (too few dated sales)"
            print(f"{i:>3}. {b['brand']}: {b['confirmed_sold_30d']} sold (trend {tr}) · median €{b['median_sold_price']}"
                  f" · {tts} · {b['active_listings']} page-checked active"
                  f" · offers max {b['max_offers_active'] if b['max_offers_active'] != '' else '-'}"
                  f"\n     {b['top_products']}")
        _write(a.out, brands)
        return
    prods, ngroups, unid = rank_products(rows, a.days, a.min_sold)
    print(f"{len(rows)} listings -> {ngroups} products (brand + type + colour + size identified); {unid} unidentified (not counted); "
          f"{len(prods)} products with >= {a.min_sold} confirmed sales in {a.days} days")
    for p in prods[: a.top]:
        tts = (f"{p['median_hours_to_sell']} h to sell (n={p['n_with_posting_date']})"
               if p["n_with_posting_date"] >= 5 else "time to sell n/a (too few dated sales)")
        print(f"\n{p['product']}: {p['confirmed_sold_30d']} confirmed sold · {p['active_listings']} active · "
              f"median €{p['median_sold_price']} · {tts}"
              f" · competition {p['competition_level']} · score {p['score']}"
              f"{' · ALERT' if p['passes_alert_rule'] else ''}"
              f"{' · ' + str(p['ai_identified_listings']) + ' AI-identified' if p['ai_identified_listings'] else ''}"
              f"\n   variants: {p['variants']}\n   e.g. {p['source_listings']}")
    print(f"\n{sum(p['passes_alert_rule'] for p in prods)} of {len(prods)} products pass the alert rule "
          "(>=30 confirmed sales/30d, competition not High, score >=70)")
    print("note: for category-sweep files, time to sell is a lower bound (see module docstring)")
    _write(a.out, prods)


def _write(path, rows):
    if path:  # written even when empty, so a quiet run still leaves a (header-only) file
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["product"])
            w.writeheader(); w.writerows(rows)
        print("\nsaved", path)


if __name__ == "__main__":
    main()
