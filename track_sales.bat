@echo off
chcp 65001 >nul

echo ===============================
echo   VINTED SALES TRACKER (PoC)
echo ===============================

REM Kill Chrome
taskkill /F /IM chrome.exe >nul 2>&1

REM Start Chrome with DEBUGGING
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
--remote-debugging-port=9222 ^
--user-data-dir="C:\Pranith\Freelancing_Projects\04-19-2026-leslie570-vinted_scraping_for_trend_analysis\Vinted\vinted_profile" ^
"https://www.vinted.fr"

timeout /t 8 >nul

echo.
echo Login if needed then press any key...
pause

REM Edit the keyword below to track a different product
python track_sales.py "stanley quencher"

pause
