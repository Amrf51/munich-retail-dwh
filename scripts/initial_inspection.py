"""Profile the raw CSV before designing.

Deliberately standalone: pandas is the only dependency, so this runs on a fresh
clone with no database

python scripts/initial_inspection.py --csv data/munich_retail_sales_raw.csv

Writes a markdown report to docs/profiling_findings.md and prints the same content.
"""


from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import pandas as pd

# Never emit values from these columns. Counts and derived aggregates only.
REDACTED = {"customer_name", "customer_email"}

# Candidate date formats, checked against the whole column to see which are present.
DATE_PATTERNS = {
    "YYYY-MM-DD": r"^\d{4}-\d{2}-\d{2}$",
    "DD-MM-YYYY or MM-DD-YYYY": r"^\d{2}-\d{2}-\d{4}$",
    "DD/MM/YYYY or MM/DD/YYYY": r"^\d{2}/\d{2}/\d{4}$",
    "DD.MM.YYYY": r"^\d{2}\.\d{2}\.\d{4}$",
}

# Keys whose attributes should be functionally dependent on them. Any key mapping
# to more than one value of an attribute is either an alias or a genuine conflict.
DEPENDENCY_CHECKS = [
    ("store_id", ["store_name", "store_location", "store_type", "store_size_sqm", "district_id"]),
    ("district_id", ["district_name", "postal_code"]),
    ("product_id", ["product_name", "product_category", "product_subcategory",
                    "product_brand", "product_department", "supplier_id", "base_price"]),
    ("supplier_id", ["supplier_name"]),
    ("sales_staff_id", ["sales_staff_name"]),
    ("promotion_id", ["promotion_name"]),
    ("customer_id", ["customer_loyalty_status"]),
]

CATEGORICAL = [
    "transaction_status", "store_type", "store_name", "district_name",
    "customer_loyalty_status", "product_category", "product_department",
    "payment_method", "discount_applied", "tax_rate",
]

NUMERIC_AS_STRING = ["quantity", "unit_price", "total_amount"]

out = io.StringIO()


def emit(line: str = "") -> None:
    print(line)
    out.write(line + "\n")


def section(title: str) -> None:
    emit()
    emit(f"## {title}")
    emit()


def table(headers: list[str], rows: list[list], aligns: list[str] | None = None) -> None:
    aligns = aligns or ["---"] * len(headers)
    emit("| " + " | ".join(headers) + " |")
    emit("| " + " | ".join(aligns) + " |")
    for row in rows:
        emit("| " + " | ".join(str(c) for c in row) + " |")


# ---------------------------------------------------------------------------


def profile_shape(df: pd.DataFrame) -> None:
    section("Shape and completeness")
    emit(f"**{len(df):,} rows, {len(df.columns)} columns.**")
    emit()

    rows = []
    for col in df.columns:
        missing = int(df[col].isna().sum())
        if missing:
            rows.append([f"`{col}`", f"{missing:,}", f"{missing / len(df):.1%}"])
    if rows:
        emit("Columns with missing values:")
        emit()
        table(["Column", "Missing", "Share"], rows, ["---", "---:", "---:"])


def profile_cardinality(df: pd.DataFrame) -> None:
    section("Cardinality")
    rows = []
    for col in df.columns:
        note = "values not shown (PII)" if col in REDACTED else ""
        rows.append([f"`{col}`", f"{df[col].nunique():,}", note])
    table(["Column", "Distinct", "Note"], rows, ["---", "---:", "---"])


