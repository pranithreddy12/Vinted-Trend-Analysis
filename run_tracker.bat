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

REM Bare "python" can resolve to a DIFFERENT install than the one with the project's deps
REM installed (live-observed 2026-08-29: PATH put a numpy-less Python 3.14 ahead of the 3.11
REM install everything was tested against, silently degrading numpy-dependent features).
REM Pin to the known-good install; fall back to PATH resolution if it's not on this machine.
REM No machine-specific path here on purpose (dropped 2026-09-25 - it pointed at the
REM freelancer's own dev machine and could never exist on the client's, so this always
REM fell through to the line below anyway; harmless, but dead weight). Bare "python" is
REM what setup.bat's own install targets, so it is the one guaranteed to have the deps.
set PYTHON=python

set VINTED_AUTOMATED=1
set VINTED_TRACK_WORKERS=2
REM Cap per-run item-page work so ONE product with a big/first-time catalog can't turn a
REM scheduled 6h-cadence run into a many-hour marathon (live-observed: a 1,551-item first-time
REM enrichment alone ran well over an hour). Uncapped work resumes automatically on the NEXT
REM run - nothing is lost, it just spreads across cycles instead of blocking everything after it.
set VINTED_MAX_ENRICH=300
set VINTED_MAX_VERIFY=300
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

REM Confirm the logged-in debug-Chrome is up. call :probe_chrome (defined at the bottom of
REM this file) retries the check a few times first, so one transient blip doesn't trigger a
REM needless restart - only act once it has genuinely failed repeatedly.
call :probe_chrome
if errorlevel 1 (
  for /f %%c in ('tasklist /FI "IMAGENAME eq chrome.exe" /NH 2^>nul ^| find /c /v ""') do set "chromecount=%%c"
  echo [%date% %time%] Chrome debug port unreachable - attempting one relaunch... ^(chrome.exe processes: %chromecount%^)
  REM MUST kill any existing chrome.exe first. Live-confirmed 2026-09-25 (client report +
  REM reproduced directly): if a Chrome process for this profile is already running,
  REM launching another "chrome.exe ... --remote-debugging-port=... URL" is SILENTLY
  REM IGNORED by Chrome's single-instance handling - it just opens a new tab in the SAME
  REM window ("Opening in existing browser session"), the debug port is never actually
  REM re-enabled, and every line here used to print "relaunched successfully" whether or
  REM not anything was actually fixed. Killing first makes this a REAL relaunch.
  taskkill /F /IM chrome.exe >nul 2>&1
  timeout /t 2 >nul
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0vinted_profile" "https://www.vinted.fr"
  timeout /t 12 >nul
  call :probe_chrome
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
REM Timestamp from PowerShell, NOT %date%: its order is locale-dependent (French Windows gave
REM YYYY-DD-MM, so logs sorted wrongly across months) and %time% pads hours with a space.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
set LOGFILE=logs\run_%STAMP%.log

echo [%date% %time%] === automated tracking run starting === > "%LOGFILE%"
echo [%date% %time%] === automated tracking run starting ===

REM A full run rarely finishes one cycle (heavy rate-limiting), so starting from line 1 every
REM time meant the SAME front products always completed while the back ones never got a turn -
REM starved indefinitely. rotate_watchlist.py starts this run wherever the LAST run left off,
REM so every product gets priority at least once every 2 cycles regardless of how far any one
REM cycle actually gets.
%PYTHON% rotate_watchlist.py tracked_keywords.txt > tracked_keywords.rotated.txt

setlocal enabledelayedexpansion
for /f "usebackq eol=# tokens=* delims=" %%k in ("tracked_keywords.rotated.txt") do (
  set "kw=%%k"

  REM Re-check Chrome before EVERY product, not just once at the top. Live-observed
  REM 2026-08-25/26: Chrome died/was closed partway through a 14-product run, and every
  REM remaining product failed instantly with no recovery attempt for the rest of the run
  REM (only the top-of-script check existed). Same one-relaunch-then-give-up logic, but
  REM per-iteration so a single closure/crash costs at most one product, not the whole rest
  REM of the batch — and on failure we SKIP this product rather than aborting the run,
  REM since Chrome may come back (or a later relaunch may succeed) for the next one.
  set "chrome_ok=1"
  call :probe_chrome
  if errorlevel 1 (
    for /f %%c in ('tasklist /FI "IMAGENAME eq chrome.exe" /NH 2^>nul ^| find /c /v ""') do set "chromecount=%%c"
    echo [!date! !time!] Chrome debug port unreachable before "!kw!" - attempting one relaunch... ^(chrome.exe processes: !chromecount!^)
    REM See the top-of-script check for why the kill is required for this to be a real
    REM relaunch rather than a silently-ignored no-op that just opens a spurious tab.
    taskkill /F /IM chrome.exe >nul 2>&1
    timeout /t 2 >nul
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0vinted_profile" "https://www.vinted.fr"
    timeout /t 12 >nul
    call :probe_chrome
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
      echo [!date! !time!] START category !cat_id! !cat_name! >> "%LOGFILE%"
      set "VINTED_CATALOG_ID=!cat_id!"
      set "VINTED_CATEGORY_NAME=!cat_name!"
      %PYTHON% track_sales.py >> "%LOGFILE%" 2>&1
      set "rc=!errorlevel!"
      echo [!date! !time!] END   category !cat_id! !cat_name! - exit code !rc! >> "%LOGFILE%"
      set "VINTED_CATALOG_ID="
      set "VINTED_CATEGORY_NAME="
    ) else (
      echo [!date! !time!] tracking: !kw!
      echo [!date! !time!] START "!kw!" >> "%LOGFILE%"
      %PYTHON% track_sales.py "!kw!" >> "%LOGFILE%" 2>&1
      set "rc=!errorlevel!"
      echo [!date! !time!] END   "!kw!" - exit code !rc! >> "%LOGFILE%"
    )
  )
  timeout /t 30 >nul
)
endlocal
echo [%date% %time%] === run complete === >> "%LOGFILE%"
echo [%date% %time%] === run complete ===
exit /b 0

:probe_chrome
REM Retries the debug-port check a few times (2s apart) before reporting failure, so one
REM transient blip (machine briefly busy, a slow response) doesn't trigger a restart that
REM was never actually needed. Returns via errorlevel: 0 = reachable, 1 = genuinely down.
for /l %%i in (1,1,3) do (
  powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 8; exit 0 } catch { exit 1 }"
  if not errorlevel 1 exit /b 0
  timeout /t 2 >nul
)
exit /b 1
