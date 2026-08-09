"""
Phase 5 — Visual Variant Recognition (CLIP image embeddings + clustering)
=========================================================================

Problem this solves
--------------------
Phases 3/4 group listings into variants from the *title text* (capacity + colour).
That only works when the seller writes a clean title. Huge numbers of listings have
vague / multilingual / photo-only titles ("Crocs rose 39", "Platform pink", "Sabot
femme") that the text tokenizer cannot line up as the SAME product. This module looks
at the *photo* instead: it embeds each listing's cover image with CLIP, then clusters
listings by visual similarity. Listings whose photos look like the same product land in
the same cluster — regardless of what the title says.

How it fits the rest of the tool
--------------------------------
* Reuses the existing Chrome-over-CDP session + catalog API (``fetch_results``) to pull
  a live, newest-first catalog for a keyword, including each listing's cover photo URL.
* Embeddings are cached by **listing id** (NOT by URL — Vinted CDN URLs expire within
  days, so we must embed while the URL is fresh and keep the *vector*, not the link).
* Output: ``visual_variants_<keyword>.csv`` + a console summary of the visual clusters,
  so a human can eyeball "these 14 listings are the same shoe".

Dependencies (all confirmed present in this environment): torch, transformers (CLIP),
Pillow, scikit-learn, numpy, requests.

Usage
-----
    # Live: uses the CDP Chrome session if one is up, else an anonymous session
    python image_cluster.py "crocs" --max-items 400 --threshold 0.90

    # Offline self-test: proves embed+cluster discriminates, no Chrome needed
    python image_cluster.py --selftest
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import requests

# Windows consoles default to cp1252 and choke on the emoji status output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

CLIP_MODEL = os.environ.get("VINTED_CLIP_MODEL", "openai/clip-vit-base-patch32")
CACHE_DIR = os.environ.get("VINTED_CLIP_CACHE", "phase5_cache")
# Cosine-similarity threshold above which two listings are "the same product".
# CALIBRATED on 300 live Crocs listings (2026-07-06): 0.85 average-linkage produced a
# 108-listing mega-cluster (shape dominates colour in CLIP space, so "all classic
# clogs" merged); 0.90 broke it up (biggest cluster 10) while still grouping ~half
# the listings and correctly unifying e.g. the Cars/McQueen edition across four
# languages. Raise toward 0.92+ for stricter colour separation at the cost of recall.
DEFAULT_THRESHOLD = float(os.environ.get("VINTED_CLIP_THRESHOLD", "0.90"))
BATCH_SIZE = int(os.environ.get("VINTED_CLIP_BATCH", "16"))
DOWNLOAD_TIMEOUT = 25
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Lazily-loaded CLIP handles (importing torch/transformers is slow, ~a few seconds).
_MODEL = None
_PROCESSOR = None
_TORCH = None


# ─────────────────────────────────────────
# CLIP MODEL + EMBEDDING
# ─────────────────────────────────────────

def _load_clip():
    """Lazy-load the CLIP model + processor once, cache on module globals."""
    global _MODEL, _PROCESSOR, _TORCH
    if _MODEL is not None:
        return
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as e:  # pragma: no cover - environment guard
        print(
            "❌ Phase 5 needs torch + transformers. Install with:\n"
            "   pip install torch transformers pillow scikit-learn\n"
            f"   (import error: {e})"
        )
        sys.exit(1)

    print(f"🧠 Loading CLIP model '{CLIP_MODEL}' (first run downloads weights)…")
    t0 = time.time()
    _MODEL = CLIPModel.from_pretrained(CLIP_MODEL)
    _PROCESSOR = CLIPProcessor.from_pretrained(CLIP_MODEL)
    _MODEL.eval()
    _TORCH = torch
    print(f"   ✅ model ready in {time.time() - t0:.1f}s")


def download_image(url: str):
    """Download one image URL → RGB PIL.Image, or None on any failure.

    Vinted CDN URLs expire, so a 404 here on old data is expected and non-fatal.
    """
    from PIL import Image

    try:
        r = requests.get(url, headers=_UA, timeout=DOWNLOAD_TIMEOUT)
        if r.status_code != 200:
            return None
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def embed_images(images: list) -> np.ndarray:
    """Embed a list of PIL images → (N, D) L2-normalised float32 array.

    None entries (failed downloads) are embedded as all-zero vectors so the row
    index stays aligned with the caller's listing list; callers should drop zeros.
    """
    _load_clip()
    torch = _TORCH
    dim = _MODEL.config.projection_dim
    out = np.zeros((len(images), dim), dtype=np.float32)

    valid_idx = [i for i, im in enumerate(images) if im is not None]
    for start in range(0, len(valid_idx), BATCH_SIZE):
        batch_idx = valid_idx[start : start + BATCH_SIZE]
        batch = [images[i] for i in batch_idx]
        with torch.no_grad():
            inp = _PROCESSOR(images=batch, return_tensors="pt")
            feats = _MODEL.get_image_features(**inp)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        out[batch_idx] = feats.cpu().numpy().astype(np.float32)
    return out


def embed_texts(texts: list) -> np.ndarray:
    """Embed a list of text prompts → (N, D) L2-normalised float32 array, in the
    SAME space as embed_images (that's what makes CLIP zero-shot work: an image and
    the text that describes it land close together). Used by product_identify.py to
    score a photo against candidate product descriptions."""
    _load_clip()
    torch = _TORCH
    dim = _MODEL.config.projection_dim
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        with torch.no_grad():
            inp = _PROCESSOR(text=batch, return_tensors="pt", padding=True, truncation=True)
            feats = _MODEL.get_text_features(**inp)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        out[start : start + len(batch)] = feats.cpu().numpy().astype(np.float32)
    return out


# ─────────────────────────────────────────
# EMBEDDING CACHE (keyed by stable listing id)
# ─────────────────────────────────────────

class EmbeddingCache:
    """Persist listing_id → embedding so repeated runs don't re-download/re-embed.

    Keyed by listing id (stable) rather than photo URL (expires). Stored as an
    ``.npz`` per keyword under CACHE_DIR.
    """

    def __init__(self, name: str):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.path = os.path.join(CACHE_DIR, f"emb_{_safe(name)}.npz")
        self.ids: dict[str, np.ndarray] = {}
        if os.path.exists(self.path):
            try:
                data = np.load(self.path, allow_pickle=True)
                ids = data["ids"]
                vecs = data["vecs"]
                self.ids = {str(i): v for i, v in zip(ids, vecs)}
                print(f"   💾 cache: loaded {len(self.ids)} embeddings from {self.path}")
            except Exception:
                self.ids = {}

    def get(self, listing_id: str):
        return self.ids.get(str(listing_id))

    def put(self, listing_id: str, vec: np.ndarray):
        self.ids[str(listing_id)] = vec

    def save(self):
        if not self.ids:
            return
        ids = np.array(list(self.ids.keys()))
        vecs = np.stack(list(self.ids.values()))
        np.savez_compressed(self.path, ids=ids, vecs=vecs)
        print(f"   💾 cache: saved {len(self.ids)} embeddings → {self.path}")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


# ─────────────────────────────────────────
# CLUSTERING
# ─────────────────────────────────────────

def cluster_embeddings(emb: np.ndarray, threshold: float = DEFAULT_THRESHOLD) -> np.ndarray:
    """Agglomerative clustering on cosine distance.

    Two listings merge when cosine similarity >= threshold (i.e. cosine distance
    <= 1 - threshold). Returns an int label per row. Rows are assumed L2-normalised.
    """
    from sklearn.cluster import AgglomerativeClustering

    n = emb.shape[0]
    if n == 0:
        return np.array([], dtype=int)
    if n == 1:
        return np.array([0], dtype=int)

    model = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=1.0 - threshold,
    )
    return model.fit_predict(emb)


# ─────────────────────────────────────────
# STABLE CROSS-RUN VISUAL VARIANT INDEX
# ─────────────────────────────────────────

class VisualVariantIndex:
    """Stable visual-variant IDs across tracking runs.

    One-shot agglomerative clustering renumbers labels every run, which would
    reset per-variant turnover history. This index instead keeps a persistent
    set of cluster CENTROIDS: each new listing joins the best-matching existing
    cluster (cosine >= threshold) or founds a new one. IDs ("v1", "v2", …) are
    assigned once and never change, so Phase 4 can accumulate sales per visual
    variant over weeks.

    Persisted per keyword under CACHE_DIR as index_<name>.npz:
      - cluster ids + centroid vectors (running mean, L2-renormalised)
      - listing_id → cluster_id assignments
      - a human label per cluster (title of its founding listing)
    """

    def __init__(self, name: str, threshold: float = DEFAULT_THRESHOLD):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.path = os.path.join(CACHE_DIR, f"index_{_safe(name)}.npz")
        self.threshold = threshold
        self.centroids: dict[str, np.ndarray] = {}   # cluster_id -> unit vector
        self.counts: dict[str, int] = {}             # cluster_id -> member count
        self.labels: dict[str, str] = {}             # cluster_id -> human label
        self.assignments: dict[str, str] = {}        # listing_id -> cluster_id
        self._next = 1
        if os.path.exists(self.path):
            try:
                d = np.load(self.path, allow_pickle=True)
                cids = [str(c) for c in d["cluster_ids"]]
                self.centroids = dict(zip(cids, d["centroids"]))
                self.counts = dict(zip(cids, (int(c) for c in d["counts"])))
                self.labels = dict(zip(cids, (str(s) for s in d["labels"])))
                self.assignments = dict(
                    zip((str(i) for i in d["listing_ids"]),
                        (str(c) for c in d["listing_clusters"]))
                )
                self._next = 1 + max(
                    (int(c[1:]) for c in cids if c[1:].isdigit()), default=0
                )
                print(f"   💾 index: {len(self.centroids)} visual variants, "
                      f"{len(self.assignments)} listings assigned")
            except Exception:
                pass

    def assign(self, listing_id: str, emb: np.ndarray, label_hint: str = "") -> str:
        """Assign one listing to a stable visual variant id (idempotent)."""
        listing_id = str(listing_id)
        if listing_id in self.assignments:
            return self.assignments[listing_id]

        best_cid, best_sim = None, -1.0
        if self.centroids:
            cids = list(self.centroids.keys())
            mat = np.stack([self.centroids[c] for c in cids])
            sims = mat @ emb
            i = int(np.argmax(sims))
            best_cid, best_sim = cids[i], float(sims[i])

        if best_cid is not None and best_sim >= self.threshold:
            cid = best_cid
            # Update the running-mean centroid, renormalised to unit length.
            n = self.counts[cid]
            merged = (self.centroids[cid] * n + emb) / (n + 1)
            norm = np.linalg.norm(merged)
            if norm > 0:
                self.centroids[cid] = (merged / norm).astype(np.float32)
            self.counts[cid] = n + 1
        else:
            cid = f"v{self._next}"
            self._next += 1
            self.centroids[cid] = emb.astype(np.float32)
            self.counts[cid] = 1
            self.labels[cid] = (label_hint or "")[:60]

        self.assignments[listing_id] = cid
        return cid

    def assign_batch(self, listings: list["Listing"]) -> dict[str, str]:
        """Assign every embedded listing; returns {listing_id: cluster_id}."""
        out = {}
        # Sort for deterministic founding order across identical inputs.
        for ls in sorted(listings, key=lambda x: str(x.id)):
            if ls.emb is None:
                continue
            out[ls.id] = self.assign(ls.id, ls.emb, ls.title)
        return out

    def save(self):
        if not self.centroids:
            return
        cids = list(self.centroids.keys())
        np.savez_compressed(
            self.path,
            cluster_ids=np.array(cids),
            centroids=np.stack([self.centroids[c] for c in cids]),
            counts=np.array([self.counts[c] for c in cids]),
            labels=np.array([self.labels.get(c, "") for c in cids]),
            listing_ids=np.array(list(self.assignments.keys())),
            listing_clusters=np.array(list(self.assignments.values())),
        )
        print(f"   💾 index: saved {len(cids)} visual variants → {self.path}")


def load_visual_assignments(name: str) -> tuple[dict[str, str], dict[str, str]]:
    """Cheap read-only load for track_sales: ({listing_id: cluster_id},
    {cluster_id: label}). No torch import — safe to call in any run."""
    path = os.path.join(CACHE_DIR, f"index_{_safe(name)}.npz")
    if not os.path.exists(path):
        return {}, {}
    try:
        d = np.load(path, allow_pickle=True)
        assignments = dict(
            zip((str(i) for i in d["listing_ids"]),
                (str(c) for c in d["listing_clusters"]))
        )
        labels = dict(
            zip((str(c) for c in d["cluster_ids"]), (str(s) for s in d["labels"]))
        )
        return assignments, labels
    except Exception:
        return {}, {}


# ─────────────────────────────────────────
# LISTING PIPELINE (live catalog → clusters)
# ─────────────────────────────────────────

@dataclass
class Listing:
    id: str
    title: str
    price: float
    photo_url: str
    brand: str = ""
    cluster: int = -1
    emb: np.ndarray | None = field(default=None, repr=False)


def _photo_url(item: dict) -> str:
    """Pull the cover photo URL from a raw Vinted catalog item, defensively."""
    photo = item.get("photo") or {}
    if isinstance(photo, dict):
        url = (
            photo.get("url")
            or (photo.get("high_resolution") or {}).get("url")
            or photo.get("full_size_url")
        )
        if url:
            return url
    photos = item.get("photos") or []
    if photos and isinstance(photos[0], dict):
        return photos[0].get("url", "")
    return ""


def listings_from_catalog(raw_items: list) -> list[Listing]:
    """Convert raw Vinted catalog items → Listing objects that have a photo URL."""
    out = []
    for it in raw_items:
        url = _photo_url(it)
        if not url:
            continue
        price = 0.0
        p = it.get("price")
        if isinstance(p, dict):
            price = float(p.get("amount") or 0)
        elif p:
            price = float(p)
        out.append(
            Listing(
                id=str(it.get("id")),
                title=it.get("title", ""),
                price=price,
                photo_url=url,
                brand=it.get("brand_title", ""),
            )
        )
    return out


def embed_listings(listings: list[Listing], cache: EmbeddingCache | None = None):
    """Fill each listing's .emb, using cache where possible, downloading the rest."""
    need = []
    for ls in listings:
        cached = cache.get(ls.id) if cache else None
        if cached is not None:
            ls.emb = cached
        else:
            need.append(ls)

    print(f"🖼️  {len(listings)} listings — {len(listings) - len(need)} cached, "
          f"{len(need)} to embed")
    if need:
        imgs = []
        for i, ls in enumerate(need, 1):
            imgs.append(download_image(ls.photo_url))
            if i % 25 == 0:
                print(f"   …downloaded {i}/{len(need)} images")
        embs = embed_images(imgs)
        for ls, vec in zip(need, embs):
            if np.any(vec):  # non-zero = successful embed
                ls.emb = vec
                if cache:
                    cache.put(ls.id, vec)
    if cache:
        cache.save()


