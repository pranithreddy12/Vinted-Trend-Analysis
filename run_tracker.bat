@echo off
REM ============================================================
REM   VINTED TRACKER - automated scheduled run (Windows)
REM ============================================================
REM Runs the sales tracker for every product in tracked_keywords.txt, unattended.
REM
REM Chrome: every run starts the tracker's own Chrome fresh (profile folder vinted_profile,
REM so the Vinted login is kept) and restarts it again whenever it stops accepting
REM connections. Only the tracker's Chrome is ever closed - other Chrome windows are left alone.
REM Only ONE run at a time: if a previous run is still going, a new start skips itself.
chcp 65001 >nul
cd /d "%~dp0"

REM Bare "python" is what setup.bat installs the dependencies into.
set PYTHON=python
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE_DIR=%~dp0vinted_profile"

set VINTED_AUTOMATED=1
set VINTED_TRACK_WORKERS=2
REM Cap per-run item-page work so one big product can't block everything after it. Uncapped
REM work resumes automatically on the next run - nothing is lost.
set VINTED_MAX_ENRICH=300
set VINTED_MAX_VERIFY=300
REM Unbuffered output, so the log fills live instead of only when Python exits.
set PYTHONUNBUFFERED=1
REM Cross-border tracking: France plus the 9 countries buyers reach you from.
set VINTED_DOMAINS=fr,be,lu,nl,de,at,es,pt,it,ie
REM Verify a disappearance against the real Vinted page before counting it as a sale.
set VINTED_VERIFY_SOLD=1
REM Opportunity ranking + alerts (no extra cost).
set VINTED_DISCOVER=1

REM ---- Optional AI features (OFF by default - each spends your own Anthropic budget) ----
REM Cleared explicitly so a stray "setx VINTED_VISION 1" can never silently turn AI spend on.
set VINTED_VISION=
set VINTED_VISION_PROVIDER=
set VINTED_DEDUP=
set VINTED_REFERENCE=
REM set VINTED_VISION=1
REM set VINTED_VISION_PROVIDER=anthropic
REM set VINTED_DEDUP=1
REM set VINTED_REFERENCE=1

if not exist logs mkdir logs

REM Single-instance lock. Live-observed 2026-10-05/06: a full cycle can take ~20h, so the next
REM 4-hourly start (or a manual one) launched a SECOND run while the first was still going. Both
REM shared one Chrome, each opened its own tabs and could kill Chrome under the other, and
REM within a day every product failed with "Could not connect to Chrome". cmd holds the lock
REM file open for the whole run and Windows releases it automatically when the run ends or
REM crashes, so a stale lock can't block future runs.
set "RAN="
2>nul (
  9>"%~dp0run_tracker.lock" call :main
)
if not defined RAN (
  echo [%date% %time%] previous run still in progress - this start skipped >> "logs\skipped_starts.log"
  echo A previous tracker run is still in progress - this start is skipped.
)
exit /b 0

:main
set "RAN=1"

if not exist tracked_keywords.txt (
  echo [%date% %time%] ERROR: tracked_keywords.txt not found
  exit /b 1
)

REM Timestamp from PowerShell, NOT %date% (locale-dependent order; %time% pads with a space).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
set LOGFILE=logs\run_%STAMP%.log

echo [%date% %time%] === automated tracking run starting === > "%LOGFILE%"
echo [%date% %time%] === automated tracking run starting ===

REM Fresh Chrome at the start of every run: a Chrome left open for a long time can still
REM answer a simple ping while refusing real connections (live-observed twice).
call :restart_chrome
call :probe_chrome
if errorlevel 1 (
  echo [%date% %time%] ERROR: Chrome did not come up with the debugging port. Run start_scraper.bat, log into Vinted, then retry. >> "%LOGFILE%"
  echo [%date% %time%] ERROR: Chrome did not come up with the debugging port. Run start_scraper.bat, log into Vinted, then retry.
  exit /b 1
)

REM rotate_watchlist.py starts this run wherever the LAST run left off, so every product gets
REM priority at least once every 2 cycles regardless of how far any one cycle gets.
%PYTHON% rotate_watchlist.py tracked_keywords.txt > tracked_keywords.rotated.txt

setlocal enabledelayedexpansion
for /f "usebackq eol=# tokens=* delims=" %%k in ("tracked_keywords.rotated.txt") do (
  set "kw=%%k"

  REM Re-check Chrome with a REAL connection before every product; restart it if it fails,
  REM and skip just this product (not the whole run) if even a restart doesn't help.
  set "chrome_ok=1"
  call :probe_chrome
  if errorlevel 1 (
    echo [!date! !time!] Chrome not accepting connections before "!kw!" - restarting Chrome
    echo [!date! !time!] Chrome not accepting connections before "!kw!" - restarting Chrome >> "%LOGFILE%"
    call :restart_chrome
    call :probe_chrome
    if errorlevel 1 (
      echo [!date! !time!] Chrome still unreachable after restart - skipping "!kw!" this run. >> "%LOGFILE%"
      set "chrome_ok=0"
    ) else (
      echo [!date! !time!] Chrome restarted successfully - continuing. >> "%LOGFILE%"
    )
  )

  if "!chrome_ok!"=="1" (
    if "!kw:~0,4!"=="cat:" (
      REM Seedless category sweep: "cat:<id> <label>" - whole category, no keyword.
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

:restart_chrome
REM Close ONLY the tracker's Chrome (the one using vinted_profile), then start it fresh.
REM A plain relaunch while it is still running is silently ignored by Chrome (it just opens a
REM tab in the existing window), so the close is required. Launched via PowerShell
REM Start-Process, NOT cmd's "start": a "start"-ed Chrome inherits the run lock above and would
REM keep it held forever after the run ends (tested), blocking every future run.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*vinted_profile*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 3 >nul
powershell -NoProfile -Command "Start-Process -FilePath $env:CHROME -ArgumentList '--remote-debugging-port=9222', ('--user-data-dir=' + [char]34 + $env:PROFILE_DIR + [char]34), '--hide-crash-restore-bubble', 'https://www.vinted.fr'"
timeout /t 15 >nul
exit /b 0

:probe_chrome
REM A REAL CDP connection (probe_cdp.py), not just an HTTP ping: a degraded Chrome can answer
REM the ping while every real connection times out. 3 tries, 2s apart.
REM Returns errorlevel 0 = usable, 1 = not usable.
for /l %%i in (1,1,3) do (
  %PYTHON% probe_cdp.py
  if not errorlevel 1 exit /b 0
  timeout /t 2 >nul
)
exit /b 1
