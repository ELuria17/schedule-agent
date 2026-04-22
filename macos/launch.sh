#!/bin/bash
# launchd wrapper. Boots the orchestrator from a per-user LaunchAgent plist.
# Assumes a venv at <repo-root>/venv with requirements installed.
set -eu
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source venv/bin/activate
exec python orchestrator.py
