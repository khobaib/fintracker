# =============================================================================
# FINTRACKER — Slack Bot
# Receives daily expense pastes, runs the parser, manages review flow
# =============================================================================
#
# FLOW:
#   1. You paste your daily entries in Slack
#   2. Bot parses them, shows review summary
#   3. You correct any lines by number, or say "save all"
#   4. Bot commits to DB, shows confirmation
#
# SLASH COMMANDS:
#   /trip start "Indonesia March 2026"   — open a trip session
#   /trip end                            — close active trip
#   /trip status                         — show active trip
#   /trip list                           — list all trips
#   /rate usd 122.5                      — set exchange rate
#   /rates                               — show all current rates
#   /summary                             — current month projection
#   /summary april                       — monthly summary
#   /actual <tx_id> <amount>             — set bank-confirmed amount
#
# REVIEW FLOW COMMANDS (during active session):
#   save all / ok / confirm              — commit all lines
#   <number> <correction>               — correct a specific line
#   cancel / abort                       — discard session
#
# =============================================================================

import os
from dotenv import load_dotenv
load_dotenv()  # loads .env file into environment variables
import re
import json
import sqlite3
import logging
from datetime import date, datetime
from typing import Optional
import threading

# Google Sheets (optional — only if package is installed)
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gapi_build
    GSHEETS_AVAILABLE = True
except ImportError:
    GSHEETS_AVAILABLE = False

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from parser import (
    parse_paste, apply_exchange_rates, format_review_summary,
    ParsedLine, ParsedPaste, MatchSource, to_slug, get_exchange_rate
)

# =============================================================================
# CONFIG
# =============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "xoxb-test")
SLACK_APP_TOKEN   = os.environ.get("SLACK_APP_TOKEN", "xapp-test")  # for Socket Mode
DB_PATH           = os.environ.get("DB_PATH", "fintracker.db")

# Channel where you post expenses (set to your actual channel name)
EXPENSE_CHANNEL   = os.environ.get("EXPENSE_CHANNEL", "expenses")

app = App(token=SLACK_BOT_TOKEN)

# =============================================================================
# DATABASE
# =============================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize DB from schema if not already done."""
    conn = get_db()
    # Check if tables exist
    tables = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    if tables < 5:
        with open("schema_v3_final.sql", encoding='utf-8') as f:
            conn.executescript(f.read())
        logger.info("Database initialized from schema_v3.sql")
    conn.close()

# =============================================================================
# =============================================================================
# GOOGLE SHEETS INTEGRATION
# =============================================================================

SHEET_HEADERS = [
    "ID", "Date", "Created At", "Type", "Purpose",
    "Amount BDT", "Estimated BDT", "Actual BDT",
    "Original Amount", "Currency", "Exchange Rate",
    "Payment Method", "City", "Trip", "Is Travel",
    "Source", "Raw Text", "Details"
]
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheets_service():
    """Return authenticated Google Sheets API service."""
    if not GSHEETS_AVAILABLE:
        raise RuntimeError("google-api-python-client not installed. Run: pip install google-api-python-client google-auth")
    creds_raw = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_raw:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS env var not set")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_raw), scopes=SHEET_SCOPES
    )
    return gapi_build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_or_create_tab(service, spreadsheet_id: str, tab_name: str):
    """Get or create a named sheet tab, write headers if new."""
    meta     = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    if tab_name not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]}
        ).execute()
    return tab_name


def tx_to_row(tx) -> list:
    """Convert a v_transactions row to a Sheets row."""
    return [
        tx["id"],
        str(tx["expense_date"] or tx["transacted_at"] or ""),
        str(tx["entry_date"] or tx["created_at"] or ""),
        str(tx["type"] or ""),
        str(tx["purpose_slug"] or tx["purpose"] or ""),
        int(tx["amount_bdt"] or 0),
        int(tx["estimated_amount_bdt"] or 0),
        int(tx["actual_amount_bdt"]) if tx["actual_amount_bdt"] else "",
        float(tx["original_amount"]) if tx["original_amount"] else "",
        str(tx["original_currency"] or "BDT"),
        float(tx["exchange_rate_used"]) if tx["exchange_rate_used"] else "",
        str(tx["payment_method"] or "cash"),
        str(tx["city_slug"] or tx["city"] or "dhaka"),
        str(tx["trip_name"] or ""),
        1 if tx["trip_id"] else 0,
        str(tx["source"] or "slack_bot"),
        str(tx["raw_text"] or ""),
        str(tx["details"] or ""),
    ]


def sync_to_sheets(tx_ids: list, conn: sqlite3.Connection):
    """Append transactions to Google Sheets. Silent on failure — never blocks the bot."""
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not spreadsheet_id or not GSHEETS_AVAILABLE:
        return
    try:
        service = get_sheets_service()
        tab     = get_or_create_tab(service, spreadsheet_id, "Transactions")
        txs     = conn.execute(f"""
            SELECT * FROM v_transactions
            WHERE id IN ({",".join("?" * len(tx_ids))})
            ORDER BY id
        """, tx_ids).fetchall()
        if not txs:
            return
        rows = [tx_to_row(tx) for tx in txs]
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows}
        ).execute()
        logger.info(f"Synced {len(rows)} transactions to Google Sheets")
    except Exception as e:
        import traceback
        logger.error(f"Google Sheets sync failed: {e}")
        logger.error(traceback.format_exc())


# SESSION STORE
# In-memory store for active review sessions.
# Key: slack_user_id  Value: {session_id, parsed_paste, pending corrections}
# =============================================================================

_sessions: dict[str, dict] = {}

def get_session(user_id: str) -> Optional[dict]:
    return _sessions.get(user_id)

def set_session(user_id: str, session: dict):
    _sessions[user_id] = session

def clear_session(user_id: str):
    _sessions.pop(user_id, None)

# =============================================================================
# ACTIVE TRIP HELPER
# =============================================================================

def get_active_trip(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM v_active_trip"
    ).fetchone()

# =============================================================================
# CITY AUTO-CREATE
# =============================================================================

def ensure_city(slug: str, conn: sqlite3.Connection) -> int:
    """
    Get or create a city by slug.
    For unknown cities, creates with country='Unknown' — user updates later.
    Returns city_id.
    """
    row = conn.execute(
        "SELECT id FROM cities WHERE slug = ?", (slug,)
    ).fetchone()
    if row:
        return row["id"]

    # Auto-create — try to infer country from active trip
    trip = get_active_trip(conn)
    country = trip["destination"] if trip else "Unknown"

    # Display name: capitalize and replace underscores
    name = slug.replace("_", " ").title()

    conn.execute(
        "INSERT INTO cities (name, slug, country) VALUES (?, ?, ?)",
        (name, slug, country)
    )
    conn.commit()
    logger.info(f"Auto-created city: {name} ({slug}) in {country}")
    return conn.execute(
        "SELECT id FROM cities WHERE slug = ?", (slug,)
    ).fetchone()["id"]


