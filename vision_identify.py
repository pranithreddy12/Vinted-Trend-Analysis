"""
Phase 5 Stage A — Vision-AI product identification (scaffolding).

Given a listing PHOTO, identify the exact product (brand + line + colour) and compose a
complete, Google-verifiable title — the accuracy upgrade over the local CLIP prototype
(`product_identify.py`), which tops out at ~73%. A multimodal LLM reads the image directly.

STATUS: scaffolding. The whole pipeline (schema, prompt, caching, cost controls, the
integration hook) is built and runs TODAY against a deterministic STUB provider, with no
API key. When the client provisions their vision-AI key (set `ANTHROPIC_API_KEY` in the
environment — see the client setup message), flip `VINTED_VISION_PROVIDER=anthropic` and the
real provider takes over unchanged. The secret is read from the environment by the SDK; this
code never receives or stores the key value.

Design decisions:
  • Provider abstraction (`VisionProvider`) → stub is a drop-in for the real one, so the
    rest of the system is fully testable before the key exists.
  • Per-listing cache (`phase5_cache/vision_<slug>.json`) → we never pay to identify the
    same listing twice. This is the main cost control at continuous scale.
  • Photo passed to the model by URL (Vinted CDN) — no download/base64 round-trip.
  • Capacity is NEVER taken from the image (a photo can't show litres) — the prompt reads it
    from the listing title when present, consistent with the rest of Phase 4/5.
  • Model is configurable (`VINTED_VISION_MODEL`); defaults to a capable model. For very high
    volume, a cheaper tier is worth evaluating — that's a cost/accuracy call for the owner.
"""

import os
import re
import json
import base64

import requests

import track_sales as ts   # reuse Phase-4 capacity/colour/line text helpers
import image_cluster as ic  # reuse CACHE_DIR + _safe


# ─────────────────────────────────────────
# STRUCTURED OUTPUT SCHEMA
# ─────────────────────────────────────────
# What the model must return. Kept flat + strict so it validates exactly and parses cleanly.

IDENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string", "description": "Brand name, e.g. 'Stanley'. '' if unknown."},
        "product_line": {"type": "string", "description": "Product line/model family, e.g. 'Quencher', 'Flip Straw', 'IceFlow'. '' if not identifiable."},
        "official_name": {"type": "string", "description": "Full official product name suitable for a Google search, e.g. 'Stanley Quencher H2.0 FlowState Tumbler'. '' if unknown."},
        "colour": {"type": "string", "description": "Canonical colour word in English, e.g. 'pink', 'black'. '' if unclear."},
        "is_accessory": {"type": "boolean", "description": "True if the item is an accessory (bag, sleeve, lid, straw, charm), not the bottle itself."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"], "description": "How sure the identification is."},
    },
    "required": ["brand", "product_line", "official_name", "colour", "is_accessory", "confidence"],
    "additionalProperties": False,
}

MODEL = os.environ.get("VINTED_VISION_MODEL", "claude-opus-4-8")


def _prompt(title_hint: str) -> str:
    """Instruction paired with the photo. States the honest capacity rule and the
    accessory guard so the model's job matches the rest of the pipeline."""
    hint = f'\n\nThe seller titled this listing: "{title_hint}". Use it as a hint, but trust the photo for brand, line and colour.' if title_hint else ""
    return (
        "You identify the exact resale product shown in this Vinted listing photo. "
        "Return the brand, the specific product line/model, the colour, and a complete "
        "OFFICIAL product name that would return the exact product if pasted into Google. "
        "Identify brand, line and colour from the image. Do NOT state a capacity/size — a "
        "photo can't show litres; capacity is handled separately from the title. If the item "
        "is an accessory (a carrying bag, insulated sleeve, replacement lid, straw, or charm) "
        "rather than the bottle itself, set is_accessory=true. Be honest with confidence: use "
        "'low' when the photo is ambiguous rather than guessing." + hint +
        '\n\nReply with ONLY a JSON object, no prose, no code fences:\n'
        '{"brand": "", "product_line": "", "official_name": "", "colour": "", '
        '"is_accessory": false, "confidence": "high|medium|low"}'
    )


