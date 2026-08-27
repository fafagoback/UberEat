@echo off
@chcp 65001 >nul
setlocal enabledelayedexpansion
title UberEats - Push to GitHub

echo ============================================================
echo   UberEats - Push to GitHub
echo ============================================================
echo.

set CURRENT_BRANCH=main
for /f %%i in ('git branch --show-current 2^>nul') do set CURRENT_BRANCH=%%i
if "%CURRENT_BRANCH%"=="" set CURRENT_BRANCH=main

set "COMMIT_MSG=%*"
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=update: %date% %time%"

echo [1/2] Staging all files (git add)...
git add -A

echo.
echo [2/2] Committing and Pushing to GitHub (!CURRENT_BRANCH!)...
git commit -m "%COMMIT_MSG%"
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No new changes to commit.
)

git push origin !CURRENT_BRANCH!
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Push to GitHub failed. Please check network connection or permissions.
    goto END
)

echo.
echo ============================================================
echo   [SUCCESS] Pushed to GitHub successfully!
echo ============================================================

:END
echo.
pause
