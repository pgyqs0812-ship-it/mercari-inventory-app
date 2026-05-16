#!/usr/bin/env bash
# setup_notarytool.sh — One-time: store Apple ID credentials in macOS Keychain
#                       for use with xcrun notarytool.
#
# Run this ONCE before your first notarization.  Credentials are stored
# in Keychain; they are NEVER written to any file or shown in any log.
#
# You need:
#   • Your Apple ID email (e.g. you@example.com)
#   • An app-specific password generated at appleid.apple.com →
#       Sign In → Security → App-Specific Passwords → Generate
#
# Usage:
#   ./scripts/setup_notarytool.sh

set -euo pipefail

TEAM_ID="8UQBUC5ZM6"
PROFILE_NAME="${NOTARIZE_PROFILE:-notarytool-profile}"

echo ""
echo "━━━ notarytool Keychain Profile Setup ━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Team ID       : ${TEAM_ID}"
echo "  Profile name  : ${PROFILE_NAME}"
echo ""
echo "  You will be prompted to enter:"
echo "    1. Your Apple ID (email address)"
echo "    2. Your app-specific password"
echo "       → Generate one at: appleid.apple.com"
echo "         Apple ID → Security → App-Specific Passwords"
echo ""
echo "  Credentials are stored securely in macOS Keychain."
echo "  They will NEVER appear in any log file or script."
echo ""

xcrun notarytool store-credentials "${PROFILE_NAME}" \
    --team-id "${TEAM_ID}"

echo ""
echo "  ✓ Credentials stored in Keychain under profile '${PROFILE_NAME}'"
echo ""
echo "  You can now run the full release pipeline:"
echo "    ./build_mac.sh                                   # build only"
echo "    SIGN_IDENTITY='Developer ID Application: YANSEN PENG' \\"
echo "    NOTARIZE=1 ./build_mac.sh                        # build + sign + notarize"
echo "    ./scripts/release.sh                             # sign + notarize existing .app"
echo ""
