# Phase 5 — Visual Variant Recognition (dev notes)

> Internal notes. Status 2026-07-06: clustering PoC + stable cross-run index + Phase 4
> integration built & calibrated. **2026-07-14: exact product IDENTIFICATION added
> (`product_identify.py`) — photo → complete searchable title.** No Chrome needed for
> catalog pulls (see below).

## Exact product identification (`product_identify.py`, 2026-07-14)

Goal (client): from a listing photo, produce a complete, Google-verifiable title —
"Stanley Quencher H2.0 FlowState Tumbler Rose Quartz 40oz (1.18L)" — not just a variant.

**What was tried and measured (don't repeat the dead end):**
- **Zero-shot CLIP vs text prompts — FAILED.** Scoring a photo against hand-written
  descriptions ("a tall tumbler with a handle and a straw") gave ≈2/6 on Quencher, 0/6
  on Flip Straw. Base CLIP (ViT-B/32) can't separate the look-alike straw tumblers from
  a text phrase. Abandoned.
- **Image-to-image PROTOTYPES — WORKS (73%).** Embed listings whose TITLES already name
  the line (self-supervised labels via `ts.detect_model`), average into one centroid per
  line, classify an unknown photo by nearest centroid. Leave-one-out accuracy **73%**
  across 4 Stanley lines (226 real photos). Confusions are the visually-similar tumblers
  (Quencher↔Flip Straw↔IceFlow); Classic (green thermos) separates cleanly.

**Accessory guard (2026-07-14):** carry-all bags, insulated sleeves, lids and straws are
not the product but slipped through — a bag titled "All Day Quencher Carry-All" (title has
the line word) and a "Housse isotherme" sleeve (photo shows a bottle inside). Two-layer
guard: a multilingual title-keyword gate (`ACCESSORY_TERMS`) + a coarse zero-shot
accessory-vs-bottle image check (`_is_accessory_image` — CLIP handles bottle-vs-bag easily,
unlike the fine line-vs-line call). Flagged listings get `is_accessory=True` and are skipped
from the product view rather than mislabelled.

**Design = TITLE-FIRST, image as the fallback:**
- Line: from the title when the seller names it (ground truth); the image only fills the
  line when the title is silent AND the top-2 prototype margin clears `IMAGE_MARGIN_MIN`
  — otherwise we leave it unstated rather than guess. `line_source` = title|image|unknown.
- Colour: title first, else zero-shot CLIP colour. Capacity: **title only** (a photo
  can't show litres — verified with the client's own two examples, whose titles carry no
  capacity).
- Prototypes are cached (`phase5_cache/prototypes_<brand>.npz`), rebuilt with `--build`.

**Measured end-to-end (title hidden, image-only, 136 held-out real listings):** the
margin gate makes it abstain when unsure — it asserts a line on **60%** of them and is
**91% correct** on those; the other 40% fall back to unstated rather than guess. With
title-first on top (the ~47% of listings that name the line = 100% ground truth), most
listings get a confident, ~correct line and the rest are honestly left blank, not wrong.

**Honest ceiling (told to client):** a useful foundation, not "exact for every photo".
Production-grade exact ID of any photo needs a stronger vision model (a multimodal LLM +
official-source lookup) — a real API/cost dependency. This local module is the no-API
foundation that layer would backstop. Not yet wired into the tracker/search — it's a
standalone capability + CLI for now.

## What Phase 5 does

Phases 3/4 decide "is this the same product variant?" from the **title text** (capacity +
colour tokeniser). That fails whenever the seller writes a vague / photo-only / multilingual
title — a large share of real listings ("Crocs rose 39", "Sabot femme", "Platform pink").

Phase 5 groups listings by what the **photo** shows instead. Each listing's cover image is
embedded with CLIP (`openai/clip-vit-base-patch32`), embeddings are compared by cosine
similarity, and listings whose photos look like the same product are clustered together —
independent of the title. This is what lets turnover/estimated-sales aggregate correctly for
products the text pipeline can't line up (especially clothing/footwear).

## File

`image_cluster.py` — standalone module. Reuses `fetch_results.py` for the Chrome/CDP session,
cookies + bearer token, and the catalog API. Does not modify Phase 3/4 code.

Key pieces:
- `embed_images()` — CLIP image encoder → L2-normalised vectors (batched, CPU).
- `EmbeddingCache` — persists **listing_id → vector** under `phase5_cache/`. Keyed by listing
  id, **not** URL, because Vinted CDN URLs expire (see gotcha below). Lets repeated tracking
  runs skip re-downloading/re-embedding.
- `cluster_embeddings()` — sklearn `AgglomerativeClustering`, cosine distance, average linkage,
  `distance_threshold = 1 - similarity_threshold`. No need to know the number of variants ahead.
- **`VisualVariantIndex`** — stable cross-run variant IDs. One-shot clustering renumbers labels
  every run (would reset turnover history), so the index keeps persistent cluster CENTROIDS:
  a new listing joins the best-matching cluster (cosine ≥ threshold, running-mean centroid
  update) or founds a new one ("v1", "v2", … assigned once, never renumbered). Persisted per
  keyword as `phase5_cache/index_<slug>.npz` with a human label per cluster (founding title).
- **`update_visual_index(slug, raw_catalog_items)`** — the track_sales hook: embeds only NEW
  listings, assigns stable IDs, saves. Incremental — already-known listings cost nothing.
- **`load_visual_assignments(slug)`** — cheap read-only load (numpy only, no torch) used by
  `track_sales.variant_analysis`.
- `fetch_live_listings()` — prefers the CDP Chrome session, **falls back to an anonymous
  session** → catalog → `Listing` objects with cover photo URL.
- `summarize()` — console cluster table + `visual_variants_<keyword>.csv`.
- `selftest()` — offline proof (no Chrome), pulls a few Wikimedia images of distinct products.

## Phase 4 integration (track_sales.py)

- `variant_analysis(tracking, visual_slug=<keyword slug>)`: listings where `build_variant()`
  returns "" (no capacity+colour in the title) now fall back to their visual cluster and
  appear as `📷 v<N> · <founding title>` variants with the full metric set (est sales/30d,
  velocity, competition, trend, price, confidence). **Text variants stay authoritative** —
  the photo only catches what text would drop. With no index file, behaviour is byte-identical
  to before (regression-checked against the real stanley_quencher tracking data, 89 variants).
- `main()`: when **`VINTED_VISUAL=1`**, each run calls `update_visual_index()` right after the
  corruption guard (never embeds a failed/partial fetch). Opt-in so automation doesn't pay the
  torch/download cost until we enable it.
- **Measured coverage gap this closes: 79% of tracked stanley_quencher listings (1,447/1,825)
  have no text-derivable variant** — they were invisible to Phase 4 analysis until now.

## Anonymous catalog access (discovered 2026-07-06)

The Vinted homepage hands an anonymous `access_token_web` cookie to a plain `requests`
session, and `/api/v2/catalog/items` accepts it (verified live: 200, full items with photo
URLs). So **Phase 5 catalog pulls + image downloads need no logged-in Chrome at all** —
`_anon_session()` in `image_cluster.py`. Item-PAGE scraping (offers, publish time) still
needs the logged-in profile; the tracker keeps using it.

## How to run

```bash
# Offline sanity check (no Chrome) — proves the embed+cluster machinery works:
python image_cluster.py --selftest

# Live PoC — uses the CDP Chrome session if up, else an anonymous session (no login needed):
python image_cluster.py "crocs" --max-items 400
#   → prints visual clusters, writes visual_variants_crocs.csv

# Tracking runs with visual variants enabled (embeds new listings each run):
VINTED_VISUAL=1 python track_sales.py "crocs"
```

Env knobs: `VINTED_CLIP_MODEL`, `VINTED_CLIP_THRESHOLD`, `VINTED_CLIP_BATCH`,
`VINTED_CLIP_CACHE`.

## Validation done (2026-07-06, offline)

- Dependency stack loads: `torch 2.12 (CPU)`, `transformers 4.57`, `Pillow`, `sklearn`, `numpy`.
- CLIP weights download + cache; model load ~5s after first run.
- Real images embed; cosine similarity **separates distinct products** (same-product mean
  ≈ 0.55 vs different-product mean ≈ 0.42 on deliberately-diverse Wikimedia stock photos —
  a positive gap on a hard set; Vinted's same-product photos are far more alike).
- Clustering recovers groups; rigid products (tumblers) separate cleanly from footwear.
- Catalog parsing handles all Vinted `photo` field shapes and drops photoless items.

## Live calibration (DONE 2026-07-06, 300 real Crocs listings, anonymous session)

Threshold sweep on cached embeddings (`phase5_cache/emb_crocs.npz`):

| threshold | clusters | biggest | listings grouped (≥2) |
|---|---|---|---|
| 0.85 | 126 | **108 (mega-cluster)** | 211 |
| 0.88 | 168 | 31 | 172 |
| **0.90** | **202** | **10** | **142** |
| 0.92 | 248 | 6 | 84 |
| 0.94 | 270 | 5 | 52 |

Findings:
- **0.85 over-merges**: shape dominates colour in CLIP space, so "every classic clog" fused
  into one 108-listing cluster. **Default moved to 0.90** (`VINTED_CLIP_THRESHOLD` overrides).
- **Flagship win @0.90**: the Cars/McQueen edition clustered across FOUR languages with no
  shared keywords — "Crocs Sabot Classici Rossi", "Mc Queen crocs", "Original Crocs Cars
  Edition", "Crocs Cars Taille 38/39 (FlashMcQueen)". Exactly the client's use-case.
- Same-seller charm lots with template photos also group perfectly (a correct-but-noisy case:
  jibbitz aren't crocs; see refinements).
- **Known impurity**: some clusters still mix colours of the same model (CLIP under-weights
  colour). Acceptable for v1; refinement below.

## Refinements (Phase 5.1 candidates)

1. **Colour purity check**: after CLIP clustering, split clusters whose members' dominant-colour
   histograms disagree (cheap, PIL-only) — fixes the colour mixing without losing shape recall.
2. Filter accessory listings (jibbitz/pins/charms — `fetch_results.EXCLUDE_TERMS`) from the
   standalone report; currently kept for listing-set parity with track_sales.
3. Multi-photo strategy (embed 2–3 photos per listing, take max similarity) if cover-photo-only
   misses angled shots.
4. `VINTED_VISUAL=1` default-on in `run_tracker` once a few supervised runs look clean.

## Gotcha discovered this session

**Vinted CDN photo URLs expire** (the old `item.html` fixture URLs now 404). So images must be
embedded **at fetch time** while the URL is fresh, and we cache the **vector**, not the URL.
The cache is keyed by stable listing id. This also means you can't rebuild embeddings later
from stored URLs — the tracking runs have to embed as they go.
