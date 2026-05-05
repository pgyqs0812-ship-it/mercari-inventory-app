#!/bin/bash
# build_mac.sh — Build Mercari Inventory as a macOS .app bundle + DMG installer.
#
# Produces:
#   dist/MIAInventory.app                Standard macOS .app bundle
#   dist/MIAInventory_Mac_<version>.dmg  Drag-and-drop DMG installer
#
# Optional signing (set env vars before running):
#   SIGN_IDENTITY  — "Developer ID Application: Your Name (TEAMID)"
#                    If unset, ad-hoc signing is applied (fixes Killed:9 locally).
#   NOTARIZE       — set to "1" to submit to Apple notarization service
#   NOTARIZE_PROFILE — keychain profile name created via:
#                       xcrun notarytool store-credentials ...
#                      (only needed when NOTARIZE=1)
#   VERSION        — override the version tag (default: latest git tag)
#
# Usage:
#   chmod +x build_mac.sh
#   ./build_mac.sh
#
# See SIGNING.md for full Developer ID signing + notarization setup.

set -euo pipefail

APP_NAME="MIAInventory"
ENTRY="main.py"
SIGN_IDENTITY="${SIGN_IDENTITY:-}"
NOTARIZE="${NOTARIZE:-0}"
NOTARIZE_PROFILE="${NOTARIZE_PROFILE:-notarytool-profile}"
VERSION="${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)}"

APP_BUNDLE="dist/${APP_NAME}.app"
DMG_NAME="MIAInventory_Mac_${VERSION}.dmg"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Mercari Inventory — Mac Build Tool         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Version : ${VERSION}"
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

# ── Install build dependencies ────────────────────────────────────────────────
if ! python3 -c "import PyInstaller" &>/dev/null; then
    echo "Installing PyInstaller..."
    pip install --quiet pyinstaller
fi
echo "✓ PyInstaller $(python3 -c "import PyInstaller; print(PyInstaller.__version__)")"

if ! python3 -c "import PIL" &>/dev/null; then
    echo "Installing Pillow..."
    pip install --quiet pillow
fi

if ! python3 -c "import dmgbuild" &>/dev/null; then
    echo "Installing dmgbuild..."
    pip install --quiet dmgbuild
fi

# ── Check entry point ─────────────────────────────────────────────────────────
if [ ! -f "${ENTRY}" ]; then
    echo "Error: ${ENTRY} not found. Run this script from the project root."
    exit 1
fi

# ── Clean previous build ──────────────────────────────────────────────────────
echo "Cleaning previous build artifacts..."
rm -rf build/ dist/ "${APP_NAME}.spec"

# ── App icon ──────────────────────────────────────────────────────────────────
echo "Generating app icon..."
python3 create_icon.py
echo "✓ AppIcon.icns"

# ── selenium-manager binary path ─────────────────────────────────────────────
# Resolved at build time so execute permissions are preserved inside the bundle.
SM_BIN="$(python3 -c "
import selenium, os
print(os.path.join(os.path.dirname(selenium.__file__), 'webdriver', 'common', 'macos', 'selenium-manager'))
")"
if [ ! -f "${SM_BIN}" ]; then
    echo "Error: selenium-manager binary not found at: ${SM_BIN}"
    exit 1
fi
echo "✓ selenium-manager: ${SM_BIN}"

# ── PyInstaller build ─────────────────────────────────────────────────────────
echo "Building ${APP_NAME}.app (this may take a minute)..."
echo ""

pyinstaller \
    --name "${APP_NAME}" \
    --onedir \
    --windowed \
    --icon "AppIcon.icns" \
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
    `# openpyxl (used by /export/xlsx route)` \
    --hidden-import "openpyxl" \
    --collect-submodules "openpyxl" \
    \
    `# python-dotenv` \
    --hidden-import "dotenv" \
    \
    `# Standard library modules PyInstaller sometimes misses` \
    --hidden-import "sqlite3" \
    --hidden-import "queue" \
    --hidden-import "concurrent.futures" \
    \
    "${ENTRY}"

