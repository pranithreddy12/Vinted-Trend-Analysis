@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHON=python

echo ===============================================
echo   CONFIRMED SALES - products in model_checks.txt
echo ===============================================
echo.
echo --- last 48 hours ---
%PYTHON% check_model_sales.py --list model_checks.txt --hours 48
echo.
echo --- last 7 days ---
%PYTHON% check_model_sales.py --list model_checks.txt --hours 168
echo.
echo Results are also saved in model_checks_log.csv - please send me that file.
pause
