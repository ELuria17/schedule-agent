"""One-time Microsoft 365 / Outlook authorization.

Run this once after creating your Azure AD app registration. It opens
your web browser, asks you to sign in to the Microsoft account whose
calendar / mail / To Do the app should read, and saves a token cache
so the app never needs to ask again.

    python authorize_microsoft.py

If you already authorized once and want to switch accounts, delete
the token file first:

    rm ~/Library/Application\\ Support/schedule-agent/microsoft_token.json   # macOS
    rm "%APPDATA%\\schedule-agent\\microsoft_token.json"                      # Windows
    rm ~/.local/share/schedule-agent/microsoft_token.json                     # Linux

Then run this script again.

Environment variables
---------------------
MICROSOFT_CLIENT_ID  — required. Application (client) ID from your
                        Azure AD app registration.
MICROSOFT_TENANT     — optional. Defaults to "common" (both personal
                        and work/school accounts). Pass a tenant
                        GUID or one of "organizations" / "consumers"
                        to scope to a specific audience.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from paths import env_path, microsoft_token_path


def main() -> int:
    load_dotenv(env_path())
    client_id = os.environ.get("MICROSOFT_CLIENT_ID")
    if not client_id:
        print(
            "MICROSOFT_CLIENT_ID is not set in your .env file.\n"
            "Follow docs/microsoft-graph-setup.md to register a public\n"
            "client app in the Azure portal, then add this line to your\n"
            ".env:\n\n"
            "    MICROSOFT_CLIENT_ID=00000000-0000-0000-0000-000000000000\n\n"
            "(Optional) Limit to a specific tenant by also setting:\n\n"
            "    MICROSOFT_TENANT=common           # personal + work/school (default)\n"
            "    MICROSOFT_TENANT=organizations    # work/school only\n"
            "    MICROSOFT_TENANT=consumers        # personal only\n"
            "    MICROSOFT_TENANT=<tenant-guid>    # one specific tenant\n",
            file=sys.stderr,
        )
        return 1
    tenant = os.environ.get("MICROSOFT_TENANT", "common")

    # Import here so the script can print the error above even if msal
    # isn't installed yet.
    from providers.microsoft_graph_auth import authorize_interactive

    print("Opening your browser for Microsoft sign-in...")
    authorize_interactive(
        client_id=client_id,
        tenant=tenant,
        token_path=microsoft_token_path(),
    )
    print(f"Done. Token cache saved to {microsoft_token_path()}")
    print("You can close the browser tab now. AutoPlan is ready to use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