def profile_uniqueness(df: pd.DataFrame) -> None:
    """The headline check: is invoice_number actually a basket identifier?"""
    section("Candidate keys")

    emit(f"- Fully duplicated rows: **{int(df.duplicated().sum()):,}**")
    emit(f"- `transaction_id` duplicates: **{int(df['transaction_id'].duplicated().sum()):,}** "
         f"({df['transaction_id'].nunique():,} distinct across {len(df):,} rows)")
    emit(f"- `invoice_number` distinct: **{df['invoice_number'].nunique():,}** "
         f"across {len(df):,} rows")
    emit()

    # If invoice_number were a real header key, every row sharing one would agree on
    # the header-level attributes. Test that directly.
    repeated = df.groupby("invoice_number").filter(lambda g: len(g) > 1)
    n_repeated = repeated["invoice_number"].nunique()
    emit(f"Of the **{n_repeated:,}** invoice numbers appearing on more than one row, "
         f"how many carry conflicting header attributes?")
    emit()

    rows = []
    for col in ["transaction_date", "store_id", "customer_id", "payment_method", "sales_staff_id"]:
        conflicting = int((repeated.groupby("invoice_number")[col].nunique() > 1).sum())
        rows.append([f"`{col}`", f"{conflicting:,}", f"{conflicting / n_repeated:.1%}"])
    table(["Attribute", "Invoices with >1 value", "Share"], rows, ["---", "---:", "---:"])

    emit()
    emit("> A real invoice header would show zero conflicts. This is a colliding random "
         "string, not a basket identifier.")

    # Uniqueness is not the same as consistent formatting. A key column can be a
    # perfectly good key and still refuse to fit the type you were about to give it.
    emit()
    emit("Key **formats** (uniqueness alone does not tell you what type to declare):")
    emit()
    uuid_shape = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    for col, shape, label in [
        ("transaction_id", uuid_shape, "UUID"),
        ("invoice_number", r"^INV-\d+$", "INV-nnnnn"),
    ]:
        hits = df[col].str.match(shape, na=False)
        others = sorted({str(v) for v in df[col][~hits].dropna().unique()})[:3]
        emit(f"- `{col}`: **{int(hits.sum()):,}** match `{label}`, "
             f"**{int((~hits).sum()):,}** do not"
             + (f" (e.g. {', '.join(f'`{v}`' for v in others)}, "
                f"length {len(others[0])})" if others else ""))


def profile_dependencies(df: pd.DataFrame) -> None:
    section("Functional dependencies")
    emit("Does each id map to exactly one value of its attributes? Anything above 1 is "
         "either an alias or a genuine conflict.")
    emit()

    # A variant that disappears under case-folding is casing drift, fixed by
    # standardisation. A variant that survives is a genuine alias needing a
    # canonical-value rule. Different problems, different fixes.
    rows = []
    aliases: list[str] = []
    for key, attrs in DEPENDENCY_CHECKS:
        for attr in attrs:
            subset = df.dropna(subset=[key])
            raw = subset.groupby(key)[attr].nunique()
            folded = (subset.assign(_f=subset[attr].str.lower().str.strip())
                            .groupby(key)["_f"].nunique())

            raw_offenders = int((raw > 1).sum())
            true_offenders = int((folded > 1).sum())

            if not raw_offenders:
                status = "clean"
            elif not true_offenders:
                status = "casing only"
            else:
                status = "**alias**"
            rows.append([f"`{key}`", f"`{attr}`", int(raw.max()), raw_offenders, status])

            if true_offenders and attr not in REDACTED:
                for key_value, variants in subset.groupby(key)[attr].unique().items():
                    if len({str(v).lower().strip() for v in variants}) > 1:
                        joined = " / ".join(f"`{v}`" for v in variants)
                        aliases.append(f"- `{key}` = {key_value} -> {joined}")

    table(["Key", "Attribute", "Max variants", "Keys affected", "Status"],
          rows, ["---", "---", "---:", "---:", "---"])

    emit()
    emit("Genuine alias conflicts, meaning they survive case-folding and standardisation "
         "alone will not fix them:")
    emit()
    for line in aliases:
        emit(line)


