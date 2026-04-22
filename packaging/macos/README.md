# macOS packaging

Turns the repo into a double-clickable `.app` via PyInstaller, optionally
wrapped in a `.dmg`. Intended for the landing-page Download button.

## What this builds

```
packaging/macos/dist/
├── schedule-agent/                    # PyInstaller intermediate
└── schedule-agent.app                 # the bundle
    └── Contents/
        ├── Info.plist
        ├── MacOS/schedule-agent       # bundled Python + run.py entry
        └── Resources/                 # hub.html, providers/, macos/…
```

`.app` launches `run.py` directly:
- First launch (no `.env`): opens the browser at
  `http://127.0.0.1:8787/setup` — the wizard writes `.env` and creates the
  Anthropic agent.
- Subsequent launches: opens `http://127.0.0.1:8787/hub?key=<token>`.

## Prereqs

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt pyinstaller
brew install create-dmg   # optional, for the .dmg wrapper step
```

## Build

From the repo root:

```bash
bash packaging/macos/build.sh
```

Outputs land in `packaging/macos/dist/`.

## What the build script does

1. Cleans prior `build/` and `dist/` trees under `packaging/macos/`.
2. Runs `pyinstaller schedule-agent.spec` — the spec bundles Python +
   every dep + the repo source, with the right `hiddenimports` for
   uvicorn/anthropic/caldav (PyInstaller's static analyzer misses those).
3. If `create-dmg` is present, wraps the `.app` in `schedule-agent.dmg`
   with a drag-to-Applications layout.

## Gatekeeper (unsigned build)

First launch on a user's Mac triggers:

> "schedule-agent" cannot be opened because the developer cannot be verified.

The workaround is built into macOS:

1. Find `schedule-agent.app` in Finder.
2. Right-click → **Open**.
3. Confirm **Open** on the warning dialog.

One click per machine, forever. Put this in the landing page's install
instructions.

## Signing + notarization (optional; $99/year)

When the project justifies it, sign with an Apple Developer ID:

```bash
# after building:
codesign --deep --force \
  --options runtime \
  --entitlements packaging/macos/entitlements.plist \
  --sign "Developer ID Application: YOUR NAME (TEAM_ID)" \
  packaging/macos/dist/schedule-agent.app

# staple-ready notarization:
xcrun notarytool submit packaging/macos/dist/schedule-agent.dmg \
  --keychain-profile "notary" --wait
xcrun stapler staple packaging/macos/dist/schedule-agent.dmg
```

Prereq: a Developer ID Application cert in the login keychain + an
`app-specific password` stored as `notary` via
`xcrun notarytool store-credentials`.

An `entitlements.plist` doesn't live in the repo yet because the unsigned
path doesn't use it. The minimum for notarization on a Python app:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict>
</plist>
```

## Application Support paths

Handled automatically by `paths.py`:

- Dev flow (`git clone && python run.py`) → state lives in the repo dir.
- Frozen bundle (`sys.frozen` set by PyInstaller) → state lives in
  `~/Library/Application Support/schedule-agent/` on macOS.
- Any environment that sets `SCHEDULE_AGENT_DATA_DIR` wins — used by the
  Docker compose setup to mount a host volume.

This means a packaged `.app` can stay read-only (required for
notarization) and the user's `state.db` / `.env` survive replacing the
bundle on upgrade.

## Icon

Drop a 1024×1024 `icon.icns` into `packaging/macos/`. The spec file
picks it up automatically. Generate with:

```bash
iconutil -c icns packaging/macos/icon.iconset
```

where `icon.iconset` is a folder of `icon_16x16.png`, `icon_16x16@2x.png`,
… through `icon_512x512@2x.png`.

## Known limitations of this first build

- Unsigned (see Gatekeeper above).
- Tests written in this env can't actually build the `.app` to verify
  the spec — run `bash packaging/macos/build.sh` on a real Mac and file
  an issue if anything errors. The `hiddenimports` list in
  `schedule-agent.spec` is the most likely culprit if PyInstaller's
  static analysis missed a dep.
- First launch still triggers the TCC prompts for Messages and
  Reminders (once per machine, expected). The Swift binary compile
  itself is automated — see [bootstrap.py](../../bootstrap.py) — but
  macOS can't grant permissions on the user's behalf.