# ── Code signing (.app) ───────────────────────────────────────────────────────
# --windowed produces dist/MIAInventory.app — sign the whole bundle.
# --deep signs the top-level bundle and all nested binaries/frameworks in one pass.

if [ -n "${SIGN_IDENTITY}" ]; then
    echo ""
    echo "Signing .app with Developer ID: ${SIGN_IDENTITY}"

    ENTITLEMENTS="entitlements.plist"
    SIGN_ARGS=(--sign "${SIGN_IDENTITY}" --force --options runtime)
    if [ -f "${ENTITLEMENTS}" ]; then
        SIGN_ARGS+=(--entitlements "${ENTITLEMENTS}")
    else
        echo "  ⚠  entitlements.plist not found — signing without entitlements"
    fi

    codesign "${SIGN_ARGS[@]}" --deep "${APP_BUNDLE}"
    echo "✓ .app signed (Developer ID)"

    # ── Notarization ─────────────────────────────────────────────────────────
    if [ "${NOTARIZE}" = "1" ]; then
        echo "Submitting .app to Apple notarization service (this takes a few minutes)..."
        NOTARIZE_ZIP="notarize_submit.zip"
        ditto -c -k --keepParent "${APP_BUNDLE}" "${NOTARIZE_ZIP}"
        xcrun notarytool submit "${NOTARIZE_ZIP}" \
            --keychain-profile "${NOTARIZE_PROFILE}" \
            --wait
        rm -f "${NOTARIZE_ZIP}"
        xcrun stapler staple "${APP_BUNDLE}"
        echo "✓ .app notarized and stapled"
    fi

else
    codesign --sign - --force --deep "${APP_BUNDLE}" 2>/dev/null || true
    echo "✓ .app ad-hoc signed (dev build — see SIGNING.md for distribution signing)"
fi

# ── DMG background image ──────────────────────────────────────────────────────
echo ""
echo "Generating DMG background..."
VERSION="${VERSION}" python3 create_dmg_bg.py

# ── DMG creation ─────────────────────────────────────────────────────────────
echo "Creating DMG installer: dist/${DMG_NAME} ..."
dmgbuild \
    -s dmgbuild_settings.py \
    -D app_path="${APP_BUNDLE}" \
    -D bg_path="dmg_background.png" \
    "MIA Inventory Installer" \
    "dist/${DMG_NAME}"

# ── DMG signing ──────────────────────────────────────────────────────────────
if [ -n "${SIGN_IDENTITY}" ]; then
    codesign --sign "${SIGN_IDENTITY}" --force "dist/${DMG_NAME}"
    echo "✓ DMG signed (Developer ID)"
else
    codesign --sign - --force "dist/${DMG_NAME}" 2>/dev/null || true
    echo "✓ DMG ad-hoc signed"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Build complete!                            ║"
echo "╠══════════════════════════════════════════════╣"
echo "║                                              ║"
printf "║  App bundle : dist/%-25s║\n" "${APP_NAME}.app"
printf "║  DMG        : dist/%-25s║\n" "${DMG_NAME}"
echo "║                                              ║"
echo "║  Install:                                    ║"
printf "║    open dist/%-31s║\n" "${DMG_NAME}"
echo "║                                              ║"
echo "║  Or launch directly:                         ║"
printf "║    open dist/%-31s║\n" "${APP_NAME}.app"
echo "║                                              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "NOTE: ChromeDriver is managed automatically by Selenium Manager."
echo "      Only Google Chrome must be installed on the user's machine."
echo "      https://www.google.com/chrome/"
echo ""
if [ -z "${SIGN_IDENTITY}" ]; then
    echo "NOTE: This is an unsigned (ad-hoc) build."
    echo "      Right-click → Open on first launch for Gatekeeper bypass."
    echo "      See SIGNING.md for Developer ID signing + notarization setup."
    echo ""
fi