def cluster_listings(listings: list[Listing], threshold: float) -> list[Listing]:
    """Embed-ready listings → assign .cluster. Returns only listings with an embedding."""
    have = [ls for ls in listings if ls.emb is not None]
    dropped = len(listings) - len(have)
    if dropped:
        print(f"   ⚠️  {dropped} listings had no usable image (expired/failed) — skipped")
    if not have:
        return []
    emb = np.stack([ls.emb for ls in have])
    labels = cluster_embeddings(emb, threshold)
    for ls, lab in zip(have, labels):
        ls.cluster = int(lab)
    return have


# ─────────────────────────────────────────
# TRACK_SALES HOOK (Phase 4 integration)
# ─────────────────────────────────────────

def update_visual_index(
    name: str, raw_catalog_items: list, threshold: float = DEFAULT_THRESHOLD
) -> dict[str, str]:
    """Called from track_sales during a run: embed any catalog listings not yet
    known, assign them stable visual-variant ids, persist. Returns the full
    {listing_id: cluster_id} map.

    Must run AT FETCH TIME because Vinted photo URLs expire — the cover image of
    a listing first seen today may be un-downloadable next week. Embedding is
    incremental: already-assigned listings cost nothing.
    """
    listings = listings_from_catalog(raw_catalog_items)
    index = VisualVariantIndex(name, threshold)
    todo = [ls for ls in listings if str(ls.id) not in index.assignments]
    if not todo:
        print(f"🖼️  visual index: all {len(listings)} listings already assigned")
        return index.assignments

    print(f"🖼️  visual index: {len(todo)} new listings to embed "
          f"({len(index.assignments)} already known)")
    cache = EmbeddingCache(name)
    embed_listings(todo, cache)
    index.assign_batch(todo)
    index.save()
    return index.assignments


