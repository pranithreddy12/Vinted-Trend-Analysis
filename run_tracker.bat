@echo off
REM ============================================================
REM   VINTED TRACKER - automated scheduled run (Phase 4.5, Windows)
REM ============================================================
REM Runs the sales tracker for every product in tracked_keywords.txt, unattended.
REM
REM REQUIRES: Chrome already running with the debugging port + logged into Vinted,
REM left OPEN. This script does NOT relaunch Chrome — relaunching starts a
REM logged-out browser even though the cookies are on disk. Keep the Chrome window
REM open on the dedicated machine. See AUTOMATION.md.
chcp 65001 >nul
cd /d "%~dp0"

set VINTED_AUTOMATED=1
set VINTED_TRACK_WORKERS=2
REM Unbuffered stdout/stderr: without this, Python fully buffers output when it is not
REM attached to a live terminal (i.e. always, under Task Scheduler / redirected to a log
REM file), so nothing appears in LOGFILE until the process exits - making a slow, healthy
REM run look identical to a hung one. This flushes every print immediately.
set PYTHONUNBUFFERED=1
REM Cross-border tracking (Phase 4 delivery, 2026-07-08): your Vinted account's shipping
REM zone - France plus the 9 countries buyers reach you from. FR-only tracking was
REM missing ~45%% of the listings actually visible in this zone.
set VINTED_DOMAINS=fr,be,lu,nl,de,at,es,pt,it,ie

REM Data reliability (2026-08-22): verify a disappearance against the real Vinted page before
REM counting it as a sale. ON by default - no extra cost (just item-page loads, same as
REM publish-time enrichment), and it fixed a ~10x sale overcount found on live data.
set VINTED_VERIFY_SOLD=1
REM Opportunity ranking + alerts (Phase 6): pure computation on data already collected, no
REM extra cost. ON by default so opportunities_<slug>.csv builds up from the first run.
set VINTED_DISCOVER=1

REM ---- Optional AI features (OFF by default - each spends your own Anthropic budget) ----
REM Cleared explicitly (not just left unset) so a stray "setx VINTED_VISION 1" left over from
REM manual testing on this machine can never silently turn AI spend on in an automated run -
REM this script is the single source of truth for what runs unattended. Uncomment a line below
REM to actually enable a feature. Cost scales with NEW distinct products; VINTED_DEDUP cuts it
REM further by identifying each product once and reusing across sellers.
set VINTED_VISION=
set VINTED_VISION_PROVIDER=
set VINTED_DEDUP=
set VINTED_REFERENCE=
REM set VINTED_VISION=1
REM set VINTED_VISION_PROVIDER=anthropic
REM set VINTED_DEDUP=1
REM set VINTED_REFERENCE=1

REM Confirm the logged-in debug-Chrome is up. If it is not - live-tested 2026-08-25: Chrome can
REM silently die during a long unattended run, which used to fail the ENTIRE rest of the watch-
REM list with no loud warning - try ONE relaunch on the same profile before giving up. A clean
REM relaunch (no taskkill of other Chrome windows) preserves the saved login: verified live.
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [%date% %time%] Chrome debug port unreachable - attempting one relaunch...
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0vinted_profile" "https://www.vinted.fr"
  timeout /t 12 >nul
  powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"
  if errorlevel 1 (
    echo [%date% %time%] ERROR: Chrome still not running with the debugging port after a relaunch attempt.
    echo    Run start_scraper.bat, log into Vinted, and LEAVE Chrome open. Then retry.
    exit /b 1
  )
  echo [%date% %time%] Chrome relaunched successfully - continuing.
)

if not exist tracked_keywords.txt (
  echo [%date% %time%] ERROR: tracked_keywords.txt not found
  exit /b 1
)

if not exist logs mkdir logs
set LOGFILE=logs\run_%date:~-4,4%-%date:~-10,2%-%date:~-7,2%_%time:~0,2%%time:~3,2%.log
set LOGFILE=%LOGFILE: =0%

echo [%date% %time%] === automated tracking run starting === > "%LOGFILE%"
echo [%date% %time%] === automated tracking run starting ===
setlocal enabledelayedexpansion
for /f "usebackq eol=# tokens=* delims=" %%k in ("tracked_keywords.txt") do (
  set "kw=%%k"

  REM Re-check Chrome before EVERY product, not just once at the top. Live-observed
  REM 2026-08-25/26: Chrome died/was closed partway through a 14-product run, and every
  REM remaining product failed instantly with no recovery attempt for the rest of the run
  REM (only the top-of-script check existed). Same one-relaunch-then-give-up logic, but
  REM per-iteration so a single closure/crash costs at most one product, not the whole rest
  REM of the batch — and on failure we SKIP this product rather than aborting the run,
  REM since Chrome may come back (or a later relaunch may succeed) for the next one.
  set "chrome_ok=1"
  powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"
  if errorlevel 1 (
    echo [!date! !time!] Chrome debug port unreachable before "!kw!" - attempting one relaunch...
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0vinted_profile" "https://www.vinted.fr"
    timeout /t 12 >nul
    powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"
    if errorlevel 1 (
      echo [!date! !time!] Chrome still unreachable - skipping "!kw!" this run.
      set "chrome_ok=0"
    ) else (
      echo [!date! !time!] Chrome relaunched successfully - continuing.
    )
  )

  if "!chrome_ok!"=="1" (
    if "!kw:~0,4!"=="cat:" (
      REM Seedless category sweep (Phase 6 Layer 1): "cat:<id> <label>" — whole category, no keyword.
      set "rest=!kw:~4!"
      for /f "tokens=1*" %%a in ("!rest!") do (
        set "cat_id=%%a"
        set "cat_name=%%b"
      )
      if "!cat_name!"=="" set "cat_name=category !cat_id!"
      echo [!date! !time!] sweeping category: !cat_id! ^(!cat_name!^)
      set "VINTED_CATALOG_ID=!cat_id!"
      set "VINTED_CATEGORY_NAME=!cat_name!"
      python track_sales.py >> "%LOGFILE%" 2>&1
      set "VINTED_CATALOG_ID="
      set "VINTED_CATEGORY_NAME="
    ) else (
      echo [!date! !time!] tracking: !kw!
      python track_sales.py "!kw!" >> "%LOGFILE%" 2>&1
    )
  )
  timeout /t 30 >nul
)
endlocal
echo [%date% %time%] === run complete === >> "%LOGFILE%"
echo [%date% %time%] === run complete ===
