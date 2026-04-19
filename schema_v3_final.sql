-- =============================================================================
-- FINTRACKER — Personal Finance Tracker
-- Database Schema v3.0
-- Author: Khobaib Chowdhury
-- =============================================================================
--
-- CHANGES FROM v2.0:
--   - Added `cities` table (name, slug, country, is_home). country_code dropped.
--   - transactions.city (free text) → transactions.city_id (FK to cities)
--   - trips.destination (free text country/region, not city — trips span cities)
--   - City input via @cityname in paste. Slug auto-normalized: "Bali"/"bali"/"BALI" → "bali"
--   - New cities auto-created on first use, no confirmation needed.
--   - Date header in paste sets transacted_at for all lines that follow.
--   - Multiple date sections supported in one paste.
--   - created_at always records when entry was typed (audit); transacted_at is the real date.
--
-- PASTE FORMAT SUMMARY:
--   17 April              ← date header (optional; defaults to today)
--   @Bali                 ← city header (optional; defaults to Dhaka)
--   hotel - 12.52 usd (ebl)
--   bike home - 40k idr (gojek)
--   snacks - 10k idr
--   rent dhaka - 25000 #home @dhaka   ← #home overrides trip; @dhaka overrides city
--
-- AMOUNT RULES:
--   All BDT stored as INTEGER (whole taka). Round: 0.5+ up, 0.4- down.
--   estimated_amount_bdt = what you recorded at entry time. Never overwritten.
--   actual_amount_bdt    = bank-confirmed figure, added later via /actual <id> <amount>.
--   amount_bdt (in views) = COALESCE(actual, estimated). Always the operative value.
--
-- TRIP SESSION:
--   /trip start "Indonesia March 2026"  → opens trip (ended_at = NULL)
--   /trip end                           → closes trip
--   One active trip at a time. Back-to-back trips allowed (end one, start next).
--   During active trip: all entries auto-tagged to trip unless line has #home.
--   #home tag → trip_id = NULL, is_home_during_trip = 1 for that line only.
--
-- PAYMENT BRACKET RULE:
--   "(ebl)" / "(scb)" / "(dbbl)" / "(bkash)" in raw text → sets payment method.
--   No bracket → cash default.
--   Service name (uber, gojek) = transport context only, not payment method.
--
-- =============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- =============================================================================
-- 1. CITIES
-- =============================================================================
-- Slug is the canonical key. Always lowercase, spaces → underscores.
-- "Bali", "bali", "BALI" all normalize to slug "bali" before lookup/insert.
-- Auto-created on first use — no confirmation step.
-- is_home = 1 marks your base city (Dhaka). Used as the default when no
-- city header or @tag is present in the paste.

CREATE TABLE IF NOT EXISTS cities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,           -- display name: "Bali", "Kuala Lumpur"
    slug        TEXT    NOT NULL UNIQUE,    -- normalized key: "bali", "kuala_lumpur"
    country     TEXT    NOT NULL,           -- "Indonesia", "Bangladesh"
    is_home     INTEGER NOT NULL DEFAULT 0, -- 1 = Dhaka (your base city)
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Seed: your home city + common destinations
INSERT OR IGNORE INTO cities (name, slug, country, is_home) VALUES
    ('Dhaka',           'dhaka',            'Bangladesh',   1),
    ('Chittagong',      'chittagong',       'Bangladesh',   0),
    ('Sylhet',          'sylhet',           'Bangladesh',   0),
    ('Cox''s Bazar',    'coxs_bazar',       'Bangladesh',   0),
    ('Bangkok',         'bangkok',          'Thailand',     0),
    ('Bali',            'bali',             'Indonesia',    0),
    ('Jakarta',         'jakarta',          'Indonesia',    0),
    ('Singapore',       'singapore',        'Singapore',    0),
    ('Kuala Lumpur',    'kuala_lumpur',     'Malaysia',     0);

-- View: all cities grouped by country — useful for /cities command
CREATE VIEW IF NOT EXISTS v_cities AS
SELECT country, name, slug, is_home
FROM cities
ORDER BY country, name;


-- =============================================================================
-- 2. PURPOSE TAXONOMY  (versioned — renames never break history)
-- =============================================================================

