#!/bin/bash
# Build an unsigned schedule-agent.app + optional .dmg on macOS.
#
# Run from the repo root:
#     bash packaging/macos/build.sh
#
# or from anywhere:
#     bash <repo>/packaging/macos/build.sh
#
# Outputs:
#     packaging/macos/dist/schedule-agent.app
#     packaging/macos/dist/schedule-agent.dmg   (if create-dmg is installed)
#
# Prereqs on the build machine:
#     python3 -m venv venv && source venv/bin/activate
#     pip install -r requirements.txt pyinstaller
#     brew install create-dmg   # optional, for the .dmg step

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

cd "$REPO"

if ! python -c "import PyInstaller" 2>/dev/null; then
    echo "PyInstaller is not installed in the current Python env." >&2
    echo "Run: pip install pyinstaller" >&2
    exit 1
fi

echo "--- Cleaning previous build artifacts ---"
rm -rf packaging/macos/build packaging/macos/dist

echo "--- Running PyInstaller ---"
python -m PyInstaller \
    --clean --noconfirm \
    --workpath packaging/macos/build \
    --distpath packaging/macos/dist \
    packaging/macos/schedule-agent.spec

APP="packaging/macos/dist/schedule-agent.app"
if [ ! -d "$APP" ]; then
    echo "Build failed — $APP not present." >&2
    exit 1
fi
echo "--- Built: $APP"

# Optional: wrap the .app in a .dmg for drag-to-Applications distribution.
if command -v create-dmg >/dev/null 2>&1; then
    echo "--- Building .dmg ---"
    DMG="packaging/macos/dist/schedule-agent.dmg"
    rm -f "$DMG"
    create-dmg \
        --volname "schedule-agent" \
        --window-size 500 300 \
        --app-drop-link 350 130 \
        --icon "schedule-agent.app" 150 130 \
        --hide-extension "schedule-agent.app" \
        "$DMG" "$APP"
    echo "--- Built: $DMG"
else
    echo "--- create-dmg not installed; skipping .dmg step."
    echo "    Install with: brew install create-dmg"
fi

cat <<EOF

Done.

Distribution notes:
  - The build is UNSIGNED. macOS Gatekeeper will refuse to open it on first
    launch with "cannot be opened because the developer cannot be verified."
    The user fix is right-click the app → Open → Open (one-time, per machine).
  - Signing + notarization requires an Apple Developer Program membership
    (\$99/year). See packaging/macos/README.md for the post-signing recipe.
  - First launch opens a browser at http://127.0.0.1:8787/setup for the
    wizard, then writes ~/Library/Application Support/schedule-agent/.env
    and restarts into the hub.
EOF
