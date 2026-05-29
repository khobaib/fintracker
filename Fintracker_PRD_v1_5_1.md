# FINTRACKER

**Personal Finance Tracker — Slack Bot & NLP Parser**

**Product Requirements Document  v1.5.1**

| Field | Value |
|-------|-------|
| Author | Khobaib Chowdhury |
| Date | May 2026 |
| Status | Phase 1 Complete — Data Ownership Added |
| Previous version | v1.4.1 — May 2026 |
| Phase | Phase 1 + Google Sheets Data Ownership |

---

## What's New in v1.5.1

Version 1.5.1 adds two major capabilities on top of the complete Phase 1 foundation:

| Area | Changes |
|------|---------|
| Google Sheets sync | Every `save all` writes to both SQLite and Google Sheets automatically. Data ownership independent of Railway. |
| `/export` command | `/export`, `/export all`, `/export 2026-04` — on-demand export from Slack to Sheets. |
| Loan detection | `wasim loan` → transfer. `ebl loan pay` → expense. Bare `loan` keyword = transfer; `loan` + `pay` = expense. |
| Transfer detection | `received from X (transfer)` → transfer in. `X transfer` → transfer. Explicit `transfer` keyword always detected. |
| `paid by X` pattern | Both `(X paid)` and `(paid by X)` now supported. Both mean my expense = 0. |
| Multi-date header | Review summary shows date range (e.g. `5–12 May`) when paste spans multiple days. |
| Classification fixes | Vegetables → grocery. Electricity/gas/bijli bill → accommodation. Seafood → food_bill. Pregnancy keywords → medical. Sports/badminton → health. Apple (fruit) vs Apple (brand) disambiguated. Mobile data → mobile_expense. |
| Railway stability | DB rules refresh on every deploy. FOREIGN KEY constraint fixed. Google Sheets threading bug fixed. DB path fixed to `/data/fintracker.db` on Railway volume. |

---

# 1. Overview

Fintracker is a personal finance tracking system that replaces a manual Google Sheets workflow with an automated Slack bot and NLP parser. The user pastes daily expense notes in Slack exactly as they already write them — no new app, no forms, no dropdowns — and the parser classifies, validates, and stores them in a structured SQLite database.

**Problem being solved:** 3 years of manual expense tracking across daily notes + weekly Google Sheets reconciliation, consuming 2–3 hours every weekend. The system automates classification, handles foreign currencies, trip segmentation, and provides monthly analytics and forecasting.

> *Design principle: "The best UI is no UI." Input format is free text, exactly as the user already writes it. The parser adapts to the human, not the other way around.*

**Accuracy baseline (5 months real data — March to July 2025)**

```
Total entries tested:    659 expense/investment rows (78 transfers excluded)
Correct by rules:        68%  — classified correctly without any user intervention
Historical exceptions:   25%  — expected gaps (old data pre-dating category additions)
Genuine ambiguity:        5%  — sent to AI classifier in Phase 2
```

**Historical exceptions explained:**
- treat entries: old data had no 'treat' keyword — importer uses purpose column directly
- others → grocery: grocery category only added November 2025
- others → health: haircut/salon added to health from January 2026
- recreation → digital_product: Spotify classified as recreation in legacy data

---

# 2. Input Format Convention

## 2.1  Paste Structure

- **Line 1 — Date header** (optional; defaults to today if omitted)
- **Line 2 — City header** (optional; defaults to Dhaka)
- **Remaining lines** — one transaction per line

```
17 April @Bali
Dinner (sate ratu) - 212000 idr (ebl)
snacks - 10000 idr
rent dhaka - 25000 #home @dhaka
ebl to bkash - 2000
```

## 2.2  Date Header

All supported formats parse to a canonical date. Year is inferred from context.

| Input / Pattern | Result |
|---|---|
| 17 April | 2026-04-17 |
| 17 Apr | 2026-04-17 |
| April 17 | 2026-04-17 |
| 17/04 | 2026-04-17 |
| 17-04 | 2026-04-17 |
| 2026-04-17 | 2026-04-17 |

> *Multi-date paste: Use multiple date headers in one paste to enter back-dated entries. Each header sets the transaction date for all lines that follow it. The review summary shows a date range (e.g. "5–12 May") when entries span multiple days. `created_at` records when the entry was actually typed (audit trail).*

## 2.3  City Header

- **Format:** `@cityname` — second line of the paste, after the date header
- If omitted — all transactions default to Dhaka (the home city)
- **Per-line override:** `@jakarta` on a transaction line overrides just that entry
- City slugs are case-insensitive and space-normalised: `Kuala Lumpur` → `kuala_lumpur`
- New cities are auto-created on first use — no confirmation needed

## 2.4  Transaction Line Format

```
[description] - [amount] [currency?] [service?] [(payment?)] [#tag?] [@city?]
```

## 2.5  Bracket Convention — Critical Rule

**First bracket = restaurant/store/person name context. Never a classification keyword.**

