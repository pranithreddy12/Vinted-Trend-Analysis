@echo off
cd /d "%~dp0"

echo ===============================
echo   VINTED CDP MODE STARTING
echo ===============================

REM Close only the tracker's own Chrome (vinted_profile) so it restarts cleanly with the
REM debugging port. Your other Chrome windows are left alone.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*vinted_profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 >nul

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
echo Done - you are logged in. The login is saved in the vinted_profile folder, so the
echo tracker can restart this Chrome by itself. Run run_tracker.bat (or wait for the
echo scheduled task) to start tracking.
pause
