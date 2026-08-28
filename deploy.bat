@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title UberEats - Push to GitHub and Deploy Sync

echo ============================================================
echo   UberEats - Push to GitHub and Deploy Sync
echo ============================================================
echo.

:: Clean up stale lock files caused by cloud sync
if exist ".git\index.lock" del /f /q ".git\index.lock" >nul 2>&1
if exist ".git\packed-refs.lock" del /f /q ".git\packed-refs.lock" >nul 2>&1

set CURRENT_BRANCH=main
for /f %%i in ('git branch --show-current 2^>nul') do set CURRENT_BRANCH=%%i
if "%CURRENT_BRANCH%"=="" set CURRENT_BRANCH=main

set "COMMIT_MSG=%*"
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=update: %date% %time%"

echo [1/4] Staging all files [git add]...
git add -A

echo.
echo [2/4] Committing changes...
git commit -m "%COMMIT_MSG%"
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No new changes to commit.
)

echo.
echo [3/4] Syncing and Pushing to GitHub [%CURRENT_BRANCH%]...
git pull --rebase origin %CURRENT_BRANCH%
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Rebase had conflicts or remote update needed.
)

:: Check if there are web-related changes in outgoing commits
set HAS_WEB_CHANGES=0
git rev-parse --verify origin/%CURRENT_BRANCH% >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    for /f "delims=" %%f in ('git diff --name-only origin/%CURRENT_BRANCH%..HEAD -- web/ .github/workflows/deploy_pages.yml 2^>nul') do (
        set HAS_WEB_CHANGES=1
    )
) else (
    for /f "delims=" %%f in ('git diff --name-only 4b825dc642cb6eb9a060e54bf8d69288fbee4904..HEAD -- web/ .github/workflows/deploy_pages.yml 2^>nul') do (
        set HAS_WEB_CHANGES=1
    )
)

git push origin %CURRENT_BRANCH%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Push to GitHub failed. Please check network connection or resolve any conflicts.
    goto END
)

set PUSH_SHA=
for /f %%i in ('git rev-parse HEAD 2^>nul') do set PUSH_SHA=%%i

echo.
echo [4/4] 檢查 GitHub Pages 部署狀態...

if not "%CURRENT_BRANCH%"=="main" goto NOT_MAIN_BRANCH
if "%HAS_WEB_CHANGES%"=="0" goto NO_WEB_CHANGES

:: Web changes detected on main branch
echo [INFO] 檢測到網頁相關檔案 [web/] 有變動，正在同步追蹤 GitHub Pages 部署進度...
echo.

where gh >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] 本機未安裝 GitHub CLI [gh]，GitHub Pages 將在背景自動建置。
    echo 網站網址: https://fafagoback.github.io/UberEat/
    goto SUCCESS_BANNER
)

echo [INFO] 等待 GitHub Actions 啟動部署任務 [Commit: !PUSH_SHA:~0,7!]...
set RUN_ID=
for /L %%i in (1,1,12) do (
    if "!RUN_ID!"=="" (
        for /f "tokens=*" %%r in ('gh run list --commit !PUSH_SHA! --workflow deploy_pages.yml --json databaseId -q ".[0].databaseId" 2^>nul') do (
            set "RUN_ID=%%r"
        )
        if "!RUN_ID!"=="" (
            ping 127.0.0.1 -n 3 >nul
        )
    )
)

if "!RUN_ID!"=="" goto NO_RUN_ID

echo.
echo [INFO] 找到部署任務 ID: !RUN_ID!，開始即時監控部署進度...
echo ------------------------------------------------------------
gh run watch !RUN_ID!
set WATCH_EXIT=!ERRORLEVEL!
echo ------------------------------------------------------------

if !WATCH_EXIT! EQU 0 (
    echo.
    echo ============================================================
    echo   [SUCCESS] GitHub Pages 部署完成並已成功上線！
    echo   網站網址: https://fafagoback.github.io/UberEat/
    echo ============================================================
    goto END
) else (
    echo.
    echo [WARNING] GitHub Pages 部署可能失敗或取消，詳情請參閱：
    echo https://github.com/fafagoback/UberEat/actions/runs/!RUN_ID!
    goto END
)

:NO_RUN_ID
echo [INFO] 已觸發 GitHub Pages 部署（未能即時獲取任務 ID，可能在排隊中）。
echo 網站網址: https://fafagoback.github.io/UberEat/
goto END

:NOT_MAIN_BRANCH
echo [INFO] 當前分支為 %CURRENT_BRANCH%（GitHub Pages 僅在 main 分支自動部署）。
goto SUCCESS_BANNER

:NO_WEB_CHANGES
echo [INFO] 本次未變更網頁相關檔案 [web/]，無需更新 GitHub Pages。
goto SUCCESS_BANNER

:SUCCESS_BANNER
echo.
echo ============================================================
echo   [SUCCESS] 程式碼已成功推送至 GitHub！
echo ============================================================

:END
echo.
pause