| Input | What the bracket means | Classification |
|---|---|---|
| `Dinner (sate ratu) - 300 (ebl)` | First bracket = restaurant name | food_bill, payment=ebl_card |
| `masala dosa (apon coffee) - 200` | 'apon coffee' is the restaurant | food_bill — NOT beverages |
| `Dinner (Abesh Hotel) - 305` | 'Abesh Hotel' is the restaurant | food_bill — NOT accommodation |
| `[G] Lunch (chayer mela) - 431` | Prefix `[G]` = paid for G | treat (because of `[G]` prefix) |
| `dinner - 691 (foodi/bkash)` | Last bracket = payment method | food_bill, platform=foodi, payment=bkash |

> **Key principle**
> Content inside the FIRST bracket = name/context (restaurant, store, person).
> Content inside the LAST bracket = payment method if it matches a known keyword.
> Never use the first bracket content as a classification signal.

---

# 3. Amount Parsing

**Default currency:** No currency code = BDT. All BDT amounts stored as integers (whole taka, rounded). No paisa, no decimals.

## 3.1  Arithmetic Expressions

| Input / Pattern | Result |
|---|---|
| `rickshaw - 60` | 60 BDT |
| `lunch - 350.7` | 351 BDT (rounded up) |
| `rickshaw - 60 + 40 + 30` | 130 BDT |
| `entry fee - 2*500` | 1000 BDT |
| `breakfast - 220 + 20 tips` | 240 BDT (tips absorbed into total) |
| `rent - 25,000` | 25000 BDT |
| `=1067+150` | 1217 BDT (Sheets formula) |

## 3.2  Foreign Currency

- **k suffix:** × 1,000 — `10k idr` → 10,000 IDR
- **m suffix:** × 1,000,000 — `1.5m idr` → 1,500,000 IDR
- Exchange rate: most recent rate on or before the transaction date
- **`estimated_amount_bdt`** = set at entry time, never overwritten
- **`actual_amount_bdt`** = bank-confirmed, added later via `/actual`
- Missing rate → bot warns immediately, entry flagged, session not committed
- Setting `/rate` mid-session recalculates ALL entries of that currency in the active session

| Format | Example | Use when |
|---|---|---|
| Standard | `/rate usd 122.5` | 1 foreign unit > 1 BDT (USD, EUR, GBP, SGD…) |
| Inverse 1/X | `/rate idr 1/140.6` | 1 BDT > 1 foreign unit (IDR, THB, VND…) |

> **`/actual` vs `/rate` — important distinction**
> `/rate usd 122.5`    → updates the GLOBAL exchange rate for future entries
> `/actual`            → updates `actual_amount_bdt` on specific past transactions only
>                        does NOT change the global rate

---

# 4. Payment Method Detection

**Core rule:** Last parenthesised token on the line = payment method if it matches a known keyword. No bracket → cash default.

## 4.1  Bracket Rule

| Input / Pattern | Result |
|---|---|
| `fuchka - 120` | cash (default — no bracket) |
| `dinner - 2105 (ebl)` | ebl_card |
| `rent - 14000 (scb)` | scb_card |
| `recharge - 399 (bkash)` | bkash |
| `dinner - 691 (foodi/bkash)` | payment: bkash, platform: foodi |
| `bike - 150 uber` | cash (uber = service context, not payment) |
| `bike - 150 (uber/cash)` | payment: cash, service: uber |
| `bike - 143 (uber/manual bkash)` | payment: bkash, service: uber |
| `metro to uttara - metro card` | metro_card |

## 4.2  Mixed Payment

One expense, two payment sources → primary (larger) method as payment field, full breakdown in details.

> *`AC setup - 6500 (6000 cash + 509 bkash)` → payment: cash, details: '6000 cash + 509 bkash'*

---

# 5. Transaction Classification

Three layers in order. Higher layers take priority.

- **Explicit #tag override** — user puts `#purposeslug` in the line → confidence 1.0, no review
- **[Name] prefix rule** — `[G]` or `[Name]` before food/beverage → purpose = treat
- **Rules engine** — deterministic keyword/regex patterns, priority-ordered
- **AI classifier** — Claude API for remaining ambiguous entries (Phase 2)

## 5.0  Metro Card Special Rule

- `metro card recharge - 200` → commuting, 200 BDT ✅
- `metro to office - metro card` → commuting, 0 BDT, no review needed ✅
- Individual metro rides draw from prepaid balance — no amount needed

## 5.1  Transfer Detection

Transfers are detected before the rules engine. The following patterns are handled:

| Input / Pattern | Result |
|---|---|
| `ebl to bkash - 2000` | transfer, from: ebl, to: bkash |
| `dbbl to ebl - 30000` | transfer, from: dbbl, to: ebl |
| `ebl cashout - 6500` | transfer, from: ebl, to: cash (ATM) |
| `bkash cashout - 2000` | transfer, from: bkash, to: cash |
| `wasim loan - 500` | transfer (giving/receiving a loan = money out) |
| `friend_xyz loan - 500` | transfer (bare loan keyword = transfer) |
| `ebl loan pay - 2000` | expense, purpose: loan (`loan` + `pay` = expense) |
| `friend_xyz loan pay - 182000` | expense, purpose: loan |
| `chaldal loan payment - 100000` | expense, purpose: loan |
| `received from Raya (transfer) - 6000` | transfer, from: person, to: cash |
| `Raya transfer - 6000 (dbbl)` | transfer (explicit 'transfer' keyword) |

