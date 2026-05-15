#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo "[ERROR] .env file is missing. Copy .env.example to .env first."
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "[ERROR] virtualenv not found. Run ./install.sh first."
  exit 1
fi

source .venv/bin/activate
exec python bot.py
