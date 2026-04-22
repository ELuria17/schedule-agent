# Linux / systemd deploy

Use this when you want to run the orchestrator as a normal Python process
on a Linux host (VPS, home server, etc.), managed by systemd. If you'd
rather run in a container, use the `Dockerfile` + `docker-compose.yml` at
the repo root instead.

## Prep

```bash
# as root or with sudo — one-time
useradd -r -s /usr/sbin/nologin schedule   # or reuse an existing user
install -d -o schedule -g schedule /srv/schedule-agent-public

# as the schedule user
cd /srv/schedule-agent-public
git clone <your-fork-url> .
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python install.py     # interactive wizard writes .env
```

Pick `ntfy` for the notifier (iMessage is macOS-only) and `None` for the
todo source (Apple Reminders is macOS-only) when the wizard asks.

## Install the unit

```bash
# as root
sed -e 's|<USER>|schedule|g' -e 's|<REPO>|/srv/schedule-agent-public|g' \
    /srv/schedule-agent-public/deploy/systemd/schedule-agent.service \
    > /etc/systemd/system/schedule-agent.service

systemctl daemon-reload
systemctl enable --now schedule-agent
systemctl status schedule-agent
journalctl -u schedule-agent -f
```

The orchestrator listens on `0.0.0.0:8787`. Put it behind a reverse proxy
(nginx, Caddy, Tailscale serve) with TLS — don't expose the raw port to
the public internet.
