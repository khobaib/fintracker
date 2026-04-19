# FINTRACKER — Setup Guide

A personal finance tracker with a Slack bot interface.
Paste your daily expenses in Slack, get an instant review, save with one word.

---

## What you need

- Python 3.11+
- A Slack workspace (your personal one works fine)
- A Railway.app account (free tier) for hosting
- An Anthropic API key (for AI classification of ambiguous entries)

---

## Step 1 — Create the Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it `Fintracker`, pick your workspace

### Enable Socket Mode
- **Settings → Socket Mode** → Enable Socket Mode
- Generate an App-Level Token with `connections:write` scope
- Save it as `SLACK_APP_TOKEN` (starts with `xapp-`)

### Add Bot Token Scopes
- **OAuth & Permissions → Scopes → Bot Token Scopes**, add:
  - `chat:write`
  - `channels:history`
  - `im:history`
  - `im:write`
  - `commands`

### Create Slash Commands
Go to **Slash Commands → Create New Command** for each:

| Command   | Request URL      | Description              |
|-----------|-----------------|--------------------------|
| `/trip`   | (Socket Mode — leave blank) | Manage trips |
| `/rate`   | (Socket Mode — leave blank) | Set exchange rates |
| `/rates`  | (Socket Mode — leave blank) | View exchange rates |
| `/actual` | (Socket Mode — leave blank) | Set bank-confirmed amount |
| `/summary`| (Socket Mode — leave blank) | Monthly summary |

### Enable Event Subscriptions
- **Event Subscriptions** → Enable Events
- Subscribe to bot events: `message.channels`, `message.im`

### Install the app
- **OAuth & Permissions → Install to Workspace**
- Copy the **Bot User OAuth Token** → save as `SLACK_BOT_TOKEN` (starts with `xoxb-`)

### Create your expenses channel
- In Slack, create a channel called `#expenses` (or any name)
- Invite the Fintracker bot: `/invite @Fintracker`

---

## Step 2 — Local setup

```bash
# Clone or create your project folder
mkdir fintracker && cd fintracker

# Copy all files here:
# bot.py, parser.py, schema_v3_final.sql, requirements.txt, .env.example

# Create virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env and fill in your tokens
```

### Initialize the database

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('fintracker.db')
with open('schema_v3_final.sql') as f:
    conn.executescript(f.read())
conn.close()
print('Database ready.')
"
```

### Run tests

```bash
python3 test_parser.py   # 145 tests
python3 test_bot.py      # 64 tests
```

### Run locally

```bash
python3 bot.py
```

You should see: `Starting Fintracker Slack bot...`
Go to Slack and paste some expenses to test.

---

## Step 3 — Deploy to Railway

Railway gives you a free persistent server. Your bot runs 24/7.

1. Push your code to a GitHub repo (private is fine):
   ```bash
   git init
   git add bot.py parser.py schema_v3_final.sql requirements.txt
   echo ".env" >> .gitignore
   echo "fintracker.db" >> .gitignore
   git add .gitignore
   git commit -m "Initial fintracker"
   git remote add origin https://github.com/yourusername/fintracker
   git push -u origin main
   ```

2. Go to https://railway.app → **New Project → Deploy from GitHub**
3. Select your repo
4. Go to **Variables** tab and add all your `.env` values
5. Go to **Settings → Start Command** → set to `python bot.py`
6. Railway auto-deploys on every `git push`

### Persistent database on Railway

Railway's filesystem resets on redeploy. For persistent SQLite:
- Go to **Add Service → Volume**
- Mount it at `/data`
- Set `DB_PATH=/data/fintracker.db` in your environment variables

---

## Step 4 — Set your exchange rates

Before entering any foreign currency transactions:

```
/rate usd 122.5
/rate idr 0.0074
/rate aud 80.0
/rate sgd 90.0
/rate thb 3.3
/rate myr 26.0
/rate inr 1.45
/rate gbp 155.0
```

Check them anytime with `/rates`.

---

## Daily usage

### Normal day (Dhaka, no trip)

Paste your day's entries in `#expenses`:

```
17 April
bike to office - 150 (uber/cash)
rickshaw - 60 + 40 + 30
fuchka - 120
ebl to bkash - 2000
guava - 100
cockroach gel - 140 (ebl)
```

Bot replies with a review. Say `save all` to confirm.

### Correcting a line

If line 3 was guessed wrong:
```
3 treat
```
Or correct the amount:
```
3 350
```
Then `save all`.

### Starting a trip

```
/trip start "Indonesia March 2026"
```

Now paste your entries as normal. All go to the trip automatically.

For home expenses during the trip (rent, subscriptions):
```
rent dhaka - 25000 #home @dhaka
claude subscription - 20 usd (ebl) #home @dhaka
```

### Ending a trip

```
/trip end
```

Back to home mode.

### Different city within a trip

Add `@cityname` as the second line of your paste:
```
18 March
@Jakarta
hotel check-in - 180 usd (ebl)
dinner - 320k idr
```

Or per-line for mixed days:
```
18 March
@Bali
checkout bali hotel - 200k idr (ebl)
airport food - 85k idr
hotel jakarta - 180 usd (ebl) @jakarta
dinner jakarta - 320k idr @jakarta
```

### Bank confirmed amount (after checking statement)

```
/actual 42 1850
```

Sets the bank-confirmed amount for transaction #42.
Original estimate is preserved. The difference is tracked.

---

## Slash command reference

| Command | Example | What it does |
|---------|---------|--------------|
| `/trip start` | `/trip start "Indonesia March 2026"` | Open a trip |
| `/trip end` | `/trip end` | Close active trip |
| `/trip status` | `/trip status` | Show active trip |
| `/trip list` | `/trip list` | All trips with totals |
| `/rate` | `/rate usd 122.5` | Set exchange rate |
| `/rates` | `/rates` | View all current rates |
| `/actual` | `/actual 42 1850` | Set bank-confirmed amount |
| `/summary` | `/summary` | Current month projection |
| `/summary april` | `/summary april` | April breakdown by purpose |
| `/summary home` | `/summary home` | Current month, home only |

---

## File structure

```
fintracker/
├── bot.py              # Slack bot — message handling, commands
├── parser.py           # Entry parser — all parsing logic
├── schema_v3_final.sql # Database schema
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .env                # Your secrets (NOT in git)
├── fintracker.db       # SQLite database (NOT in git)
├── test_parser.py      # 145 parser tests
└── test_bot.py         # 64 bot integration tests
```

---

## What's next (Phase 2)

- **AI classifier** — Claude API for ambiguous entries (dinner = food_bill or treat?)
- **Google Sheets importer** — migrate your 3 years of existing data
- **Dashboard** — web UI for charts, monthly trends, trip analysis
- **bKash reconciliation** — auto cross-check against MFS statements

---

## GitHub + LinkedIn strategy

Every time you finish a phase, write one post:

**Phase 1 complete post idea:**
> "I've been tracking every expense manually for 3 years — Google Sheets, daily notes, 2-3 hours weekly. I just built a Slack bot that cuts that to 2 minutes. Here's what I learned designing the data model..."

Tag it: `#buildinpublic #python #personalfinance #sideproject`

That's the kind of post that gets traction from both engineers and PMs.
