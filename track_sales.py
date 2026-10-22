"""
Phase 4 — Sales-Tracking PoC.

Follows the listings for ONE product over time and measures how fast they sell.

Mechanism: each run fetches the active catalog for a keyword and diffs it against what we
saw before. A listing that leaves the catalog MAY have sold — but a disappearance is never
by itself treated as a sale. Two guards make the signal trustworthy:

  1. DEBOUNCE (DISAPPEARANCE_RUNS) — Vinted's search returns a different subset each run, so
     one absence proves nothing. Measured live: of 429 listings missing from a fetch, 350
     (82%) were still active on Vinted. A listing must be absent from N consecutive runs.
  2. SOLD VERIFICATION (VINTED_VERIFY_SOLD=1) — when it does disappear, we open its item page
     and read Vinted's real status. Only "Sold/Vendu" counts as a sale; deleted/removed are
     excluded; anything unconfirmed is left uncounted rather than guessed.

Without these, disappearance-as-sale overcounted sales by ~10x on real data.

Usage (same Chrome+CDP setup as the main scraper — run start_scraper-style Chrome first):
    python track_sales.py "stanley quencher"
    (or set VINTED_KEYWORDS). Run once to baseline, then again every ~12-24h.
"""

import os
import re
import csv
import sys
import time
import random
import threading
import collections
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

import fetch_results as fr

TRACK_DIR = "tracking"
TRACK_FIELDS = [
    "id",
    "title",
    "price",
    "created_at_ts",
    "first_seen",
    "last_seen",
    "status",          # active | disappeared | sold | removed  (sold/removed set by verification)
    "sale_confirmed",  # True only when Vinted's item page actually showed "Sold" (VINTED_VERIFY_SOLD)
    "missed_runs",     # consecutive runs this listing was absent from the catalog (debounce)
    "disappeared_at",
    "lifespan_hours",  # publish → disappearance (true time-to-sell, if created_at known)
    "hours_tracked",   # first_seen → disappearance (what we directly observed)
    "brand",           # structured attribute (from item page)
    "color",           # structured attribute (from item page)
    "size",            # catalog size_title — mandatory on clothing, empty on bottles
    "variant",         # brand + capacity/size + colour signature for grouping
    "offers",          # buyer offers on this listing (early-demand signal, from item page)
    "offers_seen_at",  # when the offers count was captured (offers are a moving snapshot)
    "category",        # Vinted's own category from the item page breadcrumb ("Chaussures de course")
    "category_path",   # full Vinted breadcrumb, "Femmes > Chaussures > Chaussures de sport > Chaussures de course"
]

TS_FMT = "%Y-%m-%d %H:%M:%S"

# How many CONSECUTIVE runs a listing must be absent from the catalog before we treat it as
# disappeared. Vinted's search returns a different subset each run, so a single absence is not
# evidence of anything: measured live on 429 absences, 350 (82%) were still active on Vinted.
# 2 makes transient search churn self-correct for free. Override with VINTED_DISAPPEARANCE_RUNS.
DISAPPEARANCE_RUNS = max(1, int(os.environ.get("VINTED_DISAPPEARANCE_RUNS", "2")))


def _slug(keyword: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in keyword.lower()).strip("_")


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _hm() -> str:
    """Local [HH:MM] stamp for phase lines (run logs had none between START and END, so a
    15 h stall in the client log 2026-10-12 could not be located)."""
    return datetime.now().strftime("[%H:%M]")


def _parse(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT).replace(tzinfo=timezone.utc)


def load_tracking(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["id"]] = row
    return out


def save_tracking(path: str, tracking: dict) -> None:
    os.makedirs(TRACK_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACK_FIELDS)
        writer.writeheader()
        for row in tracking.values():
            writer.writerow({k: row.get(k, "") for k in TRACK_FIELDS})


def _coverage_line(current_ids: set, prev_ids: set) -> str:
    """Did a category sweep see every new listing? A sweep reads a ~960-per-site window, so if none of
    this run's listings were also in the previous run, more new listings arrived than the window holds
    and some were never seen. ponytail: overlap is measured across all country sites together, so a gap in
    one site can be hidden by overlap in another; measure per site if that matters."""
    overlap = len(current_ids & prev_ids)
    if overlap > 0:
        return f"   coverage: complete — {overlap} listings overlap the previous run"
    return ("   ⚠️  coverage: GAP — none of this run's listings were in the previous run; more new listings "
            "arrive than the ~960 window. Run more often or choose a narrower category.")


def _lifespan_hours(row: dict):
    """Publish -> last seen alive, in hours, or None when the publish time is unknown. Derived
    from created_at_ts and last_seen (not the stored lifespan_hours) so rows recorded before the
    last_seen fix are corrected retroactively; the stored value is only a fallback."""
    cts, ls = row.get("created_at_ts"), row.get("last_seen")
    try:
        if cts and ls:
            v = (_parse(ls) - datetime.fromtimestamp(int(float(cts)), tz=timezone.utc)
                 ).total_seconds() / 3600
            # publish time is only day-accurate ("il y a N jours"), so a quick sale can land a few
            # hours "before" it was posted (client log 2026-10-19: median -0.9h): that is ~0h, not
            # a negative; anything further off is bad data.
            if v >= -24:
                return round(max(v, 0.0), 1)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        lh = row.get("lifespan_hours")
        v = float(lh) if lh not in ("", None) else None
        return v if v is None or v >= 0 else None
    except ValueError:
        return None


def update_tracking(tracking: dict, current_items: list, now: datetime) -> tuple:
    """
    Pure state update: reconcile the tracking table against the current active set.
    Returns (newly_listed, newly_disappeared). Mutates `tracking` in place.
    """
    now_str = _fmt(now)
    current_ids = set()
    newly = 0

    for item in current_items:
        iid = str(item.get("id"))
        if not iid or iid == "None":
            continue
        current_ids.add(iid)
        title = item.get("title") or ""
        brand_title = item.get("brand_title") or ""
        # Mandatory on clothing, empty on bottles — free, straight from the catalog feed
        # (no item-page load needed), unlike colour which needs enrichment.
        size_title = item.get("size_title") or ""
        if iid in tracking:
            row = tracking[iid]
            row["last_seen"] = now_str
            # Backfill brand/size/variant from the catalog (free — no page load needed).
            if item.get("brand_is_real"):
                # seller-entered brand/size from the catalog label: authoritative, and replaces
                # the first-title-word guess older sweep rows were stored with
                row["brand"] = brand_title
                if size_title:
                    row["size"] = size_title
            if brand_title and not row.get("brand"):
                row["brand"] = brand_title
            if size_title and not row.get("size"):
                row["size"] = size_title
            if not row.get("variant"):
                row["variant"] = build_variant(
                    row.get("title", ""), row.get("brand", ""), row.get("color", ""),
                    row.get("size", ""),
                )
            # Present in this run: clear the absence counter (see DISAPPEARANCE_RUNS).
            row["missed_runs"] = 0
            if row["status"] == "disappeared":
                # Same listing id reappeared — un-mark it (rare; e.g. seller re-activated).
                row["status"] = "active"
                row["disappeared_at"] = ""
                row["lifespan_hours"] = ""
                row["hours_tracked"] = ""
        else:
            price = ""
            if isinstance(item.get("price"), dict):
                price = item["price"].get("amount", "")
            tracking[iid] = {
                "id": iid,
                "title": title[:80],
                "price": price,
                "created_at_ts": item.get("created_at_ts") or "",
                "first_seen": now_str,
                "last_seen": now_str,
                "status": "active",
                "sale_confirmed": "",
                "missed_runs": 0,
                "disappeared_at": "",
                "lifespan_hours": "",
                "hours_tracked": "",
                "brand": brand_title,
                "color": "",
                "size": size_title,
                "variant": build_variant(title, brand_title, "", size_title),
            }
            newly += 1

    disappeared = 0
    for iid, row in tracking.items():
        if row["status"] == "active" and iid not in current_ids:
            # DEBOUNCE — Vinted's search does not return a stable, complete set between runs:
            # measured live, 82% of listings missing from one fetch were still active on Vinted
            # (they had merely dropped out of the search results). Treating a single absence as
            # a disappearance overcounted sales ~10x. Require N consecutive misses instead, so
            # search churn resolves itself for free, before we spend any page loads verifying.
            row["missed_runs"] = int(row.get("missed_runs") or 0) + 1
            if row["missed_runs"] < DISAPPEARANCE_RUNS:
                continue
            row["status"] = "disappeared"
            row["disappeared_at"] = now_str
            try:
                row["hours_tracked"] = round(
                    (now - _parse(row["first_seen"])).total_seconds() / 3600, 1
                )
            except Exception:
                pass
            cts = row.get("created_at_ts")
            if cts:
                try:
                    pub = datetime.fromtimestamp(int(float(cts)), tz=timezone.utc)
                    # Measured to the last time it was SEEN alive, not to detection: with the
                    # 2-run debounce and ~daily cycles, detection lags the sale by 2+ days, which
                    # made every lifespan >= 48h (client logs: fast movers 0% in every run).
                    seen = _parse(row["last_seen"]) if row.get("last_seen") else now
                    row["lifespan_hours"] = round(
                        (seen - pub).total_seconds() / 3600, 1
                    )
                except Exception:
                    pass
            disappeared += 1

    return newly, disappeared


_TS_RE = re.compile(r'timestamp[\\":\s]+(\d{9,11})')


def extract_publish_ts_from_html(html: str) -> int | None:
    """
    Derive precise listing creation time from the item's photo-upload timestamps
    embedded in the server-rendered HTML. The item's own photos (many size
    variants) all share one timestamp, so it dominates the count — the MODE is the
    listing's creation time, to the second, for every listing and with no API call.
    """
    ts = [int(x) for x in _TS_RE.findall(html or "")]
    if not ts:
        return None
    return collections.Counter(ts).most_common(1)[0][0]


# NOT used as a publish-time source: a live cross-check on the client's machine (2026-10-15,
# 138 + 78 listings) found the page's photo timestamps NEVER within 24h of the item API / "posted
# X ago" value - they are not the listing's creation time. Rows it wrote are cleaned up by
# _repair_photo_ts below.
_PHOTO_TS_BAD_SINCE = "2026-10-15 08:00:00"   # UTC; first run of the build that used the photos


def _repair_photo_ts(tracking: dict, since: str = _PHOTO_TS_BAD_SINCE) -> int:
    """Clear publish times (and the lifespan derived from them) on rows enriched since the
    photo-timestamp fallback shipped, so they are re-captured from the real source. Rows with no
    offers_seen_at were not enriched in that window and are left alone. Returns rows cleared."""
    n = 0
    for row in tracking.values():
        if (row.get("created_at_ts") and (row.get("offers_seen_at") or "") >= since):
            row["created_at_ts"] = ""
            row["lifespan_hours"] = ""
            n += 1
    return n


# ── Shared rate-limit back-off across all worker tabs ──
# When Vinted rate-limits us, every tab should hold (not march on to the next
# product) until a cooldown elapses.
RATE_LIMIT_MARKERS = (
    "you are rate limited",
    "rate limited",
    "rate-limited",
    "too many requests",
    "trop de requêtes",
    "trop de demandes",
)
_rl_lock = threading.Lock()
_rl_held = False     # True while a rate-limit hold is in effect (all tabs wait)
_rl_prober = False   # True while one tab is actively probing for recovery


class RateLimited(Exception):
    pass


_CONFIRM_CLEAR = max(1, int(os.environ.get("VINTED_RL_CONFIRM", 2)))  # consecutive clean
# probes required before declaring the rate limit truly lifted (see _probe_until_clear)


def _rate_limit_cooldown() -> float:
    """Initial quiet wait before we start gently refreshing to probe recovery."""
    try:
        return float(os.environ.get("VINTED_RL_COOLDOWN", 60))
    except ValueError:
        return 60.0


def _is_rate_limited_text(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in RATE_LIMIT_MARKERS)


def _begin_hold_become_prober() -> bool:
    """Mark a global hold. Returns True if THIS tab should be the one prober."""
    global _rl_held, _rl_prober
    with _rl_lock:
        _rl_held = True
        if not _rl_prober:
            _rl_prober = True
            return True
        return False


def _release_hold() -> None:
    global _rl_held, _rl_prober
    with _rl_lock:
        _rl_held = False
        _rl_prober = False


def _wait_for_rate_limit() -> None:
    """Block this tab while a rate-limit hold is in effect."""
    while True:
        with _rl_lock:
            held = _rl_held
        if not held:
            return
        time.sleep(3)


