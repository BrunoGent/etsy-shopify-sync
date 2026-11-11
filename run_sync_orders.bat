@echo off
cd /d "C:\etsy-shopify-sync"
python sync.py --import-orders
echo.
pause