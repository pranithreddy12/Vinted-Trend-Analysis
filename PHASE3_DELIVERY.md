# Phase 3 — Market Intelligence Engine — Delivery Summary

This document summarises what is delivered in Phase 3 and how to use it.
Phase 3 transforms the tool from a keyword scraper into a **market-intelligence
engine**: it measures demand, competition, saturation, and opportunity, tracks
them over time, raises alerts, and discovers related keywords/niches.

---

## Scope delivered (vs. the agreed proposal)

| Proposal item | Status | Where you see it |
|---|---|---|
| Competition analysis engine | ✅ Done | Competition tier 🟢/🟡/🟠/🔴 per keyword |
| Emerging niche detection | ✅ Done | Niche score + opportunity tier |
| Automatic trend detection | ✅ Done | `keyword_research.csv` + discovered-terms report |
| Historical snapshot tracking | ✅ Done | `snapshots/keyword_YYYY-MM-DD.csv` |
| Demand velocity / trend evolution | ✅ Done | "Evolution" line in the report (vs. previous day) |
| Saturation analysis | ✅ Done | Saturation flag + competition tier |
| Intelligent opportunity scoring | ✅ Done | 0–100 score per keyword |
| Alert system foundation | ✅ Done | Alerts report + `snapshots/alerts_*.csv` |
| Recent-first intelligence | ✅ Done | Demand measured on the fresh (7-day) cohort |
| Reliability + filtering | ✅ Done | JSON-LD fallback, recency filtering, exclusions |

---

## How to run

See `INSTRUCTIONS.md`. In short: quit Chrome → run `start_scraper.sh` (Mac) or
`start_scraper.bat` (Windows) → log in if asked → type your keyword(s).

---

## What it produces

| File | Purpose |
|---|---|
| `vinted_trends.csv` | Every listing analysed: offers, age, sold status, demand score + verdict |
| `vinted_summary.csv` | Per-keyword summary: listings, offers, price, saturation flag |
| `keyword_research.csv` | Demand-weighted related keywords and specific niches (Helium-10 style) |
| `snapshots/keyword_*.csv` | Daily market snapshot for trend tracking over time |
| `snapshots/alerts_*.csv` | Alerts triggered that run |
| `vinted_trends_raw.csv` | Raw item list captured before analysis |

---

## The intelligence, explained

**Demand scoring.** Each listing is scored on offer activity, sales velocity and
freshness, then labelled: `⚡ Fast Sale` → `🚀 Explosive Early Trend` → `🔥 Trending`
→ `📈 Growing` → `👀 Early Watchlist` → `📊 Monitoring` → `⚠️ Low`.

**Competition.** Each keyword is classified by active supply:
🟢 Low (<50) · 🟡 Healthy (50–200) · 🟠 Medium Saturation (200–500) · 🔴 Competitive (>500).

**Opportunity score (0–100).** Combines demand, sales velocity, competition,
freshness, saturation, historical evolution and rarity into one figure:
💥 Explosive Niche (80+) · 🔥 Strong Opportunity (60–80) · 👍 Interesting (30–60) · ⚠️ Weak (0–30).

**Recent-first intelligence.** Competition uses *all* active listings, but demand
signals (offers, likes) are measured only on **fresh listings (last 7 days)**, so
months-old dead inventory doesn't distort the picture.

**Historical tracking.** Every run saves a dated snapshot. From the second run of a
keyword onward, the report shows evolution vs. the previous snapshot:
📈 Demand Accelerating · 📉 Demand Decaying · ⚠️ Saturation Growing · ➡️ Stable.

**Alerts.** Generated automatically: ⚡ Fast Sale, 🔥🔥 Explosive Demand,
🚀 Emerging Niche, 📈 Trend Acceleration, ⚠️ Saturation.

**Keyword research.** From any seed keyword, the tool extracts the strongest
related terms and specific niches (model + colour + capacity), ranked by demand.
Filter `is_seed = no` in `keyword_research.csv` for opportunities beyond the seed.

---

## How to read the results (important)

A keyword scoring **⚠️ Weak** means the market is **mature/saturated**, *not* that
the product doesn't sell. This engine finds *emerging* opportunities — demand
that is rising and concentrated in fresh listings, in markets with room to compete.
Established best-sellers in crowded markets will read "Weak" by design.

For the sharpest results, use **specific** keywords (model + colour + size) rather
than broad category terms, which average out the signal.

---

## Not included in Phase 3

Product-level turnover tracking (time-to-sell), product-variant intelligence and
AI image recognition are part of the **next phase** and are not included here.