CREATE TABLE IF NOT EXISTS purpose_taxonomy (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    slug            TEXT    NOT NULL UNIQUE,
    parent_slug     TEXT    REFERENCES purpose_taxonomy(slug),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    deprecated_at   TEXT,                       -- NULL = active
    replaced_by_id  INTEGER REFERENCES purpose_taxonomy(id),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS purpose_migration_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    migrated_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    from_slug               TEXT    NOT NULL,
    to_slug                 TEXT    NOT NULL,
    transactions_affected   INTEGER,
    notes                   TEXT
);

-- tour_bill kept for legacy Google Sheets import only.
-- All new entries use real purpose + trip_id.
INSERT OR IGNORE INTO purpose_taxonomy (name, slug) VALUES
    ('Food bill',       'food_bill'),
    ('Treat',           'treat'),
    ('Grocery',         'grocery'),
    ('Shopping',        'shopping'),
    ('Medical',         'medical'),
    ('Health',          'health'),
    ('Accommodation',   'accommodation'),
    ('Commuting',       'commuting'),
    ('Mobile expense',  'mobile_expense'),
    ('Beverages',       'beverages'),
    ('Drinks',          'drinks'),
    ('Recreation',      'recreation'),
    ('Loan',            'loan'),
    ('Gift',            'gift'),
    ('Household',       'household'),
    ('Others',          'others'),
    ('Tour bill',       'tour_bill');    -- legacy import only


-- =============================================================================
-- 3. CURRENCIES & EXCHANGE RATES
-- =============================================================================
-- rate_to_bdt: 1 unit of foreign currency = X BDT
-- Example: USD → 122.5 means 1 USD = 122.5 BDT
--          IDR → 0.0074 means 1 IDR = 0.0074 BDT (so 40,000 IDR = 296 BDT)
--
-- Slack commands:
--   /rate usd 122.5     → insert new rate for USD (date = today)
--   /rate idr 0.0074    → insert new rate for IDR
--   /rates              → show v_current_rates (latest rate per currency)

CREATE TABLE IF NOT EXISTS currencies (
    code    TEXT    PRIMARY KEY,    -- ISO 4217: "BDT", "USD", "IDR"
    name    TEXT    NOT NULL,
    symbol  TEXT
);

INSERT OR IGNORE INTO currencies (code, name, symbol) VALUES
    ('BDT', 'Bangladeshi Taka',     '৳'),
    ('USD', 'US Dollar',            '$'),
    ('EUR', 'Euro',                 '€'),
    ('GBP', 'British Pound',        '£'),
    ('SGD', 'Singapore Dollar',     'S$'),
    ('THB', 'Thai Baht',            '฿'),
    ('IDR', 'Indonesian Rupiah',    'Rp'),
    ('MYR', 'Malaysian Ringgit',    'RM'),
    ('AUD', 'Australian Dollar',    'A$'),
    ('INR', 'Indian Rupee',         '₹'),
    ('JPY', 'Japanese Yen',         '¥');

CREATE TABLE IF NOT EXISTS exchange_rates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code   TEXT    NOT NULL REFERENCES currencies(code),
    rate_to_bdt     REAL    NOT NULL,
    effective_date  TEXT    NOT NULL DEFAULT (date('now')),
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rates_currency
    ON exchange_rates (currency_code, effective_date DESC);

-- Latest rate per currency — what /rates returns
CREATE VIEW IF NOT EXISTS v_current_rates AS
SELECT
    c.code,
    c.name,
    c.symbol,
    r.rate_to_bdt,
    r.effective_date,
    r.notes
FROM currencies c
JOIN exchange_rates r ON r.currency_code = c.code
WHERE r.effective_date = (
    SELECT MAX(r2.effective_date)
    FROM exchange_rates r2
    WHERE r2.currency_code = c.code
);


-- =============================================================================
-- 4. PAYMENT METHODS
-- =============================================================================
-- type:        cash | card | mfs | transit_card
-- institution: EBL | SCB | DBBL | bKash | Nagad | NULL (for cash/metro)
--
-- Bracket rule (enforced at parse time, not schema level):
--   "(ebl)"   in raw text → payment_method = ebl_card
--   "(scb)"   in raw text → payment_method = scb_card
--   "(dbbl)"  in raw text → payment_method = dbbl_card
--   "(bkash)" in raw text → payment_method = bkash
--   no bracket           → payment_method = cash (default)
--
-- Transport services (uber, gojek, pathao, grab) are NOT payment methods.
-- They are stored in transactions.transport_service for context only.
-- Payment for those rides is still determined by the bracket rule above.
--
-- Add new card later via Slack: /payment add "City Card" city_card card "City Bank"

