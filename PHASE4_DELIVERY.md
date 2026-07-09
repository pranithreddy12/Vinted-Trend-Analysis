# Phase 4 — Variant & Turnover Intelligence — Delivery Summary

This document summarises what is delivered in Phase 4 and how to use it.
Phase 4 adds **product-variant-level sales intelligence** on top of Phase 3: instead of a
keyword-level opportunity score, you get concrete numbers — estimated sales, time-to-sell,
competition, price and trend — for each **exact sellable variant** (e.g. "Stanley Quencher
Pink 40oz").

---

## Scope delivered (vs. the agreed spec)

| Your requirement | Status | Where you see it |
|---|---|---|
| Estimated sales / 30 days, per exact variant | ✅ Done | `est_sales_30d` |
| Sales velocity (average days to sell) | ✅ Done | `median_days_to_sell` |
| Competition (Low/Medium/High + count) | ✅ Done | `competition_level` |
| Market trend (Increasing/Stable/Decreasing) | ✅ Done | `trend` (from run-to-run snapshots) |
| Average market price | ✅ Done | `avg_price` |
| Data transparency (what's measured, how, confidence) | ✅ Done | Console footer + `confidence` |
| No abstract scoring as the focus | ✅ Done | Concrete metrics lead; the 0–100 score is a secondary column |
| Ranking that favours real demand over low competition | ✅ Done | Reweighted after your feedback — see below |
| Buyer offers as an early-demand signal | ✅ Done | `offers` column + factored into the score — see below |
| Tracking across your full shipping zone, not just France | ✅ Done | Cross-border multi-country tracking — see below |

Also included, based on your feedback during this phase: the **demand-first ranking**
reweight, the **buyer-offers signal**, and the **summary card format** you sketched.

---

## How to run

**One-time setup** (if you haven't already): `sh setup.sh`

**Manual check on a single product:**
1. Quit Chrome completely, then run `sh start_scraper.sh` and log into Vinted if asked.
   Leave that Chrome window open.
2. In a new Terminal tab, in the same folder, run:
   ```
   python3 track_sales.py "stanley quencher"
   ```
3. First run establishes a baseline (no sales data yet — that's expected). Run it again
   after listings have had time to sell (a day or more) to start seeing real numbers, and
   keep running it periodically — the estimates get more accurate the longer it tracks.

**Automated / continuous tracking** (recommended — this is what makes the numbers
trustworthy): see `AUTOMATION.md`. In short, add your products to `tracked_keywords.txt`
and schedule `run_tracker.sh` every few hours; it runs unattended.

---

## What it produces

| File | Purpose |
|---|---|
| `variant_report_<product>.csv` | The main deliverable — one row per exact variant, with all the metrics below |
| `tracking/<product>.csv` | Raw per-listing tracking state (internal — what the report is built from) |
| `tracking/variants_YYYY-MM-DD.csv` | Daily per-variant snapshot (enables the trend column) |

---

## The summary card

Each run prints your top opportunity as a card, e.g.:

```
Stanley Quencher 40oz Pink

🔥 Estimated Sales: 38.9/month
⚡ Average Time to Sell: 15.4 days
📈 Buyer Demand (offers): 12
🏷️ Average Selling Price: €44.4
👥 Active Listings: 18
📊 Sales Trend: Stable
🏆 Competition: Medium
🎯 Opportunity: Excellent
```

The full variant table (all tracked variants, not just the top one) is in the console
output and in `variant_report_<product>.csv`.

---

## How to read the numbers (important)

**Ranking is demand-first.** After your feedback, the opportunity score weighs the four
signals in this priority order:

1. **Proven monthly sales volume** (highest) — actual completed sales.
2. **Sales velocity** — how fast it sells (under ~3 days is excellent).
3. **Buyer demand (offers)** — early demand; buyers making offers precede completed sales.
4. **Competition** (lowest) — a tie-breaker between similarly-selling variants, no longer
   able to override real demand.

A variant that sells 40 times a month will generally outrank one that sells 5 times a
month, even if the 5-a-month variant has less competition.

**Buyer offers are your early-warning signal.** The `offers` column shows the total live
offers buyers have made across that variant's active listings. Offers tend to appear
*before* sales show up in the numbers, so a fresh product that's heating up will register
offers first — giving you a head start before its estimated-sales figure catches up. This
is captured from the listing pages as the tool tracks them, so the coverage builds up over
the first several runs; a `—` means offers haven't been read for that variant yet (its
score is then based on the other three signals, not penalised). Offers keep doing their
original job in the keyword/search analysis too — this just brings the same signal into the
variant view.

> If you want to compare the tables with and without the offers column (purely a display
> preference), set `VINTED_SHOW_OFFERS=0` to hide it. The score always uses offers either
> way — the toggle only changes what's shown.

**Estimated sales is marketplace-wide, not a per-seller forecast.** "38.9/month" is the
total across all sellers of that exact variant, measured from listings disappearing from
the catalog (sold or removed). It is not a prediction of what *you specifically* would
sell — a strong seller like you can beat the market average. A rough way to read your own
odds: sales/month vs. active listings tells you how many times the standing supply turns
over each month.

**Velocity is a market median, not your personal speed.** "15.4 days" is the median
time-to-sell across all sellers of that variant. If you consistently sell faster than
that, it means you're outperforming the average listing, not that the number is wrong.

**Confidence** (High/Medium/Low) reflects sample size and how long the variant has been
tracked. It only reaches High after roughly a month of continuous tracking with enough
completed sales — trust Low/Medium numbers as directional, not final, until then.

**Trend** compares this run to the previous one; it shows "Building" until a second run
exists.

---

## Cross-border tracking (new)

Your Vinted account ships to and is visible from 10 countries — France, Belgium,
Luxembourg, Netherlands, Germany, Austria, Spain, Portugal, Italy, Ireland. Tracking now
queries all 10 by default and merges them (the same listing is recognised as one listing
across every country it's visible from, so nothing is double-counted).

Previously tracking only queried France, which was **missing roughly 45% of the listings
actually visible to your buyers** (measured on "stanley quencher": 287 listings on France
alone vs. 522 once the other 9 countries were included). This is why earlier estimates
looked a little conservative — expect the numbers to reflect the fuller market from now on.

If you ever want to track a narrower set of countries, set `VINTED_DOMAINS` (see
`run_tracker.sh`/`run_tracker.bat` — it's a comma-separated list of country codes,
currently `fr,be,lu,nl,de,at,es,pt,it,ie`).

---

## Not included in Phase 4

**Image-based variant recognition** (grouping listings by product photo when the title is
vague or in another language) and **autonomous/seedless discovery** (surfacing products you
never searched for) are separate phases, priced and scoped independently. Happy to talk
through either whenever you're ready.
