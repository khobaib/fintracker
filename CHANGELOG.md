# FINTRACKER — Changelog

All changes to the Fintracker project are documented here by PRD version.

---

## Changelog v1.4.1 — May 2026

### Bug Fixes
- Fixed `dinner + tips` being classified as gift — tips now has lower priority than food rules, so food context wins when combined
- Fixed `vegetables grocery` and any line containing the word "grocery" not being classified — added `grocery` as a keyword at high priority
- Fixed `wasim loan` (and any `[Name] loan - amount`) being classified as loan instead of transfer — removed bare `loan` keyword from rules engine; transfer detection in the parser handles this structurally
- Fixed `wasim` and other names containing `sim` being matched by the mobile_expense rule — added word boundary to `sim` pattern

---

## Changelog v1.4 — May 2026

### Accuracy Baseline Established
- Tested 5 months of real data (March–July 2025, 659 testable rows)
- 68% classified correctly by rules alone
- 25% are historical exceptions (expected — old data pre-dating category additions)
- 5% genuine ambiguity — reserved for AI classifier in Phase 2

### New Food Classification Rules
- Added Bangladeshi dishes: bhuna, vorta, shutki, ilish, hilsa, rezala, kalia, jhalmuri, shingara, singara, shomucha, doi, thali, set meal, paratha, luchi, kulfi, kul boroi, muri, kheer
- Added international dishes: steak, pasta, sushi, wrap, curry, roast, grilled, fried rice, dim sum, bbq, shawarma
- Added desserts and sweets: mousse, corn, lichu, lychee, kul boroi, chowmin
- Added common food words: soup, roll, chicken roll, egg, bread, butter, sausage
- Added street food: singara, shomucha (in addition to fuchka, chotpoti)
- Added food delivery subscription rule: foodi pro, foodi subscription → food_bill (not digital_product)
- Added Roll Chai exception at highest priority → food_bill (restaurant name, not beverage)

### New Beverage Rules
- Added coconut water, daab → beverages
- Added boba, bubble tea, pearl milk tea → beverages
- Added chai alongside cha — both recognised as tea (beverages)
- Added hot chocolate, cold chocolate, dark chocolate → beverages (priority over food rule)
- Plain "chocolate" alone → food_bill (unchanged)
- Moved juice to priority 6 — always beverages regardless of other context

### Health vs Medical Distinction
- Health: gym, fitness, yoga, pilates
- Health (skincare brands): cerave, neutrogena, garnier, loreal, nivea, vaseline
- Health (skincare generics): moisturizer, sunscreen, sunblock, spf, face wash, face cream, body lotion, lip balm, serum, vitamin c cream, vitamin e cream, skin care
- Health (grooming, from Jan 2026): haircut, beard trim, salon, parlour, barbershop
- Health (other): minoxidil, derma roller, hair serum, condom, contraceptive
- Medical: eye drop, eyedrop, ear drop (moved from health)
- Medical: teeth scaling, dental, dentist, orthodontist, tooth, teeth, braces (moved from health)
- Medical: medicine, saline, mm kit, ors, paracetamol, napa, antacid, antibiotic, syrup, tablet, capsule
- Medical: surgery, operation, medical test, ultrasound, xray, blood test

### New Grocery Rules
- Added: tissue, powder milk, baby wipes, diaper, nappy, toilet paper, toiletries, honey
- These were previously classified as "others" — now correctly go to grocery

### New Gift Rules
- Added: gablu (family member name), tips (standalone), biye bari tips, eid salami, salami keyword
- Confirmed: any Bkash to [Name] = gift going forward

### Loan Payment Rule
- Added: loan, loan pay, loan payment, loan repay, loan instalment, bank loan, loan return → loan expense
- Clarified: "X loan - 500" (giving a loan) = transfer; "X loan pay - 500" = expense
- Edge case: "Chaldal loan payment" → loan (Chaldal keyword does not override loan payment)

### New Commuting Rules
- Added: indrive, shohoz, obhai, rapido → commuting
- Added: expressway toll, road toll, toll → commuting
- Added: van → commuting
- Added: bike (standalone) → commuting (any bike payment regardless of whose ride)
- Added: rickshaw typo variants (rickhsaw, rickhsahw, riksha, rikshaw)

### New Recreation Rules
- Added: theatre, theater, shilpakala, concert, show ticket, cinema, movie
- Added: horse car, horse ride, boat ride, paddle boat, amusement, fair, mela, carnival
- Added: pot, kite → recreation

### New Shopping Rules
- Added clothing: panjabi, eid panjabi
- Added household goods: moshari, mosquito net, bedsheet, pillow, curtain, wipes, rope, string
- Added fragrance: perfume, cologne, deodorant, aftershave → shopping

