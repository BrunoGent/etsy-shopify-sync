@echo off
cd /d "%~dp0"
python sync.py --import-orders --exclude-ni
echo.
rem the Beemine Start menu does its own pause
if not defined BEEMINE_LAUNCHER pause
