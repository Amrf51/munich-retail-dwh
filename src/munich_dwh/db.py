"""Thin psycopg helpers.

No ORM. The DDL is the artefact being assessed here, and hiding it behind model
classes would obscure the part that matters. psycopg also gives native COPY, which
the bulk load in a later step depends on.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import settings


@contextlib.contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.dsn, autocommit=autocommit, row_factory=dict_row) as conn:
        yield conn


def wait_for_db(attempts: int = 30, delay: float = 1.0) -> None:
    """Docker reports a container as up before Postgres accepts connections, so
    retry rather than racing container startup."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with connect(autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(delay)
    raise RuntimeError(
        f"Postgres not reachable at {settings.host}:{settings.port}. Is `make up` running?"
    ) from last
