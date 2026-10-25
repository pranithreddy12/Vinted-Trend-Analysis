"""Build the client update package: python build_update_package.py [YYYY-MM-DD]

Output (never committed: delivery_* and *.zip are git-ignored): delivery_update_<date>/ and Phase6_Update_<date>.zip
  START_HERE_UPDATE.md, HOW_TO_RUN_YOUR_TEST.md, CLIENT_SETUP_GUIDE.md, MANIFEST.md
  repaired_data/*.csv   the client's own files with the page-verified repairs (applied by apply_repairs.bat)
  src/                  the code: every module the entry points import, the .bat launchers, the keyword list

The four repaired files are snapshots of client_data/ taken while a verification run may be writing them:
each copy is parsed and compared with the live file, and re-copied until the two agree.
"""
import ast, csv, os, shutil, sys, time, zipfile
from datetime import date

DAY = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
OUT = f"delivery_update_{DAY}"
ZIP = f"Phase6_Update_{DAY}.zip"
ENTRY = ["track_sales", "fetch_results", "discover_opportunities", "rotate_watchlist", "probe_cdp",
         "build_first_results", "check_model_sales", "lookup_product", "product_groups", "verify_backlog",
         "merge_repairs", "vision_identify", "image_cluster", "product_dedup", "reference_lookup",
         "product_identify", "test_fixes"]
FILES = ["run_tracker.bat", "start_scraper.bat", "setup.bat", "check_models.bat", "model_checks.txt",
         "requirements.txt", "requirements-phase5.txt", "probe_cdp.py", "run_tracker.sh", "start_scraper.sh"]
REPAIRED = ["smeg_tumbler", "stanley_quencher", "women", "home"]


def closure(entry):
    local, seen, todo = {f[:-3] for f in os.listdir(".") if f.endswith(".py")}, set(), list(entry)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        for n in ast.walk(ast.parse(open(m + ".py", encoding="utf-8").read())):
            names = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else \
                [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 else []
            todo += [x for x in names if x in local]
    return sorted(seen)


def snapshot(name):
    src, dst = f"client_data/{name}.csv", f"{OUT}/repaired_data/{name}.csv"
    for _ in range(10):
        shutil.copyfile(src, dst)
        a = list(csv.DictReader(open(dst, encoding="utf-8")))
        b = list(csv.DictReader(open(src, encoding="utf-8")))
        if a and len(a) == len(b) and len({r["id"] for r in a}) == len(a):
            return len(a)
        time.sleep(3)
    sys.exit(f"could not take a consistent copy of {src}")


def crlf(path, text):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))


if os.path.exists(OUT):
    shutil.rmtree(OUT)
os.makedirs(f"{OUT}/src"), os.makedirs(f"{OUT}/repaired_data")
mods = closure(ENTRY)
for f in [m + ".py" for m in mods] + FILES:
    shutil.copyfile(f, f"{OUT}/src/{f}")
shutil.copyfile("CLIENT_SETUP_GUIDE.md", f"{OUT}/CLIENT_SETUP_GUIDE.md")
# keyword list: the current one plus the client's four categories as commented lines to fill in
kw = open("tracked_keywords.txt", encoding="utf-8").read().rstrip() + """

# ---- Your four priority categories, one line per Vinted subcategory (numbers read on vinted.fr, 2026-10-25) ----
# Vinted shows only the ~960 newest listings per country for each line, so each subcategory gets its own line.
# A line also covers all the subcategories below it (2630 includes running, fitness, tennis... shoes).
cat:948 women facial care
cat:2630 women sport shoes
cat:2632 women sneakers
cat:3508 home pans
cat:3507 home saucepans
cat:3477 home kitchen tools
"""
crlf(f"{OUT}/src/tracked_keywords.txt", kw)
counts = {n: snapshot(n) for n in REPAIRED}

crlf(f"{OUT}/src/apply_repairs.bat", """@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Adds our page-verified details (colour, size, category, confirmed sales) to YOUR tracking files.
REM Nothing is deleted or downgraded. Each file is first copied to <name>.before_merge.csv.
if not exist tracking (echo The tracking folder is missing here - copy it from your old folder first. & pause & exit /b 1)
for %%N in (smeg_tumbler stanley_quencher women home) do (
  if exist "tracking\\%%N.csv" (
    python merge_repairs.py "..\\repaired_data\\%%N.csv" "tracking\\%%N.csv"
  ) else (
    echo skipped %%N: no tracking\\%%N.csv in your tracking folder
  )
)
echo.
echo Done. Now run:  python test_fixes.py
pause
""")

