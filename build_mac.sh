#!/bin/bash
# build_mac.sh — Build Mercari Inventory as a Mac desktop app.
#
# Produces:
#   dist/MercariInventory/           PyInstaller bundle (all binaries + deps)
#   dist/MercariInventory.command    Double-clickable launcher (opens in Terminal)
#
# Optional signing (set env vars before running):
#   SIGN_IDENTITY  — "Developer ID Application: Your Name (TEAMID)"
#                    If unset, ad-hoc signing is applied (fixes Killed:9 locally).
#   NOTARIZE       — set to "1" to submit to Apple notarization service
#   NOTARIZE_PROFILE — keychain profile name created via:
#                       xcrun notarytool store-credentials ...
#                      (only needed when NOTARIZE=1)
#
# Usage:
#   chmod +x build_mac.sh
#   ./build_mac.sh
#
# See SIGNING.md for full Developer ID signing + notarization setup.

set -euo pipefail

APP_NAME="MercariInventory"
ENTRY="main.py"
SIGN_IDENTITY="${SIGN_IDENTITY:-}"
NOTARIZE="${NOTARIZE:-0}"
NOTARIZE_PROFILE="${NOTARIZE_PROFILE:-notarytool-profile}"

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

# Resolve the selenium-manager binary path at build time.
# --collect-data "selenium" includes it as a data file but does NOT preserve
# the execute bit. Adding it via --add-binary ensures +x is preserved so
# Selenium Manager can launch chromedriver without a Permission denied error.
SM_BIN="$(python3 -c "
import selenium, os
print(os.path.join(os.path.dirname(selenium.__file__), 'webdriver', 'common', 'macos', 'selenium-manager'))
")"
if [ ! -f "${SM_BIN}" ]; then
    echo "Error: selenium-manager binary not found at: ${SM_BIN}"
    exit 1
fi
echo "✓ selenium-manager: ${SM_BIN}"

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
    `# Bundle selenium-manager as a binary so execute permissions are preserved` \
    --add-binary "${SM_BIN}:selenium/webdriver/common/macos/" \
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
#
# The launcher also strips com.apple.quarantine on first run.
# macOS sets this xattr on every file inside a downloaded zip, which causes
# unsigned PyInstaller binaries to be killed with SIGKILL (Killed: 9) on
# Apple Silicon. The .command itself passes the one-time Gatekeeper dialog,
# so after the user clicks "Open", this script is allowed to clean up the
# quarantine from the entire bundle.

LAUNCHER="dist/${APP_NAME}.command"

cat > "${LAUNCHER}" << LAUNCHER_SCRIPT
#!/bin/bash
# Mercari Inventory — double-click to launch.
# macOS opens .command files in Terminal.app automatically.

SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"

# Strip quarantine xattr set by macOS on downloaded files.
# Prevents "Killed: 9" on Apple Silicon for unsigned PyInstaller binaries.
# Safe here because the user already approved this .command via Gatekeeper.
if xattr -p com.apple.quarantine "\${SCRIPT_DIR}/${APP_NAME}/${APP_NAME}" 2>/dev/null | grep -q .; then
    echo "[setup] Removing macOS download quarantine (first run only)..."
    xattr -dr com.apple.quarantine "\${SCRIPT_DIR}" 2>/dev/null || true
fi

cd "\${SCRIPT_DIR}/${APP_NAME}"
./MercariInventory
LAUNCHER_SCRIPT

chmod +x "${LAUNCHER}"

# ── Code signing ──────────────────────────────────────────────────────────────
# Always apply at least ad-hoc signing to the main executable.
# Ad-hoc signing (-) fixes Killed:9 when the binary is run on the SAME machine.
# For distributed builds, set SIGN_IDENTITY to your Developer ID certificate.

BINARY="dist/${APP_NAME}/${APP_NAME}"

if [ -n "${SIGN_IDENTITY}" ]; then
    echo ""
    echo "Signing with Developer ID: ${SIGN_IDENTITY}"

    # Sign all bundled dylibs and .so files first (leaf nodes before the root).
    find "dist/${APP_NAME}" \( -name "*.dylib" -o -name "*.so" \) -print0 \
        | xargs -0 -I{} codesign --sign "${SIGN_IDENTITY}" --force --options runtime "{}" 2>/dev/null || true

    # Sign the main executable with entitlements (required for hardened runtime).
    ENTITLEMENTS="entitlements.plist"
    if [ ! -f "${ENTITLEMENTS}" ]; then
        echo "  ⚠  entitlements.plist not found — signing without entitlements"
        codesign --sign "${SIGN_IDENTITY}" --force --options runtime "${BINARY}"
    else
        codesign --sign "${SIGN_IDENTITY}" --force --options runtime \
            --entitlements "${ENTITLEMENTS}" "${BINARY}"
    fi

    echo "✓ Signed (Developer ID)"

    # ── Notarization ─────────────────────────────────────────────────────────
    if [ "${NOTARIZE}" = "1" ]; then
        echo "Submitting to Apple notarization service (this takes a few minutes)..."
        NOTARIZE_ZIP="notarize_submit.zip"
        zip -qr "${NOTARIZE_ZIP}" "dist/${APP_NAME}/"
        xcrun notarytool submit "${NOTARIZE_ZIP}" \
            --keychain-profile "${NOTARIZE_PROFILE}" \
            --wait
        rm -f "${NOTARIZE_ZIP}"
        xcrun stapler staple "${BINARY}"
        echo "✓ Notarized and stapled"
    fi

else
    # Ad-hoc sign the main binary only.
    # This alone does NOT satisfy Gatekeeper for downloaded builds, but it
    # prevents Killed:9 when running on the same machine (e.g., local dev builds).
    codesign --sign - --force "${BINARY}" 2>/dev/null || true
    echo "✓ Ad-hoc signed (dev build — see SIGNING.md for distribution signing)"
fi

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
echo "NOTE: ChromeDriver is managed automatically by Selenium Manager."
echo "      Only Google Chrome must be installed on the user's machine."
echo "      https://www.google.com/chrome/"
echo ""
if [ -z "${SIGN_IDENTITY}" ]; then
    echo "NOTE: This is an unsigned (ad-hoc) build."
    echo "      Downloaded builds may trigger Gatekeeper on other Macs."
    echo "      See SIGNING.md for Developer ID signing + notarization setup."
    echo ""
fi