CREATE TABLE IF NOT EXISTS payment_method (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    slug        TEXT    NOT NULL UNIQUE,
    type        TEXT    NOT NULL CHECK (type IN ('cash', 'card', 'mfs', 'transit_card')),
    institution TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1
);

INSERT OR IGNORE INTO payment_method (name, slug, type, institution) VALUES
    ('Cash',        'cash',         'cash',         NULL),
    ('bKash',       'bkash',        'mfs',          'bKash'),
    ('Nagad',       'nagad',        'mfs',          'Nagad'),
    ('EBL Card',    'ebl_card',     'card',         'EBL'),
    ('SCB Card',    'scb_card',     'card',         'SCB'),
    ('DBBL Card',   'dbbl_card',    'card',         'DBBL'),
    ('Metro Card',  'metro_card',   'transit_card', NULL);


-- =============================================================================
-- 5. ACCOUNTS  (for balance tracking and reconciliation)
-- =============================================================================

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    slug        TEXT    NOT NULL UNIQUE,
    type        TEXT    NOT NULL CHECK (type IN ('bank', 'mfs', 'cash', 'investment')),
    institution TEXT,
    currency    TEXT    NOT NULL DEFAULT 'BDT' REFERENCES currencies(code),
    is_active   INTEGER NOT NULL DEFAULT 1
);

INSERT OR IGNORE INTO accounts (name, slug, type, institution) VALUES
    ('Cash',            'cash',     'cash',     NULL),
    ('bKash',           'bkash',    'mfs',      'bKash'),
    ('EBL Account',     'ebl',      'bank',     'EBL'),
    ('SCB Account',     'scb',      'bank',     'SCB'),
    ('DBBL Account',    'dbbl',     'bank',     'DBBL');

-- Actual balance at a point in time.
-- System calculates expected balance from transactions.
-- Diff = missing or duplicate entries.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    snapshot_date   TEXT    NOT NULL,
    actual_balance  INTEGER NOT NULL,   -- whole BDT, rounded
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- =============================================================================
-- 6. TRIPS
-- =============================================================================
-- destination = country or region name ("Indonesia", "Thailand").
--   NOT a city — trips span multiple cities. City is tracked per transaction.
-- One active trip at a time: ended_at IS NULL.
-- Back-to-back trips allowed: /trip end then /trip start immediately after.
--
-- Slack commands:
--   /trip start "Indonesia March 2026"   → new row, ended_at = NULL
--   /trip end                            → sets ended_at = now on active trip
--   /trip list                           → shows all trips with dates + totals
--   /trip status                         → shows currently active trip if any

CREATE TABLE IF NOT EXISTS trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,       -- "Indonesia March 2026"
    destination     TEXT    NOT NULL,       -- "Indonesia" (country/region, not city)
    started_at      TEXT    NOT NULL,       -- "2026-03-15"
    ended_at        TEXT,                   -- NULL = currently active
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- At most one active trip at any time
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_trip
    ON trips (ended_at) WHERE ended_at IS NULL;

CREATE VIEW IF NOT EXISTS v_active_trip AS
SELECT * FROM trips WHERE ended_at IS NULL LIMIT 1;


-- =============================================================================
-- 7. TRANSACTIONS  (the core table)
-- =============================================================================
--
-- CITY:
--   city_id references cities table. Slug-normalized at parse time.
--   "Bali", "bali", "BALI" all resolve to cities.slug = "bali".
--   Default = home city (cities.is_home = 1 → Dhaka) when no @tag present.
--   @cityname in paste header → default for all lines that day.
--   @cityname on a single line → overrides just that transaction.
--   New cities auto-created on first use (name, slug, country auto-detected or prompted).
--
-- DATE:
--   transacted_at = the real date of the expense (from date header in paste).
--   created_at    = when you actually typed the entry (audit trail).
--   These differ when you enter yesterday's expenses today.
--
-- AMOUNTS:
--   original_amount     = what you paid in foreign currency (e.g. 12.52 for USD)
--   original_currency   = the currency code ("USD", "IDR"). NULL if BDT.
--   exchange_rate_used  = rate from exchange_rates at time of entry.
--   estimated_amount_bdt = ROUND(original_amount * exchange_rate_used).
--                          Direct BDT input stored here as-is.
--                          INTEGER. Set at entry time. Never updated.
--   actual_amount_bdt   = bank-confirmed BDT charge, added later.
--                          Set via /actual <tx_id> <amount>. NULL until confirmed.
--                          Never overwrites estimated_amount_bdt.
--   In all views: amount_bdt = COALESCE(actual_amount_bdt, estimated_amount_bdt)
--
-- TRIP:
--   trip_id = NULL              → home expense
--   trip_id = <id>              → belongs to this trip
--   is_home_during_trip = 1     → #home tag was used to override trip assignment
--                                  (trip was active but this specific expense is home)

