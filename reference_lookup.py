"""
Phase 5 — Stage B: external-source reference lookup for GENERIC (no-brand) items.

Stage A identifies branded products. For generic items (an unbranded "grey foam dog stairs")
there is no brand/model to name — Stage A only describes them. This module takes that
description and looks up a *reference*: the closest common/branded equivalent a reseller would
compare it to, plus a typical used resale price band. That turns a shapeless generic listing
into something comparable and analysable.

Two backends behind one interface (same pattern as vision_identify's providers):
  • AnthropicReferenceProvider — reuses the existing Anthropic key. The model proposes the
    reference product + price band from the description. Works today, no extra setup. It is the
    model's KNOWLEDGE, not a live web query — labelled source="ai-knowledge" so that's explicit.
  • WebSearchReferenceProvider — the "live web (Amazon/Google/brand sites)" backend the client's
    spec describes. SKELETON: needs a search API key (VINTED_SEARCH_API_KEY); until that exists it
    returns unverified, so the slot is ready without blocking the AI backend.

Cost control: results are cached by the (normalised) description, not per-listing — every
"grey foam dog stairs" listing shares one lookup. This is the main lever, exactly like Stage A.
"""

import os
import json

import image_cluster as ic          # CACHE_DIR + _safe
import vision_identify as vi         # reuse _extract_json + the anthropic MODEL default


REFERENCE_MODEL = os.environ.get("VINTED_REFERENCE_MODEL", vi.MODEL)


def _empty_reference() -> dict:
    return {"reference_name": "", "price_low": None, "price_high": None,
            "currency": "EUR", "confidence": "low", "source": "none"}


