# =============================================================================
# FINTRACKER — Entry Parser
# Parses raw Slack paste into structured transaction candidates
# =============================================================================
#
# INPUT: a raw Slack message paste, exactly as Khobaib types it
# OUTPUT: list of ParsedLine objects, ready for AI classifier + review
#
# PARSING PIPELINE per paste:
#   1. Split paste into sections by date headers
#   2. For each section, detect city header
#   3. For each remaining line, run line parser:
#      a. Skip blanks
#      b. Detect [Prefix] (paid-for-someone pattern)
#      c. Detect third-party-paid: "(X paid)" → my expense = 0
#      d. Detect cashback: "- N tk cashback" → deduct from amount
#      e. Detect transfer: "X to Y", "X cashout", "X cash withdraw"
#      f. Detect payment method from bracket rule
#      g. Parse amount expression (arithmetic, k/m suffix, foreign currency)
#      h. Parse description (everything before the amount)
#      i. Detect #home tag
#      j. Detect @city tag (per-line override)
#      k. Detect #purpose tag (explicit override)
#      l. Run rules engine
#      m. Flag for AI if confidence < threshold
#
# AMOUNT ARITHMETIC:
#   "60 + 40 + 30"   → 130
#   "80+60"          → 140
#   "2*500"          → 1000
#   "1067 + 150"     → 1217
#   "500 + 500"      → 1000
#   "220 + 20 tips"  → 240  (strip word suffixes before eval)
#   "220 + 20 tk tips." → 240
#   round(result)    → integer BDT
#
# PAYMENT BRACKET RULE:
#   Last (parenthesised token) on the line = payment method IF it matches
#   a known payment keyword. Otherwise it's part of the description.
#   "(ebl)"        → ebl_card
#   "(scb)"        → scb_card
#   "(dbbl)"       → dbbl_card
#   "(bkash)"      → bkash
#   "(foodi/bkash)"→ platform=foodi, payment=bkash
#   "(uber/cash)"  → service=uber, payment=cash
#   "(uber/manual bkash)" → service=uber, payment=bkash
#   "(uber/personal bkash)" → service=uber, payment=bkash
#   "(uber)"       → service=uber, payment=cash (default)
#   no bracket     → payment=cash (default)
#
# SPECIAL PATTERNS:
#   "[Name] ..."   → paid_for=Name, my expense = full amount
#   "(X paid)"     → paid_by=X, my expense = 0, bill stored in details
#   "- N tk cashback" → net_amount = amount - cashback
#   "X cashout"    → transfer from X account to cash
#   "X to Y - N"   → transfer from X to Y
#
# TAGS:
#   #home          → override trip assignment; trip_id = NULL
#   @cityname      → set city for this line (normalized to slug)
#   #purposeslug   → override AI purpose suggestion
#
# =============================================================================

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from enum import Enum


# =============================================================================
# DATA CLASSES
# =============================================================================

class MatchSource(str, Enum):
    RULE       = "rule"
    AI         = "ai"
    MANUAL     = "manual"
    STRUCTURAL = "structural"   # transfer, cashout — deterministic

@dataclass
class ParsedLine:
    """One line from a Slack paste, fully parsed."""
    raw_line:           str
    line_number:        int

    # Classification
    tx_type:            Optional[str]   = None  # expense | transfer | investment
    purpose_slug:       Optional[str]   = None
    purpose_override:   bool            = False  # True if #tag forced it

    # Amounts
    amount_bdt:         Optional[int]   = None   # rounded integer BDT
    original_amount:    Optional[float] = None   # if foreign currency
    original_currency:  Optional[str]   = None   # "USD", "IDR", etc.
    cashback_bdt:       Optional[int]   = None   # deducted from amount
    bill_amount_bdt:    Optional[int]   = None   # original bill when third-party paid

    # Payment
    payment_slug:       Optional[str]   = None
    transport_service:  Optional[str]   = None
    platform:           Optional[str]   = None   # foodi, fp, etc.

    # Transfer-specific
    transfer_from:      Optional[str]   = None   # account slug
    transfer_to:        Optional[str]   = None   # account slug

    # Third-party
    paid_by:            Optional[str]   = None   # person name if they paid
    paid_for:           Optional[str]   = None   # [Name] prefix — paid on behalf

    # Location / trip
    city_slug:          Optional[str]   = None
    is_home_override:   bool            = False  # #home tag present

    # Description
    description:        Optional[str]   = None   # cleaned readable description

    # Confidence
    match_source:       Optional[str]   = None
    confidence:         float           = 0.0
    needs_review:       bool            = True   # default: always review
    review_reason:      Optional[str]   = None   # why it needs review

    # Error
    parse_error:        Optional[str]   = None   # if line couldn't be parsed
    user_corrected:     bool            = False  # True if user changed a field
    entry_date:         Optional[date]  = None   # set by paste parser per section


