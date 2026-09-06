"""Command line entry point."""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .db import connect, wait_for_db
from .migrate import run_migrations

app = typer.Typer(add_completion=False, help="Munich retail sales warehouse")
console = Console()


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


if __name__ == "__main__":
    app()
