# =============================================================================
# FINTRACKER — Bot Integration Tests
# Tests commit flow, session management, and commands WITHOUT Slack
# =============================================================================

import sys
import sqlite3
from datetime import date, datetime
from unittest.mock import MagicMock

sys.path.insert(0, '/home/claude/fintracker')

# Patch Slack imports so we can test without credentials
sys.modules['slack_bolt'] = MagicMock()
sys.modules['slack_bolt.adapter.socket_mode'] = MagicMock()

from parser import parse_paste, apply_exchange_rates, ParsedPaste
from bot import (
    get_db, init_db, ensure_city, get_home_city_id,
    get_active_trip, commit_line, commit_session,
    apply_correction, get_session, set_session, clear_session,
    _sessions
)

# =============================================================================
# TEST DB
# =============================================================================

def make_test_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open('schema_v3_final.sql', encoding='utf-8') as f:
        conn.executescript(f.read())
    # Seed exchange rates
    conn.execute("INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date) VALUES ('USD', 122.5, '2026-01-01')")
    conn.execute("INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date) VALUES ('IDR', 0.0074, '2026-01-01')")
    conn.execute("INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date) VALUES ('AUD', 80.0, '2026-01-01')")
    conn.commit()
    return conn

# =============================================================================
# HELPERS
# =============================================================================

passed = failed = 0

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

