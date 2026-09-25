@echo off
cd /d "%~dp0"

echo ===============================
echo   VINTED CDP MODE STARTING
echo ===============================

REM Kill any existing Chrome so it starts cleanly with the debugging port.
taskkill /F /IM chrome.exe >nul 2>&1

REM %~dp0vinted_profile - relative to THIS script's own folder, so it works wherever this
REM package is extracted. A hardcoded absolute path here (as this file used to have, since
REM Phase 3) breaks on any machine but the one it was written on - it only "worked" for the
REM client because run_tracker.bat's own recovery logic (already portable) quietly created
REM the real, working profile instead. Keep both scripts pointed at the same portable path.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
--remote-debugging-port=9222 ^
--user-data-dir="%~dp0vinted_profile" ^
"https://www.vinted.fr"

timeout /t 8 >nul

echo.
echo Log into Vinted in the Chrome window that just opened, then press any key here...
pause

echo.
echo Done - Chrome is logged in and ready. Leave this Chrome window OPEN, then run
echo run_tracker.bat (or wait for the scheduled task) to start tracking.
pause
