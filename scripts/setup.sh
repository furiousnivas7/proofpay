#!/usr/bin/env bash
# One-time local setup: virtual environment, packages, .env, database, tests.
# Usage:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "Creating the virtual environment in .venv"
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "Installing packages"
python -m pip install -q --upgrade pip
pip install -q -r requirements-dev.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add your PayPal sandbox client ID and secret to it."
fi

echo "Creating or updating the database"
alembic upgrade head

echo "Running the unit tests"
pytest -q

echo
echo "Setup done. Next:"
echo "  source .venv/bin/activate"
echo "  uvicorn proofpay.main:app --reload      # then open http://localhost:8000/docs"
