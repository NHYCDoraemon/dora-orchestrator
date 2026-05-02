#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/NHYCDoraemon/dora-orchestrator.git"

echo "==> dora-orchestrator installer"
echo ""

# --- Python check ---
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required but not found. Install Python >= 3.10 first."
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  python3: ${PY_VERSION}"

MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
    echo "ERROR: python >= 3.10 required, found ${PY_VERSION}"
    exit 1
fi

# --- Install ---
if command -v pipx &>/dev/null; then
    echo "  installer: pipx"
    pipx install --force "git+${REPO_URL}"
else
    echo "  installer: pip (pipx not found)"
    echo "  NOTE: install pipx for isolated environments: https://pipx.pypa.io/"
    python3 -m pip install --user --break-system-packages "git+${REPO_URL}"
fi

echo ""

# --- Verify ---
if command -v orchestrator &>/dev/null; then
    echo "==> orchestrator installed successfully"
    orchestrator --help
else
    echo "==> installed but 'orchestrator' not on PATH — you may need to restart your shell"
    echo "    or add ~/.local/bin to your PATH."
fi
