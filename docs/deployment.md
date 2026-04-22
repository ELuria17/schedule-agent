# Deployment

Three supported paths for running the orchestrator as a long-lived service:
launchd (macOS), systemd (Linux), and Docker (anywhere). Pick based on
where you're running and what platform features you need.

## Which path?

| Path | When it's the right call | Caveats |
|---|---|---|
| **launchd** (macOS) | You're on a Mac and want to use `IMessageNotifier` + `AppleRemindersTodoSource`. | macOS-only. TCC prompts on first run for Messages and Reminders. |
| **systemd** (Linux) | Bare-metal / VPS / home server running a modern Linux. Long uptime, direct host access. | No iMessage / Reminders. Use `NtfyNotifier` and `TODO_SOURCE = None`. |
| **Docker** (any OS) | You want a reproducible container; you'll mount `.env` and `state.db` as volumes. | No iMessage / Reminders. Requires a portable `schedule_config.py` — the default one imports macOS providers and will fail inside a Linux image. |

macOS users who want the convenience of Docker can mix: run the container
for the orchestrator + solver, and call out to a separate local notifier
service if they need iMessage. That setup is not documented here — start
with the single-host path and split only if you need to.

---

## launchd (macOS)

1. Install the wizard output + compile the Reminders binary.
   ```bash
   python install.py
   cd macos && swiftc -O -o reminders_fetch reminders_fetch.swift && cd ..
   ```
2. Create `~/Library/LaunchAgents/com.schedule-agent.plist` using the
   template in [`macos/README.md`](../macos/README.md). Replace `REPO`
   with the absolute path of your checkout.
3. `launchctl load ~/Library/LaunchAgents/com.schedule-agent.plist`.

The plist points at `macos/launch.sh`, which uses a relative `REPO_ROOT`
computed from its own location — so the checkout can live anywhere, the
plist just needs the right absolute path.

**Logs.** The plist redirects stdout/stderr to `orchestrator.log` and
`orchestrator.err.log` in the repo root. `tail -f orchestrator.log` while
you test.

**TCC grants.** First invocation triggers macOS prompts for Messages
(AppleScript) and Reminders (EventKit). Accept them. If you rebuild
`reminders_fetch` with a different code signature, permissions reset —
re-run once to get the prompt again (ROADBLOCKS §R3, §M3).

**Wake behavior.** Mac sleep pauses APScheduler's interval trigger. When
the Mac wakes, the hub's "open page" handler runs a stale-check and kicks
a resolve if the last log entry is >5 minutes old (ROADBLOCKS §L1). You
don't normally need to do anything; the first hub load after wake sees
fresh data.

---

## systemd (Linux)

Walkthrough lives at [`deploy/systemd/README.md`](../deploy/systemd/README.md).
Short version:

```bash
# as the schedule user:
cd /srv/schedule-agent-public
venv/bin/python install.py    # pick ntfy, TODO_SOURCE=None

# as root:
sed -e 's|<USER>|schedule|g' -e 's|<REPO>|/srv/schedule-agent-public|g' \
    /srv/schedule-agent-public/deploy/systemd/schedule-agent.service \
    > /etc/systemd/system/schedule-agent.service
systemctl daemon-reload
systemctl enable --now schedule-agent
```

**Logs** land in journald — `journalctl -u schedule-agent -f`.

**Hardening.** The shipped unit leaves its `ProtectSystem`, `ProtectHome`,
etc. lines commented — they work but require you to add `ReadWritePaths=`
covering your checkout. Uncomment them once you've confirmed the vanilla
path works.

**Reverse proxy.** systemd binds the orchestrator on `0.0.0.0:8787` in the
clear. Front it with nginx / Caddy / Tailscale serve for TLS. Never expose
`:8787` directly to the internet — `REPLAN_TOKEN` is an app-level gate,
but the transport is HTTP.

---

## Docker

```bash
python install.py             # pick ntfy, TODO_SOURCE=None
docker compose up -d --build
```

The `docker-compose.yml` mounts `./data` on the host to `/data` in the
container and sets `SCHEDULE_AGENT_DATA_DIR=/data` so `.env`,
`state.db`, and `learning_state.json` all land in that single mount.
Rebuilds don't wipe state. `docker compose down -v` does — back up
`./data/state.db` before you need to. The `.dockerignore` keeps
secrets, state, and compiled binaries out of the image itself.

**schedule_config.py.** The default file imports `IMessageNotifier` and
`AppleRemindersTodoSource`, which won't work in a Linux container because
their reference implementations shell out to AppleScript / EventKit. Swap
those imports + constructors to `NtfyNotifier` and `TODO_SOURCE = None`
before your first `docker compose build`.

An alternative: keep the default `schedule_config.py` in the repo (for
macOS users) and mount a container-specific variant as a read-only volume:

```yaml
services:
  orchestrator:
    # ...
    volumes:
      - ./state.db:/app/state.db
      - ./schedule_config.docker.py:/app/schedule_config.py:ro
```

The volume line is already in `docker-compose.yml` commented out — enable
it once you have the container-flavored file.

**Logs.** `docker logs -f schedule-agent`.

**Reverse proxy.** Same advice as systemd — expose `:8787` to a reverse
proxy, not to the public internet.

---

## Choosing ports / hostnames

The orchestrator binds `0.0.0.0:8787` inside all three paths. Change the
host-side port by editing:
- launchd: `PORT` env in the LaunchAgent plist (respected by
  `orchestrator.py` if you add the plumbing) or an nginx / Caddy / Tailscale
  serve proxy.
- systemd: `ExecStart` line, or a reverse proxy.
- Docker: `ports: - "8888:8787"` in `docker-compose.yml`.

For remote access, the original author uses Tailscale (`tailscale status`
gives you a stable `<mac>.<tailnet>.ts.net` hostname that works from any
network) — see ROADBLOCKS §T1–T3 for the full story of how Cloudflare
tunnels and `tailscale serve` each fell short.

---

## Upgrades

Everything is SQLite + code — no external state to preserve. Upgrade is:

```bash
git pull
pip install -r requirements.txt           # or: docker compose up -d --build
systemctl restart schedule-agent          # or launchctl kickstart / docker restart
```

If a release adds tables, `config.py`'s `CREATE TABLE IF NOT EXISTS` handles
it automatically. Schema *changes* to existing tables need migrations —
as of this writing no migrations exist, and the convention is: pre-publish
only. Once a release is tagged, changing a CHECK constraint or column
becomes a migration, not an edit.
