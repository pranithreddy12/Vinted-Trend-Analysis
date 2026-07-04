# Vinted Market Intelligence — User Guide

This guide explains how to install and run the Vinted Market Intelligence tool.
No technical experience required.

## Prerequisites

You only need **Python 3** and **Google Chrome** installed.
- Python 3: https://www.python.org/downloads/

## Setup (first time only)

**Mac**
1. Open the **Terminal** app.
2. Type `cd ` (with a space), drag the `Vinted` folder into the window, press **Enter**.
3. Type `sh setup.sh` and press **Enter**. Wait until it says *"Setup Complete!"*.

**Windows**
1. Open **PowerShell** in the `Vinted` folder.
2. Run `pip install -r requirements.txt`
3. Run `python -m playwright install chromium`

## How to run

⚠️ **Quit Google Chrome completely before starting** (Mac: right-click the Dock icon → Quit, or `Cmd+Q`. Windows: close all Chrome windows). The tool launches its own Chrome; if Chrome is already open it will not connect.

1. **Mac:** in Terminal, `cd` into the folder and run `sh start_scraper.sh`.
   **Windows:** double-click `start_scraper.bat`.
   > *Windows note:* open `start_scraper.bat` once in Notepad and make sure the `--user-data-dir` path points to wherever you placed the `Vinted` folder.
2. A Chrome window opens on Vinted. **If it asks you to log in**, log into your Vinted account (solve any captcha), then return to the terminal and press a key.
3. At **`Enter Keyword:`**, type one or more keywords separated by commas, e.g.:
   ```
   stanley quencher rose, crocs platform
   ```
4. The tool runs automatically, showing live progress and a full intelligence report at the end.

## What you get (output files)

All files are created in the `Vinted` folder. You can open the CSVs in Excel or Google Sheets.

| File | What it contains |
|------|------------------|
| **`vinted_trends.csv`** | The main result — every listing analysed, with offers, age, sold status, demand **score** and **verdict**. Updates live, item by item, as it runs. |
| **`vinted_summary.csv`** | One row per keyword: total listings, average offers, median offers, average price, top verdict, and a saturation flag. |
| **`keyword_research.csv`** | Ranked list of related keywords and specific niches discovered from the listings (demand-weighted). Filter `is_seed = no` to see *new* opportunities beyond what you typed. |
| **`vinted_trends_raw.csv`** | The raw list of items pulled before analysis (created instantly at the start). |
| **`snapshots/keyword_YYYY-MM-DD.csv`** | A daily snapshot saved every run, so the tool can track trends, growth and saturation over time. |
| **`snapshots/alerts_YYYY-MM-DD.csv`** | Any alerts triggered that run (explosive demand, fast sales, emerging niche, saturation). |
| **`vinted_scraper.log`** | A technical log of the last run, useful if something goes wrong. |

## How to read the verdicts

**Per-listing demand verdict** (in `vinted_trends.csv`):
`⚡ Fast Sale` · `🚀 Explosive Early Trend` · `🔥 Trending` · `📈 Growing` · `👀 Early Watchlist` · `📊 Monitoring` · `⚠️ Low`

**Per-keyword opportunity** (shown in the end-of-run report, 0–100):
`💥 Explosive Niche (80+)` · `🔥 Strong Opportunity (60–80)` · `👍 Interesting (30–60)` · `⚠️ Weak (0–30)`

**Competition level** (by number of active listings):
`🟢 Low (<50)` · `🟡 Healthy (50–200)` · `🟠 Medium Saturation (200–500)` · `🔴 Competitive (>500)`

> Note: a "Weak" opportunity means the market is mature/saturated, **not** that the product doesn't sell. The tool is designed to spot *emerging* opportunities and demand concentrated in *fresh* listings.

## Tips

- Use **specific** keywords (model + colour + size, e.g. `stanley quencher rose 1.18l`) for the sharpest signal. Broad terms get diluted.
- Run the same keyword on **different days** — from the second run onward, the report shows trend evolution (accelerating / decaying / saturating) versus the previous snapshot.