CREATE TABLE IF NOT EXISTS transactions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- CLASSIFICATION
    type                    TEXT    NOT NULL CHECK (type IN ('expense', 'transfer', 'investment')),
    purpose_id              INTEGER REFERENCES purpose_taxonomy(id),

    -- CITY
    city_id                 INTEGER NOT NULL REFERENCES cities(id),

    -- AMOUNTS
    original_amount         REAL,               -- foreign currency value (e.g. 12.52)
    original_currency       TEXT    REFERENCES currencies(code),    -- NULL if BDT
    exchange_rate_used      REAL,               -- rate at time of entry
    estimated_amount_bdt    INTEGER NOT NULL,   -- operative BDT at entry time, rounded
    actual_amount_bdt       INTEGER,            -- bank-confirmed; NULL until set

    -- DATE
    transacted_at           TEXT    NOT NULL,   -- real expense date: "2026-03-17"

    -- TRIP
    trip_id                 INTEGER REFERENCES trips(id),   -- NULL = home
    is_home_during_trip     INTEGER NOT NULL DEFAULT 0,     -- 1 = #home override used

    -- PAYMENT
    payment_method_id       INTEGER REFERENCES payment_method(id),
    transport_service       TEXT,       -- "uber"|"pathao"|"gojek"|"grab" — context only

    -- RAW INPUT (immutable — exactly what you typed)
    raw_text                TEXT,       -- "hotel pondok ijo - 12.52 usd (ebl)"

    -- DETAILS (your memory layer)
    -- raw_text = the server log. Immutable. What you typed.
    -- details  = the diary entry. Human-readable, may be enriched.
    -- Simple entry: both are the same.
    -- Memorable entry: details might be "Sate Ratu dinner with Melisa, last night in Bali"
    details                 TEXT,

    -- AI AUDIT
    ai_suggested            INTEGER NOT NULL DEFAULT 0,
    ai_confidence           REAL,
    ai_model_version        TEXT,
    user_corrected          INTEGER NOT NULL DEFAULT 0,

    -- SOURCE
    source                  TEXT    NOT NULL DEFAULT 'slack_bot',
    -- "slack_bot" | "manual" | "sheets_import" | "bkash_sync"

    -- SYSTEM
    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER IF NOT EXISTS trg_transactions_updated
    AFTER UPDATE ON transactions FOR EACH ROW
    BEGIN
        UPDATE transactions SET updated_at = datetime('now') WHERE id = OLD.id;
    END;

CREATE INDEX IF NOT EXISTS idx_tx_date      ON transactions (transacted_at);
CREATE INDEX IF NOT EXISTS idx_tx_type      ON transactions (type);
CREATE INDEX IF NOT EXISTS idx_tx_purpose   ON transactions (purpose_id);
CREATE INDEX IF NOT EXISTS idx_tx_city      ON transactions (city_id);
CREATE INDEX IF NOT EXISTS idx_tx_trip      ON transactions (trip_id);
CREATE INDEX IF NOT EXISTS idx_tx_payment   ON transactions (payment_method_id);


-- =============================================================================
-- 8. TRANSFER DETAILS
-- =============================================================================

CREATE TABLE IF NOT EXISTS transfer_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
    to_account_id   INTEGER NOT NULL REFERENCES accounts(id),
    notes           TEXT
);


-- =============================================================================
-- 9. AI CLASSIFIER — RULES ENGINE
-- =============================================================================
-- Fires before the AI. Pattern matched against lowercased raw_text.
-- Priority: lower number = checked first.
-- confidence = 1.0 → skip AI for this field entirely.
-- Multiple rules can match; highest priority (lowest number) wins per field.

CREATE TABLE IF NOT EXISTS classifier_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern             TEXT    NOT NULL,
    match_type          TEXT    NOT NULL DEFAULT 'keyword', -- "keyword" | "regex"
    tx_type             TEXT,
    purpose_slug        TEXT,
    payment_slug        TEXT,
    transport_service   TEXT,
    confidence          REAL    NOT NULL DEFAULT 1.0,
    priority            INTEGER NOT NULL DEFAULT 100,
    is_active           INTEGER NOT NULL DEFAULT 1,
    notes               TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Priority bands:
