# Plan: schedule-agent SaaS — $10/mo with $5 Anthropic cap

> **Status:** shelved 2026-04-30. Pursuing mobile app / web hub first; come back to this once that lands.

## Context

The current `~/schedule-agent-public/` is a **local app** — every install holds its own `ANTHROPIC_API_KEY`, creates its own Managed Agent + Environment under the user's Anthropic account, and pays Anthropic directly. That works for technically-comfortable users; it's a wall for the average user who doesn't want to "search up keys and be technical".

Goal: turn schedule-agent into a subscription product. **$10/mo flat, $5/mo Anthropic-cost cap per user**, no API keys for end users to manage. The dev (you) owns one Anthropic account; users hit a proxy you run; the proxy meters spend per user.

This plan is intentionally lean for v0.2 — ship a working subscription + proxy, *not* a marketplace. Stretch features (referrals, tiers, teams) come later.

### Two non-negotiables before any code

1. **API key never ships in the binary.** The current `setup.py` plus `Anthropic()` reads must be moved to the server. The local app authenticates to *your server*, not Anthropic.
2. **Single Anthropic account, your billing.** No reselling Anthropic credits to users — that's Anthropic ToS land. You charge for the **schedule-agent service**; Anthropic spend is your COGS.

### Economics check (you should re-verify before shipping)

| Item | $/user/mo |
|---|---|
| Subscription revenue | +10.00 |
| Stripe fee (2.9% + 30¢) | −0.59 |
| Anthropic hard cap | −5.00 |
| **Gross / user / mo** | **+4.41** |
| Hosting (Fly.io + Neon free tier → small paid) | shared, ~$10–30/mo total |
| Email (Postmark, magic links) | ~$0 at low volume |

Sonnet 4.6 pricing today: $3/Mtok input, $15/Mtok output. A typical Schedule Agent session is ~5K input + 1K output ≈ $0.03 → $5 cap = ~165 sessions/mo = ~5/day. Fine for casual users; power users will blow the cap. **Recommend monitoring the actual session-cost distribution after 30 paid users and revisiting** — you may need a $20/$10 tier.

---

## Architecture

```
┌──────────────────┐    HTTPS (your auth token)    ┌─────────────────────┐    HTTPS (your Anthropic key)    ┌──────────────────┐
│  Local app       │ ────────────────────────────► │  Your proxy server  │ ─────────────────────────────►   │  api.anthropic   │
│  (orchestrator)  │ ◄──────────────────────────── │  (FastAPI on Fly)   │ ◄─────────────────────────────   │  .com            │
└──────────────────┘    SSE stream (proxied)       └─────────────────────┘    SSE stream                    └──────────────────┘
        │                                                  │
        │                                                  ├── Postgres (users, subs, usage)
        │                                                  ├── Stripe Checkout + Customer Portal + webhook
        │                                                  └── Postmark (magic links)
        │
        └── keychain stores: user email, JWT auth token, last sub status
```

**Anthropic SDK already supports proxying.** `Anthropic(base_url="https://api.youragent.app", api_key=jwt_from_keychain)` — the SDK sends the same wire calls; your proxy just speaks the same protocol. No SDK fork needed.

---

## What to build (3 phases, ~3–6 weeks total)

### Phase A — Auth + subscription gate (1.5 weeks)

**Goal:** users can sign up, pay $10/mo via Stripe, and the local app knows whether they're in good standing. *No proxy yet — Anthropic key still in `.env` on user's machine during Phase A so you can validate demand before investing in the proxy.*

**New repo:** `~/schedule-agent-server/` (separate from `~/schedule-agent-public/`).

- FastAPI app with these endpoints:
  - `POST /auth/request_link` → emails a magic link (`https://you.app/auth/verify?token=…`).
  - `GET  /auth/verify` → exchanges one-time token for a long-lived JWT, redirects to `schedule-agent://login?token=<jwt>`.
  - `GET  /me` → returns `{email, subscription_status, current_period_end, cents_used_this_period}`.
  - `POST /billing/checkout` → returns Stripe Checkout URL.
  - `POST /billing/portal` → returns Stripe Customer Portal URL.
  - `POST /webhooks/stripe` → handles `customer.subscription.{created,updated,deleted}` and `invoice.paid`.