# ─────────────────────────────────────────
# LIVE FETCH (reuses fetch_results Chrome/CDP session)
# ─────────────────────────────────────────

def _anon_session():
    """Bootstrap an anonymous Vinted session: the homepage sets anon cookies
    INCLUDING an anonymous ``access_token_web`` that the catalog API accepts.

    Verified working 2026-07-06. This means Phase 5 catalog pulls don't need the
    logged-in Chrome profile at all (item-PAGE scraping still does — Phase 3/4).
    Returns (cookies_dict, token) or (None, None) on failure.
    """
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _UA["User-Agent"],
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
        }
    )
    try:
        r = s.get("https://www.vinted.fr/", timeout=30)
        if r.status_code != 200:
            return None, None
        token = s.cookies.get("access_token_web", "")
        return requests.utils.dict_from_cookiejar(s.cookies), token
    except Exception:
        return None, None


def fetch_live_listings(keyword: str, max_items: int, max_pages: int | None) -> list[Listing]:
    """Fetch the live catalog. Prefers the running Chrome (CDP) session; falls
    back to an anonymous session (works for the catalog API — no login needed)."""
    import fetch_results as fr

    cookies = token = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=3000)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            cookies, token = fr.get_cookies_and_token(context, page)
            print("🔑 using logged-in Chrome session (CDP)")
    except Exception:
        print("ℹ️  no Chrome on :9222 — using anonymous session (fine for catalog)")
        cookies, token = _anon_session()
        if cookies is None:
            print("❌ could not bootstrap an anonymous Vinted session either")
            sys.exit(2)

    raw = fr.fetch_catalog_via_requests(keyword, cookies, token, max_pages=max_pages)

    listings = listings_from_catalog(raw)
    if max_items and len(listings) > max_items:
        listings = listings[:max_items]  # catalog is newest-first
    print(f"📦 {len(listings)} listings with photos (of {len(raw)} catalog items)")
    return listings