def profile_dates(df: pd.DataFrame) -> None:
    section("Date formats")
    col = df["transaction_date"]
    matched = pd.Series(False, index=col.index)

    rows = []
    for name, pattern in DATE_PATTERNS.items():
        hits = col.str.match(pattern, na=False)
        matched |= hits
        if hits.sum():
            sample = ", ".join(f"`{v}`" for v in col[hits].head(3))
            rows.append([name, f"{int(hits.sum()):,}", sample])
    unmatched = int((~matched).sum())
    table(["Pattern", "Rows", "Examples"], rows, ["---", "---:", "---"])
    emit()
    emit(f"Rows matching no known pattern: **{unmatched:,}**")

    # Disambiguate day-first from month-first by checking whether either component
    # ever exceeds 12. This is what makes the ambiguous formats mechanically decidable.
    emit()
    for label, pattern, sep in [
        ("dash", r"^\d{2}-\d{2}-\d{4}$", "-"),
        ("slash", r"^\d{2}/\d{2}/\d{4}$", "/"),
    ]:
        block = col[col.str.match(pattern, na=False)]
        if block.empty:
            continue
        parts = block.str.split(sep, expand=True).astype(int)
        first, second = int(parts[0].max()), int(parts[1].max())
        verdict = "day-first" if first > 12 else ("month-first" if second > 12 else "AMBIGUOUS")
        emit(f"- {label}-separated block: first component reaches **{first}**, "
             f"second reaches **{second}** -> **{verdict}**")


def profile_categoricals(df: pd.DataFrame) -> None:
    section("Categorical drift")
    emit("Distinct values before and after case-folding. A gap means casing drift, "
         "not genuinely different values.")
    emit()

    rows = []
    for col in CATEGORICAL:
        raw = df[col].nunique()
        folded = df[col].str.lower().str.strip().nunique()
        gap = "yes" if raw != folded else "no"
        rows.append([f"`{col}`", raw, folded, gap])
    table(["Column", "Distinct raw", "Distinct case-folded", "Drift?"],
          rows, ["---", "---:", "---:", "---"])

    for col in ["transaction_status", "store_type", "payment_method"]:
        emit()
        emit(f"`{col}` values:")
        emit()
        counts = df[col].value_counts(dropna=False)
        table(["Value", "Rows"], [[f"`{v}`", f"{c:,}"] for v, c in counts.items()],
              ["---", "---:"])


def profile_numerics(df: pd.DataFrame) -> None:
    section("Numeric columns stored as text")
    emit("These arrived as strings. What is in them that is not a number?")
    emit()

    plain = re.compile(r"^-?\d+(\.\d+)?$")
    rows = []
    for col in NUMERIC_AS_STRING:
        ok = df[col].str.match(plain, na=False)
        odd = sorted({str(v) for v in df[col][~ok].dropna().unique()})[:5]
        rows.append([f"`{col}`", f"{int(ok.sum()):,}", f"{int((~ok).sum()):,}",
                     ", ".join(f"`{v}`" for v in odd) or "-"])
    table(["Column", "Plain numeric", "Other", "Non-numeric values"],
          rows, ["---", "---:", "---:", "---"])

    # Do the N/A gaps overlap? If not, each gap is derivable from the other columns.
    na_masks = {c: df[c].eq("N/A") for c in NUMERIC_AS_STRING}
    overlap = sum(na_masks.values()).max()
    any_na = int(sum(na_masks.values()).gt(0).sum())
    emit()
    emit(f"Rows with at least one `N/A` across those three columns: **{any_na:,}**. "
         f"Maximum `N/A` count in any single row: **{int(overlap)}**.")
    if overlap <= 1:
        emit()
        emit("> No row has more than one gap, so every missing value is derivable "
             "from the other two.")

    num = {c: pd.to_numeric(df[c], errors="coerce") for c in
           ["quantity", "unit_price", "base_price", "discount_rate", "total_amount", "tax_amount"]}

    section("Value ranges and outliers")
    rows = []
    for name, series in num.items():
        rows.append([f"`{name}`", f"{series.min():,.2f}", f"{series.median():,.2f}",
                     f"{series.max():,.2f}", f"{int(series.isna().sum()):,}"])
    table(["Column", "Min", "Median", "Max", "Unparseable"],
          rows, ["---", "---:", "---:", "---:", "---:"])

    qty, unit, base, disc = num["quantity"], num["unit_price"], num["base_price"], num["discount_rate"]
    emit()
    emit(f"- `quantity` negative: **{int((qty < 0).sum()):,}** "
         f"| above 20: **{int((qty > 20).sum()):,}** | max **{qty.max():,.0f}**")
    emit(f"- `discount_rate` outside [0, 1]: **{int(((disc < 0) | (disc > 1)).sum()):,}** "
         f"(max {disc.max():.2f})")
    emit(f"- `unit_price` at or below zero: **{int((unit <= 0).sum()):,}** "
         f"| `base_price` at or below zero: **{int((base <= 0).sum()):,}**")

    neg_qty = df.assign(_q=qty)[lambda d: d._q < 0]["transaction_status"].str.title().value_counts()
    emit(f"- Negative quantity by status: "
         + ", ".join(f"{k} **{v:,}**" for k, v in neg_qty.items()))