def get_home_city_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM cities WHERE is_home = 1 LIMIT 1"
    ).fetchone()
    return row["id"] if row else 1

# =============================================================================
# COMMIT PARSED LINE → TRANSACTION
# =============================================================================

def commit_line(
    pl:             ParsedLine,
    session:        dict,
    conn:           sqlite3.Connection,
    slack_session_id: int,
) -> Optional[int]:
    """
    Write one confirmed ParsedLine to the transactions table.
    Returns transaction_id or None if skipped.
    """
    if pl.parse_error and pl.amount_bdt is None:
        logger.warning(f"Skipping line {pl.line_number} due to parse error: {pl.parse_error}")
        return None

    entry_date      = getattr(pl, "entry_date", session["parsed"].entry_date)
    city_id         = ensure_city(pl.city_slug or session["parsed"].default_city, conn)
    active_trip     = get_active_trip(conn)
    trip_id         = None

    if not pl.is_home_override and active_trip:
        trip_id = active_trip["id"]

    # Resolve purpose_id
    purpose_id = None
    if pl.purpose_slug:
        row = conn.execute(
            "SELECT id FROM purpose_taxonomy WHERE slug = ?",
            (pl.purpose_slug,)
        ).fetchone()
        if row:
            purpose_id = row["id"]

    # Resolve payment_method_id
    payment_id = None
    if pl.payment_slug:
        row = conn.execute(
            "SELECT id FROM payment_method WHERE slug = ?",
            (pl.payment_slug,)
        ).fetchone()
        if row:
            payment_id = row["id"]

    # Build details string
    details_parts = []
    if pl.description and pl.description != pl.raw_line:
        details_parts.append(pl.description)
    if pl.paid_for:
        details_parts.append(f"paid for: {pl.paid_for}")
    if pl.paid_by:
        details_parts.append(f"{pl.paid_by} paid the bill ({pl.bill_amount_bdt or '?'} BDT)")
    if pl.cashback_bdt:
        details_parts.append(f"cashback: {pl.cashback_bdt} BDT")
    if pl.platform:
        details_parts.append(f"platform: {pl.platform}")
    details = " | ".join(details_parts) if details_parts else pl.description

    estimated_bdt = pl.amount_bdt if pl.amount_bdt is not None else 0

    # --- INSERT TRANSACTION ---
    conn.execute("""
        INSERT INTO transactions (
            type, purpose_id, city_id,
            original_amount, original_currency, exchange_rate_used,
            estimated_amount_bdt, actual_amount_bdt,
            transacted_at,
            trip_id, is_home_during_trip,
            payment_method_id, transport_service,
            raw_text, details,
            ai_suggested, ai_confidence, ai_model_version, user_corrected,
            source
        ) VALUES (
            ?, ?, ?,
            ?, ?, ?,
            ?, NULL,
            ?,
            ?, ?,
            ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            'slack_bot'
        )
    """, (
        pl.tx_type,
        purpose_id,
        city_id,
        pl.original_amount,
        pl.original_currency,
        getattr(pl, "exchange_rate_used", None),
        estimated_bdt,
        entry_date.isoformat() if isinstance(entry_date, date) else entry_date,
        trip_id,
        1 if pl.is_home_override else 0,
        payment_id,
        pl.transport_service,
        pl.raw_line,
        details,
        1 if pl.match_source == MatchSource.AI else 0,
        pl.confidence,
        "rules_v1" if pl.match_source == MatchSource.RULE else "ai_v1",
        1 if pl.user_corrected else 0,
    ))
    tx_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # --- INSERT TRANSFER DETAILS if applicable ---
    if pl.tx_type == "transfer" and pl.transfer_from and pl.transfer_to:
        from_acc = conn.execute(
            "SELECT id FROM accounts WHERE slug = ?", (pl.transfer_from,)
        ).fetchone()
        to_acc = conn.execute(
            "SELECT id FROM accounts WHERE slug = ?", (pl.transfer_to,)
        ).fetchone()
        if from_acc and to_acc:
            conn.execute("""
                INSERT INTO transfer_details (transaction_id, from_account_id, to_account_id)
                VALUES (?, ?, ?)
            """, (tx_id, from_acc["id"], to_acc["id"]))

    # --- UPDATE PENDING TRANSACTION ---
    conn.execute("""
        UPDATE pending_transactions
        SET transaction_id = ?, user_action = 'confirmed'
        WHERE session_id = ? AND line_number = ?
    """, (tx_id, slack_session_id, pl.line_number))

    # --- SAVE CLASSIFIER EXAMPLE ---
    if pl.purpose_slug and pl.raw_line:
        source = "user_corrected" if pl.user_corrected else "user_confirmed"
        weight = 2.0 if pl.user_corrected else 1.0
        conn.execute("""
            INSERT INTO classifier_examples
                (raw_text, purpose_slug, payment_slug, tx_type, source, weight)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            pl.raw_line, pl.purpose_slug, pl.payment_slug,
            pl.tx_type or "expense", source, weight
        ))

    return tx_id


def commit_session(user_id: str, conn: sqlite3.Connection) -> tuple[int, int, list[int]]:
    """
    Commit all confirmed lines in the session.
    Returns (saved_count, skipped_count, saved_ids).
    """
    session = get_session(user_id)
    if not session:
        return 0, 0, []

    parsed:  ParsedPaste   = session["parsed"]
    db_session_id: int     = session["db_session_id"]

    saved = skipped = 0
    saved_ids = []
    for pl in parsed.lines:
        tx_id = commit_line(pl, session, conn, db_session_id)
        if tx_id:
            saved += 1
            saved_ids.append(tx_id)
        else:
            skipped += 1

    # Mark session as committed
    conn.execute("""
        UPDATE slack_sessions
        SET status = 'committed', committed_at = datetime('now')
        WHERE id = ?
    """, (db_session_id,))
    conn.commit()

    clear_session(user_id)
    return saved, skipped, saved_ids

# =============================================================================
# CORRECTION HANDLER
# User says "3 treat" or "3 food bill" or "3 cash" to correct line 3
# =============================================================================

def apply_correction(pl: ParsedLine, correction_text: str, conn: sqlite3.Connection) -> str:
    """
    Apply a user correction to a ParsedLine.
    Returns a human-readable description of what changed.
    """
    text = correction_text.strip().lower()
    changes = []

    # Check if it's a purpose slug
    purpose_row = conn.execute(
        "SELECT slug, name FROM purpose_taxonomy WHERE slug = ? OR lower(name) = ?",
        (text, text)
    ).fetchone()
    if purpose_row:
        old = pl.purpose_slug
        pl.purpose_slug     = purpose_row["slug"]
        pl.purpose_override = True
        pl.user_corrected   = True
        pl.needs_review     = False
        pl.review_reason    = None   # clear stale reason
        changes.append(f"purpose: {old} → {purpose_row['slug']}")
        return ", ".join(changes)

    # Check if it's a payment method
    pay_row = conn.execute(
        "SELECT slug, name FROM payment_method WHERE slug = ? OR lower(name) = ?",
        (text, text)
    ).fetchone()
    if pay_row:
        old = pl.payment_slug
        pl.payment_slug   = pay_row["slug"]
        pl.user_corrected = True
        pl.needs_review   = False
        pl.review_reason  = None
        changes.append(f"payment: {old} → {pay_row['slug']}")
        return ", ".join(changes)

    # Check if it's a tx_type
    if text in ("expense", "transfer", "investment"):
        old = pl.tx_type
        pl.tx_type        = text
        pl.user_corrected = True
        pl.needs_review   = False
        changes.append(f"type: {old} → {text}")
        return ", ".join(changes)

    # Check if it's an amount
    try:
        from parser import parse_amount_expression
        amt, curr = parse_amount_expression(text)
        if amt is not None:
            old = pl.amount_bdt
            if curr:
                pl.original_amount   = amt
                pl.original_currency = curr
                rate = get_exchange_rate(curr, date.today(), conn)
                if rate:
                    pl.amount_bdt          = round(amt * rate)
                    pl.exchange_rate_used  = rate
                else:
                    pl.amount_bdt = None
            else:
                pl.amount_bdt = round(amt)
            pl.user_corrected = True
            pl.needs_review   = False
            changes.append(f"amount: {old} → {pl.amount_bdt}")
            return ", ".join(changes)
    except Exception:
        pass

    return f"Could not understand correction: '{correction_text}'. Try a purpose name, payment method, or amount."

# =============================================================================
# MESSAGE HANDLER — main expense paste
# =============================================================================

@app.message()
def handle_message(message, say, client):
    """
    Handles all messages in the expense channel.
    If user has an active session, routes to correction handler.
    Otherwise, tries to parse as expense paste.
    """
    user_id  = message["user"]
    channel  = message["channel"]
    text     = message.get("text", "").strip()

    if not text:
        return

    conn = get_db()

    try:
        # --- BULK ACTUAL SESSION: confirm/cancel for /actual ID/date range ---
        bulk_key = f"bulk_{user_id}"
        bulk_session = _sessions.get(bulk_key)
        if bulk_session and bulk_session.get("type") == "bulk_actual":
            _handle_bulk_actual(user_id, text, bulk_key, bulk_session, say, conn)
            return

        # --- ACTIVE SESSION: route to correction or save ---
        session = get_session(user_id)
        if session:
            _handle_session_input(user_id, text, say, conn)
            return

        # --- NEW PASTE: parse it ---
        # Require at least 2 lines to treat as an expense paste
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if len(lines) < 2:
            return  # single-line messages ignored (could be reactions, etc.)

        active_trip = get_active_trip(conn)
        trip_id     = active_trip["id"] if active_trip else None

        # Parse
        parsed   = parse_paste(text, conn, active_trip_id=trip_id)
        warnings = apply_exchange_rates(parsed, conn)

        if not parsed.lines:
            say("Couldn't find any transactions in that message. "
                "Make sure each line has a description and amount.")
            return

        # Save session to DB
        home_city_id = get_home_city_id(conn)
        default_city_row = conn.execute(
            "SELECT id FROM cities WHERE slug = ?",
            (parsed.default_city,)
        ).fetchone()
        default_city_id = default_city_row["id"] if default_city_row else home_city_id

        conn.execute("""
            INSERT INTO slack_sessions
                (slack_user_id, slack_channel, raw_message, entry_date,
                 default_city_id, trip_id, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (
            user_id, channel, text,
            parsed.entry_date.isoformat(),
            default_city_id,
            trip_id
        ))
        db_session_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        # Save pending_transactions rows
        for pl in parsed.lines:
            conn.execute("""
                INSERT INTO pending_transactions
                    (session_id, line_number, raw_line,
                     parsed_type, parsed_purpose_slug, parsed_amount_bdt,
                     parsed_original_amount, parsed_currency,
                     parsed_payment_slug, parsed_transport, parsed_city_slug,
                     parsed_is_home, match_source, confidence, needs_review)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                db_session_id,
                pl.line_number,
                pl.raw_line,
                pl.tx_type,
                pl.purpose_slug,
                pl.amount_bdt,
                pl.original_amount,
                pl.original_currency,
                pl.payment_slug,
                pl.transport_service,
                pl.city_slug,
                1 if pl.is_home_override else 0,
                pl.match_source,
                pl.confidence,
                1 if pl.needs_review else 0,
            ))
        conn.commit()

        # Store in memory
        set_session(user_id, {
            "parsed":        parsed,
            "db_session_id": db_session_id,
            "channel":       channel,
        })

        # Send review summary
        summary = format_review_summary(parsed)

        # Prepend any fx warnings
        if warnings:
            summary = "\n".join(warnings) + "\n\n" + summary

        # Add trip context to header if active
        if active_trip:
            summary = (
                f"✈️  Active trip: *{active_trip['name']}*\n\n" + summary
            )

        say(summary)

    except Exception as e:
        logger.exception("Error handling message")
        say(f"Something went wrong parsing your entries: `{e}`\nPlease try again.")
    finally:
        conn.close()


def _handle_bulk_actual(
    user_id: str, text: str, bulk_key: str,
    session: dict, say, conn: sqlite3.Connection
):
    """Handle confirm/cancel for bulk /actual ID-range or date-range updates."""
    text_low = text.strip().lower()

    if text_low in ("cancel", "abort", "no"):
        _sessions.pop(bulk_key, None)
        say("Cancelled. No changes saved.")
        return

    if text_low not in ("confirm", "yes", "ok"):
        say("Reply *confirm* to save the updates, or *cancel* to abort.")
        return

    # Confirmed — apply updates
    rate     = session["rate"]
    tx_ids   = session["tx_ids"]
    currency = session["currency"]
    updated  = 0

    for tx_id in tx_ids:
        tx = conn.execute(
            "SELECT original_amount FROM transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        if tx and tx["original_amount"]:
            actual = round(tx["original_amount"] * rate)
            conn.execute(
                "UPDATE transactions SET actual_amount_bdt = ? WHERE id = ?",
                (actual, tx_id)
            )
            updated += 1

    conn.commit()
    _sessions.pop(bulk_key, None)
    say(
        f"\u2705 Updated *{updated} transaction{'s' if updated != 1 else ''}* "
        f"with actual BDT amounts.\n"
        f"Rate used: *1 {currency} = {rate:.6f} BDT* ({session['rate_display']})"
    )


def _handle_session_input(user_id: str, text: str, say, conn: sqlite3.Connection):
    """Handle input when a review session is active."""
    session  = get_session(user_id)
    parsed:  ParsedPaste = session["parsed"]
    text_low = text.strip().lower()

    # --- SAVE ALL ---
    if text_low in ("save all", "save", "ok", "confirm", "yes", "done", "✅"):
        # Check for unresolved entries before saving
        unresolved = [
            pl for pl in parsed.lines
            if pl.tx_type == "expense" and (
                pl.purpose_slug is None or
                pl.amount_bdt is None
            )
        ]
        if unresolved:
            lines_desc = ", ".join(
                f"*{pl.line_number}* ({pl.raw_line[:30]})"
                for pl in unresolved
            )
            n = len(unresolved)
            word = "entry" if n == 1 else "entries"
            say(
                f"\u26a0\ufe0f {n} {word} still need attention: {lines_desc}\n"
                f"Fix them first, or type *save anyway* to save everything including unclassified entries."
            )
            return

        saved, skipped, saved_ids = commit_session(user_id, conn)
        msg = f"\u2705 Saved *{saved}* transaction{'s' if saved != 1 else ''}."
        if skipped:
            msg += f" Skipped {skipped} (parse errors)."
        total = sum(
            pl.amount_bdt or 0
            for pl in parsed.lines
            if pl.tx_type == "expense"
        )
        msg += f"\nTotal expenses: *{total:,} BDT*"
        if saved_ids:
            id_range = f"#{saved_ids[0]}" if len(saved_ids) == 1 else f"#{saved_ids[0]}–#{saved_ids[-1]}"
            msg += f"\nTransaction IDs: *{id_range}* — use these with `/actual` to update bank amounts"
            # Sync to Google Sheets in background (non-daemon so it completes)
            t = threading.Thread(
                target=sync_to_sheets, args=(saved_ids, get_db())
            )
            t.start()
        say(msg)
        return

    # --- SAVE ANYWAY (force save even with unresolved) ---
    if text_low == "save anyway":
        saved, skipped, saved_ids = commit_session(user_id, conn)
        msg = f"✅ Saved *{saved}* transaction{'s' if saved != 1 else ''} (including unclassified)."
        total = sum(pl.amount_bdt or 0 for pl in parsed.lines if pl.tx_type == "expense")
        msg += f"\nTotal expenses: *{total:,} BDT*"
        if saved_ids:
            id_range = f"#{saved_ids[0]}" if len(saved_ids) == 1 else f"#{saved_ids[0]}–#{saved_ids[-1]}"
            msg += f"\nTransaction IDs: *{id_range}*"
            t = threading.Thread(
                target=sync_to_sheets, args=(saved_ids, get_db())
            )
            t.start()
        say(msg)
        return


    # --- CANCEL ---
    if text_low in ("cancel", "abort", "discard", "no"):
        conn.execute("""
            UPDATE slack_sessions SET status='abandoned'
            WHERE id = ?
        """, (session["db_session_id"],))
        conn.commit()
        clear_session(user_id)
        say("Session cancelled. Nothing was saved.")
        return

    # --- SHOW REVIEW AGAIN ---
    if text_low in ("review", "show", "list", "?"):
        say(format_review_summary(parsed))
        return

    # --- LINE CORRECTION: "<number> <correction>" ---
    # e.g. "3 treat", "5 ebl_card", "8 food bill", "10 350"
    m = re.match(r'^(\d+)\s+(.+)$', text.strip())
    if m:
        line_num    = int(m.group(1))
        correction  = m.group(2).strip()

        # Find the line
        target = next(
            (pl for pl in parsed.lines if pl.line_number == line_num),
            None
        )
        if not target:
            say(f"Line {line_num} not found. Valid lines: 1–{len(parsed.lines)}")
            return

        change_desc = apply_correction(target, correction, conn)
        say(f"Updated line {line_num}: {change_desc}\n\n"
            f"Type *save all* to confirm, or make more corrections.")
        # Show updated review
        say(format_review_summary(parsed))
        return

    # --- UNKNOWN INPUT DURING SESSION ---
    say(
        "Active review session. Options:\n"
        "• *save all* — save everything\n"
        "• *<number> <correction>* — fix a line (e.g. `3 treat` or `5 ebl_card`)\n"
        "• *review* — show the summary again\n"
        "• *cancel* — discard everything"
    )

# =============================================================================
# SLASH COMMANDS
# =============================================================================

@app.command("/trip")
def handle_trip_command(ack, respond, command):
    ack()
    conn = get_db()
    try:
        text = command.get("text", "").strip()
        parts = text.split(None, 1)
        sub = parts[0].lower() if parts else ""

        # /trip start "Indonesia March 2026"
        if sub == "start":
            if len(parts) < 2:
                respond("Usage: `/trip start \"Trip Name\"`")
                return
            name = parts[1].strip().strip('"\'')
            # Close any active trip first
            conn.execute("""
                UPDATE trips SET ended_at = date('now')
                WHERE ended_at IS NULL
            """)
            # Infer destination from name (last word or last 2 words)
            words = name.split()
            destination = words[-1] if len(words) <= 2 else " ".join(words[-2:])
            conn.execute("""
                INSERT INTO trips (name, destination, started_at)
                VALUES (?, ?, date('now'))
            """, (name, destination))
            conn.commit()
            respond(f"✈️  Trip started: *{name}*\n"
                    f"All entries will be tagged to this trip until you run `/trip end`.\n"
                    f"Use `#home` on a line to override for home expenses.")

        # /trip end
        elif sub == "end":
            active = get_active_trip(conn)
            if not active:
                respond("No active trip to end.")
                return
            conn.execute("""
                UPDATE trips SET ended_at = date('now') WHERE id = ?
            """, (active["id"],))
            conn.commit()
            respond(f"🏠 Trip ended: *{active['name']}*\nBack to home mode.")

        # /trip status
        elif sub == "status":
            active = get_active_trip(conn)
            if active:
                respond(
                    f"✈️  Active trip: *{active['name']}*\n"
                    f"Destination: {active['destination']}\n"
                    f"Started: {active['started_at']}"
                )
            else:
                respond("🏠 No active trip. You're in home mode.")

        # /trip list
        elif sub == "list":
            rows = conn.execute("""
                SELECT t.name, t.destination, t.started_at, t.ended_at,
                       COUNT(tx.id) as tx_count,
                       COALESCE(SUM(COALESCE(tx.actual_amount_bdt,
                                             tx.estimated_amount_bdt)), 0) as total
                FROM trips t
                LEFT JOIN transactions tx ON tx.trip_id = t.id
                GROUP BY t.id
                ORDER BY t.started_at DESC
                LIMIT 10
            """).fetchall()
            if not rows:
                respond("No trips recorded yet.")
                return
            lines = ["*Your trips:*\n"]
            for r in rows:
                status = "✈️ active" if not r["ended_at"] else f"ended {r['ended_at']}"
                lines.append(
                    f"• *{r['name']}* ({r['destination']}) — "
                    f"{status} — {r['tx_count']} entries, "
                    f"{r['total']:,} BDT"
                )
            respond("\n".join(lines))

        else:
            respond(
                "Usage:\n"
                "`/trip start \"Name\"` — start a trip\n"
                "`/trip end` — end active trip\n"
                "`/trip status` — current trip\n"
                "`/trip list` — all trips"
            )
    finally:
        conn.close()


@app.command("/rate")
def handle_rate_command(ack, respond, command):
    """Set an exchange rate.
    Usage:
      /rate usd 122.5        → 1 USD = 122.5 BDT
      /rate idr 1/140.6      → 1 BDT = 140.6 IDR  (system stores 1/140.6 = 0.00711)
    """
    ack()
    conn = get_db()
    try:
        parts = command.get("text", "").strip().split()
        if len(parts) != 2:
            respond(
                "Usage: `/rate <currency> <rate>`\n"
                "Examples:\n"
                "• `/rate usd 122.5`  → 1 USD = 122.5 BDT\n"
                "• `/rate idr 1/140.6`  → 1 BDT = 140.6 IDR"
            )
            return

        currency = parts[0].upper()
        rate_str = parts[1].strip()

        # Support 1/X notation for currencies weaker than BDT
        # e.g. "1/140.6" means 1 BDT = 140.6 IDR → 1 IDR = 1/140.6 BDT
        if rate_str.startswith("1/"):
            try:
                divisor = float(rate_str[2:])
                if divisor == 0:
                    respond("Rate divisor cannot be zero.")
                    return
                rate = 1.0 / divisor
                rate_display = f"1/{divisor} = {rate:.6f}"
            except ValueError:
                respond(f"Invalid rate format: `{rate_str}`. Use a number or 1/X format.")
                return
        else:
            try:
                rate = float(rate_str)
                rate_display = str(rate)
            except ValueError:
                respond(f"Invalid rate: `{rate_str}`")
                return

        # Ensure currency exists
        row = conn.execute(
            "SELECT code FROM currencies WHERE code = ?", (currency,)
        ).fetchone()
        if not row:
            # Auto-add unknown currency
            conn.execute(
                "INSERT INTO currencies (code, name, symbol) VALUES (?, ?, ?)",
                (currency, currency, currency)
            )

        conn.execute("""
            INSERT INTO exchange_rates (currency_code, rate_to_bdt, effective_date, notes)
            VALUES (?, ?, date('now'), 'set via /rate command')
        """, (currency, rate))
        conn.commit()

        # Refresh any active session that has entries with this currency.
        # Recalculate ALL lines with this currency — not just unresolved ones.
        # This handles the case where a rate is updated mid-session (before save all),
        # which should apply the new rate to everything about to be saved.
        refreshed_summary = None
        recalculated = 0
        for uid, session in _sessions.items():
            parsed = session["parsed"]
            changed = False
            for pl in parsed.lines:
                if pl.original_currency == currency and pl.original_amount is not None:
                    old_bdt = pl.amount_bdt
                    pl.amount_bdt         = round(pl.original_amount * rate)
                    pl.exchange_rate_used = rate
                    pl.needs_review       = False
                    pl.review_reason      = None
                    if old_bdt != pl.amount_bdt:
                        recalculated += 1
                    changed = True
            if changed:
                refreshed_summary = format_review_summary(parsed)

        bdt_per_unit = rate
        unit_per_bdt = 1.0 / rate if rate > 0 else 0
        msg = (
            "✅ Exchange rate updated:\n"
            f"*1 {currency} = {bdt_per_unit:.4f} BDT*\n"
            f"*(1 BDT = {unit_per_bdt:.4f} {currency})*\n"
            f"Effective from today ({date.today().strftime('%d %B %Y')})"
        )
        if refreshed_summary:
            if recalculated > 0:
                msg += f"\n\n*{recalculated} entr{'y' if recalculated == 1 else 'ies'} recalculated* with the new rate — here is the revised summary:\n\n"
            else:
                msg += "\n\nSession refreshed — here is the revised summary:\n\n"
            msg += refreshed_summary
        respond(msg)
    finally:
        conn.close()


@app.command("/rates")
def handle_rates_command(ack, respond, command):
    """Show all current exchange rates."""
    ack()
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM v_current_rates ORDER BY code").fetchall()
        if not rows:
            respond("No exchange rates set yet. Use `/rate usd 122.5` to add one.")
            return
        lines = ["*Current exchange rates:*\n"]
        for r in rows:
            lines.append(
                f"• *{r['code']}* ({r['name']}) — "
                f"1 {r['code']} = *{r['rate_to_bdt']} BDT* "
                f"(set {r['effective_date']})"
            )
        lines.append("\nUpdate with `/rate <currency> <new_rate>`")
        respond("\n".join(lines))
    finally:
        conn.close()


def _parse_rate_value(rate_str: str) -> tuple[Optional[float], str]:
    """Parse a rate string — supports plain float or 1/X notation.
    Returns (rate_to_bdt, display_string) or (None, error_message).
    """
    rate_str = rate_str.strip()
    if rate_str.startswith("1/"):
        try:
            divisor = float(rate_str[2:])
            if divisor == 0:
                return None, "Rate divisor cannot be zero."
            rate = 1.0 / divisor
            return rate, f"1/{divisor} ({rate:.6f} BDT per unit)"
        except ValueError:
            return None, f"Invalid rate format: `{rate_str}`"
    else:
        try:
            return float(rate_str), rate_str
        except ValueError:
            return None, f"Invalid rate: `{rate_str}`"


@app.command("/actual")
def handle_actual_command(ack, respond, command):
    """Set bank-confirmed actual BDT amount for transactions.

    Three modes:
      /actual 42 1850                              — single transaction by ID
      /actual 84-96 usd 123.7                     — ID range, recalculate from rate
      /actual 2026-04-02 2026-04-04 usd 1/140.6  — date range, recalculate from rate
    """
    ack()
    conn = get_db()
    try:
        text  = command.get("text", "").strip()
        parts = text.split()

        usage = (
            "Usage:\n"
            "• `/actual 42 1850` — set exact BDT for transaction #42\n"
            "• `/actual 52 usd 110` — recalculate transaction #52 using USD rate 110\n"
            "• `/actual 84-96 usd 123.7` — recalculate IDs 84–96 using USD rate 123.7\n"
            "• `/actual 2026-04-02 2026-04-04 usd 1/140.6` — recalculate by date range\n"
            "Rate supports 1/X format: `1/140.6` means 1 BDT = 140.6 of that currency\n"
            "Note: `/actual` never changes the global exchange rate — use `/rate` for that."
        )

        if len(parts) < 2:
            respond(usage)
            return

        # ── Mode 1a: single ID with direct BDT amount ────────────────────────
        # /actual 42 1850
        if len(parts) == 2 and "-" not in parts[0]:
            try:
                tx_id  = int(parts[0])
                actual = round(float(parts[1]))
            except ValueError:
                respond("Invalid input. Use: `/actual <id> <amount>`")
                return

            tx = conn.execute(
                "SELECT id, estimated_amount_bdt, raw_text FROM transactions WHERE id = ?",
                (tx_id,)
            ).fetchone()
            if not tx:
                respond(f"Transaction #{tx_id} not found.")
                return

            conn.execute(
                "UPDATE transactions SET actual_amount_bdt = ? WHERE id = ?",
                (actual, tx_id)
            )
            conn.commit()
            diff = actual - tx["estimated_amount_bdt"]
            sign = "+" if diff >= 0 else ""
            respond(
                f"✅ Transaction #{tx_id} updated:\n"
                f"• _{tx['raw_text']}_\n"
                f"• Estimated: {tx['estimated_amount_bdt']:,} BDT\n"
                f"• Actual: *{actual:,} BDT*\n"
                f"• Difference: {sign}{diff:,} BDT"
            )
            return

        # ── Mode 1b: single ID with currency and rate ─────────────────────────
        # /actual 52 usd 110
        # Recalculates actual_amount_bdt for that one transaction.
        # Does NOT update the global exchange rate — use /rate for that.
        if len(parts) == 3 and "-" not in parts[0]:
            try:
                tx_id = int(parts[0])
            except ValueError:
                pass
            else:
                currency = parts[1].upper()
                rate, rate_display = _parse_rate_value(parts[2])
                if rate is None:
                    respond(rate_display)
                    return

                tx = conn.execute(
                    "SELECT id, raw_text, original_amount, estimated_amount_bdt, original_currency "
                    "FROM transactions WHERE id = ?", (tx_id,)
                ).fetchone()
                if not tx:
                    respond(f"Transaction #{tx_id} not found.")
                    return
                if tx["original_currency"] != currency:
                    respond(
                        f"Transaction #{tx_id} has currency *{tx['original_currency']}*, "
                        f"not {currency}. Check the ID or currency."
                    )
                    return
                if not tx["original_amount"]:
                    respond(f"Transaction #{tx_id} has no original foreign amount to recalculate from.")
                    return

                actual = round(tx["original_amount"] * rate)
                conn.execute(
                    "UPDATE transactions SET actual_amount_bdt = ? WHERE id = ?",
                    (actual, tx_id)
                )
                conn.commit()
                diff = actual - tx["estimated_amount_bdt"]
                sign = "+" if diff >= 0 else ""
                respond(
                    f"✅ Transaction #{tx_id} updated:\n"
                    f"• _{tx['raw_text']}_\n"
                    f"• Original: {tx['original_amount']} {currency}\n"
                    f"• Rate used: {rate_display} BDT per {currency}\n"
                    f"• Estimated was: {tx['estimated_amount_bdt']:,} BDT\n"
                    f"• Actual now: *{actual:,} BDT* ({sign}{diff:,} BDT)\n"
                    f"Note: global exchange rate unchanged — use `/rate` to update for future entries."
                )
                return

        # ── Mode 2: ID range ──────────────────────────────────────────────────
        # /actual 84-96 usd 123.7
        id_range_match = re.match(r"^(\d+)-(\d+)$", parts[0])
        if id_range_match and len(parts) == 3:
            id_from    = int(id_range_match.group(1))
            id_to      = int(id_range_match.group(2))
            currency   = parts[1].upper()
            rate, rate_display = _parse_rate_value(parts[2])
            if rate is None:
                respond(rate_display)
                return

            txs = conn.execute("""
                SELECT id, raw_text, original_amount, estimated_amount_bdt
                FROM transactions
                WHERE id BETWEEN ? AND ?
                  AND original_currency = ?
                ORDER BY id
            """, (id_from, id_to, currency)).fetchall()

            if not txs:
                respond(f"No {currency} transactions found with IDs {id_from}–{id_to}.")
                return

            # Preview
            preview = [f"Found *{len(txs)} transactions* to update (IDs {id_from}–{id_to}, {currency} @ {rate_display}):\n"]
            for tx in txs[:5]:
                actual = round(tx["original_amount"] * rate)
                preview.append(f"  #{tx['id']} {tx['raw_text'][:35]} → *{actual:,} BDT*")
            if len(txs) > 5:
                preview.append(f"  ... and {len(txs)-5} more")
            preview.append("\nReply *confirm* to save, or *cancel* to abort.")
            respond("\n".join(preview))

            # Store pending bulk update in session store
            _sessions[f"bulk_{command['user_id']}"] = {
                "type": "bulk_actual",
                "tx_ids": [tx["id"] for tx in txs],
                "rate": rate,
                "currency": currency,
                "rate_display": rate_display,
            }
            return

        # ── Mode 3: date range ────────────────────────────────────────────────
        # /actual 2026-04-02 2026-04-04 usd 1/140.6
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if len(parts) == 4 and date_re.match(parts[0]) and date_re.match(parts[1]):
            date_from  = parts[0]
            date_to    = parts[1]
            currency   = parts[2].upper()
            rate, rate_display = _parse_rate_value(parts[3])
            if rate is None:
                respond(rate_display)
                return

            txs = conn.execute("""
                SELECT id, raw_text, original_amount, estimated_amount_bdt, transacted_at
                FROM transactions
                WHERE transacted_at BETWEEN ? AND ?
                  AND original_currency = ?
                ORDER BY transacted_at, id
            """, (date_from, date_to, currency)).fetchall()

            if not txs:
                respond(f"No {currency} transactions found between {date_from} and {date_to}.")
                return

            preview = [f"Found *{len(txs)} transactions* to update ({date_from} to {date_to}, {currency} @ {rate_display}):\n"]
            for tx in txs[:5]:
                actual = round(tx["original_amount"] * rate)
                preview.append(f"  #{tx['id']} {tx['transacted_at'][:10]} {tx['raw_text'][:30]} → *{actual:,} BDT*")
            if len(txs) > 5:
                preview.append(f"  ... and {len(txs)-5} more")
            preview.append("\nReply *confirm* to save, or *cancel* to abort.")
            respond("\n".join(preview))

            _sessions[f"bulk_{command['user_id']}"] = {
                "type": "bulk_actual",
                "tx_ids": [tx["id"] for tx in txs],
                "rate": rate,
                "currency": currency,
                "rate_display": rate_display,
            }
            return

        respond(usage)

    finally:
        conn.close()




def _parse_summary_date_range(text: str) -> tuple[Optional[str], Optional[str], str]:
    """
    Parse date or date-range from summary command text.
    Returns (date_from, date_to, remaining_text) or (None, None, original_text).

    Supported formats:
      "29 april 2026"          → single day
      "29 april"               → single day (current year)
      "5-12 april 2026"        → date range within a month
      "5-12 april"             → date range (current year)
      "2026-04-05"             → ISO single date
      "2026-04-05 2026-04-12"  → ISO date range
    """
    from parser import MONTH_MAP
    today = date.today()
    t = text.strip().lower()

    # ISO date range: "2026-04-05 2026-04-12"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})(.*)", t)
    if m:
        return m.group(1), m.group(2), m.group(3).strip()

    # ISO single date: "2026-04-29"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(.*)", t)
    if m:
        return m.group(1), m.group(1), m.group(2).strip()

    # Day range + month + optional year: "5-12 april 2026" or "5-12 april"
    m = re.match(r"^(\d{1,2})-(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?(.*)", t)
    if m:
        day_from, day_to = int(m.group(1)), int(m.group(2))
        month_str = m.group(3)
        year = int(m.group(4)) if m.group(4) else today.year
        remaining = m.group(5).strip()
        if month_str in MONTH_MAP:
            month_num = MONTH_MAP[month_str]
            try:
                d_from = date(year, month_num, day_from).isoformat()
                d_to   = date(year, month_num, day_to).isoformat()
                return d_from, d_to, remaining
            except ValueError:
                pass

    # Single day + month + optional year: "29 april 2026" or "29 april"
    m = re.match(r"^(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?(.*)", t)
    if m:
        day = int(m.group(1))
        month_str = m.group(2)
        year = int(m.group(3)) if m.group(3) else today.year
        remaining = m.group(4).strip()
        if month_str in MONTH_MAP:
            month_num = MONTH_MAP[month_str]
            try:
                d = date(year, month_num, day).isoformat()
                return d, d, remaining
            except ValueError:
                pass

    return None, None, text


def _format_date_range_summary(
    date_from: str, date_to: str, segment: str, conn
) -> str:
    """Build summary output for a specific date or date range."""
    rows = conn.execute("""
        SELECT purpose, purpose_slug,
               SUM(amount_bdt)  AS total_bdt,
               COUNT(*)         AS tx_count
        FROM v_transactions
        WHERE type = 'expense'
          AND expense_date BETWEEN ? AND ?
          AND (
            ? = 'all'
            OR (? = 'home'   AND is_travel = 0)
            OR (? = 'travel' AND is_travel = 1)
          )
        GROUP BY purpose_slug
        ORDER BY total_bdt DESC
    """, (date_from, date_to, segment, segment, segment)).fetchall()

    if not rows:
        return f"No expenses found between {date_from} and {date_to}."

    total = sum(r["total_bdt"] or 0 for r in rows)
    seg_label = f" ({segment})" if segment != "all" else ""

    if date_from == date_to:
        label = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d %B %Y").lstrip("0")
    else:
        d1 = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d %b").lstrip("0")
        d2 = datetime.strptime(date_to,   "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")
        label = f"{d1} – {d2}"

    lines = [f"\U0001F4CA *{label}{seg_label}*\n"]
    for r in rows:
        pct = int((r["total_bdt"] or 0) / total * 100) if total else 0
        bar = "\u2588" * (pct // 10)
        lines.append(
            f"  {(r['purpose'] or 'unknown'):20} {(r['total_bdt'] or 0):>8,.0f} BDT  "
            f"{pct:3}% {bar}"
        )
    lines.append(f"\n  *Total: {total:,.0f} BDT*")
    return "\n".join(lines)


@app.command("/summary")
def handle_summary_command(ack, respond, command):
    """Show spending summary.
    Usage:
      /summary                       → current month projection
      /summary home                  → current month, home only
      /summary travel                → current month, travel only
      /summary april                 → April breakdown by purpose
      /summary april home            → April, home only
      /summary 29 april              → single day
      /summary 29 april 2026         → single day with year
      /summary 5-12 april            → date range within a month
      /summary 5-12 april 2026       → date range with year
    """
    ack()
    conn = get_db()
    try:
        text  = command.get("text", "").strip().lower()
        today = date.today()

        # Detect segment keyword first
        segment = "all"
        if "home" in text:
            segment = "home"
        elif "travel" in text:
            segment = "travel"
        text_no_seg = text.replace("home", "").replace("travel", "").strip()

        # Try to parse a specific date or date range
        date_from, date_to, remaining = _parse_summary_date_range(text_no_seg)
        if date_from:
            result = _format_date_range_summary(date_from, date_to, segment, conn)
            respond(result)
            return

        # Parse month and segment from text
        from parser import MONTH_MAP
        target_month = today.strftime("%Y-%m")
        text_clean = text_no_seg
        for month_name, month_num in MONTH_MAP.items():
            if month_name in text_clean:
                year = today.year
                target_month = f"{year}-{month_num:02d}"
                break

        # Current month projection
        if target_month == today.strftime("%Y-%m") and not text_no_seg:
            proj = conn.execute(
                "SELECT * FROM v_monthly_projection"
            ).fetchone()
            if proj:
                lines = [
                    f"📊 *{today.strftime('%B %Y')} — Day {proj['days_elapsed']} of {proj['days_in_month']}*\n",
                    f"*All spending:*",
                    f"  Spent so far: *{proj['month_total_bdt']:,.0f} BDT*",
                    f"  Daily average: {proj['daily_avg_bdt']:,.0f} BDT/day",
                    f"  Projected month total: *{proj['projected_month_bdt']:,.0f} BDT*\n",
                    f"*Home only (excl. travel):*",
                    f"  Spent so far: {proj['home_month_total_bdt']:,.0f} BDT",
                    f"  Daily average: {proj['home_daily_avg_bdt']:,.0f} BDT/day",
                    f"  Projected: {proj['home_projected_month_bdt']:,.0f} BDT",
                ]
                respond("\n".join(lines))
                return

        # Monthly breakdown by purpose
        rows = conn.execute("""
            SELECT purpose, total_bdt, tx_count
            FROM v_monthly_summary
            WHERE month = ? AND segment = ?
            ORDER BY total_bdt DESC
        """, (target_month, segment)).fetchall()

        if not rows:
            respond(f"No data found for {target_month}.")
            return

        total = sum(r["total_bdt"] for r in rows)
        month_label = datetime.strptime(target_month, "%Y-%m").strftime("%B %Y")
        seg_label   = f" ({segment})" if segment != "all" else ""

        lines = [f"📊 *{month_label}{seg_label}*\n"]
        for r in rows:
            pct   = int(r["total_bdt"] / total * 100) if total else 0
            bar   = "█" * (pct // 10)
            lines.append(
                f"  {r['purpose']:20} {r['total_bdt']:>8,.0f} BDT  "
                f"{pct:3}% {bar}"
            )
        lines.append(f"\n  *Total: {total:,.0f} BDT*")
        lines.append(
            f"\nFilters: `/summary home` `/summary travel` "
            f"`/summary april home` `/summary 5-12 april`"
        )

        respond("\n".join(lines))
    finally:
        conn.close()



@app.command("/entries")
def handle_entries_command(ack, respond, command):
    """Show detailed transaction list for a date or date range.
    Usage:
      /entries 29 april           → all entries on 29 April
      /entries 29 april 2026      → with explicit year
      /entries 5-12 april         → entries for a date range
      /entries 5-12 april home    → home expenses only
      /entries 5-12 april travel  → travel expenses only
    """
    ack()
    conn = get_db()
    try:
        text = command.get("text", "").strip().lower()

        if not text:
            respond(
                "Usage:\n"
                "• `/entries 29 april` — entries for a single day\n"
                "• `/entries 5-12 april` — entries for a date range\n"
                "• `/entries 5-12 april home` — home expenses only\n"
                "• `/entries 5-12 april travel` — travel expenses only"
            )
            return

        # Detect segment
        segment = "all"
        if "home" in text:
            segment = "home"
        elif "travel" in text:
            segment = "travel"
        text_no_seg = text.replace("home", "").replace("travel", "").strip()

        # Parse date or date range
        date_from, date_to, _ = _parse_summary_date_range(text_no_seg)
        if not date_from:
            respond(
                f"Could not parse date from: `{text}`\n"
                "Try: `/entries 29 april` or `/entries 5-12 april 2026`"
            )
            return

        # Build segment filter
        seg_filter = ""
        if segment == "home":
            seg_filter = "AND is_travel = 0"
        elif segment == "travel":
            seg_filter = "AND is_travel = 1"

        rows = conn.execute(f"""
            SELECT
                id,
                expense_date,
                type,
                purpose,
                amount_bdt,
                original_amount,
                original_currency,
                payment_method,
                city,
                trip_name,
                is_travel,
                details,
                raw_text
            FROM v_transactions
            WHERE expense_date BETWEEN ? AND ?
              AND type IN ('expense', 'transfer', 'investment')
              {seg_filter}
            ORDER BY expense_date ASC, id ASC
        """, (date_from, date_to)).fetchall()

        if not rows:
            seg_label = f" ({segment})" if segment != "all" else ""
            if date_from == date_to:
                respond(f"No entries found for {date_from}{seg_label}.")
            else:
                respond(f"No entries found between {date_from} and {date_to}{seg_label}.")
            return

        # Build header
        seg_label = f" ({segment})" if segment != "all" else ""
        if date_from == date_to:
            d = datetime.strptime(date_from, "%Y-%m-%d")
            header = f"📋 *{d.strftime('%d %B %Y').lstrip('0')}{seg_label}* — {len(rows)} entries"
        else:
            d1 = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d %b").lstrip("0")
            d2 = datetime.strptime(date_to,   "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")
            header = f"📋 *{d1} – {d2}{seg_label}* — {len(rows)} entries"

        lines = [header, ""]

        total_expense = 0
        for r in rows:
            # Icon
            if r["type"] == "transfer":
                icon = "🔄"
            elif r["is_travel"]:
                icon = "✈️ "
            else:
                icon = "  "

            # Amount
            amt = r["amount_bdt"] or 0
            if r["original_currency"]:
                amt_str = f"{amt:,} BDT ({r['original_amount']:g} {r['original_currency']})"
            else:
                amt_str = f"{amt:,} BDT"

            # Purpose / type
            if r["type"] == "transfer":
                cat = "transfer"
            else:
                cat = (r["purpose"] or "?").lower()
                if r["type"] == "expense":
                    total_expense += amt

            # Payment
            pay = (r["payment_method"] or "").lower().replace("_", " ")

            # Description — prefer details if set, else raw_text
            desc = (r["details"] or r["raw_text"] or "")[:45]

            # City if not Dhaka
            city_note = f" @{r['city'].lower()}" if r["city"] and r["city"].lower() != "dhaka" else ""

            lines.append(
                f"{icon} `#{r['id']}` {r['expense_date']}  "
                f"*{cat}*  {amt_str}  {pay}{city_note}"
            )
            if desc and desc.lower() != (r["raw_text"] or "").lower():
                lines.append(f"       _{desc}_")

        lines.append("")
        lines.append(f"💰 *Total expenses: {total_expense:,} BDT*")
        if len(rows) > 20:
            lines.append(f"_Showing all {len(rows)} entries_")

        respond("\n".join(lines))

    finally:
        conn.close()

# =============================================================================
# HELPER: auto-run on unknown currencies
# =============================================================================

@app.event("message")
def handle_message_events(body, logger):
    """Catch-all to suppress unhandled event warnings."""
    pass

# =============================================================================
# STARTUP
# =============================================================================

@app.command("/export")
def handle_export_command(ack, respond, command):
    """Export all transactions to Google Sheets.
    Usage:
      /export           → export everything not yet in Sheets
      /export all       → re-export all transactions (full refresh)
      /export 2026-04   → export a specific month
    """
    ack()
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    if not spreadsheet_id:
        respond("❌ GOOGLE_SHEET_ID environment variable not set.")
        return
    if not GSHEETS_AVAILABLE:
        respond("❌ Google Sheets packages not installed. Run: pip install google-api-python-client google-auth")
        return

    text = command.get("text", "").strip().lower()
    conn = get_db()
    try:
        respond("⏳ Exporting to Google Sheets… this may take a moment.")

        # Build query based on argument
        if text == "all":
            txs = conn.execute("""
                SELECT * FROM v_transactions
                ORDER BY transacted_at, id
            """).fetchall()
            label = "all transactions"

        elif len(text) == 7 and text[4] == "-":
            # Month filter: 2026-04
            txs = conn.execute("""
                SELECT * FROM v_transactions
                WHERE month = ?
                ORDER BY transacted_at, id
            """, (text,)).fetchall()
            label = f"transactions for {text}"

        else:
            # Default: export everything (same as all)
            txs = conn.execute("""
                SELECT * FROM v_transactions
                ORDER BY transacted_at, id
            """).fetchall()
            label = "all transactions"

        if not txs:
            respond(f"No transactions found for {label}.")
            return

        # Write to Sheets
        service = get_sheets_service()

        if text == "all":
            # Full refresh — clear and rewrite
            tab = get_or_create_tab(service, spreadsheet_id, "Transactions")
            # Clear existing data (keep headers)
            service.spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id,
                range=f"{tab}!A2:Z"
            ).execute()
            rows = [tx_to_row(tx) for tx in txs]
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab}!A2",
                valueInputOption="RAW",
                body={"values": rows}
            ).execute()
        else:
            # Append mode
            tab  = get_or_create_tab(service, spreadsheet_id, "Transactions")
            rows = [tx_to_row(tx) for tx in txs]
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows}
            ).execute()

        respond(
            f"✅ Exported *{len(rows)} rows* to Google Sheets ({label}).\n"
            f"Open your sheet to view: https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        )

    except Exception as e:
        respond(f"❌ Export failed: {e}")
        logger.error(f"Export error: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    logger.info("Starting Fintracker Slack bot...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
