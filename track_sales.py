"""
Phase 4 — Sales-Tracking PoC.

Follows the listings for ONE product over time and measures how fast they sell.

Mechanism: each run fetches the COMPLETE active catalog for a keyword and diffs it
against what we saw before. A listing that was active before and is now gone has
sold or been removed — that disappearance, and the time it took, is the turnover
signal. (Sold items leave Vinted's active catalog, which is exactly what makes this
work.)

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
    "status",          # active | disappeared
    "disappeared_at",
    "lifespan_hours",  # publish → disappearance (true time-to-sell, if created_at known)
    "hours_tracked",   # first_seen → disappearance (what we directly observed)
    "brand",           # structured attribute (from item page)
    "color",           # structured attribute (from item page)
    "variant",         # brand + capacity + colour signature for grouping
]

TS_FMT = "%Y-%m-%d %H:%M:%S"


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
    rate-limit clears (per your observation that refreshing eventually unblocks),
    then release all tabs. Only this tab refreshes — the rest stay idle so we
    don't keep hammering while blocked.
    """
    cd = _rate_limit_cooldown()
    print(f"\n   ⛔ Rate limited by Vinted — pausing all tabs. Waiting {int(cd)}s, "
          f"then refreshing until it clears...")
    time.sleep(cd)
    url = f"https://www.vinted.fr/items/{item_id}"
    for attempt in range(1, 41):  # cap the probing so it can't loop forever
        blocked = True
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                page = browser.contexts[0].new_page()
                try:
                    resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    blocked = (resp is not None and resp.status == 429) or \
                        _is_rate_limited_text(page.content())
                finally:
                    page.close()
        except Exception:
            pass
        if not blocked:
            print(f"   ✅ Rate limit cleared after {attempt} refresh(es) — resuming all tabs.")
            _release_hold()
            return
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


def build_variant(title: str, brand: str, color: str) -> str:
    """
    Build a product-variant signature = capacity + canonical colour,
    e.g. "1.18l pink". Brand is constant within one tracked keyword, so it's
    omitted to keep grouping consistent across all listings. Capacity and colour
    are read from the title (free, full coverage), with the structured page
    colour preferred when available. Returns "" if neither can be determined.
    """
    parts = []
    cap = next(
        (t for t in fr._tokenize((title or "").lower()) if fr.CAP_RE.match(t)), ""
    )
    if cap:
        parts.append(cap)
    col = normalize_color(color)
    if not col:
        # No structured colour — look for a colour word in the title.
        toks = fr._tokenize((title or "").lower())
        col = next((COLOR_BUCKETS[t] for t in toks if t in COLOR_BUCKETS), "")
    # A true variant needs BOTH capacity and colour, else partial/overlapping
    # groups fragment the same product. Listings missing one are left ungrouped
    # (the structured page colour fills colour in as enrichment progresses).
    if cap and col:
        return f"{cap} {col}"
    return ""


def compute_variant_opportunity(competition: int, est_sales_30d: float, median_days):
    """
    Score a variant 0–100 from concrete signals the client cares about.

    DEMAND-FIRST weighting (client feedback 2026-07-07): proven sales volume is
    the primary signal (max 50), liquidity second (max 30), competition last
    (max 20). Previously low competition + fast sales let tiny niches outrank
    high-volume proven sellers (a 5-sale/mo purple scored above the 48-sale/mo
    pink); volume now leads, matching how the client reads the market.
    """
    # Sales volume — estimated sales per 30 days (max 50, the lead signal)
    if est_sales_30d >= 30:
        vol = 50
    elif est_sales_30d >= 15:
        vol = 38
    elif est_sales_30d >= 8:
        vol = 26
    elif est_sales_30d >= 3:
        vol = 14
    elif est_sales_30d >= 1:
        vol = 7
    else:
        vol = 0

    # Liquidity — faster sale = better (max 30)
    if median_days is None:
        liq = 0
    elif median_days <= 1:
        liq = 30
    elif median_days <= 3:
        liq = 24
    elif median_days <= 7:
        liq = 16
    elif median_days <= 14:
        liq = 8
    else:
        liq = 4

    # Competition — fewer active listings = more room (max 20); 0 = no market
    if competition == 0:
        comp = 4
    elif competition <= 10:
        comp = 20
    elif competition <= 30:
        comp = 15
    elif competition <= 60:
        comp = 10
    elif competition <= 120:
        comp = 5
    else:
        comp = 2

    score = min(100, liq + vol + comp)
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


