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
        # The model names the line more fully than detect_model's bare label
        # (e.g. "Quencher H2.0 FlowState Tumbler" vs "Quencher"), so match by
        # containment against the model's line + official name, not exact ==.
        pred = f"{r.get('product_line') or ''} {r.get('official_name') or ''}".strip()
        conf[r.get("confidence", "?")] += 1
        if pred:
            asserted += 1
            correct += true_line.lower() in pred.lower()

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


def _write_html(keyword: str, rows: list, path: str) -> None:
    """Self-contained photo report: each card shows the (base64-embedded) photo, the AI's
    title, confidence, and HIT/MISS vs the hidden listing title. Opens in any browser."""
    cards = []
    for row in rows:
        badge = {"HIT": "#137333", "MISS": "#c5221f", "": "#5f6368"}[row["mark"]]
        label = row["mark"] or "no ground truth"
        img = f'<img src="{row["img"]}">' if row["img"] else "<div class=noimg>photo n/a</div>"
        cards.append(f"""<div class=card>{img}
      <div class=badge style="background:{badge}">{label} · {row['conf']}</div>
      <div class=ai>{row['name']}</div>
      <div class=truth>listing title (hidden from AI):<br>{row['title']}</div></div>""")
    html = f"""<!doctype html><meta charset=utf-8><title>Vision test — {keyword}</title>
<style>body{{font-family:system-ui,Arial;margin:24px;background:#fafafa}}
h1{{font-size:18px}}.grid{{display:flex;flex-wrap:wrap;gap:16px}}
.card{{width:220px;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:10px}}
.card img,.noimg{{width:200px;height:200px;object-fit:contain;background:#f1f1f1;border-radius:4px;display:flex;align-items:center;justify-content:center;color:#999}}
.badge{{color:#fff;font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;display:inline-block;margin:8px 0 4px}}
.ai{{font-weight:600;font-size:13px}}.truth{{font-size:11px;color:#666;margin-top:6px}}</style>
<h1>Photo-only identification — “{keyword}” ({len(rows)} items, listing titles hidden from the AI)</h1>
<div class=grid>{''.join(cards)}</div>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n📄 wrote photo report → {path}")


def show(keyword: str, n: int, pages: int = 2, html: str = "") -> None:
    """Qualitative dump: print what the model says for each photo (title hidden).

    Works for ANY keyword — branded or generic no-brand ("grey dog stairs"). When
    the title happens to name a known line (detect_model), we annotate HIT/MISS;
    otherwise there's no ground truth, so we just show the model's raw call.
    Pass html=PATH to also write a self-contained photo report to share.
    """
    c, t = ic._anon_session()
    raw = fr.fetch_catalog_via_requests(
        fr.normalize_search_query(keyword), c, t, max_pages=pages
    )
    provider = vi.get_provider()
    print(f"show: {keyword!r} · provider="
          f"{vi.os.environ.get('VINTED_VISION_PROVIDER', 'stub')} · title HIDDEN\n")
    seen = set()
    shown = 0
    rows = []
    for it in raw:
        iid = it.get("id")
        if iid in seen:
            continue
        seen.add(iid)
        url = ic._photo_url(it)
        if not url:
            continue
        r = provider.identify(url, title_hint="")
        true_line = ts.detect_model(it.get("title", ""))
        # branded → official name; generic (Stage B) → descriptor built from photo only
        # (title_hint="" so nothing leaks from the hidden listing title).
        name = r.get("official_name") or vi.compose_title(r, "") or "(none)"
        mark = ""
        if true_line:
            hit = true_line.lower() in f"{r.get('product_line') or ''} {name}".lower()
            mark = "HIT" if hit else "MISS"
        acc = " ·ACCESSORY" if r.get("is_accessory") else ""
        printed = f"  [{mark}] " if mark else "  "
        print(f"{printed:8} {r.get('confidence','?'):6} | {name}{acc}")
        print(f"         listing title (hidden from model): {it.get('title','')!r}")
        if html:
            blk = vi._image_block(url)
            data = (f"data:{blk['source']['media_type']};base64,{blk['source']['data']}"
                    if blk else "")
            rows.append({"img": data, "mark": mark, "conf": r.get("confidence", "?"),
                         "name": name + acc, "title": it.get("title", "")})
        shown += 1
        if shown >= n:
            break
    if html and rows:
        _write_html(keyword, rows, html)


def main():
    ap = argparse.ArgumentParser(description="Stage A vision accuracy eval")
    ap.add_argument("keyword", nargs="?", default="stanley quencher")
    ap.add_argument("--n", type=int, default=25, help="photos to test (bounds spend)")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--show", action="store_true",
                    help="print each identification (hits + misses) instead of the score")
    ap.add_argument("--html", default="", metavar="PATH",
                    help="with --show: also write a self-contained photo report to PATH")
    args = ap.parse_args()
    if args.keyword:
        sys.stdout.reconfigure(encoding="utf-8")
    if args.show:
        show(args.keyword, args.n, args.pages, args.html)
    else:
        run(args.keyword, args.n, args.pages)


if __name__ == "__main__":
    main()
