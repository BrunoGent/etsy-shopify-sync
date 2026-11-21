@echo off
cd /d "%~dp0"
python sync.py --tracking-only --force-dispatch
echo.
pause
