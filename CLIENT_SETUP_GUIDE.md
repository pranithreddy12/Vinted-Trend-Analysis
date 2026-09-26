# Complete Setup & Verification Guide

This is the one guide to follow start to finish on the dedicated machine. It replaces piecing
together instructions from separate messages — if anything here conflicts with something said
earlier, **this file is the current, correct version.**

Do this on the actual dedicated machine that will run the tracker continuously, not a laptop
that gets closed or put to sleep.

---

## 0. Before you start — one rule that matters more than any single step

**Every time you get an updated package, delete the old folder completely and extract the new
one fresh.** Do not unzip on top of the old folder — some file managers skip files that already
exist instead of overwriting them, which has caused several rounds of "the fix doesn't seem to
be working" that turned out to be old files still sitting there. If in doubt, extract to a new
folder and copy your `tracked_keywords.txt` and `vinted_profile` folder (your login) across from
the old one — see Step 6.

---

## 1. Install Python and Chrome (skip if already done)

- **Python**: download from python.org. During install, **tick "Add python.exe to PATH"** —
  this is the single most common setup mistake. Verify afterward by opening a **new** Command
  Prompt and typing `python --version`.
- **Chrome**: the normal desktop browser, if not already installed.

## 2. Extract the package and run setup

1. Extract the zip to a folder you'll keep long-term (e.g. `C:\VintedTracker`).
2. Open Command Prompt, navigate into the `src` folder inside it, and run:
   ```
   setup.bat
   ```
3. Let it finish. It installs everything in one pass: the core Python packages, the Chrome
   driver Playwright needs, and the optional vision-AI packages. If the vision-AI step fails,
   it's not fatal — tracking still works, only AI product identification is affected — but send
   us the error message shown.

## 3. Log into Vinted (once)

1. Run `start_scraper.bat`.
2. A Chrome window opens on vinted.fr. Log into your Vinted account in that window.
3. **Leave that Chrome window open.** Don't close it — the tracker reconnects to this exact
   window every time it runs.

## 4. Check your product list

Open `tracked_keywords.txt` in Notepad. This is the list of products and categories being
tracked. A few things to check:
- Each product should appear **once**. If you add something, check it's not already there under
  slightly different wording (e.g. "Stanley Quencher" vs "stanley quencher" — these count as the
  same product and the tracker will skip the duplicate now, but it's cleaner to just not add it
  twice).
- Category sweeps look like `cat:1918 home` — these scan a whole category with no specific
  product name.

## 5. Set up Windows Task Scheduler (makes it actually automatic)

1. Open **Task Scheduler** (Windows key → search for it).
2. **Create Task** (not "Create Basic Task" — the extra options matter here).
3. **General tab**: give it a name (e.g. "Vinted Tracker"). Under Security options, select
   **"Run only when user is logged on."**
4. **Triggers tab**: New → Daily → Repeat task every → **4 hours**, for a duration of
   **Indefinitely**.
5. **Actions tab**: New → Start a program → Browse to `run_tracker.bat` in your `src` folder.
6. **Conditions tab** — this one matters if this is a laptop: **untick** "Start the task only
   if the computer is on AC power" and **untick** "Stop if the computer switches to battery
   power." If left ticked, the schedule silently stops running whenever the machine isn't
   plugged in. Also **tick** "Wake the computer to run this task" — without this, if the
   machine does end up asleep/hibernating for any reason, scheduled runs during that window
   are silently skipped, not delayed.
