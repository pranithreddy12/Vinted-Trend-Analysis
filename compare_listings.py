"""
Compare listings side by side — pull each listing's photo so you can eyeball whether two rows in
product_identities are really the SAME product (different sellers) or genuinely different.

    python compare_listings.py 7795567524 7770621887
    python compare_listings.py --csv product_identities_stanley_quencher.csv --lines 10,22

Writes a self-contained HTML page (photos embedded) you can open in any browser or send on.
"""

import re
import csv
import sys
import base64
import argparse

import requests

import image_cluster as ic


def main_photo_url(listing_id: str) -> str | None:
    """The listing's cover photo, from its (server-rendered) item page — no login needed."""
    try:
        h = requests.get(f"https://www.vinted.fr/items/{listing_id}",
                         headers=ic._UA, timeout=20).text
    except Exception:
        return None
    # Full URL incl. the ?s=<signature> query — the CDN 404s without it. Prefer the /f800/
    # (large) size; the cover photo is the first one on the page.
    urls = re.findall(
        r"https://images1\.vinted\.net/t/[^\"\\ ]+?/f800/\d+\.(?:jpe?g|webp)\?s=[a-f0-9]+", h)
    seen = []
    for u in urls:                    # dedup by photo (ignoring signature), keep order
        stem = u.split("/f800/")[-1].split("?")[0]
        if stem not in [s.split("/f800/")[-1].split("?")[0] for s in seen]:
            seen.append(u)
    return seen[0] if seen else None


def _b64(url: str) -> str:
    try:
        r = requests.get(url, headers=ic._UA, timeout=20)
        if not r.ok or not r.content:
            return ""
        ct = (r.headers.get("content-type") or "image/webp").split(";")[0]
        return f"data:{ct};base64," + base64.standard_b64encode(r.content).decode()
    except Exception:
        return ""


def build(ids: list, titles: dict, out: str = "compare.html") -> str:
    cards = []
    for iid in ids:
        u = main_photo_url(iid)
        img = _b64(u) if u else ""
        cell = (f'<img src="{img}">' if img
                else '<div class=miss>photo unavailable (listing may be sold/removed)</div>')
        cards.append(
            f'<div class=card>{cell}<div class=id>#{iid}</div>'
            f'<div class=t>{titles.get(iid, "")}</div>'
            f'<a href="https://www.vinted.fr/items/{iid}" target=_blank>open on Vinted ↗</a></div>')
    html = f"""<!doctype html><meta charset=utf-8><title>Listing comparison</title>
<style>body{{font-family:system-ui,Arial;margin:24px;background:#fafafa}}
.grid{{display:flex;flex-wrap:wrap;gap:18px}}
.card{{width:300px;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:12px}}
.card img{{width:100%;border-radius:6px}}
.miss{{height:200px;display:flex;align-items:center;justify-content:center;color:#999;background:#f1f1f1;border-radius:6px;text-align:center;padding:0 12px}}
.id{{font-family:monospace;color:#888;margin-top:8px;font-size:12px}}
.t{{font-weight:600;font-size:13px;margin:4px 0 8px}}a{{font-size:12px}}</style>
<h1>Listing comparison — same product, or different?</h1>
<div class=grid>{''.join(cards)}</div>"""
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def main():
    ap = argparse.ArgumentParser(description="Compare listing photos side by side")
    ap.add_argument("ids", nargs="*", help="listing ids to compare")
    ap.add_argument("--csv", help="product_identities CSV to pull ids/titles from")
    ap.add_argument("--lines", help="comma-separated FILE line numbers in that CSV (header=1)")
    ap.add_argument("--out", default="compare.html")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    ids, titles = list(args.ids), {}
    if args.csv:
        rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
        if args.lines:
            for ln in (int(x) for x in args.lines.split(",")):
                r = rows[ln - 2]     # header is file line 1, so data line N = rows[N-2]
                ids.append(r["listing_id"])
        for r in rows:
            titles[r["listing_id"]] = r.get("generated_title", "")
    out = build(ids, titles, args.out)
    print(f"📄 wrote {out} for listings: {', '.join(ids)}")


if __name__ == "__main__":
    main()