--   5   structural (transfer patterns, bracket payment signals)
--   10  transport (always unambiguous)
--   15  known merchants (supermarkets, pharmacies)
--   20  food / household / mobile
--   50  lower-confidence patterns (AI still reviews)

INSERT OR IGNORE INTO classifier_rules
    (pattern, match_type, tx_type, purpose_slug, payment_slug, transport_service, confidence, priority, notes)
VALUES
    -- STRUCTURAL: transfers
    ('to bkash|to ebl|to scb|to dbbl|to nagad|ebl to|bkash to|scb to|dbbl to',
        'regex',    'transfer', NULL,           NULL,           NULL,       1.0,  5,  'Account transfer'),

    -- STRUCTURAL: bracket payment overrides
    ('\\(ebl\\)',   'regex',    NULL, NULL, 'ebl_card',     NULL, 1.0,  5, 'EBL card bracket'),
    ('\\(scb\\)',   'regex',    NULL, NULL, 'scb_card',     NULL, 1.0,  5, 'SCB card bracket'),
    ('\\(dbbl\\)',  'regex',    NULL, NULL, 'dbbl_card',    NULL, 1.0,  5, 'DBBL card bracket'),
    ('\\(bkash\\)', 'regex',    NULL, NULL, 'bkash',        NULL, 1.0,  5, 'bKash bracket'),

    -- TRANSPORT
    ('uber',        'keyword',  'expense', 'commuting', 'cash', 'uber',     1.0, 10, 'Uber ride, cash default'),
    ('pathao',      'keyword',  'expense', 'commuting', 'cash', 'pathao',   1.0, 10, 'Pathao ride'),
    ('gojek',       'keyword',  'expense', 'commuting', 'cash', 'gojek',    1.0, 10, 'Gojek ride'),
    ('grab',        'keyword',  'expense', 'commuting', 'cash', 'grab',     1.0, 10, 'Grab ride'),
    ('rickshaw',    'keyword',  'expense', 'commuting', 'cash', NULL,       1.0, 10, 'Rickshaw'),
    ('cng',         'keyword',  'expense', 'commuting', 'cash', NULL,      0.95, 10, 'CNG'),
    ('metro',       'keyword',  'expense', 'commuting', 'metro_card', NULL, 1.0, 10, 'Metro rail'),
    ('bus',         'keyword',  'expense', 'commuting', 'cash', NULL,       0.9, 10, 'Bus'),
    ('bike to',     'keyword',  'expense', 'commuting', 'cash', 'uber',     1.0,  5, 'Bike/office'),
    ('flight',      'keyword',  'expense', 'commuting', NULL,   NULL,       1.0, 10, 'Flight'),

    -- KNOWN MERCHANTS
    ('shajgoj|chaldal|unimart|meena bazar|agora|lavender|shopno',
        'regex',    'expense', 'grocery',  NULL, NULL, 1.0, 15, 'Supermarkets'),
    ('pharmacy|chemist|drugstore',
        'regex',    'expense', 'medical',  NULL, NULL, 1.0, 15, 'Pharmacy'),
    ('doctor|clinic|hospital|diagnostic|lab test|labs',
        'regex',    'expense', 'medical',  NULL, NULL, 0.95,15, 'Medical services'),

    -- FOOD & BEVERAGES
    ('fuchka|chotpoti|bhelpuri',
        'regex',    'expense', 'food_bill',    'cash', NULL, 1.0, 20, 'Street food'),
    ('badam|peanut|chanachur|chips',
        'regex',    'expense', 'food_bill',    'cash', NULL, 0.9, 20, 'Snacks'),
    ('guava|watermelon|mango|aam|papaya|banana|fruit',
        'regex',    'expense', 'food_bill',    'cash', NULL, 0.9, 20, 'Fresh fruit'),
    ('water|mineral water',
        'regex',    'expense', 'beverages',    'cash', NULL, 0.9, 20, 'Water'),
    ('tea|cha|coffee',
        'regex',    'expense', 'beverages',    'cash', NULL, 0.85,20, 'Hot drinks'),
    ('juice',
        'keyword',  'expense', 'beverages',    'cash', NULL, 0.9, 20, 'Juice'),

    -- HOUSEHOLD
    ('cockroach|mosquito|coil|detergent|broom|mop|cleaning|gel|spray|disinfect',
        'regex',    'expense', 'household',    'cash', NULL, 0.9, 20, 'Household items'),

    -- MOBILE / INTERNET
    ('recharge|internet pack|mb |data pack|sim|robi|grameenphone|gp |banglalink|teletalk',
        'regex',    'expense', 'mobile_expense', NULL, NULL, 0.9, 20, 'Mobile/internet'),

    -- ACCOMMODATION
    ('hotel|hostel|airbnb|guesthouse|resort|villa|check.in|check.out',
        'regex',    'expense', 'accommodation', NULL, NULL, 0.95,20, 'Accommodation'),
    ('rent|service charge|utility bill|internet.*bill|garbage.*bill|ac rent|advance payment for room',
        'regex',    'expense', 'accommodation', NULL, NULL, 0.95,15, 'Rent and home bills'),

    -- RECREATION
    ('park ticket|entry ticket|museum|zoo|aquarium|theme park',
        'regex',    'expense', 'recreation',   NULL, NULL, 0.9, 20, 'Entry tickets');


