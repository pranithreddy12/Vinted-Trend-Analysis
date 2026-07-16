"""
Phase 5 — Exact product IDENTIFICATION (photo → complete, Google-verifiable title).

The clustering engine (image_cluster.py) answers "are these the same product?".
This module answers the harder question the client asked for: given a listing photo,
name the brand, the product line and the colour, and compose a searchable title like
"Stanley Quencher H2.0 FlowState Tumbler Rose Quartz 40oz (1.18L)" — good enough that
pasting it into Google lands on the exact model.

HOW IT WORKS — and why this way (measured, not assumed):
  • Zero-shot CLIP against hand-written text prompts was tried first and FAILED on the
    fine line distinctions (≈2/6 on Quencher, 0/6 on Flip Straw): base CLIP can't tell a
    Quencher from a Flip Straw off a text phrase — they're both straw tumblers.
  • Image-to-image PROTOTYPES work far better (CLIP's real strength). We embed listings
    whose TITLES already name the line (self-supervised labels), average them into one
    prototype per line, and classify an unknown photo by nearest prototype. Measured
    leave-one-out accuracy on real Stanley photos: **73%** across 4 lines, and much
    higher on the confident subset (clear margin between the top two prototypes).
  • So the design is TITLE-FIRST: if the seller names the line, we trust the title
    (ground truth). The IMAGE only fills the gap when the title is silent (~half of
    listings), and only when it's confident — otherwise we leave the line unstated
    rather than guess. Capacity is ALWAYS from the title (a photo can't show litres).

Honest ceiling (told to the client): ~73% off-the-shelf is a useful foundation, not
"exact" grade for every photo. The look-alike straw tumblers (Quencher/Flip Straw/
IceFlow) are where it slips. Production-grade exact identification of any photo needs a
stronger vision model (a multimodal LLM + official-source lookup) — a real API/cost
dependency. This module is the local, no-API foundation that the LLM layer would replace
or backstop.

CLI:
    python product_identify.py "stanley quencher"        # identify live listings
    python product_identify.py --build stanley           # (re)build the line prototypes
    python product_identify.py --url <photo_url> --title "stanley cup 1.18L rose"
"""

import os
import sys
import argparse

import numpy as np

import image_cluster as ic
import track_sales as ts          # Phase-4 capacity/colour/line text helpers
import fetch_results as fr


# ─────────────────────────────────────────
# BRAND REFERENCE (line → official title) + colour display
# ─────────────────────────────────────────
# The prototypes (the visual side) are learned from live listings; this table just maps
# a detected line to its official product name for the generated title, and gives the
# search queries used to gather labelled examples when (re)building prototypes.

BRAND_LINES = {
    "Stanley": {
        "official": {
            "Quencher": "Quencher H2.0 FlowState Tumbler",
            "Flip Straw": "IceFlow Flip Straw Tumbler",
            "IceFlow": "IceFlow Tumbler",
            "Classic": "Classic Legendary Bottle",
            "GO Tumbler": "GO Quencher Tumbler",
        },
        # Queries whose results carry title-labelled examples of each line.
        "build_queries": [
            "stanley quencher rose", "stanley quencher", "stanley flip straw",
            "stanley iceflow", "stanley classic",
        ],
    },
}

# Canonical colour → brand-flavoured display name (Stanley's pink is "Rose Quartz",
# which is also what verifies on Google).
COLOUR_DISPLAY = {
    "pink": "Rose Quartz", "purple": "Lavender", "blue": "Pool", "green": "Alpine",
    "black": "Black", "white": "Frost", "cream": "Cream", "grey": "Charcoal",
    "red": "Cranberry", "orange": "Tigerlily", "yellow": "Citron",
}

# An image prototype only "wins" if it beats the runner-up by this cosine margin —
# otherwise the two look-alike lines are too close to call and we stay silent.
IMAGE_MARGIN_MIN = 0.010
PROTO_MIN_EXAMPLES = 5     # don't trust a prototype built from fewer than this


# ─────────────────────────────────────────
# LINE PROTOTYPES (self-supervised from titled listings)
# ─────────────────────────────────────────

def _proto_path(brand: str) -> str:
    os.makedirs(ic.CACHE_DIR, exist_ok=True)
    return os.path.join(ic.CACHE_DIR, f"prototypes_{ic._safe(brand)}.npz")


