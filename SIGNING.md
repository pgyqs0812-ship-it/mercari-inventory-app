# Mac App Signing & Notarization Guide

This document covers everything needed to sign and notarize the MercariInventory app so
that macOS Gatekeeper does not block downloaded builds.

---

## Why this matters

When a user downloads `dist.zip` from a GitHub Release and runs `MercariInventory.command`:

| Symptom | Root cause |
|---|---|
| "Apple cannot verify this app" dialog | Binary not signed with Developer ID |
| `Killed: 9` on Apple Silicon | Unsigned binary with quarantine xattr blocked by macOS |
| "Damaged and can't be opened" | Gatekeeper rejecting unsigned quarantined binary |

The **immediate workaround** (no Apple account needed) is built into the launcher:
`MercariInventory.command` automatically strips `com.apple.quarantine` from the bundle
on first run. This resolves `Killed: 9` after the user clicks through the one-time
Gatekeeper dialog for the `.command` file itself.

The **permanent fix** is Developer ID signing + notarization, documented below.

---

## Prerequisites

1. **Apple Developer account** — https://developer.apple.com ($99/year)
2. **Xcode Command Line Tools** — `xcode-select --install`
3. **Developer ID Application certificate** in Keychain (see step 1 below)
4. **App Store Connect API key** for notarization (see step 2 below)

---

## Step 1 — Create a Developer ID Application certificate

1. Open Xcode → Settings → Accounts → select your Apple ID → Manage Certificates
2. Click `+` → **Developer ID Application**
3. Export the certificate as a `.p12` file (right-click in Keychain Access → Export)
4. Note the **Team ID** from https://developer.apple.com/account → Membership

Your `SIGN_IDENTITY` value will be:
```
Developer ID Application: Your Name (TEAMID)
```
Verify it is in your keychain:
```bash
security find-identity -v -p codesigning | grep "Developer ID Application"
```

---

## Step 2 — Create an App Store Connect API key for notarization

1. Go to https://appstoreconnect.apple.com/access/api
2. Click `+` → role **Developer** → download the `.p8` key file (save it — you cannot download again)
3. Note the **Key ID** (10-character alphanumeric) and **Issuer ID** (UUID)

Store the API key for `xcrun notarytool`:
```bash
xcrun notarytool store-credentials "notarytool-profile" \
    --key <path/to/AuthKey_KEYID.p8> \
    --key-id <KEY_ID> \
    --issuer <ISSUER_ID>
```

---

## Step 3 — Build, sign, and notarize locally

```bash
# Build the app
./build_mac.sh

# Sign and notarize (requires credentials from steps 1–2)
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
NOTARIZE=1 \
NOTARIZE_PROFILE="notarytool-profile" \
./sign_and_notarize.sh
```

Or pass everything directly to `build_mac.sh` by exporting before calling:
```bash
export SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export NOTARIZE=1
export NOTARIZE_PROFILE="notarytool-profile"
./build_mac.sh
```

---

## Step 4 — Verify signing and notarization

```bash
# Check code signature
codesign --verify --deep --verbose=2 dist/MercariInventory/MercariInventory

# Check Gatekeeper acceptance
spctl --assess --type execute --verbose dist/MercariInventory/MercariInventory

# Check notarization ticket is stapled
xcrun stapler validate dist/MercariInventory/MercariInventory
```

Expected output from `spctl`:
```
dist/MercariInventory/MercariInventory: accepted
source=Notarized Developer ID
```

---

## Step 5 — GitHub Actions (automated CI signing)

Add the following **repository secrets** at:
Settings → Secrets and variables → Actions → New repository secret

| Secret name | Value |
|---|---|
| `APPLE_CERTIFICATE_BASE64` | `base64 -i YourCert.p12` output |
| `APPLE_CERTIFICATE_PASSWORD` | Password used when exporting the .p12 |
| `APPLE_SIGN_IDENTITY` | `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_TEAM_ID` | Your 10-character Team ID |
| `APPLE_API_KEY_BASE64` | `base64 -i AuthKey_KEYID.p8` output |
| `APPLE_API_KEY_ID` | 10-character Key ID |
| `APPLE_API_ISSUER_ID` | UUID Issuer ID |

Once these secrets are set, the `build.yml` workflow automatically signs and notarizes
every tagged release. No code changes are needed.

---

## Unsigned / development builds

To build without signing (default when `SIGN_IDENTITY` is not set):
```bash
./build_mac.sh
```

The unsigned build still works locally via the quarantine-stripping launcher.
