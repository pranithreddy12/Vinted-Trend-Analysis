# Plan: Phase 6 dispute fix — automatic product discovery

**For the executing agent.** Do Tasks 1–4 in order. Tasks 5–6 are for the human; do not do them.
Do not touch any scraping, Chrome, CDP or fetch code. Keep diffs small and match the surrounding style.

## Background (read once)

The client (Vinted resale tool, Phase 6) is disputing delivery. He wants, among other things,
**5 specific products discovered automatically (never searched by him)**, each with: exact model
+ variants, source listing links, confirmed 30-day sales, median sold price, median time to sell,
competition, and a method that never mixes different products.

`product_groups.py` (commit `5d92eed`) already groups tracked listings by brand + type and ranks
them by confirmed sales. Two problems:

1. **Grouping is far too fine.** The key is brand + the first 3 leftover title words, so almost
   every listing becomes its own "product" (local `tracking/home.csv`: 5,218 single-listing
   groups out of 7,360 rows). Nothing ever reaches `--min-sold 3`. Placeholder brands like
   "Inconnu" are also treated as real brands.
2. **It never runs automatically.** It is a manual CLI only, so a normal tracker run produces no
   discovery report and the client never sees the output.

Data files in `tracking/` are from September and pre-date the brand fix. **Do not expect real
products in the output locally; "0 products" is a normal result on local data.** Correctness is
proven by the self-checks, not by the local output.

---

## Task 1 — Coarser, multilingual product key (`product_groups.py`)

### 1a. Placeholder brands → unidentified
Replace the inline tuple in `product_key`:
```python
if not brand or brand in ("pas de marque", "no brand", "sans marque"):
```
with a module constant and a membership test:
```python
_NO_BRAND = {"pas de marque", "no brand", "sans marque", "inconnu", "inconnue", "unknown",
             "autre", "other", "sin marca", "senza marca", "geen merk", "keine marke",
             "fait main", "handmade", "hecho a mano", "fatto a mano"}
```
Leave real retail brands (IKEA, Action, Disney, Amazon…) alone.

### 1b. Type synonym map
Add a dict `_TYPES` mapping **canonical English type → tuple of title words** in FR / EN / IT /
ES / NL / DE. Build a reverse lookup `_TYPE_OF = {word: canonical}` once at import. Cover at least:

| canonical | words (minimum) |
|---|---|
| jacket | veste, blouson, jacket, giacca, chaqueta, jack, jacke, doudoune, puffer |
| coat | manteau, coat, cappotto, abrigo, mantel, trench |
| dress | robe, dress, vestito, abito, vestido, jurk, kleid |
| tshirt | tshirt, tee, maglietta, camiseta |
| shirt | chemise, chemisier, blouse, camicia, camisa, hemd, overhemd |
| top | top, débardeur, debardeur, crop, canotta, bustier |
| sweater | pull, pullover, sweat, sweatshirt, hoodie, maglione, felpa, jersey, sudadera, trui, cardigan, gilet |
| jeans | jean, jeans, vaquero, vaqueros, denim |
| trousers | pantalon, trousers, pants, pantaloni, pantalón, broek, hose, legging, leggings |
| skirt | jupe, skirt, gonna, falda, rok, rock |
| shorts | short, shorts, bermuda |
| shoes | chaussures, baskets, sneakers, scarpe, zapatillas, zapatos, schoenen, schuhe, escarpins, mocassins |
| boots | bottes, bottines, boots, stivali, botas, laarzen, stiefel |
| sandals | sandales, sandals, sandali, sandalias |
| bag | sac, bag, borsa, bolso, tas, tasche, pochette, handbag, cabas |
| bottle | gourde, bouteille, bottle, borraccia, botella, fles, flasche, tumbler |
| mug | tasse, mug, taza, beker, becher, gobelet |
| plush | peluche, doudou, plush, knuffel, kuscheltier, peluche |
| candle | bougie, candle, candela, vela, kaars, kerze |
| vase | vase, vaso, jarrón, jarron |
| lamp | lampe, lamp, lampada, lámpara, lampara |
| cushion | coussin, cushion, cuscino, cojín, cojin, kussen, kissen |
| frame | cadre, frame, cornice, marco, lijst, rahmen |

