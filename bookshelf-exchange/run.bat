@echo off
rem One-command start for Windows: double-click run.bat
setlocal enabledelayedexpansion
cd /d %~dp0

if not exist .venv (
    echo First run: setting up Python environment...
    py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\pip install -q -r requirements.txt

if exist .env goto run
if not "%ANTHROPIC_API_KEY%"=="" goto run
echo.
echo AI photo scanning needs a Claude API key (platform.claude.com).
set /p KEY="Paste your API key (or press Enter to skip for now): "
if not "!KEY!"=="" (
    echo ANTHROPIC_API_KEY=!KEY!> .env
    echo Saved to .env - it will be remembered next time.
)

:run
echo.
echo Starting Bookshelf Exchange at http://localhost:5000  (Ctrl+C to stop)
.venv\Scripts\python app.py
pause
