@echo off
cd /d "%~dp0"
python sync.py --import-orders --exclude-ni