7. **Settings tab**: tick "If the task fails, restart every" 10 minutes, up to 3 times.
8. Set **both** sleep timers to Never — there are two separate ones, and Windows can
   auto-hibernate even with the first one off:
   - Settings → System → Power & Battery → set **Screen and sleep** → "When plugged in, put
     my device to sleep" → **Never**.
   - Then open **Control Panel → Power Options → Change plan settings → Change advanced power
     settings**, expand **Sleep**, and set **both** "Sleep after" AND "Hibernate after" to
     **Never (0)** — these are independent settings; disabling only the first one still lets
     Windows hibernate the machine after a delay, which looks like the computer turned off but
     silently skips every scheduled run until it's manually woken up. A telltale sign this is
     happening: the machine appears completely off overnight, but everything (open windows,
     Chrome tabs) is exactly as you left it when you turn it back on — that's Windows
     restoring from hibernation, not a real shutdown.
   - If it's a laptop, also set "when I close the lid" to **Do nothing**.

## 6. Run it once manually to confirm it works

Don't wait for the schedule — run `run_tracker.bat` directly once, right after setup.

### What a healthy run looks like

You'll see lines like:
```
🔍 Fetching complete active catalog for: stanley quencher (domains: fr, be, lu, ...)
  📄 Page 1: 96 items fetched via requests
  📄 Page 2: 96 items fetched via requests
  ...
  🌍 .fr: 958 items (372 new after cross-domain merge)
```
That's real data coming in — good.

### Red flags to watch for, and what they mean

| What you see | What it means | What to do |
|---|---|---|
| `Chrome debug port unreachable` appearing **once in a while** | Normal — Chrome recovers itself automatically | Nothing, this is expected occasionally |
| `Chrome debug port unreachable` appearing **before almost every product**, every 20-40 seconds | Real instability, still being investigated | Send us the log |
| `Error 431` | A bug we already fixed (cookie buildup) — should not appear if you're on the current files | If it still appears, send us the log |
| `Error 404` for every product | The catalog endpoint would be broken — already fixed, should not appear | If it still appears, send us the log immediately, this blocks everything |
| The run ends with `=== run complete ===` | The full cycle finished | Good sign |

## 7. (Optional) Turn on AI product identification

Only do this once steps 1-6 are working cleanly.

1. **Set a spend cap in the Anthropic Console first** — Billing → Usage limits — before doing
   anything else with the key.
2. Set two environment variables the same way (Windows key → "environment variables" → Edit the
   system environment variables → Environment Variables → New, under User variables):
   - `ANTHROPIC_API_KEY` = your key (never paste this anywhere except this one field)
   - `VINTED_VISION_MODEL` = `claude-sonnet-5`
3. Verify each saved by opening a **new** Command Prompt and running `echo %ANTHROPIC_API_KEY%`
   and `echo %VINTED_VISION_MODEL%` — each should print back what you set.
4. Open `run_tracker.bat` in Notepad, find these three lines, and remove `REM ` from the start
   of each:
   ```
   set VINTED_VISION=1
   set VINTED_VISION_PROVIDER=anthropic
   set VINTED_DEDUP=1
   ```
   Save the file.
5. Run `run_tracker.bat` once manually and check the log for a line containing
   `product identification` — it should end with `(provider: anthropic)`. If it says
   `(provider: stub)` instead, the flags above didn't take effect — re-check step 4.
6. After the first day, check **Usage** in the Anthropic Console and send us a screenshot so we
   can confirm the budget is comfortable for continuous running.

## 8. Ongoing health checklist

Once everything is running on its own schedule, check in on it every few days:

- **Is Chrome still open?** If it got closed (accidentally, or by a restart), runs will skip
  themselves safely but collect nothing until you run `start_scraper.bat` again and log back in.
- **Are new files appearing in the `logs` folder** roughly every 4-6 hours? If not, Task
  Scheduler may have stopped — check Task Scheduler's history for that task.
- **Open the newest log occasionally** and skim for the red flags in the table above.

---

## If something looks wrong

Send us:
1. The actual `.txt` log file from the `logs` folder (not a photo of the screen) — this is by
   far the most useful thing you can send, since it's complete and we can search it.
2. Which package/files you're currently running (so we know which fixes are or aren't applied).

That's it — this covers the whole setup end to end.