doc = lambda name, text: open(f"{OUT}/{name}", "w", encoding="utf-8").write(text)
doc("START_HERE_UPDATE.md", f"""# Update {DAY}: what to do, in order

This package replaces the one you have. **Your sales history and your Vinted login are kept.**

## What changed (plain list)
- A listing is counted as part of a product only when brand, model/type, colour and size (or capacity)
  are identified. Anything else is listed as "not counted".
- Confirmed sales come only from item pages that show **Sold** while logged in.
- Brand ranking and product ranking per category, each sale with its Vinted link.
- No trend and no time to sell when the dates are too coarse to compute them.
- A product lookup by name (see HOW_TO_RUN_YOUR_TEST.md) that gives sales, price, competition and a
  reliability level.
- The tracker no longer re-opens the same listings every run (it was wasting about two thirds of the
  time spent capturing publish times).
- Your four priority categories are already in `tracked_keywords.txt`, one line per subcategory (step 5).
- **Exact model names:** products are now ranked by the model name read from the titles ("Jellycat Sky Dragon
  Brown"), not by colour alone. A listing whose title names no model is left out ("not counted").
- **A logged-out run now stops and says so:** if two products in a row find Vinted logged out, the run stops, the
  log ends with "RUN STOPPED: Vinted login expired", the file `LOGIN_EXPIRED.txt` appears and Task Scheduler shows
  an error. Before, the run went through every product and ended with "run complete" having collected nothing.
- **Photo AI (stays OFF until you switch it on):** when it is on, a listing whose brand or model is missing in the
  title can be identified from its photo (medium or high confidence only; never the size, capacity or colour you
  entered). Such listings are labelled "AI-identified" in the results.

## Steps (about 20 minutes)
1. **Stop the Task Scheduler task** (right-click it, End, then Disable). Never update while it runs.
2. Extract this zip to a **new** folder, for example `C:\\VintedTracker_new`. Do not unzip over the old one.
3. From your **old** `src` folder, copy these into the **new** `src` folder:
   - the `tracking` folder (your whole sales history),
   - the `vinted_profile` folder (your Vinted login),
   - your `tracked_keywords.txt`, **only if you changed it**: then add the four category lines from the new
     file at the bottom of yours.
4. In the new `src` folder, double-click **`apply_repairs.bat`**. It adds our verified details to your
   tracking files (each file is first saved as `<name>.before_merge.csv`). Then open a Command Prompt in
   `src` and run `python test_fixes.py` and `python track_sales.py --selftest`: each must end with OK.
5. **Your four categories:** the six lines are already in `tracked_keywords.txt` (near the bottom, "Your four
   priority categories"). If you copied your own `tracked_keywords.txt` in step 3, add those six lines to it.
   The first runs take longer because there are more sweeps; after the first day send me the newest log from `logs`
   and I will tell you, for each subcategory, how many listings were seen and whether Vinted's limit was reached.
6. In Task Scheduler, point the task's action to the **new** `src\\run_tracker.bat`, enable it, and run
   `run_tracker.bat` once by hand. Check the newest file in `logs` for `=== run complete ===`.
7. Keep the old folder for a week, then delete it.

## What to expect
- Numbers on your Smeg and Stanley products will match our report only after step 4.
- Vinted ends the login session every few hours. If the log ends with "RUN STOPPED: Vinted login expired" (or the
  file `LOGIN_EXPIRED.txt` appears), run `start_scraper.bat`, **log out of Vinted and then log in again**; the next
  scheduled run resumes by itself. Logging in on top of the old session does not work.
- Trend and time to sell appear after a few days of runs. Offers (13+) are collected for listings checked
  1 to 3 days after they appear.
""")
doc("HOW_TO_RUN_YOUR_TEST.md", f"""# Your test: look up one product

Open a Command Prompt in the `src` folder and run, for example:

    python lookup_product.py "adidas samba" --files tracking\\women.csv
    python lookup_product.py "adidas samba white 37" --files tracking\\women.csv
    python lookup_product.py "tupperware" --files tracking\\home.csv --days 30

You give a **name** (brand, product, and optionally a colour or a size). You get, for each distinct product:
confirmed sales over the last 30 days, how many are for sale now, the competition level, the median asking
price at sale, the Vinted link of every sale, and a **reliability level (Low / Medium / High) with the reason**.

How to read it
- Several different products can match one name: each is shown on its own and never added to another.
- Listings that match the name but could not be identified are counted and shown, not used.
- Reliability is **Low** when your files cover fewer days than the window or when there are fewer than 5 sales.
  It is **High** only with 20+ confirmed sales, the window covered, and most sales read on the item page.
- The price is the asking price when the listing was last seen. Vinted does not publish the price paid.
- Time to sell appears only with 5+ sales that have a posting date.
- A photo cannot be used yet; please give the name. If a name has no identified product, the answer is "no data":
  nothing is guessed.

Smeg and Stanley checks: `check_models.bat` (it reads `model_checks.txt`).
""")
doc("MANIFEST.md", f"""# Update {DAY} - contents
- START_HERE_UPDATE.md - the steps. HOW_TO_RUN_YOUR_TEST.md - the product lookup. CLIENT_SETUP_GUIDE.md - full setup.
- repaired_data/ - {', '.join(f'{k}: {v} rows' for k, v in counts.items())} (your own files, with verified details).
- src/ - {len(mods)} Python modules ({', '.join(mods)}) and the launchers.
""")

with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(OUT):
        for f in files:
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, OUT))
print(f"{ZIP}: {os.path.getsize(ZIP) / 1e6:.1f} MB, {len(mods)} modules, repaired rows {counts}")
