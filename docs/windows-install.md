# AutoPlan on Windows — full walkthrough

Total time: ~15 minutes, most of it waiting for downloads.

Everything below is copy-paste. You won't need to write anything yourself,
just paste the commands into PowerShell one block at a time.

---

## Step 1 — Install Python (one time, ~3 min)

AutoPlan is written in Python. Windows doesn't ship with it, so you need
to install it once.

1. Go to **<https://www.python.org/downloads/windows/>**.
2. Under **Stable Releases**, click the **Latest Python 3 Release**
   (currently 3.12 or 3.13 — anything 3.9 or newer works).
3. Scroll down on the release page to **Files** and click
   **Windows installer (64-bit)**. It'll download a file called
   something like `python-3.13.0-amd64.exe`.
4. Open the downloaded file. **Important**: on the first screen of the
   installer, **check the box that says "Add python.exe to PATH"** at
   the bottom. Then click **Install Now**.
5. When it finishes, close the installer.

**Verify it worked:** press **Windows + R**, type `powershell`, press
Enter. A blue terminal window opens. Type:

```powershell
python --version
```

You should see `Python 3.13.0` (or whatever version you installed). If
you see *"python is not recognized"*, the "Add to PATH" checkbox was
missed — re-run the installer, choose **Modify**, and check the box.

---

## Step 2 — Install Git (one time, ~2 min)

Git is the tool that downloads AutoPlan's code.

1. Go to **<https://git-scm.com/download/win>**. The download starts
   automatically; if not, click the "64-bit Git for Windows Setup" link.
2. Open the downloaded `Git-x.x.x-64-bit.exe`.
3. Click **Next** through all the default options. Don't overthink
   them — the defaults are fine.
4. When it finishes, close the installer.

**Verify:** back in PowerShell:

```powershell
git --version
```

You should see something like `git version 2.45.0.windows.1`.

---

## Step 3 — Download AutoPlan (~30 seconds)

Pick where on your computer AutoPlan should live. Your user folder is a
safe choice. In PowerShell:

```powershell
cd $HOME
git clone https://github.com/ELuria17/schedule-agent.git autoplan
cd autoplan
```

The `git clone` line downloads the whole project into a new folder
called `autoplan` inside your user folder. The `cd autoplan` line steps
inside it.

---

## Step 4 — Install AutoPlan's dependencies (~1-2 min)

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Break-down:
- `python -m venv venv` makes a clean, isolated Python environment
  inside the project so it doesn't conflict with anything else on your
  computer.
- `venv\Scripts\activate` steps into that environment. You'll know it
  worked because your prompt now starts with `(venv)`.
- `pip install -r requirements.txt` downloads every library AutoPlan
  needs. Takes a minute. You'll see a lot of output; that's normal.

When it's done you should see a line like
`Successfully installed anthropic-0.96.0 caldav-1.3.9 ...`.

---

## Step 5 — Launch AutoPlan for the first time (~30 seconds)

Before you run the next command, have these two things ready to paste:

- An **Anthropic API key** — sign up free at
  [console.anthropic.com](https://console.anthropic.com/settings/keys),
  click **Create Key**, copy the long string that starts with
  `sk-ant-...`. Keep it somewhere handy.
- Your **iCloud app-specific password** — go to
  [appleid.apple.com](https://appleid.apple.com/), sign in, then under
  **Sign-In and Security** click **App-Specific Passwords** → **+** →
  label it "AutoPlan" → copy the generated `xxxx-xxxx-xxxx-xxxx` code.
  (This is a safe way to let apps access your iCloud calendar without
  handing them your real Apple password.)

Then run:

```powershell
python run.py
```

A few things happen:
1. The first time you do this, Windows Firewall pops up a dialog
   asking whether Python should be allowed to talk on the network.
   Click **Allow** (private networks is enough).
2. Your default browser opens automatically to a setup page —
   `http://127.0.0.1:8787/setup`.
3. The setup page has a simple form. Fill it in:
   - **Anthropic API key** — paste the one you copied.
   - **Calendar** — pick **iCloud**. Enter your primary Apple ID email
     and the app-specific password.
   - **Task source** — if you're a student with Canvas, pick **Canvas**
     and follow the prompts. If not, pick **None / manual only** — you
     can still type tasks into the hub by hand.
   - **Notifier** — **ntfy.sh** is pre-selected (iMessage is
     macOS-only). The wizard generates a random topic for you. Copy
     that topic; you'll use it on your phone in a minute.
4. Click **Save and create Anthropic agent**.

When the success page appears, close the browser tab and go back to
PowerShell. Press **Ctrl+C** to stop AutoPlan for a moment.

---

## Step 6 — Get phone notifications (~2 min)

Install the **ntfy** app on your phone (it's free, no account):

- iPhone: <https://apps.apple.com/us/app/ntfy/id1625396347>
- Android: <https://play.google.com/store/apps/details?id=io.heckel.ntfy>

Open the ntfy app, tap **+** to add a subscription, and paste the
**topic** the setup wizard generated for you. Done — every morning
summary AutoPlan sends will appear as a push notification on your
phone.

> The topic is a secret: anyone who knows it can both read and send
> messages on it. Don't share it.

---

## Step 7 — Use AutoPlan day-to-day

Start it up whenever you want to use it:

```powershell
cd $HOME\autoplan
venv\Scripts\activate
python run.py
```

Your browser opens to the hub at
`http://127.0.0.1:8787/hub?key=<your-token>`. Bookmark that URL — it's
the page you'll come back to every day.

When you're done, go to PowerShell and press **Ctrl+C**. AutoPlan stops.

### Making it easier to launch

AutoPlan comes with a file called `run.bat` that does all three of the
above lines for you. You can:

- **Double-click** `run.bat` in File Explorer to start AutoPlan.
- Make a shortcut: right-click `run.bat` → **Send to** →
  **Desktop (create shortcut)**. Now AutoPlan is one click from the
  desktop.
- **Launch at login**: press **Windows + R**, type `shell:startup`,
  press Enter. Drag the shortcut you made into the folder that opens.
  AutoPlan will now start automatically whenever you sign in — it runs
  in a background console window.

---

## Troubleshooting

**"python is not recognized"**
You missed the "Add to PATH" checkbox during install (Step 1). Re-run
the Python installer, click **Modify**, and check the box.

**"git is not recognized"**
Close PowerShell and open a new window. If it still doesn't work,
reinstall Git and make sure "Git from the command line and also from
3rd-party software" is selected during the installer.

**The ntfy app doesn't receive notifications**
- Check you subscribed to the exact same topic the setup wizard
  generated (it's in the `.env` file in the `autoplan` folder, under
  `NTFY_TOPIC=`).
- On iPhone, make sure notifications are on for ntfy in iOS Settings →
  Notifications → ntfy.

**Windows SmartScreen blocks `run.bat`**
If you see "Windows protected your PC," click **More info** →
**Run anyway**. SmartScreen is cautious about unsigned scripts. This
only happens the first time.

**Something else broke**
Open an issue at
<https://github.com/ELuria17/schedule-agent/issues> with the exact
error message copied from PowerShell.
