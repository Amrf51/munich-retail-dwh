"""Transform: raw.sales_raw -> staging.sales_clean.

Governing principle: **nothing is deleted to make the numbers look clean.** Every
suspect row is loaded with a bit set in `dq_flags`, and the views in `030_views.sql`
decide what to exclude. Only rows that cannot be typed at all -- no parseable date,
no transaction id, no product id -- are quarantined in `raw.rejects`, with their full
payload, so even those are recoverable.

The stages run in a fixed order, and the order matters:

  1. parse       strings -> date / time / int / Decimal
  2. standardise casing drift and one typo collapsed to canonical values
  3. aliases     one canonical name per id, decided by majority vote
  4. repair      derive missing numerics from the price identity
  5. money       recompute total_amount rather than trusting the source
  6. flag        set the dq_flags bitmask
  7. customer    email domain extracted, name optionally pseudonymised

Steps 3 and 4 depend on step 2 having already collapsed casing, and step 6 depends on
5 having produced a canonical total to compare against. Step 3 needs two passes over
the data, which is why the whole batch is held in memory; at 35,612 rows that is a
few hundred megabytes at worst and buys a much simpler implementation than a
streaming alternative.

Deliberately no pandas. This is row-wise work over Decimals, which the stdlib does
more legibly, and it keeps the runtime dependencies to four packages.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from enum import IntFlag
from typing import Any, Iterable

from rich.console import Console

from .config import settings
from .db import connect

console = Console()

TAX_RATE = Decimal("0.19")
CENT = Decimal("0.01")
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Quality flags
# ---------------------------------------------------------------------------

class DQ(IntFlag):
    """Bitmask of quality conditions. One integer column, ten independent flags,
    filterable with `WHERE dq_flags & 4 > 0`."""
    NONE = 0
    TOTAL_MISMATCH = 1            # total_amount disagrees with quantity * unit_price
    QUANTITY_OUTLIER = 2          # quantity above threshold, source reaches 9,963
    DISCOUNT_OUT_OF_RANGE = 4     # discount_rate outside [0, 1], source reaches 2.0
    NEGATIVE_QTY_COMPLETED = 8    # negative quantity on a Completed line
    PLACEHOLDER_STORE = 16        # store 6, "Imaginary Store", -500 sqm
    REPAIRED_FROM_NA = 32         # a numeric was "N/A" and was derived
    MISSING_CUSTOMER = 64         # walk-in, no customer_id
    DISCOUNT_FLAG_CONFLICT = 128  # discount_applied contradicts discount_rate
    UNPARSED_DATE = 256           # date matched no known format (these go to rejects)
    NON_UUID_TRANSACTION_ID = 512 # 10-char alphanumeric id rather than a UUID
    UNPARSEABLE_EMAIL_DOMAIN = 1024  # address has no "@" separator


# store 6 ships as "Imaginary Store" at "Nowhere St. 123" with -500 sqm in district
# 99 "Unknown", postal code 00000. But its 5,154 transactions reference real products,
# real customers and real staff, and their amounts are ordinary. The master record is
# fabricated; the transactions are not. So the facts are kept and the store is marked.
PLACEHOLDER_STORE_IDS = {6}
PLACEHOLDER_DISTRICT_IDS = {99}


# ---------------------------------------------------------------------------
# 1. Parsers
# ---------------------------------------------------------------------------

NULL_TOKENS = {"", "n/a", "na", "null", "none", "-", "nan", "?"}

# Order matters. Profiling found exactly three formats, disambiguated mechanically:
#   33,864 rows  YYYY-MM-DD
#      877 rows  DD/MM/YYYY  -- first component reaches 31, so day-first
#      871 rows  MM-DD-YYYY  -- first component never exceeds 12, so month-first
# The DD.MM.YYYY branch is defensive; the current file contains none.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m-%d-%Y", "%d.%m.%Y")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
COPY_PREFIX_RE = re.compile(r"^(copy of\s+)+", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")


def is_null(value: Any) -> bool:
    return value is None or str(value).strip().lower() in NULL_TOKENS


def clean_text(value: Any) -> str | None:
    """Trim and collapse internal whitespace. Null tokens become None."""
    if is_null(value):
        return None
    return WHITESPACE_RE.sub(" ", str(value).strip())


def parse_date(value: Any) -> dt.date | None:
    text = clean_text(value)
    if text is None:
        return None
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_time(value: Any) -> dt.time | None:
    text = clean_text(value)
    if text is None:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_decimal(value: Any) -> Decimal | None:
    """Handles plain decimals, German comma decimals, thousands separators and
    currency symbols.

    The current file is almost entirely plain, but a loader that only handles the
    happy path breaks the first time upstream changes its locale, and German sources
    change locale often.
    """
    text = clean_text(value)
    if text is None:
        return None
    text = text.replace("\u20ac", "").replace("EUR", "").strip()
    if "," in text and "." in text:
        # 1.234,56 (German) vs 1,234.56 (English): the rightmost separator is the
        # decimal point in both conventions.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_int(value: Any) -> int | None:
    number = parse_decimal(value)
    return None if number is None else int(number)


def parse_bool(value: Any) -> bool | None:
    text = clean_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"true", "t", "yes", "y", "1", "ja"}:
        return True
    if lowered in {"false", "f", "no", "n", "0", "nein"}:
        return False
    return None


# ---------------------------------------------------------------------------
# 2. Standardisation
# ---------------------------------------------------------------------------

def _lookup(canonical: dict[str, str]):
    """Map by case-folded key, falling back to title case for unseen values.

    An explicit lookup rather than a blanket .title() because .title() would turn
    "EC-Karte" into "Ec-Karte" and "H&M" into "H&M" only by luck.
    """
    def standardise(value: Any) -> str | None:
        text = clean_text(value)
        if text is None:
            return None
        return canonical.get(text.lower(), text.title())
    return standardise


# 9 distinct source values collapse to 3.
standardize_status = _lookup({
    "completed": "Completed",
    "canceled": "Canceled",
    "cancelled": "Canceled",
    "returned": "Returned",
})

# 12 distinct source values collapse to 4.
standardize_store_type = _lookup({
    "shopping center": "Shopping Center",
    "city center": "City Center",
    "outlet": "Outlet",
    "luxury mall": "Luxury Mall",
})

# 20 distinct source values collapse to 5. "Mobil Zahlung" (70 rows) and
# "Mobile Zahlung" (6,450 rows) are the same method with a typo, not two methods.
standardize_payment_method = _lookup({
    "bargeld": "Bargeld",
    "kreditkarte": "Kreditkarte",
    "ec-karte": "EC-Karte",
    "ec karte": "EC-Karte",
    "gutschein": "Gutschein",
    "mobile zahlung": "Mobile Zahlung",
    "mobil zahlung": "Mobile Zahlung",
})

standardize_loyalty = _lookup({
    "bronze": "Bronze",
    "silber": "Silber",
    "silver": "Silber",
    "gold": "Gold",
    "platin": "Platin",
    "platinum": "Platin",
})


def strip_copy_prefix(value: Any) -> str | None:
    """3,535 rows across 4 product ids carry a "Copy of " prefix. It is a source
    artefact, not part of the product name. Applied repeatedly for "Copy of Copy of"."""
    text = clean_text(value)
    if text is None:
        return None
    return COPY_PREFIX_RE.sub("", text).strip() or None


# ---------------------------------------------------------------------------
# 3. Alias resolution
# ---------------------------------------------------------------------------

def resolve_aliases(rows: list[dict], key: str, value: str) -> dict[Any, str]:
    """Pick one canonical value per key: most frequent wins, ties broken by length.

    Two genuine aliases survive case-folding in this source:
        store_id 1     -> "Olympia Einkaufszentrum" / "OEZ Muenchen"
        supplier_id 1  -> "Deutsche Waren GmbH" / "DW GmbH"

    Majority vote is deterministic given the same input and needs no hand-maintained
    mapping table that would rot the moment a new alias appears. Longest-wins on ties
    is deliberate: an expanded legal name is more useful in a dimension than an
    abbreviation.
    """
    counts: dict[Any, Counter] = defaultdict(Counter)
    for row in rows:
        k, v = row.get(key), row.get(value)
        if k is not None and v is not None:
            counts[k][v] += 1

    return {
        k: max(counter.items(), key=lambda item: (item[1], len(item[0])))[0]
        for k, counter in counts.items()
    }


# ---------------------------------------------------------------------------
# 7. Customer handling
# ---------------------------------------------------------------------------

def pseudonymize(value: Any, salt: str | None = None, prefix: str = "CUST") -> str | None:
    """Salted HMAC-SHA256. Deterministic for a given salt, so a customer keeps the
    same identity across re-runs; rotating the salt severs the link permanently."""
    text = clean_text(value)
    if text is None:
        return None
    key = (salt or settings.pseudonym_salt).encode("utf-8")
    digest = hmac.new(key, text.lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{prefix}-{digest[:16]}"


def email_domain(value: Any) -> tuple[str | None, bool]:
    """Return (domain, was_parseable).

    Splitting on "@" and taking the last part returns the WHOLE STRING when there is
    no "@" -- which would put a local part, i.e. a name, into the mart. 2,367 source
    addresses use "#" instead of "@". Those get a null domain and a flag, never a
    guessed one.
    """
    text = clean_text(value)
    if text is None:
        return None, True
    if text.count("@") != 1:
        return None, False
    domain = text.rsplit("@", 1)[-1].lower().strip()
    return (domain or None), bool(domain)


# ---------------------------------------------------------------------------
# The transform
# ---------------------------------------------------------------------------

STAGING_COLUMNS: tuple[str, ...] = (
    "transaction_id", "batch_id", "invoice_number",
    "transaction_date", "transaction_time", "transaction_status",
    "store_id", "store_name", "store_type", "store_location",
    "district_name", "postal_code", "store_size_sqm",
    "customer_id", "customer_name", "email_domain", "loyalty_status",
    "product_id", "product_name", "category", "subcategory", "brand",
    "department", "base_price",
    "supplier_id", "supplier_name",
    "quantity", "unit_price", "discount_rate", "tax_rate", "tax_amount",
    "total_amount", "total_amount_raw",
    "payment_method", "sales_staff_name", "promotion_name",
    "dq_flags",
)


def transform(raw_rows: Iterable[dict], batch_id: int | None = None
              ) -> tuple[list[dict], list[dict]]:
    """Returns (clean_rows, rejected_rows)."""
    rows = list(raw_rows)
    if not rows:
        return [], []

    threshold = Decimal(str(settings.quantity_outlier_threshold))

    # --- 1 & 2: parse and standardise ---------------------------------------
    parsed: list[dict] = []
    for source in rows:
        parsed.append({
            "_row_hash": source.get("_row_hash"),
            "_source_line_no": source.get("_source_line_no"),
            "transaction_id": clean_text(source.get("transaction_id")),
            "invoice_number": clean_text(source.get("invoice_number")),
            "transaction_date": parse_date(source.get("transaction_date")),
            "transaction_time": parse_time(source.get("transaction_time")),
            "transaction_status": standardize_status(source.get("transaction_status")),

            "store_id": parse_int(source.get("store_id")),
            "store_name": clean_text(source.get("store_name")),
            "store_type": standardize_store_type(source.get("store_type")),
            "store_location": clean_text(source.get("store_location")),
            "district_id": parse_int(source.get("district_id")),
            "district_name": clean_text(source.get("district_name")),
            # Postal codes are identifiers, not numbers. Zero-padded and kept as text
            # so 80331 and 08033 stay distinct and leading zeros survive.
            "postal_code": (pc.zfill(5) if (pc := clean_text(source.get("postal_code"))) else None),
            "store_size_sqm": parse_int(source.get("store_size_sqm")),

            "customer_id": parse_int(source.get("customer_id")),
            "customer_name_raw": clean_text(source.get("customer_name")),
            "customer_email_raw": clean_text(source.get("customer_email")),
            "loyalty_status": standardize_loyalty(source.get("customer_loyalty_status")),

            "product_id": parse_int(source.get("product_id")),
            "product_name": strip_copy_prefix(source.get("product_name")),
            "category": clean_text(source.get("product_category")),
            "subcategory": clean_text(source.get("product_subcategory")),
            "brand": clean_text(source.get("product_brand")),
            # All 3,393 missing departments belong to the Miscellaneous category, so
            # this is a structural gap rather than random loss. 'Unassigned' keeps
            # department roll-ups complete instead of dropping rows from them.
            "department": clean_text(source.get("product_department")) or "Unassigned",
            "base_price": parse_decimal(source.get("base_price")),

            "supplier_id": parse_int(source.get("supplier_id")),
            "supplier_name": clean_text(source.get("supplier_name")),

            "quantity": parse_decimal(source.get("quantity")),
            "unit_price": parse_decimal(source.get("unit_price")),
            "discount_rate": parse_decimal(source.get("discount_rate")),
            "discount_applied": parse_bool(source.get("discount_applied")),
            "total_amount_raw": parse_decimal(source.get("total_amount")),

            "payment_method": standardize_payment_method(source.get("payment_method")),
            "sales_staff_name": clean_text(source.get("sales_staff_name")),
            "promotion_name": clean_text(source.get("promotion_name")),
        })

    # --- 3: aliases (needs the whole batch, hence the second pass) -----------
    store_names = resolve_aliases(parsed, "store_id", "store_name")
    store_types = resolve_aliases(parsed, "store_id", "store_type")
    supplier_names = resolve_aliases(parsed, "supplier_id", "supplier_name")
    product_names = resolve_aliases(parsed, "product_id", "product_name")

    clean: list[dict] = []
    rejects: list[dict] = []

    for row in parsed:
        row["store_name"] = store_names.get(row["store_id"], row["store_name"])
        row["store_type"] = store_types.get(row["store_id"], row["store_type"])
        row["supplier_name"] = supplier_names.get(row["supplier_id"], row["supplier_name"])
        row["product_name"] = product_names.get(row["product_id"], row["product_name"])

        flags = DQ.NONE

        # --- quarantine: rows that cannot be typed at all --------------------
        if row["transaction_date"] is None or not row["transaction_id"] or row["product_id"] is None:
            rejects.append({
                "_row_hash": row["_row_hash"],
                "transaction_id": row["transaction_id"],
                "reason_code": ("UNPARSEABLE_DATE" if row["transaction_date"] is None
                                else "MISSING_MANDATORY_KEY"),
                "reason_detail": (
                    f'line {row["_source_line_no"]}: '
                    f'date={row["transaction_date"]!r} '
                    f'transaction_id={bool(row["transaction_id"])} '
                    f'product_id={row["product_id"]!r}'
                ),
                "payload": {k: str(v) for k, v in row.items() if not k.startswith("_")},
            })
            continue

        # --- 4: repair -------------------------------------------------------
        # unit_price = base_price * (1 - discount_rate) holds for 100% of parseable
        # rows, which is what makes deriving missing values from it safe. No source
        # row has more than one "N/A" among quantity / unit_price / total_amount, so
        # every gap has two intact neighbours to be derived from.
        repaired = False

        if row["unit_price"] is None and row["base_price"] is not None \
                and row["discount_rate"] is not None:
            row["unit_price"] = (row["base_price"] * (Decimal(1) - row["discount_rate"])).quantize(CENT)
            repaired = True

        if row["quantity"] is None and row["total_amount_raw"] is not None \
                and row["unit_price"]:
            row["quantity"] = Decimal(round(row["total_amount_raw"] / row["unit_price"]))
            repaired = True

        if row["total_amount_raw"] is None and row["quantity"] is not None \
                and row["unit_price"] is not None:
            row["total_amount_raw"] = (row["quantity"] * row["unit_price"]).quantize(CENT)
            repaired = True

        if repaired:
            flags |= DQ.REPAIRED_FROM_NA

        # A discount_rate outside [0, 1] produces a negative unit price. base_price is
        # never negative, so the rate is the corrupt side: null it and fall back to
        # the list price rather than propagating a negative price into revenue.
        if row["discount_rate"] is not None and not (ZERO <= row["discount_rate"] <= Decimal(1)):
            flags |= DQ.DISCOUNT_OUT_OF_RANGE
            row["unit_price"] = row["base_price"]
            row["discount_rate"] = None

        # --- 5: money --------------------------------------------------------
        quantity, unit_price = row["quantity"], row["unit_price"]
        if quantity is not None and unit_price is not None:
            row["total_amount"] = (quantity * unit_price).quantize(CENT)
            row["tax_amount"] = (row["total_amount"] * TAX_RATE).quantize(CENT)
        else:
            row["total_amount"] = None
            row["tax_amount"] = None
        row["tax_rate"] = TAX_RATE

        # --- 6: flags --------------------------------------------------------
        if row["total_amount"] is not None and row["total_amount_raw"] is not None \
                and abs(row["total_amount"] - row["total_amount_raw"]) > CENT:
            flags |= DQ.TOTAL_MISMATCH

        if quantity is not None:
            if quantity > threshold:
                flags |= DQ.QUANTITY_OUTLIER
            if quantity < 0 and row["transaction_status"] == "Completed":
                flags |= DQ.NEGATIVE_QTY_COMPLETED

        if row["store_id"] in PLACEHOLDER_STORE_IDS or row["district_id"] in PLACEHOLDER_DISTRICT_IDS:
            flags |= DQ.PLACEHOLDER_STORE

        if row["customer_id"] is None:
            flags |= DQ.MISSING_CUSTOMER

        # discount_applied is unreliable: 1,756 rows null it and 257 contradict the
        # rate. The column is not carried forward; presence is derived from the rate.
        if row["discount_applied"] is not None:
            has_discount = row["discount_rate"] is not None and row["discount_rate"] > ZERO
            if row["discount_applied"] != has_discount:
                flags |= DQ.DISCOUNT_FLAG_CONFLICT

        if not UUID_RE.match(row["transaction_id"]):
            flags |= DQ.NON_UUID_TRANSACTION_ID

        # --- 7: customer -----------------------------------------------------
        domain, parseable = email_domain(row["customer_email_raw"])
        if not parseable:
            flags |= DQ.UNPARSEABLE_EMAIL_DOMAIN
        row["email_domain"] = domain
        row["customer_name"] = (
            pseudonymize(row["customer_name_raw"]) if settings.pseudonymize_customers
            else row["customer_name_raw"]
        )

        # Quantity is whole units. Decimal only existed to survive parsing.
        row["quantity"] = int(quantity) if quantity is not None else None
        row["batch_id"] = batch_id
        row["dq_flags"] = int(flags)

        clean.append({column: row.get(column) for column in STAGING_COLUMNS})

    flagged = sum(1 for r in clean if r["dq_flags"])
    console.print(
        f"[green]+[/green] staging: {len(clean):,} rows, {flagged:,} flagged, "
        f"{len(rejects):,} quarantined"
    )
    return clean, rejects


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def write_staging(rows: list[dict]) -> int:
    """Upsert into staging.sales_clean, keyed on transaction_id.

    Same COPY-into-temp-then-upsert pattern as the extract: COPY is the fast path but
    cannot express conflict handling, so the conflict resolution happens in one
    set-based statement afterwards.
    """
    if not rows:
        return 0

    columns = ", ".join(STAGING_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in STAGING_COLUMNS if c != "transaction_id")

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _stg (LIKE staging.sales_clean INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )
        with cur.copy(f"COPY _stg ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(tuple(row[c] for c in STAGING_COLUMNS))
        cur.execute(
            f"""INSERT INTO staging.sales_clean ({columns})
                SELECT {columns} FROM _stg
                ON CONFLICT (transaction_id) DO UPDATE SET {updates}"""
        )
        affected = cur.rowcount
        conn.commit()
    return affected


def write_rejects(rows: list[dict], batch_id: int | None) -> int:
    """Quarantine, with the full payload as JSON so a rejected row can be inspected
    and replayed rather than merely counted."""
    if not rows:
        return 0
    import json

    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO raw.rejects
                   (batch_id, _row_hash, transaction_id, reason_code, reason_detail, payload)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
            [
                (batch_id, r["_row_hash"], r["transaction_id"], r["reason_code"],
                 r["reason_detail"], json.dumps(r["payload"], default=str))
                for r in rows
            ],
        )
        conn.commit()
    return len(rows)


def flag_summary() -> list[tuple[str, int]]:
    """Flag distribution over staging, for the console summary."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT dq_flags FROM staging.sales_clean WHERE dq_flags <> 0")
        counter: Counter = Counter()
        for record in cur.fetchall():
            for flag in DQ:
                if flag.value and record["dq_flags"] & flag.value:
                    counter[flag.name.lower()] += 1
    return counter.most_common()