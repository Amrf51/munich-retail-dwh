"""Command line entry point."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .db import connect, wait_for_db
from .extract import extract as run_extract
from .extract import finish_batch, raw_row_count
from .migrate import run_migrations

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