# Mac App Signing & Notarization Guide

This document covers signing and notarizing MIA Inventory so that macOS Gatekeeper
does not block downloaded builds.

---

## Why this matters

| Symptom | Root cause |
|---|---|
| "Apple cannot verify this app" dialog | Binary not signed with Developer ID |
| `Killed: 9` on Apple Silicon | Unsigned binary blocked by Gatekeeper |
| "Damaged and can't be opened" | Unsigned quarantined binary |

---

## Prerequisites

- **Developer ID Application certificate** in Keychain — already installed
  (`Developer ID Application: YANSEN PENG`, Team ID `8UQBUC5ZM6`)
- **Xcode Command Line Tools** — `xcode-select --install`
- **Apple ID + app-specific password** for notarization
  (generate at https://appleid.apple.com → App-Specific Passwords)

---

## One-time setup — store notarization credentials in Keychain

Run this **once** per machine. It stores your Apple ID + app-specific password
securely in the macOS Keychain under the profile name `notarytool-profile`.
**Never hardcode credentials in scripts or files.**

```bash
./scripts/setup_notarytool.sh
```

This calls:
```
xcrun notarytool store-credentials "notarytool-profile" --team-id "8UQBUC5ZM6"
```
You will be prompted interactively for your Apple ID and app-specific password.

---

## Full release pipeline

### Option A — Build + sign + notarize in one command

```bash
SIGN_IDENTITY="Developer ID Application: YANSEN PENG" \
NOTARIZE=1 \
./build_mac.sh
```

`build_mac.sh` calls the individual scripts in order:
1. `./scripts/sign_app.sh` — signs `.dylib`/`.so` individually, then the `.app` with hardened runtime
2. `dmgbuild` — creates `dist/MIAInventory_Mac_<version>.dmg`
3. `codesign` — signs the DMG with Developer ID + timestamp
4. `./scripts/notarize_dmg.sh` — submits DMG to Apple, waits, staples ticket
5. `./scripts/verify.sh` — runs all Gatekeeper checks and exits non-zero on failure

### Option B — Build first, then release separately

```bash
# Step 1: build (unsigned or ad-hoc)
./build_mac.sh

# Step 2: sign + package + notarize + verify
VERSION=v1.7.0 ./scripts/release.sh
```

`release.sh` accepts the same environment variables and runs the same pipeline
but expects `dist/MIAInventory.app` to already exist.

### Skip notarization (sign + DMG only)

```bash
SIGN_IDENTITY="Developer ID Application: YANSEN PENG" \
./build_mac.sh

# or via release.sh:
SKIP_NOTARIZE=1 ./scripts/release.sh
```

---

## Individual scripts

| Script | Purpose |
|---|---|
| `scripts/setup_notarytool.sh` | One-time: store Apple ID credentials in Keychain |
| `scripts/sign_app.sh` | Sign `.app` bundle (inner-first: `.dylib` → `.so` → `.app`) |
| `scripts/package_dmg.sh` | Create + sign DMG from a built `.app` |
| `scripts/notarize_dmg.sh` | Submit DMG to Apple, wait, staple ticket |
| `scripts/verify.sh` | Verify codesign + Gatekeeper for `.app` and DMG |
| `scripts/release.sh` | Orchestrate all steps (sign → package → notarize → verify) |

---

## Verify signing and notarization manually

```bash
./scripts/verify.sh dist/MIAInventory.app dist/MIAInventory_Mac_v1.7.0.dmg
```

Or individually:
```bash
# App bundle
codesign --verify --deep --strict dist/MIAInventory.app
spctl --assess --type execute dist/MIAInventory.app

# DMG
codesign --verify dist/MIAInventory_Mac_v1.7.0.dmg
spctl --assess --type open --context "context:primary-signature" dist/MIAInventory_Mac_v1.7.0.dmg
xcrun stapler validate dist/MIAInventory_Mac_v1.7.0.dmg
```

---

## Unsigned / development builds

```bash
./build_mac.sh
```

The unsigned build is ad-hoc signed and works locally. Right-click → Open to bypass
Gatekeeper on first launch.