def build_line_prototypes(brand: str = "Stanley", pages: int = 2) -> dict:
    """Fetch listings whose TITLES name a line, embed their photos, and average each
    line into one prototype vector. Cached to phase5_cache/prototypes_<brand>.npz.
    This is the 'training' step — cheap, self-supervised, re-runnable when the range
    changes. Returns {line: unit centroid}."""
    cfg = BRAND_LINES.get(brand)
    if cfg is None:
        raise ValueError(f"no line config for brand '{brand}'")
    c, t = ic._anon_session()
    seen, labelled = set(), []
    for q in cfg["build_queries"]:
        for it in fr.fetch_catalog_via_requests(fr.normalize_search_query(q), c, t, max_pages=pages):
            iid = it.get("id")
            if iid in seen:
                continue
            seen.add(iid)
            line = ts.detect_model(it.get("title", ""))
            url = ic._photo_url(it)
            if line in cfg["official"] and url:
                labelled.append((line, url))
    print(f"   gathered {len(labelled)} title-labelled examples")
    imgs = [ic.download_image(u) for _, u in labelled]
    embs = ic.embed_images(imgs)

    protos, counts = {}, {}
    for (line, _), e in zip(labelled, embs):
        if not np.any(e):
            continue
        protos.setdefault(line, []).append(e)
    centroids = {}
    for line, vecs in protos.items():
        if len(vecs) < PROTO_MIN_EXAMPLES:
            continue
        m = np.mean(vecs, axis=0)
        n = np.linalg.norm(m)
        if n > 0:
            centroids[line] = (m / n).astype(np.float32)
            counts[line] = len(vecs)
    if centroids:
        lines = list(centroids)
        np.savez_compressed(
            _proto_path(brand),
            lines=np.array(lines),
            centroids=np.stack([centroids[x] for x in lines]),
            counts=np.array([counts[x] for x in lines]),
        )
        print(f"   💾 prototypes: {counts} → {_proto_path(brand)}")
    return centroids


def load_line_prototypes(brand: str = "Stanley") -> dict:
    """Load cached prototypes (no torch needed). Returns {} if none built yet."""
    path = _proto_path(brand)
    if not os.path.exists(path):
        return {}
    try:
        d = np.load(path, allow_pickle=True)
        return dict(zip((str(x) for x in d["lines"]), d["centroids"]))
    except Exception:
        return {}


def _line_from_image(img_emb: np.ndarray, protos: dict) -> tuple[str, float]:
    """Nearest-prototype line, with the top-2 cosine MARGIN as confidence. Returns
    ('', 0) when it's too close to call (look-alike lines) or no prototypes exist."""
    if not protos:
        return "", 0.0
    lines = list(protos)
    sims = np.array([float(protos[ln] @ img_emb) for ln in lines])
    order = np.argsort(sims)[::-1]
    best = lines[order[0]]
    margin = float(sims[order[0]] - sims[order[1]]) if len(order) > 1 else float(sims[order[0]])
    if margin < IMAGE_MARGIN_MIN:
        return "", margin
    return best, margin


# ─────────────────────────────────────────
# COLOUR (zero-shot from image, fallback when title has none)
# ─────────────────────────────────────────

def _colour_from_image(img_emb: np.ndarray) -> str:
    canon = list(COLOUR_DISPLAY.keys())
    sims = ic.embed_texts([f"a {c} coloured insulated bottle or tumbler" for c in canon]) @ img_emb
    return canon[int(np.argmax(sims))]


# ─────────────────────────────────────────
# IDENTIFY
# ─────────────────────────────────────────