> *Loan rule: bare `loan` keyword (without `pay`/`payment`/`repay`) = transfer. Any form of `pay` anywhere in the line with `loan` = expense, purpose: loan.*

## 5.2  Food Classification — The Treat Rule

> - `treat` keyword anywhere in line → treat (confidence 1.0, no review)
> - `[Name]` prefix + food/beverage → treat (paid for someone else)
> - Any food word without `treat` → food_bill (confidence 1.0, no review)

| Input / Pattern | Result |
|---|---|
| `Dinner - 300 (ebl)` | food_bill |
| `dinner (sate ratu) - 300` | food_bill |
| `treat dinner (nandos) - 800` | treat |
| `[friend_xyz] lunch - 350 (bkash)` | treat |
| `[G] chicken khichuri - 260` | treat |
| `fuchka - 100 (friend_xyz paid)` | food_bill, amount=0 |
| `matcha icecream - paid by Raya` | food_bill, amount=0 (paid by X pattern) |
| `breakfast (rutiwala) - paid by abbu` | food_bill, amount=0 (paid by X pattern) |

## 5.3  Third-Party Paid Patterns

Both patterns mean the same thing: X paid, so my expense = 0.

| Input / Pattern | Result |
|---|---|
| `fuchka - 100 (friend_xyz paid)` | amount=0, bill=100 in details, paid_by=Friend_xyz |
| `matcha icecream - paid by Raya` | amount=0, paid_by=Raya |
| `breakfast - paid by abbu` | amount=0, paid_by=Abbu |
| `[friend_xyz] bike - 150 (uber)` | amount=150, paid_for=friend_xyz, purpose=commuting |

## 5.4  Category Rules — Full Reference

### Food & Beverages

- **Meal context (always food_bill):** breakfast, sehri, lunch, brunch, dinner, supper, iftar, khichuri, biryani, tehari
- **Bangladeshi dishes:** bhuna, vorta, shutki, ilish, hilsa, rezala, jhalmuri, singara, samosa, pitha, mishti, payesh, halwa, dal bhaat, roti, luchi, kulfi, muri
- **International dishes:** steak, pasta, sushi, burger, pizza, taco, wrap, curry, grilled, fried rice, dim sum, bbq, shawarma, noodle, sandwich, kebab
- **Seafood (added v1.5.1):** squid, prawn, shrimp, crab, lobster, fish fry, fish curry, hilsa fry
- **Desserts:** cake, pastry, cookie, biscuit, brownie, waffle, donut, ice cream, mousse
- **Fruits (expanded v1.5.1):** apple, pineapple, mango, banana, watermelon, guava, papaya, orange, grape, strawberry, lemon, lime, coconut, litchi, berry, melon, kiwi, dates
- **Beverages:** tea, cha, chai (word-boundary matched), juice (high priority), hot/cold chocolate, coconut water, daab, boba, bubble tea, smoothie, milkshake

### Grocery

- **Explicit keyword:** any line containing the word `grocery` → grocery (priority 1.0)
- **Vegetables (added v1.5.1):** cucumber, cauliflower, broccoli, tomato, potato, onion, garlic, ginger, carrot, spinach, cabbage, eggplant/brinjal/begun, capsicum, pumpkin, lau, korola, dherosh, okra, shim, dal, moong, masoor, chana, mushroom, beans, peas, corn
- **Household consumables:** tissue, powder milk, dairy, raw ingredients, baby wipes, diaper, nappy, toilet paper, toiletries, honey, raw chicken, frozen, yogurt, sanitary
- **Supermarkets:** Shajgoj, Chaldal, Meena Bazar, Agora, Unimart, Shwapno, Apon Bazar

### Medical

- **Services:** doctor, clinic, hospital, diagnostic, lab test, ultrasound, xray, blood test, surgery
- **Pregnancy (added v1.5.1):** pregnancy kit, pregnancy test, hcg, beta-hcg, pregnancy blood test
- **Medicines:** pharmacy, medicine, saline, ors, paracetamol, napa, antacid, antibiotic, syrup, tablet, capsule, eye drop, ear drop
- **Dental:** teeth scaling, dental, dentist, orthodontist, tooth, braces

### Health

- **Gym & fitness:** gym, fitness, yoga, pilates, crossfit
- **Sports (added v1.5.1):** badminton, tennis, cricket, football, soccer, basketball, volleyball, swimming, cycling, squash, table tennis, golf, archery, boxing, martial arts, karate, taekwondo, futsal, handball, rugby, hockey, skating, climbing, trekking, hiking, outdoor sports
- **Skincare:** cerave, neutrogena, garnier, loreal, nivea, vaseline, moisturizer, sunscreen, face wash, face cream, body lotion, lip balm, serum
- **Grooming (from Jan 2026):** haircut, beard trim, salon, parlour, barbershop
- **Other:** minoxidil, derma roller, hair serum, condom, contraceptive

### Shopping

