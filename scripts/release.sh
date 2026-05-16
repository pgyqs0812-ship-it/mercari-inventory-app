#!/usr/bin/env bash
# release.sh — Sign + package + notarize + verify pipeline.
#
# Run AFTER build_mac.sh has produced dist/MIAInventory.app.
#
# ── Prerequisites ────────────────────────────────────────────────────────────
#   1. Run ./build_mac.sh to produce dist/MIAInventory.app
#   2. Run ./scripts/setup_notarytool.sh once to store credentials in Keychain
#
# ── Usage ────────────────────────────────────────────────────────────────────
#   ./scripts/release.sh
#
# ── Environment variables ────────────────────────────────────────────────────
#   SIGN_IDENTITY     codesign identity  (default: Developer ID Application: YANSEN PENG)
#   NOTARIZE_PROFILE  Keychain profile   (default: notarytool-profile)
#   VERSION           version tag        (default: latest git tag)
#   APP_BUNDLE        path to .app       (default: dist/MIAInventory.app)
#   SKIP_NOTARIZE     set to 1 to skip notarization (sign + package only)
#
# ── Example ──────────────────────────────────────────────────────────────────
#   VERSION=v1.7.0 ./scripts/release.sh
#   SKIP_NOTARIZE=1 ./scripts/release.sh     # sign + DMG only, no notarization

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_DIR}"

APP_BUNDLE="${APP_BUNDLE:-dist/MIAInventory.app}"
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: YANSEN PENG}"
NOTARIZE_PROFILE="${NOTARIZE_PROFILE:-notarytool-profile}"
VERSION="${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)}"
SKIP_NOTARIZE="${SKIP_NOTARIZE:-0}"
ENTITLEMENTS="${ENTITLEMENTS:-entitlements.plist}"

DMG_NAME="MIAInventory_Mac_${VERSION}.dmg"
DMG_PATH="dist/${DMG_NAME}"

echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║   MIA Inventory — Sign + Package + Notarize       ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""
printf "  Version       : %s\n" "${VERSION}"
printf "  App bundle    : %s\n" "${APP_BUNDLE}"
printf "  Identity      : %s\n" "${SIGN_IDENTITY}"
printf "  Keychain prof : %s\n" "${NOTARIZE_PROFILE}"
printf "  Notarize      : %s\n" "$([[ "${SKIP_NOTARIZE}" == "1" ]] && echo "SKIPPED" || echo "YES")"
echo ""

# ── Prerequisite checks ───────────────────────────────────────────────────────
if [[ ! -d "${APP_BUNDLE}" ]]; then
    echo "Error: ${APP_BUNDLE} not found." >&2
    echo "Run ./build_mac.sh first to produce the .app bundle." >&2
    exit 1
fi
if [[ ! -f "${ENTITLEMENTS}" ]]; then
    echo "Error: ${ENTITLEMENTS} not found." >&2
    exit 1
fi

# Verify the signing identity is available in Keychain
if ! security find-identity -v -p codesigning 2>/dev/null | grep -qF "${SIGN_IDENTITY}"; then
    echo "Error: Signing identity not found in Keychain:" >&2
    printf "  %s\n" "${SIGN_IDENTITY}" >&2
    echo "" >&2
    echo "Available identities:" >&2
    security find-identity -v -p codesigning >&2
    exit 1
fi

START_TIME=$(date +%s)

# ── Step 1/4: Sign .app ───────────────────────────────────────────────────────
echo "════ Step 1/4: Sign .app ════════════════════════════════════"
APP_BUNDLE="${APP_BUNDLE}" \
SIGN_IDENTITY="${SIGN_IDENTITY}" \
ENTITLEMENTS="${ENTITLEMENTS}" \
    "${SCRIPT_DIR}/sign_app.sh" "${APP_BUNDLE}"

# ── Step 2/4: Package DMG ─────────────────────────────────────────────────────
echo "════ Step 2/4: Package DMG ══════════════════════════════════"
APP_BUNDLE="${APP_BUNDLE}" \
VERSION="${VERSION}" \
SIGN_IDENTITY="${SIGN_IDENTITY}" \
    "${SCRIPT_DIR}/package_dmg.sh" "${APP_BUNDLE}" "${VERSION}" "${SIGN_IDENTITY}" \
    | grep -v "^DMG_PATH=" || true   # suppress the export line in pipeline output

# ── Step 3/4: Notarize DMG ────────────────────────────────────────────────────
if [[ "${SKIP_NOTARIZE}" == "1" ]]; then
    echo "════ Step 3/4: Notarize DMG — SKIPPED ══════════════════════"
    echo ""
else
    echo "════ Step 3/4: Notarize DMG ═════════════════════════════════"
    DMG_PATH="${DMG_PATH}" \
    NOTARIZE_PROFILE="${NOTARIZE_PROFILE}" \
        "${SCRIPT_DIR}/notarize_dmg.sh" "${DMG_PATH}" "${NOTARIZE_PROFILE}"
fi

# ── Step 4/4: Verify ──────────────────────────────────────────────────────────
echo "════ Step 4/4: Verify ═══════════════════════════════════════"
if [[ "${SKIP_NOTARIZE}" == "1" ]]; then
    "${SCRIPT_DIR}/verify.sh" "${APP_BUNDLE}" ""
else
    "${SCRIPT_DIR}/verify.sh" "${APP_BUNDLE}" "${DMG_PATH}"
fi

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo "╔════════════════════════════════════════════════════╗"
echo "║   Release pipeline complete                       ║"
echo "╠════════════════════════════════════════════════════╣"
printf "║  DMG     : %-38s║\n" "${DMG_NAME}"
printf "║  Elapsed : %-38s║\n" "${ELAPSED}s"
echo "╚════════════════════════════════════════════════════╝"
echo ""
