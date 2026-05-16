#!/usr/bin/env bash
# notarize_dmg.sh — Submit a signed DMG to Apple notarization, then staple.
#
# Notarizes the DMG (not the .app) so the distribution artifact carries a
# stapled ticket that satisfies Gatekeeper offline verification.
#
# Prerequisite — run ONCE to store credentials in Keychain:
#   ./scripts/setup_notarytool.sh
#
# Usage:
#   ./scripts/notarize_dmg.sh <dmg_path> [profile_name]
#
# Environment variables (or positional args):
#   DMG_PATH          — path to signed DMG
#   NOTARIZE_PROFILE  — keychain profile (default: notarytool-profile)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_DIR}"

DMG_PATH="${1:-${DMG_PATH:-}}"
NOTARIZE_PROFILE="${2:-${NOTARIZE_PROFILE:-notarytool-profile}}"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ -z "${DMG_PATH}" ]]; then
    echo "Usage: $0 <dmg_path> [profile_name]" >&2
    exit 1
fi
if [[ ! -f "${DMG_PATH}" ]]; then
    echo "Error: DMG not found: ${DMG_PATH}" >&2
    exit 1
fi

echo ""
echo "━━━ Notarize DMG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DMG     : ${DMG_PATH}"
echo "  Profile : ${NOTARIZE_PROFILE}"
echo ""
echo "  Note: this step requires internet access to connect to Apple's"
echo "  notarization service. It typically takes 2–10 minutes."
echo ""

# ── Submit + wait ─────────────────────────────────────────────────────────────
echo "  [1/3] Submitting to Apple notarization service..."
xcrun notarytool submit "${DMG_PATH}" \
    --keychain-profile "${NOTARIZE_PROFILE}" \
    --wait \
    --verbose

echo ""

# ── Staple ────────────────────────────────────────────────────────────────────
echo "  [2/3] Stapling notarization ticket to DMG..."
xcrun stapler staple "${DMG_PATH}"
echo "        Ticket stapled"

# ── Validate staple ───────────────────────────────────────────────────────────
echo "  [3/3] Validating stapled ticket..."
xcrun stapler validate "${DMG_PATH}"

echo ""
echo "  ✓ ${DMG_PATH} notarized and stapled"
echo ""