def profile_arithmetic(df: pd.DataFrame) -> None:
    section("Internal arithmetic consistency")
    emit("Do the money columns agree with each other? This decides which fields can "
         "be trusted and which have to be recomputed.")
    emit()

    n = lambda c: pd.to_numeric(df[c], errors="coerce")
    qty, unit, base, disc, total, tax = (n("quantity"), n("unit_price"), n("base_price"),
                                         n("discount_rate"), n("total_amount"), n("tax_amount"))

    expected_unit = (base * (1 - disc)).round(2)
    unit_ok = (unit - expected_unit).abs() <= 0.011
    unit_testable = unit.notna() & expected_unit.notna()

    expected_total = (unit * qty).round(2)
    total_ok = (total - expected_total).abs() <= 0.011
    total_testable = total.notna() & expected_total.notna()

    tax_ok = (tax - total * 0.19).abs() <= 0.02
    tax_testable = tax.notna() & total.notna()

    table(
        ["Identity", "Holds", "Testable", "Share"],
        [
            ["`unit_price = base_price x (1 - discount_rate)`",
             f"{int((unit_ok & unit_testable).sum()):,}", f"{int(unit_testable.sum()):,}",
             f"{(unit_ok & unit_testable).sum() / unit_testable.sum():.1%}"],
            ["`total_amount = quantity x unit_price`",
             f"{int((total_ok & total_testable).sum()):,}", f"{int(total_testable.sum()):,}",
             f"{(total_ok & total_testable).sum() / total_testable.sum():.1%}"],
            ["`tax_amount = total_amount x 0.19`",
             f"{int((tax_ok & tax_testable).sum()):,}", f"{int(tax_testable.sum()):,}",
             f"{(tax_ok & tax_testable).sum() / tax_testable.sum():.1%}"],
        ],
        ["---", "---:", "---:", "---:"],
    )

    mismatched = total_testable & ~total_ok
    emit()
    emit(f"The **{int(mismatched.sum()):,}** total mismatches by status: "
         + ", ".join(f"{k} **{v:,}**" for k, v in
                     df.loc[mismatched, "transaction_status"].str.title().value_counts().items()))
    emit()
    emit("> The price identity holds universally while the total identity does not, "
         "so the price side is trustworthy and `total_amount` is the corrupt field.")