def make_session(conn, paste_text, trip_id=None):
    """Parse a paste and create a session, returns (parsed, db_session_id)"""
    parsed   = parse_paste(paste_text, conn, active_trip_id=trip_id)
    apply_exchange_rates(parsed, conn)

    conn.execute("""
        INSERT INTO slack_sessions
            (slack_user_id, slack_channel, raw_message, entry_date,
             default_city_id, trip_id, status)
        VALUES ('U_TEST', 'C_TEST', ?, ?, 1, ?, 'pending')
    """, (paste_text, parsed.entry_date.isoformat(), trip_id))
    db_session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for pl in parsed.lines:
        conn.execute("""
            INSERT INTO pending_transactions
                (session_id, line_number, raw_line, parsed_type,
                 parsed_purpose_slug, parsed_amount_bdt, parsed_payment_slug,
                 parsed_is_home, match_source, confidence, needs_review)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            db_session_id, pl.line_number, pl.raw_line, pl.tx_type,
            pl.purpose_slug, pl.amount_bdt, pl.payment_slug,
            1 if pl.is_home_override else 0,
            str(pl.match_source), pl.confidence, 1 if pl.needs_review else 0
        ))
    conn.commit()

    return parsed, db_session_id


# =============================================================================
# 1. CITY AUTO-CREATE
# =============================================================================
section("CITY AUTO-CREATE")

conn = make_test_db()

city_id = ensure_city("dhaka", conn)
check("dhaka exists",       city_id > 0,            True)

city_id2 = ensure_city("dhaka", conn)
check("dhaka idempotent",   city_id,                city_id2)

yoga_id = ensure_city("yogyakarta", conn)
check("yogyakarta created", yoga_id > 0,            True)
row = conn.execute("SELECT name, slug FROM cities WHERE slug='yogyakarta'").fetchone()
check("yogyakarta name",    row["name"],            "Yogyakarta")
check("yogyakarta slug",    row["slug"],            "yogyakarta")

conn.close()


# =============================================================================
# 2. COMMIT SINGLE LINE
# =============================================================================
section("COMMIT SINGLE LINE")

conn = make_test_db()

PASTE = """
17 April
fuchka - 120
rickshaw - 60 + 40 + 30
ebl to bkash - 2000
hotel room - 12.52 usd (ebl)
rent - 25000 (scb)
"""

parsed, db_session_id = make_session(conn, PASTE)
session = {"parsed": parsed, "db_session_id": db_session_id}

# Commit fuchka line
pl_fuchka = parsed.lines[0]
tx_id = commit_line(pl_fuchka, session, conn, db_session_id)
check("fuchka tx_id",       tx_id is not None,      True)

row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
check("fuchka type",        row["type"],            "expense")
check("fuchka amount",      row["estimated_amount_bdt"], 120)
check("fuchka source",      row["source"],          "slack_bot")
check("fuchka raw",         row["raw_text"],        "fuchka - 120")

# Check purpose
purpose = conn.execute(
    "SELECT slug FROM purpose_taxonomy WHERE id=?", (row["purpose_id"],)
).fetchone()
check("fuchka purpose",     purpose["slug"],        "food_bill")

# Check payment
payment = conn.execute(
    "SELECT slug FROM payment_method WHERE id=?", (row["payment_method_id"],)
).fetchone()
check("fuchka payment",     payment["slug"],        "cash")

conn.close()


# =============================================================================
# 3. COMMIT TRANSFER LINE
# =============================================================================
section("COMMIT TRANSFER LINE")

conn = make_test_db()
parsed2, db_session_id2 = make_session(conn, PASTE)
session2 = {"parsed": parsed2, "db_session_id": db_session_id2}

pl_transfer = parsed2.lines[2]  # "ebl to bkash - 2000"
check("transfer type",      pl_transfer.tx_type,    "transfer")
check("transfer from",      pl_transfer.transfer_from, "ebl")
check("transfer to",        pl_transfer.transfer_to,   "bkash")

tx_id2 = commit_line(pl_transfer, session2, conn, db_session_id2)
check("transfer committed", tx_id2 is not None,     True)

# Check transfer_details row
td = conn.execute(
    "SELECT * FROM transfer_details WHERE transaction_id=?", (tx_id2,)
).fetchone()
check("transfer_details exists", td is not None,    True)

from_acc = conn.execute(
    "SELECT slug FROM accounts WHERE id=?", (td["from_account_id"],)
).fetchone()
to_acc = conn.execute(
    "SELECT slug FROM accounts WHERE id=?", (td["to_account_id"],)
).fetchone()
check("transfer from slug",  from_acc["slug"],      "ebl")
check("transfer to slug",    to_acc["slug"],        "bkash")

conn.close()


# =============================================================================
# 4. COMMIT FOREIGN CURRENCY LINE
# =============================================================================
section("COMMIT FOREIGN CURRENCY")

conn = make_test_db()
parsed3, db_session_id3 = make_session(conn, PASTE)
session3 = {"parsed": parsed3, "db_session_id": db_session_id3}

pl_hotel = parsed3.lines[3]  # "hotel room - 12.52 usd (ebl)"
check("hotel currency",     pl_hotel.original_currency, "USD")
check("hotel orig amount",  pl_hotel.original_amount,   12.52)
expected_bdt = round(12.52 * 122.5)
check("hotel bdt",          pl_hotel.amount_bdt,        expected_bdt)

tx_id3 = commit_line(pl_hotel, session3, conn, db_session_id3)
row3 = conn.execute(
    "SELECT * FROM transactions WHERE id=?", (tx_id3,)
).fetchone()
check("hotel orig in db",   row3["original_amount"],    12.52)
check("hotel curr in db",   row3["original_currency"],  "USD")
check("hotel rate in db",   row3["exchange_rate_used"], 122.5)
check("hotel bdt in db",    row3["estimated_amount_bdt"], expected_bdt)

conn.close()


# =============================================================================
# 5. COMMIT FULL SESSION
# =============================================================================
section("COMMIT FULL SESSION")

conn = make_test_db()
PASTE_FULL = """
17 April
fuchka - 120
rickshaw - 60 + 40 + 30
park ticket - 40 (ebl)
ebl to bkash - 2000
guava - 100
Breakfast - 452 (foodi/bkash) - 45 tk cashback
fuchka - 100 (friend_xyz paid)
"""

parsed_f, db_sess_f = make_session(conn, PASTE_FULL)
set_session("U_TEST", {
    "parsed":        parsed_f,
    "db_session_id": db_sess_f,
    "channel":       "C_TEST",
})

saved, skipped, saved_ids = commit_session("U_TEST", conn)
check("all saved",          saved,                  7)
check("none skipped",       skipped,                0)
check("session cleared",    get_session("U_TEST"),  None)

# Verify session marked committed
sess_row = conn.execute(
    "SELECT status FROM slack_sessions WHERE id=?", (db_sess_f,)
).fetchone()
check("session committed",  sess_row["status"],     "committed")

# Verify cashback transaction
cashback_tx = conn.execute("""
    SELECT t.estimated_amount_bdt, t.raw_text
    FROM transactions t
    WHERE t.raw_text LIKE '%cashback%'
""").fetchone()
check("cashback amount",    cashback_tx["estimated_amount_bdt"], 407)

# Verify third-party paid
friend_tx = conn.execute("""
    SELECT estimated_amount_bdt, details
    FROM transactions
    WHERE raw_text LIKE '%friend_xyz paid%'
""").fetchone()
check("friend_xyz paid amount",   friend_tx["estimated_amount_bdt"],     0)
check("friend_xyz paid details",  "Friend_xyz" in (friend_tx["details"] or ""), True)

conn.close()


# =============================================================================
# 6. TRIP ASSIGNMENT
# =============================================================================
section("TRIP ASSIGNMENT")

conn = make_test_db()

# Insert a trip
conn.execute("""
    INSERT INTO trips (name, destination, started_at)
    VALUES ('Indonesia March 2026', 'Indonesia', '2026-03-15')
""")
trip_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.commit()

PASTE_TRIP = """
17 March
@Bali
hotel pondok ijo - 12.52 usd (ebl)
bike to melisa home - 40000 idr (gojek)
snacks - 10000 idr
sate ratu dinner - 212000 idr (ebl) #treat
rent dhaka - 25000 #home @dhaka
"""

parsed_t, db_sess_t = make_session(conn, PASTE_TRIP, trip_id=trip_id)
set_session("U_TRIP", {
    "parsed":        parsed_t,
    "db_session_id": db_sess_t,
    "channel":       "C_TEST",
})
_, _, _ = commit_session("U_TRIP", conn)

# Check trip assignments
txs = conn.execute("""
    SELECT t.raw_text, t.trip_id, t.is_home_during_trip,
           c.slug as city_slug
    FROM transactions t
    JOIN cities c ON t.city_id = c.id
    ORDER BY t.id
""").fetchall()

check("trip lines count",       len(txs),               5)
check("hotel trip_id",          txs[0]["trip_id"],      trip_id)
check("hotel city",             txs[0]["city_slug"],    "bali")
check("bike trip_id",           txs[1]["trip_id"],      trip_id)
check("snacks trip_id",         txs[2]["trip_id"],      trip_id)
check("treat trip_id",          txs[3]["trip_id"],      trip_id)
check("rent no trip",           txs[4]["trip_id"],      None)
check("rent home_override",     txs[4]["is_home_during_trip"], 1)
check("rent city dhaka",        txs[4]["city_slug"],    "dhaka")

conn.close()


# =============================================================================
# 7. CORRECTION HANDLER
# =============================================================================
section("CORRECTION HANDLER")

conn = make_test_db()
PASTE_CORR = """17 April\ndinner - 2400 (ebl)\nshopping - 3500 (ebl)\n"""
parsed_c, _ = make_session(conn, PASTE_CORR)

pl1 = parsed_c.lines[0]  # dinner
pl2 = parsed_c.lines[1]  # shopping

# Correct purpose
result = apply_correction(pl1, "treat", conn)
check("correction treat",       pl1.purpose_slug,   "treat")
check("correction override",    pl1.purpose_override, True)
check("correction user flag",   pl1.user_corrected,  True)
check("correction no review",   pl1.needs_review,    False)
check("correction result msg",  "treat" in result,   True)

# Correct payment
result2 = apply_correction(pl1, "ebl_card", conn)
check("correction payment",     pl1.payment_slug,   "ebl_card")

# Correct amount
result3 = apply_correction(pl2, "3200", conn)
check("correction amount",      pl2.amount_bdt,     3200)

# Correct type
result4 = apply_correction(pl2, "transfer", conn)
check("correction type",        pl2.tx_type,        "transfer")

conn.close()


# =============================================================================
# 8. CLASSIFIER EXAMPLES SAVED
# =============================================================================
section("CLASSIFIER EXAMPLES")

conn = make_test_db()
PASTE_EX = """17 April\nfuchka - 120\ndinner - 2400 (ebl)\n"""
parsed_e, db_sess_e = make_session(conn, PASTE_EX)

# Manually set a correction on dinner
pl_dinner = parsed_e.lines[1]
apply_correction(pl_dinner, "treat", conn)

set_session("U_EX", {
    "parsed":        parsed_e,
    "db_session_id": db_sess_e,
    "channel":       "C_TEST",
})
_, _, _ = commit_session("U_EX", conn)

examples = conn.execute(
    "SELECT * FROM classifier_examples ORDER BY id"
).fetchall()

check("examples saved",     len(examples) >= 2,     True)

# Find the corrected one — should have weight 2.0
dinner_ex = next(
    (e for e in examples if "dinner" in e["raw_text"]), None
)
if dinner_ex:
    check("corrected weight",   dinner_ex["weight"],    2.0)
    check("corrected source",   dinner_ex["source"],    "user_corrected")
    check("corrected purpose",  dinner_ex["purpose_slug"], "treat")

conn.close()


# =============================================================================
# 9. ACTUAL AMOUNT UPDATE
# =============================================================================
section("ACTUAL AMOUNT UPDATE")

conn = make_test_db()
PASTE_ACT = """17 April\nhotel - 12.52 usd (ebl)\n"""
parsed_a, db_sess_a = make_session(conn, PASTE_ACT)
set_session("U_ACT", {
    "parsed": parsed_a, "db_session_id": db_sess_a, "channel": "C_TEST"
})
_, _, _ = commit_session("U_ACT", conn)

tx = conn.execute(
    "SELECT id, estimated_amount_bdt, actual_amount_bdt FROM transactions LIMIT 1"
).fetchone()
check("no actual yet",      tx["actual_amount_bdt"],    None)

# Simulate /actual command
conn.execute(
    "UPDATE transactions SET actual_amount_bdt = ? WHERE id = ?",
    (1550, tx["id"])
)
conn.commit()

tx2 = conn.execute(
    "SELECT estimated_amount_bdt, actual_amount_bdt FROM transactions WHERE id=?",
    (tx["id"],)
).fetchone()
check("estimated preserved", tx2["estimated_amount_bdt"], round(12.52 * 122.5))
check("actual saved",        tx2["actual_amount_bdt"],    1550)

# Verify v_transactions uses actual
vt = conn.execute(
    "SELECT amount_bdt, estimated_amount_bdt, actual_amount_bdt, exchange_diff_bdt "
    "FROM v_transactions WHERE id=?", (tx["id"],)
).fetchone()
check("view uses actual",    vt["amount_bdt"],            1550)
check("view diff",           vt["exchange_diff_bdt"],     1550 - round(12.52 * 122.5))

conn.close()


# =============================================================================
# 10. MONTHLY PROJECTION VIEW
# =============================================================================
section("MONTHLY PROJECTION VIEW")

conn = make_test_db()
today = date.today()

# Insert some transactions for current month
for amount in [500, 300, 200, 400]:
    conn.execute("""
        INSERT INTO transactions
            (type, city_id, estimated_amount_bdt, transacted_at, source)
        VALUES ('expense', 1, ?, date('now'), 'test')
    """, (amount,))
conn.commit()

proj = conn.execute("SELECT * FROM v_monthly_projection").fetchone()
check("days elapsed > 0",       proj["days_elapsed"] > 0,    True)
check("days in month",          proj["days_in_month"] >= 28, True)
check("month total",            proj["month_total_bdt"],      1400)
check("daily avg correct",
      proj["daily_avg_bdt"] == round(1400 / proj["days_elapsed"]), True)
check("projected exists",       proj["projected_month_bdt"] is not None, True)

conn.close()


# =============================================================================
# RESULTS
# =============================================================================
print(f"\n{'='*60}")
print(f"  RESULTS: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("  🎉 All tests passed!")
else:
    print(f"  ⚠️  {failed} test(s) failed")