def _probe_until_clear(item_id: str) -> None:
    """
    One tab handles recovery: wait quietly, then gently refresh a page until the
    rate-limit clears, then release all tabs. Only this tab refreshes — the rest
    stay idle so we don't keep hammering while blocked.

    Requires _CONFIRM_CLEAR consecutive clean checks, not just one, before releasing.
    Live-observed: a single successful probe is NOT reliable evidence Vinted's limit is
    actually lifted (it took up to 12 refreshes to genuinely clear on a real run) —
    releasing on one success let every worker retry at once, immediately re-tripping the
    limit, which looked like tabs opening nonstop with no real cooldown.
    """
    cd = _rate_limit_cooldown()
    print(f"\n   ⛔ Rate limited by Vinted — pausing all tabs. Waiting {int(cd)}s, "
          f"then refreshing until it clears...")
    time.sleep(cd)
    url = f"https://www.vinted.fr/items/{item_id}"
    consecutive_clear = 0
    for attempt in range(1, 41):  # cap the probing so it can't loop forever
        if _cdp_dead():
            print("   ⚠️  Chrome stopped accepting connections — releasing the hold.")
            _release_hold()
            return
        blocked = True
        try:
            with sync_playwright() as p:
                browser = _cdp_connect(p)
                if browser is None:
                    continue
                page = browser.contexts[0].new_page()
                try:
                    resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    # ponytail: HTTP 429 is the only trustworthy block signal; the
                    # raw-HTML text scan false-matched bundled i18n strings on good pages.
                    blocked = resp is not None and resp.status == 429
                finally:
                    page.close()
        except Exception:
            pass
        if not blocked:
            consecutive_clear += 1
            if consecutive_clear < _CONFIRM_CLEAR:
                print(f"   …looking clear ({consecutive_clear}/{_CONFIRM_CLEAR} confirmations); "
                      f"double-checking before resuming")
                time.sleep(random.uniform(4, 6))
                continue
            print(f"   ✅ Rate limit cleared after {attempt} refresh(es) — resuming all tabs.")
            # Stagger the release so waiting workers don't all retry in the same instant
            # and immediately re-trip the limit as a synchronized burst.
            time.sleep(random.uniform(1, 3))
            _release_hold()
            return
        consecutive_clear = 0
        print(f"   …still limited (refresh {attempt}); waiting ~10s")
        time.sleep(random.uniform(8, 14))
    print("   ⚠️  Still limited after many refreshes — releasing anyway to retry.")
    _release_hold()


# Multilingual colour names → canonical buckets, so the same product doesn't
# fragment into rose/fuchsia/roze/rosa as separate variants.
COLOR_BUCKETS = {
    "rose": "pink", "rosa": "pink", "roze": "pink", "pink": "pink",
    "fuchsia": "pink", "fuksia": "pink", "magenta": "pink",
    "crème": "cream", "creme": "cream", "cream": "cream", "beige": "cream",
    "ivoire": "cream", "ivory": "cream", "écru": "cream", "ecru": "cream", "nude": "cream",
    "gris": "grey", "grijs": "grey", "grau": "grey", "grey": "grey", "gray": "grey",
    "lila": "purple", "lilas": "purple", "lilac": "purple", "violet": "purple",
    "purple": "purple", "lavande": "purple", "lavender": "purple", "mauve": "purple", "paars": "purple",
    "bleu": "blue", "blauw": "blue", "blau": "blue", "blue": "blue", "azul": "blue",
    "marine": "blue", "navy": "blue",
    "vert": "green", "groen": "green", "grün": "green", "green": "green", "verde": "green",
    "kaki": "green", "olive": "green", "menthe": "green", "mint": "green",
    "noir": "black", "zwart": "black", "schwarz": "black", "black": "black", "negro": "black",
    "blanc": "white", "wit": "white", "weiß": "white", "weiss": "white", "white": "white", "blanco": "white",
    "rouge": "red", "rood": "red", "rot": "red", "red": "red", "rojo": "red", "bordeaux": "red",
    "orange": "orange", "oranje": "orange", "corail": "coral", "coral": "coral",
    "jaune": "yellow", "geel": "yellow", "gelb": "yellow", "yellow": "yellow",
    "marron": "brown", "brun": "brown", "braun": "brown", "brown": "brown", "camel": "brown", "taupe": "brown",
}


# Two-word shades the structured colour attribute returns ("Bleu clair"). Kept distinct from
# plain blue/green (a light-blue Smeg is its own product) but unified across languages.
_SHADES = {
    "bleu clair": "light blue", "bleu ciel": "light blue", "light blue": "light blue",
    "lichtblauw": "light blue", "hellblau": "light blue", "azul claro": "light blue",
    "bleu foncé": "dark blue", "bleu fonce": "dark blue", "dark blue": "dark blue",
    "donkerblauw": "dark blue", "dunkelblau": "dark blue",
    "vert foncé": "dark green", "vert fonce": "dark green", "dark green": "dark green",
    "donkergroen": "dark green", "dunkelgrün": "dark green",
    "vert clair": "light green", "light green": "light green",
    "lichtgroen": "light green", "hellgrün": "light green",
    "argenté": "silver", "argente": "silver", "silver": "silver", "zilver": "silver",
    "silber": "silver",
    "multicolore": "multicolour", "multicolor": "multicolour", "multicolour": "multicolour",
    "meerkleurig": "multicolour", "mehrfarbig": "multicolour",
    "rose clair": "light pink", "light pink": "light pink",
    "rose foncé": "dark pink", "dark pink": "dark pink",
}


def normalize_color(color: str) -> str:
    if not color:
        return ""
    first = color.split(",")[0].strip().lower()
    return _SHADES.get(first) or COLOR_BUCKETS.get(first, first)


# Known bottle sizes → canonical label. A listing's capacity is converted to
# millilitres and snapped to the nearest of these within tolerance, so the SAME
# physical size written different ways (40oz, 1.18L, 1.2L, 1.19L) collapses to one
# variant instead of fragmenting the demand across four. (40oz = 1183ml, so those
# are all the same product.) Sizes outside tolerance keep a rounded-litre label.
_CANONICAL_SIZES_ML = [
    (414, "14oz"), (473, "16oz"), (591, "20oz"), (709, "24oz"),
    (887, "30oz"), (946, "32oz"), (1183, "40oz"), (1419, "48oz"), (1892, "64oz"),
]
_CAP_UNIT_TO_ML = {"oz": 29.5735, "l": 1000.0, "cl": 10.0, "ml": 1.0}
_CAP_PARSE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)(l|oz|ml|cl)$")


def canonical_capacity(token: str) -> str:
    """Normalise a capacity token to a canonical size label.

    e.g. '40oz', '1.18l', '1.2l', '1.19l' → '40oz'; '0.89l', '887ml' → '30oz'.
    Returns '' if the token isn't a capacity. This fixes same-size-different-unit
    variant fragmentation (client feedback 2026-07-11)."""
    m = _CAP_PARSE_RE.match((token or "").strip().lower().replace(",", "."))
    if not m:
        return ""
    try:
        ml = float(m.group(1)) * _CAP_UNIT_TO_ML[m.group(2)]
    except (ValueError, KeyError):
        return ""
    if ml <= 0:
        return ""
    for known_ml, label in _CANONICAL_SIZES_ML:
        if abs(ml - known_ml) / known_ml <= 0.045:  # within 4.5% → same size
            return label
    # Not a standard bottle size — keep a stable rounded-litre label.
    return f"{round(ml / 1000, 2)}l"


# Stanley product LINES. Same size+colour on two different lines (a Quencher 40oz
# Pink vs a Flip Straw 40oz Pink) are different sellable products, so the line is
# added to the variant when the title names it. FlowState / H2.0 are the Quencher's
# own sub-branding, not a separate line → they map to Quencher. Only applies when a
# line word is actually present (~47% of Stanley titles); the rest need the photo
# (Phase 5) to identify the line — the title alone ("Stanley Cup") can't.
_MODEL_PATTERNS = [
    ("Quencher", re.compile(r"quencher|flow\s?state|h2\.?0(?:\b|\s)", re.I)),
    ("Flip Straw", re.compile(r"flip\s?straw|flip jet|paille", re.I)),
    ("IceFlow", re.compile(r"ice\s?flow", re.I)),
    ("Adventure", re.compile(r"adventure", re.I)),
    ("Go Tumbler", re.compile(r"\bgo\b\s*(tumbler|quencher)|gotumbler", re.I)),
    # NB: "thermos" is deliberately NOT here — it's the generic word for an insulated
    # bottle in FR/DE/ES/IT, so it would mislabel any Stanley as the Classic line.
    ("Classic", re.compile(r"\bclassic\b|legendary", re.I)),
]


def detect_model(title: str) -> str:
    """Return the product LINE named in the title (e.g. 'Quencher', 'Flip Straw'),
    or '' if none is named. Text-only — the ~half of listings with bare titles
    ('Stanley cup') can't be resolved here; that's what Phase 5 image recognition
    is for."""
    t = title or ""
    for label, pat in _MODEL_PATTERNS:
        if pat.search(t):
            return label
    return ""


def _is_stanley(row: dict) -> bool:
    """The product-line patterns above are Stanley's. A row whose catalog brand is another
    brand (Owala/Smeg/Jellycat…) must not get a Stanley line from a title that merely says
    'quencher' (client log 2026-10-09: 'quencher pink' High(53) inside the Owala report).
    Unknown brand stays eligible — bare titles are common."""
    b = (row.get("brand") or "").lower()
    return not b or "stanley" in b


def _row_model(row: dict) -> str:
    return detect_model(row.get("title", "")) if _is_stanley(row) else ""


# oz→metric companion for the display label, so the client sees both units at once
# (he referred to sizes in both, e.g. "40oz" and "1.18L").
_CAP_METRIC = {
    "14oz": "0.41L", "16oz": "0.47L", "20oz": "0.59L", "24oz": "0.71L",
    "30oz": "0.89L", "32oz": "0.95L", "40oz": "1.18L", "48oz": "1.42L", "64oz": "1.9L",
}


def product_display_name(model: str, base: str, brand: str = "Stanley") -> str:
    """Human, sellable product name for the card/report, e.g.
    'Stanley Quencher 40oz (1.18L) Pink'. base is 'cap colour' (or ''). No
    duplicate brand/model words. Falls back gracefully when the line is unknown."""
    if not base:
        return ""
    parts = base.split()
    cap = parts[0] if parts else ""
    colour = " ".join(parts[1:]).title() if len(parts) > 1 else ""
    cap_disp = f"{cap} ({_CAP_METRIC[cap]})" if cap in _CAP_METRIC else cap
    bits = [brand]
    if model:
        bits.append(model)
    if cap_disp:
        bits.append(cap_disp)
    if colour:
        bits.append(colour)
    return " ".join(bits)


# Vinted forces a size on every clothing listing, but non-clothing items come back as a
# meaningless placeholder — don't build a variant key out of those.
_NOOP_SIZES = {"", "one size", "onesize", "one-size", "one size fits all", "unique",
               "taille unique", "taglia unica", "talla única", "única", "einheitsgröße",
               "os", "u", "n/a"}


def _real_size(size: str) -> str:
    s = (size or "").strip()
    return "" if s.lower() in _NOOP_SIZES else s


def build_variant(title: str, brand: str, color: str, size: str = "") -> str:
    """
    Build the BASE variant key identifying a sellable product, e.g. "40oz pink".

    Live-observed 2026-08-29: this required BOTH capacity and colour, so it returned "" for
    every clothing/plush listing (no capacity concept) — 7 of 14 watch-list products (Ralph
    Lauren + all 6 Jellycat) produced ZERO variants no matter how much history accumulated,
    since there was never a key to group by, not a data problem. Degrades gracefully instead:
      1. capacity + colour  (bottles — UNCHANGED, exact prior behaviour/priority)
      2. size + colour      (clothing — `size` is the catalog's mandatory size_title)
      3. colour alone       (plush/generic with no size or capacity concept, e.g. Jellycat
                             characters that only vary by colour)
      4. "" only when there's truly no colour at all — the genuinely ungroupable case.
    Capacity is unit-normalised (40oz ≡ 1.18L ≡ 1.2L → "40oz") so the same size written
    different ways doesn't fragment into separate variants. Colour prefers the structured
    page attribute, falling back to a colour word in the title.

    The product LINE (Quencher / Flip Straw / …) is layered on in variant_analysis, not
    here, because assigning it correctly needs the global picture — a bare "Stanley cup
    40oz" title names no line, so it's imputed to the dominant line at that size+colour
    rather than fragmenting off on its own.
    """
    # "500 ml" / "0,5 L" written with a space would otherwise split into two tokens and the
    # capacity (the model dimension for Smeg 0.5 vs 0.6 L) would be lost.
    toks = fr._tokenize(re.sub(r"(\d)\s+(ml|cl|l|oz)\b", r"\1\2", (title or "").lower()))
    cap = ""
    for t in toks:
        cap = canonical_capacity(t)
        if cap:
            break
    col = normalize_color(color)
    if not col:
        # No structured colour — look for a colour word in the title.
        col = next((COLOR_BUCKETS[t] for t in toks if t in COLOR_BUCKETS), "")
    if cap and col:
        return f"{cap} {col}"
    real_size = _real_size(size)
    if real_size and col:
        return f"{real_size} {col}"
    return col


def compute_variant_opportunity(
    est_sales_30d: float,
    median_days,
    offers_total: int = 0,
    offers_measured: bool = False,
):
    """
    Score a variant 0–100 from concrete signals the client cares about.

    Weighting (client spec, 2026-09-04):
      1. Verified monthly sales volume (max 45, the lead signal) — real confirmed sales
         in the trailing 30 days, not an extrapolated rate.
      2. Sales velocity / liquidity    (max 25) — full marks for selling within 72h.
      3. Buyer demand signals (offers) (max 18) — early demand, leads sales in time.
      4. Competition is NOT scored. Client: "a good, high-selling product can still face
         significant competition" — it must not move the result, so it's surfaced
         separately (competition_level) as information only, never blended into the score.

    offers_measured distinguishes "measured 0 offers" (real weak early demand → the
    offers component is a genuine 0) from "offers not captured yet for this variant"
    (unknown → we score the other two factors renormalised to 100 instead of
    penalising it with a phantom 0). Offer coverage builds up as item pages get
    enriched over successive runs, so this avoids a first-run dip in every score.
    """
    # Sales volume — verified confirmed sales in the trailing 30 days (max 45, the lead signal)
    if est_sales_30d >= 30:
        vol = 45
    elif est_sales_30d >= 15:
        vol = 34
    elif est_sales_30d >= 8:
        vol = 23
    elif est_sales_30d >= 3:
        vol = 13
    elif est_sales_30d >= 1:
        vol = 6
    else:
        vol = 0

    # Liquidity — full marks for selling within 72h, the client's own cutoff (max 25)
    if median_days is None:
        liq = 0
    elif median_days <= 3:
        liq = 25
    elif median_days <= 7:
        liq = 15
    elif median_days <= 14:
        liq = 8
    else:
        liq = 3

    # Buyer demand — total live offers across the variant's active listings (max 18).
    # An early-demand signal: buyers submitting offers precede completed sales.
    if offers_total >= 20:
        off = 18
    elif offers_total >= 10:
        off = 14
    elif offers_total >= 5:
        off = 10
    elif offers_total >= 2:
        off = 6
    elif offers_total >= 1:
        off = 3
    else:
        off = 0

    if offers_measured:
        score = min(100, round((vol + liq + off) * 100 / 88))
    else:
        # Offers not yet captured for this variant — score the two proven factors
        # (max 70) renormalised to 100 so an un-measured signal is neutral, not a
        # penalty. Once offers arrive, the formula above applies.
        score = min(100, round((vol + liq) * 100 / 70))
    if score >= 70:
        verdict = "🚀 High Resale Opportunity"
    elif score >= 50:
        verdict = "🔥 Strong"
    elif score >= 30:
        verdict = "👍 Worth Watching"
    else:
        verdict = "⚠️ Weak"
    return {"score": score, "verdict": verdict}


