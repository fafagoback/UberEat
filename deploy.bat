@echo off
@chcp 65001 >nul
setlocal enabledelayedexpansion
title UberEats - Push to GitHub

echo ============================================================
echo   UberEats - Push to GitHub
echo ============================================================
echo.

:: Clean up any stale lock files caused by cloud sync
if exist ".git\index.lock" del /f /q ".git\index.lock" >nul 2>&1
if exist ".git\packed-refs.lock" del /f /q ".git\packed-refs.lock" >nul 2>&1

set CURRENT_BRANCH=main
for /f %%i in ('git branch --show-current 2^>nul') do set CURRENT_BRANCH=%%i
if "%CURRENT_BRANCH%"=="" set CURRENT_BRANCH=main

set "COMMIT_MSG=%*"
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=update: %date% %time%"

echo [1/3] Staging all files (git add)...
git add -A

echo.
echo [2/3] Committing changes...
git commit -m "%COMMIT_MSG%"
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No new changes to commit.
)

echo.
echo [3/3] Syncing and Pushing to GitHub (!CURRENT_BRANCH!)...
git pull --rebase origin !CURRENT_BRANCH!
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Rebase had conflicts or remote update needed.
)

git push origin !CURRENT_BRANCH!
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Push to GitHub failed. Please check network connection or resolve any conflicts.
    goto END
)

echo.
echo ============================================================
echo   [SUCCESS] Pushed to GitHub successfully!
echo ============================================================

:END
echo.
pause

