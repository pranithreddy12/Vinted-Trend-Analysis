# Coding tasks for the agent (Sonnet)

Context and the reasons behind each task: `PLAN_PHASE6_COMPLETION.md`. This file is the instructions only.
Do the tasks **in order**. After each task, run the self-check, commit, push, and report the results in one or two lines.

## Hard rules (read before touching anything)

1. **A long background job is running.** `run_backlog.py` (in the scratchpad) is checking the client's listings on Vinted, one pass at a time, and edits `client_data/*.csv` in place. Each pass re-imports `track_sales.py` / `product_groups.py` / `verify_backlog.py`, so your changes go live on the next pass.
   - **Never** stop it, restart Chrome, close Chrome, or run `run_tracker.bat`.
   - **Never** browse or request vinted.fr from this PC. It shares the IP and triggers rate limits.
   - **Never** write to `client_data/`. To read the files, copy them to the scratchpad first.
   - **Never** break an import. Every edit must leave `python track_sales.py --selftest` passing, because a syntax error would crash the running job's next pass.
2. Commit as the repo user, with **no `Co-Authored-By` line**. Stage only the files named in the task. Never stage `*.csv`, `client_data/`, `model_checks_log.csv`, `*.zip`, `graphify-out/`, `delivery_*`.
3. Keep the existing style: small diffs, comments only where the reason isn't obvious. Every new piece of logic gets asserts in `track_sales._demo()`.
4. Self-check command: `python track_sales.py --selftest`. The last line must start `track_sales self-check OK`.
5. Line endings: `.py` files are CRLF in the working copy, and `.bat` files **must** stay CRLF. When editing with Python, read and write `.py` with the default newline handling. For `.bat`, read and write with `newline=""` and insert `\r\n`.
6. If an instruction doesn't match the code (an anchor isn't found, a test can't pass), **stop and report**. Don't improvise around it.

---

## Task 1: store the full category path (URGENT, must be committed before ~19:45 on 2026-10-20)

**Why:** the backlog job reaches `women.csv` around 19:45. Reports need to filter by parent category ("Chaussures de sport"), but only the deepest crumb is stored today.

**`track_sales.py`:**
1. `TRACK_FIELDS` (~line 41): after the `"category",` entry, add
   `"category_path",   # full Vinted breadcrumb, "Femmes > Chaussures > Chaussures de sport > Chaussures de course"`
2. `_details_from_attrs` (~line 1441): the `crumbs` list already has the `<brand> …` crumb removed. Add `"category_path": " > ".join(crumbs) if len(crumbs) > 1 else ""` to `d`. Empty values are already filtered out by the final dict comprehension.
3. `verify_disappearances` (~line 1560), in the block that applies `details` to the row: change `for k in ("brand", "size", "color", "category"):` to also include `"category_path"`.
4. `_demo()`: the assert `assert d == {"brand": "Nike", ... "category": "Chaussures de course", ...}` must now also expect
   `"category_path": "Femmes > Chaussures > Chaussures de sport > Chaussures de course"`.
   The `Bougies` assert expects `"category_path": "Maison > Décoration > Bougies"`.
   In the fake-worker test, also assert `_trk["n"]["category_path"]` is set.

**Verify:** self-check OK.
**Commit:** `Store Vinted's full category path from the item page`, staging `track_sales.py`. Push.

---

## Task 2: Smeg/Stanley check uses the page colour and category

**`check_model_sales.py`, `check()`:** today it uses the stored `variant` column first (built from the title by old code), so a colour read from the page is ignored. Change it so that when `r.get("category")` is non-empty, the variant is always rebuilt with
`ts.build_variant(r["title"], r.get("brand", ""), r.get("color", ""), r.get("size", ""))`.
Otherwise keep the current behaviour.

**Test:** add to `_demo()` in `track_sales.py`, or as a `__main__` assert block in `check_model_sales.py` if that's simpler. A row with `variant="0.5l"`, `color="Crème"`, `category="Gourdes"` and title `"Gourde Smeg 500 ml"` must match `color="cream", cap="0.5l"`.