- **Electronics & accessories:** cable, charger, adapter, earphone, headphone, keyboard, mouse, phone, watch, speaker, camera, usb, hdmi
- **Clothing:** shirt, pant, trouser, jacket, hoodie, tshirt, dress, sari, saree, lungi, kameez, punjabi, panjabi
- **Bags & footwear:** bag, wallet, shoe, sandal
- **Fragrance:** perfume, cologne, deodorant
- **Household goods:** moshari, mosquito net, bedsheet, pillow, curtain, wipes, tissue (in shopping context), rope

### Accommodation

- **Rent & utilities:** rent, service charge, utility bill, internet bill, garbage bill, ac rent
- **Utility bills (added v1.5.1):** electricity bill, electric bill, bijli bill, gas bill, wasa bill, power bill, water bill, ac bill
- **Staff:** bua salary, maid salary, house salary, guard tips
- **Travel accommodation:** hotel, hostel, airbnb, guesthouse, resort, villa

> *`Dinner (Hotel Name)` = food_bill, not accommodation (first bracket = restaurant name rule).*

### Commuting

- **Ride-hailing:** uber, pathao, gojek, grab, indrive, shohoz, obhai, rapido
- **Traditional:** rickshaw (+ typo variants), cng, metro, bus, bike, van
- **Travel:** flight, expressway toll, road toll

### Recreation

- **Entertainment:** park ticket, entry ticket, museum, zoo, aquarium, theme park, theatre, shilpakala, concert, cinema, movie
- **Activities:** horse car, horse ride, boat ride, paddle boat, amusement, fair, mela, carnival, pot, kite

### Gift

- **Keywords:** gift, eid salami, salami, boishakh gift, birthday gift, wedding gift
- **Family/named:** gablu (family member name)
- **Tips:** standalone tips, biye bari tips

### Loan

- **Keywords:** loan, loan pay, loan payment, loan repay, loan instalment, bank loan, loan return

> *`X loan - 500` (giving a loan) = transfer. `X loan pay` = expense, purpose: loan.*

### Others

- **Bank/MFS fees:** statement charge, statement fee, solvency certificate, card annual fee, maintenance fee, bank charge
- **Courier:** courier, parcel, pathao parcel
- **Print:** print, photocopy, lamination
- **Service charge:** standalone → others; `service charge and utility` → accommodation

### Mobile Expense

- **Keywords:** recharge, internet pack, data pack, sim, robi, grameenphone, gp, banglalink, teletalk, sms charge, mobile charge, mobile data charge
- **Added v1.5.1:** mobile data

> *`metro card recharge` → commuting (higher priority than mobile)*

### Digital Products

- **Subscriptions:** spotify, netflix, youtube premium, google one, claude, chatgpt, openai, slack, notion, dropbox, adobe, microsoft 365
- **Apple products (disambiguated v1.5.1):** apple music, apple tv, apple one, apple arcade, apple watch, apple keyboard, macbook, iphone, ipad, icloud — bare `apple` or `apple pie` → food_bill (fruit)

### Bank/MFS Service Charges

| Input | Classification | Note |
|---|---|---|
| `dbbl statement charge - 50` | others | Bank fee |
| `ebl solvency certificate - 200` | others | Document fee |
| `sms charge - 5` | mobile_expense | Not others |
| `mobile data charge - 29` | mobile_expense | Not others |
| `service charge and utility - 725` | accommodation | Utility context wins |
| `service charge - 500` | others | Standalone = bank fee |

### Food Delivery Subscriptions

`foodi pro subscription`, `foodpanda pro subscription` → food_bill (not digital_product)

> **Restaurant name convention**
> Any word inside the FIRST bracket is a restaurant/store name — never a classification keyword.
> `masala dosa (apon coffee)` → food_bill, not beverages.
> `Dinner (Abesh Hotel)` → food_bill, not accommodation.
> If your description has no food context word, add one: `Food (Roll Chai)` not just `Roll Chai`.

### Cashback

```
Breakfast - 452 (foodi/bkash) - 45 tk cashback   → stored as 407 BDT (452 - 45)
```

### Split Expenses

```
Electric work - 850 (total 1700, split with friend_xyz)   → my expense: 850 BDT
```

### Tips

- `breakfast - 220 + 20 tips` → food_bill, 240 BDT (tips absorbed into total)
- `guard tips (friend_xyz) - 500` → accommodation (apartment context)
- `Tips - 20` → gift (standalone tip)

### Mixed Items with +

- If ALL items are food → food_bill
- If AT LEAST ONE item is non-food (perfume, bag, notebook) → shopping

```
chocolate + perfume - 27000      → shopping (perfume is non-food)
fuchka + badam - 150             → food_bill (all food)
```

---

# 6. Data Architecture

## 6.1  Dual Storage — SQLite + Google Sheets

Every `save all` writes to both storage layers simultaneously:

```
Slack input → Parser → SQLite (Railway) ↔ Google Sheets (permanent record)

Every save all writes to both:
  • fintracker.db on Railway (/data/fintracker.db, persistent volume)
  • A row in Google Sheets via Google Sheets API (automatic sync)
```

