# macOS-specific files

Everything in this folder is only used on macOS. Linux/Windows installs can
ignore it entirely.

| File | Purpose |
|---|---|
| `reminders_fetch.swift` | EventKit bridge that lets `AppleRemindersTodoSource` read modern (post-iOS-13) reminders, which CalDAV can't see. Compile with `swiftc -O -o reminders_fetch reminders_fetch.swift`. Grant TCC permission to Reminders on first run. See ROADBLOCKS §R1-R3. |
| `send_imessage.applescript` | Helper script `IMessageNotifier` invokes via `osascript` to send iMessages from the signed-in Messages.app. Grant TCC permission to Messages on first run. See ROADBLOCKS §M2-M3. |
| `launch.sh` | launchd wrapper for running the orchestrator as a LaunchAgent. Uses a relative path so the repo can live anywhere. |

## Using these in `schedule_config.py`

`schedule_config.py` already points at these files via `macos/…`:

```python
TODO_SOURCE = AppleRemindersTodoSource(
    binary_path=str(_PROJECT_DIR / "macos" / "reminders_fetch"),
)

NOTIFIER = IMessageNotifier(
    script_path=str(_PROJECT_DIR / "macos" / "send_imessage.applescript"),
    recipient=os.environ["USER_PHONE"],
)
```

If you're on Linux or Windows, swap those for cross-platform providers
(`NtfyNotifier`, and set `TODO_SOURCE = None` or plug a TodoSource that
hits a web API) and nothing in this folder ever executes.

## launchd plist

Save something like this to `~/Library/LaunchAgents/com.schedule-agent.plist`,
adjusting `REPO` to the absolute path of your checkout:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>                   <string>com.schedule-agent</string>
  <key>ProgramArguments</key>        <array>
    <string>/bin/bash</string>
    <string>REPO/macos/launch.sh</string>
  </array>
  <key>RunAtLoad</key>               <true/>
  <key>KeepAlive</key>               <true/>
  <key>StandardOutPath</key>         <string>REPO/orchestrator.log</string>
  <key>StandardErrorPath</key>       <string>REPO/orchestrator.err.log</string>
</dict>
</plist>
```

Then `launchctl load ~/Library/LaunchAgents/com.schedule-agent.plist`.
