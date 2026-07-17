# Phase 5 — Image Recognition Suite (technical documentation)

Reference for the Phase 5 image-recognition tooling: what it does, how to run it, how it
works, how to extend it, and its honest limits. For the running design log and measured
experiments see `PHASE5_NOTES.md`; this file is the how-to.

> Status: built and validated on spec; **not yet a paid/delivered client feature.** Runs
> standalone via its CLIs and is **opt-in** inside the tracker. Currently tuned for Stanley.

---

## 1. What it does

Phase 3/4 decide "is this the same product?" from the **title text** (capacity + colour).
That fails on vague, photo-only or multilingual titles. Phase 5 adds three photo-based
capabilities:

| Capability | Module | Answers |
|---|---|---|
| **Visual clustering** | `image_cluster.py` | "Which listings show the *same* product?" (groups by photo, title-independent) |
| **Stable visual variants** | `image_cluster.py` (`VisualVariantIndex`) | Same, but with IDs that persist across tracking runs so turnover accumulates per product |
| **Exact identification** | `product_identify.py` | "*What* is this product?" → brand + line + colour → a complete, searchable title |

---

## 2. Install

Phase 5's dependencies are heavier than the core tool and are kept separate so a
Phase-1–4 setup isn't burdened by them:

```
pip install -r requirements-phase5.txt      # torch, transformers, pillow, scikit-learn, numpy
```

First run downloads the CLIP model weights (~600 MB) once, then caches them. No GPU
required (runs on CPU). No external API keys.

---

## 3. Files

| File | Role |
|---|---|
| `image_cluster.py` | CLIP model loading, image/text embedding, clustering, `VisualVariantIndex`, live fetch, CLI |
| `product_identify.py` | Exact identification: line prototypes, title generation, accessory guard, CLI |
| `requirements-phase5.txt` | Heavy deps (torch/transformers/etc.) |
| `phase5_cache/` | Caches (gitignored): embeddings, cluster index, line prototypes |
| `PHASE5_NOTES.md` | Design log + measured experiments |

Both modules reuse the Phase-4 text helpers in `track_sales.py`
(`canonical_capacity`, `detect_model`, `COLOR_BUCKETS`) via lazy import — there is no
import cycle (`track_sales` → `fetch_results`, never the reverse).

---

## 4. CLI usage

### 4.1 Visual clustering — `image_cluster.py`

```
python image_cluster.py "crocs"                       # cluster live listings for a keyword
python image_cluster.py "crocs" --max-items 400 --threshold 0.90
python image_cluster.py --selftest                    # offline sanity check, no Chrome
```

| Flag | Default | Meaning |
|---|---|---|
| `<keyword>` | — | Search term to fetch and cluster (live mode) |
| `--max-items` | 400 | Cap on listings embedded |
| `--max-pages` | none | Cap on catalog pages fetched |
| `--threshold` | 0.90 | Cosine similarity above which two photos are "the same product" |
| `--no-cache` | off | Ignore the embedding cache and re-embed |
| `--selftest` | off | Offline proof on a handful of Wikimedia images |

Output: a console cluster table + `visual_variants_<keyword>.csv`.

### 4.2 Exact identification — `product_identify.py`

```
python product_identify.py "stanley quencher rose"          # identify live listings
python product_identify.py --build stanley                  # (re)build the line prototypes
python product_identify.py --url <photo_url> --title "stanley cup 1.18L rose"
```

| Flag | Default | Meaning |
|---|---|---|
| `<keyword>` | `stanley quencher` | Search term to fetch and identify |
| `--build BRAND` | — | (Re)build the visual line prototypes for a brand, then exit |
| `--url URL` | — | Identify a single photo instead of a live search |
| `--title "..."` | "" | Listing title — supplies capacity and (preferred) colour |
| `--brand NAME` | from keyword | Brand override |
| `--max N` | 8 | How many live listings to identify |

On first live run it auto-builds prototypes if none are cached.

### 4.3 Inside the tracker (opt-in)

```
VINTED_VISUAL=1 python track_sales.py "crocs"
```

With `VINTED_VISUAL=1`, the tracker embeds new listings each run and shows `📷 vN`
visual variants in the report for listings the text pipeline can't group. Default off →
tracker behaviour is unchanged.

---

## 5. Environment variables

| Var | Default | Effect |
|---|---|---|
| `VINTED_CLIP_MODEL` | `openai/clip-vit-base-patch32` | Which CLIP model to load |
| `VINTED_CLIP_THRESHOLD` | `0.90` | Clustering same-product cosine threshold |
| `VINTED_CLIP_CACHE` | `phase5_cache` | Cache directory |
| `VINTED_CLIP_BATCH` | `16` | Embedding batch size |
| `VINTED_VISUAL` | unset | `1` turns on visual variants in `track_sales.py` |

---

## 6. How it works