@dataclass
class ParsedPaste:
    """Full parsed result of one Slack paste."""
    raw_message:    str
    entry_date:     date                        # from date header, or today
    default_city:   str                         = "dhaka"  # from @city header
    trip_id:        Optional[int]               = None     # active trip at parse time
    lines:          list[ParsedLine]            = field(default_factory=list)


# =============================================================================
# CONSTANTS
# =============================================================================

PAYMENT_KEYWORDS = {
    "ebl":              "ebl_card",
    "ebl card":         "ebl_card",
    "scb":              "scb_card",
    "scb card":         "scb_card",
    "dbbl":             "dbbl_card",
    "dbbl card":        "dbbl_card",
    "bkash":            "bkash",
    "b-kash":           "bkash",
    "nagad":            "nagad",
    "metro card":       "metro_card",
    "cash":             "cash",
}

# Slash-pattern: right side is payment method
# "foodi/bkash" → payment=bkash, platform=foodi
# "uber/cash"   → payment=cash,  service=uber
# "fp/bkash"    → payment=bkash, platform=fp (foodpanda)
TRANSPORT_SERVICES = {"uber", "pathao", "gojek", "grab"}
PLATFORM_NAMES     = {"foodi", "fp", "foodpanda"}

TRANSFER_KEYWORDS = {
    "cashout", "cash out", "cash withdraw", "cash withdrawal",
    "withdraw", "atm"
}

# Account name → slug mapping for transfer parsing
ACCOUNT_SLUGS = {
    "ebl":      "ebl",
    "scb":      "scb",
    "dbbl":     "dbbl",
    "bkash":    "bkash",
    "b-kash":   "bkash",
    "nagad":    "nagad",
    "cash":     "cash",
}

# Known currencies (lowercase)
KNOWN_CURRENCIES = {
    "bdt", "usd", "eur", "sgd", "thb", "idr",
    "myr", "aud", "gbp", "inr", "jpy"
}

# Amount word suffixes to strip before arithmetic eval
AMOUNT_NOISE = re.compile(
    r'\b(tk|taka|bdt|tips?|tip|only|approx|roughly|incl\.?|including)\b',
    re.IGNORECASE
)

# Date header patterns
DATE_PATTERNS = [
    re.compile(r'^(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)$', re.I),
    re.compile(r'^(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)$', re.I),
    re.compile(r'^(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})$', re.I),
    re.compile(r'^(\d{1,2})[/\-](\d{1,2})$'),          # 17/04 or 17-04
    re.compile(r'^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$'),  # 2026-04-17
]

MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
    "sep":9,"oct":10,"nov":11,"dec":12
}

# High-confidence threshold — below this, flag for review
CONFIDENCE_THRESHOLD = 0.85


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def to_slug(text: str) -> str:
    """Normalize to slug: 'Kuala Lumpur' → 'kuala_lumpur'"""
    return re.sub(r'\s+', '_', text.strip().lower())