**Verify:** self-check OK. Then run on **scratchpad copies** (`cp client_data/smeg_tumbler.csv <scratch>/`):
`python check_model_sales.py --files <scratch>/smeg_tumbler.csv --brand smeg --color cream --cap 0.5l --hours 720`
It should run without error. **Delete `model_checks_log.csv` afterwards**; it's test output.

**Commit:** `check_models: use page colour when the row was checked on Vinted`. Push.

---

## Task 3: per-category product identity (`product_groups.py`)

**Why:** the client chose Women > Facial care, Women > Athletic shoes, Home > Kitchenware and Home > Pans. The current `identified_key` requires colour + size, which rejects all skincare (no colour) and merges every Nike running shoe into one product (no model).

**1. Add a helper `_family(row)`** returning one of `"shoes"`, `"face"`, `"pan"`, `"kitchen"`, `"other"`:
- First look at the lower-cased `category_path` (falling back to `category`):
  - contains "chaussure" or "basket" or "sneaker" → `shoes`
  - contains "visage" or "soin" → `face`
  - contains "poêle" or "poele" or "casserole" or "sauteuse" or "wok" → `pan`
  - contains "cuisine" → `kitchen`
- With no category, use the product type from `product_key`: `shoes` → shoes; `pan` → pan; else `other`.
- Add a `_TYPES` entry `"pan": ("poêle", "poele", "casserole", "sauteuse", "wok", "pan", "padella", "sartén", "sarten", "pfanne", "koekenpan")`.

**2. Add `_model_of(row)`:** the first **two** title tokens left after removing:
- brand tokens
- `_STOP`
- colour words (`_colour_of`)
- `_SIZE_RE` matches
- any word with a digit
- type words (`_type_of(w)`)
- words of 1 character

Keep title order and return them joined by a space, or `""` if none.
Normalise the title the same way `product_key` does (lower case, `t-shirt` → `tshirt`).

**3. Add `_volume_ml(title)`:** `fr.title_capacity_ml(title)` already exists in `fetch_results.py`. Return the rounded int, or 0.
**Add `_diameter_cm(title)`:** regex `(?<!\d)(1\d|2\d|3\d|40)\s?cm\b` (10–40 cm), returning `"28cm"` style, or `""`.

**4. Rewrite `identified_key(row)`:**

| family | key | requires |
|---|---|---|
| shoes | `(brand, "shoes", model)` | model, colour and size (`_variant_parts` both non-empty) |
| face | `(brand, type, model)` | model and volume > 0 |
| pan | `(brand, "pan")` | diameter |
| kitchen | `(brand, type)` | nothing extra |
| other | `product_key(row)` | colour and size, as today |

`brand`/`type` come from `product_key(row)`. If `product_key` is None, return None.

**5. Update `variant_of(row)`** to match the family:
- shoes: `"<colour> <size>"`
- face: `"<ml>ml"`
- pan: the diameter
- kitchen and other: as today

**6. `rank_products`** unpacks `for (brand, typ), g in groups.items()`. Keys can now have 3 parts, so change it to unpack `k` and build `"product": " ".join(k)`, `"brand": k[0]`, `"type": " ".join(k[1:])`.

**7. Update the module docstring** rule paragraph to describe the per-family rule in 4–6 lines.

**8. Tests in `_demo()`** (adjust the exact expected model strings if your token rules give a sensible variant, but **assert them**):
- `{"brand": "Nike", "title": "Nike Pegasus 40 noir", "size": "38", "color": "Noir", "category_path": "Femmes > Chaussures > Chaussures de sport > Chaussures de course"}` → `("nike", "shoes", "pegasus")`, variant `"black 38"`
- the same with title `"Nike running noir"` and no model word → `None`
- `{"brand": "Caudalie", "title": "Caudalie Vinoperfect sérum éclat 30 ml", "category_path": "Femmes > Beauté > Soins du visage > Sérums"}` → a 3-part key starting `("caudalie",`; variant `"30ml"`
- the same with no volume in the title → `None`
- `{"brand": "Tefal", "title": "Poêle Tefal Ingenio 28 cm", "category_path": "Maison > Cuisine > Poêles"}` → `("tefal", "pan")`, variant `"28cm"`
- `{"brand": "Le Creuset", "title": "Cocotte Le Creuset", "category_path": "Maison > Cuisine > Ustensiles de cuisine"}` → a 2-part key, identified
- **every existing `identified_key` / `rank_products` assert must still pass**; the Zara jacket with colour + size is family `other`

