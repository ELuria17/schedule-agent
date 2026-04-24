#!/bin/bash
# Sign + notarize + staple the schedule-agent macOS build.
#
# Prerequisites (one-time setup):
#   1. Developer ID Application certificate installed in login keychain.
#      Verify:  security find-identity -v -p codesigning
#   2. Notarization credentials stored in keychain as a profile.
#      Run once:
#        xcrun notarytool store-credentials "notarytool-autoplan" \
#            --apple-id "you@example.com" \
#            --team-id  "YOUR_TEAM_ID" \
#            --password "xxxx-xxxx-xxxx-xxxx"   # app-specific password
#
# Usage:
#   bash packaging/macos/build.sh                      # build unsigned .app + .dmg first
#   bash packaging/macos/sign_and_notarize.sh          # then run this
#
# Configuration:
#   Either edit the defaults below, or set these env vars before running:
#     SIGN_IDENTITY      — full cert name, e.g. "Developer ID Application: Jane Doe (ABCD123456)"
#                          Copy it verbatim from `security find-identity -v -p codesigning`.
#     NOTARY_PROFILE     — the label you passed to `notarytool store-credentials`
#
# What it does:
#   - Code-signs the .app with --options runtime + the entitlements.plist
#   - Verifies the signature
#   - Signs the .dmg
#   - Submits the .dmg for notarization (waits ~5-20 min on first submit per account)
#   - Staples the notarization ticket to the .dmg
#   - Verifies the stapled DMG with spctl
#
# Idempotent — safe to re-run after fixing a failure.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

SIGN_IDENTITY="${SIGN_IDENTITY:-CHANGE_ME_Developer ID Application: Your Name (TEAMID)}"
NOTARY_PROFILE="${NOTARY_PROFILE:-notarytool-autoplan}"
ENTITLEMENTS="$HERE/entitlements.plist"

APP="$HERE/dist/schedule-agent.app"
DMG="$HERE/dist/schedule-agent.dmg"

# ---------- Sanity checks ----------

echo "== Sanity check =="

if [[ "$SIGN_IDENTITY" == CHANGE_ME* ]]; then
    echo "ERROR: SIGN_IDENTITY is not set." >&2
    echo "       Edit this script or export SIGN_IDENTITY=<full cert name>." >&2
    echo "       Find the exact string with:" >&2
    echo "           security find-identity -v -p codesigning" >&2
    exit 1
fi

if [ ! -d "$APP" ]; then
    echo "ERROR: $APP not found — build the app first with packaging/macos/build.sh." >&2
    exit 1
fi

if [ ! -f "$DMG" ]; then
    echo "ERROR: $DMG not found — build the dmg first with packaging/macos/build.sh." >&2
    exit 1
fi

if [ ! -f "$ENTITLEMENTS" ]; then
    echo "ERROR: $ENTITLEMENTS not found — entitlements.plist should live next to this script." >&2
    exit 1
fi

if ! security find-identity -v -p codesigning | grep -q "$SIGN_IDENTITY"; then
    echo "ERROR: Signing identity '$SIGN_IDENTITY' not found in the codesigning keychain." >&2
    echo "       Available identities:" >&2
    security find-identity -v -p codesigning >&2
    exit 1
fi

# Best-effort check that the keychain has a notarytool-compatible entry.
# We probe with `history` but treat network/transient failures as "keep
# going" — only fail if the error is specifically a missing profile.
NOTARY_PROBE="$(xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" --limit 1 2>&1 || true)"
if echo "$NOTARY_PROBE" | grep -qi "could not find.*profile\|profile.*not found\|could not load profile"; then
    echo "ERROR: Keychain profile '$NOTARY_PROFILE' isn't stored in your keychain." >&2
    echo "       Run this once, replacing the three placeholders:" >&2
    echo "           xcrun notarytool store-credentials \"$NOTARY_PROFILE\" \\" >&2
    echo "               --apple-id \"YOUR_APPLE_ID\" \\" >&2
    echo "               --team-id  \"YOUR_TEAM_ID\" \\" >&2
    echo "               --password \"APP_SPECIFIC_PASSWORD\"" >&2
    exit 1
fi

echo "OK — identity, profile, and artifacts all present."

# ---------- Sign the .app ----------

echo ""
echo "== Signing $APP =="

# --deep signs everything inside the bundle (nested frameworks, helpers,
# the CPython binary, every .dylib and .so). PyInstaller produces a lot
# of these. --force lets us re-sign if we're iterating.
codesign --deep --force --verify --verbose \
    --options runtime \
    --timestamp \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGN_IDENTITY" \
    "$APP"

echo ""
echo "== Verifying $APP signature =="
codesign --verify --deep --strict --verbose=2 "$APP"
# spctl pre-check on the .app — won't pass until notarized, but should
# at least say "source=Developer ID" with no errors.
spctl --assess --type execute --verbose "$APP" || true

# ---------- Rebuild the DMG (so it contains the signed .app) ----------

echo ""
echo "== Rebuilding DMG around signed .app =="

rm -f "$DMG"
if command -v create-dmg >/dev/null 2>&1; then
    create-dmg \
        --volname "schedule-agent" \
        --window-size 500 300 \
        --app-drop-link 350 130 \
        --icon "schedule-agent.app" 150 130 \
        --hide-extension "schedule-agent.app" \
        "$DMG" "$APP"
else
    echo "ERROR: create-dmg not installed; install with: brew install create-dmg" >&2
    exit 1
fi

# ---------- Sign the DMG ----------

echo ""
echo "== Signing $DMG =="

codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
codesign --verify --verbose=2 "$DMG"

# ---------- Notarize ----------

echo ""
echo "== Submitting $DMG for notarization =="
echo "   (first submission per account can take 10-30 min; subsequent usually <5)"

xcrun notarytool submit "$DMG" \
    --keychain-profile "$NOTARY_PROFILE" \
    --wait

# If notarytool exits 0, the submission was Accepted. If it failed,
# pull the log so you know why:
if [ $? -ne 0 ]; then
    echo ""
    echo "Notarization failed. Fetching the log of the last submission:" >&2
    SUBMISSION_ID="$(xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" --limit 1 \
                     | awk '/id:/ {print $2; exit}')"
    if [ -n "$SUBMISSION_ID" ]; then
        xcrun notarytool log "$SUBMISSION_ID" --keychain-profile "$NOTARY_PROFILE" >&2
    fi
    exit 1
fi

# ---------- Staple ----------

echo ""
echo "== Stapling notarization ticket to $DMG =="
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

# ---------- Final Gatekeeper check ----------

echo ""
echo "== Gatekeeper assessment =="
spctl --assess --type open --context context:primary-signature --verbose "$DMG"

cat <<EOF

Done. $DMG is signed, notarized, and stapled.

Users will be able to open it without the "Apple could not verify..."
warning. First-launch will show Apple's normal "App downloaded from the
internet" prompt; one click on Open and they're in — no System Settings
trip needed.

You can publish this DMG by attaching it to a GitHub release:
    gh release upload <tag> "$DMG"
or replace the file in the existing v0.1.0 release.
EOF
