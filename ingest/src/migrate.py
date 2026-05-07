"""Idempotent migration runner.

Two-phase startup against the existing pgvector postgres instance:

1. Connect to the admin DB (via DB_ADMIN_URL) and CREATE the
   `affine_ingest` database if missing.
2. Connect to `affine_ingest` (via DATABASE_URL) and apply every
   .sql file under migrations/ in lexical order.

Re-running is safe: `CREATE DATABASE` is gated by an existence check;
all migration SQL uses `IF NOT EXISTS`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

INGEST_DB = "affine_ingest"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def ensure_database(admin_url: str) -> None:
    """Create the affine_ingest database if it doesn't exist."""
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", INGEST_DB
        )
        if exists:
            print(f"Database {INGEST_DB!r} already exists.")
            return
        # CREATE DATABASE cannot run inside a transaction block.
        await conn.execute(f'CREATE DATABASE "{INGEST_DB}"')
        print(f"Created database {INGEST_DB!r}.")
    finally:
        await conn.close()


async def apply_migrations(target_url: str) -> None:
    """Apply every *.sql under migrations/ in filename order."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migration files found in {MIGRATIONS_DIR}")

    conn = await asyncpg.connect(target_url)
    try:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            print(f"Applying {path.name} ({len(sql)} chars)")
            await conn.execute(sql)
        print(f"Applied {len(files)} migration file(s).")
    finally:
        await conn.close()


async def main() -> None:
    admin_url = os.environ.get("DB_ADMIN_URL")
    target_url = os.environ.get("DATABASE_URL")
    if not admin_url:
        raise SystemExit("DB_ADMIN_URL is required")
    if not target_url:
        raise SystemExit("DATABASE_URL is required")

    await ensure_database(admin_url)
    await apply_migrations(target_url)
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(main())