**Verify:** self-check OK.
**Commit:** `Per-category product identity: shoe model, skincare product + ml, pan diameter`. Push.

---

## Task 4: category filter for reports (`product_groups.py`)

- `main()`: add `ap.add_argument("--category", action="append", default=[], help="keep rows whose category path contains this text (repeatable, OR)")`.
- After loading `rows`, if `a.category` is given, keep a row only when any lower-cased filter text is in `(r.get("category_path") or r.get("category") or "").lower()`. Print how many rows were kept.
- It applies to both `--by brand` and `--by product`.

**Verify:** on a scratchpad copy of `client_data/smeg_tumbler.csv`:
`python product_groups.py <copy> --by brand --page-brands-only --category gourde --min-sold 1`
It runs, keeps fewer rows, and shows Smeg.
**Commit:** `product_groups: --category filter`. Push.

---

## Task 5: coverage check for category sweeps (`track_sales.py` `main`)

- Find the line `current_ids = {str(i.get("id")) for i in raw if i.get("id")}` (~line 1859).
- Right after the corruption-guard `if … sys.exit(3)` block, and only when `seedless and not first_run`:
  - `prev_ids = {i for i, r in tracking.items() if r.get("last_seen") == _last_run}`
  - `overlap = len(current_ids & prev_ids)`
  - print `f"   coverage: complete — {overlap} listings overlap the previous run"` if `overlap > 0`, else
    `"   ⚠️  coverage: GAP — none of this run's listings were in the previous run; more new listings arrive than the ~960 window. Run more often or choose a narrower category."`
- Add a comment containing `ponytail:`: the overlap is measured across all country sites together, so a gap in one site can be hidden by overlap in another; measure per site if needed.
- **Test:** this sits inside `main()`, which is hard to test, so put the decision in a tiny helper `_coverage_line(current_ids, prev_ids) -> str` and assert both branches in `_demo()`.

**Commit:** `Category sweeps report whether they saw every new listing`. Push.

---

## Task 6: offers re-check for category sweeps (`track_sales.py`)

**Why:** offers are read when a listing is first seen (always 0). The client wants to see listings that attract offers (e.g. 13+).

1. `enrich_publish_times(tracking, path, workers=5)` → add a parameter `todo=None`. If `todo` is None, keep `todo = _enrich_queue(...)` as today; otherwise use the given list.
2. In its result loop, change `if info.get("created_at_ts"):` so it only **fills** a missing publish time: `if info.get("created_at_ts") and not row.get("created_at_ts"):`. Keep the `captured += 1` inside.
3. New `_reoffer_queue(tracking, now, cap)`: active rows whose `first_seen` is between 24 h and 72 h before `now`, and whose `offers_seen_at` is empty or within 12 h of `first_seen` (that is, not yet re-read). Newest first, capped at `int(cap)` when given.
4. In `main()`, right after the existing `enrich_publish_times(tracking, path)` call (~line 1911), and only `if seedless`:
   `enrich_publish_times(tracking, path, todo=_reoffer_queue(tracking, now, os.environ.get("VINTED_MAX_REOFFER", "300")))`
   Wrap it in the same try/except pattern used around neighbouring optional steps, if there is one.
5. **Test** `_reoffer_queue` in `_demo()`:
   - a row first seen 48 h ago with no `offers_seen_at` is queued
   - one first seen 2 h ago is not
   - one first seen 48 h ago with `offers_seen_at` = first_seen + 30 h is not

**Commit:** `Category sweeps re-read offers on listings 1-3 days old`. Push.

---

## Task 7: STOP and report

Report: the commit hashes, the self-check output, and anything that didn't match these instructions.
**Do not** set up the client's categories, build a delivery zip or write client reports. Those need the category IDs (Vinted lookups wait until the backlog job finishes) and the user's decisions.
