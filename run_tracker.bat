@echo off
REM ============================================================
REM   VINTED TRACKER - automated scheduled run (Phase 4.5, Windows)
REM ============================================================
REM Runs the sales tracker for every product in tracked_keywords.txt, unattended.
REM Uses an existing debug-Chrome if one is up; otherwise relaunches a CLEAN Chrome
REM with the logged-in Vinted profile (same as start_scraper). Schedule with Task
REM Scheduler. See AUTOMATION.md.
chcp 65001 >nul
cd /d "%~dp0"

set VINTED_AUTOMATED=1
set VINTED_TRACK_WORKERS=2

REM Is a debug-Chrome already running on port 9222?
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3 ^| Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [%date% %time%] Chrome/CDP not up - launching a clean Chrome with the Vinted profile...
  REM Kill first so the debug port + profile are clean (this is what makes the
  REM logged-in account appear, exactly like start_scraper).
  taskkill /F /IM chrome.exe >nul 2>&1
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%~dp0vinted_profile" "https://www.vinted.fr"
  timeout /t 12 >nul
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
