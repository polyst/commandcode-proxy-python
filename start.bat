@echo off
rem Start the CommandCode proxy. Double-click to run, or run from a terminal.
rem Anything you type after the script name is passed straight to the server:
rem   start.bat --port 8080
rem   start.bat --debug
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

echo.
echo [run] Starting CommandCode Proxy - press Ctrl+C to stop
echo.

"%PY%" -m commandcode_proxy.main %*
