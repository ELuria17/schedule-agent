@echo off
REM Windows launcher for AutoPlan.
REM Place a shortcut to this file in your Startup folder to have AutoPlan
REM run automatically when you sign in.

setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Virtual environment not found. Running first-time setup...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

python run.py
pause
