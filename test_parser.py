# =============================================================================
# FINTRACKER — Parser Tests
# Tests every pattern found in real data
# =============================================================================

import sys
import sqlite3
from datetime import date

sys.path.insert(0, '/home/claude/fintracker')
from parser import (
    parse_date, parse_amount_expression, parse_payment_bracket,
    parse_transfer, parse_third_party_paid, parse_cashback,
    parse_prefix_tag, extract_inline_tags, split_description_and_amount,
    parse_line, parse_paste, format_review_summary, apply_exchange_rates,
    to_slug
)

# Load real schema and seed data
def make_db():
    conn = sqlite3.connect(':memory:')
    with open('schema_v3_final.sql', encoding='utf-8') as f:
        conn.executescript(f.read())
    # Add exchange rates for tests
    conn.execute("INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date) VALUES ('USD', 122.5, '2026-01-01')")
    conn.execute("INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date) VALUES ('IDR', 0.0074, '2026-01-01')")
    conn.execute("INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date) VALUES ('AUD', 80.0, '2026-01-01')")
    conn.commit()
    return conn

DB = make_db()

# =============================================================================
# TEST HELPERS
# =============================================================================

passed = 0
failed = 0

def check(label, got, expected):
    global passed, failed
    if got == expected:
        print(f"  ✅ {label}")
        passed += 1
    else:
        print(f"  ❌ {label}")
        print(f"     expected: {repr(expected)}")
        print(f"     got:      {repr(got)}")
        failed += 1

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# =============================================================================
# 1. DATE PARSING
# =============================================================================
section("DATE PARSING")

today_year = date.today().year

check("17 April",    parse_date("17 April"),    date(today_year, 4, 17))
check("17 Apr",      parse_date("17 Apr"),       date(today_year, 4, 17))
check("April 17",    parse_date("April 17"),     date(today_year, 4, 17))
check("17/04",       parse_date("17/04"),        date(today_year, 4, 17))
check("17-04",       parse_date("17-04"),        date(today_year, 4, 17))
check("2026-04-17",  parse_date("2026-04-17"),   date(2026, 4, 17))
check("not a date",  parse_date("rickshaw - 60"), None)
check("not a date2", parse_date("@Bali"),         None)
check("blank",       parse_date(""),              None)


# =============================================================================
# 2. AMOUNT PARSING
# =============================================================================
section("AMOUNT PARSING")

def amt(expr):
    a, c = parse_amount_expression(expr)
    return (round(a) if a is not None else None, c)

check("plain int",          amt("350"),                 (350, None))
check("float round up",     amt("350.7"),               (351, None))
check("float round down",   amt("350.3"),               (350, None))
check("sum",                amt("60 + 40 + 30"),        (130, None))
check("sum no spaces",      amt("80+60"),               (140, None))
check("sum 4 terms",        amt("50 + 30 + 40 + 40"),  (160, None))
check("multiply",           amt("2*500"),               (1000, None))
check("k suffix IDR",       amt("10k idr"),             (10000, "IDR"))
check("float k suffix",     amt("12.52 usd"),           (13, "USD"))  # rounded
check("k suffix large",     amt("212k idr"),            (212000, "IDR"))
check("m suffix",           amt("1.5m idr"),            (1500000, "IDR"))
check("sheets formula",     amt("=1067+150"),           (1217, None))
check("with comma",         amt("25,000"),              (25000, None))
check("tips suffix",        amt("220 + 20 tk tips."),   (240, None))
check("tips short",         amt("120 + 20 tips"),       (140, None))
check("500+500 transfer",   amt("500 + 500"),            (1000, None))
check("aud currency",       amt("4.49 aud"),             (4, "AUD"))  # rounded
check("slack 8.75 usd",     amt("8.75 usd"),             (9, "USD"))  # rounded
check("empty",              amt(""),                    (None, None))
check("no number",          amt("metro card"),          (None, None))


