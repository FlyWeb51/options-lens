@echo off
REM Windows launcher. Double-click this file, then open http://localhost:8000

cd /d "%~dp0"

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -q -r requirements.txt

if not exist .env (
  echo.
  echo ================================================================
  echo  No .env file found.
  echo  Copy .env.example to .env and paste your Massive API key in it.
  echo  Get a key at https://massive.com/dashboard/keys
  echo ================================================================
  echo.
  pause
  exit /b 1
)

echo.
echo Starting Options Lens at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
