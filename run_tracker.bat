@echo off
REM ============================================================
REM   VINTED TRACKER - automated scheduled run (Phase 4.5, Windows)
REM ============================================================
REM Runs the sales tracker for every product in tracked_keywords.txt, unattended.
REM Assumes Chrome is already running with the debugging port + logged-in Vinted
REM profile (keep it running; see AUTOMATION.md). Schedule with Task Scheduler.
chcp 65001 >nul
cd /d "%~dp0"

set VINTED_AUTOMATED=1
set VINTED_TRACK_WORKERS=2

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