### New Others Rules
- Added bank/MFS fees: statement charge, statement fee, solvency certificate, card annual fee, maintenance fee, bank charge
- Added: courier, parcel, pathao parcel → others (not commuting)
- Added: print, photocopy, lamination → others
- Clarified: sms charge, mobile data charge, mobile charge → mobile_expense (not others)
- Clarified: service charge + utility → accommodation; bare service charge → others

### New Accommodation Rules
- Added: ac bill, air condition bill, water filter, water bill, bua salary, maid salary, house salary

### [Name] Prefix Rule
- `[G]` or `[Name]` before food or beverage entry → purpose = treat (paid for guest)
- `[Name]` before commuting entry → commuting (still your expense)
- Example: `[G] chicken khichuri - 260` → treat

### Mixed Items Rule
- If a line contains multiple items with `+` and at least one is non-food → shopping
- Example: `chocolate + perfume - 27000` → shopping

### Bracket Convention Clarified
- First bracket always = restaurant/store/person name — never a classification keyword
- `masala dosa (apon coffee)` → food_bill (not beverages — "apon coffee" is a restaurant)
- `Dinner (Abesh Hotel)` → food_bill (not accommodation — hotel is the restaurant name)
- `[G] Lunch (chayer mela)` → treat ("Chayer Mela" is a restaurant name)

### Bug Fixes
- Fixed `cha` and `chai` using word-boundary matching — no longer matches inside "recharge", "charge", "teacher" etc.
- Fixed double-escaped backslashes in several regex rules that caused silent match failures
- Fixed `medicine`, `saline` not matching (same backslash escape issue)
- Fixed `teeth scaling` incorrectly going to health instead of medical

---

## Changelog v1.3 — April 2026

### New /entries Command
- `/entries 29 april` — full transaction list for a single day
- `/entries 5-12 april` — transaction list for a date range
- `/entries 5-12 april home` — home expenses only
- `/entries 5-12 april travel` — travel expenses only
- Output: transaction ID, date, purpose, amount (with original currency), payment method, city, trip flag
- Total expenses at the bottom (transfers excluded from total)

### /summary Date Range Support
- `/summary 29 april` — single day breakdown by purpose
- `/summary 29 april 2026` — with explicit year
- `/summary 5-12 april` — date range breakdown
- `/summary 5-12 april home` — date range, home only
- ISO format: `/summary 2026-04-29` and `/summary 2026-04-05 2026-04-12`
- Segment filter (home/travel) works on all date range variants

### /summary Month + Segment Combined
- `/summary april home` — April, home only
- `/summary april travel` — April, travel only
- Previously only month or segment worked separately

### Metro Card Zero-Amount Rule
- `metro card recharge - 200` → commuting, 200 BDT (this is the actual expense)
- `metro to office - metro card` → commuting, 0 BDT, no review needed
- Individual rides use the prepaid balance — no amount required

### Bank/MFS Service Charges
- `statement charge`, `statement fee`, `solvency certificate`, `card annual fee`, `maintenance fee` → others
- `sms charge`, `mobile data charge`, `mobile charge` → mobile_expense
- `service charge and utility` → accommodation
- Bare `service charge` → others

### Beverage Keywords
- Both `cha` and `chai` recognised as tea (beverages)
- Word-boundary matching prevents false hits on `recharge`, `statement charge`, `charge`

### Shopping Rules Expanded
- Added: cable, charger, adapter, earphone, headphone, keyboard, mouse, bag, wallet, shoe, sandal
- Added clothing: shirt, pant, trouser, jacket, hoodie, tshirt, dress, sari, saree, lungi, kameez, punjabi
- Added electronics: phone, watch, speaker, camera, usb, hdmi

### Common Food Words Added
- kabab, kebab, burger, pizza, pasta, shawarma, noodle, sandwich, hotdog
- cake, pastry, cookie, biscuit, brownie, waffle, donut, pudding, ice cream, chocolate
- biriyani, kacchi, tehari, halim, nihari, halwa, singara, chips, crisps, popcorn, nachos

### /actual Never Changes Global Rate
- Documented and enforced: `/actual` only updates `actual_amount_bdt` on specific transactions
- Global exchange rate unchanged — use `/rate` for that

---

## Changelog v1.2 — April 2026

### Review Summary Header Improvement
- Header now explicitly lists which line numbers need review and why
- Example: `⚠️  Lines needing review: *3* — no amount | *7* — needs ai classification`
- Previously only the count was shown — now the exact line numbers are visible

