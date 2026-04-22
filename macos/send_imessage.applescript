on run argv
    set phoneNumber to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        set svc to 1st service whose service type = iMessage
        send messageText to buddy phoneNumber of svc
    end tell
end run