# =============================================================================
# 3. PAYMENT BRACKET PARSING
# =============================================================================
section("PAYMENT BRACKET PARSING")

def pay(line):
    _, payment, platform, service = parse_payment_bracket(line)
    return payment, platform, service

# No bracket → cash default
check("no bracket cash default",    pay("fuchka - 120"),                ("cash", None, None))
check("plain ebl",                  pay("dinner - 2105 (ebl)"),         ("ebl_card", None, None))
check("plain scb",                  pay("rent - 14000 (scb)"),          ("scb_card", None, None))
check("plain dbbl",                 pay("shopping - 2500 (dbbl)"),      ("dbbl_card", None, None))
check("plain bkash",                pay("recharge - 399 (bkash)"),      ("bkash", None, None))
check("foodi/bkash",                pay("dinner - 691 (foodi/bkash)"),  ("bkash", "foodi", None))
check("fp/bkash",                   pay("lunch - 308 (fp/bkash)"),      ("bkash", "fp", None))
check("foodi/ebl",                  pay("lunch - 686 (foodi/ebl)"),     ("ebl_card", "foodi", None))
check("uber/cash",                  pay("bike - 150 (uber/cash)"),      ("cash", None, "uber"))
check("uber/bkash",                 pay("bike - 180 (uber/bkash)"),     ("bkash", None, "uber"))
check("uber/manual bkash",          pay("bike - 143 (uber/manual bkash)"), ("bkash", None, "uber"))
check("uber/personal bkash",        pay("bike - 145 (uber/personal bkash)"), ("bkash", None, "uber"))
check("uber/bkash manual",          pay("bike - 175 (uber/bkash manual)"),  ("bkash", None, "uber"))
check("pathao/cash",                pay("bike - 140 (pathao)"),         ("cash", None, "pathao"))
check("uber with note",             pay("bike - 250 (uber, too much charge)"), ("cash", None, "uber"))
check("metro card bare",            pay("metro to uttara - metro card"), ("metro_card", None, None))
check("desc paren + payment",       pay("dinner (thai express) - 2105 (ebl)"), ("ebl_card", None, None))
check("multiple desc parens + pay", pay("(sate ratu) dinner - 212k idr (ebl)"), ("ebl_card", None, None))
check("name paren no payment",      pay("khichuri (with samir) - 350 (bkash)"), ("bkash", None, None))


# =============================================================================
# 4. TRANSFER DETECTION
# =============================================================================
section("TRANSFER DETECTION")

def transfer(line):
    is_t, frm, to = parse_transfer(line.lower())
    return is_t, frm, to

check("ebl to bkash",       transfer("ebl to bkash - 2000"),     (True, "ebl", "bkash"))
check("scb to bkash",       transfer("scb to bkash - 3000"),     (True, "scb", "bkash"))
check("dbbl to ebl",        transfer("DBBL to EBL - 30000"),     (True, "dbbl", "ebl"))
check("scb to ebl",         transfer("scb to ebl - 15000"),      (True, "scb", "ebl"))
check("bkash cashout",      transfer("bkash cashout - 2000"),    (True, "bkash", "cash"))
check("ebl cashout",        transfer("EBL cashout - 6500"),      (True, "ebl", "cash"))
check("ebl cash withdraw",  transfer("ebl cash withdraw - 2500"), (True, "ebl", "cash"))
check("not a transfer",     transfer("bike to office - 150"),    (False, None, None))
check("not a transfer 2",   transfer("fuchka - 120"),            (False, None, None))
# Loan rules (AC-04)
check("person loan transfer",      transfer("wasim loan - 500"),             (True,  "cash", "person"))
check("friend loan transfer",      transfer("friend_xyz loan - 500"),        (True,  "cash", "person"))
check("person loan pay → expense", transfer("friend_xyz loan pay - 182000"), (False, None,   None))
check("ebl loan pay → expense",    transfer("ebl loan pay - 2000"),          (False, None,   None))
check("loan payment → expense",    transfer("chaldal loan payment - 100000"),(False, None,   None))