### Transfer Display Fix
- Transfer lines now show the amount when known: `ebl to bkash - 2000 — transfer — 2,000 BDT`
- Transfer lines without an amount are flagged for review with `no amount` reason
- Transfer descriptions now show in full — no truncation of notes in brackets

### Transfer Detection Order Fixed
- Transfer detection now runs before payment bracket parsing
- Previously: `dbbl to ebl - 40000 (bftn, received on 26 April)` — bracket note was stripped
- Now: full description preserved including any notes

### Missing Expense Amount — No Longer Defaults to 0
- Expenses with no amount now flag for review with `? BDT`
- Previously silently stored as 0 BDT
- 0 BDT is now only stored when explicitly written or when someone else paid

### Correction Commands — Stale Flags Fixed
- Correcting a line now clears `review_reason` immediately
- Previously: after `3 shopping`, line 3 still showed `[tag override, needs_ai_classification]`

### Rate Change Mid-Session
- `/rate usd 121` during an active session now recalculates ALL entries with that currency
- Previously only recalculated entries that had no amount yet
- Session summary updates immediately showing the new amounts
- Confirmation message shows how many entries were recalculated

### /actual — Three New Modes Added
- Mode 1 (existing): `/actual 42 1850` — set exact BDT for single transaction
- Mode 2 (new): `/actual 52 usd 110` — single transaction with currency and rate
- Mode 3 (new): `/actual 84-96 usd 123.7` — ID range, preview + confirm
- Mode 4 (new): `/actual 2026-04-02 2026-04-04 usd 1/140.6` — date range
- All modes support 1/X rate format
- ID range and date range modes show preview before saving, require confirm or cancel

### /rate — 1/X Notation Added
- `/rate idr 1/140.6` — use when 1 BDT buys more than 1 unit of foreign currency
- System stores `1 ÷ 140.6 = 0.00711` internally — user never has to calculate
- Rate confirmation now shows both directions (e.g. `1 IDR = 0.0071 BDT` and `1 BDT = 140.6 IDR`)

### Transaction IDs Shown After Save
- After `save all`, bot shows transaction ID range: `Transaction IDs: #48–#56`
- Makes it easy to use `/actual` immediately after saving

### PRD Correction Reference Added
- AC-09a: full correction command table (purpose, payment, amount, type)
- AC-09b: session command reference (save all, save anyway, review, cancel, confirm)
- AC-09c: end-to-end correction dialogue example

---

## Changelog v1.1 — April 2026

### No Currency = BDT
- If no currency code is present in a line, amount is treated as BDT directly
- No conversion needed, no flag, no review

### Payment Method — Slash Pattern Expanded
- `(foodi/ebl)` explicitly documented alongside `(foodi/bkash)`
- Any `(service/payment)` slash pattern works: left = platform/service, right = payment method

### Exchange Rate — 1/X Format
- `/rate idr 1/140.6` supported — system evaluates `1 ÷ 140.6` automatically
- Applies to all weak currencies where 1 BDT buys many foreign units

### /actual — Bulk Rate Update
- `/actual 84-96 usd 123.7` — update all USD entries in ID range 84 to 96
- `/actual 2026-04-02 2026-04-04 usd 1/140.6` — update by date range with 1/X rate
- Both modes show preview before saving; user types confirm or cancel

### /summary Home-Only and Travel-Only
- `/summary home` — current month, home expenses only
- `/summary travel` — current month, travel only
- `/summary april home` — month + segment combined (previously only one at a time)

### Category Versioning — Multiple Changes Supported
- Clarified that multiple category changes over time are fully supported
- Each change is one log entry in purpose_migration_log
- Full history always queryable
- Example: food_bill → meal_expense (2024) → food_bill (2025) — each tracked separately

### Historical Data Import Design
- New entries (Slack bot): purpose from parser rules + treat keyword
- Historical entries (Sheets import): purpose taken from Sheets column directly
- `source` field distinguishes them: `slack_bot` vs `sheets_import`
- Old treat entries import as treat even without the keyword in description text

### Classifier Training Weights Explained
- Confirmed entries: weight 1.0 (probably right)
- Corrected entries: weight 2.0 (definitely right — AI prioritises learning from corrections)

---

## v1.0 — April 2026 (Initial Release)

Initial release. See PRD v1.0 for full specification.

Core features delivered:
- NLP parser with rules engine (86 rules)
- Slack bot with review flow and corrections
- 6 slash commands: /trip, /rate, /rates, /actual, /summary, /entries
- Multi-currency support with estimated and actual BDT
- Trip management with home override
- Category versioning with migration log
- 145 parser tests and 64 bot integration tests passing
- Deployed on Railway, running 24/7