# ─────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────

def summarize(listings: list[Listing], keyword: str) -> str:
    """Print cluster summary + write visual_variants_<keyword>.csv. Returns csv path."""
    import csv
    from collections import defaultdict

    clusters: dict[int, list[Listing]] = defaultdict(list)
    for ls in listings:
        clusters[ls.cluster].append(ls)

    # Order clusters by size (biggest visual variant first). Cosine distance is 1-sim.
    ordered = sorted(clusters.items(), key=lambda kv: -len(kv[1]))

    print("\n" + "=" * 64)
    print(f"VISUAL VARIANTS for '{keyword}'  —  {len(listings)} listings, "
          f"{len(clusters)} clusters")
    print("=" * 64)
    for rank, (cid, members) in enumerate(ordered, 1):
        prices = [m.price for m in members if m.price]
        avg = f"{sum(prices) / len(prices):.2f}€" if prices else "n/a"
        singleton = " (singleton)" if len(members) == 1 else ""
        print(f"\n#{rank}  cluster {cid}: {len(members)} listings  · avg {avg}{singleton}")
        for m in members[:5]:
            print(f"     • {m.title[:58]:58}  {m.price:>6.2f}€")
        if len(members) > 5:
            print(f"     … +{len(members) - 5} more")

    csv_path = f"visual_variants_{_safe(keyword)}.csv"
    # cluster_size lets the client sort by "biggest visual group" in a spreadsheet.
    size_by_cid = {cid: len(members) for cid, members in clusters.items()}
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["cluster_id", "cluster_size", "listing_id", "title", "price",
                    "brand", "photo_url"])
        for rank, (cid, members) in enumerate(ordered, 1):
            for m in members:
                w.writerow([rank, size_by_cid[cid], m.id, m.title, m.price,
                            m.brand, m.photo_url])
    print(f"\n💾 wrote {csv_path}")
    return csv_path


