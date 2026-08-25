@echo off
title UberEats Radar - Local Dashboard

echo ============================================================
echo   UberEats Radar - Price and Product Monitoring System
echo ============================================================
echo.
echo [1/2] Running alert engine to compute discounts and changes...
py local_scr\alert_engine.py
if %ERRORLEVEL% NEQ 0 (
    python local_scr\alert_engine.py
)

echo.
echo [2/2] Starting local web server and opening browser...
echo Server URL: http://localhost:8000
echo.

py local_scr\server.py --open
if %ERRORLEVEL% NEQ 0 (
    python local_scr\server.py --open
)

pause
