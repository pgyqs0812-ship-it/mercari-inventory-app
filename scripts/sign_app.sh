#!/usr/bin/env bash
# sign_app.sh — Sign a PyInstaller .app bundle for Developer ID distribution.
#
# Signs inner .dylib and .so files individually (inner→outer) because
# codesign --deep silently skips Python .so extensions that lack a standard
# bundle structure.  The final --deep pass on the .app bundle then seals
# any remaining Mach-O binaries and the bundle itself.
#
# Usage:
#   ./scripts/sign_app.sh <app_bundle>
#
# Environment variables (or positional args):
#   SIGN_IDENTITY  — codesign identity  (default: Developer ID Application: YANSEN PENG)
#   ENTITLEMENTS   — path to .plist     (default: entitlements.plist)
#
# Example:
#   SIGN_IDENTITY="Developer ID Application: YANSEN PENG" \
#   ./scripts/sign_app.sh dist/MIAInventory.app

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_DIR}"

APP_BUNDLE="${1:-${APP_BUNDLE:-dist/MIAInventory.app}}"
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: YANSEN PENG}"
ENTITLEMENTS="${ENTITLEMENTS:-entitlements.plist}"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ ! -d "${APP_BUNDLE}" ]]; then
    echo "Error: App bundle not found: ${APP_BUNDLE}" >&2
    echo "Run ./build_mac.sh first." >&2
    exit 1
fi
if [[ ! -f "${ENTITLEMENTS}" ]]; then
    echo "Error: Entitlements file not found: ${ENTITLEMENTS}" >&2
    exit 1
fi

echo ""
echo "━━━ Sign App ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Bundle      : ${APP_BUNDLE}"
echo "  Identity    : ${SIGN_IDENTITY}"
echo "  Entitlements: ${ENTITLEMENTS}"
echo ""

# Common flags for inner binaries (no entitlements needed on leaves)
INNER_ARGS=(
    --sign       "${SIGN_IDENTITY}"
    --force
    --options    runtime
    --timestamp
)
# Full flags for the app bundle itself
OUTER_ARGS=(
    "${INNER_ARGS[@]}"
    --entitlements "${ENTITLEMENTS}"
)

# ── Step 1: Sign .dylib files ────────────────────────────────────────────────
echo "  [1/4] Signing .dylib files..."
DYLIB_COUNT=0
DYLIB_FAIL=0
while IFS= read -r -d '' f; do
    if codesign "${INNER_ARGS[@]}" "${f}" 2>/dev/null; then
        ((DYLIB_COUNT++))
    else
        echo "        ⚠ could not sign: ${f}"
        ((DYLIB_FAIL++))
    fi
done < <(find "${APP_BUNDLE}" -type f -name "*.dylib" -print0)
echo "        ${DYLIB_COUNT} signed, ${DYLIB_FAIL} skipped"

# ── Step 2: Sign .so files (Python C extensions) ─────────────────────────────
echo "  [2/4] Signing .so files (Python extensions)..."
SO_COUNT=0
SO_FAIL=0
while IFS= read -r -d '' f; do
    if codesign "${INNER_ARGS[@]}" "${f}" 2>/dev/null; then
        ((SO_COUNT++))
    else
        echo "        ⚠ could not sign: ${f}"
        ((SO_FAIL++))
    fi
done < <(find "${APP_BUNDLE}" -type f -name "*.so" -print0)
echo "        ${SO_COUNT} signed, ${SO_FAIL} skipped"

# ── Step 3: Sign the app bundle (deep pass for any remaining Mach-O) ─────────
echo "  [3/4] Signing app bundle (deep)..."
codesign "${OUTER_ARGS[@]}" --deep "${APP_BUNDLE}"
echo "        Done"

# ── Step 4: Verify ───────────────────────────────────────────────────────────
echo "  [4/4] Verifying..."
codesign --verify --deep --strict "${APP_BUNDLE}"

# Show runtime flag presence
FLAGS=$(codesign -dv "${APP_BUNDLE}" 2>&1 | grep "flags=" || true)
if echo "${FLAGS}" | grep -q "runtime"; then
    echo "        Hardened runtime: ✓"
else
    echo "        Hardened runtime: ⚠ flag not detected — check entitlements"
fi

echo ""
echo "  ✓ ${APP_BUNDLE} signed successfully"
echo ""