# =============================================================================
# 5. THIRD-PARTY PAID
# =============================================================================
section("THIRD-PARTY PAID")

def tp(line):
    _, paid_by, _ = parse_third_party_paid(line)
    return paid_by

check("friend_xyz paid bracket",  tp("fuchka - 100 (friend_xyz paid)"),           "Friend_xyz")
check("friend_xyz paid no amt",   tp("Dinner (food items) - friend_xyz paid"), "Friend_xyz")
check("waste box",          tp("Waste box - 100 (friend_xyz paid)"),         "Friend_xyz")
check("not paid",           tp("dinner (thai express) - 2105 (ebl)"),  None)
check("not paid 2",         tp("bike to office - 150"),                 None)


# =============================================================================
# 6. CASHBACK
# =============================================================================
section("CASHBACK")

def cb(line):
    remaining, cashback = parse_cashback(line)
    return cashback

check("45 tk cashback",     cb("Breakfast - 452 (foodi/bkash) - 45 tk cashback"),  45)
check("50 tk cashback",     cb("Brunch - 579 (foodi/bkash) - 50 tk cashback"),     50)
check("no cashback",        cb("dinner - 691 (foodi/bkash)"),                      None)


# =============================================================================
# 7. PREFIX TAG [Name]
# =============================================================================
section("PREFIX TAG")

def prefix(line):
    _, paid_for = parse_prefix_tag(line)
    return paid_for

check("[Friend_xyz] prefix",  prefix("[Friend_xyz] bike to destination - 150 (uber)"),  "Friend_xyz")
check("[G] prefix",     prefix("[G] car to bashundhara - 542"),         "G")
check("no prefix",      prefix("bike to office - 150"),                  None)


# =============================================================================
# 8. INLINE TAGS
# =============================================================================
section("INLINE TAGS")

def tags(line):
    _, t = extract_inline_tags(line)
    return t

check("#home tag",          tags("rent - 25000 #home")["is_home"],          True)
check("@city tag",          tags("hotel - 120 @bali")["city_slug"],          "bali")
check("#purpose tag",       tags("dinner - 500 #treat")["purpose_slug"],     "treat")
check("no tags",            tags("fuchka - 120"),                             {"is_home": False, "purpose_slug": None, "city_slug": None})
check("#home + @city",      tags("rent - 25000 #home @dhaka")["is_home"],    True)
check("#home + @city slug", tags("rent - 25000 #home @dhaka")["city_slug"],  "dhaka")


# =============================================================================
# 9. DESCRIPTION + AMOUNT SPLIT
# =============================================================================
section("DESCRIPTION / AMOUNT SPLIT")

def split(line):
    return split_description_and_amount(line)

check("simple",         split("rickshaw - 60"),                         ("rickshaw", "60"))
check("with paren",     split("dinner (tandoor) - 691"),                ("dinner (tandoor)", "691"))
check("sum",            split("rickshaw - 60 + 40 + 30"),               ("rickshaw", "60 + 40 + 30"))
check("foreign",        split("hotel - 12.52 usd"),                     ("hotel", "12.52 usd"))
check("k suffix",       split("snacks - 10k idr"),                      ("snacks", "10k idr"))
check("no amount",      split("metro to uttara north"),                  ("metro to uttara north", None))
check("multi word",     split("bike to office - 150"),                   ("bike to office", "150"))
check("multiply",       split("entry fee - 2*500"),                      ("entry fee", "2*500"))
check("tips",           split("breakfast - 220 + 20 tk tips."),          ("breakfast", "220 + 20 tk tips."))


# =============================================================================
# 10. FULL LINE PARSING
# =============================================================================
section("FULL LINE PARSING")

def line(raw, city="dhaka", trip=None):
    return parse_line(raw, 1, city, trip, DB, date(2026, 4, 17))

