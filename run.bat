@echo off
rem Run Codex Context Tool from source.
rem First run creates .venv next to this file and installs PySide6 into it.
setlocal
cd /d "%~dp0"

set "CCT_PYTHON=%~dp0.venv\Scripts\python.exe"
rem pythonw.exe = same interpreter without a console window.
set "CCT_PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%CCT_PYTHON%" goto check_deps

echo [setup] Creating virtual environment in .venv ...
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist "%CCT_PYTHON%" (
    echo [error] Failed to create .venv. Python 3.11 or newer is required.
    pause
    exit /b 1
)

:check_deps
"%CCT_PYTHON%" -c "import sys, PySide6; sys.version_info >= (3, 14) or __import__('zstandard')" >nul 2>nul
if not errorlevel 1 goto run
echo [setup] Installing dependencies from requirements.txt ...
"%CCT_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [error] Failed to install dependencies.
    pause
    exit /b 1
)

:run
rem Start the GUI detached so this console closes at once. Startup errors are
rem shown in a message box by main.py, because pythonw has no console.
start "" "%CCT_PYTHONW%" main.py %*
