import re
import os
import sys
import time
import random
import logging
import csv
import json
import statistics
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    from zoneinfo import ZoneInfo
except ImportError:
    pass  # Fallback handled in code if using older python, though 3.9+ is expected.

sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("vinted_scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

OFFER_PATTERNS = [
    r"(\d+)[^\d]{0,40}acheteurs?[^\d]{0,40}offre",
    r"(\d+)[^\d]{0,40}buyers?[^\d]{0,40}offer",
    r"(\d+)\s*offres?\s+soumises?",
    r"(\d+)\s*offers?\s+submitted",
]

# Thread safety lock for CSV writing
CSV_LOCK = threading.Lock()

# ── Rate-limit back-off (shared across scraping threads) ──
# When Vinted throttles us, all threads pause briefly instead of skipping items.
RATE_LIMIT_MARKERS = (
    "you are rate limited",
    "rate limited",
    "too many requests",
    "trop de requêtes",
)
_RL_LOCK = threading.Lock()
_RL_UNTIL = 0.0  # epoch seconds; threads hold until time.time() >= this


class RateLimited(Exception):
    pass


def _rl_wait():
    """Block this thread while a rate-limit cooldown is in effect."""
    while True:
        with _RL_LOCK:
            remaining = _RL_UNTIL - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5))


def _rl_trigger(seconds: float = 45.0):
    """Start (or extend) a shared cooldown that pauses every thread."""
    global _RL_UNTIL
    with _RL_LOCK:
        first = time.time() >= _RL_UNTIL
        _RL_UNTIL = max(_RL_UNTIL, time.time() + seconds)
    if first:
        print(f"  ⛔ Rate limited by Vinted — pausing all tabs ~{int(seconds)}s...")

# ─────────────────────────────────────────
# HUMAN-BEHAVIOUR HELPERS
# ─────────────────────────────────────────


def human_delay(base_min: float = 3.0, base_max: float = 8.0):
    """
    Sleep for a random duration.
    10 % of the time adds an extra 'distracted-human' pause of 8–20 s.
    """
    delay = random.uniform(base_min, base_max)
    if random.random() < 0.10:
        extra = random.uniform(8, 20)
        print(f"  💤 Taking a short break ({extra:.0f}s)...")
        delay += extra
    time.sleep(delay)


def big_break(every: int = 50, count: int = 0):
    """
    Every `every` items take a longer 30–90 s break so the session
    doesn't look like a bot hammering pages at a constant rate.
    """
    if count > 0 and count % every == 0:
        pause = random.uniform(30, 90)
        print(f"\n  ☕ Big break after {count} items ({pause:.0f}s)...\n")
        time.sleep(pause)


def human_scroll(page):
    """Scroll down the page gradually, then back to the top."""
    try:
        height = page.evaluate("document.body.scrollHeight") or 3000
        step = random.randint(250, 450)
        pos = 0
        while pos < height:
            pos = min(pos + step, height)
            page.evaluate(f"window.scrollTo(0, {pos})")
            time.sleep(random.uniform(0.08, 0.25))
        time.sleep(random.uniform(0.5, 1.5))
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass


def random_mouse_move(page):
    """Move the mouse to a random position on the viewport."""
    try:
        vw = page.viewport_size or {"width": 1280, "height": 800}
        x = random.randint(100, vw["width"] - 100)
        y = random.randint(100, vw["height"] - 100)
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.1, 0.4))
    except Exception:
        pass


# ─────────────────────────────────────────
# CONNECT TO CHROME
# ─────────────────────────────────────────


def connect(p):
    """Connect to the already-running Chrome via CDP."""
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    return browser, context


def get_or_create_page(context, url: str | None = None):
    """
    Reuse an existing Vinted tab if possible.
    If a url is given and no vinted tab exists, open one.
    """
    page = next(
        (pg for pg in context.pages if "vinted" in pg.url.lower()),
        None,
    )
    if not page:
        page = context.new_page()
        if url:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(5)
    return page


# ─────────────────────────────────────────
# CATALOG FETCH — VIA REQUESTS
# ─────────────────────────────────────────


def get_cookies_and_token(context, page):
    """Extract cookies and access token from the authenticated page."""
    raw_cookies = context.cookies()
    cookies_dict = {c["name"]: c["value"] for c in raw_cookies}
    access_token = cookies_dict.get("access_token_web", "")

    if not access_token:
        captured_token = [None]

        def on_request(request):
            if "/api/v2/" in request.url:
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    captured_token[0] = auth.replace("Bearer ", "")

        page.on("request", on_request)
        # Trigger an API call by scrolling
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)
        page.remove_listener("request", on_request)

        if captured_token[0]:
            access_token = captured_token[0]

    return cookies_dict, access_token


def fetch_catalog_via_requests(
    keyword: str,
    cookies: dict,
    access_token: str,
    max_pages: int | None = None,
    catalog_id: int | None = None,
    stop_when_old_ratio: float = 0.5,
) -> list:
    """
    Fetch catalog items for a keyword via the Vinted API.
    max_pages=None means unlimited — keeps going until Vinted returns 0 items.
    catalog_id: optional Vinted category ID to narrow results.
    stop_when_old_ratio: if this fraction of items on a page are >72h old, stop early.
    """
    all_items = []
    url = "https://www.vinted.fr/api/v2/catalog/items"
    pg = 1

    while True:
        if max_pages is not None and pg > max_pages:
            break

        params = {
            "page": pg,
            "per_page": 96,
            "search_text": keyword,
            "order": "newest_first",
        }

        if catalog_id:
            params["catalog[]"] = catalog_id

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.vinted.fr/catalog",
            "Origin": "https://www.vinted.fr",
            "X-Requested-With": "XMLHttpRequest",
        }

        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        response = requests.get(url, params=params, headers=headers, cookies=cookies)

        if response.status_code != 200:
            print(
                f"  ❌ Error {response.status_code} for keyword: {keyword} on page {pg}"
            )
            break

        data = response.json()
        items = data.get("items", [])

        if not items:
            print(f"  📄 Page {pg}: no more items — stopping")
            break

        print(f"  📄 Page {pg}: {len(items)} items fetched via requests")
        all_items.extend(items)
        pg += 1

        # Dynamic stopping: if too many items on this page are >72h old, assume we've
        # passed the useful recency window and stop early.
        try:
            timestamps = [
                i.get("created_at_ts") for i in items if i.get("created_at_ts")
            ]
            if timestamps:
                old_count = sum(1 for ts in timestamps if compute_age_hours(ts) > 72)
                if (old_count / len(timestamps)) >= stop_when_old_ratio:
                    print(
                        f"  ⏹️  {old_count}/{len(timestamps)} items >72h old — stopping early"
                    )
                    break
        except Exception:
            pass

        # Delay between catalog pages
        human_delay(4, 9)

    return all_items


# ─────────────────────────────────────────
# EXTRACT ITEM METADATA
# ─────────────────────────────────────────