def competition_label(active: int) -> str:
    """Relative market saturation per variant."""
    if active <= 15:
        return "Low"
    elif active <= 50:
        return "Medium"
    else:
        return "High"


def demand_label(est_sales_30d: float) -> str:
    """Plain demand level from proven monthly sales — the client-facing lead
    signal ("Pink 40oz → 48 sales/month • High demand • Medium competition")."""
    if est_sales_30d >= 20:
        return "High"
    elif est_sales_30d >= 8:
        return "Medium"
    else:
        return "Low"


def variant_confidence(sold_tracked: int, window_days: float) -> str:
    """
    How much to trust a variant's numbers, from sample size + tracking length.
    High needs ~a month of data because time-to-sell is censored by the window —
    a short window can't measure slow sales, so it can't be high-confidence yet.
    """
    if sold_tracked >= 10 and window_days >= 30:
        return "High"
    if sold_tracked >= 3 and window_days >= 7:
        return "Medium"
    return "Low"


def variant_trend(cur_est: float, prev_est) -> str:
    """Direction of demand vs the previous run's estimate."""
    if prev_est is None:
        return "Building"  # no prior snapshot yet
    if prev_est <= 0:
        # No baseline: 0 → first sales is a ramp-up, not growth (every variant read "Increasing"
        # in the client's 2026-10-09 log, right after sold-verification started).
        return "Building" if cur_est > 0 else "Stable"
    ratio = cur_est / prev_est
    if ratio >= 1.2:
        return "Increasing"
    if ratio <= 0.8:
        return "Decreasing"
    return "Stable"


