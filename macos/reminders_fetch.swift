import EventKit
import Foundation

let store = EKEventStore()
let sema = DispatchSemaphore(value: 0)
var grantedAccess = false

if #available(macOS 14.0, *) {
    store.requestFullAccessToReminders { granted, _ in
        grantedAccess = granted
        sema.signal()
    }
} else {
    store.requestAccess(to: .reminder) { granted, _ in
        grantedAccess = granted
        sema.signal()
    }
}
sema.wait()

guard grantedAccess else {
    FileHandle.standardError.write("access denied\n".data(using: .utf8)!)
    exit(2)
}

let calendars = store.calendars(for: .reminder)
var results: [[String: Any]] = []
let group = DispatchGroup()

// Incomplete (all)
group.enter()
let incompletePredicate = store.predicateForIncompleteReminders(
    withDueDateStarting: nil, ending: nil, calendars: calendars
)
store.fetchReminders(matching: incompletePredicate) { reminders in
    defer { group.leave() }
    for r in reminders ?? [] {
        var due: Any = NSNull()
        if let comps = r.dueDateComponents, let d = Calendar.current.date(from: comps) {
            let fmt = ISO8601DateFormatter()
            due = fmt.string(from: d)
        }
        results.append([
            "uid": r.calendarItemIdentifier,
            "list": r.calendar.title,
            "title": r.title ?? "",
            "due": due,
            "completed": false,
            "completed_at": NSNull(),
        ])
    }
}

// Recently completed (last 7 days)
let sevenDaysAgo = Date().addingTimeInterval(-7 * 86400)
group.enter()
let completedPredicate = store.predicateForCompletedReminders(
    withCompletionDateStarting: sevenDaysAgo, ending: Date(), calendars: calendars
)
store.fetchReminders(matching: completedPredicate) { reminders in
    defer { group.leave() }
    let fmt = ISO8601DateFormatter()
    for r in reminders ?? [] {
        var due: Any = NSNull()
        if let comps = r.dueDateComponents, let d = Calendar.current.date(from: comps) {
            due = fmt.string(from: d)
        }
        var completedAt: Any = NSNull()
        if let d = r.completionDate {
            completedAt = fmt.string(from: d)
        }
        results.append([
            "uid": r.calendarItemIdentifier,
            "list": r.calendar.title,
            "title": r.title ?? "",
            "due": due,
            "completed": true,
            "completed_at": completedAt,
        ])
    }
}

group.wait()

let data = try JSONSerialization.data(withJSONObject: results, options: [])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
