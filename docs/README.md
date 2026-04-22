# Docs

User and contributor documentation. The main repo `README.md` is the
project summary; this folder goes deeper.

| Doc | Audience | Read when |
|---|---|---|
| [`getting-started.md`](getting-started.md) | New users | You just cloned the repo. |
| [`providers.md`](providers.md) | Contributors | You want to add Google Calendar / Todoist / Pushover / any other backend. |
| [`deployment.md`](deployment.md) | Operators | You're taking it from laptop → long-lived service. |
| [`troubleshooting.md`](troubleshooting.md) | Anyone debugging | Something doesn't work. |

Related material outside this folder:

- `../README.md` — project overview, publish checklist, architecture summary.
- `../ROADBLOCKS.md` — exhaustive build log. Every bug + fix + test signal
  from the two-month iteration that led to this repo. The user-facing
  troubleshooting doc here is a curated subset; `ROADBLOCKS.md` is the
  unabridged source.
- `../macos/README.md` — macOS-specific runtime bits (TCC, launchd plist
  template, swiftc compile command).
- `../deploy/systemd/README.md` — Linux service walkthrough.