-- =============================================================================
-- 10. AI CLASSIFIER — TRAINING EXAMPLES
-- =============================================================================

CREATE TABLE IF NOT EXISTS classifier_examples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text        TEXT    NOT NULL,
    purpose_slug    TEXT    NOT NULL,
    payment_slug    TEXT,
    tx_type         TEXT    NOT NULL DEFAULT 'expense',
    source          TEXT    NOT NULL,
    -- "user_confirmed" | "user_corrected" | "rule_matched"
    weight          REAL    NOT NULL DEFAULT 1.0,
    -- corrections stored with weight 2.0; confirmations 1.0
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_examples_purpose ON classifier_examples (purpose_slug);


-- =============================================================================
-- 11. SLACK SESSION — REVIEW FLOW
-- =============================================================================

CREATE TABLE IF NOT EXISTS slack_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slack_user_id   TEXT    NOT NULL,
    slack_channel   TEXT    NOT NULL,
    raw_message     TEXT    NOT NULL,
    entry_date      TEXT    NOT NULL,       -- parsed from date header; default = today
    default_city_id INTEGER REFERENCES cities(id),  -- parsed from @city header
    trip_id         INTEGER REFERENCES trips(id),   -- active trip at time of paste
    status          TEXT    NOT NULL DEFAULT 'pending',
    -- "pending" | "committed" | "abandoned"
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    committed_at    TEXT
);

CREATE TABLE IF NOT EXISTS pending_transactions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id              INTEGER NOT NULL REFERENCES slack_sessions(id),
    line_number             INTEGER NOT NULL,
    raw_line                TEXT    NOT NULL,

    -- Parsed / suggested fields
    parsed_type             TEXT,
    parsed_purpose_slug     TEXT,
    parsed_amount_bdt       INTEGER,
    parsed_original_amount  REAL,
    parsed_currency         TEXT,
    parsed_payment_slug     TEXT,
    parsed_transport        TEXT,
    parsed_city_slug        TEXT,
    parsed_details          TEXT,
    parsed_is_home          INTEGER DEFAULT 0,  -- 1 if #home detected

    -- Classification metadata
    match_source            TEXT,               -- "rule" | "ai" | "manual"
    confidence              REAL,
    needs_review            INTEGER DEFAULT 0,

    -- Your review action
    user_action             TEXT,               -- "confirmed" | "corrected" | "skipped"
    final_type              TEXT,
    final_purpose_slug      TEXT,
    final_payment_slug      TEXT,
    final_amount_bdt        INTEGER,
    final_city_slug         TEXT,
    final_details           TEXT,

    -- Set after commit
    transaction_id          INTEGER REFERENCES transactions(id)
);


-- =============================================================================
-- 12. VIEWS
-- =============================================================================

