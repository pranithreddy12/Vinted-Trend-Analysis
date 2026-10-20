# Phase 6 completion plan

Written 2026-10-20 18:00. It replaces `PLAN_DISPUTE_FIX.md`, whose Tasks 1–4 are done.
Branch `phase-5-image-recognition`. Commit as pranithreddy12, with **no Co-Authored-By line**.
Never commit `client_data/`, the root CSVs (`women.csv`, `home.csv`, …) or `model_checks_log.csv`.

Tasks are tagged **[AGENT]** (code; give the task number to the coding agent) or **[YOU]**
(messages, checks by hand, the client's machine).

---

## 1. What the client asked for

| # | Request | Source |
|---|---|---|
| C1 | At least 5 products found automatically that he never searched. Each needs the exact model + variants, source listing links, confirmed 30-day sales, median price at sale, median time to sell, and competition. | Oct 20 |
| C2 | A method that never mixes models, colours or sizes. | Oct 20 |
| C3 | Corrected figures for **Smeg cream insulated bottle 500 ml** and **Stanley Quencher H2.0 pink 1.18 L**: sales volume, speed and price. He finds the current numbers too low because titles often omit capacity or colour. | Oct 20, 16:31 |
| C4 | A straight answer on what is built and what is not. No promises of future features. | Oct 20 |
| C5 | The whole category analysed, not part of it. A smaller category is acceptable if it gives complete, reliable data. | Oct 20, FR |
| C6 | A 30-day ranking of the best-selling **brands** (and products) per category, sorted by **trend**, showing offers (e.g. 13+), sales, fast turnover and average price. Five products is the minimum. | Oct 20, FR |
| C7 | Image recognition as a **general** feature of the system, not specific to Stanley or Smeg. | Oct 20, FR |
| C8 | His chosen categories: **Women > Facial care, Women > Athletic shoes, Home > Kitchenware, Home > Pans**. | Oct 20, latest |

He will not approve the order until he has concrete results in hand.

## 2. What we promised

| # | Promise | Due |
|---|---|---|
| P1 | Smeg and Stanley figures from **his own files**, with every sale linked. | **Oct 21 morning** |
| P2 | Five products + a brand ranking, built from his existing data, with links. | **Oct 22** |
| P3 | Rule: a listing counts only when it is fully identified; anything else is "unidentified" and never counted. | always |
| P4 | Honesty: median price = listing price at sale (Vinted doesn't publish the price paid); time to sell is day-accurate, and a lower bound for sweep data; say exactly how many products qualify if fewer than 5. | always |
| P5 | No approval is asked before he has the results. | always |
| P6 | (Draft reply on his 4 categories, **not yet sent**) Set up the 4 categories, measure coverage on day 1, adapt identification per category, first results Oct 22, full 30-day ranking building from setup. | — |

## 3. Where things stand (Oct 20, 18:00)

- **His files** are in `client_data/` (working copies, being modified) and `client_data/original/` (untouched). They came from the old code: size is empty on every row, the brand is the first word of the title, and Women/Home have almost no offer counts.
- **The backlog check is running** in the background: script `run_backlog.py`, log `backlog.log`, both in the scratchpad. It opens every unchecked listing on Vinted and records sold status plus brand, size, colour and category.
- **Progress and order:** Smeg (about 203 left, done ~18:30) → Stanley (703, done ~19:45) → Women (15,431, done ~**Oct 22 00:00**) → Home (10,495, done ~Oct 23).
- **Already built and pushed:**
  - strict product grouping
  - `--by brand` ranking with trend and offers
  - category read from the item page
  - Smeg filter by category
  - logged-out guard on the sold check
  - `verify_backlog.py`
  - `check_model_sales.py` with time to sell
  - auto reports in `run_tracker.bat`
- **Running conditions:** the tracker Chrome (`vinted_profile`, port 9222) must stay open and **logged in**, the PC must not sleep, and `run_tracker.bat` must not run. Browsing Vinted from this PC slows the check, because Vinted rate-limits by IP.

---

## 4. Tasks

### T1 [AGENT] — URGENT, before Women starts (~19:45): store the full category path

**Why:** `_details_from_attrs` keeps only the deepest breadcrumb ("Chaussures de course"). To filter his data to "Athletic shoes" or "Pans", the parent path is needed.

**Build:**
- Add a TRACK_FIELDS column `category_path`, appended at the end.
- In `track_sales._details_from_attrs`, set `category_path` to the crumbs with the `<brand> …` crumb removed, joined with `" > "`. Example: `Femmes > Chaussures > Chaussures de sport > Chaussures de course`.
- Apply it to the row in `verify_disappearances`, next to `category`.

**Test:** extend the existing `_details_from_attrs` assert in `_demo()`.

**Done when:** `python track_sales.py --selftest` passes, and the change is committed **before** the backlog runner reaches `women.csv`. Each pass imports the code fresh, so no restart is needed.

### T2 [AGENT] — Smeg/Stanley check uses the page data

**Problem:** `check_model_sales.check()` prefers the stored `variant` column, which was built from the title by the old code, so it ignores the colour read from the page.

**Build:**
- When a row has `category`, rebuild the variant with `ts.build_variant(title, brand, color, size)` from the page colour instead of using the stored value.
- Add `--level strict|colour|all`, or simply document running it 3 times with or without `--cap`/`--color`.

**Done when:** the self-check passes, and it runs on `client_data/smeg_tumbler.csv` and `client_data/stanley_quencher.csv`.

### T3 [YOU + AGENT] — Deliverable P1, Smeg & Stanley (Oct 21 morning)

1. Once Stanley shows 0 left, run for each product, with `--hours 720`, at three levels:
   - colour + capacity (`--color cream --cap 0.5l` / `--color pink --cap 40oz --line Quencher`)
   - colour only
   - all colours
2. **[YOU]** Open every sold link while **logged in**, and check that it really is that product.
3. **[AGENT]** Write the report: one table per product for the three levels (sold, tracked, active, median price, range, time to sell + n), then one line per sale with date, price, variant and link.
   - Say that the 30 days end at his tracker's last run (Oct 19).
   - Say that the price is the listing price at sale.
   - The Smeg report must state the split by category (kettles / bottles / …) to explain why the old figure was wrong.
4. **[YOU]** Send it.

### T4 [AGENT] — Per-category product identity (needed for C2, C8 and P6)

The current `identified_key` requires colour + size, which suits clothing but rejects all skincare. Make the required parts depend on the product type:

| Type (from `category_path` / type) | Product key | Required to count |
|---|---|---|
| shoes | brand + type + **model** | colour + size (page `size`) |
| facial care (category path contains "Soins du visage" / "Visage") | brand + type + **product name** | volume, parsed from "50 ml" / "50ml" |
| pans ("Poêles", "Casseroles") | brand + type | **diameter**, `(\d{2})\s?cm` in the title |
| kitchenware (other "Cuisine" paths) | brand + type | nothing extra |
| everything else (clothing) | brand + type | colour + size (current rule) |

- **Model / product name** = the first two title words left after removing brand, type words, colours, sizes, numbers and `_STOP`, kept in title order. A listing with no such word is **unidentified** at product level; it still counts in the brand ranking.
- Variants: shoes colour + size; facial care volume; pans diameter.
- Add `_demo()` asserts with real-looking titles:
  - "Nike Pegasus 40 noir 38": key (nike, shoes, pegasus), variant black 38
  - "Caudalie Vinoperfect sérum 30 ml": key (caudalie, …, vinoperfect sérum) or similar; pick and assert
  - "Poêle Tefal Ingenio 28 cm": key (tefal, pan), variant 28cm
  - a skincare listing with no volume is unidentified
- **Acceptance [YOU]:** on the Women data after T6, read the top 10 product groups and confirm none mixes two models. If one does, tighten the rule. Don't ship a mixed group.

### T5 [AGENT] — Category filter for reports

**Build:** add `--category "<text>"` to `product_groups.py`. It keeps only rows whose `category_path` contains the text (case-insensitive). It can be repeated to OR several categories, and works for both `--by brand` and `--by product`.

**Done when:** `python product_groups.py client_data/women.csv --by brand --page-brands-only --category "chaussures de sport"` runs.

### T6 [YOU + AGENT] — Deliverable P2, five products + brand ranking (Oct 22)

1. Once Women shows 0 left (~Oct 22 00:00), produce:
   - a brand ranking for **Women > Athletic shoes** and **Women > Facial care**, sorted by trend, with sales shown;
   - a product ranking for the same, applying the T4 rules;
   - optionally all of Women.
2. Pick the top 5+ products the client never searched (not Stanley, Smeg, Owala, Alo, Ralph Lauren, Jellycat).
3. **Competition:** his active rows have guessed brands, so measure the number of live listings for each of the 5 products with one Vinted search per product **after the backlog finishes** (to avoid rate limits).
4. **[YOU]** Check every sold link by hand while logged in.
5. **[AGENT]** Write the report:
   - per product: links, variants, confirmed sales, median price, time to sell ("at least"), competition, trend;
   - plus the brand tables;
   - state that the data runs to Oct 16, that the sale dates are approximate, and how many listings were unidentified.
6. **[YOU]** Send it on Oct 22. Home (kitchenware/pans) follows as an addendum when Home finishes (~Oct 23).

### T7 [AGENT] — Set up the 4 categories on his tracker

1. **Category IDs:** load one Vinted catalog page per category **after the backlog finishes** (rate limit), or read them from item-page breadcrumb links (`[itemprop='breadcrumb'] a` href, `/catalog/<id>-…`). Needed: Women > Beauté > Soins du visage; Women > Chaussures > Chaussures de sport; Maison > (Cuisine / Outils de cuisine); Maison > … > Poêles (and Casseroles if separate). Confirm the names with the client if they're ambiguous.
2. **`tracked_keywords.txt`:** replace `cat:1904 women` and `cat:1918 home` with 4 `cat:<id> <label>` lines, and keep his watch-list products. The broad sweeps go, to save runtime.
3. **Coverage check** (C5, P6): in `track_sales.main` for seedless sweeps, print `coverage: complete` when this run's fetched IDs overlap the previous run's tracked IDs. If there's no overlap, print `coverage: GAP — more new listings than the ~960 window; run more often or narrow the category`. Add a test with two fake runs.
4. **Offers re-check** (C6): for seedless sweeps, re-open active listings first seen 24–72 h ago (cap with `VINTED_MAX_REOFFER`, default 300) and refresh `offers` using the existing item-page enrichment that already reads offers. Add a test.

### T8 [YOU] — Image recognition on his machine (C7)

- It is **already built and paid for** (Phase 5 Stage A). It's general: any brand, and runs on listings with ≥4 offers. Turn it on with **his** API key using `VISION_AI_SETUP_WINDOWS.md`; set a spend cap first.
- Tell him plainly: the photo reliably names brand and model, but not exact capacity or shade. Those still come from the listing.
- **[AGENT, optional]** Use the vision model name, when present, as the model for T4 grouping, which is better than title words.

### T9 [AGENT + YOU] — Ship the update to his machine

1. Build a zip of the changed code, the new `tracked_keywords.txt` and short install instructions: replace files, keep the `tracking` folder, log in to the tracker Chrome, run once.
2. Scan for secrets. Never include `client_data/` or keys.
3. **[YOU]** Send it after P2 (Oct 22–23). After his first run, ask for the log to read the coverage lines.

### T10 [YOU] — Messages

| When | Message |
|---|---|
| Now | Send the reply confirming his 4 categories (drafted in the chat). Fill in the scope decision. |
| Oct 21 morning | P1 report. |
| Oct 22 | P2 report + "update package coming". |
| Oct 23 | Home addendum + update package + coverage measurement plan. |
| After 7 days | First signals from the new category tracking. |
| After 30 days | Full 30-day ranking. |

---

## 5. Schedule

| When | What |
|---|---|
| Oct 20, before 19:45 | **T1** (urgent), then T2 |
| Oct 20 evening | Smeg/Stanley finish, then T3 steps 1–3 |
| Oct 21 morning | **Send P1** |
| Oct 21 day | T4, T5, T7 (except the ID lookup), T8 with the client |
| Oct 22, ~00:00 | Women finishes |
| Oct 22 | T6, **send P2** |
| Oct 23 | Home finishes, Home addendum, T7 IDs, T9 package |
| Oct 23 + 1 run | Read his coverage lines; narrow athletic shoes if there's a gap |

## 6. Risks and rules

- **Chrome closes or logs out:** the run stops or leaves listings unverified, and nothing is lost. Re-log in, then rerun `run_backlog.py`; it resumes. Read progress from the CSVs, not the log (its output is buffered).
- **Rate limit:** don't browse Vinted from this PC while the backlog runs.
- **No preliminary numbers to the client** (the "69 Smeg bottles" figure included appliances). Only send checked numbers with links.
- **If fewer than 5 products qualify, say how many.** Never loosen the rule silently.

**Scope decision (YOU):** T4, T5 and T7 finish what Phase 6 promised (category discovery). The recommendation is to do them inside Phase 6. T8 uses the paid Stage A. Not in this plan and to be quoted separately if asked:
- subcategory-by-subcategory coverage of all of Women/Home
- dashboard / SaaS
- Stage B generic-product lookup
- AI-written insights (Phase 7)
