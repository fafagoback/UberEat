@echo off
@chcp 65001 >nul
setlocal enabledelayedexpansion
title UberEats Radar - Deploy to GitHub

echo ============================================================
echo   UberEats Radar - One-Click Deploy to GitHub
echo ============================================================
echo.

set CURRENT_BRANCH=main
for /f %%i in ('git branch --show-current 2^>nul') do set CURRENT_BRANCH=%%i
if "%CURRENT_BRANCH%"=="" set CURRENT_BRANCH=main

set "COMMIT_MSG=%*"
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=auto: deploy update %date% %time%"

REM Auto-detect GitHub repository owner and repo name
set "REPO_OWNER="
set "REPO_NAME="
for /f "tokens=1,2,3,4 delims=/:" %%a in ('git remote get-url origin 2^>nul') do (
    if /i "%%a"=="http" ( set "REPO_OWNER=%%c" & set "REPO_NAME=%%d" )
    if /i "%%a"=="https" ( set "REPO_OWNER=%%c" & set "REPO_NAME=%%d" )
    if /i "%%a"=="git@github.com" ( set "REPO_OWNER=%%b" & set "REPO_NAME=%%c" )
)
if defined REPO_NAME set "REPO_NAME=!REPO_NAME:.git=!"

REM Auto-generate build version timestamp & synchronize web/ folder
echo [0/3] Generating build version and syncing frontend files...
where py >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    py -3 -c "import json, datetime; now = datetime.datetime.now(); v = {'version': now.strftime('%%Y%%m%%d%%H%%M%%S'), 'buildTime': now.strftime('%%Y-%%m-%%d %%H:%%M:%%S')}; open('version.json','w',encoding='utf-8').write(json.dumps(v,indent=2)); open('web/version.json','w',encoding='utf-8').write(json.dumps(v,indent=2))"
) else (
    where python >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        python -c "import json, datetime; now = datetime.datetime.now(); v = {'version': now.strftime('%%Y%%m%%d%%H%%M%%S'), 'buildTime': now.strftime('%%Y-%%m-%%d %%H:%%M:%%S')}; open('version.json','w',encoding='utf-8').write(json.dumps(v,indent=2)); open('web/version.json','w',encoding='utf-8').write(json.dumps(v,indent=2))"
    )
)
if not exist "web" mkdir "web"
copy /y index.html web\index.html >nul
copy /y app.js web\app.js >nul
copy /y style.css web\style.css >nul
copy /y config.js web\config.js >nul
if exist "version.json" copy /y version.json web\version.json >nul
if exist "favicon.svg" copy /y favicon.svg web\favicon.svg >nul
if exist "favicon.png" copy /y favicon.png web\favicon.png >nul
if exist "favicon.ico" copy /y favicon.ico web\favicon.ico >nul
if exist "apple-touch-icon.png" copy /y apple-touch-icon.png web\apple-touch-icon.png >nul

echo.
echo [1/3] Staging all files (git add)...
git add -A

echo.
echo [2/3] Committing changes...
git commit -m "%COMMIT_MSG%"
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] No new changes to commit.
)

echo.
echo [3/3] Pushing to GitHub branch (!CURRENT_BRANCH!)...
git push origin !CURRENT_BRANCH!
if !ERRORLEVEL! NEQ 0 (
    where gh >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        if defined REPO_OWNER (
            echo [INFO] Primary push failed. Retrying with account: !REPO_OWNER!...
            gh auth switch --user !REPO_OWNER! >nul 2>&1
            git push origin !CURRENT_BRANCH!
        )
    )
)
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo [ERROR] Push to GitHub failed. Please check network connection or account permissions.
    goto END
)

echo.
echo ============================================================
echo   [SUCCESS] Deployment pushed successfully!
if defined REPO_OWNER (
    echo   Repository:   https://github.com/!REPO_OWNER!/!REPO_NAME!
    echo   GitHub Pages: https://!REPO_OWNER!.github.io/!REPO_NAME!/
)
echo ============================================================

:END
echo.
pause
