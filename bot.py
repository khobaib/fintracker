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


def commit_session(user_id: str, conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Commit all confirmed lines in the session.
    Returns (saved_count, skipped_count).
    """
    session = get_session(user_id)
    if not session:
        return 0, 0

    parsed:  ParsedPaste   = session["parsed"]
    db_session_id: int     = session["db_session_id"]

    saved = skipped = 0
    for pl in parsed.lines:
        tx_id = commit_line(pl, session, conn, db_session_id)
        if tx_id:
            saved += 1
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
    return saved, skipped

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


def _handle_session_input(user_id: str, text: str, say, conn: sqlite3.Connection):
    """Handle input when a review session is active."""
    session  = get_session(user_id)
    parsed:  ParsedPaste = session["parsed"]
    text_low = text.strip().lower()

    # --- SAVE ALL ---
    if text_low in ("save all", "save", "ok", "confirm", "yes", "done", "✅"):
        saved, skipped = commit_session(user_id, conn)
        msg = f"✅ Saved *{saved}* transaction{'s' if saved != 1 else ''}."
        if skipped:
            msg += f" Skipped {skipped} (parse errors)."
        # Show quick stats
        total = sum(
            pl.amount_bdt or 0
            for pl in parsed.lines
            if pl.tx_type == "expense"
        )
        msg += f"\nTotal expenses: *{total:,} BDT*"
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
    """Set an exchange rate. Usage: /rate usd 122.5"""
    ack()
    conn = get_db()
    try:
        parts = command.get("text", "").strip().split()
        if len(parts) != 2:
            respond("Usage: `/rate <currency> <rate>`\nExample: `/rate usd 122.5`")
            return

        currency = parts[0].upper()
        try:
            rate = float(parts[1])
        except ValueError:
            respond(f"Invalid rate: `{parts[1]}`")
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

        respond(
            f"✅ Exchange rate updated:\n"
            f"*1 {currency} = {rate} BDT*\n"
            f"Effective from today ({date.today().strftime('%d %B %Y')})"
        )
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


@app.command("/actual")
def handle_actual_command(ack, respond, command):
    """Set actual bank-confirmed amount for a transaction.
    Usage: /actual <tx_id> <amount>
    """
    ack()
    conn = get_db()
    try:
        parts = command.get("text", "").strip().split()
        if len(parts) != 2:
            respond("Usage: `/actual <transaction_id> <amount>`\n"
                    "Example: `/actual 42 1850`")
            return
        try:
            tx_id  = int(parts[0])
            actual = round(float(parts[1]))
        except ValueError:
            respond("Invalid input. Both ID and amount must be numbers.")
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
            f"• Description: _{tx['raw_text']}_\n"
            f"• Estimated: {tx['estimated_amount_bdt']:,} BDT\n"
            f"• Actual: *{actual:,} BDT*\n"
            f"• Difference: {sign}{diff:,} BDT"
        )
    finally:
        conn.close()


@app.command("/summary")
def handle_summary_command(ack, respond, command):
    """Show monthly summary or projection.
    Usage: /summary              → current month projection
           /summary april        → April summary
           /summary home         → current month, home only
    """
    ack()
    conn = get_db()
    try:
        text  = command.get("text", "").strip().lower()
        today = date.today()

        # Parse month from text if given
        target_month = today.strftime("%Y-%m")
        segment      = "all"

        if text:
            if text in ("home",):
                segment = "home"
            elif text in ("travel",):
                segment = "travel"
            else:
                # Try to parse as month name
                from parser import MONTH_MAP
                for month_name, month_num in MONTH_MAP.items():
                    if month_name in text:
                        year = today.year
                        target_month = f"{year}-{month_num:02d}"
                        break

        # Current month projection
        if target_month == today.strftime("%Y-%m"):
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
        lines.append(f"\nUse `/summary home` or `/summary travel` to filter.")

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

if __name__ == "__main__":
    init_db()
    logger.info("Starting Fintracker Slack bot...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