EXCLUDE_TERMS = [
    "pin",
    "pins",
    "jibbitz",
    "jibbit",
    "labubu",
    "stitch",
    "star wars",
    "avengers",
    "pikachu",
    "peppa pig",
    "winnie the pooh",
    "kawaï",
    "kawai",
    "marvel",
    "support",
    "cadre",
    "frame",
    "supporto",
]


def extract_data(items: list, keyword: str) -> list:
    out = []
    for item in items:
        try:
            title = item.get("title", "")
            title_lower = title.lower()

            # Skip irrelevant accessories / add-ons
            if any(term in title_lower for term in EXCLUDE_TERMS):
                continue

            out.append(
                {
                    "keyword": keyword,
                    "title": title,
                    "price": float(item["price"]["amount"]) if item.get("price") else 0,
                    "likes": item.get("favourite_count", 0),
                    "views": item.get("view_count", 0),
                    "brand": item.get("brand_title", ""),
                    "country": item.get("user", {}).get("country_code", ""),
                    "url": f"https://www.vinted.fr/items/{item.get('id')}",
                    "status": "active",
                    "sold_confirmed": False,
                    "offers": 0,
                    "published_at": "",
                    "hours": 24.0,
                    "age_band": "",
                    "age_display": "",
                    "score": 0,
                    "verdict": "",
                }
            )
        except Exception:
            continue
    return out


# ─────────────────────────────────────────
# SCRAPE ONE ITEM PAGE (with retry)
# ─────────────────────────────────────────


LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_jsonld(html: str) -> dict:
    """
    Parse the server-rendered schema.org JSON-LD block from an item page.
    This is robust even when client-hydrated data-testid selectors are absent.
    Returns any of: availability, price, brand, color.
    """
    out = {}
    for m in LD_JSON_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for d in data if isinstance(data, list) else [data]:
            if not isinstance(d, dict):
                continue
            offers = d.get("offers")
            if isinstance(offers, dict):
                out["availability"] = offers.get("availability", "")
                out["price"] = offers.get("price")
            brand = d.get("brand")
            if brand:
                out["brand"] = brand.get("name") if isinstance(brand, dict) else brand
            if d.get("color"):
                out["color"] = d["color"]
    return out


def intercept_item_api(page):
    """
    Sets up an interceptor to capture the item API response.
    Returns a dictionary to hold the captured data.
    """
    api_data = {}

    def handle_response(response):
        if "/api/v2/items/" in response.url:
            try:
                data = response.json()
                if "item" in data:
                    item_data = data["item"]
                    api_data["created_at_ts"] = item_data.get("created_at_ts")
                    api_data["favourite_count"] = item_data.get("favourite_count")
                    api_data["view_count"] = item_data.get("view_count")
                    api_data["is_closed"] = item_data.get("is_closed")
                    api_data["can_buy"] = item_data.get("can_buy")
                    api_data["status"] = item_data.get("status")
            except Exception:
                pass

    page.on("response", handle_response)
    return api_data, handle_response


def scrape_item(page, item: dict, attempt: int = 1, max_attempts: int = 3):
    timeout = 25000 + (attempt - 1) * 15000  # 25 → 40 → 55 s

    _rl_wait()  # hold if a shared rate-limit cooldown is active
    api_data, handler = intercept_item_api(page)

    try:
        resp = page.goto(item["url"], timeout=timeout, wait_until="domcontentloaded")
        if resp is not None and resp.status == 429:
            raise RateLimited()

        time.sleep(random.uniform(2.5, 4.5))
        random_mouse_move(page)
        human_scroll(page)

        # Offers count - DOM text matching
        offers = 0
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
            if any(m in body_text.lower() for m in RATE_LIMIT_MARKERS):
                raise RateLimited()
            for pattern in OFFER_PATTERNS:
                match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
                if match:
                    offers = int(match.group(1))
                    break
        except RateLimited:
            raise
        except Exception:
            pass

        # Use API data if available, else fallback
        france_tz = ZoneInfo("Europe/Paris")

        if api_data.get("created_at_ts"):
            hours = compute_age_hours(api_data["created_at_ts"])
            published_date = datetime.fromtimestamp(
                api_data["created_at_ts"], tz=timezone.utc
            ).astimezone(france_tz)
            published_at = published_date.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            hours = 24.0
            published_at = ""
            try:
                txt = page.locator("[itemprop='upload_date'] span").inner_text(
                    timeout=3000
                )
                hours = parse_time(txt)
                published_date = datetime.now(france_tz) - timedelta(hours=hours)
                published_at = published_date.strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                pass

        if api_data.get("favourite_count") is not None:
            likes = api_data["favourite_count"]
        else:
            likes = item["likes"]

        if api_data.get("view_count") is not None:
            views = api_data["view_count"]
        else:
            views = item.get("views", 0)

        # -----------------------
        # STATUS DETECTION (UPGRADED)
        # -----------------------
        sold_confirmed = False
        status = "active"

        # API layer first
        if api_data:
            if api_data.get("is_closed") is True:
                sold_confirmed = True
                status = "sold"

            elif (
                api_data.get("can_buy") is False and api_data.get("is_closed") is False
            ):
                status = "unavailable"

        # DOM structural validation overrides weak assumptions
        dom_sold, dom_status = detect_sold_status(page)

        if dom_sold:
            sold_confirmed = True
            status = "sold"
        elif status == "active" and dom_status != "active":
            status = dom_status

        # JSON-LD fallback — only when the API response wasn't captured (the
        # weakest path). Server-rendered availability is robust against missing
        # hydrated selectors. price backfills a missing catalog price.
        price = item.get("price", 0)
        if not api_data:
            try:
                ld = extract_jsonld(page.content())
                avail = (ld.get("availability") or "").lower()
                if "soldout" in avail or "outofstock" in avail:
                    sold_confirmed = True
                    status = "sold"
                elif "instock" in avail and not dom_sold:
                    status = "active"
                if not price and ld.get("price"):
                    try:
                        price = float(ld["price"])
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass

        return {
            "offers": offers,
            "hours": hours,
            "published_at": published_at,
            "likes": likes,
            "views": views,
            "price": price,
            "status": status,
            "sold_confirmed": sold_confirmed,
        }

    finally:
        page.remove_listener("response", handler)


def scrape_item_with_retry(page, item: dict, max_attempts: int = 3):
    rl_retries = 0
    attempt = 1
    while True:
        try:
            return scrape_item(page, item, attempt, max_attempts)
        except RateLimited:
            # Not the item's fault — pause all threads, then retry the same item.
            if rl_retries >= 3:
                raise
            rl_retries += 1
            _rl_trigger()
            _rl_wait()
        except PlaywrightTimeout:
            if attempt < max_attempts:
                wait = attempt * 6
                print(
                    f"    ⏳ Timeout, retry {attempt}/{max_attempts - 1} in {wait}s..."
                )
                time.sleep(wait)
                attempt += 1
            else:
                raise