**T-shirts:** `_tokens` splits on non-letters, so "T-shirt" would become `t` + `shirt` and be
mis-typed as `shirt`. In `product_key`, normalise the title before tokenising:
`title = (row.get("title") or "").lower().replace("t-shirt", "tshirt").replace("t shirt", "tshirt")`.

### 1c. New `product_key`
After the existing token filtering (brand words, `_STOP`, colours, sizes, `len(w) > 1`):
```python
typ = next((_TYPE_OF[w] for w in toks if w in _TYPE_OF), None)
return (brand, typ or toks[0]) if toks else None
```
i.e. the product is **brand + first recognised type word (canonical)**, falling back to the first
leftover word when no type word is recognised. Colour/size stay variants (`variant_of` unchanged).

Update the module docstring's rule paragraph to describe this (brand + canonical type; the
fallback; placeholder brands are unidentified). Keep the "Known limits" paragraph, but change it to:
"two different models of the same brand + type merge (e.g. two Zara jackets); image
recognition (Phase 5) is the later fix."

`verification_order` uses `product_key` and needs no change.

### 1d. Self-checks (`track_sales.py` `_demo()`, around line 1981)
Replace the existing assert
```python
assert pg.product_key({"brand": "Zara", "title": "Veste en jean blanche Zara zippée taille 38"}) == ("zara", "veste jean zippée"), ...
```
with these (keep the two existing `is None` asserts):
```python
assert pg.product_key({"brand": "Zara", "title": "Veste en jean blanche Zara zippée taille 38"}) == ("zara", "jacket")
assert pg.product_key({"brand": "Zara", "title": "Giacca jeans Zara bianca M"}) == ("zara", "jacket")   # IT == FR
assert pg.product_key({"brand": "Zara", "title": "T-shirt Zara noir S"}) == ("zara", "tshirt")
assert pg.product_key({"brand": "Inconnu", "title": "Veste en jean"}) is None
assert pg.product_key({"brand": "Smeg", "title": "Gourde isotherme Smeg 500 ml crème"}) == ("smeg", "bottle")
assert pg.product_key({"brand": "Nao", "title": "Figura porcelana Nao"}) == ("nao", "figura")      # fallback word
```
Note `"jeans"` in the Giacca title is also a type word; the key must take the **first** type word
in title order (`giacca` → jacket), which the `next(...)` above does.

**Verify:** `python track_sales.py --selftest` prints `track_sales self-check OK`.
Then on stale local data (just a sanity number, not a pass/fail):
```
python product_groups.py tracking/home.csv --days 400 --min-sold 1 --top 5
```
The "N products with a brand + type" figure must be **much lower than 5,371** (expect well under
2,000). Report the before/after numbers.

---

## Task 2 — Add the client's own metrics and alert rule to each product (`product_groups.py`)

In `rank_products`, for each product dict also add (reuse existing helpers in `track_sales`, do
not re-implement them):
```python
median_days = (statistics.median(life) / 24) if life else None
opp = ts.compute_variant_opportunity(len(sold) * 30 / days, median_days)
comp = ts.competition_label(active)          # compute `active` once and reuse it
...
"median_days_to_sell": round(median_days, 1) if median_days is not None else "",
"competition_level": comp,
"score": opp["score"],
"verdict": opp["verdict"],
"passes_alert_rule": len(sold) * 30 / days >= 30 and comp != "High" and opp["score"] >= 70,
```
Put these right after `active_listings` so the CSV columns read in a sensible order. Keep sorting
by confirmed sales (do **not** sort by score).

In `main()`'s printed block per product, add competition level, score and `ALERT` when
`passes_alert_rule` is true. Print one summary line at the end:
`"{k} of {n} products pass the alert rule (>=30 confirmed sales/30d, competition not High, score >=70)"`.