def _coerce_reference(d: dict, source: str) -> dict:
    out = _empty_reference()
    for k in ("reference_name", "currency", "confidence"):
        if isinstance(d.get(k), str):
            out[k] = d[k]
    for k in ("price_low", "price_high"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            out[k] = round(float(v), 2)
    if out["confidence"] not in ("high", "medium", "low"):
        out["confidence"] = "low"
    out["source"] = source
    return out


def _prompt(description: str) -> str:
    return (
        "A reseller is analysing a second-hand item on Vinted described as:\n"
        f'  "{description}"\n'
        "Name the closest common REFERENCE product a reseller would compare it to (a typical "
        "branded or standard equivalent), and estimate a typical USED resale price range in "
        "euros. If you genuinely can't tell, use an empty reference_name and low confidence — "
        "do not invent a brand.\n"
        'Reply with ONLY a JSON object, no prose:\n'
        '{"reference_name": "", "price_low": 0, "price_high": 0, "currency": "EUR", '
        '"confidence": "high|medium|low"}'
    )


class ReferenceProvider:
    """Interface: item description → reference dict (reference_name + price band + source)."""

    def lookup(self, description: str) -> dict:
        raise NotImplementedError


class StubReferenceProvider(ReferenceProvider):
    """No key, no cost. Returns an empty reference so the pipeline runs end to end without
    spending anything — the same role StubVisionProvider plays for Stage A."""

    def lookup(self, description: str) -> dict:
        return _empty_reference()


class AnthropicReferenceProvider(ReferenceProvider):
    """AI-knowledge backend: the model proposes a reference product + price band from the
    description. Auth from the environment (ANTHROPIC_API_KEY) via the SDK. Text-only (no
    image) — the description already carries what Stage A saw, so this stays cheap."""

    def __init__(self, model: str = REFERENCE_MODEL):
        self.model = model
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def lookup(self, description: str) -> dict:
        if not description.strip():
            return _empty_reference()
        resp = self._client_lazy().messages.create(
            model=self.model, max_tokens=512,
            messages=[{"role": "user", "content": _prompt(description)}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text")
        data = vi._extract_json(text)
        return _coerce_reference(data, "ai-knowledge") if data else _empty_reference()


class WebSearchReferenceProvider(ReferenceProvider):
    """Live web (Amazon/Google/brand sites) backend — the client's literal spec. SKELETON:
    needs a search API key. Until VINTED_SEARCH_API_KEY is set it returns unverified, so this
    slot is ready without blocking the AI backend. Wire the actual search call here when the
    client provisions a key (mirror vision_identify.verify_title_on_google)."""

    def lookup(self, description: str) -> dict:
        if not os.environ.get("VINTED_SEARCH_API_KEY"):
            return _empty_reference()  # skeleton — search wiring pending the key
        # TODO(stage-B): query the search API with `description`, read the top shopping result,
        # set reference_name + price band from it, source="web". Kept out until the key exists
        # so we don't hardcode a provider prematurely.
        return _empty_reference()


def get_reference_provider() -> ReferenceProvider:
    """Pick the backend. Default 'stub' (free, no key). 'anthropic' = AI-knowledge (works with
    the Stage A key). 'search' = live web (needs VINTED_SEARCH_API_KEY)."""
    choice = os.environ.get("VINTED_REFERENCE_PROVIDER", "stub").lower()
    if choice == "anthropic":
        return AnthropicReferenceProvider()
    if choice == "search":
        return WebSearchReferenceProvider()
    return StubReferenceProvider()


class ReferenceCache:
    """description → reference dict, persisted as JSON. Keyed by the NORMALISED description so
    every identical generic ("grey foam dog stairs") is looked up once, ever."""

    def __init__(self, slug: str):
        os.makedirs(ic.CACHE_DIR, exist_ok=True)
        self.path = os.path.join(ic.CACHE_DIR, f"reference_{ic._safe(slug)}.json")
        self.data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    @staticmethod
    def key(description: str) -> str:
        return " ".join((description or "").lower().split())

    def get(self, description: str):
        return self.data.get(self.key(description))

    def put(self, description: str, ref: dict):
        self.data[self.key(description)] = ref

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=0)


def _is_generic(v: dict) -> bool:
    """A generic item = no brand identified but we do have a description to look up."""
    return not (v.get("brand") or "").strip() and bool((v.get("generated_title") or "").strip()) \
        and not v.get("is_accessory")


def enrich_generics(identities: dict, slug: str,
                    provider: ReferenceProvider | None = None,
                    max_new: int | None = None) -> dict:
    """Attach a `reference` dict to each GENERIC item in an identify_listings() result. Branded
    items are left untouched (Stage A already names them). Cached by description + per-run cap so
    the cost stays bounded, exactly like Stage A's identification.
    """
    provider = provider or get_reference_provider()
    cache = ReferenceCache(slug)
    if max_new is None:
        max_new = int(os.environ.get("VINTED_REFERENCE_MAX_NEW", "100"))

    pname = type(provider).__name__
    new = 0
    for v in identities.values():
        if not _is_generic(v):
            continue
        desc = v["generated_title"]
        ref = cache.get(desc)
        if ref is None or ref.get("_provider") != pname:
            if new >= max_new:
                continue  # cost cap reached; remaining resolve next run
            ref = provider.lookup(desc)
            ref["_provider"] = pname
            cache.put(desc, ref)
            new += 1
        v["reference"] = {k: val for k, val in ref.items() if k != "_provider"}
    if new:
        cache.save()
    return identities


def _demo() -> None:
    """Self-check (no key): stub returns a well-formed empty reference; cache dedups by
    description; branded items are skipped; generics get a reference attached."""
    prov = StubReferenceProvider()
    idents = {
        "1": {"brand": "Stanley", "generated_title": "Stanley Quencher", "is_accessory": False},
        "2": {"brand": "", "generated_title": "Grey foam dog stairs", "is_accessory": False},
        "3": {"brand": "", "generated_title": "grey foam dog stairs", "is_accessory": False},
    }
    out = enrich_generics(idents, "_selftest", provider=prov)
    assert "reference" not in out["1"], "branded item must be skipped"
    assert out["2"]["reference"]["source"] == "none", out["2"]
    # #2 and #3 are the same description (case/space) → one cache entry
    assert ReferenceCache.key("Grey foam dog stairs") == ReferenceCache.key("grey  foam dog stairs")
    ref = _coerce_reference({"reference_name": "Pet stairs", "price_low": 12, "price_high": 25,
                             "confidence": "medium"}, "ai-knowledge")
    assert ref["price_low"] == 12.0 and ref["source"] == "ai-knowledge", ref
    # clean the tiny self-test cache
    p = os.path.join(ic.CACHE_DIR, "reference__selftest.json")
    if os.path.exists(p):
        os.remove(p)
    print("reference_lookup self-check OK")


if __name__ == "__main__":
    _demo()
