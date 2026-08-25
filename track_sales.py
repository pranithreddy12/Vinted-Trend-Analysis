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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "variant",         # brand + capacity + colour signature for grouping
    "offers",          # buyer offers on this listing (early-demand signal, from item page)
    "offers_seen_at",  # when the offers count was captured (offers are a moving snapshot)
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
        if iid in tracking:
            row = tracking[iid]
            row["last_seen"] = now_str
            # Backfill brand/variant from the catalog (free — no page load needed).
            if brand_title and not row.get("brand"):
                row["brand"] = brand_title
            if not row.get("variant"):
                row["variant"] = build_variant(
                    row.get("title", ""), row.get("brand", ""), row.get("color", "")
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
                "variant": build_variant(title, brand_title, ""),
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
                    row["lifespan_hours"] = round(
                        (now - pub).total_seconds() / 3600, 1
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
        blocked = True
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
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


def normalize_color(color: str) -> str:
    if not color:
        return ""
    first = color.split(",")[0].strip().lower()
    return COLOR_BUCKETS.get(first, first)


# Known bottle sizes → canonical label. A listing's capacity is converted to
# millilitres and snapped to the nearest of these within tolerance, so the SAME
# physical size written different ways (40oz, 1.18L, 1.2L, 1.19L) collapses to one
# variant instead of fragmenting the demand across four. (40oz = 1183ml, so those
# are all the same product.) Sizes outside tolerance keep a rounded-litre label.
_CANONICAL_SIZES_ML = [
    (414, "14oz"), (473, "16oz"), (591, "20oz"), (709, "24oz"),
    (887, "30oz"), (1183, "40oz"), (1419, "48oz"), (1892, "64oz"),
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


# oz→metric companion for the display label, so the client sees both units at once
# (he referred to sizes in both, e.g. "40oz" and "1.18L").
_CAP_METRIC = {
    "14oz": "0.41L", "16oz": "0.47L", "20oz": "0.59L", "24oz": "0.71L",
    "30oz": "0.89L", "40oz": "1.18L", "48oz": "1.42L", "64oz": "1.9L",
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


def build_variant(title: str, brand: str, color: str) -> str:
    """
    Build the BASE variant key = canonical capacity + colour, e.g. "40oz pink".
    Capacity is unit-normalised (40oz ≡ 1.18L ≡ 1.2L → "40oz") so the same size
    written different ways doesn't fragment into separate variants. Colour prefers
    the structured page attribute, falling back to a colour word in the title.
    Returns "" if capacity+colour can't both be determined (the pair is the minimum
    for a real variant).

    The product LINE (Quencher / Flip Straw / …) is layered on in variant_analysis,
    not here, because assigning it correctly needs the global picture — a bare
    "Stanley cup 40oz" title names no line, so it's imputed to the dominant line at
    that size+colour rather than fragmenting off on its own.
    """
    toks = fr._tokenize((title or "").lower())
    cap = ""
    for t in toks:
        cap = canonical_capacity(t)
        if cap:
            break
    col = normalize_color(color)
    if not col:
        # No structured colour — look for a colour word in the title.
        col = next((COLOR_BUCKETS[t] for t in toks if t in COLOR_BUCKETS), "")
    if not (cap and col):
        return ""
    return f"{cap} {col}"


def compute_variant_opportunity(
    competition: int,
    est_sales_30d: float,
    median_days,
    offers_total: int = 0,
    offers_measured: bool = False,
):
    """
    Score a variant 0–100 from concrete signals the client cares about.

    Weighting (client-confirmed priority order, 2026-07-08):
      1. Proven monthly sales volume  (max 45, the lead signal)
      2. Sales velocity / liquidity   (max 25)
      3. Buyer demand signals (offers)(max 18) — early demand, leads sales in time
      4. Competition level            (max 12, tie-breaker only)
    Volume still clearly leads so a high-volume proven seller never loses to a tiny
    fast niche; offers were added as the #3 factor so a product that's heating up
    (buyers making offers) surfaces before its sales history has caught up.

    offers_measured distinguishes "measured 0 offers" (real weak early demand → the
    offers component is a genuine 0) from "offers not captured yet for this variant"
    (unknown → we score the other three factors renormalised to 100 instead of
    penalising it with a phantom 0). Offer coverage builds up as item pages get
    enriched over successive runs, so this avoids a first-run dip in every score.
    """
    # Sales volume — estimated sales per 30 days (max 45, the lead signal)
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

    # Liquidity — faster sale = better (max 25)
    if median_days is None:
        liq = 0
    elif median_days <= 1:
        liq = 25
    elif median_days <= 3:
        liq = 20
    elif median_days <= 7:
        liq = 13
    elif median_days <= 14:
        liq = 7
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

    # Competition — fewer active listings = more room (max 12); 0 = no market
    if competition == 0:
        comp = 2
    elif competition <= 10:
        comp = 12
    elif competition <= 30:
        comp = 9
    elif competition <= 60:
        comp = 6
    elif competition <= 120:
        comp = 3
    else:
        comp = 1

    if offers_measured:
        score = min(100, vol + liq + off + comp)
    else:
        # Offers not yet captured for this variant — score the three proven factors
        # (max 82) renormalised to 100 so an un-measured signal is neutral, not a
        # penalty. Once offers arrive, the formula above applies.
        score = min(100, round((vol + liq + comp) * 100 / 82))
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
        return "Increasing" if cur_est > 0 else "Stable"
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
        base = build_variant(r.get("title", ""), r.get("brand", ""), r.get("color", ""))
        if not base:
            continue
        m = detect_model(r.get("title", ""))
        if m:
            model_votes.setdefault(base, collections.Counter())[m] += 1
    dominant_model = {
        base: votes.most_common(1)[0][0] for base, votes in model_votes.items()
    }

    groups = {}
    for r in tracking.values():
        base = build_variant(r.get("title", ""), r.get("brand", ""), r.get("color", ""))
        if base:
            model = detect_model(r.get("title", "")) or dominant_model.get(base, "")
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
            {"active": 0, "gone": 0, "lifes": [], "prices": [],
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
            lh = r.get("lifespan_hours")
            if lh not in ("", None):
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
                g["prices"].append(float(r["price"]))
        except ValueError:
            pass

    out = []
    for v, g in groups.items():
        est_30d = round(g["gone"] / window_days * 30, 1)
        med_days = (
            round(statistics.median(g["lifes"]) / 24, 1) if g["lifes"] else None
        )
        avg_price = round(statistics.mean(g["prices"]), 1) if g["prices"] else None
        opp = compute_variant_opportunity(
            g["active"], est_30d, med_days, g["offers_total"],
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
    price = f"€{v['avg_price']}" if v["avg_price"] is not None else "—"
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
        f"🏷️ Average Selling Price: {price}",
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
        html = page.content()

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
                    timeout=2500
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
        _wait_for_rate_limit()  # hold if a global cooldown is active
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
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


def enrich_publish_times(tracking: dict, path: str, workers: int = 5) -> int:
    """
    Fill in publish time for active listings missing it, loading item pages across
    several tabs in parallel (default 5; override with VINTED_TRACK_WORKERS).
    Progress is checkpointed so a long backfill is interruptible/resumable.
    Cap per run with VINTED_MAX_ENRICH. Returns how many were captured this run.
    """
    todo = [
        iid
        for iid, row in tracking.items()
        if row["status"] == "active" and not row.get("created_at_ts")
    ]
    cap = os.environ.get("VINTED_MAX_ENRICH")
    if cap:
        todo = todo[: int(cap)]
    if not todo:
        return 0
    workers = int(os.environ.get("VINTED_TRACK_WORKERS", workers))
    print(f"\n⏳ Capturing publish time for {len(todo)} listings via {workers} parallel tabs...")
    print("   (progress is saved as it goes — safe to stop and resume next run)")

    captured = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {
            executor.submit(_publish_ts_worker, iid): iid for iid in todo
        }
        for future in as_completed(future_to_id):
            iid = future_to_id[future]
            try:
                _id, info = future.result()
            except Exception:
                info = None
            done += 1
            if info:
                row = tracking[iid]
                if info.get("created_at_ts"):
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
                    row.get("title", ""), row.get("brand", ""), row.get("color", "")
                )
            if done % 25 == 0:
                print(f"   ...{done}/{len(todo)} ({captured} captured)")
                save_tracking(path, tracking)  # checkpoint from the main thread

    save_tracking(path, tracking)
    print(f"   → publish time captured for {captured}/{len(todo)} listings")
    return captured


# ─────────────────────────────────────────
# SOLD VERIFICATION (VINTED_VERIFY_SOLD=1)
# ─────────────────────────────────────────
# A disappeared listing is NOT a sale. When a listing leaves the active catalog we open its
# item page and read Vinted's real status: only pages Vinted actually marks "Sold/Vendu" become
# sales; 404/removed/unavailable are excluded; a page that's live again was a glitch (revert to
# active). Anything we couldn't verify stays "disappeared" and is NOT counted as a sale.

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
    if sold:
        return "sold"                            # Vinted shows Sold/Vendu — a CONFIRMED sale
    if status == "unavailable":
        return "removed"
    if status == "active":
        return "active"                          # still live — relist/glitch, revert
    return "disappeared"


def _verify_sold_worker(item_id: str):
    """Thread worker mirroring _publish_ts_worker: own tab over CDP, verify one listing's
    real sold status, respect the shared rate-limit back-off."""
    for _attempt in range(3):
        _wait_for_rate_limit()
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0]
                page = context.new_page()
                try:
                    return item_id, _verify_sold_from_page(page, item_id)
                finally:
                    page.close()
                    time.sleep(random.uniform(0.3, 0.9))
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
    Only runs when VINTED_VERIFY_SOLD=1. Returns a small tally for the report."""
    if not ids:
        return {}
    workers = int(os.environ.get("VINTED_TRACK_WORKERS", workers))
    print(f"\n🔎 Verifying sold status of {len(ids)} disappeared listings via {workers} tabs "
          f"(a disappearance is not a sale until Vinted confirms it)...")
    tally = collections.Counter()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_verify_sold_worker, iid): iid for iid in ids}
        for future in as_completed(futures):
            iid = futures[future]
            try:
                _id, outcome = future.result()
            except Exception:
                outcome = "disappeared"
            row = tracking.get(iid)
            if row is not None:
                if outcome == "sold":
                    row["status"], row["sale_confirmed"] = "sold", True
                elif outcome == "removed":
                    row["status"], row["sale_confirmed"] = "removed", False
                elif outcome == "active":
                    row["status"] = "active"
                    row["disappeared_at"] = row["lifespan_hours"] = row["hours_tracked"] = ""
                    row["sale_confirmed"] = ""
                else:
                    row["sale_confirmed"] = False   # unverified — stays "disappeared", not a sale
            tally[outcome] += 1
            done += 1
            if done % 25 == 0:
                print(f"   ...{done}/{len(ids)} verified")
                save_tracking(path, tracking)
    save_tracking(path, tracking)
    print(f"   → confirmed sold: {tally['sold']} · removed: {tally['removed']} · "
          f"relisted/active: {tally['active']} · unverified: {tally['disappeared']}")
    return dict(tally)


def _median(vals):
    vals = [v for v in vals if v not in ("", None)]
    return round(statistics.median(float(v) for v in vals), 1) if vals else None


def report(keyword: str, tracking: dict, newly: int, disappeared: int, first_run: bool):
    total = len(tracking)
    st = collections.Counter(r["status"] for r in tracking.values())
    active = st["active"]

    print("\n" + "=" * 60)
    print(f"📦 SALES TRACKING — {keyword}")
    print("=" * 60)
    print(f"  Tracked (all time):   {total}")
    print(f"  Currently active:     {active}")
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
        life = sorted(
            float(r["lifespan_hours"]) for r in gone if r.get("lifespan_hours") != ""
        )
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
            price = f"{v['avg_price']}€" if v["avg_price"] is not None else "—"
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
        page = fr.get_or_create_page(context, "https://www.vinted.fr")
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
        print(f"   → {len(raw)} active listings right now")
    # Auth session closed; the enrichment workers open their own CDP connections.

    now = datetime.now(timezone.utc)
    tracking = load_tracking(path)
    first_run = len(tracking) == 0

    # SAFETY GUARD — a failed/partial catalog fetch (401 auth expiry, rate limit,
    # network drop) returns few/zero listings. Without this guard, update_tracking
    # would mark EVERY tracked listing as "disappeared/sold" and corrupt the data.
    # If the active count collapses versus what we already know is live, skip this
    # run entirely and preserve the tracking file.
    prior_active = sum(1 for r in tracking.values() if r.get("status") == "active")
    current_ids = {str(i.get("id")) for i in raw if i.get("id")}
    if not first_run and prior_active >= 20 and len(current_ids) < prior_active * 0.5:
        print(
            f"⚠️  Catalog returned only {len(current_ids)} listings vs {prior_active} "
            f"active previously — almost certainly a failed/partial fetch (expired "
            f"login or rate limit). Skipping this run to protect the tracking data. "
            f"Re-login to Vinted and the next run will resume normally."
        )
        sys.exit(3)

    newly, disappeared = update_tracking(tracking, raw, now)

    # Data reliability (opt-in VINTED_VERIFY_SOLD=1): a disappeared listing is NOT a sale.
    # Verify each newly-disappeared listing against Vinted's real item-page status — only the
    # ones actually marked Sold become sales; removed/deleted are excluded, relisted revert.
    if os.environ.get("VINTED_VERIFY_SOLD") == "1":
        now_str = _fmt(now)
        gone_ids = [iid for iid, r in tracking.items()
                    if r["status"] == "disappeared" and r.get("disappeared_at") == now_str]
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

    # Phase 5 Stage A (opt-in): vision-AI product identification → full, specific
    # titles. Uses the deterministic stub by default (free, no key); set
    # VINTED_VISION_PROVIDER=anthropic once the client's API key is in the env to use
    # the real vision model. Identifies new listings only, best-first (by likes),
    # capped per run; results are cached so each product is paid for once.
    if os.environ.get("VINTED_VISION") == "1":
        try:
            import vision_identify

            # Prefer each listing's own declared colour (structured attribute captured during
            # enrichment) over the vision model's photo-guess in the composed title.
            colours = {str(iid): row.get("color", "")
                       for iid, row in tracking.items() if row.get("color")}
            idents = vision_identify.identify_listings(raw, _slug(keyword), colours=colours)
            # Stage B (opt-in): look up a reference product + price band for GENERIC no-brand
            # items. Off unless VINTED_REFERENCE=1; branded items are untouched.
            if os.environ.get("VINTED_REFERENCE") == "1":
                import reference_lookup
                idents = reference_lookup.enrich_generics(idents, _slug(keyword))
            vpath = vision_identify.save_identities(idents, _slug(keyword))
            named = sum(1 for v in idents.values() if v.get("generated_title"))
            print(f"🔎 product identification: {named}/{len(idents)} titled → {vpath} "
                  f"(provider: {os.environ.get('VINTED_VISION_PROVIDER', 'stub')})")
        except Exception as e:
            print(f"⚠️  product identification skipped: {type(e).__name__}: {e}")

    # Capture publish time across parallel tabs for active listings missing it.
    if os.environ.get("VINTED_TRACK_ENRICH", "1") != "0":
        enrich_publish_times(tracking, path)

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


if __name__ == "__main__":
    main()
