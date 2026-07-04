# Phase 4.5 — Continuous Automation Guide

This runs the sales tracker **automatically every few hours** so estimated sales,
velocity and trend become accurate over time (and confidence rises to High after
~a month). It tracks every product listed in `tracked_keywords.txt`.

## How it works

- **Chrome stays running** with the debugging port + your logged-in Vinted profile.
- A scheduler (cron / Task Scheduler) runs **`run_tracker`** every few hours.
- Each run: for every keyword in `tracked_keywords.txt`, it updates the tracking
  data, captures new listings' publish times, detects sales, and rewrites
  `variant_report_<product>.csv`.
- It runs in **gentle mode** (2 tabs) — steady-state has only a few new listings per
  cycle, so low concurrency avoids Vinted's rate limiting.

## One-time setup

1. **Pick the machine.** Use an **always-on computer on a home/residential
   internet connection** (a spare PC, mini-PC, or the client's Mac left on).
   > Why not a cloud/VPS: a datacenter IP gets throttled by Vinted far harder.
   > A residential IP is the single biggest reliability factor. (A cloud server is
   > possible later but likely needs a paid residential proxy.)

2. **Install** Python 3 + Chrome, then in this folder run:
   - Mac: `sh setup.sh`  · Windows: `pip install -r requirements.txt` then
     `python -m playwright install chromium`

3. **Choose the products** to monitor — edit `tracked_keywords.txt` (one per line).

4. **Start Chrome (once) and log in.** Launch Chrome with the debugging profile and
   log into Vinted, then **leave it running**:
   - Mac: `sh start_scraper.sh` (log in, then you can leave it — you don't need to
     finish the manual run)
   - Windows: run `start_scraper.bat` (log in, leave Chrome open)

## Schedule it

Run `run_tracker` every 4–6 hours.

**Mac / Linux (cron):** run `crontab -e` and add (adjust the path):
```
0 */4 * * * /full/path/to/Vinted/run_tracker.sh >> /full/path/to/Vinted/automation.log 2>&1
```

**Windows (Task Scheduler):** create a Basic Task → Trigger: Daily, repeat every
4 hours → Action: Start a program → point it at `run_tracker.bat`.

## Keeping it healthy

- **Keep the Chrome window OPEN — do not close it.** `run_tracker` connects to the
  already-running Chrome; it deliberately does **not** relaunch it, because a
  relaunched Chrome starts logged out (a known Vinted/Chrome session quirk). If
  Chrome is closed (or the machine reboots), `run_tracker` stops with a clear
  message; just run `start_scraper` again, log in, and leave it open.
- **Login expires periodically** (roughly weekly). When it does, a run fetches 0
  listings — the safety guard skips it and preserves the data. Re-open Chrome / log
  into Vinted again, and the next run resumes normally.
- **Check `automation.log`** occasionally to confirm runs are completing and to spot
  repeated "Not logged in" messages (time to re-login).
- Output to watch: `variant_report_<product>.csv` — refreshed every run, accuracy
  improving over the weeks.

## Costs

- **Machine:** free if you use a spare always-on computer; ~$5–15/month for a small
  cloud server (only if you go the VPS route — then also budget a residential proxy).
- **Upkeep:** occasional re-login (a few minutes, ~weekly).

## Scope note

This is the **focused, private** always-on version for your own products. The full
24/7-for-all-subscribers pipeline (feeding a public dashboard) is the Phase 8 SaaS
platform.
