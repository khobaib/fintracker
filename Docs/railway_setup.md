# FINTRACKER — Railway Deployment Guide

Deploy your Fintracker bot to Railway so it runs 24/7 without your laptop.

---

## What is Railway?

Railway is a cloud hosting platform. Your bot runs on their servers permanently.
Free tier is sufficient for personal use. No credit card required to start.

---

## Prerequisites

Before deploying:
- Code is pushed to GitHub (`github.com/yourusername/fintracker`)
- `schema_v3_final.sql` is in the repo root
- `requirements.txt` is in the repo root
- `.env` is in `.gitignore` (never commit your secrets)

---

## Step 1 — Create Railway account

1. Go to https://railway.app
2. Click **Login** → **Login with GitHub**
3. Authorize Railway to access your GitHub account

Using GitHub login connects Railway directly to your repos — no separate signup needed.

---

## Step 2 — Create new project

1. Click **New Project**
2. Click **Deploy from GitHub repo**
3. Select your `fintracker` repo from the list

Railway will detect it as a Python project automatically.

---

## Step 3 — Add environment variables

Go to your project → **Variables** tab.
Add each variable one by one — do NOT press Deploy yet.

| Variable | Value |
|----------|-------|
| `SLACK_BOT_TOKEN` | Your bot token (starts with `xoxb-`) |
| `SLACK_APP_TOKEN` | Your app token (starts with `xapp-`) |
| `EXPENSE_CHANNEL` | `expenses` (or your channel name) |
| `DB_PATH` | `/data/fintracker.db` |

> **Note:** Do not add `ANTHROPIC_API_KEY` yet — it is only needed for Phase 2 (AI classifier).
> You can add it later when that feature is built.

---

## Step 4 — Add a persistent volume

This is the most important step. Without a volume, your database resets every time Railway redeploys your code.

1. In your project, click **New** → **Volume**
2. Set mount path to `/data`
3. Click **Create**

Your `fintracker.db` will live at `/data/fintracker.db` permanently — surviving all future deploys.

> **Why this order matters:** The volume must be created BEFORE the first deploy.
> If you deploy first, the database gets created in the wrong location and resets on every redeploy.

---

## Step 5 — Set the start command

1. Go to your project → **Settings** → **Deploy**
2. Find **Start Command**
3. Set it to:

```
python bot.py
```

---

## Step 6 — Deploy

Click **Deploy**. Railway will:
1. Pull your code from GitHub
2. Run `pip install -r requirements.txt`
3. Start `python bot.py`

This takes about 2 minutes. Watch the **Logs** tab in real time.

When you see these lines, the bot is live:

```
INFO: Starting Fintracker Slack bot...
INFO: Bolt app is running!
```

---

## Step 7 — Database initialisation (automatic)

You do not need to do anything manually. When `bot.py` starts, it automatically:

1. Checks if `/data/fintracker.db` exists
2. If not — creates it and loads the schema from `schema_v3_final.sql`
3. If yes — skips creation and starts normally

The full startup sequence on Railway:

```
Railway pulls code from GitHub
    ↓
pip install -r requirements.txt
    ↓
python bot.py starts
    ↓
init_db() runs → creates /data/fintracker.db if missing
    ↓
Bot connects to Slack
    ↓
"Bolt app is running!"
```

---

## Step 8 — Verify in Slack

Go to your `#expenses` channel and paste a test entry:

```
30 April
rickshaw - 60
fuchka - 120
```

The bot should respond within a few seconds with a review summary.

---

## Updating the bot after Railway deployment

Every time you change code or schema:

```
git add .
git commit -m "Description of change"
git push
```

Railway detects the push and redeploys automatically in about 2 minutes.

### When you update schema_v3_final.sql

After pushing, Railway restarts `bot.py`. The `init_db()` function refreshes the rules, purposes, and payment methods on the live database automatically. Your transaction data is never touched.

---

## Checking logs

If something is not working, check the logs:

1. Go to your Railway project
2. Click on your service
3. Click **Logs** tab

Common things to look for:

| Log message | Meaning |
|-------------|---------|
| `Bolt app is running!` | Bot started successfully |
| `invalid_auth` | SLACK_BOT_TOKEN is wrong or missing |
| `No such file: schema_v3_final.sql` | Schema file not pushed to GitHub |
| `FileNotFoundError: .env` | Normal — Railway uses Variables tab, not .env file |

---

## Adding environment variables later

To add `ANTHROPIC_API_KEY` when Phase 2 is ready:

1. Go to your Railway project → **Variables** tab
2. Add the variable
3. Railway redeploys automatically

---

## Troubleshooting

**Bot deployed but not responding in Slack**
- Check that `message.channels` and `message.im` are subscribed under Event Subscriptions in your Slack app settings
- Check that the bot is invited to your `#expenses` channel: `/invite @Fintracker`
- Check Railway logs for errors

**Database resets on every deploy**

This happens when the volume was not created before the first deploy, so the DB is living inside the container at `/app/fintracker.db` instead of `/data/fintracker.db`.

Recovery steps (do NOT delete the service — you will lose data):

1. Go to Railway → your service → **Variables** tab — confirm `DB_PATH` is set to `/data/fintracker.db`
2. Install Railway CLI — use whichever method fits your system:
   - **macOS:** `brew install railway`
   - **Windows (Scoop):** first install Scoop, then `scoop install railway`
     - To install Scoop, open PowerShell (NOT as admin) and run:
       `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
       then: `Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression`
   - **Node.js (any OS):** `npm install -g @railway/cli`
   - **Linux/macOS one-liner:** `bash <(curl -fsSL railway.com/install.sh)`
3. Run `railway login`, then `railway link`, then `railway ssh`
4. Inside the shell, copy the DB to the volume:
   ```
   cp /app/fintracker.db /data/fintracker.db
   ```
5. Verify: `ls /data/` — should show `fintracker.db`
6. Exit the shell and trigger a redeploy from the Railway dashboard

From this point on, all data persists on the volume permanently.

**`ModuleNotFoundError`**
- A package is missing from `requirements.txt`
- Add it locally, push — Railway will reinstall

---

## Railway free tier limits

- 500 hours/month execution time (enough for 24/7 on one service)
- 1 GB volume storage (more than enough for SQLite)
- Sleeps after inactivity on hobby plan — upgrade to Developer plan ($5/month) if you need guaranteed uptime

---

## File checklist before deploying

Make sure these files are in your GitHub repo:

```
✅ bot.py
✅ parser.py
✅ schema_v3_final.sql
✅ requirements.txt
✅ init_db.py
✅ .gitignore

❌ .env              (secrets — never commit)
❌ fintracker.db     (your data — never commit)
❌ venv/             (local environment — never commit)
```