def scrape_item_worker(item, max_attempts=3):
    """
    Thread worker that connects to the shared CDP port, creates its own page,
    and scrapes the item safely in a threaded environment.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            page = context.new_page()
            try:
                scraped_data = scrape_item_with_retry(
                    page, item, max_attempts=max_attempts
                )
                return scraped_data, None
            finally:
                page.close()
    except Exception as e:
        return None, e


# ─────────────────────────────────────────
# MAIN SCRAPE LOOP (Multi-Threaded)
# ─────────────────────────────────────────


def scrape_all(all_items: list, output_file: str = "vinted_trends.csv"):
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "keyword",
                "title",
                "price",
                "likes",
                "views",
                "brand",
                "country",
                "url",
                "status",
                "sold_confirmed",
                "offers",
                "published_at",
                "age_hours",
                "age_band",
                "score",
                "verdict",
            ]
        )

        with sync_playwright() as p:
            _, context = connect(p)
            page = get_or_create_page(context)

            # Verify login
            page.goto(
                "https://www.vinted.fr", timeout=40000, wait_until="domcontentloaded"
            )
            time.sleep(3)
            if page.query_selector("[data-testid='header--login-button']"):
                print("⚠️  Not logged in — please log in, then press ENTER.")
                input()
            # Do NOT close this baseline page so the browser doesn't shutdown contexts.

        total = len(all_items)
        skipped = 0
        processed_count = 0

        print(
            f"\n🚀 Starting multi-threaded extraction (Max 3 parallel tabs) for {total} items..."
        )

        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_item = {
                executor.submit(scrape_item_worker, item): item for item in all_items
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                processed_count += 1

                # Global human delay block to space out processing slightly
                time.sleep(random.uniform(2, 4))

                try:
                    scraped_data, err = future.result()
                    if err:
                        raise err

                    item.update(scraped_data)
                    item["age_band"] = age_band(item["hours"])
                    item["age_display"] = age_display(item["hours"])
                    item["score"] = calculate_score(item)
                    item["verdict"] = get_verdict(item, item["score"])

                    with CSV_LOCK:
                        writer.writerow(
                            [
                                item["keyword"],
                                item["title"],
                                item["price"],
                                item["likes"],
                                item.get("views", 0),
                                item["brand"],
                                item["country"],
                                item["url"],
                                item["status"],
                                item["sold_confirmed"],
                                item["offers"],
                                item["published_at"],
                                item["hours"],
                                item["age_band"],
                                item["score"],
                                item["verdict"],
                            ]
                        )
                        f.flush()

                    print(
                        f"  ✅ {processed_count}/{total} — "
                        f"{item['title'][:38]} | "
                        f"{item['offers']} offers | {item['age_display']} | {item['status']} | {item['verdict']}"
                    )

                except Exception as e:
                    skipped += 1
                    print(
                        f"  ❌ {processed_count}/{total} — skipped ({type(e).__name__})"
                    )
                    item["age_band"] = age_band(item.get("hours", 24.0))
                    item["age_display"] = age_display(item.get("hours", 24.0))
                    item["score"] = calculate_score(item)
                    item["verdict"] = get_verdict(item, item["score"])

                    with CSV_LOCK:
                        writer.writerow(
                            [
                                item["keyword"],
                                item["title"],
                                item["price"],
                                item["likes"],
                                item.get("views", 0),
                                item["brand"],
                                item["country"],
                                item["url"],
                                item["status"],
                                item.get("sold_confirmed", False),
                                item["offers"],
                                item.get("published_at", ""),
                                item["hours"],
                                item.get("age_band", ""),
                                item["score"],
                                item["verdict"],
                            ]
                        )
                        f.flush()

        print(
            f"\n📊 Finished: {total - skipped}/{total} scraped, {skipped} permanently skipped."
        )


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────


def age_band(hours: float) -> str:
    if hours < 1:
        return "<1h"
    elif hours <= 24:
        return "24h"
    elif hours <= 72:
        return "72h"
    elif hours <= 168:
        return "1 week"
    elif hours <= 720:
        return "1 month"
    else:
        return "old"


def detect_sold_status(page) -> tuple[bool, str]:
    """
    Robust sold detection using dedicated Vinted sidebar status components.
    Returns:
        (sold_confirmed: bool, status: str)
    status ∈ active / sold / unavailable
    """
    try:
        # STRICTEST: sidebar item-status
        sidebar_status = page.locator("aside [data-testid='item-status']")

        if sidebar_status.count() > 0:
            txt = sidebar_status.first.inner_text(timeout=2000).strip().lower()

            if "sold" in txt or "vendu" in txt:
                return True, "sold"

            if (
                "no longer available" in txt
                or "n'est plus disponible" in txt
                or "unavailable" in txt
            ):
                return False, "unavailable"

        # Secondary: item-status-content
        status_content = page.locator("[data-testid='item-status-content']")

        if status_content.count() > 0:
            txt = status_content.first.inner_text(timeout=2000).strip().lower()

            if txt in ["sold", "vendu"]:
                return True, "sold"

        # Tertiary: success block
        success_block = page.locator(".web_ui__Cell__success")

        if success_block.count() > 0:
            txt = success_block.first.inner_text(timeout=2000).strip().lower()

            if "sold" in txt or "vendu" in txt:
                return True, "sold"

        return False, "active"

    except Exception:
        return False, "active"


def compute_age_hours(created_at_ts: int) -> float:
    """Convert a Unix timestamp to hours since publication."""
    try:
        published = datetime.fromtimestamp(created_at_ts, tz=timezone.utc)
        delta = datetime.now(tz=timezone.utc) - published
        return round(delta.total_seconds() / 3600, 1)
    except Exception:
        return 24.0


def parse_time(text: str) -> float:
    """
    Convert a Vinted publication time string into fractional hours.
    14 minutes -> 0.23
    2 hours    -> 2.0
    2 days     -> 48.0
    """
    if not text:
        return 9999

    txt = text.lower().strip()
    m = re.search(r"(\d+)", txt)
    if not m:
        if any(
            x in txt
            for x in [
                "instants",
                "instant",
                "moments",
                "moment",
                "just now",
                "récemment",
                "recently",
                "vient",
                "juste",
            ]
        ):
            return 0.02  # "a few moments ago" → ~1 minute
        # Handle "a week ago" / "une semaine" without a digit
        if "week" in txt or "semaine" in txt:
            return 168.0
        if "month" in txt or "mois" in txt:
            return 720.0
        if "day" in txt or "jour" in txt:
            return 24.0
        if "hour" in txt or "heure" in txt:
            return 1.0
        return 0.5  # safe fallback for any unrecognized text

    v = int(m.group(1))

    # Seconds -> round up to 1 minute (0.02h)
    if any(
        x in txt
        for x in ["second", "seconds", "segundo", "segundos", "sekunde", "sekunden"]
    ):
        return round(1 / 60, 2)  # treat as 1 minute

    # Minutes (check BEFORE "m" which would match month)
    if any(
        x in txt for x in ["minute", "minutes", "min", "minuto", "minutos", "minuten"]
    ):
        return round(v / 60, 2)

    # Hours
    if any(
        x in txt
        for x in [
            "hour",
            "hours",
            "heure",
            "heures",
            "hora",
            "horas",
            "stunde",
            "stunden",
            "hr",
        ]
    ):
        return float(v)

    # Days
    if any(
        x in txt for x in ["day", "days", "jour", "jours", "día", "días", "tag", "tage"]
    ):
        return float(v * 24)

    # Weeks
    if any(x in txt for x in ["week", "weeks", "semaine", "semaines"]):
        return float(v * 24 * 7)

    # Months
    if any(x in txt for x in ["month", "months", "mois"]):
        return float(v * 24 * 30)

    return 9999


def calculate_score(item: dict) -> int:
    """
    Age-differentiated trend scoring:
    - <1h:   ultra-fresh bonus for explosive early demand
    - <24h: fresh listings evaluated leniently (early watchlist allowed)
    - 24-72h: moderate window, requires some signal
    - >72h: hard cutoff, Low unless exceptional
    """
    hours = item.get("hours", 24.0)
    offers = item.get("offers", 0)

    # ⚡ Fast Sale (overrides everything)
    if (
        item.get("status") == "sold"
        and item.get("sold_confirmed", False)
        and hours <= 24
        and offers >= 1
    ):
        return 99

    # >72h: hard cutoff — only 11+ offers qualifies
    if hours > 72:
        if offers >= 11:
            return 16
        return 0

    # ── Offers-based scoring (within 72h window) ──
    if offers >= 11:
        return 16  # 🔥🔥 Very Strong Demand
    elif offers >= 5:
        base = 11  # 🔥 Trending
    elif offers >= 1:
        base = 6  # 📈 Growing
    elif hours <= 24:
        return 3  # 👀 Early Watchlist (too new for offers)
    else:
        return 2  # 📊 Monitoring (24-72h, no traction yet)

    # Recency bonus — finer granularity now that hours can be <1
    if hours <= 1:
        base += 3  # 🚀 Explosive Early Trend
    elif hours < 6:
        base += 2
    elif hours < 24:
        base += 1

    return base


def age_display(hours: float) -> str:
    """Human-readable age string from fractional hours."""
    if hours < 1:
        return f"{int(round(hours * 60))}m"
    return f"{round(hours, 1)}h"


def get_verdict(item: dict, score: int) -> str:
    if score == 99:
        return "⚡ Fast Sale (Confirmed)"
    if score >= 14:
        return "🚀 Explosive Early Trend"
    if score >= 11:
        return "🔥 Trending"
    if score >= 6:
        return "📈 Growing"
    if score >= 3:
        return "👀 Early Watchlist"
    if score >= 2:
        return "📊 Monitoring"
    return "⚠️ Low"


def write_summary(all_items: list, output_file: str = "vinted_summary.csv"):
    keywords = list(set(item["keyword"] for item in all_items))

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "keyword",
                "total_items",
                "active_count",
                "sold_count",
                "avg_offers",
                "median_offers",
                "avg_likes",
                "avg_price",
                "top_verdict",
                "saturation_flag",
            ]
        )

        for kw in keywords:
            kw_items = [i for i in all_items if i["keyword"] == kw]
            total = len(kw_items)
            active = sum(1 for i in kw_items if i["status"] == "active")
            sold = sum(1 for i in kw_items if i["status"] == "sold")

            offers_list = [i["offers"] for i in kw_items]
            likes_list = [i["likes"] for i in kw_items]
            price_list = [i["price"] for i in kw_items]

            avg_offers = sum(offers_list) / total if total > 0 else 0
            median_offers = statistics.median(offers_list) if total > 0 else 0
            avg_likes = sum(likes_list) / total if total > 0 else 0
            avg_price = sum(price_list) / total if total > 0 else 0

            scores = [i["score"] for i in kw_items]
            top_score = max(scores) if scores else 0
            # Dummy item to pass to get_verdict to just translate score
            top_verdict = get_verdict(
                {"offers": 0, "hours": 0, "likes": 0, "views": 0, "status": "active"},
                top_score,
            )

            saturation_flag = (
                "Yes"
                if active > 100
                and median_offers == 0
                and statistics.median(likes_list) < 5
                else "No"
            )

            writer.writerow(
                [
                    kw,
                    total,
                    active,
                    sold,
                    round(avg_offers, 2),
                    median_offers,
                    round(avg_likes, 2),
                    round(avg_price, 2),
                    top_verdict,
                    saturation_flag,
                ]
            )


# ─────────────────────────────────────────
# HISTORICAL TRACKING LAYER (Phase 3)
# ─────────────────────────────────────────

SNAPSHOT_DIR = "snapshots"

# Demand signals are a "right now" measure, so they are computed over listings
# published within this window. Supply/competition still uses ALL active
# listings. Override with the VINTED_RECENCY_HOURS env var.
RECENCY_WINDOW_HOURS = float(os.environ.get("VINTED_RECENCY_HOURS", 168))  # 7 days


def competition_level(total: int) -> str:
    """Classify market supply into the roadmap competition tiers."""
    if total < 50:
        return "🟢 Low Competition"
    elif total <= 200:
        return "🟡 Healthy Competition"
    elif total <= 500:
        return "🟠 Medium Saturation"
    else:
        return "🔴 Competitive Market"


def compute_keyword_metrics(
    kw_items: list,
    recency_hours: float = RECENCY_WINDOW_HOURS,
    total_override: int | None = None,
) -> dict:
    """
    Aggregate per-keyword market metrics from a list of scraped items.
    Single source of truth used by both the live analysis and the daily snapshot.

    Supply/competition uses the TRUE catalog size (total_override, when the scrape
    was capped) so a broad keyword still reads as saturated. Demand signals
    (offers, likes, price) are computed over the FRESH cohort of the scraped items.
    """
    scraped_n = len(kw_items)
    total = total_override if total_override is not None else scraped_n

    fresh = [i for i in kw_items if (i.get("hours", 24) or 24) <= recency_hours]
    fresh_count = len(fresh)
    demand_pool = fresh if fresh else kw_items  # avoid divide-by-zero / empty avgs

    offers_list = [i.get("offers", 0) for i in demand_pool]
    likes_list = [i.get("likes", 0) for i in demand_pool]
    price_list = [i["price"] for i in demand_pool if i.get("price", 0) > 0]

    dn = len(demand_pool)
    avg_offers = round(sum(offers_list) / dn, 1) if dn else 0
    avg_likes = round(sum(likes_list) / dn, 1) if dn else 0
    avg_price = round(sum(price_list) / len(price_list), 1) if price_list else 0

    sold_count = sum(1 for i in kw_items if i.get("status") == "sold")

    fast_sales = sum(
        1
        for i in kw_items
        if i.get("status") == "sold"
        and i.get("hours", 24) <= 24
        and i.get("offers", 0) >= 1
    )

    # Early velocity: listings <24h old with 5+ offers (hype concentration)
    explosive_count = sum(
        1 for i in kw_items if i.get("hours", 24) < 24 and i.get("offers", 0) >= 5
    )

    # Saturation check: too many listings in first 24h suggests overheated market
    listings_under_24h = sum(1 for i in kw_items if i.get("hours", 24) < 24)

    # ── Component scores (each 1-4, max 12) ──
    if total < 50:
        comp_score = 4
    elif total < 150:
        comp_score = 3
    elif total < 300:
        comp_score = 2
    else:
        comp_score = 1

    if avg_offers >= 20:
        offer_score = 4
    elif avg_offers >= 10:
        offer_score = 3
    elif avg_offers >= 5:
        offer_score = 2
    else:
        offer_score = 1

    if explosive_count >= 25:
        velocity_score = 4
    elif explosive_count >= 10:
        velocity_score = 3
    elif explosive_count >= 5:
        velocity_score = 2
    else:
        velocity_score = 1

    niche_score = comp_score + offer_score + velocity_score

    saturation_note = ""
    if listings_under_24h > 50:
        niche_score -= 1
        saturation_note = f"{listings_under_24h} listings <24h (saturated)"

    if niche_score >= 10:
        verdict = "🚀 High Opportunity Niche"
    elif niche_score >= 7:
        verdict = "🔥 Strong Potential"
    elif niche_score >= 4:
        verdict = "👍 Promising"
    else:
        verdict = "⚠️ Weak"

    return {
        "total_listings": total,
        "fresh_listings": fresh_count,
        "competition_level": competition_level(total),
        "avg_offers": avg_offers,
        "avg_likes": avg_likes,
        "avg_price": avg_price,
        "sold_count": sold_count,
        "fast_sales": fast_sales,
        "explosive_count": explosive_count,
        "listings_under_24h": listings_under_24h,
        "comp_score": comp_score,
        "offer_score": offer_score,
        "velocity_score": velocity_score,
        "niche_score": niche_score,
        "saturation_note": saturation_note,
        "verdict": verdict,
    }


# Columns persisted per keyword per day. Order matters for the CSV.
SNAPSHOT_FIELDS = [
    "date",
    "run_timestamp",
    "keyword",
    "total_listings",
    "fresh_listings",
    "competition_level",
    "avg_offers",
    "avg_likes",
    "avg_price",
    "sold_count",
    "fast_sales",
    "explosive_count",
    "listings_under_24h",
    "comp_score",
    "offer_score",
    "velocity_score",
    "niche_score",
    "verdict",
    "opportunity_score",
    "opportunity_tier",
]


def save_daily_snapshot(metrics_by_keyword: dict) -> str:
    """
    Persist today's per-keyword metrics to snapshots/keyword_YYYY-MM-DD.csv.
    If the file already exists (a second run on the same day), it is overwritten
    so each file represents the latest daily reading — keeping 'daily' semantics.
    Returns the snapshot file path.
    """
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    run_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(SNAPSHOT_DIR, f"keyword_{date_str}.csv")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        for kw, m in metrics_by_keyword.items():
            row = {k: m.get(k, "") for k in SNAPSHOT_FIELDS}
            row["date"] = date_str
            row["run_timestamp"] = run_ts
            row["keyword"] = kw
            writer.writerow(row)

    return path


def load_previous_snapshot(exclude_date: str | None = None) -> dict:
    """
    Load the most recent prior daily snapshot (excluding `exclude_date`, normally
    today). Returns {keyword: row_dict}. Empty dict if no prior snapshot exists.
    """
    if not os.path.isdir(SNAPSHOT_DIR):
        return {}

    files = sorted(
        fn
        for fn in os.listdir(SNAPSHOT_DIR)
        if fn.startswith("keyword_") and fn.endswith(".csv")
    )
    # Strip today's file so we compare against an earlier day.
    if exclude_date:
        files = [fn for fn in files if f"keyword_{exclude_date}.csv" != fn]

    if not files:
        return {}

    latest = os.path.join(SNAPSHOT_DIR, files[-1])
    out = {}
    try:
        with open(latest, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row.get("keyword", "")] = row
    except Exception:
        return {}
    return out


def _to_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def compute_evolution(today: dict, prev: dict | None) -> dict:
    """
    Compare today's metrics against the previous snapshot for the same keyword.
    Detects demand acceleration/decay and saturation growth.
    Returns deltas plus a human-readable trend label.
    """
    if not prev:
        return {
            "prev_date": "",
            "d_listings": 0,
            "d_avg_offers": 0.0,
            "d_niche_score": 0,
            "trend": "🆕 First snapshot (no history yet)",
        }

    d_listings = today["total_listings"] - int(_to_float(prev.get("total_listings")))
    d_avg_offers = round(
        today["avg_offers"] - _to_float(prev.get("avg_offers")), 1
    )
    d_niche = today["niche_score"] - int(_to_float(prev.get("niche_score")))

    prev_listings = int(_to_float(prev.get("total_listings"))) or 1
    listing_growth_pct = d_listings / prev_listings

    # Classification priority: demand momentum first, then supply/saturation.
    if d_avg_offers >= 2 or d_niche >= 2:
        trend = "📈 Demand Accelerating"
    elif d_avg_offers <= -2 or d_niche <= -2:
        trend = "📉 Demand Decaying"
    elif listing_growth_pct >= 0.5 and d_avg_offers <= 0:
        trend = "⚠️ Saturation Growing (supply up, demand flat)"
    else:
        trend = "➡️ Stable"

    return {
        "prev_date": prev.get("date", ""),
        "d_listings": d_listings,
        "d_avg_offers": d_avg_offers,
        "d_niche_score": d_niche,
        "trend": trend,
    }


# ─────────────────────────────────────────
# BUSINESS OPPORTUNITY ENGINE (Phase 3)
# ─────────────────────────────────────────


def compute_opportunity_score(m: dict, evo: dict) -> dict:
    """
    Global 0–100 business-opportunity score combining the roadmap inputs:
    offer velocity, sales velocity, competition, freshness, saturation,
    historical evolution, and rarity.

    Component caps (sum to 100 before the saturation penalty):
        demand intensity 25 · sales velocity 20 · competition 15 ·
        freshness 15 · historical evolution 15 · rarity 10
    """
    total = m["total_listings"] or 1
    avg_offers = m["avg_offers"]

    # Demand intensity — offer concentration (10 avg offers → full marks)
    demand = min(25.0, avg_offers * 2.5)

    # Sales velocity — confirmed fast sales relative to supply (10% → full marks)
    sales = min(20.0, (m["fast_sales"] / total) * 200)

    # Competition — scarcer supply scores higher
    t = m["total_listings"]
    if t < 50:
        competition = 15.0
    elif t <= 200:
        competition = 11.0
    elif t <= 500:
        competition = 6.0
    else:
        competition = 2.0

    # Freshness — hype concentrated in the newest listings
    freshness = min(15.0, (m["explosive_count"] / total) * 100)

    # Historical evolution — rewards acceleration, neutral on first snapshot
    trend = evo.get("trend", "")
    if "Accelerating" in trend:
        history = 15.0
    elif "Saturation Growing" in trend:
        history = 2.0
    elif "Decaying" in trend:
        history = 0.0
    else:  # Stable or first snapshot
        history = 7.0

    # Rarity — real demand against scarce supply (the prime arbitrage signal)
    if t < 50 and avg_offers >= 3:
        rarity = 10.0
    elif t < 150 and avg_offers >= 5:
        rarity = 5.0
    else:
        rarity = 0.0

    # Saturation penalty — overheated fresh supply
    saturation_penalty = -10.0 if m["listings_under_24h"] > 50 else 0.0

    raw = (
        demand + sales + competition + freshness + history + rarity + saturation_penalty
    )
    score = max(0, min(100, round(raw)))

    if score >= 80:
        tier = "💥 Explosive Niche"
    elif score >= 60:
        tier = "🔥 Strong Opportunity"
    elif score >= 30:
        tier = "👍 Interesting"
    else:
        tier = "⚠️ Weak"

    return {
        "score": score,
        "tier": tier,
        "breakdown": {
            "demand": round(demand, 1),
            "sales": round(sales, 1),
            "competition": round(competition, 1),
            "freshness": round(freshness, 1),
            "history": round(history, 1),
            "rarity": round(rarity, 1),
            "saturation_penalty": round(saturation_penalty, 1),
        },
    }


# ─────────────────────────────────────────
# ALERT LAYER (Phase 3)
# ─────────────────────────────────────────


def generate_alerts(kw: str, m: dict, evo: dict, opp: dict) -> list:
    """
    Generate intelligent alerts from the current metrics, evolution, and
    opportunity score. Returns a list of {level, type, message} dicts.
    level ∈ info / opportunity / warning
    """
    alerts = []

    # ⚡ Fast Sale — confirmed quick sales signal strong, proven demand
    if m["fast_sales"] >= 1:
        alerts.append({
            "level": "info",
            "type": "Fast Sale",
            "message": f"⚡ {m['fast_sales']} fast sale(s) within 24h on '{kw}'.",
        })

    # 🔥🔥 Explosive Demand — high offer concentration or many hot fresh listings
    if m["avg_offers"] >= 10 or m["explosive_count"] >= 5:
        alerts.append({
            "level": "opportunity",
            "type": "Explosive Demand",
            "message": (
                f"🔥🔥 '{kw}' showing explosive demand "
                f"(avg {m['avg_offers']} offers, {m['explosive_count']} hot listings <24h)."
            ),
        })

    # 🚀 Emerging Niche — strong opportunity score with room to compete
    if opp["score"] >= 60 and m["total_listings"] <= 200:
        alerts.append({
            "level": "opportunity",
            "type": "Emerging Niche",
            "message": (
                f"🚀 '{kw}' is an emerging niche — opportunity {opp['score']}/100 "
                f"({opp['tier']}) with only {m['total_listings']} listings."
            ),
        })

    # 📈 Trend Acceleration — demand growing vs the prior snapshot
    if "Accelerating" in evo.get("trend", ""):
        alerts.append({
            "level": "opportunity",
            "type": "Trend Acceleration",
            "message": (
                f"📈 '{kw}' demand accelerating since {evo.get('prev_date', 'last run')} "
                f"(offers {evo['d_avg_offers']:+.1f}, niche {evo['d_niche_score']:+d})."
            ),
        })

    # ⚠️ Saturation — supply outrunning demand, or overheated fresh supply
    if "Saturation Growing" in evo.get("trend", "") or m["listings_under_24h"] > 50:
        detail = (
            evo["trend"]
            if "Saturation" in evo.get("trend", "")
            else f"{m['listings_under_24h']} listings <24h"
        )
        alerts.append({
            "level": "warning",
            "type": "Saturation",
            "message": f"⚠️ '{kw}' saturation risk — {detail}.",
        })

    return alerts


def save_alerts(alerts: list) -> str | None:
    """Persist this run's alerts to snapshots/alerts_YYYY-MM-DD.csv. Returns path."""
    if not alerts:
        return None
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    now = datetime.now()
    path = os.path.join(SNAPSHOT_DIR, f"alerts_{now.strftime('%Y-%m-%d')}.csv")
    run_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["run_timestamp", "level", "type", "message"])
        for a in alerts:
            writer.writerow([run_ts, a["level"], a["type"], a["message"]])
    return path


