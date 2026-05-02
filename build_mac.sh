#!/bin/bash
# build_mac.sh — Build Mercari Inventory as a Mac desktop app.
#
# Produces:
#   dist/MercariInventory/           PyInstaller bundle (all binaries + deps)
#   dist/MercariInventory.command    Double-clickable launcher (opens in Terminal)
#
# Usage:
#   chmod +x build_mac.sh
#   ./build_mac.sh

set -euo pipefail

APP_NAME="MercariInventory"
ENTRY="main.py"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Mercari Inventory — Mac Build Tool         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Python / venv ─────────────────────────────────────────────────────────────
if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
    echo "✓ venv activated: $(python3 --version)"
else
    echo "  (no venv found — using system Python: $(python3 --version))"
fi

# Warn if Python ≥ 3.13 since PyInstaller's official support lags behind.
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
if [ "${PY_MAJOR}" -eq 3 ] && [ "${PY_MINOR}" -ge 13 ]; then
    echo ""
    echo "  ⚠  Python 3.${PY_MINOR} detected."
    echo "     PyInstaller officially supports up to Python 3.12."
    echo "     If the build fails, create a Python 3.12 venv and retry:"
    echo "       python3.12 -m venv venv312 && source venv312/bin/activate"
    echo "       pip install -r requirements.txt pyinstaller"
    echo "       ./build_mac.sh"
    echo ""
fi

# ── Install PyInstaller ───────────────────────────────────────────────────────
if ! python3 -c "import PyInstaller" &>/dev/null; then
    echo "Installing PyInstaller..."
    pip install --quiet pyinstaller
fi
echo "✓ PyInstaller $(python3 -c "import PyInstaller; print(PyInstaller.__version__)")"

# ── Check entry point ─────────────────────────────────────────────────────────
if [ ! -f "${ENTRY}" ]; then
    echo "Error: ${ENTRY} not found. Run this script from the project root."
    exit 1
fi

# ── Clean previous build ──────────────────────────────────────────────────────
echo "Cleaning previous build artifacts..."
rm -rf build/ dist/ "${APP_NAME}.spec"

# ── PyInstaller build ─────────────────────────────────────────────────────────
echo "Building ${APP_NAME} (this may take a minute)..."
echo ""

pyinstaller \
    --name "${APP_NAME}" \
    --onedir \
    --console \
    --noconfirm \
    \
    `# Flask and its runtime deps` \
    --hidden-import "flask" \
    --hidden-import "flask.logging" \
    --hidden-import "werkzeug" \
    --hidden-import "werkzeug.serving" \
    --hidden-import "werkzeug.debug" \
    --hidden-import "jinja2" \
    --hidden-import "markupsafe" \
    --hidden-import "click" \
    --collect-submodules "flask" \
    \
    `# Selenium 4 — dynamically imports many submodules` \
    --hidden-import "selenium.webdriver.chrome" \
    --hidden-import "selenium.webdriver.chrome.service" \
    --hidden-import "selenium.webdriver.chrome.options" \
    --hidden-import "selenium.webdriver.chrome.webdriver" \
    --hidden-import "selenium.webdriver.support" \
    --hidden-import "selenium.webdriver.support.ui" \
    --hidden-import "selenium.webdriver.support.expected_conditions" \
    --hidden-import "selenium.webdriver.common.by" \
    --hidden-import "selenium.webdriver.remote.webdriver" \
    --collect-submodules "selenium" \
    --collect-data "selenium" \
    \
    `# python-dotenv (used by create_jira_ticket.py)` \
    --hidden-import "dotenv" \
    \
    `# Standard library modules PyInstaller sometimes misses` \
    --hidden-import "sqlite3" \
    --hidden-import "queue" \
    --hidden-import "concurrent.futures" \
    \
    "${ENTRY}"

# ── Double-clickable launcher ─────────────────────────────────────────────────
# When a .command file is double-clicked in Finder, macOS opens it in
# Terminal.app — giving the user a visible window for Mercari login prompts.

LAUNCHER="dist/${APP_NAME}.command"

cat > "${LAUNCHER}" << LAUNCHER_SCRIPT
#!/bin/bash
# Mercari Inventory — double-click to launch.
# macOS opens .command files in Terminal.app automatically.
SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
cd "\${SCRIPT_DIR}/${APP_NAME}"
./MercariInventory
LAUNCHER_SCRIPT

chmod +x "${LAUNCHER}"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Build complete!                            ║"
echo "╠══════════════════════════════════════════════╣"
echo "║                                              ║"
printf "║  Bundle:   dist/%-28s║\n" "${APP_NAME}/"
printf "║  Launcher: dist/%-28s║\n" "${APP_NAME}.command"
echo "║                                              ║"
echo "║  Launch (double-click):                      ║"
printf "║    dist/%-36s║\n" "${APP_NAME}.command"
echo "║                                              ║"
echo "║  Launch from Terminal:                       ║"
printf "║    ./dist/%s/%s  %-10s║\n" "${APP_NAME}" "${APP_NAME}" ""
echo "║                                              ║"
echo "║  Distribute:                                 ║"
echo "║    zip -r MercariInventory.zip dist/         ║"
echo "║                                              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "NOTE: ChromeDriver must be installed and on PATH."
echo "      Install via Homebrew: brew install chromedriver"
echo "      Then allow it in: System Settings → Privacy → Security"
echo ""
