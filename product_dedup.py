"""
Phase 5 completion — identify each PRODUCT once, then reuse across sellers.

The per-listing cache already avoids paying to analyse the SAME listing twice. This adds the step
the client asked for: before calling the Vision AI on a new listing, check whether we've already
identified this PRODUCT (a different seller's listing of the same thing). If so, reuse that result
— no AI call.

Matching is by PHOTO EMBEDDING *within the same colour + capacity*:
  • the colour/capacity gate means a Cream tumbler can never borrow a Rose Quartz title just to
    save a call (look-alike colours in CLIP space don't merge);
  • the photo embedding then separates distinct products that share a colour (a plain pink
    Quencher vs a Barbie pink Quencher).
Embeddings are local (CLIP) and free; only genuinely new products cost an AI call.
"""

import os
import json

import numpy as np

import image_cluster as ic
import track_sales as ts


def listing_keys(listing, listing_colour: str = "") -> tuple:
    """(colour, capacity) for a Listing, from the listing's own data — available BEFORE any AI
    call. Colour prefers the listing's declared colour, else a colour word in the title;
    capacity is read from the title (a photo can't show litres)."""
    toks = ts.fr._tokenize((getattr(listing, "title", "") or "").lower())
    title_col = next((ts.COLOR_BUCKETS[t] for t in toks if t in ts.COLOR_BUCKETS), "")
    colour = listing_colour or title_col
    cap = next((c for c in (ts.canonical_capacity(t) for t in toks) if c), "")
    return colour, cap


class ProductRegistry:
    """Known products for one keyword/category: photo centroid + attributes + identity,
    persisted so reuse carries across runs. `find` gates on (colour, capacity) first, then photo
    cosine — the attribute gate is what makes reuse safe."""

    def __init__(self, slug: str, threshold: float | None = None):
        os.makedirs(ic.CACHE_DIR, exist_ok=True)
        self.path = os.path.join(ic.CACHE_DIR, f"registry_{ic._safe(slug)}.json")
        self.threshold = ic.DEFAULT_THRESHOLD if threshold is None else threshold
        self.products = []          # [{colour, capacity, emb:[...], identity:{...}}]
        self._dirty = False
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.products = json.load(f)
            except Exception:
                self.products = []

    @staticmethod
    def _key(colour: str, capacity: str) -> tuple:
        return (ts.normalize_color(colour or "") or (colour or "").lower().strip(),
                (capacity or "").lower())

    def find(self, colour: str, capacity: str, emb) -> dict | None:
        """Identity of a matching known product, or None. `emb` must be a unit vector."""
        if emb is None:
            return None
        k = self._key(colour, capacity)
        best, best_sim = None, -1.0
        for p in self.products:
            if (p["colour"], p["capacity"]) != k:
                continue
            sim = float(np.dot(emb, np.asarray(p["emb"], dtype=np.float32)))
            if sim > best_sim:
                best, best_sim = p, sim
        return best["identity"] if best is not None and best_sim >= self.threshold else None

    def add(self, colour: str, capacity: str, emb, identity: dict) -> None:
        if emb is None:
            return
        k = self._key(colour, capacity)
        self.products.append({
            "colour": k[0], "capacity": k[1],
            "emb": [round(float(x), 5) for x in emb],
            "identity": {kk: vv for kk, vv in identity.items() if kk != "_provider"},
        })
        self._dirty = True

    def save(self) -> None:
        if self._dirty:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.products, f, ensure_ascii=False)
            self._dirty = False


def _demo() -> None:
    """Self-check (synthetic unit vectors, no CLIP/network): the colour+capacity gate and the
    photo-similarity threshold both have to hold for a reuse."""
    r = ProductRegistry("_deduptest")
    r.products = []                       # start clean regardless of any stale file
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)   # one product's photo
    b = np.array([0.0, 1.0, 0.0], dtype=np.float32)   # a visually different photo
    ident = {"official_name": "Stanley Quencher", "colour": "pink"}
    assert r.find("pink", "40oz", a) is None, "empty registry → no match"
    r.add("pink", "40oz", a, ident)
    assert r.find("pink", "40oz", a) == ident, "same colour+cap+photo → reuse"
    assert r.find("black", "40oz", a) is None, "different colour must NOT reuse (attribute gate)"
    assert r.find("pink", "20oz", a) is None, "different capacity must NOT reuse"
    assert r.find("pink", "40oz", b) is None, "same colour but different photo → no reuse"
    if os.path.exists(r.path):
        os.remove(r.path)
    print("product_dedup self-check OK")


if __name__ == "__main__":
    _demo()