**Why dual storage:** Railway's SQLite volume is reliable but not directly exportable. Google Sheets gives permanent, platform-independent ownership of all expense data, accessible from any device without needing Railway access.

## 6.2  Google Sheets Integration

- Service account authentication — no human login required, fully automated
- Tab name: `Transactions` — auto-created on first sync if not present
- Sync trigger: every `save all` → new transactions appended automatically
- Threading model: fresh SQLite connection per sync thread (fixed in v1.5.1)

| Environment variable | Value | Notes |
|---|---|---|
| `GOOGLE_SHEETS_CREDENTIALS` | Service account JSON | Full content of downloaded key file |
| `GOOGLE_SHEET_ID` | Spreadsheet ID | From the Google Sheets URL |

## 6.3  Railway Deployment

- Platform: Railway.app — always-on, auto-deploys on GitHub push
- DB path: `/data/fintracker.db` — persistent volume, survives all redeploys
- Rules refresh: every startup clears and reloads `classifier_rules`, `purpose_taxonomy`, `payment_method`, `accounts`, `currencies`, `cities` from `schema_v3_final.sql`
- Transaction data (`transactions`, `trips`, `slack_sessions`) is never touched by rules refresh
- FOREIGN KEY constraint: disabled during rules refresh, re-enabled after (fixed v1.5.1)

---

# 7. Trip Management

## 7.1  Trip Session

| Command | Example | What it does |
|---|---|---|
| `/trip start` | `/trip start "Indonesia March 2026"` | Opens trip — all entries auto-tagged |
| `/trip end` | `/trip end` | Closes trip — back to home mode |
| `/trip status` | `/trip status` | Shows currently active trip |
| `/trip list` | `/trip list` | All trips with totals |

- One active trip at a time. Back-to-back trips supported — end one, start the next.
- **Trip ≠ city.** A trip spans multiple cities. City is tracked per transaction via `@tag`.

## 7.2  Home Override During Trip

```
rent dhaka - 25000 #home @dhaka
claude subscription - 20 usd (ebl) #home @dhaka
```

- `#home` tag → `trip_id = NULL`, `is_home_during_trip = 1` for that entry only

## 7.3  Analytics Views

- **Monthly — all:** home + travel
- **Monthly — home only:** `trip_id IS NULL` — your real baseline cost of living
- **Monthly — travel only:** `trip_id IS NOT NULL`
- **Trip view — full:** all expenses for a trip, independent of calendar months
- **Trip × month:** how much a trip cost in a specific calendar month
- **Trip × week:** spending by week within a trip
- **Home vs travel per month:** e.g. March: 18,000 home / 32,000 travel

---

# 8. Category Versioning & Backward Compatibility

**Problem:** Categories evolve over time. Multiple changes are fully supported — each change is one log entry, full history queryable.

## 8.1  Known Category Changes

| Category | Status | Change date | Notes |
|---|---|---|---|
| others → grocery | Active from Nov 2025 | 2025-11-01 | Tissue, powder milk, eggs etc. |
| others → health | Active from Jan 2026 | 2026-01-01 | Haircut, salon, beard trim |
| tour_bill | Legacy import only | — | New entries use real purpose + trip_id |

## 8.2  Historical Data Import (Phase 2)

> **New vs historical entries**
> - New entries (Slack bot): purpose from parser rules + `treat` keyword
> - Historical entries (Sheets import): purpose from Sheets 'Transaction Purpose' column directly
> - `source` field distinguishes them: `slack_bot` vs `sheets_import`
> - treat entries in old data: imported as treat even without the keyword in description text.
> - others entries before Nov 2025: imported as others, user can migrate to grocery later.

---

# 9. Slash Commands

| Command | Description |
|---|---|
| `/summary` | Current month projection + daily avg (all + home-only) |
| `/summary april` | April breakdown by purpose |
| `/summary 5-12 april` | Date range summary |
| `/summary april home` | April, home only |
| `/entries 29 april` | All transactions for that day with IDs and amounts |
| `/entries 5-12 april` | Date range transaction list |
| `/trip start [name]` | Start a new trip — all entries get `trip_id` |
| `/trip end` | End active trip |
| `/rate usd 122.5` | Set exchange rate for future entries |
| `/rate idr 1/140.6` | Set inverse exchange rate (1 BDT > 1 foreign unit) |
| `/rates` | Show all current exchange rates |
| `/actual 52 usd 110` | Update `actual_amount_bdt` on specific transaction |
| `/actual 84-96 usd 123.7` | ID range — preview before save |
| `/export` | Export all transactions to Google Sheets (append mode) |
| `/export all` | Full refresh — clears sheet and re-exports everything |
| `/export 2026-04` | Export a specific month only |

---

# 10. Review & Correction Flow

## 10.1  Correction Commands

| What to correct | Command | Effect |
|---|---|---|
| Purpose | `3 treat` | Line 3 purpose → treat |
| Purpose | `5 food_bill` | Line 5 purpose → food_bill |
| Purpose | `8 accommodation` | Line 8 purpose → accommodation |
| Payment method | `4 ebl_card` | Line 4 payment → EBL card |
| Payment method | `7 bkash` | Line 7 payment → bKash |
| Amount (BDT) | `7 2000` | Line 7 amount → 2000 BDT |
| Transfer amount | `7 6500` | Line 7 (cashout with no amount) → 6500 BDT |
| Transaction type | `4 transfer` | Line 4 type → transfer |

