"""
Funnel measurement — how many listings each layer resolves, and what the AI layer costs.

Client question (before scaling to ~100k listings/day): on a real sample, how many listings
are solved by TITLE alone, how many by the LOCAL model, how many actually need the paid
VISION AI, and what does that cost?

Run:
    python funnel_measure.py --target 10000            # stage 1 (titles) only, fast
    python funnel_measure.py --target 10000 --sample 300   # + stages 2/3 (needs CLIP)

Stage 1 (titles) runs over every listing — it's free and fast. Stages 2/3 embed images, so
they run on a random SAMPLE of the title-unresolved listings and are extrapolated; the report
says so explicitly rather than implying full coverage.
"""

import os
import sys
import json
import random
import argparse
import collections

import fetch_results as fr
import track_sales as ts


# A resale-relevant spread, not just Stanley — so the funnel isn't flattered by one
# well-modelled brand. "Matured" = we have line patterns for it (Stanley today).
KEYWORDS = [
    "stanley quencher", "stanley cup", "stanley flip straw",
    "crocs", "nike air force 1", "lululemon leggings", "north face jacket",
    "ugg boots", "birkenstock", "longchamp bag", "zara dress", "dyson airwrap",
]
MATURED_BRANDS = {"stanley"}


def fetch_listings(target: int, max_pages: int) -> list:
    """Pull listings across keywords via the anonymous catalog API until we hit target."""
    cookies, token = fr._anon_session_for_domain("fr")
    seen, out = set(), []
    for kw in KEYWORDS:
        if len(out) >= target:
            break
        try:
            raw = fr.fetch_catalog_via_requests(
                fr.normalize_search_query(kw), cookies, token,
                max_pages=max_pages, stop_when_old_ratio=2.0,
            )
        except Exception as e:
            print(f"   ! {kw}: {type(e).__name__}")
            continue
        added = 0
        for it in raw:
            iid = it.get("id")
            if iid in seen:
                continue
            seen.add(iid)
            out.append(it)
            added += 1
        print(f"   {kw:24} +{added:4}  (total {len(out)})")
    return out[:target]


def classify_title(item: dict) -> dict:
    """What can we learn from the TITLE (+ free catalog brand) alone?"""
    title = item.get("title") or ""
    tl = title.lower()
    toks = fr._tokenize(tl)
    brand = (item.get("brand_title") or "").strip()
    line = ts.detect_model(title)
    cap = next((c for c in (ts.canonical_capacity(t) for t in toks) if c), "")
    col = next((ts.COLOR_BUCKETS[t] for t in toks if t in ts.COLOR_BUCKETS), "")
    matured = brand.lower() in MATURED_BRANDS
    # "Resolved by title" = we know the brand AND can pin the specific product:
    # either the line is named, or (for products where size+colour defines the variant)
    # both of those are present.
    resolved = bool(brand) and (bool(line) or (bool(cap) and bool(col)))
    return {"brand": brand, "line": line, "cap": cap, "col": col,
            "matured": matured, "resolved": resolved}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--max-pages", type=int, default=12)
    ap.add_argument("--sample", type=int, default=0,
                    help="how many title-unresolved listings to run through the local model")
    ap.add_argument("--out", default="funnel_report.json")
    args = ap.parse_args()

    print(f"Fetching up to {args.target} listings…")
    items = fetch_listings(args.target, args.max_pages)
    total = len(items)
    if not total:
        print("no listings fetched")
        sys.exit(1)

    # ── STAGE 1: title layer (every listing, free) ──
    facts = [classify_title(i) for i in items]
    resolved = [f for f in facts if f["resolved"]]
    unresolved_idx = [i for i, f in enumerate(facts) if not f["resolved"]]
    matured = [f for f in facts if f["matured"]]
    matured_resolved = [f for f in matured if f["resolved"]]

    print()
    print("=" * 66)
    print(f"FUNNEL — measured on {total} real listings")
    print("=" * 66)
    print(f"  STAGE 1  title only (free)      : {len(resolved):5}  "
          f"({round(100*len(resolved)/total)}%)")
    if matured:
        print(f"           └ of a matured brand    : {len(matured_resolved)}/{len(matured)} "
              f"({round(100*len(matured_resolved)/len(matured))}%) — brands whose model "
              f"lines we've configured")
    print(f"  remaining for the image layers  : {len(unresolved_idx):5}  "
          f"({round(100*len(unresolved_idx)/total)}%)")

    report = {
        "total_listings": total,
        "stage1_title_resolved": len(resolved),
        "stage1_pct": round(100 * len(resolved) / total, 1),
        "matured_brand_listings": len(matured),
        "matured_brand_resolved": len(matured_resolved),
        "remaining_after_title": len(unresolved_idx),
    }

    # ── STAGES 2/3: local model + dedup, on a SAMPLE (embedding is the slow part) ──
    if args.sample and unresolved_idx:
        import image_cluster as ic
        import product_identify as pi
        import numpy as np

        random.seed(0)
        pick = random.sample(unresolved_idx, min(args.sample, len(unresolved_idx)))
        sample_items = [items[i] for i in pick]
        urls = [ic._photo_url(it) for it in sample_items]
        pairs = [(it, u) for it, u in zip(sample_items, urls) if u]
        print(f"\n  sampling {len(pairs)} of the {len(unresolved_idx)} remaining "
              f"for the local model…")

        imgs = [ic.download_image(u) for _, u in pairs]
        embs = ic.embed_images(imgs)
        ok = [i for i in range(len(pairs)) if np.any(embs[i])]
        E = embs[ok]

        protos = pi.load_line_prototypes("Stanley")
        confident = 0
        for i in ok:
            # accessory guard first — those are dropped, not sent to the AI
            if pi.is_accessory_title(pairs[i][0].get("title") or ""):
                confident += 1
                continue
            line, _margin = pi._line_from_image(embs[i], protos) if protos else ("", 0)
            if line:
                confident += 1
        local_rate = confident / len(ok) if ok else 0

        # Dedup: how many DISTINCT products are in the residual? This is the real cost
        # driver — we pay per distinct product, not per listing.
        residual = [embs[i] for i in ok
                    if not (protos and pi._line_from_image(embs[i], protos)[0])]
        distinct_ratio = 1.0
        if len(residual) > 1:
            labels = ic.cluster_embeddings(np.stack(residual), ic.DEFAULT_THRESHOLD)
            distinct_ratio = len(set(labels)) / len(residual)

        print(f"  STAGE 2  local model (free)     : {round(100*local_rate)}% of the remainder")
        print(f"  STAGE 3  needs vision AI        : {round(100*(1-local_rate))}% of the remainder")
        print(f"  dedup    distinct products      : {round(100*distinct_ratio)}% of that "
              f"(the rest are repeats we've already identified)")

        report.update({
            "sample_size": len(ok),
            "stage2_local_rate": round(local_rate, 3),
            "stage3_vision_rate": round(1 - local_rate, 3),
            "distinct_ratio": round(distinct_ratio, 3),
        })

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\n  saved → {args.out}")


if __name__ == "__main__":
    main()
