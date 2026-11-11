@echo off
cd /d "%~dp0"
python sync.py --import-orders
echo.
pause