## 10.2  Session Commands

| Command | What it does |
|---|---|
| `save all` | Save everything. Blocks if unresolved lines remain. |
| `save anyway` | Force save including unclassified entries. |
| `review` | Reprint the full review summary. |
| `cancel` | Abandon session — nothing saved to database. |
| `confirm` | Confirm a bulk `/actual` update (after preview). |

## 10.3  Full Correction Example

```
Bot reply:
  Found 5 entries (1 transfer) — 2 need your review.
  ⚠️  Lines needing review: *3* — needs ai classification | *5* — no amount
  ✅ 1. bike to office — commuting — 170 BDT — cash
  ✅ 2. fuchka — food bill — 120 BDT — cash
  ❓ 3. dinner — ? (needs classification) — 2400 BDT — ebl card
  🔄 4. ebl to bkash - 2000 — transfer — 2,000 BDT
  🔄 5. ebl cashout - — transfer  [no amount]

You type:   3 treat
Bot:        Updated line 3: purpose: None → treat

You type:   5 6500
Bot:        Updated line 5: amount: None → 6500

You type:   save all
Bot:        ✅ Saved 5 transactions.
            Total expenses: 2,690 BDT
            Transaction IDs: #14–#18 — use /actual to update bank amounts
```

---

# 11. Use Cases

## UC-01  Daily expense entry

- User pastes day's expenses in `#expenses` Slack channel
- Bot parses all lines and returns a review summary
- Header shows which lines need attention: `⚠️ Lines needing review: *3* — no amount | *7* — needs ai classification`
- User types `save all` to confirm or corrects individual lines
- Bot saves and confirms with total + transaction ID range
- Every `save all` automatically syncs the new transactions to Google Sheets

## UC-02  Correcting a misclassified line

- Type `3 treat` — correct line 3 purpose to treat
- Type `5 ebl_card` — correct payment method on line 5
- Type `7 2000` — correct or fill in amount on line 7
- Type `4 transfer` — correct transaction type
- Correction clears the review flag and `review_reason` immediately
- All corrections saved as `classifier_examples` with weight 2.0 for AI training

## UC-03  Foreign currency expense

- User enters expense with foreign currency amount
- If rate not set, bot warns and asks user to set it
- User runs `/rate usd 122.5` or `/rate idr 1/140.6`
- Bot recalculates ALL currency entries in active session, shows updated summary
- User runs `/actual 52 usd 110` after checking bank statement

## UC-04  Travel trip

- User runs `/trip start "Indonesia March 2026"`
- All entries auto-tagged to trip. City set per-paste with `@Bali` or per-line with `@jakarta`
- Home expenses: `rent dhaka - 25000 #home @dhaka`
- User runs `/trip end` when back home
- Trip analytics: full trip / per month / per week / per city

## UC-04b  Detailed entry list

- User runs `/entries 29 april` — all transactions for a single day
- User runs `/entries 5-12 april` — all entries for a date range
- User runs `/entries 5-12 april home` — home expenses only
- Output: transaction ID, date, purpose, amount, payment, city, trip flag, total at bottom

## UC-05  Monthly review and projections

- User runs `/summary` — current month projection
- Shows: days elapsed, total spent, daily average, projected month total (all + home-only)
- User runs `/summary april` — April breakdown by purpose
- User runs `/summary april home` — April, home only
- User runs `/summary 29 april` — single day summary
- User runs `/summary 5-12 april` — date range summary
- User runs `/summary 5-12 april home` — date range, home only

## UC-06  Bank reconciliation

- User checks bank statement and finds actual BDT differs from estimate
- **Mode 1 — exact BDT:** `/actual 42 1850` — set exact amount
- **Mode 2 — single ID with rate:** `/actual 52 usd 110` — recalculate one transaction
- **Mode 3 — ID range:** `/actual 84-96 usd 123.7` — preview + confirm
- **Mode 4 — date range:** `/actual 2026-04-02 2026-04-04 idr 1/140.6`
- `estimated_amount_bdt` is NEVER overwritten. `actual_amount_bdt` stored separately.
- `/actual` never changes the global exchange rate — use `/rate` for that.

## UC-07  Exchange rates

- `/rate usd 122.5` — standard format (foreign unit > BDT)
- `/rate idr 1/140.6` — inverse format (BDT > foreign unit)
- `/rates` — view all current rates with effective dates
- Multiple rates per currency stored date-stamped. Old transactions keep their rate.
- Unknown currency → auto-created rate=0, user prompted immediately

## UC-08  Back-dated entries

- User pastes with a date header: `17 April`
- All entries get `transacted_at = 17 April` regardless of when typed
- `created_at` records actual entry time
- Multiple date headers in one paste supported
- Review summary shows date range (e.g. `5–12 May`) when multiple dates present

## UC-09  Google Sheets export