-- Master view — all joins resolved, amount_bdt logic applied
CREATE VIEW IF NOT EXISTS v_transactions AS
SELECT
    t.id,
    t.transacted_at,
    strftime('%Y-%m',   t.transacted_at)    AS month,
    strftime('%Y',      t.transacted_at)    AS year,
    strftime('%W',      t.transacted_at)    AS week_of_year,

    -- City
    c.name                                  AS city,
    c.slug                                  AS city_slug,
    c.country,
    c.is_home                               AS city_is_home,

    -- Classification
    t.type,
    p.name                                  AS purpose,
    p.slug                                  AS purpose_slug,

    -- Payment
    pm.name                                 AS payment_method,
    pm.institution                          AS payment_institution,
    t.transport_service,

    -- Amounts
    COALESCE(t.actual_amount_bdt,
             t.estimated_amount_bdt)        AS amount_bdt,
    t.estimated_amount_bdt,
    t.actual_amount_bdt,
    CASE
        WHEN t.actual_amount_bdt IS NOT NULL
         AND t.original_currency IS NOT NULL
        THEN t.actual_amount_bdt - t.estimated_amount_bdt
        ELSE NULL
    END                                     AS exchange_diff_bdt,
    t.original_amount,
    t.original_currency,
    t.exchange_rate_used,

    -- Dates
    t.transacted_at                         AS expense_date,
    t.created_at                            AS entry_date,

    -- Trip
    t.trip_id,
    tr.name                                 AS trip_name,
    tr.destination                          AS trip_destination,
    CASE WHEN t.trip_id IS NOT NULL
         THEN 1 ELSE 0 END                  AS is_travel,
    t.is_home_during_trip,

    -- Notes
    t.details,
    t.raw_text,

    -- AI audit
    t.ai_suggested,
    t.ai_confidence,
    t.user_corrected,
    t.source,
    t.created_at
FROM transactions t
LEFT JOIN cities            c   ON t.city_id            = c.id
LEFT JOIN purpose_taxonomy  p   ON t.purpose_id          = p.id
LEFT JOIN payment_method    pm  ON t.payment_method_id   = pm.id
LEFT JOIN trips             tr  ON t.trip_id             = tr.id;


-- Monthly summary with segment filter
-- segment: "all" | "home" | "travel"
-- Usage: SELECT * FROM v_monthly_summary WHERE month='2026-04' AND segment='home'
CREATE VIEW IF NOT EXISTS v_monthly_summary AS
SELECT month, year, purpose, purpose_slug,
       'all'        AS segment,
       COUNT(*)     AS tx_count,
       SUM(amount_bdt) AS total_bdt
FROM v_transactions WHERE type = 'expense'
GROUP BY month, purpose_slug

UNION ALL

SELECT month, year, purpose, purpose_slug,
       'home'       AS segment,
       COUNT(*)     AS tx_count,
       SUM(amount_bdt) AS total_bdt
FROM v_transactions WHERE type = 'expense' AND is_travel = 0
GROUP BY month, purpose_slug

UNION ALL

SELECT month, year, purpose, purpose_slug,
       'travel'     AS segment,
       COUNT(*)     AS tx_count,
       SUM(amount_bdt) AS total_bdt
FROM v_transactions WHERE type = 'expense' AND is_travel = 1
GROUP BY month, purpose_slug

ORDER BY month DESC, segment, total_bdt DESC;


-- Home vs travel split per month
CREATE VIEW IF NOT EXISTS v_monthly_home_vs_travel AS
SELECT
    month,
    year,
    SUM(CASE WHEN is_travel = 0 THEN amount_bdt ELSE 0 END) AS home_total_bdt,
    SUM(CASE WHEN is_travel = 1 THEN amount_bdt ELSE 0 END) AS travel_total_bdt,
    SUM(amount_bdt)                                          AS grand_total_bdt,
    COUNT(CASE WHEN is_travel = 0 THEN 1 END)               AS home_tx_count,
    COUNT(CASE WHEN is_travel = 1 THEN 1 END)               AS travel_tx_count
FROM v_transactions
WHERE type = 'expense'
GROUP BY month
ORDER BY month DESC;


-- City breakdown per month — "how much in Bali vs Jakarta in March?"
CREATE VIEW IF NOT EXISTS v_monthly_by_city AS
SELECT
    month,
    year,
    city,
    city_slug,
    country,
    purpose,
    purpose_slug,
    COUNT(*)            AS tx_count,
    SUM(amount_bdt)     AS total_bdt
FROM v_transactions
WHERE type = 'expense'
GROUP BY month, city_slug, purpose_slug
ORDER BY month DESC, total_bdt DESC;


-- Full trip summary (date-independent)
CREATE VIEW IF NOT EXISTS v_trip_summary AS
SELECT
    trip_id,
    trip_name,
    trip_destination,
    city,
    city_slug,
    purpose,
    purpose_slug,
    COUNT(*)            AS tx_count,
    SUM(amount_bdt)     AS total_bdt,
    original_currency,
    SUM(original_amount) AS total_original