# Basic expense
r = line("fuchka - 120")
check("fuchka purpose",   r.purpose_slug,   "food_bill")
check("fuchka amount",    r.amount_bdt,     120)
check("fuchka payment",   r.payment_slug,   "cash")
check("fuchka type",      r.tx_type,        "expense")

# Rickshaw sum
r = line("rickshaw - 60 + 40 + 30")
check("rickshaw purpose", r.purpose_slug,   "commuting")
check("rickshaw amount",  r.amount_bdt,     130)
check("rickshaw payment", r.payment_slug,   "cash")

# EBL card
r = line("dinner (thai express) - 2105 (ebl)")
check("dinner amount",    r.amount_bdt,     2105)
check("dinner payment",   r.payment_slug,   "ebl_card")

# Uber transport
r = line("bike to office - 150 (uber/cash)")
check("uber purpose",     r.purpose_slug,   "commuting")
check("uber service",     r.transport_service, "uber")
check("uber payment",     r.payment_slug,   "cash")

# Uber with bkash
r = line("bike to office - 143 (uber/manual bkash)")
check("uber/bkash pay",   r.payment_slug,   "bkash")
check("uber/bkash svc",   r.transport_service, "uber")

# Transfer
r = line("ebl to bkash - 2000")
check("transfer type",    r.tx_type,        "transfer")
check("transfer from",    r.transfer_from,  "ebl")
check("transfer to",      r.transfer_to,    "bkash")

# Cashout
r = line("EBL cashout - 6500")
check("cashout type",     r.tx_type,        "transfer")
check("cashout from",     r.transfer_from,  "ebl")
check("cashout to",       r.transfer_to,    "cash")

# Third-party paid
r = line("fuchka - 100 (friend_xyz paid)")
check("friend_xyz paid amt",    r.amount_bdt,     0)
check("friend_xyz paid by",     r.paid_by,        "Friend_xyz")
check("friend_xyz bill",        r.bill_amount_bdt, 100)

# Cashback
r = line("Breakfast - 452 (foodi/bkash) - 45 tk cashback")
check("cashback net",     r.amount_bdt,     407)
check("cashback stored",  r.cashback_bdt,   45)
check("cashback pay",     r.payment_slug,   "bkash")

# #home override
r = line("rent dhaka - 25000 #home @dhaka", city="bali", trip=99)
check("#home override",   r.is_home_override, True)
check("#home city",       r.city_slug,      "dhaka")

# #treat override
r = line("sate ratu dinner - 212k idr (ebl) #treat", city="bali")
check("#treat purpose",   r.purpose_slug,   "treat")
check("#treat override",  r.purpose_override, True)
check("#treat currency",  r.original_currency, "IDR")
check("#treat orig amt",  r.original_amount,  212000.0)

# [Friend_xyz] prefix
r = line("[Friend_xyz] bike to destination - 150 (uber)")
check("[Friend_xyz] paid_for",  r.paid_for,       "Friend_xyz")
check("[Friend_xyz] amount",    r.amount_bdt,     150)
check("[Friend_xyz] purpose",   r.purpose_slug,   "commuting")

# Metro card
r = line("metro to uttara north - metro card")
check("metro payment",    r.payment_slug,   "metro_card")
check("metro purpose",    r.purpose_slug,   "commuting")
check("metro amount",     r.amount_bdt,     0)

# Accommodation
r = line("Rent (H10-309) for April - 14000 (scb)")
check("rent purpose",     r.purpose_slug,   "accommodation")
check("rent payment",     r.payment_slug,   "scb_card")
check("rent amount",      r.amount_bdt,     14000)

# Foodi/bkash
r = line("dinner (domino's) - 545 (foodi/bkash)")
check("foodi platform",   r.platform,       "foodi")
check("foodi payment",    r.payment_slug,   "bkash")


# =============================================================================
# 11. FULL PASTE PARSING
# =============================================================================
section("FULL PASTE PARSING")

