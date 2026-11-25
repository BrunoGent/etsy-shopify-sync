@echo off
cd /d "%~dp0"
python sync.py --tracking-only
echo.
rem the Beemine Start menu does its own pause
if not defined BEEMINE_LAUNCHER pause
