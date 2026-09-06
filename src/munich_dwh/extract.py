"""Extraction: CSV -> raw.sales_raw.

The landing zone rejects nothing and coerces nothing.

Three properties this module is responsible for:

  Idempotency   Each row is keyed on a SHA-256 hash of its own contents. Re-running
                the extract inserts nothing, because every hash already exists.

  Traceability  Every row records which file it came from, which line it was on and
                when it landed. A mart row can always be traced back to a line number.

  Auditability  Each run opens a meta.load_batch row and closes it with counts, so
                a load that half-finished is visibly a load that half-finished.

Deliberately no pandas. The raw layer performs no analysis.
"""
from __future__ import annotations

import csv
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .db import connect

console = Console()

RAW_COLUMNS: tuple[str, ...] = (
    "transaction_id", "invoice_number", "transaction_date", "transaction_time",
    "transaction_status", "store_id", "store_name", "store_location", "store_type",
    "store_size_sqm", "district_id", "district_name", "postal_code", "customer_id",
    "customer_name", "customer_email", "customer_loyalty_status", "product_id",
    "product_name", "product_category", "product_subcategory", "product_brand",
    "product_department", "supplier_id", "supplier_name", "quantity", "unit_price",
    "base_price", "discount_rate", "discount_applied", "total_amount", "tax_rate",
    "tax_amount", "payment_method", "sales_staff_id", "sales_staff_name",
    "promotion_id", "promotion_name",
)

# Metadata columns prepended to every row.
META_COLUMNS: tuple[str, ...] = ("_row_hash", "_source_file", "_source_line_no")

COPY_COLUMNS: tuple[str, ...] = META_COLUMNS + RAW_COLUMNS

# Field separator for the hash. A unit separator cannot occur in the source data, so
# hashing "a" + SEP + "bc" can never collide with "ab" + SEP + "c".
_HASH_SEP = "\x1f"


@dataclass
class ExtractResult:
    batch_id: int
    rows_read: int
    rows_landed: int          # genuinely new
    rows_already_present: int # seen in a previous run

    @property
    def is_noop(self) -> bool:
        return self.rows_landed == 0 and self.rows_read > 0


# ---------------------------------------------------------------------------
# Batch bookkeeping
# ---------------------------------------------------------------------------

def start_batch(source_file: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.load_batch (source_file) VALUES (%s) RETURNING batch_id",
            (source_file,),
        )
        batch_id = cur.fetchone()["batch_id"]
        conn.commit()
    return batch_id


def finish_batch(batch_id: int, *, extracted: int, staged: int = 0, rejected: int = 0,
                 status: str = "succeeded") -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE meta.load_batch
                  SET finished_at = now(), rows_extracted = %s, rows_staged = %s,
                      rows_rejected = %s, status = %s
                WHERE batch_id = %s""",
            (extracted, staged, rejected, status, batch_id),
        )
        conn.commit()


def fail_batch(batch_id: int, reason: str) -> None:
    """Leave a failed run visible rather than leaving a batch open forever."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE meta.load_batch
                  SET finished_at = now(), status = %s
                WHERE batch_id = %s""",
            (f"failed: {reason[:200]}", batch_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def row_hash(values: list[str | None]) -> str:
    """Content hash of one source row, used as the raw primary key"""
    joined = _HASH_SEP.join("" if v is None else v for v in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def validate_header(header: list[str], source: Path) -> None:
    actual = [h.strip() for h in header]
    missing = [c for c in RAW_COLUMNS if c not in actual]
    unexpected = [c for c in actual if c not in RAW_COLUMNS]

    if missing:
        raise ValueError(
            f"{source.name} is missing {len(missing)} expected column(s): "
            f"{', '.join(missing)}"
        )
    if unexpected:
        # Not fatal: extra columns are ignored, but say so rather than dropping them
        # without a word.
        console.print(
            f"[yellow]![/yellow] ignoring {len(unexpected)} unexpected column(s): "
            f"{', '.join(unexpected)}"
        )


def iter_rows(csv_path: Path):
    """Yield (hash, source_file, line_no, *values) tuples, one per source row.

    Empty fields become NULL. Everything else, including the literal string "N/A",
    is preserved exactly as delivered -- turning "N/A" into NULL here would hide a
    finding the transform stage is supposed to make an explicit decision about.
    """
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{csv_path.name} is empty") from None

        validate_header(header, csv_path)
        index = {name: pos for pos, name in enumerate(h.strip() for h in header)}
        name = csv_path.name

        # Line 1 is the header, so data starts at line 2.
        for line_no, record in enumerate(reader, start=2):
            if not any(field.strip() for field in record):
                continue  # skip blank lines rather than landing an all-null row

            values: list[str | None] = []
            for column in RAW_COLUMNS:
                position = index[column]
                raw = record[position] if position < len(record) else ""
                values.append(raw if raw != "" else None)

            yield (row_hash(values), name, line_no, *values)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def extract(csv_path: Path, batch_id: int | None = None) -> ExtractResult:
    """Stream the CSV into raw.sales_raw"""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found at {csv_path}")

    batch_id = batch_id if batch_id is not None else start_batch(csv_path.name)
    columns = ", ".join(COPY_COLUMNS)
    rows_read = 0

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _raw_stage "
                "(LIKE raw.sales_raw INCLUDING DEFAULTS) ON COMMIT DROP"
            )

            with cur.copy(f"COPY _raw_stage ({columns}) FROM STDIN") as copy:
                for row in iter_rows(csv_path):
                    copy.write_row(row)
                    rows_read += 1

            if rows_read == 0:
                raise ValueError(f"{csv_path.name} contained no data rows")

            # A file can legitimately contain the same row twice. DISTINCT ON keeps
            # the first occurrence so the INSERT cannot fail on its own duplicates.
            cur.execute(
                f"""INSERT INTO raw.sales_raw ({columns})
                    SELECT DISTINCT ON (_row_hash) {columns}
                      FROM _raw_stage
                     ORDER BY _row_hash, _source_line_no
                    ON CONFLICT (_row_hash) DO NOTHING"""
            )
            rows_landed = cur.rowcount
            conn.commit()

    except Exception as exc:
        fail_batch(batch_id, str(exc))
        raise

    already_present = rows_read - rows_landed
    console.print(
        f"[green]+[/green] raw: {rows_read:,} rows read, {rows_landed:,} landed"
        + (f", {already_present:,} already present" if already_present else "")
    )
    return ExtractResult(batch_id, rows_read, rows_landed, already_present)


def raw_row_count() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM raw.sales_raw")
        return cur.fetchone()["n"]


def fetch_raw() -> list[dict]:
    """Read the raw layer back out. The transform stage consumes this.

    Returned as dicts rather than a DataFrame so this module stays pandas-free; the
    transform stage builds its own frame from these rows.
    """
    columns = ", ".join(("_row_hash", "_source_line_no", *RAW_COLUMNS))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {columns} FROM raw.sales_raw ORDER BY _source_line_no")
        return cur.fetchall()


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/munich_retail_sales_raw.csv")
    result = extract(path)
    finish_batch(result.batch_id, extracted=result.rows_read)
    console.print(f"batch {result.batch_id}: raw.sales_raw now holds {raw_row_count():,} rows")