### Clustering
Each listing's cover photo → CLIP image embedding (512-d, L2-normalised). Cosine
similarity ≥ threshold ⇒ same product. sklearn agglomerative clustering (average
linkage, `distance_threshold = 1 − threshold`). Threshold **0.90** was calibrated on 300
real Crocs listings (0.85 over-merged into one 108-item blob).

### Stable variant IDs
`VisualVariantIndex` keeps a persistent centroid per visual product. A new listing joins
the nearest centroid (cosine ≥ threshold, running-mean update) or founds a new ID
(`v1`, `v2`, …). IDs are assigned once and never renumbered, so per-variant turnover
history survives across runs. Persisted to `phase5_cache/index_<keyword>.npz`.

### Exact identification (the important one)
1. **Line** — *title-first*. If the seller's title names the line (`detect_model`), trust
   it (ground truth). Otherwise fall back to the **image**: nearest **line prototype**
   (a per-line average of photos from title-labelled listings), asserted only when the
   top-two prototype margin clears `IMAGE_MARGIN_MIN` — else left unstated.
2. **Colour** — title first (`COLOR_BUCKETS`), else zero-shot CLIP colour.
3. **Capacity** — **title only** (a photo can't show litres), unit-normalised (40oz ≡ 1.18L).
4. **Accessory guard** — a carry-all/sleeve/lid is not the product: title keyword gate
   (`ACCESSORY_TERMS`) + a conservative zero-shot bottle-vs-accessory image check.
5. **Title** — composed from the resolved fields, e.g.
   `Stanley Quencher H2.0 FlowState Tumbler Rose Quartz 40oz (1.18L)`.

Each result carries `line_source` (`title` / `image` / `unknown`) and `is_accessory` so a
caller knows how much to trust it.

**Why prototypes, not text prompts:** zero-shot CLIP against hand-written descriptions was
tried and failed to separate the look-alike straw tumblers (≈2/6). Image-to-image
prototypes (self-supervised from titled listings) reach 73% leave-one-out, and 91% on the
confident subset the margin gate keeps. See `PHASE5_NOTES.md` for the numbers.

---

## 7. Extending to a new brand

Add an entry to `BRAND_LINES` in `product_identify.py`:

```python
BRAND_LINES = {
    "YourBrand": {
        "official": {                       # detected line → official title text
            "LineA": "Official Line A Name",
            "LineB": "Official Line B Name",
        },
        "build_queries": [                  # searches that return title-labelled examples
            "yourbrand linea", "yourbrand lineb",
        ],
    },
}
```

Line detection itself comes from `track_sales.detect_model` / `_MODEL_PATTERNS` — add the
brand's line keywords there too. Then build the prototypes:

```
python product_identify.py --build yourbrand
```

Colour display names live in `COLOUR_DISPLAY` (brand-flavoured, e.g. Stanley pink =
"Rose Quartz").

---

## 8. Tuning knobs (in `product_identify.py`)

| Constant | Default | Raise to… |
|---|---|---|
| `IMAGE_MARGIN_MIN` | 0.010 | assert the line less often but more confidently |
| `PROTO_MIN_EXAMPLES` | 5 | require more examples before trusting a prototype |
| `ACCESSORY_MARGIN` | 0.04 | reject fewer things as accessories (fewer false positives) |
| `ACCESSORY_TERMS` | list | add title keywords for accessories in other languages |

---

## 9. Outputs & caches

| Path | What |
|---|---|
| `visual_variants_<keyword>.csv` | Clustering report (per listing → cluster) |
| `phase5_cache/emb_<keyword>.npz` | Cached image embeddings |
| `phase5_cache/index_<keyword>.npz` | Stable `VisualVariantIndex` |
| `phase5_cache/prototypes_<brand>.npz` | Line prototypes for identification |

All of `phase5_cache/` and `visual_variants_*.csv` are gitignored (regenerable).

---

## 10. Honest limitations

- **Brand-specific.** Only Stanley is configured. Other brands and generic products need a
  brand entry + prototypes, and generics need the (not-built) external-source lookup.
- **No official-source lookup.** "Official" names come from the hand-typed `BRAND_LINES`
  table, not from brand sites / Google / Amazon.
- **Capacity never comes from the photo** — only from the title when the seller states it.
- **Accuracy ceiling.** ~73% raw line accuracy on the hard look-alike tumblers; the margin
  gate trades coverage for precision (asserts on ~60% at ~91%), abstaining on the rest.
- **Colour-from-photo is unreliable** — prefer the title; the colour dictionary also misses
  some languages.
- **Not integrated.** `product_identify.py` is a standalone CLI; it does not yet feed the
  tracker, search, or variant report.

Getting past these (any brand, generic products, official names, higher accuracy for every
photo) is the scope of the paid Phase 5 build — realistically a multimodal vision LLM +
web lookup, which carries an API/per-image cost. This suite is the local, no-API
foundation that layer would replace or backstop.