- User runs `/export` — appends all new transactions not yet in Sheets
- User runs `/export all` — clears sheet and re-exports everything from scratch
- User runs `/export 2026-04` — exports April 2026 only
- Export runs in background; bot responds immediately
- Auto-sync on `save all` continues to work independently

---

# 12. Acceptance Criteria

## AC-01  Date parsing

- All date formats in section 2.2 parse correctly
- Missing date header defaults to today
- Multi-date paste: each date header sets transaction date for lines that follow
- `created_at` and `transacted_at` stored independently
- Review summary shows date range (e.g. `5–12 May`) when paste spans multiple days

## AC-02  Amount parsing

- Arithmetic, multiplications, comma format, tip stripping, Sheets formula all work
- `60 + 40 + 30` → 130;  `2*500` → 1000;  `=1067+150` → 1217
- `10k idr` → 10000 IDR;  `1.5m idr` → 1500000 IDR
- `220 + 20 tk tips.` → 240 BDT
- Foreign currency + exchange rate → `estimated_amount_bdt` set correctly
- Missing amount on expense → `? BDT`, flagged for review (NOT defaulted to 0)
- Missing amount on metro card ride → 0 BDT, no review needed
- All stored amounts are integers, rounded

## AC-03  Payment method detection

- `fuchka - 120` → cash
- `dinner (foodi/bkash)` → payment: bkash, platform: foodi
- `bike - 150 (uber/manual bkash)` → payment: bkash, service: uber
- `bike to office - 150 uber` → cash (uber = service context)
- `metro to uttara - metro card` → metro_card

## AC-04  Transfer detection

| Input / Pattern | Result |
|---|---|
| `ebl to bkash - 2000` | transfer, from: ebl, to: bkash |
| `ebl cashout - 6500` | transfer, from: ebl, to: cash |
| `wasim loan - 500` | transfer (bare loan = transfer) |
| `any [Name] loan - 500` | transfer (bare loan keyword) |
| `ebl loan pay - 2000` | expense, purpose: loan |
| `friend_xyz loan pay - 182000` | expense, purpose: loan |
| `received from Raya (transfer) - 6000` | transfer, from: person, to: cash |
| `Raya transfer - 6000 (dbbl)` | transfer (explicit transfer keyword) |

- Transfer lines display: `description — transfer — amount BDT`
- Transfer without amount → flagged for review

## AC-05  Food and treat classification

- `Dinner - 300` → food_bill, no review
- `masala dosa (apon coffee) - 200` → food_bill (restaurant name in bracket ignored)
- `Dinner (Abesh Hotel) - 305` → food_bill (hotel = restaurant name)
- `treat dinner - 500` → treat
- `[G] lunch (restaurant) - 400` → treat (G prefix + food = treat)
- `[friend_xyz] bike payment - 100` → commuting (bike = commuting regardless of who)
- `chocolate + perfume - 27000` → shopping (one non-food item)
- `mango juice - 80` → beverages (juice always beverages, high priority)
- `Roll Chai - 370` → food_bill (Roll Chai is a restaurant name)
- `squid fry - 200` → food_bill (seafood added v1.5.1)
- `pineapple - 200` → food_bill (fruit)
- `apple - 50` → food_bill (bare apple = fruit, not Apple brand)
- `apple music - 300` → digital_product (Apple brand keyword)

## AC-06  Special entry patterns

- `fuchka - 100 (friend_xyz paid)` → amount=0, bill=100 in details
- `matcha icecream - paid by Raya` → amount=0, paid_by=Raya (paid by X pattern)
- `breakfast - paid by abbu` → amount=0, paid_by=Abbu
- `[friend_xyz] bike - 150` → amount=150, paid_for=friend_xyz
- `Breakfast - 452 (foodi/bkash) - 45 tk cashback` → 407 BDT
- `friend_xyz Loan Pay - 182000` → loan expense
- `Chaldal loan payment - 100000` → loan (Chaldal keyword does not override)
- `Pathao parcel - 238` → others (parcel = courier, not commuting)
- `tissue - 28` → grocery
- `honey - 100` → grocery
- `toiletries - 200` → grocery
- `vegetables grocery - 160` → grocery (word `grocery` anywhere in line = grocery)
- `cucumber - 30` → grocery (vegetable, added v1.5.1)
- `electricity bill - 1000` → accommodation (utility bill, added v1.5.1)
- `badminton payment - 240` → health (sports, added v1.5.1)
- `pregnancy kit - 160` → medical (added v1.5.1)
- `beta-hCG (pregnancy blood test) - 1120` → medical (added v1.5.1)
- `mobile data - 157 (bkash)` → mobile_expense (added v1.5.1)
- `cerave moisturizer - 850` → health
- `neutrogena face wash - 320` → health
- `condom - 120` → health
- `eye drop - 120` → medical
- `teeth scaling - 1500` → medical
- `Pot - 900` → recreation
- `baby_xyz toy - 640` → gift
- `Foodi pro subscription - 150` → food_bill
- `Spotify Recharge - 220` → digital_product

## AC-07  Trip management