# ─────────────────────────────────────────
# AUTOMATIC TREND DISCOVERY (Phase 3)
# ─────────────────────────────────────────

# Multi-language filler words that should never surface as "trending terms".
DISCOVERY_STOPWORDS = {
    # FR
    "de", "la", "le", "les", "un", "une", "des", "du", "et", "en", "avec",
    "pour", "sur", "au", "aux", "ou", "dans", "par", "taille", "neuf",
    # EN
    "the", "and", "with", "for", "new", "size", "style", "look", "original",
    # ES
    "el", "los", "las", "con", "para", "por", "nuevo", "nueva", "talla",
    # DE
    "der", "die", "das", "und", "mit", "fur", "neu", "grosse",
    # IT
    "il", "lo", "gli", "per", "nuovo", "nuova", "taglia",
    # generic
    "vintage", "authentique", "authentic", "edition", "rare",
}

# Capacity patterns (1.18l, 40oz, 500ml, 33cl) are kept as single tokens instead
# of being split on the decimal — they're essential to a specific product niche.
TOKEN_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s?(?:l|oz|ml|cl)\b|[a-zA-ZÀ-ÿ0-9]+", re.IGNORECASE
)
CAP_RE = re.compile(r"^\d+(?:[.,]\d+)?(?:l|oz|ml|cl)$")