- Postgres schema:
  ```sql
  users(id uuid PK, email text UNIQUE, created_at timestamptz)
  subscriptions(user_id PK FK, stripe_customer_id, stripe_subscription_id,
                status text, current_period_end timestamptz)
  auth_tokens(token text PK, user_id FK, expires_at, revoked_at)
  -- usage tables come in Phase B
  ```
- Hosting: Fly.io (free SSE, deploys via `flyctl`). Neon Postgres free tier. Postmark for email. Caddy/Fly built-in HTTPS.

**Local-app changes (`~/schedule-agent-public/`):**
- New file `auth_client.py`: handles magic-link redirect via custom URL scheme `schedule-agent://login?token=…` (register in `packaging/macos/schedule-agent.spec` Info.plist with `CFBundleURLTypes`).
- Setup wizard (`setup_server.py:1080-1090`): replace the `ANTHROPIC_API_KEY` field with a "Sign in / Sign up" button → opens browser to `https://you.app/signin`. After magic-link verify, app receives token, stores in macOS Keychain via `keyring`, fetches `/me` and shows subscription status.
- Hub gets a new "Billing" tab: usage gauge ($0.00 / $5.00), "Manage subscription" button (opens Stripe portal), expiry date.
- `orchestrator.py:43` — keep `REPLAN_TOKEN` for local hub auth, but add a new outbound layer: every agent call needs the JWT.

### Phase B — Proxy + per-user metering (1.5 weeks)

**Goal:** Anthropic key leaves the user's machine entirely. Cost cap enforced server-side.

**Server additions:**
- New endpoints under `/v1/*` that mirror the Anthropic Managed Agents surface used by `orchestrator.py`:
  - `POST /v1/beta/environments` (create one shared env at server boot, hide from API)
  - `POST /v1/beta/agents` — return the shared agent_id (one per app version, you create these by hand once)
  - `POST /v1/beta/sessions`
  - `POST /v1/beta/sessions/{id}/events`
  - `GET  /v1/beta/sessions/{id}/events`
  - `GET  /v1/beta/sessions/{id}/events:stream` ← **SSE streaming proxy**
  - `GET  /v1/beta/files`, `GET /v1/beta/files/{id}`
  - `POST /v1/beta/sessions/{id}:archive`
- For each request: validate JWT → check subscription `active` → check `usage.cents_this_period < 500` → forward to Anthropic with your real key → on response, parse usage block, atomically increment `usage_periods.cents_used`.
- Cap behavior: refuse `POST /v1/beta/sessions` with HTTP 402 + JSON `{"error": "monthly_cap_reached", "resets_at": "2026-05-01T00:00:00Z"}`. Don't kill mid-session — accept overshoot on a session that started under the cap. Soft fail is better UX than truncated mid-stream.
- Streaming: FastAPI's `StreamingResponse` with `media_type="text/event-stream"` + `httpx.AsyncClient.stream()` upstream. Tested pattern; the gotcha is *not* using a buffering reverse proxy in front (Caddy is fine; Cloudflare Free needs `Cache-Control: no-cache` + may still buffer — use Fly.io's edge directly, no CF in front).
- New tables:
  ```sql
  usage_periods(user_id, period_start, period_end, cents_used INT, PRIMARY KEY(user_id, period_start))
  usage_log(id, user_id, ts, model, input_tok, output_tok, cents)  -- audit
  ```
- Period rollover: Stripe webhook `invoice.paid` is the source of truth — when fired, insert a fresh `usage_periods` row with the new period bounds. Avoid drifting from Stripe's calendar.

**Local-app changes:**
- `orchestrator.py:58` `anthropic = Anthropic()` → `anthropic = Anthropic(base_url=PROXY_URL, api_key=keychain.get_token())`.
- Drop `ANTHROPIC_API_KEY`, `AGENT_ID`, `ENVIRONMENT_ID` from `.env`. The proxy returns the right agent_id at sign-in.
- `setup.py` is no longer needed at install time — agent + env exist server-side.
- Hub billing tab now shows live usage from `/me` (poll every 60s).
- Cap-reached UI: red banner in the hub, link to upgrade (future) or wait for reset.

### Phase C — Polish + ship (1–2 weeks)

