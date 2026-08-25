@echo off
setlocal enabledelayedexpansion
title UberEats Radar - Deploy to GitHub

echo ============================================================
echo   UberEats Radar - One-Click Deploy to GitHub
echo ============================================================
echo.

set CURRENT_BRANCH=main
for /f %%i in ('git branch --show-current 2^>nul') do set CURRENT_BRANCH=%%i
if "%CURRENT_BRANCH%"=="" set CURRENT_BRANCH=main

set COMMIT_MSG=%*
if "%COMMIT_MSG%"=="" set COMMIT_MSG=auto: deploy update %date% %time%

echo [1/3] Staging all files (git add)...
git add -A

echo.
echo [2/3] Committing changes...
git commit -m "%COMMIT_MSG%"
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No new changes to commit.
)

echo.
echo [3/3] Pushing to GitHub branch (%CURRENT_BRANCH%)...
git push origin %CURRENT_BRANCH%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Push to GitHub failed. Please check network connection.
    goto END
)

echo.
echo ============================================================
echo   [SUCCESS] Deployment pushed successfully!
echo   Repository:   https://github.com/hub-google/UberEat
echo   GitHub Pages: https://hub-google.github.io/UberEat/
echo ============================================================

:END
echo.
pause