def _tokenize(text: str) -> list:
    """Tokenize a title, keeping capacity units intact and normalised (1,18 l → 1.18l)."""
    toks = []
    for raw in TOKEN_RE.findall(text):
        t = raw.replace(" ", "")
        if CAP_RE.match(t.replace(",", ".")):
            t = t.replace(",", ".")
        toks.append(t)
    return toks


def _is_meaningful_token(tok: str) -> bool:
    if len(tok) < 3:
        return False
    if tok in DISCOVERY_STOPWORDS:
        return False
    if tok in EXCLUDE_TERMS:
        return False
    if tok.isdigit():
        return False
    return True


def discover_trending_keywords(items: list, top_n: int = 15) -> list:
    """
    Auto-surface trending terms from scraped item titles, weighting each
    occurrence by its demand signal so hot listings dominate the ranking.

    Weight per item = (1 + offers + fast-sale bonus) × recency multiplier.
    Returns a ranked list of {term, score, count, avg_offers}, unigrams and
    bigrams combined.
    """
    agg = {}  # term -> {"score": float, "count": int, "offers": int}

    for it in items:
        title = (it.get("title") or "").lower()
        tokens = _tokenize(title)

        offers = it.get("offers", 0) or 0
        hours = it.get("hours", 24) or 24
        fast_sale = (
            it.get("status") == "sold" and hours <= 24 and offers >= 1
        )

        recency = 2.0 if hours < 24 else (1.0 if hours < 72 else 0.5)
        weight = (1 + offers + (5 if fast_sale else 0)) * recency

        # Build candidate terms: meaningful unigrams + adjacent n-grams up to 4
        # words, so specific niches (model + colour + size) can form.
        terms = [t for t in tokens if _is_meaningful_token(t)]
        for n in (2, 3, 4):
            for i in range(len(tokens) - n + 1):
                gram = tokens[i : i + n]
                if all(_is_meaningful_token(x) for x in gram):
                    terms.append(" ".join(gram))

        for term in set(terms):  # count each term once per listing
            slot = agg.setdefault(term, {"score": 0.0, "count": 0, "offers": 0})
            slot["score"] += weight
            slot["count"] += 1
            slot["offers"] += offers

    ranked = [
        {
            "term": term,
            "score": round(v["score"], 1),
            "count": v["count"],
            "avg_offers": round(v["offers"] / v["count"], 1) if v["count"] else 0,
        }
        for term, v in agg.items()
        # Require a minimum of supporting listings so one-off noise is filtered out.
        if v["count"] >= 2
    ]
    ranked.sort(key=lambda x: x["score"], reverse=True)

    # Prefer the most specific term: drop a shorter term when a longer one
    # contains all its words with equal score and listing count (i.e. they always
    # co-occur — so "stanley quencher" gives way to "stanley quencher 1.18l").
    deduped = []
    for r in ranked:
        r_words = set(r["term"].split())
        r_len = len(r_words)
        redundant = any(
            len(o["term"].split()) > r_len
            and r_words <= set(o["term"].split())
            and o["count"] == r["count"]
            and o["score"] >= r["score"]
            for o in ranked
        )
        if not redundant:
            deduped.append(r)

    return deduped[:top_n]


