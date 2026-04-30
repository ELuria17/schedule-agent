#!/usr/bin/env python3
"""Wrap the orchestrator behind `tailscale serve` so the hub is HTTPS.

Why: the orchestrator binds plain HTTP on `0.0.0.0:8787`. WireGuard already
encrypts the traffic, but Safari / Chrome flag the URL as "Not Secure"
because the hostname is HTTP. `tailscale serve` provisions a real LetsEncrypt
cert tied to your tailnet's MagicDNS hostname and front-ends localhost:8787.

Prereqs (one-time, on the Tailscale admin console at https://login.tailscale.com/admin/dns):
1. MagicDNS enabled.
2. HTTPS certificates enabled (DNS → "Enable HTTPS").

This script then runs the local commands needed on this device. Idempotent —
safe to re-run, and prints what `tailscale serve status` looks like at the end.

Tested on macOS and Linux; Windows users with the official tailscale.exe in
PATH should be able to run it via `python scripts\\enable_tailscale_https.py`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


def _run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check,
                          capture_output=capture, text=True)


def _tailscale_bin() -> str:
    # On macOS the Mac App Store install lives at this path; CLI symlink is
    # commonly absent. Fall back to PATH.
    candidates = [
        "tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
    ]
    for c in candidates:
        path = shutil.which(c) if "/" not in c else (c if shutil.which(c) or _path_exists(c) else None)
        if path:
            return path
    sys.exit("tailscale CLI not found. Install from https://tailscale.com/download")


def _path_exists(p: str) -> bool:
    from pathlib import Path
    return Path(p).exists()


def _hostname(ts: str) -> str:
    """Return the MagicDNS hostname (e.g. mybox.tail1234.ts.net)."""
    res = _run([ts, "status", "--json"], capture=True)
    data = json.loads(res.stdout)
    self_node = data.get("Self") or {}
    dns = self_node.get("DNSName") or ""
    if not dns:
        sys.exit("tailscale status returned no Self.DNSName — is tailscale up?")
    return dns.rstrip(".")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787,
                        help="Local orchestrator port (default 8787).")
    parser.add_argument("--reset", action="store_true",
                        help="Clear any existing tailscale serve config first.")
    args = parser.parse_args()

    ts = _tailscale_bin()
    host = _hostname(ts)
    print(f"\nTailscale hostname: {host}")

    if args.reset:
        _run([ts, "serve", "reset"], check=False)

    # Provision a cert. Idempotent — re-issues if expired, no-ops if fresh.
    # Required even when `serve` would auto-fetch, because some installs
    # need the explicit provisioning step to bootstrap.
    _run([ts, "cert", host], check=False)

    # Front-end :443 → http://localhost:<port>. The `--bg` form persists
    # across reboots (managed by tailscaled).
    _run([ts, "serve", "--bg", f"http://localhost:{args.port}"])

    print("\nServing config:")
    _run([ts, "serve", "status"], check=False)

    print(f"\nDone. Hub URL: https://{host}/hub")
    print("If `tailscale serve status` shows 'No serve config', enable HTTPS")
    print("certificates in the admin console and re-run with --reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
