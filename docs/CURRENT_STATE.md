# Current State — frozen 2026-04-30 as `v0.2.0-local`

This document is the "what works today, how to use it" snapshot tied to the
[`v0.2.0-local`](https://github.com/ELuria17/schedule-agent/releases/tag/v0.2.0-local)
git tag and release. If you found this from a friend who said "try this thing,"
you're in the right place — use the .dmg from that release.

The point of this snapshot: development is pivoting to native iPhone + Mac apps
shipped through the App Store. That's a multi-week effort and could fall over.
This frozen build is the stable known-good fork-point so existing users keep
working software regardless.

---

## What this is

A Motion-style scheduler that:

- Watches your task sources (Canvas LMS, Apple Reminders, Google Tasks,
  Microsoft To-Do, Todoist — pick what you use).
- Places study/work blocks on your calendar around your fixed commitments
  (classes, meetings, working hours, sleep).
- Re-solves automatically when anything changes (new assignment posted,
  task marked done, deadline shifts).
- Sends you a notification when there's something to know (iMessage, ntfy,
  Pushover, Slack, email — pick one).
- Has a small built-in agent (Claude Sonnet 4.6) for natural-language task
  intake — say "I have an essay due Friday," it makes the task.

Everything runs locally on your Mac. No subscription, no cloud server you have
to trust — but you do supply your own Anthropic API key (~$3-5/mo of usage for
typical loads).

## What works in `v0.2.0-local`

| Area | Status |
|---|---|
| macOS install via signed+notarized .dmg | ✅ One-click, no Gatekeeper warning |
| iCloud Calendar (CalDAV) | ✅ Production-tested |
| Generic CalDAV (Fastmail, Posteo, Nextcloud, …) | ✅ Same provider |
| Google Calendar | ✅ Tested |
| Microsoft / Outlook Calendar | ✅ Tested |
| Canvas LMS task sync | ✅ Production-tested |
| Apple Reminders (read + reconcile) | ✅ Native Swift binary |
| Google Tasks | ✅ |
| Microsoft To-Do | ✅ |
| Todoist | ✅ |
| iMessage notifier | ✅ macOS only |
| ntfy / Pushover / Slack / email notifiers | ✅ Cross-platform |
| Browser hub at `localhost:8787` | ✅ Today + Tasks + Sessions tabs |
| Tailscale remote access | ✅ See `docs/deployment.md` |
| HTTPS via `tailscale serve` | ✅ `scripts/enable_tailscale_https.py` |
| Wake-from-sleep auto-resolve | ✅ `wake_watchdog.py` |
| Session transcripts persist across restart | ✅ SQLite-backed |
| Solver dependency enforcement (`task_deps`) | ✅ |
| Soft preferred-window bias (morning/afternoon/evening) | ✅ |
| 344 unit + integration tests passing | ✅ |
| Roadblocks Test-signal runner (40 signals tracked) | ✅ |

## What this does NOT have (planned, deferred)

- **No iPhone or native Mac app.** The interface is a webpage at
  `localhost:8787` (or a Tailscale URL on iPhone Safari). Native apps via the
  App Store are the next phase of work — see the project plan.
- **No multi-user / hosted SaaS.** Single user per install. Each user pays
  Anthropic directly. Plan for the SaaS pivot is in
  [`docs/SAAS_PLAN.md`](SAAS_PLAN.md).
- **macOS-only for the optional iMessage / Apple Reminders providers.** Linux
  and Windows users use the cross-platform notifiers and skip Apple Reminders
  (use Google Tasks, Microsoft To-Do, or Todoist instead).
- **No mobile-friendly hub layout.** Hub renders on a phone but it's
  desktop-first. The native iPhone app is the answer here.

---

## Five-minute install (the friend path)

1. Download `schedule-agent.dmg` from
   [the v0.2.0-local release](https://github.com/ELuria17/schedule-agent/releases/tag/v0.2.0-local).
2. Open it, drag `schedule-agent.app` to `/Applications`.
3. Launch it. The setup wizard opens in your browser.
4. Paste:
   - Anthropic API key (get one at <https://console.anthropic.com/settings/keys>;
     add ~$5 of credit at Billing → Credits — typically lasts a month).
   - Provider credentials for whichever stacks you use (the wizard tells you
     what it needs and links to instructions).
5. Click *Finish*. The hub opens at `http://localhost:8787/hub`.

For remote-access from your phone, install Tailscale on both Mac and iPhone,
note your Mac's `<host>.tail<…>.ts.net` name, and visit
`http://<host>:8787/hub` from Safari on the phone.

If you want HTTPS (no "Not Secure" warning in Safari), run
`python scripts/enable_tailscale_https.py` after enabling MagicDNS + HTTPS in
your Tailscale admin console. See [`docs/deployment.md`](deployment.md).

## When something breaks

- Run the orchestrator directly to see live logs: `python run.py` from the repo
  checkout.
- Check `state.db` (SQLite) — every task, every solver run, every agent
  session is recorded.
- The roadblocks runner regenerates green on a fresh install:
  `pytest tests/test_roadblocks_signals.py -q`.
- Reach out via GitHub Issues; this snapshot is the support window.

## Path forward

The code on the `main` branch will start shifting toward the native-apps
architecture (Phase 2 onward of the plan). The `v0.2.0-local` tag and release
do not move — they remain a perpetually working fallback.

If the native apps ship through the App Store, this self-hosted version stays
alive for users who prefer to run the orchestrator on their own Mac. If the
native apps stall, this is the last good build. Either way, the stable promise
is the tag.
