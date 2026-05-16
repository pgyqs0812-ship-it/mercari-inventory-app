#!/usr/bin/env bash
# verify.sh — Verify signing, notarization, and Gatekeeper for .app and DMG.
#
# Usage:
#   ./scripts/verify.sh <app_bundle> [dmg_path]
#
# Examples:
#   ./scripts/verify.sh dist/MIAInventory.app
#   ./scripts/verify.sh dist/MIAInventory.app dist/MIAInventory_Mac_v1.7.0.dmg
#
# Exit code: 0 = all checks passed, 1 = one or more checks failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_DIR}"

APP_BUNDLE="${1:-${APP_BUNDLE:-}}"
DMG_PATH="${2:-${DMG_PATH:-}}"

PASS=0
FAIL=0
declare -a ERRORS=()

run_check() {
    local label="$1"
    shift
    local output
    if output=$("$@" 2>&1); then
        printf "  ✓  %s\n" "${label}"
        ((PASS++)) || true
    else
        printf "  ✗  %s\n" "${label}"
        ERRORS+=("${label}")
        ((FAIL++)) || true
    fi
}

run_check_grep() {
    local label="$1"
    local pattern="$2"
    shift 2
    local output
    output=$("$@" 2>&1) || true
    if echo "${output}" | grep -q "${pattern}"; then
        printf "  ✓  %s\n" "${label}"
        ((PASS++)) || true
    else
        printf "  ✗  %s\n" "${label}"
        printf "       output: %s\n" "${output}" | head -3
        ERRORS+=("${label}")
        ((FAIL++)) || true
    fi
}

run_info_grep() {
    local label="$1"
    local pattern="$2"
    shift 2
    local output
    output=$("$@" 2>&1) || true
    if echo "${output}" | grep -q "${pattern}"; then
        printf "  ℹ  %s\n" "${label}"
    else
        printf "  ℹ  %s — (not matched; Gatekeeper is authoritative)\n" "${label}"
    fi
}

echo ""
echo "━━━ Verification Report ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── App bundle checks ─────────────────────────────────────────────────────────
if [[ -n "${APP_BUNDLE}" && -d "${APP_BUNDLE}" ]]; then
    echo ""
    echo "  App: ${APP_BUNDLE}"
    echo ""

    run_check \
        "codesign: valid signature (deep + strict)" \
        codesign --verify --deep --strict "${APP_BUNDLE}"

    run_check_grep \
        "codesign: hardened runtime enabled" \
        "runtime" \
        codesign -dv "${APP_BUNDLE}"

    run_info_grep \
        "codesign: Developer ID Application identity" \
        "Developer ID Application" \
        codesign -dv "${APP_BUNDLE}"

    run_check_grep \
        "codesign: secure timestamp present" \
        "Timestamp=" \
        codesign -dv --verbose=4 "${APP_BUNDLE}"

    run_check \
        "Gatekeeper: execute — accepted" \
        spctl --assess --type execute "${APP_BUNDLE}"
fi

# ── DMG checks ────────────────────────────────────────────────────────────────
if [[ -n "${DMG_PATH}" && -f "${DMG_PATH}" ]]; then
    echo ""
    echo "  DMG: ${DMG_PATH}"
    echo ""

    run_check \
        "codesign: valid signature (DMG)" \
        codesign --verify "${DMG_PATH}"

    run_info_grep \
        "codesign: Developer ID Application identity (DMG)" \
        "Developer ID Application" \
        codesign -dv "${DMG_PATH}"

    run_check \
        "Gatekeeper: open — accepted (notarized)" \
        spctl --assess --type open --context "context:primary-signature" "${DMG_PATH}"

    run_check \
        "notarytool: ticket stapled to DMG" \
        xcrun stapler validate "${DMG_PATH}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "  ─────────────────────────────────────────────"
printf "  Passed : %d\n" "${PASS}"
printf "  Failed : %d\n" "${FAIL}"

if [[ ${FAIL} -gt 0 ]]; then
    echo ""
    echo "  Failed checks:"
    for e in "${ERRORS[@]}"; do
        printf "    • %s\n" "${e}"
    done
    echo ""
    echo "  The artifact is NOT ready for distribution."
    echo ""
    exit 1
else
    echo ""
    echo "  ✓ All checks passed — ready for distribution."
    echo ""
fi
