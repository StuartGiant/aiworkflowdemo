#!/usr/bin/env bash
# setup_venv.sh — create and populate the project virtual environment.
#
# Run once from the project root:
#   chmod +x setup_venv.sh
#   ./setup_venv.sh
#
# After this completes, activate the venv in any shell with:
#   source venv/bin/activate
# ---------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Require Python 3.10+
PYTHON=$(command -v python3 || true)
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: python3 not found on PATH. Install Python 3.10+ and retry." >&2
  exit 1
fi

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED_MINOR=10
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MINOR" -lt "$REQUIRED_MINOR" ]]; then
  echo "ERROR: Python $PY_VERSION found; 3.$REQUIRED_MINOR+ required." >&2
  exit 1
fi

echo "Using Python $PY_VERSION at $PYTHON"

# Create venv if it doesn't exist; recreate if --clean is passed.
if [[ "${1:-}" == "--clean" ]] && [[ -d venv ]]; then
  echo "Removing existing venv (--clean requested)..."
  rm -rf venv
fi

if [[ ! -d venv ]]; then
  echo "Creating virtual environment at ./venv ..."
  "$PYTHON" -m venv venv
fi

# Activate
# shellcheck disable=SC1091
source venv/bin/activate

echo "Upgrading pip..."
pip install --upgrade pip --quiet

echo "Installing dependencies from requirements.txt..."
pip install -r requirements.txt

echo ""
echo "============================================================"
echo "Virtual environment ready."
echo "Activate with:  source venv/bin/activate"
echo "Deactivate with: deactivate"
echo "============================================================"
