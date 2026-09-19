@echo off
REM ============================================================
REM   VINTED TRACKER - one-time setup (Windows)
REM ============================================================
REM Installs everything needed to run the tracker, including vision AI (optional -
REM installs cleanly even if you never turn VINTED_VISION on). Run this once before
REM the first start_scraper.bat / run_tracker.bat.
chcp 65001 >nul
cd /d "%~dp0"

echo ===================================================
echo Vinted Scraper - Setup
echo ===================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed, or not on PATH.
    echo Download it from https://www.python.org/downloads/ and check
    echo "Add Python to PATH" during install, then run this script again.
    pause
    exit /b 1
)

echo [1/3] Installing required Python packages...
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo [2/3] Setting up Browser Automation (Playwright)...
python -m playwright install chromium
if errorlevel 1 goto :fail

echo.
echo [3/3] Installing vision AI packages (optional - product identification)...
REM Optional: runs LAST and a failure here is NOT fatal, so it can never block the essentials
REM above (e.g. no torch build for a very new Python version). Tracking works without it.
python -m pip install -r requirements-phase5.txt
if errorlevel 1 (
    echo.
    echo [WARNING] Vision AI packages did not install. Tracking will still work; only AI
    echo product identification is unavailable until this is fixed. Send us the message above.
)

echo.
echo ===================================================
echo Setup Complete!
echo You can now run start_scraper.bat to log in, then
echo run_tracker.bat to start tracking.
echo ===================================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup did not finish - see the message above.
pause
exit /b 1
