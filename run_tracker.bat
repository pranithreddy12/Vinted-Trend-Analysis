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

REM Confirm the logged-in debug-Chrome is up; if not, stop (don't corrupt anything).
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3 ^| Out-Null; exit 0 } catch { exit 1 }"
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
for /f "usebackq eol=# tokens=* delims=" %%k in ("tracked_keywords.txt") do (
  echo [%date% %time%] tracking: %%k
  python track_sales.py "%%k"
  timeout /t 30 >nul
)
echo [%date% %time%] === run complete ===