def save_variant_snapshot(variants: list) -> str | None:
    """Persist today's per-variant sales estimate so the next run can show a trend."""
    if not variants:
        return None
    os.makedirs(TRACK_DIR, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(TRACK_DIR, f"variants_{date}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "est_sales_30d"])
        for v in variants:
            w.writerow([v["variant"], v["est_sales_30d"]])
    return path


def load_prev_variant_snapshot(exclude_date: str) -> dict:
    """Load the most recent prior per-variant snapshot → {variant: est_sales_30d}."""
    if not os.path.isdir(TRACK_DIR):
        return {}
    files = sorted(
        fn
        for fn in os.listdir(TRACK_DIR)
        if fn.startswith("variants_") and fn.endswith(".csv")
        and fn != f"variants_{exclude_date}.csv"
    )
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


def variant_analysis(
    tracking: dict, now: datetime | None = None, visual_slug: str | None = None
):
    """
    Aggregate per-variant turnover into the concrete metrics the SaaS shows:
    estimated sales/30d, sales velocity (days), competition level, market trend,
    average price, confidence, and last-updated. Returns (sorted list, window_days).

    visual_slug (Phase 5): keyword slug whose visual-variant index should fill in
    groupings for listings the text tokenizer can't parse (no capacity+colour in
    the title). Text variants stay authoritative when they exist; the photo-based
    cluster only catches what would otherwise be dropped from the analysis.
    """
    now = now or datetime.now(timezone.utc)
    updated = now.strftime("%Y-%m-%d %H:%M UTC")
    prev_snap = load_prev_variant_snapshot(now.strftime("%Y-%m-%d"))
    vis_map, vis_labels = _load_visual_variants(visual_slug)

    firsts = []
    for r in tracking.values():
        try:
            firsts.append(_parse(r["first_seen"]))
        except Exception:
            pass
    window_days = (
        max(1.0, (now - min(firsts)).total_seconds() / 86400) if firsts else 1.0
    )

    groups = {}
    for r in tracking.values():
        v = build_variant(r.get("title", ""), r.get("brand", ""), r.get("color", ""))
        if not v:
            # Phase 5 fallback: no text variant — group by what the PHOTO shows.
            cid = vis_map.get(str(r.get("id", "")))
            if not cid:
                continue
            label = (vis_labels.get(cid) or "").strip()
            v = f"📷 {cid}" + (f" · {label[:40]}" if label else "")
        g = groups.setdefault(
            v, {"active": 0, "gone": 0, "lifes": [], "prices": []}
        )
        if r["status"] == "active":
            g["active"] += 1
        elif r["status"] == "disappeared":
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
        opp = compute_variant_opportunity(g["active"], est_30d, med_days)
        out.append(
            {
                "variant": v,
                "est_sales_30d": est_30d,
                "demand_level": demand_label(est_30d),
                "median_days_to_sell": med_days,
                "competition": g["active"],
                "competition_level": competition_label(g["active"]),
                "trend": variant_trend(est_30d, prev_snap.get(v)),
                "avg_price": avg_price,
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


def format_variant_card(variant_name: str, v: dict) -> str:
    """Client-requested summary-card layout for one variant — the concrete,
    numbers-first format he asked for in place of the raw table row."""
    md = v["median_days_to_sell"]
    vel = f"{md} days" if md is not None else "not yet measured"
    price = f"€{v['avg_price']}" if v["avg_price"] is not None else "—"
    lines = [
        f"{variant_name.title()}",
        "",
        f"🔥 Estimated Sales: {v['est_sales_30d']}/month",
        f"⚡ Average Time to Sell: {vel}",
        f"🏷️ Average Selling Price: {price}",
        f"👥 Active Listings: {v['competition']}",
        f"📈 Trend: {v['trend']}",
        f"🏆 Competition: {v['competition_level']}",
        f"🎯 Opportunity: {opportunity_label(v['score'])}",
    ]
    return "\n".join(lines)


def save_variant_report(variants: list, output_file: str = "variant_report.csv") -> str | None:
    """Export the per-variant opportunity table — the concrete Phase 4 deliverable."""
    if not variants:
        return None
    fields = [
        "variant",
        "est_sales_30d",
        "demand_level",
        "median_days_to_sell",
        "competition",
        "competition_level",
        "trend",
        "avg_price",
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
    info = {"created_at_ts": None, "brand": "", "color": ""}
    try:
        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if resp is not None and resp.status == 429:
            raise RateLimited()

        html = page.content()
        if _is_rate_limited_text(html):
            raise RateLimited()

        # Structured attributes (brand, colour) from the server-rendered JSON-LD.
        try:
            ld = fr.extract_jsonld(html)
            info["brand"] = ld.get("brand") or ""
            info["color"] = ld.get("color") or ""
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
                row["variant"] = build_variant(
                    row.get("title", ""), row.get("brand", ""), row.get("color", "")
                )
            if done % 25 == 0:
                print(f"   ...{done}/{len(todo)} ({captured} captured)")
                save_tracking(path, tracking)  # checkpoint from the main thread

    save_tracking(path, tracking)
    print(f"   → publish time captured for {captured}/{len(todo)} listings")
    return captured


def _median(vals):
    vals = [v for v in vals if v not in ("", None)]
    return round(statistics.median(float(v) for v in vals), 1) if vals else None


def report(keyword: str, tracking: dict, newly: int, disappeared: int, first_run: bool):
    total = len(tracking)
    active = sum(1 for r in tracking.values() if r["status"] == "active")
    gone = [r for r in tracking.values() if r["status"] == "disappeared"]

    print("\n" + "=" * 60)
    print(f"📦 SALES TRACKING — {keyword}")
    print("=" * 60)
    print(f"  Tracked (all time):   {total}")
    print(f"  Currently active:     {active}")
    print(f"  Sold / removed:       {len(gone)}")
    print(f"  New this run:         {newly}")
    print(f"  Disappeared this run: {disappeared}")

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
    variants, window_days = variant_analysis(tracking, visual_slug=_slug(keyword))
    if variants:
        print(f"\n  ════ TOP OPPORTUNITY ════")
        print("  " + format_variant_card(f"{keyword} {variants[0]['variant']}", variants[0])
              .replace("\n", "\n  "))
        print(f"\n  ════ PRODUCT VARIANTS — market data ════")
        print(f"  {'variant':<17}{'sales/30d':>10}{'demand':>8}{'velocity':>9}"
              f"{'competition':>13}{'trend':>11}{'price':>7}{'conf':>7}")
        for v in variants[:12]:
            md = v["median_days_to_sell"]
            vel = f"{md}d" if md is not None else "—"
            price = f"{v['avg_price']}€" if v["avg_price"] is not None else "—"
            comp = f"{v['competition_level']}({v['competition']})"
            print(
                f"  {v['variant'][:17]:<17}{('~' + str(v['est_sales_30d'])):>10}"
                f"{v['demand_level']:>8}{vel:>9}"
                f"{comp:>13}{v['trend']:>11}{price:>7}{v['confidence']:>7}"
            )
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
        print(f"     • Confidence reflects sample size + tracking length")
        print(f"     • Updated: {variants[0]['last_updated']}")

    if first_run:
        print("  ───────────────────────────────────")
        print("  🟡 Baseline captured. Run again in ~12-24h — time-to-sell")
        print("     numbers appear once listings start disappearing.")
    print("=" * 60)


def main():
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

        # Cross-border coverage (opt-in): VINTED_DOMAINS="fr,de,it,es,be,nl,pt,at"
        # merges catalogs across the client's shipping-zone domains by listing id.
        # Default is FR-only (unchanged behavior) — FR alone misses ~45% of the
        # listings actually visible to a seller shipping cross-border (measured
        # 2026-07-07 on stanley quencher: 287 on .fr vs 522 union across 8 domains).
        domains = tuple(
            d.strip() for d in os.environ.get("VINTED_DOMAINS", "fr").split(",") if d.strip()
        )
        print(f"\n🔍 Fetching complete active catalog for: {keyword}"
              + (f"  (domains: {', '.join(domains)})" if len(domains) > 1 else ""))
        # Fetch ALL pages and disable the age-based early stop so the active set is
        # complete — otherwise items would look 'disappeared' just for being old.
        raw = fr.fetch_catalog_multi_domain(
            keyword, cookies, token, domains=domains,
            max_pages=None, stop_when_old_ratio=2.0,
        )
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

    # Capture publish time across parallel tabs for active listings missing it.
    if os.environ.get("VINTED_TRACK_ENRICH", "1") != "0":
        enrich_publish_times(tracking, path)

    save_tracking(path, tracking)
    report(keyword, tracking, newly, disappeared, first_run)
    print(f"\n💾 Tracking state saved: {path}")

    variants, _ = variant_analysis(tracking, visual_slug=_slug(keyword))
    vpath = save_variant_report(variants, f"variant_report_{_slug(keyword)}.csv")
    if vpath:
        print(f"💾 Variant report saved: {vpath} ({len(variants)} variants)")
    save_variant_snapshot(variants)  # enables the trend column on the next run


if __name__ == "__main__":
    main()
