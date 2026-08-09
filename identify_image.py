"""
Identify products from LOCAL image files, using the same vision provider as the live pipeline.
Handy for testing a NEW category from reference photos (e.g. Jellycat plush) before those items
are pulled from the catalogue.

    python identify_image.py dragon.jpg egg.webp marshmallow.png croissant.jpg

Needs the real provider to actually look at the image:
    setx VINTED_VISION_PROVIDER anthropic   (+ ANTHROPIC_API_KEY)
The default stub reads only titles, so it can't identify a bare photo — it will say so.
"""

import sys

import vision_identify as vi


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    files = sys.argv[1:]
    if not files:
        print("usage: python identify_image.py <image> [<image> ...]")
        return
    provider = vi.get_provider()
    pname = vi.os.environ.get("VINTED_VISION_PROVIDER", "stub")
    print(f"provider = {pname}"
          + ("   ⚠️  stub can't read images — set VINTED_VISION_PROVIDER=anthropic" if pname == "stub" else "")
          + "\n")
    for path in files:
        r = provider.identify(path, title_hint="")     # title hidden — pure photo test
        title = vi.compose_title(r, "")
        print(f"📷 {path}")
        print(f"   → {title or '(nothing identified)'}")
        print(f"     brand={r.get('brand','')!r}  line={r.get('product_line','')!r}  "
              f"category={r.get('category','')!r}  colour={r.get('colour','')!r}  "
              f"confidence={r.get('confidence','')}")
        print()


if __name__ == "__main__":
    main()