def identify_product(img, brand: str = "Stanley", title_text: str = "",
                     protos: dict | None = None) -> dict:
    """Identify one product from a PIL image (+ optional listing title).

    TITLE-FIRST: the line/colour/capacity come from the title when stated (ground
    truth); the image fills the line and colour only when the title is silent, and the
    line only when the image is confident. Returns a dict with the fields and a
    `line_source` of 'title' | 'image' | 'unknown' so the caller knows how sure to be.
    """
    cfg = BRAND_LINES.get(brand, {"official": {}})
    r = {"brand": brand, "line": "", "line_official": "", "colour": "",
         "colour_display": "", "capacity": "", "line_source": "unknown",
         "line_confidence": 0.0, "generated_title": "", "note": ""}

    toks = ts.fr._tokenize((title_text or "").lower())

    # LINE — title first, image fallback.
    title_line = ts.detect_model(title_text or "")
    emb = None
    if title_line:
        r["line"], r["line_source"], r["line_confidence"] = title_line, "title", 1.0
    else:
        emb = ic.embed_images([img])[0]
        if np.any(emb):
            protos = load_line_prototypes(brand) if protos is None else protos
            line, margin = _line_from_image(emb, protos)
            r["line_confidence"] = round(margin, 3)
            if line:
                r["line"], r["line_source"] = line, "image"
            else:
                r["note"] = "line not confidently readable from the photo"
        else:
            r["note"] = "image could not be embedded"
    r["line_official"] = cfg["official"].get(r["line"], "")

    # COLOUR — title first, image fallback.
    col = next((ts.COLOR_BUCKETS[t] for t in toks if t in ts.COLOR_BUCKETS), "")
    if not col:
        if emb is None:
            emb = ic.embed_images([img])[0]
        if np.any(emb):
            col = _colour_from_image(emb)
    r["colour"] = col
    r["colour_display"] = COLOUR_DISPLAY.get(col, col.title()) if col else ""

    # CAPACITY — title only (a photo can't show litres).
    r["capacity"] = next((c for c in (ts.canonical_capacity(t) for t in toks) if c), "")

    r["generated_title"] = compose_title(r)
    return r


def compose_title(r: dict) -> str:
    """Complete searchable title from the identified fields; omits what we don't know
    rather than inventing it. e.g. 'Stanley Quencher H2.0 FlowState Tumbler Rose Quartz
    40oz (1.18L)'."""
    if not r.get("line_official"):
        bits = [r.get("brand", "")]
        if r.get("colour_display"):
            bits.append(r["colour_display"])
        if r.get("capacity"):
            bits.append(r["capacity"])
        return " ".join(b for b in bits if b).strip()
    bits = [r["brand"], r["line_official"]]
    if r.get("colour_display"):
        bits.append(r["colour_display"])
    if r.get("capacity"):
        metric = ts._CAP_METRIC.get(r["capacity"])
        bits.append(f"{r['capacity']} ({metric})" if metric else r["capacity"])
    return " ".join(bits).strip()


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────

def _print(r: dict, source: str = ""):
    print("─" * 64)
    if source:
        print(f"  {source}")
    src = {"title": "from title", "image": "from PHOTO", "unknown": "—"}[r["line_source"]]
    print(f"  line: {r['line'] or '(unresolved)':12} ({src})   colour: {r['colour_display'] or '?'}")
    if r["note"]:
        print(f"  note: {r['note']}")
    print(f"  🏷️  TITLE → {r['generated_title'] or '(insufficient signal)'}")


def _run_live(keyword: str, n: int):
    brand = keyword.split()[0].title()
    protos = load_line_prototypes(brand)
    if not protos:
        print(f"ℹ️  no prototypes for {brand} yet — building them first…")
        protos = build_line_prototypes(brand)
    listings = ic.fetch_live_listings(keyword, max_items=n * 3, max_pages=2)
    done = 0
    for ls in listings:
        img = ic.download_image(ls.photo_url)
        if img is None:
            continue
        r = identify_product(img, brand=brand, title_text=ls.title, protos=protos)
        _print(r, source=f'"{ls.title[:54]}"')
        done += 1
        if done >= n:
            break
    print("─" * 64)
    print(f"identified {done} listings for '{keyword}'")


def main():
    ap = argparse.ArgumentParser(description="Phase 5 — exact product identification")
    ap.add_argument("keyword", nargs="?", default="stanley quencher")
    ap.add_argument("--build", metavar="BRAND", help="(re)build line prototypes for a brand")
    ap.add_argument("--url", help="identify a single photo URL")
    ap.add_argument("--title", default="", help="listing title (gives capacity/colour)")
    ap.add_argument("--brand", default="", help="brand override (default: from keyword)")
    ap.add_argument("--max", type=int, default=8, help="how many live listings to identify")
    args = ap.parse_args()

    if args.build:
        build_line_prototypes(args.build.title())
    elif args.url:
        img = ic.download_image(args.url)
        if img is None:
            print("❌ could not download that image URL")
            sys.exit(1)
        brand = args.brand or "Stanley"
        _print(identify_product(img, brand=brand, title_text=args.title), source=args.url)
    else:
        _run_live(args.keyword, args.max)


if __name__ == "__main__":
    main()