- Marketing site copy at `site/` updated with pricing, "How it works", privacy policy, ToS. **Privacy policy is required** — you're now a data processor for users' calendars/tasks, even if you only see the prompts the agent generates. Keep proxy *non-logging* of prompt bodies (only token counts) to minimize liability.
- Failed-payment UX: `subscription.status='past_due'` → 7-day grace, then revoke at next session start.
- Refund/cancel: Stripe Customer Portal handles UI; your webhook flips `current_period_end`.
- New signed+notarized .dmg (you've got the pipeline now: `packaging/macos/sign_and_notarize.sh`).
- Light load test: 50 concurrent SSE streams to confirm Fly.io machine size.

---

## Critical files to modify (paths)

**Local app (existing):**
- `~/schedule-agent-public/orchestrator.py:58` — Anthropic client base_url
- `~/schedule-agent-public/orchestrator.py:43` — auth flow alongside REPLAN_TOKEN
- `~/schedule-agent-public/setup_server.py:242-393` — wizard step rewrite
- `~/schedule-agent-public/setup_server.py:1080-1090` — wizard form HTML
- `~/schedule-agent-public/install.py:196-201` — drop the ANTHROPIC_API_KEY prompt
- `~/schedule-agent-public/setup.py` — delete (server now owns agent creation)
- `~/schedule-agent-public/providers/email_task_extractor.py:70` — same `Anthropic()` patch
- `~/schedule-agent-public/hub.html` — billing tab
- `~/schedule-agent-public/packaging/macos/schedule-agent.spec` — register `schedule-agent://` URL scheme
- `~/schedule-agent-public/site/` — pricing + ToS + privacy

**Local app (new):**
- `~/schedule-agent-public/auth_client.py` — magic-link round trip, keychain via `keyring`

**New repo (separate):**
- `~/schedule-agent-server/` — FastAPI proxy, Postgres, Stripe, Postmark

**Reusable from existing code:** the existing reconnect pattern in `orchestrator.py:321-360` (lossless SSE replay with dedupe) is the same pattern your proxy needs on the upstream side — port it.

---

## Verification

End-to-end test path (run after Phase B):

1. **Sign up.** New email → request magic link → click → app opens via URL scheme → `/me` returns `subscription_status='trialing'` (if you offer a 7-day trial) or `'incomplete'`.
2. **Pay.** Click "Subscribe" → Stripe Checkout → return → webhook fires → `/me` shows `'active'`, `cents_used_this_period=0`.
3. **Session.** Trigger schedule-agent from hub. Confirm via server logs: JWT validated, sub active, cap check passed, request forwarded to Anthropic, usage row created, `cents_used` incremented by ~3.
4. **SSE works.** Watch the hub's session view — events stream live with no buffering hiccup. (This is the make-or-break test; if SSE breaks here, Phase B fails.)
5. **Cap check.** Manually set `cents_used=499` for the test user → trigger another session → server returns 402 with `monthly_cap_reached` → hub shows red banner.
6. **Period rollover.** Manually fire a `invoice.paid` webhook → new `usage_periods` row → `cents_used` resets.
7. **Cancel.** Stripe Customer Portal → cancel → webhook fires → `current_period_end` recorded → app keeps working until that date → subsequent session-start returns 402 `subscription_inactive`.

Automated tests:
- New `tests/test_auth_client.py` — keychain round-trip, custom URL scheme parsing.
- New `tests/test_proxy_metering.py` (server side) — token counts in, cents out, cap enforcement, period rollover.
- Roadblocks runner stays green; signal A2 still passes (no `calendar_*` tools).

Smoke before shipping the .dmg:
- `packaging/macos/sign_and_notarize.sh` — already working as of 2026-04-30.
- `tests/schedule-agent-e2e` agent — exercise the new sign-in wizard end-to-end on a clean macOS account.

---

## What I am NOT doing in this plan

- No Apple/Google sign-in (magic link is enough for v0.2; add OAuth later).
- No teams/orgs (single-user accounts only).
- No usage-based overage billing (cap-and-stop, not cap-and-charge).
- No mobile app or web hub (still a local app + browser hub talking to localhost).
- No Anthropic-credits reseller flow (ToS minefield).
- No referrals, no annual pricing, no coupons (Stripe supports them — add post-launch when you have a reason).