- `/trip start` creates active trip; all entries get `trip_id` until `/trip end`
- `#home` tag → `trip_id: NULL`, `is_home_during_trip: 1`
- Monthly view correctly segments: all / home-only / travel-only
- Trip view is date-independent — spans calendar months

## AC-08  Foreign currency

- `estimated_amount_bdt` stored at entry, never overwritten
- `/actual 52 usd 110` → single transaction recalculate
- `/actual 84-96 usd 123.7` → ID range, preview before save
- `/actual 2026-04-02 2026-04-04 idr 1/140.6` → date range, 1/X format
- `/actual` never changes global exchange rate

## AC-09  Review flow

- Bot header explicitly lists which lines need review and why
- `save all` blocks if any expense has no classification or unresolved amount
- `save anyway` force-saves including unclassified entries
- `cancel` discards entire session
- All corrections clear `review_reason` immediately — no stale flags
- Corrections saved as `classifier_examples`: confirmed=weight 1.0, corrected=weight 2.0
- **Weight meaning:** corrections count twice in AI training — stronger learning signal

## AC-10  /summary date range

| Command | What it shows |
|---|---|
| `/summary` | Current month projection + daily avg (all + home-only) |
| `/summary home` | Current month, home only |
| `/summary april` | April breakdown by purpose |
| `/summary april home` | April, home only |
| `/summary 29 april` | Single day summary |
| `/summary 5-12 april` | Date range summary |
| `/summary 5-12 april home` | Date range, home only |
| `/summary 2026-04-29` | ISO single date |

## AC-11  /entries command

- `/entries 29 april` → all transactions for that day with IDs and amounts
- `/entries 5-12 april` → date range transaction list
- `/entries 5-12 april home` → home only
- Transfers shown with 🔄, travel entries with ✈️
- Total expenses at bottom (transfers excluded)

## AC-12  /export command

| Input / Pattern | Result |
|---|---|
| `/export` | Export all transactions to Google Sheets (append mode) |
| `/export all` | Clear sheet and do full fresh export of everything |
| `/export 2026-04` | Export April 2026 only |

- Export runs in background — bot responds immediately, does not block
- Requires `GOOGLE_SHEET_ID` and `GOOGLE_SHEETS_CREDENTIALS` env vars set
- If env vars missing → error message shown, no crash

## AC-13  Google Sheets auto-sync

- Every `save all` appends new transactions to Google Sheets automatically
- Sync is silent on failure — never blocks the bot or the save operation
- Fresh SQLite connection per sync thread — no threading errors

## AC-14  Railway stability

- DB rules refresh on every startup — schema changes take effect on next deploy
- Transaction data preserved across all deploys
- FOREIGN KEY constraint disabled during rules refresh, re-enabled after
- `fintracker.db` lives at `/data/fintracker.db` (persistent volume)
- `init_db()` logs `Database rules and taxonomy refreshed.` and `Transaction data preserved.` on every startup

## AC-15  Test coverage

- 150 parser tests passing (date, amount, payment, transfer, classification, trips, FX, loan rules, paid-by patterns)
- 64 bot integration tests passing (commit, correction, sessions, trips, projection)
- All tests pass on both Windows and Linux

---

# 13. Out of Scope — Phase 2

- **AI classifier** — Claude API for the 5% ambiguous entries. Training data (`classifier_examples`) already being collected from every correction.
- **Google Sheets importer** — migration of 3 years of historical data. 5-month sample tested, accuracy baseline established.
- **Analytics dashboard** — web UI with charts. `/summary` and `/entries` commands available via Slack in Phase 1.
- **bKash/bank reconciliation** — auto cross-check against MFS and bank statements
- **Money-to-collect tracker** — tracking shared expenses and who owes what
- **Multi-platform support** — Telegram and WhatsApp adapters planned for Phase 2. Architecture designed: platform-agnostic `core.py`, thin adapters per platform, simultaneous Slack + Telegram on same DB with session isolation by `platform:user_id`.
- **Multi-user / SaaS mode** — single-user only in Phase 1. Each person runs their own instance.

---

# 14. File Checklist

| Status | File | Notes |
|---|---|---|
| ✅ | `bot.py` | Slack bot — init_db refresh, Google Sheets sync, `/export` command, threading fix |
| ✅ | `parser.py` | NLP parser — transfer detection, loan rules, paid-by patterns, multi-date header |
| ✅ | `schema_v3_final.sql` | DB schema + classifier rules — vegetables, utility bills, seafood, pregnancy, sports, apple disambiguation, mobile data |
| ✅ | `requirements.txt` | google-auth, google-auth-httplib2, google-api-python-client added |
| ✅ | `test_parser.py` | 150 tests — loan transfer rules, paid-by patterns added |
| ✅ | `init_db.py` | Standalone DB init — for fresh setup only |
| ✅ | `railway_setup.md` | Railway guide — volume recovery steps, all CLI install methods |
| ❌ | `.env` | Secrets — never commit |
| ❌ | `fintracker.db` | Your data — never commit |
| ❌ | `venv/` | Local environment — never commit |

---

*Fintracker PRD v1.5.1  |  Khobaib Chowdhury  |  May 2026  |  Phase 1 Complete + Data Ownership*