FROM v_transactions
WHERE is_travel = 1 AND type = 'expense'
GROUP BY trip_id, city_slug, purpose_slug, original_currency
ORDER BY trip_id, total_bdt DESC;


-- Trip × calendar month slice
CREATE VIEW IF NOT EXISTS v_trip_by_month AS
SELECT
    trip_id,
    trip_name,
    month,
    city,
    city_slug,
    purpose,
    purpose_slug,
    COUNT(*)            AS tx_count,
    SUM(amount_bdt)     AS total_bdt
FROM v_transactions
WHERE is_travel = 1 AND type = 'expense'
GROUP BY trip_id, month, city_slug, purpose_slug
ORDER BY trip_id, month, total_bdt DESC;


-- Trip × week slice
CREATE VIEW IF NOT EXISTS v_trip_by_week AS
SELECT
    trip_id,
    trip_name,
    week_of_year,
    month,
    city,
    city_slug,
    purpose,
    purpose_slug,
    COUNT(*)            AS tx_count,
    SUM(amount_bdt)     AS total_bdt
FROM v_transactions
WHERE is_travel = 1 AND type = 'expense'
GROUP BY trip_id, week_of_year, city_slug, purpose_slug
ORDER BY trip_id, week_of_year, total_bdt DESC;


-- Daily average + monthly projection (current month)
-- 0-spend days counted — they correctly bring the average down.
-- Shows both: all spending and home-only (travel excluded).
CREATE VIEW IF NOT EXISTS v_monthly_projection AS
SELECT
    strftime('%Y-%m', 'now')        AS current_month,
    CAST(strftime('%d', 'now') AS INTEGER)
                                    AS days_elapsed,
    CAST(strftime('%d',
        date(strftime('%Y-%m', 'now') || '-01', '+1 month', '-1 day')
    ) AS INTEGER)                   AS days_in_month,

    -- All spending
    SUM(CASE WHEN month = strftime('%Y-%m', 'now')
         AND type = 'expense' THEN amount_bdt ELSE 0 END)
                                    AS month_total_bdt,
    ROUND(
        SUM(CASE WHEN month = strftime('%Y-%m', 'now')
             AND type = 'expense' THEN amount_bdt ELSE 0 END)
        * 1.0 / CAST(strftime('%d', 'now') AS INTEGER)
    )                               AS daily_avg_bdt,
    ROUND(
        SUM(CASE WHEN month = strftime('%Y-%m', 'now')
             AND type = 'expense' THEN amount_bdt ELSE 0 END)
        * 1.0 / CAST(strftime('%d', 'now') AS INTEGER)
        * CAST(strftime('%d',
            date(strftime('%Y-%m', 'now') || '-01', '+1 month', '-1 day')
          ) AS INTEGER)
    )                               AS projected_month_bdt,

    -- Home-only spending
    SUM(CASE WHEN month = strftime('%Y-%m', 'now')
         AND type = 'expense' AND is_travel = 0 THEN amount_bdt ELSE 0 END)
                                    AS home_month_total_bdt,
    ROUND(
        SUM(CASE WHEN month = strftime('%Y-%m', 'now')
             AND type = 'expense' AND is_travel = 0 THEN amount_bdt ELSE 0 END)
        * 1.0 / CAST(strftime('%d', 'now') AS INTEGER)
    )                               AS home_daily_avg_bdt,
    ROUND(
        SUM(CASE WHEN month = strftime('%Y-%m', 'now')
             AND type = 'expense' AND is_travel = 0 THEN amount_bdt ELSE 0 END)
        * 1.0 / CAST(strftime('%d', 'now') AS INTEGER)
        * CAST(strftime('%d',
            date(strftime('%Y-%m', 'now') || '-01', '+1 month', '-1 day')
          ) AS INTEGER)
    )                               AS home_projected_month_bdt

FROM v_transactions;


-- AI accuracy per model version
CREATE VIEW IF NOT EXISTS v_ai_accuracy AS
SELECT
    ai_model_version,
    COUNT(*)                                                    AS total_classified,
    SUM(user_corrected)                                         AS corrections,
    ROUND((1.0 - SUM(user_corrected) * 1.0 / COUNT(*)) * 100, 1)
                                                                AS accuracy_pct
FROM transactions
WHERE ai_suggested = 1
GROUP BY ai_model_version;


-- =============================================================================
-- END OF SCHEMA v3.0
-- =============================================================================
