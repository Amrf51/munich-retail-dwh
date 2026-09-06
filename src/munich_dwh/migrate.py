"""Numbered, checksummed, idempotent migrations.

The brief asks for scripts that can be run repeatedly without error. Two mechanisms
combine to deliver that:

1. Every statement in sql/ is individually idempotent -- CREATE TABLE IF NOT EXISTS,
   CREATE OR REPLACE VIEW, INSERT ... ON CONFLICT DO NOTHING. Running any single file
   twice is harmless on its own.

2. A meta.schema_migrations ledger records which files have been applied, so the
   runner skips them entirely on a second pass. This is what makes the behaviour
   predictable rather than merely tolerable: applying nothing is different from
   applying everything again and happening to get away with it.

Each file runs inside its own transaction, so a failure halfway through leaves the
database in the state it was in before that file started.

Checksums exist to catch a specific mistake: editing a file that has already been
applied. The edit will not be picked up, and without a checksum the divergence is
silent. The runner reports it and tells you to add a new numbered file instead.
"""
from __future__ import annotations

import hashlib

from rich.console import Console

from .config import SQL_DIR
from .db import connect

console = Console()

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_migrations() -> dict[str, int]:
    """Apply every unapplied file in sql/, in filename order."""
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"No .sql files found in {SQL_DIR}")

    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute("SELECT filename, checksum FROM meta.schema_migrations")
        applied = {r["filename"]: r["checksum"] for r in cur.fetchall()}

    counts = {"applied": 0, "skipped": 0, "changed": 0}

    for path in files:
        body = path.read_text(encoding="utf-8")
        digest = _checksum(body)

        if path.name in applied:
            if applied[path.name] != digest:
                counts["changed"] += 1
                console.print(
                    f"[yellow]![/yellow] {path.name} has changed since it was applied "
                    f"(recorded {applied[path.name]}, now {digest}). Not re-run. "
                    f"Add a new numbered file rather than editing an applied one."
                )
            else:
                counts["skipped"] += 1
                console.print(f"[dim]=[/dim] {path.name} [dim]already applied[/dim]")
            continue

        # One transaction per file: a failure rolls back that file completely, and
        # the ledger row is written in the same transaction as the DDL, so the
        # ledger can never claim a file was applied when it was not.
        with connect() as conn, conn.cursor() as cur:
            cur.execute(body)
            cur.execute(
                "INSERT INTO meta.schema_migrations (filename, checksum) VALUES (%s, %s)",
                (path.name, digest),
            )
            conn.commit()
        counts["applied"] += 1
        console.print(f"[green]+[/green] applied {path.name}")

    return counts
