"""
Phase 5 Stage A — vision accuracy eval (the client's acceptance bar).

Measures how well the vision model identifies the product LINE from the PHOTO ALONE
(title hidden), against the title as ground truth — a fair, like-for-like comparison to
the local CLIP prototype's 73% raw / 91%-confident number.

Ground truth: listings whose title names a line (via ts.detect_model). We hide that title
from the model, ask it to identify from the image, and compare. Runs on whatever provider
VINTED_VISION_PROVIDER selects (stub → trivial 0%; anthropic → the real number, costs a few
cents on the configured key). Cap the sample with --n to bound spend.

    python eval_vision.py "stanley quencher" --n 25
"""

import sys
import argparse
import collections

import image_cluster as ic
import track_sales as ts
import fetch_results as fr
import vision_identify as vi


def run(keyword: str, n: int, pages: int = 2) -> dict:
    c, t = ic._anon_session()
    raw = fr.fetch_catalog_via_requests(
        fr.normalize_search_query(keyword), c, t, max_pages=pages
    )
    seen, labelled = set(), []
    for it in raw:
        iid = it.get("id")
        if iid in seen:
            continue
        seen.add(iid)
        line = ts.detect_model(it.get("title", ""))
        url = ic._photo_url(it)
        # ground-truth = title names a real line, has a photo, isn't an accessory listing
        try:
            import product_identify as pi
            acc = pi.is_accessory_title(it.get("title", ""))
        except Exception:
            acc = False
        if line and url and not acc:
            labelled.append((line, url))
        if len(labelled) >= n:
            break

    provider = vi.get_provider()
    print(f"eval: {len(labelled)} title-labelled photos · provider="
          f"{vi.os.environ.get('VINTED_VISION_PROVIDER', 'stub')}")
    correct = asserted = 0
    conf = collections.Counter()
    for true_line, url in labelled:
        r = provider.identify(url, title_hint="")   # TITLE HIDDEN — pure photo test
        pred = (r.get("product_line") or "").strip()
        conf[r.get("confidence", "?")] += 1
        if pred:
            asserted += 1
            correct += pred.lower() == true_line.lower()

    acc = round(100 * correct / asserted) if asserted else 0
    cov = round(100 * asserted / len(labelled)) if labelled else 0
    print("─" * 56)
    print(f"  photos tested (title hidden) : {len(labelled)}")
    print(f"  model asserted a line        : {asserted}/{len(labelled)} ({cov}% coverage)")
    print(f"  correct of those asserted    : {correct}/{asserted} ({acc}%)")
    print(f"  confidence spread            : {dict(conf)}")
    print(f"  baseline (CLIP prototype)    : ~73% raw / 91% on the confident subset")
    print("─" * 56)
    return {"tested": len(labelled), "asserted": asserted, "correct": correct,
            "accuracy_pct": acc, "coverage_pct": cov}


def main():
    ap = argparse.ArgumentParser(description="Stage A vision accuracy eval")
    ap.add_argument("keyword", nargs="?", default="stanley quencher")
    ap.add_argument("--n", type=int, default=25, help="photos to test (bounds spend)")
    ap.add_argument("--pages", type=int, default=2)
    args = ap.parse_args()
    if args.keyword:
        sys.stdout.reconfigure(encoding="utf-8")
    run(args.keyword, args.n, args.pages)


if __name__ == "__main__":
    main()