def parse_date(text: str) -> Optional[date]:
    """Try to parse a line as a date header. Return date or None."""
    text = text.strip()

    # "17 April" or "17 Apr"
    m = re.match(r'^(\d{1,2})\s+([a-zA-Z]+)$', text)
    if m:
        day, month_str = int(m.group(1)), m.group(2).lower()
        if month_str in MONTH_MAP:
            today = date.today()
            year = today.year
            # If parsed month is in the future by more than 1 month, use last year
            try:
                d = date(year, MONTH_MAP[month_str], day)
                if (d - today).days > 30:
                    d = date(year - 1, MONTH_MAP[month_str], day)
                return d
            except ValueError:
                return None

    # "April 17"
    m = re.match(r'^([a-zA-Z]+)\s+(\d{1,2})$', text)
    if m:
        month_str, day = m.group(1).lower(), int(m.group(2))
        if month_str in MONTH_MAP:
            today = date.today()
            try:
                d = date(today.year, MONTH_MAP[month_str], day)
                if (d - today).days > 30:
                    d = date(today.year - 1, MONTH_MAP[month_str], day)
                return d
            except ValueError:
                return None

    # "17/04" or "17-04"
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})$', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        today = date.today()
        try:
            d = date(today.year, month, day)
            if (d - today).days > 30:
                d = date(today.year - 1, month, day)
            return d
        except ValueError:
            return None

    # "2026-04-17"
    m = re.match(r'^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$', text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    return None


def parse_amount_expression(expr: str) -> tuple[Optional[float], Optional[str]]:
    """
    Parse an amount expression. Returns (amount_float, currency_code).
    currency_code is None if BDT.

    Examples:
      "350"           → (350.0, None)
      "350.7"         → (350.7, None)
      "60 + 40 + 30"  → (130.0, None)
      "2*500"         → (1000.0, None)
      "10k idr"       → (10000.0, "IDR")
      "12.52 usd"     → (12.52, "USD")
      "212k idr"      → (212000.0, "IDR")
      "1.5m idr"      → (1500000.0, "IDR")
      "=1067+150"     → (1217.0, None)   ← Google Sheets formula
      "500 + 500"     → (1000.0, None)
      "220 + 20 tk tips." → (240.0, None)
    """
    if not expr:
        return None, None

    original = expr.strip()

    # Strip leading = (Google Sheets formula)
    if original.startswith('='):
        original = original[1:]

    # Detect and strip trailing currency code
    currency = None
    curr_match = re.search(
        r'\b(usd|eur|sgd|thb|idr|myr|aud|gbp|inr|jpy|bdt)\b',
        original, re.I
    )
    if curr_match:
        currency = curr_match.group(1).upper()
        original = original[:curr_match.start()].strip()

    # Strip noise words (tips, tk, taka, etc.)
    cleaned = AMOUNT_NOISE.sub('', original).strip()

    # Handle k/m suffix BEFORE arithmetic (e.g. "10k", "1.5m", "212k")
    def expand_km(s: str) -> str:
        s = re.sub(r'([\d.]+)\s*k\b', lambda m: str(float(m.group(1)) * 1000), s, flags=re.I)
        s = re.sub(r'([\d.]+)\s*m\b', lambda m: str(float(m.group(1)) * 1000000), s, flags=re.I)
        return s

    cleaned = expand_km(cleaned)

    # Remove any remaining non-numeric characters except operators and decimal
    # Keep: digits, ., +, -, *, /
    # Remove: letters, commas (treat "1,000" as "1000")
    cleaned = cleaned.replace(',', '')
    # Strip trailing punctuation
    cleaned = cleaned.rstrip('.')

    # Try safe arithmetic eval
    # Only allow digits, spaces, operators, decimal point
    safe = re.sub(r'[^\d\s\+\-\*\/\.\(\)]', '', cleaned).strip()
    if not safe:
        return None, None

    try:
        result = eval(safe)  # safe: only numeric operators
        return float(result), currency
    except Exception:
        # Try just extracting first number
        m = re.search(r'[\d.]+', safe)
        if m:
            try:
                return float(m.group()), currency
            except ValueError:
                pass
        return None, None


def parse_payment_bracket(text: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Find the LAST parenthesised token and check if it's a payment method.
    Returns: (text_without_payment_bracket, payment_slug, platform, service)

    Examples:
      "dinner (tandoor) - 691 (foodi/bkash)"
        → ("dinner (tandoor) - 691", "bkash", "foodi", None)
      "bike to office - 150 (uber/cash)"
        → ("bike to office - 150", "cash", None, "uber")
      "dinner (thai express) - 2105 (ebl)"
        → ("dinner (thai express) - 2105", "ebl_card", None, None)
      "fuchka - 120"
        → ("fuchka - 120", "cash", None, None)   ← default cash
      "metro - metro card"
        → ("metro", "metro_card", None, None)
      "bike to office - 150 (uber, too much charge)"
        → ("bike to office - 150", "cash", None, "uber")  ← note stripped to details
    """
    payment_slug = "cash"   # default
    platform     = None
    service      = None
    remaining    = text

    # Check for "metro card" (not in brackets)
    if re.search(r'\bmetro\s*card\b', text, re.I):
        remaining = re.sub(r'\s*-?\s*metro\s*card\b', '', remaining, flags=re.I).strip()
        return remaining, "metro_card", None, None

    # Find ALL parenthesised groups
    parens = list(re.finditer(r'\(([^)]+)\)', text))
    if not parens:
        return remaining, payment_slug, platform, service

    # Check the LAST paren group for payment keywords
    last = parens[-1]
    content = last.group(1).strip().lower()

    # Handle slash pattern: "foodi/bkash", "uber/cash", "uber/manual bkash"
    if '/' in content:
        parts = [p.strip() for p in content.split('/', 1)]
        left, right = parts[0], parts[1]

        # Normalize right side (strip "manual", "personal" qualifiers)
        right_norm = re.sub(r'\b(manual|personal|auto)\b\s*', '', right).strip()

        # Identify payment from right side
        right_pay = _identify_payment(right_norm)
        if right_pay:
            payment_slug = right_pay
            # Identify left side as service or platform
            left_norm = re.sub(r'\b(manual|personal)\b', '', left).strip()
            if left_norm in TRANSPORT_SERVICES:
                service = left_norm
            elif left_norm in PLATFORM_NAMES:
                platform = left_norm
            else:
                platform = left_norm  # unknown platform, store anyway
            # Remove this bracket from text
            remaining = text[:last.start()].rstrip() + text[last.end():]
            return remaining.strip(), payment_slug, platform, service

    # Single keyword in bracket
    pay = _identify_payment(content)
    if pay:
        payment_slug = pay
        # Check if there's also a service name in this content
        for svc in TRANSPORT_SERVICES:
            if svc in content:
                service = svc
                break
        remaining = text[:last.start()].rstrip() + text[last.end():]
        return remaining.strip(), payment_slug, platform, service

    # Last bracket not a payment method → check if it contains a service name
    # e.g. "(uber, too much charge because of rain)"
    for svc in TRANSPORT_SERVICES:
        if svc in content:
            service = svc
            # Still cash default; strip this bracket but keep content in notes
            remaining = text[:last.start()].rstrip() + text[last.end():]
            return remaining.strip(), payment_slug, platform, service

    # Last bracket is a description/name — don't touch it
    return remaining, payment_slug, platform, service


def _identify_payment(text: str) -> Optional[str]:
    """Map a string to a payment slug, or None if not a payment keyword."""
    t = text.strip().lower()
    # Direct match
    if t in PAYMENT_KEYWORDS:
        return PAYMENT_KEYWORDS[t]
    # Partial match for common abbreviations
    for kw, slug in PAYMENT_KEYWORDS.items():
        if t == kw:
            return slug
    # Check if any payment keyword is the dominant word
    for kw, slug in sorted(PAYMENT_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if re.search(r'\b' + re.escape(kw) + r'\b', t):
            return slug
    return None


def parse_transfer(line_lower: str) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Detect if a line is a transfer and extract from/to accounts.
    Returns: (is_transfer, from_slug, to_slug)

    Patterns:
      "ebl to bkash"      → (True, "ebl", "bkash")
      "scb to bkash"      → (True, "scb", "bkash")
      "bkash cashout"     → (True, "bkash", "cash")
      "ebl cashout"       → (True, "ebl", "cash")
      "ebl cash withdraw" → (True, "ebl", "cash")
      "dbbl to ebl"       → (True, "dbbl", "ebl")
    """
    # Pattern: "X to Y"
    m = re.search(
        r'\b(ebl|scb|dbbl|bkash|b-kash|nagad|cash)\s+to\s+(ebl|scb|dbbl|bkash|b-kash|nagad|cash)\b',
        line_lower
    )
    if m:
        from_acc = ACCOUNT_SLUGS.get(m.group(1), m.group(1))
        to_acc   = ACCOUNT_SLUGS.get(m.group(2), m.group(2))
        return True, from_acc, to_acc

    # Pattern: "X cashout" / "X cash withdraw" / "X cash out"
    m = re.search(
        r'\b(ebl|scb|dbbl|bkash|b-kash|nagad)\s+(cashout|cash\s*out|cash\s*withdraw\w*|atm)\b',
        line_lower
    )
    if m:
        from_acc = ACCOUNT_SLUGS.get(m.group(1), m.group(1))
        return True, from_acc, "cash"

    return False, None, None


def parse_third_party_paid(text: str) -> tuple[str, Optional[str], Optional[int]]:
    """
    Detect "(X paid)" pattern.
    Returns: (text_without_paid_bracket, paid_by_name, original_bill_bdt)

    "(raya paid)"   → my expense = 0, paid_by = "Raya"
    "- raya paid"   → my expense = 0, paid_by = "Raya" (no amount)
    """
    # Pattern: ends with "(name paid)" or "(name paid, ...)"
    m = re.search(r'\(([^)]+?\s+paid[^)]*)\)', text, re.I)
    if m:
        content = m.group(1)
        name_match = re.match(r'^(\w+)\s+paid', content, re.I)
        if name_match:
            paid_by = name_match.group(1).capitalize()
            remaining = text[:m.start()].rstrip() + text[m.end():]
            return remaining.strip(), paid_by, None

    # Pattern: line ends with "- X paid" (no amount at all)
    m = re.search(r'-\s*(\w+)\s+paid\s*$', text, re.I)
    if m:
        paid_by = m.group(1).capitalize()
        remaining = text[:m.start()].rstrip()
        return remaining.strip(), paid_by, None

    return text, None, None


def parse_cashback(text: str) -> tuple[str, Optional[int]]:
    """
    Detect "- N tk cashback" pattern.
    Returns: (text_without_cashback, cashback_amount)
    """
    m = re.search(
        r'\s*[-–]\s*([\d,]+(?:\.\d+)?)\s*(?:tk\s+)?cashback\b',
        text, re.I
    )
    if m:
        cashback_str = m.group(1).replace(',', '')
        try:
            cashback = round(float(cashback_str))
            remaining = text[:m.start()] + text[m.end():]
            return remaining.strip(), cashback
        except ValueError:
            pass
    return text, None


def parse_prefix_tag(text: str) -> tuple[str, Optional[str]]:
    """
    Detect [Name] prefix: paid on behalf of someone.
    "[Raya] bike to mirpur" → ("bike to mirpur", "Raya")
    "[G] car to bashundhara" → ("car to bashundhara", "G")
    """
    m = re.match(r'^\[([^\]]+)\]\s*', text)
    if m:
        return text[m.end():].strip(), m.group(1).strip()
    return text, None


def extract_inline_tags(text: str) -> tuple[str, dict]:
    """
    Extract #purpose and @city tags from a line.
    Returns: (text_without_tags, {tags})

    "#treat" → purpose_override = "treat"
    "#home"  → is_home = True
    "@bali"  → city_slug = "bali"
    "@kuala lumpur" is NOT supported inline (use header) — must be @kuala_lumpur
    """
    tags = {"is_home": False, "purpose_slug": None, "city_slug": None}
    remaining = text

    # #home tag
    if re.search(r'#home\b', remaining, re.I):
        tags["is_home"] = True
        remaining = re.sub(r'\s*#home\b', '', remaining, flags=re.I).strip()

    # @city tag (word characters + underscores)
    m = re.search(r'@([\w]+)', remaining, re.I)
    if m:
        tags["city_slug"] = to_slug(m.group(1))
        remaining = remaining[:m.start()].rstrip() + remaining[m.end():]
        remaining = remaining.strip()

    # #purpose tag (must come after #home to avoid false match)
    m = re.search(r'#([\w]+)', remaining, re.I)
    if m:
        tags["purpose_slug"] = m.group(1).lower()
        remaining = remaining[:m.start()].rstrip() + remaining[m.end():]
        remaining = remaining.strip()

    return remaining, tags


def split_description_and_amount(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Split "description - amount_expression" on the LAST dash.
    The last dash separates the final amount from the description.

    "dinner (tandoor crafts) - 691"    → ("dinner (tandoor crafts)", "691")
    "rickshaw - 60 + 40 + 30"          → ("rickshaw", "60 + 40 + 30")
    "bike to office - 150"             → ("bike to office", "150")
    "Bike to Bashundhara (Leo cafe) - 182" → ("Bike to Bashundhara (Leo cafe)", "182")
    "metro to uttara north"             → ("metro to uttara north", None) ← no amount
    "Electric work (saiful bhai) - 850 (total 1700, split with Tahsin)"
      → ("Electric work (saiful bhai)", "850") [after bracket already stripped]

    Edge case: "tarc to shyamli - 10 + 40 + 10" — the dash belongs to amount
    Rule: find the last " - " (space-dash-space) before what looks like a number
    """
    # Find all " - " positions
    splits = [m.start() for m in re.finditer(r'\s+-\s+', text)]

    if not splits:
        return text.strip(), None

    # Walk splits from right to left; pick first one where right side
    # starts with a digit or arithmetic expression
    for pos in reversed(splits):
        right = text[pos:].lstrip(' -').strip()
        # Right side should start with digit, =, or currency keyword
        if re.match(r'^[\d=]', right) or re.match(r'^metro\s*card', right, re.I):
            desc = text[:pos].strip()
            amt_str = right
            return desc, amt_str

    # Fallback: use last dash
    pos = splits[-1]
    return text[:pos].strip(), text[pos:].lstrip(' -').strip()


# =============================================================================
# RULES ENGINE
# =============================================================================

def run_rules_engine(
    description_lower: str,
    db_conn: sqlite3.Connection
) -> tuple[Optional[str], Optional[str], Optional[str], float]:
    """
    Run classifier_rules against the description.
    Returns: (tx_type, purpose_slug, payment_slug_override, confidence)
    payment_slug_override is only set when a rule explicitly overrides payment
    (e.g. metro → metro_card). Does NOT override the bracket-based payment.
    """
    rules = db_conn.execute("""
        SELECT tx_type, purpose_slug, payment_slug, transport_service, confidence, match_type, pattern
        FROM classifier_rules
        WHERE is_active = 1
        ORDER BY priority ASC
    """).fetchall()

    best_type     = None
    best_purpose  = None
    best_payment  = None
    best_conf     = 0.0

    for rule in rules:
        tx_type, purpose, pay, transport, conf, match_type, pattern = rule

        matched = False
        if match_type == 'keyword':
            matched = pattern.lower() in description_lower
        elif match_type == 'regex':
            try:
                matched = bool(re.search(pattern, description_lower))
            except re.error:
                continue

        if matched:
            if tx_type and best_type is None:
                best_type = tx_type
            if purpose and best_purpose is None:
                best_purpose = purpose
                best_conf = conf
            if pay and best_payment is None:
                best_payment = pay
            # Once we have type + purpose, we can stop
            if best_type and best_purpose:
                break

    return best_type, best_purpose, best_payment, best_conf


# =============================================================================
# MAIN LINE PARSER
# =============================================================================

def parse_line(
    raw_line:       str,
    line_number:    int,
    default_city:   str,
    active_trip_id: Optional[int],
    db_conn:        sqlite3.Connection,
    entry_date:     date,
) -> Optional[ParsedLine]:
    """
    Parse a single transaction line. Returns ParsedLine or None if line should be skipped.
    """
    result = ParsedLine(raw_line=raw_line, line_number=line_number)

    # --- Step 0: Skip blank lines ---
    line = raw_line.strip()
    if not line:
        return None

    # --- Step 1: Extract inline tags (#home, @city, #purpose) ---
    line, tags = extract_inline_tags(line)
    result.is_home_override = tags["is_home"]
    result.city_slug        = tags["city_slug"] or default_city

    # Trip assignment
    if result.is_home_override:
        result.tx_type = None  # will be set later; no trip
    elif active_trip_id:
        pass  # trip_id applied at session level

    # --- Step 2: Extract [Prefix] tag ---
    line, paid_for = parse_prefix_tag(line)
    result.paid_for = paid_for

    # --- Step 3: Third-party paid detection ---
    line, paid_by, _ = parse_third_party_paid(line)
    if paid_by:
        result.paid_by  = paid_by
        result.amount_bdt = 0
        # The bill amount will be parsed and stored in description

    # --- Step 4: Cashback detection ---
    line, cashback = parse_cashback(line)
    if cashback:
        result.cashback_bdt = cashback

    # --- Step 5: Payment method from bracket ---
    line, payment_slug, platform, service = parse_payment_bracket(line)
    result.payment_slug     = payment_slug
    result.platform         = platform
    result.transport_service = service

    # --- Step 6: Transfer detection (on original lowercased full line) ---
    is_transfer, from_acc, to_acc = parse_transfer(raw_line.lower())
    if is_transfer:
        result.tx_type      = "transfer"
        result.transfer_from = from_acc
        result.transfer_to   = to_acc
        result.match_source  = MatchSource.STRUCTURAL
        result.confidence    = 1.0
        result.needs_review  = False
        # Parse amount from line for transfers too
        _, amt_str = split_description_and_amount(line)
        if amt_str:
            amt, curr = parse_amount_expression(amt_str)
            if amt is not None:
                result.estimated_amount_bdt = round(amt)
        result.description = line.strip()
        return result

    # --- Step 7: Split description and amount ---
    description, amt_str = split_description_and_amount(line)
    result.description = description or line

    # --- Step 8: Parse amount ---
    if result.paid_by:
        # Third-party paid — parse bill amount for reference, store in details
        if amt_str:
            amt, curr = parse_amount_expression(amt_str)
            if amt is not None:
                result.bill_amount_bdt = round(amt)
        result.amount_bdt = 0
        result.original_amount   = None
        result.original_currency = None
    elif amt_str:
        amt, curr = parse_amount_expression(amt_str)
        if amt is None:
            result.parse_error  = f"Could not parse amount from: {repr(amt_str)}"
            result.needs_review = True
            result.review_reason = "amount_parse_failed"
        else:
            result.original_currency = curr
            if curr:
                result.original_amount = amt
                # BDT conversion deferred — exchange rate applied at commit time
                result.amount_bdt = None  # will be filled by caller with rate
            else:
                raw_bdt = amt
                # Apply cashback if present
                if result.cashback_bdt:
                    raw_bdt = max(0, raw_bdt - result.cashback_bdt)
                result.amount_bdt = round(raw_bdt)
    else:
        # No amount found
        result.amount_bdt    = 0
        result.needs_review  = True
        result.review_reason = "no_amount"

    # --- Step 9: Run rules engine ---
    desc_lower = (description or line).lower()
    rule_type, rule_purpose, rule_pay_override, rule_conf = run_rules_engine(
        desc_lower, db_conn
    )

    # Apply purpose tag override (#treat etc.) — highest priority
    if tags["purpose_slug"]:
        result.purpose_slug     = tags["purpose_slug"]
        result.purpose_override = True
        result.match_source     = MatchSource.MANUAL
        result.confidence       = 1.0
    elif rule_purpose:
        result.purpose_slug  = rule_purpose
        result.match_source  = MatchSource.RULE
        result.confidence    = rule_conf
        if rule_type:
            result.tx_type = rule_type
    else:
        result.match_source = MatchSource.AI
        result.confidence   = 0.0   # AI will fill this

    # Payment override from rules (e.g. metro → metro_card)
    # Only apply if user didn't specify a bracket
    if rule_pay_override and result.payment_slug == "cash":
        result.payment_slug = rule_pay_override

    # Default tx_type
    if not result.tx_type:
        result.tx_type = "expense"

    # --- Step 10: Decide if needs review ---
    if result.purpose_override:
        result.needs_review = False
    elif result.confidence >= CONFIDENCE_THRESHOLD and result.amount_bdt is not None:
        result.needs_review = False
        result.review_reason = None
    else:
        result.needs_review = True
        if not result.review_reason:
            if result.confidence == 0.0:
                result.review_reason = "needs_ai_classification"
            else:
                result.review_reason = f"low_confidence_{result.confidence:.2f}"

    return result


# =============================================================================
# PASTE PARSER (top level)
# =============================================================================

def parse_paste(
    raw_message:    str,
    db_conn:        sqlite3.Connection,
    active_trip_id: Optional[int] = None,
) -> ParsedPaste:
    """
    Parse a full Slack paste into a ParsedPaste.

    Handles:
    - Multiple date sections in one paste
    - City header after date header
    - Per-line @city and #tags
    - All transaction line patterns
    """
    result = ParsedPaste(raw_message=raw_message, entry_date=date.today())

    lines = raw_message.strip().split('\n')

    current_date  = date.today()
    current_city  = "dhaka"      # default home city
    line_number   = 0

    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        i += 1

        if not raw:
            continue

        # --- Check for date header ---
        parsed_date = parse_date(raw)
        if parsed_date:
            current_date = parsed_date
            # Check if next non-blank line is a city header
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                next_line = lines[j].strip()
                if next_line.startswith('@'):
                    city_slug = to_slug(next_line[1:])
                    current_city = city_slug
                    i = j + 1   # consume the city header line
            # Update paste-level date (first date wins for session record)
            if result.entry_date == date.today() or current_date < result.entry_date:
                result.entry_date   = current_date
                result.default_city = current_city
            continue

        # --- Check for standalone city header (without preceding date) ---
        if raw.startswith('@') and ' ' not in raw.split('@', 1)[1][:20]:
            city_candidate = to_slug(raw[1:])
            # Only treat as header if it looks like a city name (no spaces, no digits)
            if re.match(r'^[a-zA-Z_]+$', city_candidate):
                current_city = city_candidate
                result.default_city = current_city
                continue

        # --- Parse as transaction line ---
        line_number += 1
        parsed = parse_line(
            raw_line       = raw,
            line_number    = line_number,
            default_city   = current_city,
            active_trip_id = active_trip_id,
            db_conn        = db_conn,
            entry_date     = current_date,
        )

        if parsed:
            # Attach the correct date for this section
            parsed.entry_date  = current_date  # store on line for commit
            result.lines.append(parsed)

    return result


# =============================================================================
# REVIEW SUMMARY FORMATTER
# =============================================================================

def format_review_summary(parsed: ParsedPaste) -> str:
    """
    Format the parsed result into a Slack review message.
    This is what the bot sends back to you for confirmation.

    Example output:
    ──────────────────────────────────────
    📅 17 April | 📍 Dhaka | ✈️ Indonesia March 2026
    Got 9 transactions. Reply with line number to correct, or *save all* to confirm.

    1. ✅ rickshaw — commuting — 130 BDT — cash
    2. ✅ park ticket (ebl) — recreation — 40 BDT — EBL card
    3. ✅ fuchka — food bill — 120 BDT — cash
    4. ⚠️  dinner — ? — 2400 BDT — EBL card  [low confidence: food bill or treat?]
    5. ✅ ebl to bkash — transfer — 2000 BDT
    6. ✅ hotel pondok ijo — accommodation — 1534 BDT (12.52 USD) — EBL card  [trip]
    7. 🏠 rent dhaka — accommodation — 25000 BDT — cash  [home override]
    8. ❓ others entry — ? — 500 BDT — cash  [needs AI]
    9. 💸 fuchka — food bill — 0 BDT (Raya paid, bill: 100)
    ──────────────────────────────────────
    """
    lines_out = []

    # Header
    date_str = parsed.entry_date.strftime("%-d %B")
    city_str = parsed.default_city.replace('_', ' ').title()
    header = f"📅 {date_str}  📍 {city_str}"
    lines_out.append(header)

    tx_count = len([l for l in parsed.lines if l.tx_type != "transfer"])
    tr_count = len([l for l in parsed.lines if l.tx_type == "transfer"])
    needs_review = sum(1 for l in parsed.lines if l.needs_review)

    summary = f"Found *{len(parsed.lines)} entries*"
    if tr_count:
        summary += f" ({tr_count} transfer{'s' if tr_count>1 else ''})"
    if needs_review:
        summary += f" — *{needs_review} need your review*"
    summary += ". Reply with line number to correct, or *save all* to confirm."
    lines_out.append(summary)
    lines_out.append("")

    for pl in parsed.lines:
        # Icon
        if pl.parse_error:
            icon = "❌"
        elif pl.tx_type == "transfer":
            icon = "🔄"
        elif pl.paid_by:
            icon = "💸"
        elif pl.is_home_override:
            icon = "🏠"
        elif pl.needs_review:
            icon = "⚠️ " if pl.confidence > 0 else "❓"
        else:
            icon = "✅"

        # Description
        desc = (pl.description or pl.raw_line)[:40]

        # Amount
        if pl.original_currency:
            amt_str = f"{pl.original_amount} {pl.original_currency} → ? BDT"
        elif pl.amount_bdt == 0 and pl.paid_by:
            amt_str = f"0 BDT (bill: {pl.bill_amount_bdt or '?'}, {pl.paid_by} paid)"
        elif pl.amount_bdt is not None:
            amt_str = f"{pl.amount_bdt:,} BDT"
        else:
            amt_str = "? BDT"

        # Purpose
        if pl.tx_type == "transfer":
            purpose_str = f"transfer  {pl.transfer_from or '?'} → {pl.transfer_to or '?'}"
        elif pl.purpose_slug:
            purpose_str = pl.purpose_slug.replace('_', ' ')
        else:
            purpose_str = "? (needs classification)"

        # Payment
        pay_str = (pl.payment_slug or "?").replace('_', ' ')

        # Flags
        flags = []
        if pl.purpose_override:      flags.append("tag override")
        if pl.is_home_override:      flags.append("home override")
        if pl.paid_for:              flags.append(f"paid for {pl.paid_for}")
        if pl.cashback_bdt:          flags.append(f"cashback {pl.cashback_bdt}")
        if pl.review_reason:         flags.append(pl.review_reason)
        if pl.parse_error:           flags.append(f"ERROR: {pl.parse_error}")

        flag_str = f"  [{', '.join(flags)}]" if flags else ""

        # Confidence badge
        if pl.match_source == MatchSource.RULE and not pl.needs_review:
            conf_badge = ""
        elif pl.confidence > 0:
            conf_badge = f" ({pl.confidence:.0%})"
        else:
            conf_badge = ""

        line_out = (
            f"{icon} *{pl.line_number}.* {desc} — "
            f"{purpose_str}{conf_badge} — "
            f"{amt_str} — {pay_str}"
            f"{flag_str}"
        )
        lines_out.append(line_out)

    return "\n".join(lines_out)


# =============================================================================
# EXCHANGE RATE HELPER
# =============================================================================

def get_exchange_rate(currency_code: str, tx_date: date, db_conn: sqlite3.Connection) -> Optional[float]:
    """
    Get the exchange rate for a currency on or before tx_date.
    Returns rate_to_bdt or None if not found.
    """
    row = db_conn.execute("""
        SELECT rate_to_bdt FROM exchange_rates
        WHERE currency_code = ?
          AND effective_date <= ?
        ORDER BY effective_date DESC
        LIMIT 1
    """, (currency_code.upper(), tx_date.isoformat())).fetchone()

    return row[0] if row else None


def apply_exchange_rates(parsed: ParsedPaste, db_conn: sqlite3.Connection) -> list[str]:
    """
    Apply exchange rates to all lines with foreign currencies.
    Returns list of warning messages for unknown currencies.
    """
    warnings = []
    for pl in parsed.lines:
        if pl.original_currency and pl.amount_bdt is None:
            rate = get_exchange_rate(pl.original_currency, parsed.entry_date, db_conn)
            if rate:
                raw_bdt = pl.original_amount * rate
                if pl.cashback_bdt:
                    raw_bdt = max(0, raw_bdt - pl.cashback_bdt)
                pl.amount_bdt        = round(raw_bdt)
                pl.exchange_rate_used = rate
            else:
                pl.needs_review  = True
                pl.review_reason = f"no_rate_for_{pl.original_currency}"
                warnings.append(
                    f"⚠️ No exchange rate found for {pl.original_currency}. "
                    f"Please set it with: `/rate {pl.original_currency.lower()} <rate>`"
                )
    return warnings