def save_keyword_research(
    research: list, seed_keywords: list, output_file: str = "keyword_research.csv"
) -> str | None:
    """
    Export the demand-weighted related terms as a reusable keyword-research list
    (Helium-10 style), ranked strongest-first. `is_seed` marks the original typed
    keywords so they can be filtered out to see only newly discovered terms.
    """
    if not research:
        return None
    # A term is "seed-derived" when all its words appear in one of the typed
    # keywords. Filtering is_seed=no leaves only genuinely new related terms.
    seed_token_sets = [set(_tokenize(s.lower())) for s in seed_keywords]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["rank", "term", "demand_score", "listings", "avg_offers", "is_seed"]
        )
        for rank, d in enumerate(research, 1):
            term_tokens = set(d["term"].split())
            is_seed = any(term_tokens <= sts for sts in seed_token_sets)
            writer.writerow(
                [
                    rank,
                    d["term"],
                    d["score"],
                    d["count"],
                    d["avg_offers"],
                    "yes" if is_seed else "no",
                ]
            )
    return output_file


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────


def main():
    # Keywords can be strings ("keyword") or tuples ("keyword", catalog_id)
    # keywords = [
    #     "Audemars Piguet x Swatch",
    # ]
    # Keywords come from VINTED_KEYWORDS env var if set (scriptable / non-interactive
    # runs), otherwise prompt interactively.
    env_keywords = os.environ.get("VINTED_KEYWORDS")
    if env_keywords:
        keywords = env_keywords
        print(f"Enter Keyword: {keywords}  (from VINTED_KEYWORDS)")
    else:
        keywords = input("Enter Keyword:")
    keywords = keywords.strip().split(",")
    keywords = [kw.strip() for kw in keywords if kw.strip()]
    if not keywords:
        print("No keywords entered.")
        return
    output_file = "vinted_trends.csv"
    raw_output_file = "vinted_trends_raw.csv"
    summary_file = "vinted_summary.csv"

    # Optional cap on catalog pages fetched per keyword (newest-first), to avoid
    # paging deep into stale listings. Default: unlimited (rely on dynamic stop).
    max_pages_env = os.environ.get("VINTED_MAX_PAGES")
    max_pages = int(max_pages_env) if max_pages_env else None

    # Cap how many item PAGES are scraped per keyword (the freshest ones). Broad
    # keywords return 1000+ listings; visiting every page trips Vinted's rate
    # limiting. We scrape the freshest N for demand, but keep the TRUE catalog
    # count for competition. Default 300; set VINTED_MAX_ITEMS=0 for unlimited.
    max_items_env = os.environ.get("VINTED_MAX_ITEMS")
    max_items = int(max_items_env) if max_items_env is not None else 300
    if max_items <= 0:
        max_items = None
    keyword_totals = {}  # true catalog size per keyword (for competition)

    if os.path.exists(output_file):
        os.remove(output_file)
    if os.path.exists(raw_output_file):
        os.remove(raw_output_file)
    if os.path.exists(summary_file):
        os.remove(summary_file)

    all_items = []

    with open(raw_output_file, "w", newline="", encoding="utf-8") as raw_f:
        raw_writer = csv.writer(raw_f)
        raw_writer.writerow(
            ["keyword", "title", "price", "likes", "views", "brand", "country", "url"]
        )

        with sync_playwright() as p:
            _, context = connect(p)
            page = get_or_create_page(context, "https://www.vinted.fr")

            # Check login once at the start
            time.sleep(3)
            if page.query_selector("[data-testid='header--login-button']"):
                print("⚠️  Not logged in — please log in, then press ENTER.")
                input()

            # Phase 1 — catalog fetch via requests
            print("🔄 Getting cookies and access token...")
            cookies_dict, access_token = get_cookies_and_token(context, page)
            print(f"   🔑 Access token: {'found' if access_token else 'NOT FOUND'}")

            for entry in keywords:
                if isinstance(entry, tuple):
                    kw, catalog_id = entry
                else:
                    kw, catalog_id = entry, None

                print(f"\n🔍 Fetching catalog for: {kw}")
                raw = fetch_catalog_via_requests(
                    kw,
                    cookies_dict,
                    access_token,
                    catalog_id=catalog_id,
                    max_pages=max_pages,
                )
                extracted = extract_data(raw, kw)
                keyword_totals[kw] = len(extracted)  # true catalog size
                print(f"   → {len(extracted)} items found")

                # Scrape only the freshest N item pages (catalog is newest-first)
                # to stay under Vinted's rate limits on broad keywords.
                if max_items and len(extracted) > max_items:
                    print(
                        f"   → scraping the freshest {max_items} of {len(extracted)} "
                        f"(competition still counts all {len(extracted)})"
                    )
                    extracted = extracted[:max_items]

                # Write to raw CSV in real-time
                for item in extracted:
                    raw_writer.writerow(
                        [
                            item["keyword"],
                            item["title"],
                            item["price"],
                            item["likes"],
                            item.get("views", 0),
                            item["brand"],
                            item["country"],
                            item["url"],
                        ]
                    )
                raw_f.flush()

                all_items.extend(extracted)

                # Pause between keywords to look natural
                human_delay(5, 12)

    print(f"\n📦 Total items to scrape: {len(all_items)}")

    # Phase 2 — visit each item page
    scrape_all(all_items, output_file)

    # Phase 3 - Write summary statistics
    write_summary(all_items, summary_file)

    # Phase 4 - Velocity-weighted niche opportunity analysis + historical evolution
    print("\n" + "=" * 60)
    print("📊 NICHE OPPORTUNITY ANALYSIS")
    print("   Competition = all listings · Demand = fresh listings only")
    print(f"   (fresh = published within {int(RECENCY_WINDOW_HOURS)}h)")
    print("=" * 60)

    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_snapshot = load_previous_snapshot(exclude_date=today_str)

    metrics_by_keyword = {}
    all_alerts = []
    for kw in set(item["keyword"] for item in all_items):
        kw_items = [i for i in all_items if i["keyword"] == kw]
        m = compute_keyword_metrics(kw_items, total_override=keyword_totals.get(kw))

        evo = compute_evolution(m, prev_snapshot.get(kw))
        opp = compute_opportunity_score(m, evo)
        m["opportunity_score"] = opp["score"]
        m["opportunity_tier"] = opp["tier"]
        metrics_by_keyword[kw] = m

        kw_alerts = generate_alerts(kw, m, evo, opp)
        all_alerts.extend(kw_alerts)

        print(f"\n  Keyword:          {kw}")
        print(f"  Competition:      {m['total_listings']} listings — {m['competition_level']}")
        print(f"  Fresh listings:   {m['fresh_listings']} of {m['total_listings']} "
              f"(demand measured on these)")
        print(f"  Avg Offers:       {m['avg_offers']}  (fresh)")
        print(f"  Avg Likes:        {m['avg_likes']}  (fresh)")
        print(
            f"  Avg Price:        €{m['avg_price']}"
            if m["avg_price"]
            else "  Avg Price:        N/A"
        )
        print(f"  Fast Sales:       {m['fast_sales']}")
        print(f"  Explosive (<24h): {m['explosive_count']} items with 5+ offers")
        print(f"  ───────────────────────────────────")
        print(f"  Competition:      {m['comp_score']}/4")
        print(f"  Offer Intensity:  {m['offer_score']}/4")
        print(f"  Early Velocity:   {m['velocity_score']}/4")
        if m["saturation_note"]:
            print(f"  Saturation:       ⚠️  {m['saturation_note']}")
        print(f"  ───────────────────────────────────")
        print(f"  Niche Score:      {m['niche_score']}/12")
        print(f"  Verdict:          {m['verdict']}")
        print(f"  Opportunity:      {opp['score']}/100 — {opp['tier']}")
        print(f"  ── Evolution ──")
        if evo["prev_date"]:
            print(f"  vs {evo['prev_date']}:     "
                  f"listings {evo['d_listings']:+d}, "
                  f"offers {evo['d_avg_offers']:+.1f}, "
                  f"niche {evo['d_niche_score']:+d}")
        print(f"  Trend:            {evo['trend']}")

    print("=" * 60)

    # Phase 3 - Alert digest
    print("\n" + "=" * 60)
    print(f"🔔 ALERTS ({len(all_alerts)})")
    print("=" * 60)
    if all_alerts:
        # Surface opportunities and warnings before info-level alerts.
        order = {"opportunity": 0, "warning": 1, "info": 2}
        for a in sorted(all_alerts, key=lambda x: order.get(x["level"], 3)):
            print(f"  {a['message']}")
        alerts_path = save_alerts(all_alerts)
        if alerts_path:
            print(f"\n  🗄️  Alerts saved: {alerts_path}")
    else:
        print("  No alerts triggered this run.")
    print("=" * 60)

    # Phase 3 - Automatic trend discovery + keyword-research export
    research = discover_trending_keywords(all_items, top_n=100)
    discovered = research[:15]  # console shows the strongest 15
    print("\n" + "=" * 60)
    print("🔎 AUTO-DISCOVERED TRENDING TERMS (demand-weighted)")
    print("=" * 60)
    if discovered:
        print(f"  {'term':<28} {'score':>8} {'listings':>9} {'avg_off':>8}")
        for d in discovered:
            print(
                f"  {d['term'][:28]:<28} {d['score']:>8} "
                f"{d['count']:>9} {d['avg_offers']:>8}"
            )
        print("\n  💡 High-scoring terms not in your keyword list are candidate niches"
              " to scrape next.")
        research_path = save_keyword_research(research, keywords)
        if research_path:
            print(f"  🗄️  Keyword research exported: {research_path} "
                  f"({len(research)} terms)")
    else:
        print("  Not enough data to surface trending terms.")
    print("=" * 60)

    # Phase 3 - Persist today's snapshot for future evolution analysis
    snapshot_path = save_daily_snapshot(metrics_by_keyword)
    print(f"🗄️  Daily snapshot saved: {snapshot_path}")

    print(
        f"\n🎉 Done! Results saved to {output_file}, {raw_output_file}, and {summary_file}"
    )


def test_single_url(url):
    with sync_playwright() as p:
        browser, context = connect(p)
        page = context.new_page()

        # Minimal item structure
        item = {
            "keyword": "QA_TEST",
            "title": "",
            "price": 0,
            "likes": 0,
            "views": 0,
            "brand": "",
            "country": "",
            "url": url,
            "status": "active",
            "offers": 0,
            "published_at": "",
            "hours": 24.0,
            "score": 0,
            "verdict": "",
            "sold_confirmed": False,
        }

        print(f"\n🌐 Testing URL: {url}")

        scraped = scrape_item_with_retry(page, item)

        item.update(scraped)

        item["score"] = calculate_score(item)
        item["verdict"] = get_verdict(item, item["score"])

        print("\n📊 RESULTS:")
        print(f"Status          : {item['status']}")
        print(f"Sold Confirmed  : {item['sold_confirmed']}")
        print(f"Offers          : {item['offers']}")
        print(f"Hours           : {item['hours']}")
        print(f"Published At    : {item['published_at']}")
        print(f"Score           : {item['score']}")
        print(f"Verdict         : {item['verdict']}")

        page.close()


if __name__ == "__main__":
    main()