def _extract_json(text: str) -> dict:
    """Parse the model's reply into a dict — tolerant of code fences or surrounding prose,
    so it works whether the endpoint enforces JSON (Claude) or just follows the prompt
    (Ollama and other local models)."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return {}


def _image_block(photo_url: str) -> dict | None:
    """Download the photo and return a base64 image content block. base64 is the one source
    type every anthropic-compatible endpoint accepts (Ollama only takes base64; Claude takes
    both) — so this keeps one code path for any provider."""
    try:
        r = requests.get(photo_url, headers=ic._UA, timeout=ic.DOWNLOAD_TIMEOUT)
        if r.status_code != 200 or not r.content:
            return None
        media = (r.headers.get("content-type") or "").split(";")[0].strip()
        if not media.startswith("image/"):
            media = "image/jpeg"  # Vinted CDN serves jpeg; header sometimes generic
        return {"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": base64.standard_b64encode(r.content).decode()}}
    except Exception:
        return None


# ─────────────────────────────────────────
# PROVIDERS
# ─────────────────────────────────────────

class VisionProvider:
    """Interface: photo URL (+ title hint) → validated identity dict matching IDENTITY_SCHEMA."""

    def identify(self, photo_url: str, title_hint: str = "") -> dict:
        raise NotImplementedError


class StubVisionProvider(VisionProvider):
    """No API, no key. Produces a deterministic, plausible result from the listing TITLE
    (reusing the Phase-4 text helpers) so the full pipeline — caching, integration, the
    verification harness — is testable before the real key exists. It is NOT the product:
    it only knows what the title says, exactly the gap the real vision model will close."""

    def identify(self, photo_url: str, title_hint: str = "") -> dict:
        toks = ts.fr._tokenize((title_hint or "").lower())
        line = ts.detect_model(title_hint or "")
        colour = next((ts.COLOR_BUCKETS[t] for t in toks if t in ts.COLOR_BUCKETS), "")
        try:
            import product_identify as pi
            accessory = pi.is_accessory_title(title_hint)
        except Exception:
            accessory = False
        official = ""
        if line:
            official = f"Stanley {line}"  # crude; the real provider returns the true official name
        return {
            "brand": "Stanley" if "stanley" in (title_hint or "").lower() else "",
            "product_line": line,
            "official_name": official,
            "colour": colour,
            "is_accessory": bool(accessory),
            "confidence": "low",  # a stub is never confident — it's a placeholder
        }


class AnthropicVisionProvider(VisionProvider):
    """Real provider: a multimodal Claude model reads the photo and returns the structured
    identity. Auth comes from the environment (ANTHROPIC_API_KEY) via the SDK — this code
    never sees the key. Requires `pip install anthropic` and a funded key on the client's
    account. Not exercised until VINTED_VISION_PROVIDER=anthropic."""

    def __init__(self, model: str = MODEL):
        self.model = model
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic  # lazy — package only needed when this provider is used
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        return self._client

    def identify(self, photo_url: str, title_hint: str = "") -> dict:
        img = _image_block(photo_url)
        if img is None:
            return _empty_identity()
        client = self._client_lazy()
        # Minimal request shape only — no output_config/thinking, so the same call works on
        # Claude and on local anthropic-compatible endpoints (Ollama). JSON is requested in
        # the prompt and parsed tolerantly.
        resp = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [img, {"type": "text", "text": _prompt(title_hint)}],
            }],
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        )
        data = _extract_json(text)
        return _coerce_identity(data) if data else _empty_identity()


def get_provider() -> VisionProvider:
    """Pick the provider. Defaults to the stub so everything runs with no key; set
    VINTED_VISION_PROVIDER=anthropic once the client's key is in the environment."""
    choice = os.environ.get("VINTED_VISION_PROVIDER", "stub").lower()
    if choice == "anthropic":
        return AnthropicVisionProvider()
    return StubVisionProvider()


def _empty_identity() -> dict:
    return {"brand": "", "product_line": "", "official_name": "", "colour": "",
            "is_accessory": False, "confidence": "low"}


def _coerce_identity(d: dict) -> dict:
    out = _empty_identity()
    for k in out:
        if k in d:
            out[k] = d[k]
    out["is_accessory"] = bool(out["is_accessory"])
    if out["confidence"] not in ("high", "medium", "low"):
        out["confidence"] = "low"
    return out


# ─────────────────────────────────────────
# PER-LISTING CACHE (the main cost control)
# ─────────────────────────────────────────

class VisionCache:
    """listing_id → identity dict, persisted as JSON. A listing is identified at most once,
    ever — the single biggest lever on the per-image AI cost at continuous scale."""

    def __init__(self, slug: str):
        os.makedirs(ic.CACHE_DIR, exist_ok=True)
        self.path = os.path.join(ic.CACHE_DIR, f"vision_{ic._safe(slug)}.json")
        self.data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def get(self, listing_id: str):
        return self.data.get(str(listing_id))

    def put(self, listing_id: str, identity: dict):
        self.data[str(listing_id)] = identity

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=0)


# ─────────────────────────────────────────
# COMPOSE TITLE + BATCH HOOK
# ─────────────────────────────────────────

