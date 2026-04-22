"""One-time Google Calendar authorization.

Run this once after saving your credentials.json file. It opens your web
browser, asks you to sign in to the Google account whose calendar AutoPlan
should read/write, and saves a refresh token so the app never needs to ask
again.

    python authorize_google.py

If you already authorized once and want to switch accounts, delete the
token file first:

    rm ~/Library/Application\\ Support/schedule-agent/google_token.json   # macOS
    rm "%APPDATA%\\schedule-agent\\google_token.json"                      # Windows
    rm ~/.local/share/schedule-agent/google_token.json                     # Linux

Then run this script again.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from paths import env_path, google_token_path


def main() -> int:
    load_dotenv(env_path())
    creds_path = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS")
    if not creds_path:
        print(
            "GOOGLE_CALENDAR_CREDENTIALS is not set in your .env file.\n"
            "Follow docs/google-calendar-setup.md to download your\n"
            "credentials.json, then add this line to your .env:\n\n"
            "    GOOGLE_CALENDAR_CREDENTIALS=/full/path/to/credentials.json\n",
            file=sys.stderr,
        )
        return 1

    # Import here so the script can print the error above even if the
    # google libs aren't installed yet.
    from providers.google_calendar import GoogleCalendarProvider

    provider = GoogleCalendarProvider(
        credentials_path=creds_path,
        token_path=str(google_token_path()),
        write_calendar_name=os.environ.get("WRITE_CALENDAR_NAME", "Study Blocks"),
    )
    print("Opening your browser for Google sign-in...")
    provider.authorize_interactive()
    print(f"Done. Refresh token saved to {google_token_path()}")
    print("You can close the browser tab now. AutoPlan is ready to use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
