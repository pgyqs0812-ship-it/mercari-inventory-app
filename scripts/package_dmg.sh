#!/usr/bin/env bash
# package_dmg.sh — Create and sign a DMG installer from a built .app bundle.
#
# Usage:
#   ./scripts/package_dmg.sh [app_bundle] [version] [sign_identity]
#
# Environment variables (or positional args):
#   APP_BUNDLE     — path to .app  (default: dist/MIAInventory.app)
#   VERSION        — version tag   (default: latest git tag)
#   SIGN_IDENTITY  — codesign id   (default: Developer ID Application: YANSEN PENG)
#
# Produces:
#   dist/MIAInventory_Mac_<VERSION>.dmg   (signed)
#
# Requires: dmgbuild, Pillow (installed by build_mac.sh)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_DIR}"

APP_BUNDLE="${1:-${APP_BUNDLE:-dist/MIAInventory.app}}"
VERSION="${2:-${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)}}"
SIGN_IDENTITY="${3:-${SIGN_IDENTITY:-Developer ID Application: YANSEN PENG}}"

DMG_NAME="MIAInventory_Mac_${VERSION}.dmg"
DMG_PATH="dist/${DMG_NAME}"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ ! -d "${APP_BUNDLE}" ]]; then
    echo "Error: App bundle not found: ${APP_BUNDLE}" >&2
    echo "Run ./build_mac.sh first, then re-run this script." >&2
    exit 1
fi

echo ""
echo "━━━ Package DMG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  App      : ${APP_BUNDLE}"
echo "  Version  : ${VERSION}"
echo "  Output   : ${DMG_PATH}"
echo ""

# ── DMG background ───────────────────────────────────────────────────────────
echo "  [1/3] Generating DMG background..."
python3 create_dmg_bg.py
echo "        dmg_background.png ready"

# ── Build DMG ────────────────────────────────────────────────────────────────
echo "  [2/3] Building DMG with dmgbuild..."
mkdir -p dist
dmgbuild \
    -s dmgbuild_settings.py \
    -D app_path="${APP_BUNDLE}" \
    -D bg_path="dmg_background.png" \
    "MIA Inventory Installer ${VERSION}" \
    "${DMG_PATH}"
echo "        ${DMG_PATH} created"

# ── Sign DMG ─────────────────────────────────────────────────────────────────
echo "  [3/3] Signing DMG..."
codesign \
    --sign     "${SIGN_IDENTITY}" \
    --force \
    --timestamp \
    "${DMG_PATH}"
echo ""
echo "  ✓ ${DMG_PATH} signed"
echo ""

# Export so callers (release.sh) can capture it
echo "DMG_PATH=${DMG_PATH}"