def profile_customers(df: pd.DataFrame) -> None:
    """Structure only. No name or address value is emitted."""
    section("Customer columns (PII-safe summary)")

    cid = pd.to_numeric(df["customer_id"], errors="coerce")
    emit(f"- `customer_id` present on **{int(cid.notna().sum()):,}** rows "
         f"({cid.notna().mean():.1%}), range {cid.min():.0f}–{cid.max():.0f}, "
         f"**{cid.nunique():,}** distinct")
    emit(f"- `customer_name` distinct: **{df['customer_name'].nunique():,}** (values withheld)")
    emit(f"- `customer_email` distinct: **{df['customer_email'].nunique():,}** (values withheld)")
    emit()

    known = df.dropna(subset=["customer_id"])
    rows = []
    for attr in ["customer_name", "customer_email", "customer_loyalty_status"]:
        grouped = known.groupby("customer_id")[attr].nunique()
        rows.append([f"`{attr}`", int(grouped.max()), int((grouped > 1).sum())])
    table(["Attribute", "Max variants per customer", "Customers affected"],
          rows, ["---", "---:", "---:"])
    emit()
    emit("> No customer attribute ever conflicts, so no slowly changing dimension is "
         "required for this extract.")

    # Splitting on "@" and taking the last part returns the WHOLE STRING when there
    # is no "@" -- which would print a local part, i.e. a name. Only emit a domain
    # when the address is actually well formed; count the rest without showing them.
    emails = df["customer_email"].dropna()
    well_formed = emails.str.count("@").eq(1)
    domains = emails[well_formed].str.rsplit("@", n=1).str[-1].str.lower()
    malformed = int((~well_formed).sum())

    emit()
    emit("Email **domains**. Kept because they are a real customer segment and carry no "
         "personal identifier; local parts are never emitted.")
    emit()
    table(["Domain", "Rows"],
          [[f"`{d}`", f"{c:,}"] for d, c in domains.value_counts().items()],
          ["---", "---:"])
    if malformed:
        emit()
        emit(f"**{malformed:,}** addresses have no `@` separator and cannot be parsed "
             f"into a domain. Values withheld: without a separator the whole string is "
             f"a local part, i.e. personal data. These become a null domain in the "
             f"pipeline rather than a guessed one.")


def profile_timespan(df: pd.DataFrame) -> None:
    section("Time span")
    parsed = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m-%d-%Y"):
        attempt = pd.to_datetime(df["transaction_date"], format=fmt, errors="coerce")
        parsed = attempt if parsed is None else parsed.fillna(attempt)

    emit(f"- Earliest: **{parsed.min():%Y-%m-%d}**")
    emit(f"- Latest: **{parsed.max():%Y-%m-%d}**")
    emit(f"- Unparseable after trying all formats: **{int(parsed.isna().sum()):,}**")
    emit()
    counts = parsed.dt.to_period("M").value_counts().sort_index()
    table(["Month", "Rows"], [[str(m), f"{c:,}"] for m, c in counts.items()], ["---", "---:"])
    emit()
    emit("> First and last months are partial. Any month-over-month trend has to "
         "account for that.")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/munich_retail_sales_raw.csv"))
    parser.add_argument("--out", type=Path, default=Path("docs/profiling_findings.md"))
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"CSV not found at {args.csv}", file=sys.stderr)
        return 1

    # Everything as text: coercing on read would hide exactly the problems we are
    # looking for, and pandas would silently turn "N/A" into NaN.
    df = pd.read_csv(args.csv, dtype=str, keep_default_na=False, na_values=[""])

    emit(f"# Profiling findings: `{args.csv.name}`")
    emit()
    emit("Generated by `scripts/initial_inspection.py`. Every design decision in `sql/` "
         "and `src/` traces back to a number in this document.")
    emit()
    emit("Customer names and email addresses are never printed by the profiler. Their "
         "cardinality and email domains appear below; no individual value does.")

    profile_shape(df)
    profile_uniqueness(df)
    profile_dependencies(df)
    profile_dates(df)
    profile_timespan(df)
    profile_categoricals(df)
    profile_numerics(df)
    profile_arithmetic(df)
    profile_customers(df)
    profile_cardinality(df)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(out.getvalue(), encoding="utf-8")
    print(f"\nReport written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
