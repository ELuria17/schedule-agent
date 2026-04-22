# Connecting AutoPlan to your Google Calendar

This page walks you through connecting AutoPlan to Google Calendar. You'll
do this **once**, and AutoPlan will remember it forever. The whole thing
takes about 10 minutes.

**Good news:** none of the steps are technical. You'll click buttons in a
Google website, download one small file, and drop it in a folder. That's it.

**Why this is needed:** Google requires every app that touches your calendar
to identify itself, and they don't hand out that identification for free to
open-source tools. So each person who uses AutoPlan needs to register
AutoPlan with Google under their own name. You're essentially telling Google
"I'm allowed to use this thing on my own account." It sounds annoying but
it takes a few minutes and you only do it once.

---

## What you need before you start

- A Google account (the one whose calendar AutoPlan should read and write)
- A web browser
- AutoPlan already installed (see the main README)
- About 10 minutes

---

## Part 1 — Tell Google that you want to use AutoPlan

### Step 1. Open the Google Cloud Console

Go to this link in your browser:

> **https://console.cloud.google.com/**

If it asks you to sign in, use the same Google account whose calendar you
want AutoPlan to read/write.

The first time you visit, it may show you a page titled "Welcome" and ask
you to agree to Terms of Service. Check the box, pick your country, and
click **Agree and Continue**.

### Step 2. Create a new project

Look at the **top of the page**, to the right of the "Google Cloud" logo.
There is a dropdown — it might say "Select a project" or show an existing
project name. Click it.

A window pops up. Click **NEW PROJECT** (top right of the window).

Fill in:

- **Project name:** `AutoPlan` (anything is fine)
- **Location:** leave as "No organization"

Click **CREATE**.

Wait about 10 seconds. A notification in the top right will say
"Creating project AutoPlan… Done." When it's done, click **SELECT PROJECT**
on that notification (or click the project dropdown again and pick AutoPlan).

**You should now see "AutoPlan" at the top of the page, next to the Google
Cloud logo.** If you don't, click the dropdown and pick it.

### Step 3. Turn on the Calendar API

Every Google tool is off by default. You need to turn on the calendar one.

In the **search bar at the very top of the page** (it says "Search
products and resources"), type:

> **Google Calendar API**

Click the first result. You'll land on a page titled "Google Calendar API"
with a big blue **ENABLE** button.

Click **ENABLE**.

Wait a few seconds. The page will change to show statistics and tabs like
"Overview / Metrics / Quotas". That means it's on.

### Step 4. Set up the "consent screen"

Think of this like the screen that pops up on your phone saying "This app
wants to access your photos — Allow?" You're building that screen now.

On the left sidebar, click **APIs & Services**, then **OAuth consent
screen**. *(If you don't see a sidebar, click the ≡ menu icon in the top
left first.)*

Google now has two layouts depending on when you made your project. Look
at your screen:

**If you see a page that says "Get started":**
   1. Click **GET STARTED**.
   2. App name: **AutoPlan**
   3. User support email: your email (pick from the dropdown)
   4. Click **NEXT**.
   5. Audience: pick **External**. Click **NEXT**.
   6. Contact email: your email again. Click **NEXT**.
   7. Agree to the User Data Policy. Click **CONTINUE**, then **CREATE**.

**If you see a page asking for User Type:**
   1. Pick **External**. Click **CREATE**.
   2. App name: **AutoPlan**
   3. User support email: your email (pick from the dropdown)
   4. Scroll to "Developer contact information". Put your email.
   5. Click **SAVE AND CONTINUE**.
   6. On the next page ("Scopes"), just click **SAVE AND CONTINUE**. You
      don't need to add anything.
   7. On the next page ("Test users"), click **+ ADD USERS**, type your
      own Google email, click **ADD**, then **SAVE AND CONTINUE**.
   8. Click **BACK TO DASHBOARD**.

> **Why "External"?** Google's two choices are "Internal" (only if you
> pay for Google Workspace at a company) or "External" (everyone else).
> Pick External. The word sounds alarming but it just means "a normal
> Google account." AutoPlan won't actually be shown to the public —
> only you can use it.

