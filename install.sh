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
NEEDS_PATH_HINT=0
if command -v pipx &>/dev/null; then
    echo "  installer: pipx"
    pipx install --force "git+${REPO_URL}"
else
    echo "  installer: pip (pipx not found)"
    echo "  NOTE: install pipx for isolated environments: https://pipx.pypa.io/"
    python3 -m pip install --user --break-system-packages --quiet "git+${REPO_URL}"
    NEEDS_PATH_HINT=1
fi

echo ""

# --- Verify ---
if command -v orchestrator &>/dev/null; then
    echo "==> orchestrator installed successfully"
    orchestrator --help
elif [ "$NEEDS_PATH_HINT" -eq 1 ]; then
    SCRIPTS_DIR=$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts", vars={"base": sysconfig.get_path("userbase")}))' 2>/dev/null || echo "")
    if [ -n "$SCRIPTS_DIR" ] && [ -x "$SCRIPTS_DIR/orchestrator" ]; then
        echo "==> orchestrator installed to ${SCRIPTS_DIR}"
        echo "    Add it to your PATH:"
        echo ""
        if [ -f "$HOME/.zshrc" ]; then
            echo "    echo 'export PATH=\"${SCRIPTS_DIR}:\$PATH\"' >> ~/.zshrc"
        elif [ -f "$HOME/.bashrc" ]; then
            echo "    echo 'export PATH=\"${SCRIPTS_DIR}:\$PATH\"' >> ~/.bashrc"
        else
            echo "    export PATH=\"${SCRIPTS_DIR}:\$PATH\""
        fi
    else
        echo "==> installed but binary location unknown — try: pipx install git+${REPO_URL}"
    fi
else
    echo "==> installed — run 'orchestrator --help' to verify"
fi
