@echo off
cd /d "%~dp0"
python sync.py --tracking-only
echo.
pause
