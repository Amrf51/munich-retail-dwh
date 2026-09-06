"""Environment-driven configuration. Nothing is hardcoded to one machine."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = PROJECT_ROOT / "sql"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("POSTGRES_HOST", "localhost")
    port: int = int(os.getenv("POSTGRES_PORT", "5433"))
    user: str = os.getenv("POSTGRES_USER", "dwh")
    password: str = os.getenv("POSTGRES_PASSWORD", "dwh")
    database: str = os.getenv("POSTGRES_DB", "munich_retail")

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


settings = Settings()