def compose_title(identity: dict, title_hint: str = "") -> str:
    """Build the final searchable title from the identity + capacity read from the listing
    title (never the photo). Mirrors product_identify.compose_title's shape."""
    if identity.get("is_accessory"):
        return f"{identity.get('brand', '') or 'Unknown'} accessory — not a bottle".strip()
    brand = identity.get("brand", "")
    official = identity.get("official_name", "")
    colour = identity.get("colour", "")
    colour_disp = ""
    try:
        import product_identify as pi
        colour_disp = pi.COLOUR_DISPLAY.get(colour.lower(), colour.title()) if colour else ""
    except Exception:
        colour_disp = colour.title() if colour else ""
    # Capacity: title only.
    cap = next((c for c in (ts.canonical_capacity(t)
                            for t in ts.fr._tokenize((title_hint or "").lower())) if c), "")

    title = official if (not brand or brand.lower() in official.lower()) else f"{brand} {official}"
    title = title or brand
    # The model often already bakes the colour (and sometimes the size) into
    # official_name — only append a piece if it isn't already present, else it doubles
    # ("Rose Quartz Rose Quartz").
    if colour_disp and colour_disp.lower() not in title.lower():
        title += f" {colour_disp}"
    if cap and cap.lower() not in title.lower():
        metric = ts._CAP_METRIC.get(cap)
        title += f" {cap} ({metric})" if metric else f" {cap}"
    return title.strip()


def identify_listings(raw_catalog_items: list, slug: str,
                      provider: VisionProvider | None = None,
                      max_new: int | None = None) -> dict:
    """Integration hook (not yet wired live). Identify listings we haven't seen, cache the
    results, and return {listing_id: {**identity, 'generated_title': ...}}.

    Incremental + cost-bounded: already-cached listings cost nothing; `max_new` caps how many
    fresh identifications happen per call (env VINTED_VISION_MAX_NEW also honoured). Only
    listings with a usable photo are considered.
    """
    provider = provider or get_provider()
    cache = VisionCache(slug)
    if max_new is None:
        # Default per-run cap so the first backfill can't burn the whole budget in one
        # burst. The client's account-level spend cap is the real ceiling; this is a rail.
        max_new = int(os.environ.get("VINTED_VISION_MAX_NEW", "200"))

    # Demand-first ordering (client request): spend the AI budget on the best products
    # first. Likes (favourite_count) are the demand signal available for free on every
    # catalog item — no history needed. Sales/offers priority would need the tracking
    # state; likes are the immediate proxy.
    likes = {str(it.get("id")): (it.get("favourite_count") or 0) for it in raw_catalog_items}
    listings = ic.listings_from_catalog(raw_catalog_items)
    listings.sort(key=lambda ls: likes.get(str(ls.id), 0), reverse=True)

    out, new = {}, 0
    for ls in listings:
        cached = cache.get(ls.id)
        if cached is None:
            if max_new is not None and new >= max_new:
                continue  # cost cap reached this run; remaining listings resolve next run
            cached = provider.identify(ls.photo_url, title_hint=ls.title)
            cache.put(ls.id, cached)
            new += 1
        out[ls.id] = {**cached, "generated_title": compose_title(cached, ls.title)}
    if new:
        cache.save()
    return out


def save_identities(identities: dict, slug: str) -> str:
    """Write the identified products (full titles) to a CSV the client can read —
    the concrete "use full titles that identify a specific product" deliverable."""
    import csv
    path = f"product_identities_{ic._safe(slug)}.csv"
    fields = ["listing_id", "generated_title", "brand", "product_line", "colour",
              "is_accessory", "confidence"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for lid, v in identities.items():
            w.writerow({"listing_id": lid, "generated_title": v.get("generated_title", ""),
                        "brand": v.get("brand", ""), "product_line": v.get("product_line", ""),
                        "colour": v.get("colour", ""), "is_accessory": v.get("is_accessory", False),
                        "confidence": v.get("confidence", "")})
    return path


# ─────────────────────────────────────────
# GOOGLE-VERIFICATION HARNESS (skeleton)
# ─────────────────────────────────────────

def verify_title_on_google(title: str, expected_brand: str = "") -> dict:
    """The client's acceptance test, automated: does the generated title return the exact
    product on Google? SKELETON — the actual search call needs a search API key (client
    dependency); until then this returns 'unverified' so the pipeline runs end to end.

    Returns {title, verified: bool|None, note}. When wired, `verified` becomes True/False
    from checking whether the top results match the identified product."""
    key = os.environ.get("VINTED_SEARCH_API_KEY")
    if not key:
        return {"title": title, "verified": None,
                "note": "search API key not configured (VINTED_SEARCH_API_KEY) — skeleton"}
    # TODO(stage-A): call the search API with `title`, inspect the top results, and set
    # verified=True when they match `expected_brand`/the identified product. Kept out until
    # the client provides the search key so we don't hardcode a provider prematurely.
    return {"title": title, "verified": None, "note": "search wiring pending"}


def verification_report(identities: dict) -> dict:
    """Run the (skeleton) verification over a batch and summarise the pass-rate."""
    results = [verify_title_on_google(v.get("generated_title", ""), v.get("brand", ""))
               for v in identities.values()]
    checked = [r for r in results if r["verified"] is not None]
    passed = [r for r in checked if r["verified"]]
    return {
        "total": len(results),
        "checked": len(checked),
        "passed": len(passed),
        "pass_rate": (round(100 * len(passed) / len(checked)) if checked else None),
        "note": "verification is a skeleton until VINTED_SEARCH_API_KEY is set",
        "results": results,
    }
