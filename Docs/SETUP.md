# FINTRACKER — Setup Guide

A personal finance tracker with a Slack bot interface.
Paste your daily expenses in Slack, get an instant review, save with one word.

---

## What you need

- Python 3.11+
- A Slack workspace (your personal one works fine)
- A Railway.app account (free tier) for hosting
- An Anthropic API key (for AI classification — Phase 2)

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
Go to **Slash Commands → Create New Command** for each.
Socket Mode is enabled — leave Request URL blank for all of them.

| Command    | Description                        | Usage Hint                                   |
|------------|------------------------------------|----------------------------------------------|
| `/trip`    | Manage trips                       | `start "Name" \| end \| status \| list`      |
| `/rate`    | Set exchange rate                  | `usd 122.5`                                  |
| `/rates`   | View all current rates             | *(no arguments)*                             |
| `/actual`  | Set bank-confirmed amount          | `<id> <amount>`                              |
| `/summary` | Spending summary                   | `april \| home \| travel \| 5-12 april`      |
| `/entries` | Detailed transaction list by date  | `29 april \| 5-12 april \| 5-12 april home`  |

### Enable Event Subscriptions
- **Event Subscriptions** → Enable Events
- Subscribe to bot events: `message.channels`, `message.im`

### Install the app
- **OAuth & Permissions → Install to Workspace**
- Copy the **Bot User OAuth Token** → save as `SLACK_BOT_TOKEN` (starts with `xoxb-`)

### Create your expenses channel
- In Slack, create a channel called `#expenses` (or any name you prefer)
- Invite the Fintracker bot: `/invite @Fintracker`

---

## Step 2 — Local setup

### Project files
Place all these files in one folder:

```
bot.py
parser.py
schema_v3_final.sql
requirements.txt
init_db.py
test_parser.py
test_bot.py
.env.example
.gitignore
SETUP.md
```

### Virtual environment (Windows)

```
python -m venv venv
venv\Scripts\activate
```

You should see `(venv)` at the start of your terminal line.

### Install dependencies

```
pip install -r requirements.txt
```

### Set up environment variables

Copy `.env.example` to `.env` and fill in your tokens.
When saving in Notepad, set Save as type to `All Files (*.*)` — otherwise Windows saves it as `.env.txt`.

```
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_APP_TOKEN=xapp-your-app-token-here
EXPENSE_CHANNEL=expenses
DB_PATH=fintracker.db
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

### Initialize the database

```
python init_db.py
```

You should see: `Database rules and taxonomy refreshed.`

> **Note:** Run `init_db.py` every time you update `schema_v3_final.sql`.
> It safely refreshes rules, purposes, and payment methods while preserving all transaction data.

### Run tests

```
python test_parser.py   # 145 tests
python test_bot.py      # 64 tests
```

All tests should pass before running the bot.

### Run locally

```
python bot.py
```

You should see:
```
INFO: Starting Fintracker Slack bot...
INFO: Bolt app is running!
```

Go to your `#expenses` channel in Slack and paste some entries to test.

---

## Step 3 — Set your exchange rates

Before entering any foreign currency transactions:

```
/rate usd 122.5
/rate idr 1/140.6
/rate aud 80.0
/rate sgd 90.0
/rate thb 1/3.3
/rate myr 26.0
/rate inr 1.45
/rate gbp 155.0
```

**Two formats supported:**
- `/rate usd 122.5` — when 1 foreign unit is worth MORE than 1 BDT (USD, EUR, GBP, SGD...)
- `/rate idr 1/140.6` — when 1 BDT is worth MORE than 1 foreign unit (IDR, THB...)

Check all current rates anytime: `/rates`

---

## Step 4 — Deploy to Railway

Railway runs your bot 24/7 for free. Your laptop can be off.

### Push to GitHub first

```
git init
git add .
git commit -m "Initial fintracker"
git remote add origin https://github.com/yourusername/fintracker
git push -u origin main
```

### Deploy on Railway

1. Go to https://railway.app → **New Project → Deploy from GitHub**
2. Select your repo
3. Go to **Variables** tab — add all values from your `.env` file
4. Go to **Settings → Start Command** → set to `python bot.py`
5. Railway auto-deploys on every `git push`

### Persistent database on Railway

Railway's filesystem resets on redeploy. For persistent SQLite:
- **Add Service → Volume**
- Mount it at `/data`
- Set `DB_PATH=/data/fintracker.db` in your Variables tab

---

## Daily usage

### Paste format

```
[date]         ← optional, defaults to today
@[city]        ← optional, defaults to Dhaka
[entries...]   ← one per line
```

### Normal day (Dhaka, no trip)

```
17 April
bike to office - 150 (uber/cash)
rickshaw - 60 + 40 + 30
Dinner (falguni) - 320
ebl to bkash - 2000
metro card recharge - 200
metro to office - metro card
guava - 100
dbbl statement charge - 50
Spotify - 2.5 usd (ebl)
```

Bot replies with a review summary. Check it, then say `save all`.

### Review flow commands

| What to do | Type |
|------------|------|
| Save everything | `save all` |
| Force save with unresolved lines | `save anyway` |
| Show summary again | `review` |
| Discard everything | `cancel` |
| Correct purpose on line 3 | `3 treat` or `3 food_bill` |
| Correct payment on line 5 | `5 ebl_card` or `5 bkash` |
| Correct amount on line 7 | `7 2000` |
| Correct transaction type | `4 transfer` or `4 expense` |

