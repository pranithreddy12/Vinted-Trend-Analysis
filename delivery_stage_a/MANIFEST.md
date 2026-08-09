# Phase 5 — Stage A delivery package

**Date:** 3 August 2026 · **For:** leslie570

This package contains everything for Phase 5, Stage A (AI product identification): the offer, the
documentation (English & French), and the source code.

## Contents

**Documents (read these first)**
- `PHASE5_STAGE_A_OFFER_EN.md` / `PHASE5_STAGE_A_OFFER_FR.md` — the offer.
- `PHASE5_STAGE_A_DOCUMENTATION_EN.md` / `PHASE5_STAGE_A_DOCUMENTATION_FR.md` — how it works, how
  to enable and run it, outputs, cost control, limits.

**Source code (`src/`)**
- `vision_identify.py` — Stage A core: the vision-AI identifier + title composer + per-listing
  cache + cost controls.
- `eval_vision.py` — accuracy test and the visual (`--html`) photo reports.
- `image_cluster.py` — Phase 5 image/embedding foundation (used by the above).
- `product_identify.py` — the earlier local (CLIP) identifier; still used for helpers.
- `track_sales.py` — the sales-tracking engine, with the Stage A hooks integrated
  (`VINTED_VISION=1`) and colour/size taken from the listing.
- `fetch_results.py` — the base catalog/scraping engine everything builds on.
- `requirements.txt` — core dependencies. `requirements-phase5.txt` — the extra AI/vision
  dependency (`anthropic`), only needed with identification enabled.

## Quick start

```
pip install -r requirements.txt
pip install -r requirements-phase5.txt
# then follow section 4 of the documentation to set your API key and run
```

## Note on versions

The authoritative, always-current source is the Git branch
`phase-5-image-recognition` in the project repository — a simple `git pull` gets the latest.
The `src/` files here are a snapshot of that branch at delivery, provided for convenience; if you
already work from the repository, these replace your existing copies of the same filenames.

---

*FR — Ce dossier contient l'offre, la documentation (anglais & français) et le code source de la
Phase 5, Étape A. Commencez par les documents ; le code se trouve dans `src/`. La source de
référence reste la branche Git `phase-5-image-recognition` (`git pull` pour la dernière version).*
