"""Command line entry point."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .db import connect, wait_for_db
from .extract import extract as run_extract
from .extract import extract as run_extract_fn
from .extract import fetch_raw, finish_batch, raw_row_count, start_batch
from .load import load_all, mart_summary, unknown_member_usage
from .migrate import run_migrations
from .transform import flag_summary
from .transform import transform as run_transform
from .transform import write_rejects, write_staging

app = typer.Typer(add_completion=False, help="Munich retail sales warehouse")
console = Console()

DEFAULT_CSV = Path("data/munich_retail_sales_raw.csv")


@app.command()
def migrate() -> None:
    """Apply SQL migrations. Safe to run repeatedly."""
    wait_for_db()
    counts = run_migrations()
    console.print(
        f"\n[bold]{counts['applied']} applied, {counts['skipped']} already present"
        + (f", {counts['changed']} changed since apply" if counts["changed"] else "")
        + "[/bold]"
    )


@app.command()
def extract(
    csv: Path = typer.Option(DEFAULT_CSV, "--csv", help="Path to the source CSV"),
    skip_migrate: bool = typer.Option(False, "--skip-migrate", help="Assume the schema exists"),
) -> None:
    """Load the CSV into raw.sales_raw. Safe to run repeatedly."""
    wait_for_db()
    if not skip_migrate:
        run_migrations()

    result = run_extract(csv)
    finish_batch(result.batch_id, extracted=result.rows_read)

    console.print(f"\nbatch [bold]{result.batch_id}[/bold] complete")
    console.print(f"raw.sales_raw now holds [bold]{raw_row_count():,}[/bold] rows")
    if result.is_noop:
        console.print("[dim]Nothing new landed: every row was already present.[/dim]")


@app.command()
def transform(batch: int = typer.Option(None, "--batch", help="Batch id to tag rows with")) -> None:
    """Transform raw.sales_raw into staging.sales_clean. Safe to run repeatedly."""
    wait_for_db()

    raw_rows = fetch_raw()
    if not raw_rows:
        console.print("[yellow]raw.sales_raw is empty. Run `make extract` first.[/yellow]")
        raise typer.Exit(code=1)

    clean, rejects = run_transform(raw_rows, batch_id=batch)
    staged = write_staging(clean)
    rejected = write_rejects(rejects, batch)

    console.print(f"\nstaged [bold]{staged:,}[/bold] rows, quarantined [bold]{rejected:,}[/bold]")

    summary = flag_summary()
    if summary:
        table = Table(title="Quality flags")
        table.add_column("Flag")
        table.add_column("Rows", justify="right")
        table.add_column("% of staging", justify="right")
        for name, count in summary:
            table.add_row(name, f"{count:,}", f"{100 * count / staged:.1f}%")
        console.print(table)
        console.print(
            "[dim]Flagged rows are loaded, not deleted. mart.vw_net_revenue "
            "decides what to exclude.[/dim]"
        )


@app.command()
def load() -> None:
    """Load staging into the mart dimensions and fact. Safe to run repeatedly."""
    wait_for_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM staging.sales_clean")
        if cur.fetchone()["n"] == 0:
            console.print("[yellow]staging.sales_clean is empty. Run `make transform` first.[/yellow]")
            raise typer.Exit(code=1)

    load_all()
    _print_mart_summary()


@app.command()
def run(
    csv: Path = typer.Option(DEFAULT_CSV, "--csv", help="Path to the source CSV"),
) -> None:
    """The whole pipeline: migrate, extract, transform, load."""
    if not csv.exists():
        raise typer.BadParameter(f"CSV not found at {csv}")

    wait_for_db()
    run_migrations()

    batch_id = start_batch(csv.name)
    console.rule(f"batch {batch_id}")

    extracted = run_extract_fn(csv, batch_id=batch_id)
    clean, rejects = run_transform(fetch_raw(), batch_id=batch_id)
    staged = write_staging(clean)
    rejected = write_rejects(rejects, batch_id)
    load_all()

    finish_batch(batch_id, extracted=extracted.rows_read, staged=staged, rejected=rejected)

    console.rule("mart")
    _print_mart_summary()
    console.print("\n[green]Pipeline complete.[/green] "
                  "Query [bold]mart.vw_net_revenue[/bold] to start.")


def _print_mart_summary() -> None:
    table = Table(title="Mart")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for r in mart_summary():
        table.add_row(r["table_name"], f'{r["rows"]:,}')
    console.print(table)

    usage = [r for r in unknown_member_usage() if r["unknown_rows"]]
    if usage:
        table = Table(title="Rows resolved to the Unknown member")
        table.add_column("Dimension")
        table.add_column("Rows", justify="right")
        table.add_column("Share", justify="right")
        for r in usage:
            table.add_row(r["dimension"], f'{r["unknown_rows"]:,}',
                          f'{100 * r["unknown_rows"] / r["total_rows"]:.1f}%')
        console.print(table)


@app.command()
def inspect() -> None:
    """Show what the schema currently contains."""
    wait_for_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema IN ('raw', 'staging', 'mart', 'meta')
            ORDER BY table_schema, table_type DESC, table_name
        """)
        rows = cur.fetchall()

        table = Table(title="Schema contents")
        table.add_column("Schema")
        table.add_column("Object")
        table.add_column("Type")
        table.add_column("Rows", justify="right")
        for r in rows:
            qualified = f'{r["table_schema"]}.{r["table_name"]}'
            if r["table_type"] == "BASE TABLE":
                cur.execute(f"SELECT COUNT(*) AS n FROM {qualified}")
                count = f'{cur.fetchone()["n"]:,}'
            else:
                count = "-"
            table.add_row(r["table_schema"], r["table_name"],
                          "view" if r["table_type"] == "VIEW" else "table", count)
        console.print(table)


@app.command()
def batches(limit: int = typer.Option(10, "--limit")) -> None:
    """Show recent load batches."""
    wait_for_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT batch_id, source_file, started_at, rows_extracted, status
                 FROM meta.load_batch ORDER BY batch_id DESC LIMIT %s""",
            (limit,),
        )
        table = Table(title="Load batches")
        for col in ("Batch", "Source", "Started", "Rows", "Status"):
            table.add_column(col)
        for r in cur.fetchall():
            table.add_row(
                str(r["batch_id"]), r["source_file"],
                r["started_at"].strftime("%Y-%m-%d %H:%M:%S"),
                f'{r["rows_extracted"]:,}' if r["rows_extracted"] else "-",
                r["status"],
            )
        console.print(table)


if __name__ == "__main__":
    app()