### Step 5. (Only if you had to add a "Test user") Add yourself as a test user

Some Google Cloud layouts skip this. If yours asked you to add test
users in Step 4, you're already done. If not:

On the left sidebar, under **APIs & Services → OAuth consent screen**,
find a section called **Test users** and click **+ ADD USERS**. Type your
own Google email, click **ADD**, then **SAVE**.

> **Why?** While the app is "in testing" only users on this list can
> use it. Adding yourself lets you use AutoPlan. (If you ever want a
> family member to also use AutoPlan on their own calendar, they follow
> this whole doc on their own account — you don't need to add them here.)

### Step 6. Create the credentials file

This is the file you'll download and give to AutoPlan.

On the left sidebar, click **APIs & Services → Credentials**.

At the top, click **+ CREATE CREDENTIALS** and pick **OAuth client ID**.

Fill in:

- **Application type:** pick **Desktop app**
- **Name:** `AutoPlan desktop` (anything is fine)

Click **CREATE**.

A window pops up saying "OAuth client created." It shows a Client ID and
Client Secret. **Ignore those.** Instead, click **DOWNLOAD JSON**.

A file called something like `client_secret_123456-abc.apps.googleusercontent.com.json`
downloads to your Downloads folder.

**Rename this file to `credentials.json`** — that's easier to remember and
the instructions below assume that name.

> On Mac: right-click the file → Rename.
> On Windows: right-click the file → Rename.

### Step 7. Close the "OAuth client created" window

Just click **OK** or the X. You're done with Google Cloud Console forever.
(Unless you ever want to use AutoPlan on a different Google account —
then you'd repeat this whole process on that account.)

---

## Part 2 — Give AutoPlan the credentials file

### Step 8. Move `credentials.json` to the AutoPlan folder

Move the downloaded `credentials.json` file to the AutoPlan folder on your
computer.

**On Mac, if you installed the .dmg app:** the folder is
`~/Library/Application Support/schedule-agent/`. Easiest way to open it:

1. Open Finder.
2. Press **⌘ + Shift + G** (Go to Folder).
3. Paste: `~/Library/Application Support/schedule-agent/`
4. Press Return.

Drag `credentials.json` into that folder.

**On Mac/Linux, if you `git clone`d the source:** put it inside the
`schedule-agent-public` folder (next to `run.py`).

**On Windows:** the folder is `%APPDATA%\schedule-agent\`. Easiest way:

1. Open File Explorer.
2. Click the address bar at the top.
3. Paste: `%APPDATA%\schedule-agent\`
4. Press Enter.

Drag `credentials.json` into that folder.

### Step 9. Tell AutoPlan where the file is

Open your `.env` file in any text editor (TextEdit on Mac, Notepad on
Windows). It lives in the same folder as `credentials.json`.

Add this line at the bottom (replace the path with the **full path** to
wherever you just put `credentials.json`):

**Mac:**
```
GOOGLE_CALENDAR_CREDENTIALS=/Users/yourname/Library/Application Support/schedule-agent/credentials.json
```

**Windows:**
```
GOOGLE_CALENDAR_CREDENTIALS=C:\Users\yourname\AppData\Roaming\schedule-agent\credentials.json
```

**Linux:**
```
GOOGLE_CALENDAR_CREDENTIALS=/home/yourname/.local/share/schedule-agent/credentials.json
```

Save the file.

> **Tip:** If you right-click `credentials.json` and pick "Get Info" (Mac)
> or "Properties" (Windows), you can see the full path and copy it.

### Step 10. Create a "Study Blocks" calendar in Google Calendar

AutoPlan keeps its scheduled events in a dedicated calendar so they don't
clutter your main one. You'll create it once.

1. Open **https://calendar.google.com** in your browser.
2. On the **left sidebar**, find the section labeled **Other calendars**.
3. Click the **+** next to it.
4. Pick **Create new calendar**.
5. Name: **Study Blocks** (exactly this — capital S, capital B, with a space)
6. Click **Create calendar**.

Wait 5 seconds, then refresh the page. "Study Blocks" should show up in
your sidebar under "My calendars".

> **Want a different name?** Name it whatever you like, but then add this
> line to your `.env` file: `WRITE_CALENDAR_NAME=YourCalendarName`

### Step 11. Run the one-time authorization

Open Terminal (Mac/Linux) or Command Prompt (Windows), go to the AutoPlan
folder, and run:

```
python authorize_google.py
```

Your web browser will pop up. Sign in with the Google account you want
AutoPlan to use.

Google will show a scary-looking warning: **"Google hasn't verified this
app"**. This is normal and expected — AutoPlan isn't a publicly-verified
app, it's your own copy that only you can use. Here's what to do:

1. Click the small link that says **"Advanced"** (bottom left of the warning).
2. Click **"Go to AutoPlan (unsafe)"**. It's safe. "Unsafe" is Google's
   default wording for any unverified app; it just means you're trusting
   your own setup.
3. On the next page, check all the permission boxes and click **Continue**.
4. The browser will show "The authentication flow has completed." You
   can close the tab.

Back in Terminal, you'll see:

```
Done. Refresh token saved to /path/to/google_token.json
You can close the browser tab now. AutoPlan is ready to use.
```

**You're done.** AutoPlan now has permission to read and write your Google
calendar, and it will never need to ask you again — unless you delete the
`google_token.json` file, in which case just re-run `python authorize_google.py`.

---

## Testing that it worked

From the same Terminal/Command Prompt:

```
python -c "from schedule_config import CALENDAR; print(CALENDAR.health_check())"
```

You should see something like:

```
{
  'provider': 'GoogleCalendarProvider',
  'token_present': True,
  'write_calendar': 'Study Blocks',
  'write_calendar_exists': True,
  'total_calendars_visible': 4,
}
```

If `write_calendar_exists` is `True`, everything's wired up correctly.

If you see an error instead, skip to **Troubleshooting** below.

---

## Troubleshooting

### "credentials.json not found at …"
The path in your `.env` file doesn't match where the file actually is.
Double-check that you:
- Used the **full path** (not `~`, not a relative path)
- Didn't accidentally put quotes around the path in `.env`
- The file is actually named `credentials.json` (not `credentials.json.txt`
  — Windows sometimes hides the `.txt`. Turn on "File name extensions" in
  File Explorer's View tab to see it for sure.)

### "No refresh token yet"
You haven't run `python authorize_google.py` yet, or it didn't finish.
Run it again.

### "Google hasn't verified this app" warning won't go away
This is by design. Google only "verifies" apps after the developer submits
a privacy policy, demo video, and goes through a several-week review.
AutoPlan is self-hosted, so it isn't verified. Use the "Advanced → Go to
AutoPlan (unsafe)" link as described in Step 11. It is safe; the "unsafe"
label is Google's default for any unverified app.

### "Access blocked: AutoPlan has not completed the Google verification process"
This means you didn't add yourself as a test user in Step 5. Go back to
**console.cloud.google.com → APIs & Services → OAuth consent screen → Test
users** and add your own email.

### "Write calendar 'Study Blocks' not found on Google"
You didn't create the calendar in Step 10, or you named it differently.
Double-check the spelling is **exactly** `Study Blocks` (capital S, capital
B, one space) at calendar.google.com → left sidebar. Or, set
`WRITE_CALENDAR_NAME=your-actual-name` in `.env`.

### I want to switch to a different Google account
Delete `google_token.json` (in the same folder as `credentials.json`) and
run `python authorize_google.py` again. It'll ask you to sign in again and
you can pick a different account.

### I don't want AutoPlan to see all my calendars
By default AutoPlan reads every calendar on your account to know when
you're busy. If you want to restrict it, open `schedule_config.py` and
uncomment the `read_calendar_allowlist=[…]` line, listing only the
calendars you want it to consider.
