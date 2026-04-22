# PyInstaller spec for schedule-agent.app (macOS).
#
# Build with:
#   cd packaging/macos && bash build.sh
#
# What this produces:
#   dist/schedule-agent.app         a relocatable .app bundle
#   dist/schedule-agent.dmg         a drag-to-Applications installer (optional)
#
# The .app bundles a full Python interpreter + every dependency + the repo
# source. It does NOT bundle the user's .env or state.db — those live in
# ~/Library/Application Support/schedule-agent/ at runtime (created on
# first launch by run.py).
#
# Gatekeeper: this build is UNSIGNED. First launch triggers
#   "App cannot be opened because the developer cannot be verified."
# The fix is right-click → Open → Open on the first launch only. Signing
# with an Apple Developer ID cert is a later-milestone task.
# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parents[1]

# Hidden imports that PyInstaller's static analysis misses.
HIDDEN = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anthropic",
    "caldav",
    "vobject",
    "python_multipart",
    "multipart",
]

# Data files to ship inside the .app.
DATA = [
    (str(REPO_ROOT / "hub.html"), "."),
    (str(REPO_ROOT / "providers"), "providers"),
    (str(REPO_ROOT / "macos" / "send_imessage.applescript"), "macos"),
]

a = Analysis(
    [str(REPO_ROOT / "run.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=DATA,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # These balloon the bundle and aren't used.
        "matplotlib", "notebook", "pandas", "numpy",
        "sphinx", "pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="schedule-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(REPO_ROOT / "packaging" / "macos" / "icon.icns")
        if (REPO_ROOT / "packaging" / "macos" / "icon.icns").exists() else None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="schedule-agent",
)

app = BUNDLE(
    coll,
    name="schedule-agent.app",
    icon=str(REPO_ROOT / "packaging" / "macos" / "icon.icns")
        if (REPO_ROOT / "packaging" / "macos" / "icon.icns").exists() else None,
    bundle_identifier="com.schedule-agent.app",
    version="0.1.0",
    info_plist={
        "CFBundleName": "schedule-agent",
        "CFBundleDisplayName": "schedule-agent",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "12.0",
        # Agent = background app, no Dock icon. Flip to False if you want
        # a Dock presence. Users can still reach the hub via browser URL.
        "LSUIElement": False,
        # iMessage notifier invokes osascript → Messages.app. macOS prompts
        # for the TCC grant on first run; this usage-description string is
        # what appears in that prompt.
        "NSAppleEventsUsageDescription":
            "schedule-agent sends iMessages via Messages.app to deliver your "
            "daily schedule summary.",
        # Apple Reminders source invokes macos/reminders_fetch → EventKit.
        "NSRemindersUsageDescription":
            "schedule-agent reads your Reminders so it can cross-reference "
            "completed items against scheduled tasks.",
    },
)
