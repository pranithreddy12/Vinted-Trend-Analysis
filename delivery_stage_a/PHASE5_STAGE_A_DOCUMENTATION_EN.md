# Phase 5 — Stage A: AI Product Identification
### Delivery documentation (English)

**Date:** 3 August 2026
**Component:** Vision-AI product identification for the Vinted market-intelligence tool
**Status:** Delivered & validated

---

## 1. What Stage A does

Stage A adds **AI image recognition** on top of the existing sales-tracking engine. Given a
listing **photo**, it identifies the exact product and produces a **complete, specific title** —
for example, from a blurry photo titled only *"Gourde Stanley"* it returns:

> **Stanley Quencher H2.0 FlowState Tumbler — Rose — 40oz (1.18L)**

This solves the core problem that listing titles are inconsistent, multilingual, and often vague:
the tool can now group and analyse products by **what they actually are**, not by whatever text
the seller happened to type.

---

## 2. Measured accuracy

The identification was tested on a **photo-only** basis — the listing title was **hidden** from
the AI, so it worked purely from the image — and scored against the real title as ground truth:

| Metric | Vision-AI (Stage A) | Previous local prototype |
|---|---|---|
| Correct product-line identification | **96%** | ~73% |
| Coverage (items it can identify) | **96%** | lower |

The AI both identifies **more** items and identifies them **more accurately** than the earlier
approach. You can reproduce this number yourself at any time (see §5).

---

## 3. Where the data comes from (accuracy by design)

To keep titles reliable, each attribute is taken from its most trustworthy source:

- **Product line/model** (Quencher, Flip Straw, Polo Shirt…) → from the **photo** (the AI). This
  is what the AI is genuinely reliable at.
- **Colour** → from the **listing's own declared colour** (Vinted makes colour mandatory), *not*
  from guessing the photo. This removed the earlier weakness where a coral tumbler could be
  mislabelled.
- **Size (clothing)** → from the **listing's mandatory size field**, added automatically
  (e.g. *"Ralph Lauren Polo Shirt — S"*).
- **Capacity (bottles)** → read from the **title text** (a photo cannot show litres).

In short: the AI identifies the *product*; the *attributes* come from the listing's own mandatory
fields. This is why the titles are dependable.

---

## 4. How to enable it

Stage A is **opt-in** and off by default, so nothing changes in your normal runs until you turn
it on.

**One-time setup** (your own Anthropic API key — the key stays on your machine, it is never
stored in the code):

```
pip install -U anthropic
setx ANTHROPIC_API_KEY   "sk-ant-..."           # your key
setx ANTHROPIC_BASE_URL  "https://api.anthropic.com"
setx VINTED_VISION_PROVIDER "anthropic"
```

Then open a **new** terminal window (so the variables load) before running.

**Run the tracker with identification on:**

```
setx VINTED_VISION 1
python track_sales.py "stanley quencher"
```

Each run identifies the products it hasn't seen before and writes the results (see §6).

---

## 5. Checking the accuracy yourself

Photo-only accuracy test (title hidden), a few cents on your key:

```
python eval_vision.py "stanley quencher" --n 25
```

Visual report — a self-contained HTML page showing each photo next to what the AI said, with a
HIT/MISS badge (openable in any browser, easy to share):

```
python eval_vision.py "stanley quencher" --show --n 25 --html stanley_report.html
```

---

## 6. What it produces

- **`product_identities_<product>.csv`** — one row per listing: the generated full title, brand,
  product line, category, colour, size, and the AI's confidence.
- **`variant_report_<product>.csv`** — gains an **`ai_product`** column: the dominant
  AI-identified product name for each variant, alongside the existing sales/velocity/competition
  metrics.
- **HTML photo reports** (via `--html`) for visual review.

---

## 7. Cost control

Cost scales with **new, distinct products**, not with the number of listings:

- **Per-listing cache** — each listing is identified **once, ever**. Repeated runs re-use the
  saved result and cost nothing.
- **Demand-first order** — the budget is spent on the most in-demand items first (by likes).
- **Per-run cap** — `VINTED_VISION_MAX_NEW` limits how many new identifications happen per run.
- **Your account spend cap** (the ~$20 you set) is the hard ceiling.

---

## 8. Honest scope & limitations

- The AI reliably identifies the **product line**. It does **not** reconstruct the exact marketing
  colourway name (e.g. it gives *"Pink"*, not *"Rose Quartz"*) — colour is taken canonically from
  the listing, which is always correct and ideal for grouping.
- **Confidence is a strong signal but not a guarantee** — a small number of confident
  mis-identifications can occur (measured: ~1 in 25).
- **Generic, no-brand products** (e.g. unbranded "dog stairs") are described (category + colour +
  size) but not matched to a specific reference product. Deeper generic identification with
  external lookups is **Stage B**.
- The optional **automatic Google-verification** of titles is built but inactive — it needs a
  search API key to switch on.

---

## 9. Requirements summary

- Your own **Anthropic API key** with a billing/spend cap (you keep control of the cost).
- `anthropic` Python package (`pip install -U anthropic`, version 0.69 or newer).
- No Chrome/login needed for identification (the catalog is read anonymously).
- Ongoing per-image AI cost is small and capped; billed via your key. This is handled as part of
  the monthly running cost.

---

*Prepared for leslie570 — Phase 5, Stage A delivery.*