# ─────────────────────────────────────────
# SELF-TEST (no Chrome needed) — proves embed+cluster discriminates
# ─────────────────────────────────────────

def _wikimedia_images(term: str, n: int) -> list[str]:
    api = "https://commons.wikimedia.org/w/api.php"
    p = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {term}", "gsrnamespace": 6,
        "gsrlimit": n, "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 400,
    }
    try:
        r = requests.get(api, params=p, headers=_UA, timeout=25).json()
    except Exception:
        return []
    urls = []
    for pg in r.get("query", {}).get("pages", {}).values():
        ii = (pg.get("imageinfo") or [{}])[0]
        u = ii.get("thumburl") or ii.get("url")
        if u:
            urls.append(u)
    return urls


def selftest():
    """Embed a few images each of 3 distinct products and show that intra-product
    similarity >> inter-product similarity, and that clustering recovers the groups."""
    print("🔬 Phase 5 self-test: does CLIP separate distinct products?\n")
    groups = {
        "crocs": _wikimedia_images("crocs clog shoe footwear", 4),
        "tumbler": _wikimedia_images("stanley quencher tumbler cup", 4),
        "sneaker": _wikimedia_images("running sneaker nike shoe", 4),
    }
    urls, truth = [], []
    for label, us in groups.items():
        for u in us:
            urls.append(u)
            truth.append(label)
    print(f"   fetched {len(urls)} images across {len(groups)} products")

    imgs = [download_image(u) for u in urls]
    keep = [i for i, im in enumerate(imgs) if im is not None]
    imgs = [imgs[i] for i in keep]
    truth = [truth[i] for i in keep]
    emb = embed_images(imgs)

    sim = emb @ emb.T
    import itertools
    intra, inter = [], []
    for i, j in itertools.combinations(range(len(truth)), 2):
        (intra if truth[i] == truth[j] else inter).append(float(sim[i, j]))
    print(f"\n   same-product cosine sim:  mean {np.mean(intra):.3f}  "
          f"(min {np.min(intra):.3f})")
    print(f"   diff-product cosine sim:  mean {np.mean(inter):.3f}  "
          f"(max {np.max(inter):.3f})")
    gap = np.mean(intra) - np.mean(inter)
    print(f"   separation gap: {gap:+.3f}  "
          f"{'✅ CLIP separates products' if gap > 0.05 else '⚠️ weak separation'}")

    from collections import Counter

    def _report(thr):
        labels = cluster_embeddings(emb, thr)
        print(f"\n   clustering @ threshold {thr:.3f}: "
              f"{len(set(labels))} clusters for {len(labels)} images")
        pure = 0
        for lab in sorted(set(labels)):
            members = [truth[i] for i in range(len(truth)) if labels[i] == lab]
            c = Counter(members)
            if len(c) == 1:
                pure += 1
            print(f"     cluster {lab}: {dict(c)}")
        print(f"     → {pure}/{len(set(labels))} clusters are single-product (pure)")

    # The default is the near-duplicate threshold (right for Vinted, where same-product
    # listing photos look alike). On these deliberately-diverse stock photos it is
    # too strict, so also show a threshold calibrated to THIS data's similarity scale.
    _report(DEFAULT_THRESHOLD)
    calib = (float(np.mean(intra)) + float(np.mean(inter))) / 2
    print(f"\n   — same run, threshold calibrated to test data ({calib:.3f}) —")
    _report(calib)
    print("\n   Takeaway: machinery runs; CLIP separates products (positive gap);")
    print("   the merge threshold must be calibrated to the photo set. Vinted's")
    print("   same-product photos are far more alike than these stock images, so")
    print(f"   the live default ({DEFAULT_THRESHOLD}) is deliberately near-duplicate-strict.")


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Phase 5 — visual variant clustering")
    ap.add_argument("keyword", nargs="?", help="search keyword (live mode)")
    ap.add_argument("--max-items", type=int, default=400)
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="offline demo (no Chrome): prove embed+cluster works")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.keyword:
        ap.error("provide a keyword, or use --selftest")

    listings = fetch_live_listings(args.keyword, args.max_items, args.max_pages)
    if not listings:
        print("no listings with photos — nothing to cluster")
        return
    cache = None if args.no_cache else EmbeddingCache(args.keyword)
    embed_listings(listings, cache)
    clustered = cluster_listings(listings, args.threshold)
    if clustered:
        summarize(clustered, args.keyword)


if __name__ == "__main__":
    main()