def save_variant_snapshot(variants: list, slug: str = "") -> str | None:
    """Persist today's per-variant sales estimate so the next run can show a trend.

    Snapshots are namespaced PER PRODUCT (`variants_<slug>_<date>.csv`). They used to
    share one `variants_<date>.csv`, which meant run_tracker — looping over every
    keyword in tracked_keywords.txt — had each product overwrite the previous one's
    snapshot, so every product but the last compared its sales against a different
    product's numbers. Only one keyword was tracked, so it never surfaced.
    """
    if not variants:
        return None
    os.makedirs(TRACK_DIR, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    prefix = f"variants_{slug}_" if slug else "variants_"
    path = os.path.join(TRACK_DIR, f"{prefix}{date}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "est_sales_30d"])
        for v in variants:
            w.writerow([v["variant"], v["est_sales_30d"]])
    return path


_SNAP_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def snapshot_date(filename: str, slug: str = "") -> str | None:
    """Date of a `variants_[<slug>_]<date>.csv` snapshot, but ONLY if it belongs to exactly
    `slug`. Returns None otherwise.

    The remainder after the prefix must be exactly a date. A plain startswith() is not enough:
    one slug can be a PREFIX of another, so "stanley_quencher" would also match
    "variants_stanley_quencher_rose_<date>.csv" — a DIFFERENT product — silently mixing two
    products' numbers into one trend/momentum series (observed live: it produced a bogus
    "+200% rising" on every variant).
    """
    if not filename.startswith("variants_") or not filename.endswith(".csv"):
        return None
    stem = filename[len("variants_"):-len(".csv")]
    if slug:
        pre = slug + "_"
        if not stem.startswith(pre):
            return None
        stem = stem[len(pre):]
    return stem if _SNAP_DATE_RE.match(stem) else None


def load_prev_variant_snapshot(exclude_date: str, slug: str = "") -> dict:
    """Load this product's most recent prior snapshot → {variant: est_sales_30d}.

    Scoped to `slug` so a multi-product watch-list can't cross-contaminate trends
    (see save_variant_snapshot). A legacy un-namespaced `variants_<date>.csv` is
    ignored — the affected product simply shows "Building" for one run, then
    resumes on its own namespaced history.
    """
    if not os.path.isdir(TRACK_DIR):
        return {}
    # Strictly this product's own snapshots (see snapshot_date — prefix matching alone would
    # pull in a different product whose slug merely starts with this one).
    dated = [(d, fn) for fn in os.listdir(TRACK_DIR)
             if (d := snapshot_date(fn, slug)) and d != exclude_date]
    files = [fn for _d, fn in sorted(dated)]
    if not files:
        return {}
    out = {}
    try:
        with open(os.path.join(TRACK_DIR, files[-1]), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    out[row["variant"]] = float(row["est_sales_30d"])
                except (ValueError, KeyError):
                    pass
    except Exception:
        return {}
    return out


def _load_visual_variants(slug: str) -> tuple[dict, dict]:
    """Phase 5 hook: stable {listing_id: cluster_id} + {cluster_id: label} from
    the visual index, if one has been built for this keyword. Read-only and
    cheap (numpy file load, no torch) — returns empty dicts when Phase 5 is
    unused, so Phase 4 behaviour is unchanged."""
    if not slug:
        return {}, {}
    try:
        import image_cluster

        return image_cluster.load_visual_assignments(slug)
    except Exception:
        return {}, {}


def _load_vision_identities(slug: str) -> dict:
    """Phase 5 Stage A hook: {listing_id: official_title} from the vision-ID cache, if
    one exists. Cheap JSON read, no key/torch — empty when vision is unused, so Phase 4
    output is unchanged. Skips accessories and blank titles."""
    if not slug:
        return {}
    try:
        import vision_identify

        cache = vision_identify.VisionCache(slug)
        return {
            lid: v.get("generated_title") or vision_identify.compose_title(v)
            for lid, v in cache.data.items()
            if not v.get("is_accessory") and (v.get("official_name") or v.get("generated_title"))
        }
    except Exception:
        return {}


def _verify_sold_on() -> bool:
    """True when sold-verification is enabled — sales then require a Vinted-confirmed 'sold'
    status, not a bare disappearance."""
    return os.environ.get("VINTED_VERIFY_SOLD") == "1"


def _sold_within_days(row: dict, now: datetime, days: int = 30) -> bool:
    """Was this confirmed-sale row's last_seen (≈ when it sold) within the trailing window?
    Used for a VERIFIED monthly sales count (client spec, 2026-09-04), replacing the old
    gone/window_days*30 extrapolation which could inflate a young variant's rate or dilute
    an old one's recent activity into a long-run average."""
    try:
        return (now - _parse(row["last_seen"])).total_seconds() <= days * 86400
    except Exception:
        return False


def variant_analysis(
    tracking: dict,
    now: datetime | None = None,
    visual_slug: str | None = None,
    slug: str = "",
):
    """
    Aggregate per-variant turnover into the concrete metrics the SaaS shows:
    estimated sales/30d, sales velocity (days), competition level, market trend,
    average price, confidence, and last-updated. Returns (sorted list, window_days).

    slug: this product's keyword slug — scopes the trend comparison to this
    product's own snapshot history (see save_variant_snapshot).

    visual_slug (Phase 5): keyword slug whose visual-variant index should fill in
    groupings for listings the text tokenizer can't parse (no capacity+colour in
    the title). Text variants stay authoritative when they exist; the photo-based
    cluster only catches what would otherwise be dropped from the analysis.
    """
    now = now or datetime.now(timezone.utc)
    updated = now.strftime("%Y-%m-%d %H:%M UTC")
    prev_snap = load_prev_variant_snapshot(now.strftime("%Y-%m-%d"), slug)
    vis_map, vis_labels = _load_visual_variants(visual_slug)
    vision_titles = _load_vision_identities(slug)  # {listing_id: official title}
    brand = slug.split("_")[0].title() if slug else ""

    firsts = []
    for r in tracking.values():
        try:
            firsts.append(_parse(r["first_seen"]))
        except Exception:
            pass
    window_days = (
        max(1.0, (now - min(firsts)).total_seconds() / 86400) if firsts else 1.0
    )

    # Pass 1 — per base key (capacity+colour), find the dominant product LINE among
    # the listings that actually name one. A bare "Stanley cup 40oz" title names no
    # line; rather than fragment it into its own variant, we impute the dominant line
    # at that size+colour (for Stanley the 40oz is overwhelmingly the Quencher).
    # Where two lines genuinely coexist at one size+colour (e.g. Quencher vs Flip
    # Straw 40oz Pink), the explicitly-named minority still splits off correctly.
    model_votes = {}
    for r in tracking.values():
        base = build_variant(r.get("title", ""), r.get("brand", ""), r.get("color", ""), r.get("size", ""))
        if not base:
            continue
        m = _row_model(r)
        if m:
            model_votes.setdefault(base, collections.Counter())[m] += 1
    dominant_model = {
        base: votes.most_common(1)[0][0] for base, votes in model_votes.items()
    }

    groups = {}
    for r in tracking.values():
        base = build_variant(r.get("title", ""), r.get("brand", ""), r.get("color", ""), r.get("size", ""))
        if base:
            model = _row_model(r) or dominant_model.get(base, "") if _is_stanley(r) else ""
            v = f"{model.lower()} {base}".strip() if model else base
        else:
            # Phase 5 fallback: no text variant — group by what the PHOTO shows.
            cid = vis_map.get(str(r.get("id", "")))
            if not cid:
                continue
            label = (vis_labels.get(cid) or "").strip()
            v = f"📷 {cid}" + (f" · {label[:40]}" if label else "")
            model, base = "", ""
        g = groups.setdefault(
            v,
            {"active": 0, "gone": 0, "sold_30d": 0, "lifes": [], "prices": [], "sold_prices": [],
             "offers_total": 0, "offers_listings": 0,
             "model": model, "base": base, "vision_titles": collections.Counter()},
        )
        vt = vision_titles.get(str(r.get("id", "")))
        if vt:
            g["vision_titles"][vt] += 1
        if r["status"] == "active":
            g["active"] += 1
            # Buyer-offer signal: only active listings, only where we actually
            # captured a value (offers live on the item page, enriched for fresh
            # listings). Empty string / missing = not measured, skip it.
            ov = r.get("offers")
            if ov not in ("", None):
                try:
                    g["offers_total"] += int(float(ov))
                    g["offers_listings"] += 1
                except (ValueError, TypeError):
                    pass
        # A sale counts only when confirmed. With VINTED_VERIFY_SOLD on, that means a verified
        # "sold" status; with it off, we fall back to the legacy disappearance proxy. "removed"
        # and unverified "disappeared" (in verify mode) are NEVER counted as sales.
        elif r["status"] == "sold" or (r["status"] == "disappeared" and not _verify_sold_on()):
            g["gone"] += 1
            if _sold_within_days(r, now, 30):
                g["sold_30d"] += 1
            lh = _lifespan_hours(r)
            if lh is not None:
                try:
                    life = float(lh)
                    # Only trust lifespan as "time to sell" when we caught the
                    # listing FRESH (created_at within ~72h of first_seen) and the
                    # value is plausible (≤60 days). This excludes stale timestamps
                    # and pre-aged old inventory, both of which inflate velocity.
                    cts, fs = r.get("created_at_ts"), r.get("first_seen")
                    if cts and fs and 0.1 <= life <= 60 * 24:
                        gap_h = (
                            _parse(fs)
                            - datetime.fromtimestamp(
                                int(float(cts)), tz=timezone.utc
                            )
                        ).total_seconds() / 3600
                        if 0 <= gap_h <= 72:
                            g["lifes"].append(life)
                except (ValueError, OverflowError, OSError):
                    pass
        try:
            if r.get("price") not in ("", None):
                price = float(r["price"])
                g["prices"].append(price)
                # Last known asking price for a CONFIRMED sale is the closest proxy we have to
                # "what it actually sold for" (Vinted doesn't expose the real transaction price).
                # Only status=="sold" (VINTED_VERIFY_SOLD-confirmed) counts here — a bare
                # disappearance is never good enough evidence of a real sale price either.
                if r["status"] == "sold":
                    g["sold_prices"].append(price)
        except ValueError:
            pass

    out = []
    for v, g in groups.items():
        # Verified — real confirmed sales in the trailing 30 days, not an extrapolated rate
        # (client spec, 2026-09-04). Feeds demand_level, trend, the report, AND the score.
        est_30d = g["sold_30d"]
        med_days = (
            round(statistics.median(g["lifes"]) / 24, 1) if g["lifes"] else None
        )
        # Client feedback: a plain mean is skewed by a few extreme listings; median is the more
        # reliable headline number. Keep mean too (avg_price, for anything already reading it)
        # and add median/range/confirmed-sold-price so callers can show the fuller picture.
        avg_price = round(statistics.mean(g["prices"]), 1) if g["prices"] else None
        median_price = round(statistics.median(g["prices"]), 1) if g["prices"] else None
        price_low = round(min(g["prices"]), 1) if g["prices"] else None
        price_high = round(max(g["prices"]), 1) if g["prices"] else None
        sold_median_price = (
            round(statistics.median(g["sold_prices"]), 1) if g["sold_prices"] else None
        )
        opp = compute_variant_opportunity(
            est_30d, med_days, g["offers_total"],
            offers_measured=g["offers_listings"] > 0,
        )
        product = product_display_name(g.get("model", ""), g.get("base", ""), brand)
        # Phase 5 Stage A: the AI-identified official product name (dominant across the
        # variant's listings), when vision has run — else "".
        vt = g.get("vision_titles")
        ai_product = vt.most_common(1)[0][0] if vt else ""
        out.append(
            {
                "variant": v,
                "product": product or v,
                "ai_product": ai_product,
                "est_sales_30d": est_30d,
                "demand_level": demand_label(est_30d),
                "median_days_to_sell": med_days,
                "competition": g["active"],
                "competition_level": competition_label(g["active"]),
                "trend": variant_trend(est_30d, prev_snap.get(v)),
                "avg_price": avg_price,
                "median_price": median_price,
                "price_low": price_low,
                "price_high": price_high,
                "sold_median_price": sold_median_price,
                "offers": g["offers_total"],
                "offers_coverage": g["offers_listings"],
                "confidence": variant_confidence(g["gone"], window_days),
                "sold_tracked": g["gone"],
                "score": opp["score"],
                "verdict": opp["verdict"],
                "last_updated": updated,
            }
        )
    # Rank by estimated sales (the client's main demand indicator), then velocity.
    out.sort(
        key=lambda x: (x["est_sales_30d"], -(x["median_days_to_sell"] or 9999)),
        reverse=True,
    )
    return out, round(window_days, 1)


def opportunity_label(score: int) -> str:
    """Plain one-word read of the 0-100 score for the summary card."""
    if score >= 65:
        return "Excellent"
    elif score >= 50:
        return "Good"
    elif score >= 30:
        return "Fair"
    else:
        return "Low"


def _show_offers() -> bool:
    """Whether to DISPLAY the offers signal (card/table). Default on; set
    VINTED_SHOW_OFFERS=0 to hide it (for A/B-comparing the layout). The score
    always uses offers regardless — this toggle is display-only."""
    return os.environ.get("VINTED_SHOW_OFFERS", "1") != "0"


def _offers_display(v: dict) -> str:
    """Human read of the buyer-offer signal. '—' when we have no offer coverage
    for this variant yet (offers are captured from item pages as fresh listings
    are enriched, so coverage builds up over runs)."""
    if v.get("offers_coverage", 0) <= 0:
        return "—"
    return str(v.get("offers", 0))


def format_variant_card(variant_name: str, v: dict) -> str:
    """Client-requested summary-card layout for one variant — the concrete,
    numbers-first format he asked for in place of the raw table row. variant_name
    is a fallback title; the variant's own resolved product name is preferred."""
    md = v["median_days_to_sell"]
    vel = f"{md} days" if md is not None else "not yet measured"
    # Client feedback: median is more reliable than mean when a few extreme listings can skew
    # it. Lead with median + the price range; keep mean available as a secondary figure.
    if v.get("median_price") is not None:
        price = f"€{v['median_price']} (median"
        if v.get("price_low") is not None and v.get("price_high") is not None:
            price += f", range €{v['price_low']}–{v['price_high']}"
        price += ")"
        if v.get("sold_median_price") is not None:
            price += f" · €{v['sold_median_price']} sold-median"
    else:
        price = "—"
    title = v.get("product") or variant_name.title()
    lines = [
        f"{title}",
        "",
        f"🔥 Estimated Sales: {v['est_sales_30d']}/month",
        f"⚡ Average Time to Sell: {vel}",
    ]
    if _show_offers():
        lines.append(f"📈 Buyer Demand (offers): {_offers_display(v)}")
    lines += [
        f"🏷️ Price: {price}",
        f"👥 Active Listings: {v['competition']}",
        f"📊 Sales Trend: {v['trend']}",
        f"🏆 Competition: {v['competition_level']}",
        f"🎯 Opportunity: {opportunity_label(v['score'])}",
    ]
    return "\n".join(lines)


def save_variant_report(variants: list, output_file: str = "variant_report.csv") -> str | None:
    """Export the per-variant opportunity table — the concrete Phase 4 deliverable."""
    if not variants:
        return None
    fields = [
        "product",
        "ai_product",
        "variant",
        "est_sales_30d",
        "demand_level",
        "median_days_to_sell",
        "competition",
        "competition_level",
        "trend",
        "avg_price",
        "median_price",
        "price_low",
        "price_high",
        "sold_median_price",
        "offers",
        "offers_coverage",
        "confidence",
        "sold_tracked",
        "score",
        "verdict",
        "last_updated",
    ]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for v in variants:
            row = {k: ("" if v.get(k) is None else v.get(k)) for k in fields}
            writer.writerow(row)
    return output_file


def _publish_ts_from_page(page, item_id: str) -> dict:
    """
    Load one listing's item page and extract: publish time (when THIS listing was
    posted — its "Ajouté: il y a X" date, correct even for relisted items) plus
    its structured attributes (brand, colour) for variant grouping.
    Raises RateLimited on a rate-limit page. Returns a dict (values may be empty).
    """
    url = f"https://www.vinted.fr/items/{item_id}"
    api_data, handler = fr.intercept_item_api(page)
    info = {"created_at_ts": None, "brand": "", "color": "", "offers": None}
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp is not None and resp.status == 429:
            raise RateLimited()

        # ponytail: trust the HTTP 429 above only. The old raw-HTML text scan
        # false-matched Vinted's bundled i18n error dictionary ("rate limited" /
        # "too many requests"), which is present on EVERY page — causing an endless
        # false rate-limit loop even when nothing was blocked.
        # Live probe 2026-10-17: no /api/v2/items call fires any more and page.content() raised
        # "page is navigating" (locale redirect / hydration), which the blanket except below
        # swallowed -> nothing captured on ~80% of listings. Let the page settle, retry content(),
        # and never let a content() failure skip the on-page "posted X ago" fallback.
        try:
            page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        html = ""
        for _ in range(4):
            try:
                html = page.content()
                break
            except Exception:
                time.sleep(1.5)

        # Structured attributes (brand, colour) from the server-rendered JSON-LD.
        try:
            ld = fr.extract_jsonld(html)
            info["brand"] = ld.get("brand") or ""
            info["color"] = ld.get("color") or ""
        except Exception:
            pass

        # Buyer offers — the early-demand signal ("N acheteurs ont envoyé une offre").
        # Same page load we already did for publish time, so this is free. Reuses the
        # Phase 3 OFFER_PATTERNS. 0 = page loaded but no offer banner (a real zero);
        # None (default) = we couldn't read it.
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
            n = 0
            for pattern in fr.OFFER_PATTERNS:
                m = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
                if m:
                    n = int(m.group(1))
                    break
            # Sanity bound: a real listing has at most a handful of offers. A huge
            # value means the regex latched onto a stray number, not the offer
            # banner — discard it rather than let it pollute the demand score.
            info["offers"] = n if 0 <= n <= 500 else 0
        except Exception:
            pass

        # Publish time: exact item-API created_at if it fired, else the on-page
        # "posted X ago" (the current listing's age).
        for _ in range(6):
            if api_data.get("created_at_ts"):
                break
            time.sleep(0.3)
        if api_data.get("created_at_ts"):
            info["created_at_ts"] = int(api_data["created_at_ts"])
        else:
            try:
                txt = page.locator("[itemprop='upload_date'] span").inner_text(
                    timeout=6000
                )
                hrs = fr.parse_time(txt)
                if 0 < hrs < 9000:
                    pub = datetime.now(timezone.utc) - timedelta(hours=hrs)
                    info["created_at_ts"] = int(pub.timestamp())
            except Exception:
                pass
    except RateLimited:
        raise
    except Exception:
        pass
    finally:
        try:
            page.remove_listener("response", handler)
        except Exception:
            pass
    return info


def _publish_ts_worker(item_id: str):
    """
    Thread worker: connects to the shared Chrome over CDP, opens its own tab,
    fetches one listing's publish time, and closes the tab. Mirrors the main
    scraper's scrape_item_worker so several tabs run in parallel. Respects the
    shared rate-limit back-off: holds while a cooldown is active, and on a
    rate-limit page pauses all tabs and retries the same item.
    """
    for _attempt in range(3):
        if _cdp_dead():
            return item_id, None
        _wait_for_rate_limit()  # hold if a global cooldown is active
        try:
            with sync_playwright() as p:
                browser = _cdp_connect(p)
                if browser is None:
                    return item_id, None
                context = browser.contexts[0]
                page = context.new_page()
                try:
                    info = _publish_ts_from_page(page, item_id)
                    return item_id, info
                finally:
                    page.close()
                    time.sleep(random.uniform(0.3, 0.9))  # space out requests
        except RateLimited:
            # Pause all tabs; one tab refreshes until it clears, then retry item.
            if _begin_hold_become_prober():
                _probe_until_clear(item_id)
            else:
                _wait_for_rate_limit()
        except Exception:
            return item_id, None
    return item_id, None


# Circuit breaker: once Chrome stops accepting CDP connections, every remaining item would
# wait out its own connect timeout (live-observed 2026-10-05: 0/300 captured over ~16h).
# After N consecutive connect failures, workers return immediately so the product finishes
# fast and run_tracker.bat restarts Chrome before the next one.
_cdp_lock = threading.Lock()
_cdp_fails = 0


def _cdp_dead() -> bool:
    return _cdp_fails >= int(os.environ.get("VINTED_CDP_FAIL_LIMIT", "5"))


def _note_cdp(ok: bool) -> None:
    global _cdp_fails
    with _cdp_lock:
        _cdp_fails = 0 if ok else _cdp_fails + 1


# Stall watchdog. Client log 2026-10-13: the run stopped writing at 14:30, 100/124 into sold
# verification, and the task still read 'running' hours later (and a 15 h smeg tumbler run on
# 10-12 looked the same). A worker blocked inside Playwright never returns, so as_completed()
# - and the executor's own shutdown - waited forever. If NO item finishes for VINTED_STALL_SECS,
# treat Chrome as hung: stop the phase, mark it dead so later phases skip, keep saved progress.
_STALLED = False


def _as_completed_or_stall(futures):
    global _cdp_fails, _STALLED
    pending = set(futures)
    limit = int(os.environ.get('VINTED_STALL_SECS', '300'))
    while pending:
        done, pending = wait(pending, timeout=limit, return_when=FIRST_COMPLETED)
        if not done:
            print(f'   WARNING {_hm()} no item finished in {limit}s - Chrome looks hung; '
                  f'stopping this step ({len(pending)} left, progress saved).')
            for f in pending:
                f.cancel()
            with _cdp_lock:
                _cdp_fails = 10 ** 6
            _STALLED = True
            return
        yield from done


def _cdp_connect(p):
    """connect_over_cdp that feeds the circuit breaker; None if it failed."""
    try:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=30000)
    except Exception:
        _note_cdp(False)
        return None
    _note_cdp(True)
    return browser


def _enrich_queue(tracking: dict, cap) -> list:
    """Active listings still missing a publish time, NEWEST first-seen first, capped.
    Oldest-first (the old order) was survivorship-biased: with a backlog far larger than the
    per-run cap, only listings that had stayed up for weeks ever reached the front, while fast
    sellers vanished before being enriched - so measured time-to-sell skewed slow (client logs
    2026-10-09: 0 of 64 smeg / 0 of 46 owala measured sales under 48h; in local data, sold
    listings WITHOUT a publish time vanished within 48h 29% of the time vs 1% with one)."""
    todo = [iid for iid, row in tracking.items()
            if row["status"] == "active" and not row.get("created_at_ts")]
    todo.sort(key=lambda iid: tracking[iid].get("first_seen", ""), reverse=True)
    return todo[: int(cap)] if cap else todo


def _reoffer_queue(tracking: dict, now: datetime, cap) -> list:
    """Active listings first seen 24-72 h ago whose offers were not re-read yet, newest first, capped.
    Offers are read once, when a listing is first enriched (always ~0 then); listings that attract
    offers show it a day or more later (client 2026-10-20: wants to see 13+)."""
    out = []
    for iid, row in tracking.items():
        if row.get("status") != "active" or not row.get("first_seen"):
            continue
        try:
            fs = _parse(row["first_seen"])
            osa = _parse(row["offers_seen_at"]) if row.get("offers_seen_at") else None
        except ValueError:
            continue
        if timedelta(hours=24) <= now - fs <= timedelta(hours=72) and (osa is None or osa - fs <= timedelta(hours=12)):
            out.append(iid)
    out.sort(key=lambda i: tracking[i]["first_seen"], reverse=True)
    return out[: int(cap)] if cap else out


def enrich_publish_times(tracking: dict, path: str, workers: int = 5, todo=None) -> int:
    """
    Fill in publish time for active listings missing it, loading item pages across
    several tabs in parallel (default 5; override with VINTED_TRACK_WORKERS).
    Progress is checkpointed so a long backfill is interruptible/resumable.
    Cap per run with VINTED_MAX_ENRICH. Returns how many were captured this run.
    `todo` (a list of ids) replaces the default queue, e.g. the offers re-check.
    """
    if todo is None:
        todo = _enrich_queue(tracking, os.environ.get("VINTED_MAX_ENRICH"))
    if not todo:
        return 0
    workers = int(os.environ.get("VINTED_TRACK_WORKERS", workers))
    print(f"\n{_hm()} ⏳ Capturing publish time for {len(todo)} listings via {workers} parallel tabs...")
    print("   (progress is saved as it goes — safe to stop and resume next run)")

    captured = 0
    done = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_id = {
            executor.submit(_publish_ts_worker, iid): iid for iid in todo
        }
        for future in _as_completed_or_stall(future_to_id):
            iid = future_to_id[future]
            try:
                _id, info = future.result()
            except Exception:
                info = None
            done += 1
            if _cdp_dead():
                # Chrome is gone: drop what hasn't started instead of grinding through the
                # queue (the "stopped early" note below used to print after 300/300).
                for f in future_to_id:
                    f.cancel()
                break
            if info:
                row = tracking[iid]
                if info.get("created_at_ts") and not row.get("created_at_ts"):  # only fills a missing time
                    row["created_at_ts"] = info["created_at_ts"]
                    captured += 1
                if info.get("brand") and not row.get("brand"):
                    row["brand"] = info["brand"]
                if info.get("color") and not row.get("color"):
                    row["color"] = info["color"]
                if info.get("offers") is not None:
                    row["offers"] = info["offers"]
                    row["offers_seen_at"] = _fmt(datetime.now(timezone.utc))
                row["variant"] = build_variant(
                    row.get("title", ""), row.get("brand", ""), row.get("color", ""),
                    row.get("size", ""),
                )
            if done % 25 == 0:
                print(f"   ...{done}/{len(todo)} ({captured} captured)")
                save_tracking(path, tracking)  # checkpoint from the main thread

    finally:
        executor.shutdown(wait=not _STALLED, cancel_futures=True)
    save_tracking(path, tracking)
    print(f"   {_hm()} → publish time captured for {captured}/{len(todo)} listings")
    if _cdp_dead():
        print(f"   ⚠️  Chrome stopped accepting connections — stopped early at {done}/{len(todo)} "
              "(progress saved); Chrome will be restarted before the next product.")
    return captured


# ─────────────────────────────────────────
# SOLD VERIFICATION (VINTED_VERIFY_SOLD=1)
# ─────────────────────────────────────────
# A disappeared listing is NOT a sale. When a listing leaves the active catalog we open its
# item page and read Vinted's real status: only pages Vinted actually marks "Sold/Vendu" become
# sales; 404/removed/unavailable are excluded; a page that's live again was a glitch (revert to
# active). Anything we couldn't verify stays "disappeared" and is NOT counted as a sale.

# Item attributes read on the same page visit as the sold check. Category-sweep rows recorded by
# older versions have no size and a first-title-word brand ("Robe", "Pull"); the item page has
# the seller-entered values (verified live 2026-10-20 on vinted.fr/items/10141891209).
_ATTR_JS = """() => {
  const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : ""; };
  return {brand: t("[itemprop='name']"), size: t("[itemprop='size']") || t("[data-testid='item-attributes-size_familiar-link']") || t("[itemprop='size_familiar']"), color: t("[itemprop='color']"),
          upload: t("[itemprop='upload_date']"),
          crumbs: [...document.querySelectorAll("[itemprop='title']")].map(e => e.innerText.trim())};
}"""
_PAGE_DETAILS = {}  # item_id -> attributes; filled by _verify_sold_from_page, applied by verify_disappearances
_LOGGED_OUT_PAGES = [0]  # item pages seen logged out during verification (sold status unreadable)


def _details_from_attrs(a: dict, now: datetime) -> dict:
    """Item-page attributes -> tracking-row fields. 'brand' is always set once the page rendered
    its breadcrumb (empty = the page shows no brand, so a guessed one must go); other fields only
    when present. category = deepest breadcrumb, minus the '<brand> <category>' crumb."""
    if not a.get("crumbs"):
        return {}
    brand = (a.get("brand") or "").strip()
    crumbs = [c for c in a["crumbs"] if c and not (brand and c.lower().startswith(brand.lower()))]
    # shoes/bras use itemprop=size ("43"); clothing uses size_familiar ("Taille S", checked 2026-10-21)
    size = re.sub(r"^(taille|size|größe|taglia|talla|maat)\s*", "", (a.get("size") or "").strip(), flags=re.I)
    d = {"brand": brand, "size": size, "color": (a.get("color") or "").strip(),
         "category": crumbs[-1] if len(crumbs) > 1 else "",
         "category_path": " > ".join(crumbs) if len(crumbs) > 1 else ""}
    try:
        hrs = fr.parse_time(a["upload"]) if a.get("upload") else 0
    except Exception:
        hrs = 0
    # Only "il y a N heures/jours" (< 1 week) is precise enough to time a sale; "il y a 3 semaines"
    # can be a week off, which made time-to-sell meaningless on backlog-checked rows.
    if 0 < hrs < 168:
        d["created_at_ts"] = str(int((now - timedelta(hours=hrs)).timestamp()))
    return {k: v for k, v in d.items() if v or k == "brand"}


def _verify_sold_from_page(page, item_id: str) -> str:
    """Return the verified outcome for a disappeared listing:
    'sold' | 'removed' | 'active' | 'disappeared' (unverified). Raises RateLimited on a 429."""
    url = f"https://www.vinted.fr/items/{item_id}"
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception:
        return "disappeared"                     # couldn't load — leave unverified, never a sale
    if resp is not None and resp.status == 429:
        raise RateLimited()
    if resp is not None and resp.status in (404, 410):
        return "removed"                         # page gone — deleted/pulled, not a sale
    try:
        sold, status = fr.detect_sold_status(page)
    except Exception:
        return "disappeared"
    try:
        _PAGE_DETAILS[item_id] = _details_from_attrs(page.evaluate(_ATTR_JS), datetime.now(timezone.utc))
    except Exception:
        pass
    if sold:
        return "sold"                            # Vinted shows Sold/Vendu — a CONFIRMED sale
    if status == "unavailable":
        return "removed"
    if status == "active":
        # "active" only means no Sold badge was found. Logged out, Vinted shows a SOLD item with a
        # Buy button and no badge (checked 2026-10-20 on items/10141891209), and a page that never
        # rendered has no badge either: both would wrongly revert a sale. Trust it only on a
        # rendered (attributes read), logged-in page; otherwise leave it unverified.
        try:
            logged_out = page.locator("[data-testid^='header--login-button']").count() > 0
        except Exception:
            logged_out = True
        if logged_out:
            _LOGGED_OUT_PAGES[0] += 1
        if logged_out or not _PAGE_DETAILS.get(item_id):
            return "disappeared"
        return "active"                          # still live — relist/glitch, revert
    return "disappeared"


def _verify_sold_worker(item_id: str):
    """Thread worker mirroring _publish_ts_worker: own tab over CDP, verify one listing's
    real sold status, respect the shared rate-limit back-off."""
    for _attempt in range(3):
        if _cdp_dead():
            return item_id, "disappeared"
        _wait_for_rate_limit()
        try:
            with sync_playwright() as p:
                browser = _cdp_connect(p)
                if browser is None:
                    return item_id, "disappeared"
                context = browser.contexts[0]
                page = context.new_page()
                try:
                    return item_id, _verify_sold_from_page(page, item_id)
                finally:
                    page.close()
                    # VINTED_PAGE_PAUSE: extra seconds between pages (a hot IP needs a gentler pace)
                    time.sleep(random.uniform(0.3, 0.9) + float(os.environ.get("VINTED_PAGE_PAUSE", 0)))
        except RateLimited:
            if _begin_hold_become_prober():
                _probe_until_clear(item_id)
            else:
                _wait_for_rate_limit()
        except Exception:
            return item_id, "disappeared"
    return item_id, "disappeared"


def verify_disappearances(tracking: dict, path: str, ids: list, workers: int = 2) -> dict:
    """Verify the real status of newly-disappeared listings and reclassify them:
    sold → confirmed sale; removed → excluded; active → reverted; else left unverified.
    Only runs when VINTED_VERIFY_SOLD=1. Returns a small tally for the report.

    Cap per run with VINTED_MAX_VERIFY (mirrors enrich_publish_times' VINTED_MAX_ENRICH) — a
    product with a lot of history could otherwise verify thousands of listings in one run,
    turning a scheduled 6h-cadence job into a many-hour marathon. Unverified ids simply resolve
    on the NEXT run (they stay 'disappeared', never counted as sold in the meantime — safe)."""
    if not ids:
        return {}
    cap = os.environ.get("VINTED_MAX_VERIFY")
    if cap:
        ids = ids[: int(cap)]
    workers = int(os.environ.get("VINTED_TRACK_WORKERS", workers))
    print(f"\n{_hm()} 🔎 Verifying sold status of {len(ids)} disappeared listings via {workers} tabs "
          f"(a disappearance is not a sale until Vinted confirms it)...")
    tally = collections.Counter()
    done = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(_verify_sold_worker, iid): iid for iid in ids}
        for future in _as_completed_or_stall(futures):
            iid = futures[future]
            try:
                _id, outcome = future.result()
            except Exception:
                outcome = "disappeared"
            row = tracking.get(iid)
            details = _PAGE_DETAILS.pop(iid, None)
            if row is not None and details:
                for k in ("brand", "size", "color", "category", "category_path"):
                    if k in details:
                        row[k] = details[k]
                if details.get("created_at_ts") and not row.get("created_at_ts"):
                    row["created_at_ts"] = details["created_at_ts"]
            if row is not None:
                if outcome == "sold":
                    row["status"], row["sale_confirmed"] = "sold", True
                elif outcome == "removed":
                    row["status"], row["sale_confirmed"] = "removed", False
                elif outcome == "active":
                    row["status"] = "active"
                    row["disappeared_at"] = row["lifespan_hours"] = row["hours_tracked"] = ""
                    row["sale_confirmed"] = ""
                    # Else a listing that's live but missing from search is re-verified EVERY
                    # run (missed_runs stays >= DISAPPEARANCE_RUNS), eating the verify cap.
                    row["missed_runs"] = 0
                elif row["status"] != "sold":       # a confirmed sale re-opened for repair keeps its flag
                    row["sale_confirmed"] = False   # unverified — stays "disappeared", not a sale
            tally[outcome] += 1
            done += 1
            if done % 25 == 0:
                print(f"   ...{done}/{len(ids)} verified")
                save_tracking(path, tracking)
    finally:
        executor.shutdown(wait=not _STALLED, cancel_futures=True)
    save_tracking(path, tracking)
    print(f"   {_hm()} → confirmed sold: {tally['sold']} · removed: {tally['removed']} · "
          f"relisted/active: {tally['active']} · unverified: {tally['disappeared']}")
    if _cdp_dead():
        print("   ⚠️  Chrome stopped accepting connections — unverified ones retry next run.")
    if _LOGGED_OUT_PAGES[0]:
        print(f"   ⚠️  {_LOGGED_OUT_PAGES[0]} item pages showed Vinted LOGGED OUT — sold status can't be read; "
              "log in again in the tracker Chrome (those listings stay unverified, never reverted).")
    return dict(tally)


def _median(vals):
    vals = [v for v in vals if v not in ("", None)]
    return round(statistics.median(float(v) for v in vals), 1) if vals else None


def report(keyword: str, tracking: dict, newly: int, disappeared: int, first_run: bool):
    total = len(tracking)
    st = collections.Counter(r["status"] for r in tracking.values())
    active = st["active"]

    print("\n" + "=" * 60)
    print(f"{_hm()} 📦 SALES TRACKING — {keyword}")
    print("=" * 60)
    print(f"  Tracked (all time):   {total}")
    print(f"  Currently active:     {active}")
    pending = sum(1 for r in tracking.values()
                  if r["status"] == "active" and int(r.get("missed_runs") or 0) > 0)
    if pending:
        print(f"    (of which {pending} missing from this run's search, awaiting confirmation)")
    if _verify_sold_on():
        # Verified mode: report confirmed sales separately from removed / unverified, so a
        # disappearance is never presented as a sale.
        print(f"  Confirmed sold:       {st['sold']}")
        print(f"  Removed (not a sale): {st['removed']}")
        print(f"  Disappeared (unverified, NOT counted): {st['disappeared']}")
    else:
        print(f"  Sold / removed:       {st['disappeared']}")
    print(f"  New this run:         {newly}")
    print(f"  Disappeared this run: {disappeared}")

    # Time-to-sell distribution is built from COUNTED sales only (verified "sold", or — with
    # verification off — the legacy disappearance proxy).
    gone = [r for r in tracking.values()
            if r["status"] == "sold" or (r["status"] == "disappeared" and not _verify_sold_on())]
    if gone:
        # Time-to-sell from the listing's posting date. Show the DISTRIBUTION,
        # because a single median is dominated by old stock that lingered months.
        life = sorted(v for v in (_lifespan_hours(r) for r in gone) if v is not None)
        print("  ─────────────── time-to-sell ───────────────")
        if life:
            b24 = sum(1 for v in life if v <= 24)
            b48 = sum(1 for v in life if 24 < v <= 48)
            b7d = sum(1 for v in life if 48 < v <= 168)
            bold = sum(1 for v in life if v > 168)
            fast = b24 + b48
            print(f"  Measured (known posting date): {len(life)}")
            print(f"    ≤24h:    {b24}")
            print(f"    24-48h:  {b48}")
            print(f"    2-7d:    {b7d}")
            print(f"    >7d:     {bold}")
            print(f"  Fast movers (≤48h): {fast}/{len(life)} ({round(100 * fast / len(life))}%)")
            fresh = [v for v in life if v <= 168]  # turnover-relevant sellers
            if fresh:
                print(f"  Median among ≤7d sellers: {round(statistics.median(fresh), 1)}h")
        else:
            print("  (no posting dates captured yet — keep running to accumulate)")
        sell_through = round(100 * len(gone) / total, 1) if total else 0
        print(f"  Sell-through: {sell_through}% of tracked listings gone")

    # Per-variant market data — the concrete Phase 4 output (no abstract score lead).
    variants, window_days = variant_analysis(
        tracking, visual_slug=_slug(keyword), slug=_slug(keyword)
    )
    if variants:
        print(f"\n  ════ TOP OPPORTUNITY ════")
        print("  " + format_variant_card(f"{keyword} {variants[0]['variant']}", variants[0])
              .replace("\n", "\n  "))
        show_off = _show_offers()
        print(f"\n  ════ PRODUCT VARIANTS — market data ════")
        header = (f"  {'variant':<21}{'sales/30d':>10}{'demand':>8}{'velocity':>9}"
                  f"{'competition':>13}{'trend':>11}{'price':>7}")
        if show_off:
            header += f"{'offers':>8}"
        header += f"{'conf':>7}"
        print(header)
        for v in variants[:12]:
            md = v["median_days_to_sell"]
            vel = f"{md}d" if md is not None else "—"
            # Median, not mean (client feedback: a few extreme listings skew the mean) — the
            # full range + sold-price detail is in the TOP OPPORTUNITY card above and the CSV.
            price = f"{v['median_price']}€" if v["median_price"] is not None else "—"
            comp = f"{v['competition_level']}({v['competition']})"
            line = (
                f"  {v['variant'][:21]:<21}{('~' + str(v['est_sales_30d'])):>10}"
                f"{v['demand_level']:>8}{vel:>9}"
                f"{comp:>13}{v['trend']:>11}{price:>7}"
            )
            if show_off:
                line += f"{_offers_display(v):>8}"
            line += f"{v['confidence']:>7}"
            print(line)
        print(f"\n  📊 Data transparency:")
        print(f"     • Estimated sales — MARKETPLACE-WIDE sales of that exact variant, "
              f"calculated from listing turnover over the last {window_days} days "
              f"(Estimated; not a per-seller forecast)")
        print(f"     • Demand — level of proven monthly sales volume (High ≥20/mo, "
              f"Medium ≥8/mo)")
        print(f"     • Velocity — median posted→sold time (Calculated)")
        print(f"     • Competition — active listings of that exact variant now (Calculated)")
        print(f"     • Trend — vs the previous run (Signal); 'Building' until 2+ runs exist")
        print(f"     • Price — average of current listing prices (Calculated)")
        if show_off:
            print(f"     • Offers — total live buyer offers across the variant's active "
                  f"listings (early-demand Signal; '—' until item pages are enriched, "
                  f"builds up over runs)")
        print(f"     • Confidence reflects sample size + tracking length")
        print(f"     • Updated: {variants[0]['last_updated']}")

    if first_run:
        print("  ───────────────────────────────────")
        print("  🟡 Baseline captured. Run again in ~12-24h — time-to-sell")
        print("     numbers appear once listings start disappearing.")
    print("=" * 60)


def main():
    # Phase 6 Layer 1 — seedless category sweep. Set VINTED_CATALOG_ID=<vinted category id>
    # to track a WHOLE category with no search keyword (the foundation the autonomous-discovery
    # detector will later mine). VINTED_CATEGORY_NAME labels the output files. Everything
    # downstream (turnover, variants, history snapshots) is unchanged — only the fetch differs.
    cat_id_env = os.environ.get("VINTED_CATALOG_ID")
    seedless = bool(cat_id_env)
    catalog_id = int(cat_id_env) if seedless else None
    if seedless:
        keyword = os.environ.get("VINTED_CATEGORY_NAME") or f"category {cat_id_env}"
    else:
        keyword = (
            (sys.argv[1] if len(sys.argv) > 1 else None)
            or os.environ.get("VINTED_KEYWORDS")
            or "stanley quencher"
        )
        keyword = keyword.split(",")[0].strip()
    path = os.path.join(TRACK_DIR, f"{_slug(keyword)}.csv")

    with sync_playwright() as p:
        try:
            _, context = fr.connect(p)
        except Exception as e:
            print(
                "⚠️  Could not connect to Chrome on port 9222. Chrome must be running "
                "with --remote-debugging-port=9222 and logged into Vinted (see "
                f"AUTOMATION.md). Skipping this run. [{type(e).__name__}]"
            )
            sys.exit(4)
        # Live-observed 2026-08-29: a transient DNS blip (net::ERR_NAME_NOT_RESOLVED) on this
        # very first real navigation crashed with an unhandled traceback, which then repeated
        # identically for every remaining product in the batch (each one hits this same line
        # first) — one brief network hiccup killed the rest of the whole run. The Chrome-health
        # check above only verifies the LOCAL debug port (127.0.0.1), never real internet
        # reachability, so it can't catch this class of failure. Retry once after a short pause
        # — DNS blips are typically transient — then fail this ONE product cleanly (matching the
        # existing "could not connect" exit path) rather than crash uncaught.
        try:
            page = fr.get_or_create_page(context, "https://www.vinted.fr")
        except Exception as e:
            print(f"⚠️  Could not reach vinted.fr ({type(e).__name__}) — retrying in 15s...")
            time.sleep(15)
            try:
                page = fr.get_or_create_page(context, "https://www.vinted.fr")
            except Exception as e2:
                print(f"⚠️  Still could not reach vinted.fr. Skipping this run. "
                      f"[{type(e2).__name__}]")
                sys.exit(4)
        time.sleep(3)
        if page.query_selector("[data-testid='header--login-button']"):
            if os.environ.get("VINTED_AUTOMATED"):
                # Unattended run: never block on input(). Exit so the scheduler
                # can flag it; a human re-logs the profile in when convenient.
                print("⚠️  Not logged in and running automated — exiting. Re-login "
                      "the Vinted profile; the next scheduled run will resume.")
                sys.exit(2)
            print("⚠️  Not logged in — log in, then press ENTER.")
            input()
        print("🔄 Getting cookies and access token...")
        cookies, token = fr.get_cookies_and_token(context, page)
        print(f"   🔑 Access token: {'found' if token else 'NOT FOUND'}")

        # Cross-border coverage: VINTED_DOMAINS overrides which Vinted country domains
        # get merged (by listing id) into one catalog fetch. Defaults to the client's
        # full shipping zone (Phase 4 delivery, 2026-07-08) — FR alone was missing ~45%
        # of the listings actually visible there (measured on stanley quencher: 287 on
        # .fr vs 522 union across 8 domains). Set VINTED_DOMAINS="fr" to go back to
        # France-only.
        domains = tuple(
            d.strip()
            for d in os.environ.get(
                "VINTED_DOMAINS", "fr,be,lu,nl,de,at,es,pt,it,ie"
            ).split(",")
            if d.strip()
        )
        # Refine the query + filter to product identity (client feedback 2026-07-11)
        # so the tracker follows the actual product, not every generic 'gourde'.
        # Toggle off with VINTED_IDENTITY_FILTER=0.
        # Seedless sweeps browse a whole category (empty search_text + catalog_id), so the
        # identity refine/filter — which follows a specific product — is off for them.
        identity_on = (not seedless) and os.environ.get("VINTED_IDENTITY_FILTER", "1") != "0"
        search_q = fr.normalize_search_query(keyword) if identity_on else ("" if seedless else keyword)
        print(f"\n🔍 Fetching complete active catalog for: {keyword}"
              + ("  (seedless category sweep)" if seedless else "")
              + (f'  → "{search_q}"' if identity_on and search_q != keyword else "")
              + (f"  (domains: {', '.join(domains)})" if len(domains) > 1 else ""))
        # Fetch ALL pages and disable the age-based early stop so the active set is
        # complete — otherwise items would look 'disappeared' just for being old.
        raw = fr.fetch_catalog_multi_domain(
            search_q, cookies, token, domains=domains,
            max_pages=None, stop_when_old_ratio=2.0, catalog_id=catalog_id,
        )
        if identity_on:
            # Tiered relevance: track the brand's whole range (exact product +
            # same-family variants + other same-brand models) so the variant report
            # can surface the best-performing products around the search, not just
            # the exact one. Drops only off-brand/generic listings (tier 4).
            before = len(raw)
            raw, tiers = fr.rank_by_identity(raw, keyword, drop_offbrand=True)
            if before != len(raw):
                print(f"   🎯 relevance: {tiers.get(1,0)} exact · {tiers.get(2,0)} "
                      f"same-family · {tiers.get(3,0)} same-brand · dropped "
                      f"{tiers.get(4,0)} off-brand")
        print(f"   {_hm()} → {len(raw)} active listings right now")
    # Auth session closed; the enrichment workers open their own CDP connections.

    now = datetime.now(timezone.utc)
    tracking = load_tracking(path)
    # Rows tracked before the product-type filter (e.g. Smeg kettles/fridges under "smeg
    # tumbler") would otherwise all look 'disappeared' and be queued for sold-verification, and
    # kettle sales would pollute the bottle numbers. Move them to an archive CSV instead.
    off = {i: r for i, r in tracking.items() if fr.is_off_product_type(r.get("title", ""), keyword, r.get("category", ""))}
    if off:
        arch = path.replace(".csv", "_offtype_archive.csv")
        new_file = not os.path.exists(arch)
        with open(arch, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRACK_FIELDS)
            if new_file:
                w.writeheader()
            for r in off.values():
                w.writerow({k: r.get(k, "") for k in TRACK_FIELDS})
        for i in off:
            del tracking[i]
        print(f"   🧹 moved {len(off)} off-product-type rows (not {keyword.split()[-1]}s) to {arch}")
    first_run = len(tracking) == 0
    # One-time cleanup of publish times written by the withdrawn photo-timestamp fallback.
    _marker = os.path.join(TRACK_DIR, ".photo_ts_repaired")
    if tracking and not os.path.exists(_marker):
        cleared = _repair_photo_ts(tracking)
        if cleared:
            print(f"   🧹 cleared {cleared} publish times from the withdrawn photo fallback "
                  "(they are re-captured from the item page over the next runs)")
        os.makedirs(TRACK_DIR, exist_ok=True)
        open(_marker, "w").close()

    # SAFETY GUARD — a failed/partial catalog fetch (401 auth expiry, rate limit,
    # network drop) returns few/zero listings. Without this guard, update_tracking
    # would mark EVERY tracked listing as "disappeared/sold" and corrupt the data.
    # If the active count collapses versus what we already know is live, skip this
    # run entirely and preserve the tracking file.
    # Baseline = rows the PREVIOUS run actually saw (last_seen == newest stamp), not every
    # "active" row: those include listings missing from search awaiting confirmation, which
    # inflate the baseline and made a normal run look like a collapsed fetch.
    _last_run = max((r.get("last_seen") or "" for r in tracking.values()), default="")
    prior_active = sum(1 for r in tracking.values()
                       if r.get("status") == "active" and r.get("last_seen") == _last_run)
    current_ids = {str(i.get("id")) for i in raw if i.get("id")}
    # A category sweep is capped (~960 per domain), so it can never see most of its tracked
    # listings (client log 2026-10-19: women 3211 vs 8037, skipped twice). For those, only an
    # outright collapse counts as a failed fetch.
    _floor = 0.1 if seedless else 0.5
    if not first_run and prior_active >= 20 and len(current_ids) < max(prior_active * _floor, 500 if seedless else 0):
        print(
            f"⚠️  Catalog returned only {len(current_ids)} listings vs {prior_active} "
            f"active previously — almost certainly a failed/partial fetch (expired "
            f"login or rate limit). Skipping this run to protect the tracking data. "
            f"Re-login to Vinted and the next run will resume normally."
        )
        sys.exit(3)

    if seedless and not first_run:
        prev_ids = {i for i, r in tracking.items() if r.get("last_seen") == _last_run}
        print(_coverage_line(current_ids, prev_ids))

    newly, disappeared = update_tracking(tracking, raw, now)

    # Data reliability (opt-in VINTED_VERIFY_SOLD=1): a disappeared listing is NOT a sale.
    # Verify each newly-disappeared listing against Vinted's real item-page status — only the
    # ones actually marked Sold become sales; removed/deleted are excluded, relisted revert.
    if os.environ.get("VINTED_VERIFY_SOLD") == "1":
        now_str = _fmt(now)
        gone_ids = [iid for iid, r in tracking.items()
                    if r["status"] == "disappeared" and r.get("disappeared_at") == now_str]
        if seedless:
            # A category sweep sees ~960 listings/domain, so thousands "disappear" per run and the
            # 300/run cap used to verify only that run's first arrivals; the rest were never
            # retried (client files 2026-10-20: 25,711 unverified vs 215 confirmed sold). Spend
            # the cap on the whole backlog instead, repeat products first.
            import product_groups
            gone_ids = product_groups.verification_order(tracking)
        try:
            verify_disappearances(tracking, path, gone_ids)
            save_tracking(path, tracking)
        except Exception as e:
            print(f"⚠️  sold verification skipped: {type(e).__name__}: {e}")

    # Phase 5 (opt-in): embed new listings' cover photos and assign stable visual
    # variant ids. MUST happen at fetch time — Vinted photo URLs expire, so a
    # listing's image is only reliably downloadable while it's in the live catalog.
    # Runs after the corruption guard so we never embed a failed/partial fetch.
    if os.environ.get("VINTED_VISUAL") == "1":
        try:
            import image_cluster

            image_cluster.update_visual_index(_slug(keyword), raw)
        except Exception as e:
            print(f"⚠️  visual variant indexing skipped: {type(e).__name__}: {e}")

    # Capture publish time across parallel tabs for active listings missing it. MUST run
    # before vision identification below — offers (the vision targeting signal) live on the
    # item page and are captured here, not in the catalog feed.
    if os.environ.get("VINTED_TRACK_ENRICH", "1") != "0":
        enrich_publish_times(tracking, path)
        if seedless:
            try:
                enrich_publish_times(tracking, path,
                                     todo=_reoffer_queue(tracking, now, os.environ.get("VINTED_MAX_REOFFER", "300")))
            except Exception as e:
                print(f"⚠️  offers re-check skipped: {type(e).__name__}: {e}")

    # Phase 5 Stage A (opt-in): vision-AI product identification → full, specific
    # titles. Uses the deterministic stub by default (free, no key); set
    # VINTED_VISION_PROVIDER=anthropic once the client's API key is in the env to use
    # the real vision model. Client spec (2026-09-09): identify only listings already
    # showing real buyer interest (offers ≥ VINTED_VISION_MIN_OFFERS, default 4) instead
    # of a blanket sweep — capped per run; results are cached so each product is paid for
    # once (VINTED_DEDUP=1 extends that across sellers/listings of the same product).
    if os.environ.get("VINTED_VISION") == "1":
        try:
            import vision_identify

            # Prefer each listing's own declared colour (structured attribute captured during
            # enrichment) over the vision model's photo-guess in the composed title.
            colours = {str(iid): row.get("color", "")
                       for iid, row in tracking.items() if row.get("color")}
            offers = {str(iid): row.get("offers", "") for iid, row in tracking.items()}
            idents = vision_identify.identify_listings(
                raw, _slug(keyword), colours=colours, offers=offers
            )
            # Stage B (opt-in): look up a reference product + price band for GENERIC no-brand
            # items. Off unless VINTED_REFERENCE=1; branded items are untouched.
            if os.environ.get("VINTED_REFERENCE") == "1":
                import reference_lookup
                idents = reference_lookup.enrich_generics(idents, _slug(keyword))
            vpath = vision_identify.save_identities(idents, _slug(keyword))
            named = sum(1 for v in idents.values() if v.get("generated_title"))
            min_offers = os.environ.get("VINTED_VISION_MIN_OFFERS", "4")
            print(f"🔎 product identification: {named}/{len(idents)} titled (offers≥{min_offers}) "
                  f"→ {vpath} (provider: {os.environ.get('VINTED_VISION_PROVIDER', 'stub')})")
        except Exception as e:
            print(f"⚠️  product identification skipped: {type(e).__name__}: {e}")

    save_tracking(path, tracking)
    report(keyword, tracking, newly, disappeared, first_run)
    print(f"\n💾 Tracking state saved: {path}")

    variants, _ = variant_analysis(
        tracking, visual_slug=_slug(keyword), slug=_slug(keyword)
    )
    vpath = save_variant_report(variants, f"variant_report_{_slug(keyword)}.csv")
    if vpath:
        print(f"💾 Variant report saved: {vpath} ({len(variants)} variants)")
    save_variant_snapshot(variants, _slug(keyword))  # enables next run's trend column

    # Phase 6 Layer 2 (opt-in): rank the sweep's products by discovery score (demand +
    # velocity + low competition + RISING momentum from the snapshot history) and surface the
    # hidden opportunities. Momentum strengthens as daily history accumulates.
    if os.environ.get("VINTED_DISCOVER") == "1":
        try:
            import discover_opportunities as disc
            ranked = disc.rank_opportunities(variants, _slug(keyword))
            disc.print_top(ranked)
            disc.print_alerts(disc.alerts(ranked))  # smart alerts: the actionable subset
            opath = disc.save_opportunities_report(ranked, _slug(keyword))
            print(f"💾 Opportunities report saved: {opath} ({len(ranked)} ranked)")
        except Exception as e:
            print(f"⚠️  opportunity discovery skipped: {type(e).__name__}: {e}")


def _demo() -> None:
    """Self-check for build_variant's graceful degradation (no network). Regression: this used
    to require capacity+colour, so clothing/plush (no capacity concept) always got "" — 7 of 14
    real watch-list products produced zero variants no matter how much history accumulated."""
    # bottle: capacity+colour path is UNCHANGED from the original behaviour/priority.
    assert build_variant("Stanley Quencher 40oz", "Stanley", "rose", "") == "40oz pink"
    # clothing: no capacity, but a real size + colour -> forms a key.
    assert build_variant("Ralph Lauren Polo", "Ralph Lauren", "Blanc", "XXL") == "XXL white"
    # a no-op size (Vinted's "one size" placeholder) must NOT be used as a key component.
    assert build_variant("Ralph Lauren cap", "Ralph Lauren", "noir", "Taille unique") == "black"
    # plush/generic: no capacity, no size, colour alone still forms a key.
    assert build_variant("Jellycat Dragon Blue", "Jellycat", "blue", "") == "blue"
    # truly nothing usable -> still empty, same as before.
    assert build_variant("mystery item", "", "", "") == ""
    print("track_sales self-check OK: build_variant degrades gracefully for non-bottle products")

    # _sold_within_days: verified trailing-30-day window (client spec, 2026-09-04).
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    recent = {"last_seen": "2026-08-20 00:00:00"}  # 15 days ago
    old = {"last_seen": "2026-06-01 00:00:00"}      # ~95 days ago
    assert _sold_within_days(recent, now) is True
    assert _sold_within_days(old, now) is False
    assert _sold_within_days({"last_seen": "garbage"}, now) is False

    # compute_variant_opportunity: competition must never move the score (client: "a good,
    # high-selling product can still face significant competition") — the parameter is gone
    # entirely now, so there's no way to feed it in. Velocity's full score requires selling
    # within 72h; one day slower already scores lower.
    fast = compute_variant_opportunity(est_sales_30d=1, median_days=3, offers_total=0, offers_measured=True)
    slow = compute_variant_opportunity(est_sales_30d=1, median_days=3.1, offers_total=0, offers_measured=True)
    assert fast["score"] > slow["score"], (fast, slow)
    maxed = compute_variant_opportunity(est_sales_30d=40, median_days=1, offers_total=25, offers_measured=True)
    assert maxed == {"score": 100, "verdict": "🚀 High Resale Opportunity"}, maxed

    # Enrichment queue: newest first-seen first, only active + still missing a publish time.
    q = {
        "old": {"status": "active", "created_at_ts": "", "first_seen": "2026-09-01 10:00:00"},
        "new": {"status": "active", "created_at_ts": "", "first_seen": "2026-10-09 03:00:00"},
        "mid": {"status": "active", "created_at_ts": "", "first_seen": "2026-09-20 10:00:00"},
        "done": {"status": "active", "created_at_ts": "123", "first_seen": "2026-10-09 04:00:00"},
        "gone": {"status": "sold", "created_at_ts": "", "first_seen": "2026-10-09 05:00:00"},
    }
    assert _enrich_queue(q, None) == ["new", "mid", "old"], _enrich_queue(q, None)
    assert _enrich_queue(q, "2") == ["new", "mid"]

    # Withdrawn photo-timestamp fallback: rows enriched since it shipped lose their publish time
    # and lifespan; older rows and rows never enriched are untouched.
    rp = {
        "bad": {"created_at_ts": "1760000000", "lifespan_hours": "50", "offers_seen_at": "2026-10-15 11:00:00"},
        "ok": {"created_at_ts": "1750000000", "lifespan_hours": "70", "offers_seen_at": "2026-10-14 09:00:00"},
        "none": {"created_at_ts": "1750000001", "lifespan_hours": "", "offers_seen_at": ""},
    }
    assert _repair_photo_ts(rp) == 1
    assert rp["bad"]["created_at_ts"] == "" and rp["bad"]["lifespan_hours"] == ""
    assert rp["ok"]["created_at_ts"] == "1750000000" and rp["none"]["created_at_ts"] == "1750000001"

    # Lifespan is publish -> LAST SEEN, not detection: a listing posted 30h before it was last
    # seen, detected gone two days later, must read 30h (a fast mover), not 78h. Old stored
    # lifespan_hours is only a fallback.
    t_pub = datetime(2026, 10, 10, 6, 0, 0, tzinfo=timezone.utc)
    lr = {"created_at_ts": str(int(t_pub.timestamp())), "last_seen": "2026-10-11 12:00:00",
          "lifespan_hours": "78.0"}
    assert _lifespan_hours(lr) == 30.0, _lifespan_hours(lr)
    assert _lifespan_hours({"created_at_ts": "", "last_seen": "2026-10-11 12:00:00",
                            "lifespan_hours": ""}) is None
    assert _lifespan_hours({"created_at_ts": "", "last_seen": "", "lifespan_hours": "12"}) == 12.0
    # product grouping: brand + type words identify a product; colour/size are variants; no brand = unidentified
    import product_groups as pg
    assert pg.product_key({"brand": "Zara", "title": "Veste en jean blanche Zara zippée taille 38"}) == ("zara", "jacket")
    assert pg.product_key({"brand": "Zara", "title": "Giacca jeans Zara bianca M"}) == ("zara", "jacket")   # IT == FR
    assert pg.product_key({"brand": "Zara", "title": "T-shirt Zara noir S"}) == ("zara", "tshirt")
    assert pg.product_key({"brand": "Inconnu", "title": "Veste en jean"}) is None
    assert pg.product_key({"brand": "Smeg", "title": "Gourde isotherme Smeg 500 ml crème"}) == ("smeg", "bottle")
    assert pg.product_key({"brand": "Nao", "title": "Figura porcelana Nao"}) == ("nao", "figurine")
    assert pg.product_key({"brand": "Nike", "title": "Baskets Nike Air 39"}) == ("nike", "shoes")     # plural
    assert pg.product_key({"brand": "Maisons du Monde", "title": "Assiettes plates x6"}) == ("maisons du monde", "plate")
    assert pg.product_key({"brand": "Ikea", "title": "Kallax étagère"}) == ("ikea", "kallax")          # fallback word
    assert pg.product_key({"brand": "Accessories", "title": "Veste en jean"}) is None
    assert pg.product_key({"brand": "Vintage", "title": "Robe rayée vintage rouge"}) is None   # guessed brand
    assert pg.product_key({"brand": "Mug", "title": "Tazze"}) is None
    # item-page attributes (verification visit) repair old sweep rows; Vinted's category gives the type
    _now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    _crumbs = ["Femmes", "Chaussures", "Chaussures de sport", "Chaussures de course", "Nike Chaussures de course"]
    d = _details_from_attrs({"brand": "Nike", "size": "43", "color": "Bordeaux, Noir",
                             "upload": "Il y a 2 jours", "crumbs": _crumbs}, _now)
    assert d == {"brand": "Nike", "size": "43", "color": "Bordeaux, Noir", "category": "Chaussures de course",
                 "category_path": "Femmes > Chaussures > Chaussures de sport > Chaussures de course",
                 "created_at_ts": str(int((_now - timedelta(hours=48)).timestamp()))}, d
    assert _details_from_attrs({"crumbs": ["Maison", "Décoration", "Bougies"]}, _now) == {
        "brand": "", "category": "Bougies", "category_path": "Maison > Décoration > Bougies"}
    assert _details_from_attrs({"brand": "Nike", "crumbs": []}, _now) == {}  # page didn't render: change nothing
    assert _details_from_attrs({"brand": "Zara", "size": "Taille S", "crumbs": ["Femmes", "Vêtements", "Pulls"]}, _now)["size"] == "S"
    assert "created_at_ts" not in _details_from_attrs({"brand": "Nike", "upload": "Il y a 3 semaines",
                                                      "crumbs": _crumbs}, _now)  # too coarse to time a sale
    assert pg.identified_key({"brand": "Nike", "title": "Scarpe LunarTempo", "size": "43", "color": "Bordeaux, Noir",
                              "category": "Chaussures de course"}) == ("nike", "shoes", "lunartempo")
    # per-family identity (client's categories): shoes need a model + colour + size, face care a name + volume,
    # pans a diameter, kitchenware only brand + type
    _sp = "Femmes > Chaussures > Chaussures de sport > Chaussures de course"
    _shoe = {"brand": "Nike", "title": "Nike Pegasus 40 noir", "size": "38", "color": "Noir", "category_path": _sp}
    assert pg.identified_key(_shoe) == ("nike", "shoes", "pegasus 40") and pg.variant_of(_shoe) == "black 38"
    assert pg.identified_key(dict(_shoe, title="Nike running noir")) is None            # no model word
    # model numbers keep models apart; the listing's own size is not a model number; materials are not models
    assert pg.identified_key(dict(_shoe, title="Nike Air Max 90 noir"))[2] == "air max 90"
    assert pg.identified_key(dict(_shoe, title="Nike Air Max 95 noir"))[2] == "air max 95"
    assert pg.identified_key(dict(_shoe, title="Nike Pegasus 38 noir"))[2] == "pegasus"
    assert pg.identified_key(dict(_shoe, brand="Zara", title="Bottes Zara cuir noir")) is None
    assert pg.identified_key(dict(_shoe, brand="Reebok", title="Reebok deportivas negras")) is None
    assert pg.product_key({"brand": "indépendante", "title": "pull col v noir"}) is None
    _face = {"brand": "Caudalie", "title": "Caudalie Vinoperfect sérum éclat 30 ml",
             "category_path": "Femmes > Beauté > Soins du visage > Sérums"}
    _fk = pg.identified_key(_face)
    assert _fk and len(_fk) == 3 and _fk[0] == "caudalie" and pg.variant_of(_face) == "30ml", _fk
    assert pg.identified_key(dict(_face, title="Caudalie Vinoperfect sérum éclat")) is None   # no volume
    _pan = {"brand": "Tefal", "title": "Poêle Tefal Ingenio 28 cm", "category_path": "Maison > Cuisine > Poêles"}
    assert pg.identified_key(_pan) == ("tefal", "pan") and pg.variant_of(_pan) == "28cm"
    assert pg.identified_key(dict(_pan, title="Poêle Tefal Ingenio")) is None              # no diameter
    _kit = {"brand": "Le Creuset", "title": "Cocotte Le Creuset",
            "category_path": "Maison > Cuisine > Ustensiles de cuisine"}
    assert len(pg.identified_key(_kit)) == 2, pg.identified_key(_kit)
    assert pg.product_key({"brand": "Zara", "title": "Zara", "category": "Montres"}) == ("zara", "montres")
    assert _coverage_line({"1", "2", "3"}, {"3", "4"}) == "   coverage: complete — 1 listings overlap the previous run"
    assert _coverage_line({"1", "2"}, {"3", "4"}).startswith("   ⚠️  coverage: GAP")
    # offers re-check queue: 24-72 h old and not re-read yet
    _n = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    _fs = lambda h: _fmt(_n - timedelta(hours=h))
    _rq = {"old": {"status": "active", "first_seen": _fs(48), "offers_seen_at": ""},
           "young": {"status": "active", "first_seen": _fs(2), "offers_seen_at": ""},
           "done": {"status": "active", "first_seen": _fs(48), "offers_seen_at": _fmt(_n - timedelta(hours=18))},
           "first_read": {"status": "active", "first_seen": _fs(60), "offers_seen_at": _fs(59)},
           "gone": {"status": "disappeared", "first_seen": _fs(48), "offers_seen_at": ""}}
    assert _reoffer_queue(_rq, _n, "300") == ["old", "first_read"], _reoffer_queue(_rq, _n, "300")
    assert _reoffer_queue(_rq, _n, "1") == ["old"]
    assert _coverage_line({"1"}, set()).startswith("   ⚠️  coverage: GAP")
    # check_model_sales: a row read on its page (category set) matches on the PAGE colour, not the old stored variant
    import check_model_sales as cms, tempfile
    _cdir = tempfile.mkdtemp()
    _crow = {k: "" for k in TRACK_FIELDS}
    _crow.update({"id": "1", "title": "Gourde Smeg 500 ml", "brand": "Smeg", "variant": "0.5l", "color": "Crème",
                  "category": "Gourdes", "status": "sold", "last_seen": _fmt(datetime.now(timezone.utc)), "price": "16"})
    with open(os.path.join(_cdir, "s.csv"), "w", newline="", encoding="utf-8") as _f:
        _w = csv.DictWriter(_f, fieldnames=TRACK_FIELDS)
        _w.writeheader()
        _w.writerow(_crow)
    assert cms.check(os.path.join(_cdir, "s.csv"), "smeg", "cream", "0.5l", hours=24)["sold"] == 1
    _crow["category"] = ""                      # not checked on its page: old stored variant ("0.5l", no colour)
    with open(os.path.join(_cdir, "s.csv"), "w", newline="", encoding="utf-8") as _f:
        _w = csv.DictWriter(_f, fieldnames=TRACK_FIELDS)
        _w.writeheader()
        _w.writerow(_crow)
    assert cms.check(os.path.join(_cdir, "s.csv"), "smeg", "cream", "0.5l", hours=24)["sold"] == 0
    _real_w = globals()["_verify_sold_worker"]

    def _fake_w(iid):
        _PAGE_DETAILS[iid] = d
        return iid, "sold"
    globals()["_verify_sold_worker"] = _fake_w
    try:
        import tempfile
        _trk = {"n": {"id": "n", "title": "Scarpe Nike", "status": "disappeared", "brand": "Scarpe", "size": "",
                      "color": "", "created_at_ts": "", "last_seen": "2026-10-18 00:00:00"}}
        verify_disappearances(_trk, os.path.join(tempfile.mkdtemp(), "t.csv"), ["n"], workers=1)
        assert _trk["n"]["status"] == "sold" and _trk["n"]["brand"] == "Nike" and _trk["n"]["size"] == "43", _trk
        assert _trk["n"]["category"] == "Chaussures de course" and _trk["n"]["created_at_ts"], _trk
        assert _trk["n"]["category_path"] == "Femmes > Chaussures > Chaussures de sport > Chaussures de course", _trk
    finally:
        globals()["_verify_sold_worker"] = _real_w

    # "active" (no Sold badge) is trusted only on a rendered, logged-in page
    class _Loc:
        def __init__(self, n):
            self.n = n

        def count(self):
            return self.n

    class _Page:
        def __init__(self, logged_out, attrs):
            self.logged_out, self.attrs = logged_out, attrs

        def goto(self, *a, **k):
            return type("R", (), {"status": 200})()

        def locator(self, sel):
            return _Loc(1 if (self.logged_out and "login-button" in sel) else 0)

        def evaluate(self, js):
            return self.attrs

    _attrs = {"brand": "Nike", "size": "43", "crumbs": _crumbs}
    assert _verify_sold_from_page(_Page(False, _attrs), "p1") == "active"
    assert _verify_sold_from_page(_Page(True, _attrs), "p2") == "disappeared" and _LOGGED_OUT_PAGES[0] == 1
    assert _verify_sold_from_page(_Page(False, {}), "p3") == "disappeared"   # page never rendered
    _LOGGED_OUT_PAGES[0] = 0
    assert pg.product_key({"brand": "", "title": "Veste en jean"}) is None
    assert pg.product_key({"brand": "Zara", "title": "Zara blanc S"}) is None
    # discovery ranking: 30 sold in-window + 3 active -> alert-rule pass
    zrows = []
    for i in range(30):
        zrows.append({"id": f"s{i}", "title": "Veste Zara noire", "brand": "Zara", "status": "sold", "price": "20",
                      "created_at_ts": str(int(t_pub.timestamp())), "last_seen": "2026-10-11 06:00:00",
                      "lifespan_hours": "", "color": "", "size": "M"})
    for i in range(3):
        zrows.append({"id": f"a{i}", "title": "Veste Zara noire", "brand": "Zara", "status": "active", "price": "25",
                      "created_at_ts": "", "last_seen": "2026-10-11 06:00:00", "lifespan_hours": "",
                      "color": "", "size": "M"})
    # promised rule: no colour, or no size/capacity ('Autre' is not a size) -> unidentified, never counted
    zrows += [dict(zrows[0], id="nosize", size=""), dict(zrows[0], id="autre", size="Autre"),
              dict(zrows[0], id="nocol", title="Veste Zara")]
    assert all(pg.identified_key(r) is None for r in zrows[-3:])
    assert pg.identified_key({"brand": "Smeg", "title": "Gourde Smeg 500 ml crème", "size": ""}) == ("smeg", "bottle")
    zp = pg.rank_products(zrows)[0][0]
    assert zp["variants"] == "black M: 30 sold", zp["variants"]
    # brand ranking: 30-day confirmed sales, trend = recent half vs earlier half, guessed brands ignored
    brows = ([{"id": f"z{i}", "brand": "Zara", "title": "Robe Zara", "status": "sold", "price": str(10 + i),
               "last_seen": f"2026-10-{25 + i} 00:00:00" if i < 4 else f"2026-10-{6 + i} 00:00:00", "created_at_ts": ""}
              for i in range(6)]
             + [{"id": f"n{i}", "brand": "Nike", "title": "Baskets Nike", "status": "sold", "price": "40",
                 "last_seen": "2026-10-28 00:00:00", "created_at_ts": ""} for i in range(3)]
             + [{"id": "a1", "brand": "Nike", "title": "Baskets Nike", "status": "active", "price": "45",
                 "last_seen": "2026-10-28 00:00:00", "created_at_ts": "", "offers": "13"},
                {"id": "g1", "brand": "Robe", "title": "Robe longue", "status": "sold", "price": "5",
                 "last_seen": "2026-10-28 00:00:00", "created_at_ts": ""}])
    br = pg.rank_brands(brows, now=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert [b["brand"] for b in br] == ["zara", "nike"], br
    assert br[0]["trend_pct"] == 100 and br[0]["median_sold_price"] == 12.5 and br[0]["top_products"] == "dress: 6 sold", br[0]
    assert br[1]["trend_pct"] is None and br[1]["max_offers_active"] == 13, br[1]
    # a sweep file stamps sales on a few days only (< 5 distinct dates): no trend is invented from them
    coarse = pg.rank_brands([dict(r, last_seen="2026-10-28 00:00:00") for r in brows[:6]], now=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert coarse[0]["trend_dated"] is False and coarse[0]["trend_pct"] is None, coarse[0]
    for r in brows[:3]:
        r["category"] = "Robes"
    assert [b["brand"] for b in pg.rank_brands(brows, now=datetime(2026, 10, 30, tzinfo=timezone.utc),
                                               page_brands_only=True)] == ["zara"]
    assert zp["product"] == "zara jacket" and zp["confirmed_sold_30d"] == 30, zp
    assert zp["competition_level"] == "Low" and zp["passes_alert_rule"] is True, zp
    # Smeg bottle watch keeps drinkware, drops appliances; drinkware-only brands are untouched
    assert not fr.is_off_product_type("Gourde isotherme Smeg 500 ml rose", "smeg tumbler")
    assert not fr.is_off_product_type("Borraccia SMEG rosa 500 ml", "smeg tumbler")
    assert fr.is_off_product_type("Bouilloire SMEG 1,7 L bleu", "smeg tumbler")
    assert fr.is_off_product_type("Smeg waterkoker 0,8L", "smeg tumbler")
    assert fr.is_off_product_type("Étagère porte bouteilles frigo Smeg", "smeg tumbler")
    assert not fr.is_off_product_type("Owala FreeSip pink", "owala bottle")
    assert fr.is_off_product_type("Smeg porsche", "smeg tumbler", "Bouilloires")       # category wins
    assert not fr.is_off_product_type("Smeg porsche", "smeg tumbler", "Gourdes")
    assert build_variant("Smeg gourde 500 ml crème", "Smeg", "", "") == "0.5l cream"
    # day-rounded publish time landing ~1h after last_seen -> 0h, never negative
    assert _lifespan_hours({"created_at_ts": str(int(t_pub.timestamp()) + 3600),
                            "last_seen": "2026-10-10 06:00:00", "lifespan_hours": "-0.9"}) == 0.0
    assert _lifespan_hours({"created_at_ts": "", "last_seen": "", "lifespan_hours": "-3"}) is None
    trk = {"a": {"id": "a", "status": "active", "created_at_ts": str(int(t_pub.timestamp())),
                 "first_seen": "2026-10-10 07:00:00", "last_seen": "2026-10-11 12:00:00",
                 "missed_runs": 0, "title": "", "brand": "", "size": "", "color": "", "variant": ""}}
    update_tracking(trk, [], datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc))   # miss 1
    update_tracking(trk, [], datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc))   # miss 2
    assert trk["a"]["status"] == "disappeared" and trk["a"]["lifespan_hours"] == 30.0, trk["a"]

    # CDP circuit breaker: a degraded Chrome (every connect times out) must stop the item loop
    # after a few attempts, not burn one timeout per item (live: 0/300 over ~16h, 2026-10-05).
    global sync_playwright
    calls = []

    class _Chromium:
        def connect_over_cdp(self, *a, **k):
            calls.append(1)
            raise TimeoutError("simulated degraded Chrome")

    class _P:
        chromium = _Chromium()

    class _SP:
        def __enter__(self):
            return _P()

        def __exit__(self, *a):
            return False

    real_sp, real_wait = sync_playwright, globals()["_wait_for_rate_limit"]
    sync_playwright, globals()["_wait_for_rate_limit"] = (lambda: _SP()), (lambda: None)
    try:
        assert all(_publish_ts_worker(str(i)) == (str(i), None) for i in range(40))
        assert len(calls) == 5 and _cdp_dead(), len(calls)
        assert _verify_sold_worker("x") == ("x", "disappeared") and len(calls) == 5
        _note_cdp(True)
        assert not _cdp_dead()
        # Rate-limit prober on a dead Chrome must give up and release the hold, not keep
        # every worker frozen through 40 refresh attempts.
        real_cd = globals()["_rate_limit_cooldown"]
        globals()["_rate_limit_cooldown"] = lambda: 0
        try:
            assert _begin_hold_become_prober()
            t0 = time.time()
            _probe_until_clear("x")
            assert not _rl_held and len(calls) == 10 and time.time() - t0 < 5, len(calls)
        finally:
            globals()["_rate_limit_cooldown"] = real_cd
        _note_cdp(True)
    finally:
        sync_playwright, globals()["_wait_for_rate_limit"] = real_sp, real_wait
    # Stall watchdog: a worker that never returns must not hold the phase (and the process)
    # hostage - after VINTED_STALL_SECS with no completion it stops, flags Chrome dead.
    global _STALLED
    import threading as _th
    from concurrent.futures import ThreadPoolExecutor as _TPE
    _gate = _th.Event()
    _ex = _TPE(1)
    _futs = {_ex.submit(_gate.wait): 1}
    _old = os.environ.get('VINTED_STALL_SECS')
    os.environ['VINTED_STALL_SECS'] = '1'
    try:
        t0 = time.time()
        assert list(_as_completed_or_stall(_futs)) == [] and _STALLED and _cdp_dead()
        assert time.time() - t0 < 5
    finally:
        _gate.set()
        _ex.shutdown(wait=True)
        _STALLED = False
        _note_cdp(True)
        if _old is None:
            os.environ.pop('VINTED_STALL_SECS', None)
        else:
            os.environ['VINTED_STALL_SECS'] = _old
    print("track_sales self-check OK: verified 30d volume + 72h velocity + competition unscored"
          " + CDP circuit breaker")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _demo()
    else:
        main()
        if _STALLED:  # a hung Playwright thread would block interpreter exit
            sys.stdout.flush()
            os._exit(0)