Add one assert to `_demo()` that builds 30 fake sold rows for one product (brand "Zara",
title "Veste Zara noire", status "sold", price "20", last_seen within the window,
`created_at_ts` 24 h before last_seen) plus 3 active rows, calls `pg.rank_products(rows)`, and
checks the product is `("zara jacket")`, `confirmed_sold_30d == 30`, `competition_level == "Low"`
and `passes_alert_rule is True`. Look at how `_demo()` already builds rows with `t_pub` and
`last_seen` strings, and copy that pattern.

**Verify:** `python track_sales.py --selftest` passes.

---

## Task 3 — Run discovery automatically after every tracker run

### 3a. `product_groups.py`: default to the category sweep files
Make `files` optional (`nargs="*"`). When none are given, read `tracked_keywords.txt`, take every
non-comment line starting with `cat:` (format `cat:<id> <label>`, e.g. `cat:1918 home`), and use
`os.path.join(ts.TRACK_DIR, ts._slug(label) + ".csv")` for each label whose file exists. If no
file is found, print a one-line message and exit 0 (it must never fail the batch run).

### 3b. `run_tracker.bat`
After the main `for` loop closes (current line 158 `)`), and **before** `endlocal`, add:
```bat
echo [!date! !time!] START product discovery >> "%LOGFILE%"
%PYTHON% product_groups.py --top 5 --out discovered_products.csv >> "%LOGFILE%" 2>&1
echo [!date! !time!] END   product discovery - exit code !errorlevel! >> "%LOGFILE%"
```
Do not change anything else in the .bat file. (It already has an uncommitted change — the
exit-code-4 Chrome restart/retry blocks — keep it, it ships in the same commit.)

**Verify:** `python product_groups.py --top 5` runs from the repo root, finds `tracking/home.csv`
and `tracking/women.csv`, and exits 0.

---

## Task 4 — Commit and push

- Stage only: `product_groups.py`, `track_sales.py`, `run_tracker.bat`, `PLAN_DISPUTE_FIX.md`.
- **Never stage** any `.csv`, `.zip`, `delivery_phase6/`, `graphify-out/`, `model_checks_log.csv`
  or `*_wait.txt`.
- Commit as the existing git user (pranithreddy12). **No `Co-Authored-By` line** — the user asked
  for commits under their name only.
- Message: `Group discovered products by brand + canonical type; run discovery after every tracker run`
  with a 2–3 line body (coarser multilingual key, placeholder brands unidentified, alert-rule
  columns, auto-run in run_tracker.bat, Chrome restart on exit 4).
- `git push` on branch `phase-5-image-recognition`.

Report back: self-check output, before/after product counts from Task 1, commit hash.

---

## Task 5 — Real numbers (HUMAN, not the agent)

Local data cannot prove anything. Ask the client to:
1. Pull the new code and let one full tracker run finish (~23 h).
2. Send `discovered_products.csv`, the run log, `tracking/women.csv`, `tracking/home.csv`,
   `tracking/smeg_tumbler.csv`, `tracking/stanley_quencher.csv`, and `model_checks_log.csv`
   (from `check_models.bat`).

Then rerun `product_groups.py` on his files and send him the top 5 with links, plus the
corrected Smeg cream 500 ml and Stanley Quencher H2.0 pink 1.18 L figures.
Fallback if he will not send files: run a Home sweep locally for ~48 h (a sale is only confirmed
after a listing disappears and is then checked as sold).

## Task 6 — Reply to client (HUMAN)

Send the updated reply: what is fixed (Smeg mixing, brand from Vinted's label, automatic
discovery report each run), what is left (image-based model separation), the strict rule used
("unidentified" listings are never counted; every figure has listing links). Do not promise
"guaranteed" exact identification. Ask the platform for a delivery extension.

## Out of scope
Image recognition for grouping; any change to scraping, scoring formulas, or alert delivery.