After saving, the bot shows the transaction ID range:
```
✅ Saved 9 transactions.
Total expenses: 3,420 BDT
Transaction IDs: #48–#56 — use /actual to update bank amounts
```

### Food and treat convention

- Any food entry **without** the word `treat` → `food_bill`
- The word `treat` **anywhere** in the line → `treat`

```
Dinner (restaurant name) - 320 (ebl)         → food_bill
treat dinner (restaurant name) - 800 (ebl)   → treat
Dinner (treat) - 2105 (ebl)                  → treat
```

### Transfers and cashouts

```
ebl to bkash - 2000                           → transfer
dbbl to scb - 30000 (bftn, received 5 apr)   → transfer (note preserved)
ebl cashout - 6500                            → transfer, ebl → cash
```

### Metro card

```
metro card recharge - 200     → commuting, 200 BDT (this records the expense)
metro to office - metro card  → commuting, 0 BDT (prepaid usage, no review needed)
```

### Cashback

```
Breakfast - 452 (foodi/bkash) - 45 tk cashback   → stored as 407 BDT
```

### Third-party paid

```
fuchka - 100 (friend_xyz paid)   → my expense = 0, bill amount stored in details
```

### Missing amounts

If you omit the amount on an expense, it flags for review:
```
dbbl statement charge -    → ? BDT, needs review
```
Correct it during review: `1 150` (or whatever the charge was).

---

## Trip usage

### Start and end a trip

```
/trip start "Indonesia March 2026"
```
All entries from this point go to the trip automatically.

```
/trip end
```
Back to home mode.

### Home expenses during a trip

Add `#home` to any line to keep it out of the trip:
```
rent dhaka - 25000 #home @dhaka
claude subscription - 20 usd (ebl) #home @dhaka
```

### City tagging

City header applies to all lines in that day's paste:
```
18 March
@Jakarta
hotel check-in - 180 usd (ebl)
dinner - 320k idr
```

Per-line override for mixed days (e.g. flew from Bali to Jakarta):
```
18 March
@Bali
checkout hotel - 200k idr (ebl)
airport food - 85k idr
hotel jakarta - 180 usd (ebl) @jakarta
dinner jakarta - 320k idr @jakarta
```

---

## Slash command reference

### Trip

| Command | Example | What it does |
|---------|---------|--------------|
| `/trip start` | `/trip start "Indonesia March 2026"` | Open a trip |
| `/trip end` | `/trip end` | Close active trip |
| `/trip status` | `/trip status` | Show active trip |
| `/trip list` | `/trip list` | All trips with totals |

### Exchange rates

| Command | Example | What it does |
|---------|---------|--------------|
| `/rate` | `/rate usd 122.5` | Set rate (standard) |
| `/rate` | `/rate idr 1/140.6` | Set rate (1/X format) |
| `/rates` | `/rates` | View all current rates |

### Summary

| Command | Example | What it does |
|---------|---------|--------------|
| `/summary` | `/summary` | Current month projection + daily avg |
| `/summary home` | `/summary home` | Current month, home only |
| `/summary travel` | `/summary travel` | Current month, travel only |
| `/summary april` | `/summary april` | April breakdown by purpose |
| `/summary april home` | `/summary april home` | April, home only |
| `/summary 29 april` | `/summary 29 april` | Single day summary |
| `/summary 5-12 april` | `/summary 5-12 april` | Date range summary |
| `/summary 5-12 april home` | `/summary 5-12 april home` | Date range, home only |

### Entry list

| Command | Example | What it does |
|---------|---------|--------------|
| `/entries` | `/entries 29 april` | All transactions for a day |
| `/entries` | `/entries 5-12 april` | Transactions for a date range |
| `/entries` | `/entries 5-12 april home` | Date range, home only |
| `/entries` | `/entries 5-12 april travel` | Date range, travel only |

### Bank reconciliation

| Command | Example | What it does |
|---------|---------|--------------|
| `/actual` | `/actual 42 1850` | Set exact BDT for one transaction |
| `/actual` | `/actual 52 usd 110` | Recalculate one transaction with rate |
| `/actual` | `/actual 48-56 usd 121` | Recalculate ID range with rate |
| `/actual` | `/actual 2026-04-01 2026-04-30 usd 121` | Recalculate by date range |

> For ID range and date range modes: bot shows a preview, type `confirm` to save or `cancel` to abort.
> `/actual` never changes the global exchange rate — use `/rate` for that.

---

## File structure

```
fintracker/
├── bot.py                # Slack bot — commands, sessions, database writes
├── parser.py             # Entry parser — text to data, rules engine, display
├── schema_v3_final.sql   # Database schema, classifier rules, taxonomy
├── init_db.py            # Database initializer — run after schema changes
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
├── .env                  # Your secrets (NOT in git)
├── .gitignore            # Excludes venv/, .env, fintracker.db
├── fintracker.db         # SQLite database (NOT in git)
├── test_parser.py        # 145 parser tests
├── test_bot.py           # 64 bot integration tests
└── SETUP.md              # This file
```

**Two files, two jobs:**
- `parser.py` — understands text. Parsing, classification, rules engine, review summary.
- `bot.py` — talks to Slack and the database. Commands, sessions, saving, corrections.

---

## What's next (Phase 2)

- **AI classifier** — Claude API for ambiguous entries. Training data already being collected from your corrections.
- **Google Sheets importer** — migrate 3 years of existing data
- **Dashboard** — web UI with charts, monthly trends, trip analysis
- **bKash reconciliation** — auto cross-check against MFS statements
- **Money-to-collect tracker** — track shared expenses and who owes what
