#!/usr/bin/env bash
# macOS / Linux launcher. Then open http://localhost:8000
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo
  echo "No .env file found."
  echo "Copy .env.example to .env and paste your Massive API key into it."
  echo "Get a key at https://massive.com/dashboard/keys"
  exit 1
fi

echo
echo "Starting Options Lens at http://localhost:8000  (Ctrl+C to stop)"
echo
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