PASTE_1 = """
17 April
bike to office - 150 (uber/cash)
rickshaw - 60 + 40 + 30
park ticket - 40 (ebl)
Fuchka - 120
metro to uttara north - metro card
ebl to bkash - 2000
badam - 10
cockroach gel - 140 (ebl)
guava - 100
Watermelon - 210
"""

result = parse_paste(PASTE_1, DB)
check("paste date",         result.entry_date,          date(today_year, 4, 17))
check("paste city default", result.default_city,        "dhaka")
check("paste line count",   len(result.lines),          10)
check("paste transfer",     result.lines[5].tx_type,    "transfer")
check("paste sum amount",   result.lines[1].amount_bdt, 130)

# Multi-date paste
PASTE_2 = """
17 April
rickshaw - 80
lunch - 350

18 April
fuchka - 120
tea - 30
"""
result2 = parse_paste(PASTE_2, DB)
check("multi-date count",       len(result2.lines),                     4)
check("multi-date first date",  result2.lines[0].entry_date.day,        17)
check("multi-date second date", result2.lines[2].entry_date.day,        18)

# Trip paste with @city header and #home
PASTE_3 = """
17 March
@Bali
hotel pondok ijo - 12.52 usd (ebl)
bike to melisa home - 40000 idr (gojek)
snacks - 10000 idr
sate ratu dinner - 212000 idr (ebl) #treat
rent dhaka - 25000 #home @dhaka
"""
result3 = parse_paste(PASTE_3, DB, active_trip_id=1)
check("trip city default",      result3.default_city,                   "bali")
check("trip line count",        len(result3.lines),                     5)
check("trip hotel currency",    result3.lines[0].original_currency,     "USD")
check("trip home override",     result3.lines[4].is_home_override,      True)
check("trip home city",         result3.lines[4].city_slug,             "dhaka")
check("trip treat override",    result3.lines[3].purpose_override,      True)


# =============================================================================
# 12. EXCHANGE RATE APPLICATION
# =============================================================================
section("EXCHANGE RATES")

PASTE_FX = """
17 April
@Bali
hotel - 12.52 usd (ebl)
snacks - 10k idr
"""
result_fx = parse_paste(PASTE_FX, DB)
warnings = apply_exchange_rates(result_fx, DB)
check("usd converted",      result_fx.lines[0].amount_bdt,     round(12.52 * 122.5))
check("idr converted",      result_fx.lines[1].amount_bdt,     round(10000 * 0.0074))
check("no fx warnings",     len(warnings),                      0)

PASTE_FX_MISSING = """17 April\nitem - 100 sgd (ebl)"""
result_miss = parse_paste(PASTE_FX_MISSING, DB)
# Remove SGD rate if it exists to simulate missing rate
DB.execute("DELETE FROM exchange_rates WHERE currency_code='SGD'")
warnings_miss = apply_exchange_rates(result_miss, DB)
check("missing rate flagged",   result_miss.lines[0].needs_review,  True)
check("missing rate warning",   len(warnings_miss) > 0,             True)


# =============================================================================
# RESULTS
# =============================================================================
print(f"\n{'='*60}")
print(f"  RESULTS: {passed} passed, {failed} failed")
print(f"{'='*60}")

if failed == 0:
    print("  🎉 All tests passed!")
else:
    print(f"  ⚠️  {failed} test(s) failed — check above for details")


# =============================================================================
# 13. SAMPLE REVIEW SUMMARY
# =============================================================================
section("SAMPLE REVIEW SUMMARY (visual check)")

PASTE_DEMO = """
17 April
bike to office - 150 (uber/cash)
rickshaw - 60 + 40 + 30
park ticket - 40 (ebl)
Fuchka - 120
ebl to bkash - 2000
cockroach gel - 140 (ebl)
guava - 100
Breakfast - 452 (foodi/bkash) - 45 tk cashback
fuchka - 100 (friend_xyz paid)
dinner - 2400 (ebl)
"""
demo_result = parse_paste(PASTE_DEMO, DB)
apply_exchange_rates(demo_result, DB)
print()
print(format_review_summary(demo_result))
