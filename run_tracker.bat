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
REM Cross-border tracking (Phase 4 delivery, 2026-07-08): your Vinted account's shipping
REM zone - France plus the 9 countries buyers reach you from. FR-only tracking was
REM missing ~45%% of the listings actually visible in this zone.
set VINTED_DOMAINS=fr,be,lu,nl,de,at,es,pt,it,ie

REM ---- Optional AI features (OFF by default - each spends your own Anthropic budget) ----
REM Uncomment to enable during continuous collection. Cost scales with NEW distinct products;
REM VINTED_DEDUP cuts it further by identifying each product once and reusing across sellers.
REM set VINTED_VISION=1
REM set VINTED_VISION_PROVIDER=anthropic
REM set VINTED_DEDUP=1
REM set VINTED_REFERENCE=1
REM set VINTED_DISCOVER=1

REM Confirm the logged-in debug-Chrome is up; if not, stop (don't corrupt anything).
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [%date% %time%] ERROR: Chrome is not running with the debugging port.
  echo    Run start_scraper.bat, log into Vinted, and LEAVE Chrome open. Then retry.
  exit /b 1
)

if not exist tracked_keywords.txt (
  echo [%date% %time%] ERROR: tracked_keywords.txt not found
  exit /b 1
)

echo [%date% %time%] === automated tracking run starting ===
setlocal enabledelayedexpansion
for /f "usebackq eol=# tokens=* delims=" %%k in ("tracked_keywords.txt") do (
  set "kw=%%k"
  if "!kw:~0,4!"=="cat:" (
    REM Seedless category sweep (Phase 6 Layer 1): "cat:<id> <label>" — whole category, no keyword.
    set "rest=!kw:~4!"
    for /f "tokens=1*" %%a in ("!rest!") do (
      set "cat_id=%%a"
      set "cat_name=%%b"
    )
    if "!cat_name!"=="" set "cat_name=category !cat_id!"
    echo [%date% %time%] sweeping category: !cat_id! ^(!cat_name!^)
    set "VINTED_CATALOG_ID=!cat_id!"
    set "VINTED_CATEGORY_NAME=!cat_name!"
    python track_sales.py
    set "VINTED_CATALOG_ID="
    set "VINTED_CATEGORY_NAME="
  ) else (
    echo [%date% %time%] tracking: !kw!
    python track_sales.py "!kw!"
  )
  timeout /t 30 >nul
)
endlocal
echo [%date% %time%] === run complete ===
