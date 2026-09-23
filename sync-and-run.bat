@echo off
rem Refresh the ZCode provider config from models.json, then start the proxy.
rem   sync-and-run.bat             sync (skip when models.json is unchanged), then start
rem   sync-and-run.bat --sync-only refresh the ZCode config and exit
rem   sync-and-run.bat --start-only skip the sync and just start the proxy
rem   sync-and-run.bat --debug     anything left is passed straight to the server
setlocal EnableDelayedExpansion
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo         Install it from https://www.python.org/downloads/ and try again.
    exit /b 1
)

if not exist ".env" (echo [WARN] .env not found - copy .env.example to .env and set API_KEY, or pass --api-key)

if not exist ".venv\Scripts\python.exe" (
    echo [setup] First run - creating a virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (echo [ERROR] Could not create the virtual environment. & exit /b 1)
)

set "PY=%~dp0.venv\Scripts\python.exe"

"%PY%" -c "import fastapi, uvicorn, httpx" >nul 2>nul
if errorlevel 1 (
    echo [setup] Installing dependencies ...
    "%PY%" -m pip install --disable-pip-version-check -q fastapi uvicorn httpx
    if errorlevel 1 (echo [ERROR] Dependency install failed. & exit /b 1)
)

set "MODE=full"
set "PUSHED="
if /i "%~1"=="--sync-only" ( set "MODE=sync" & set "PUSHED=1" )
if /i "%~1"=="--start-only" ( set "MODE=start" & set "PUSHED=1" )

if not "%MODE%"=="start" (
    echo.
    echo [sync] Refreshing the ZCode provider config from models.json ...
    "%PY%" install_to_zcode.py --if-changed
    if errorlevel 1 echo [WARN] sync failed - continuing with the proxy anyway
    if not "%MODE%"=="sync" echo        Restart ZCode to pick up any config change.
)

if "%MODE%"=="sync" exit /b 0

echo.
echo [run] Starting CommandCode Proxy - press Ctrl+C to stop
echo.

if defined PUSHED shift
"%PY%" -m commandcode_proxy.main %*
