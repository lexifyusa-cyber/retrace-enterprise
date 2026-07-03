#!/usr/bin/env python3
"""Copy FastAPI app data from SQLite into an Alembic-created PostgreSQL DB.

Usage:
  DATABASE_URL=postgresql+asyncpg://retrace:retrace_dev_password@localhost:5432/retrace \
    python scripts/migrate_sqlite_to_postgres.py --sqlite retrace.db --truncate

This script does not create schema. Run `alembic upgrade head` first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import asyncpg

DEFAULT_ORDER = [
    "tenants", "users", "teams", "pods", "team_access", "connections",
    "folder_groups", "folder_paths", "training_jobs",
    "email_connections", "email_inboxes", "email_messages", "channel_connections",
    "conversations", "conversation_messages", "terminal_sessions",
    "agent_sessions", "sops", "documentation", "automation_runs",
    "query_history", "llm_settings", "llm_logs", "mcp_servers", "mcp_extensions",
    "user_extensions", "user_sessions", "password_history", "audit_logs",
    "scheduled_followups", "tool_monitors", "remote_triggers",
    "chunk_records", "knowledge_bases", "encrypted_chunks",
    "kb_folders", "kb_files", "kb_chunks", "kb_ingestion_runs",
    "teamknowparse_kb_folders", "teamknowparse_kb_files",
    "teamknowparse_kb_chunks", "teamknowparse_kb_ingestion_runs",
]


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalize_pg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


async def pg_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        """
    )
    return {row["tablename"] for row in rows}


async def pg_columns(conn: asyncpg.Connection, table: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
        """,
        table,
    )
    return [dict(row) for row in rows]


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({qident(table)})").fetchall()]


def coerce_value(value: Any, column: dict[str, Any]) -> Any:
    if value is None:
        return None
    udt = column["udt_name"]
    dtype = column["data_type"]
    if dtype == "boolean":
        if isinstance(value, str):
            return value.lower() in {"1", "t", "true", "yes", "y"}
        return bool(value)
    if dtype.startswith("timestamp"):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dtype == "date":
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
    if udt in {"json", "jsonb"}:
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        if value == "":
            return None
        # Validate JSON and preserve normalized representation.
        return json.dumps(json.loads(value))
    if udt == "bytea" and isinstance(value, memoryview):
        return bytes(value)
    return value


def placeholder(index: int, column: dict[str, Any]) -> str:
    marker = f"${index}"
    udt = column["udt_name"]
    if udt in {"json", "jsonb"}:
        return f"{marker}::{udt}"
    return marker


async def table_has_rows(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {qident(table)} LIMIT 1)"))


async def copy_table(sqlite_conn: sqlite3.Connection, pg_conn: asyncpg.Connection, table: str) -> int:
    source_cols = sqlite_columns(sqlite_conn, table)
    target_cols_meta = await pg_columns(pg_conn, table)
    target_by_name = {c["column_name"]: c for c in target_cols_meta}
    cols = [c for c in source_cols if c in target_by_name]
    if not cols:
        return 0

    rows = sqlite_conn.execute(
        f"SELECT {', '.join(qident(c) for c in cols)} FROM {qident(table)}"
    ).fetchall()
    if not rows:
        return 0

    casts = [placeholder(i + 1, target_by_name[col]) for i, col in enumerate(cols)]
    sql = (
        f"INSERT INTO {qident(table)} ({', '.join(qident(c) for c in cols)}) "
        f"VALUES ({', '.join(casts)}) ON CONFLICT DO NOTHING"
    )
    payload = [
        tuple(coerce_value(row[i], target_by_name[col]) for i, col in enumerate(cols))
        for row in rows
    ]
    await pg_conn.executemany(sql, payload)
    return len(payload)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="Path to source retrace.db")
    parser.add_argument("--postgres", default=None, help="PostgreSQL URL; defaults to DATABASE_URL")
    parser.add_argument("--truncate", action="store_true", help="TRUNCATE target tables before copy")
    args = parser.parse_args()

    import os
    postgres_url = args.postgres or os.environ.get("DATABASE_URL")
    if not postgres_url:
        raise SystemExit("Provide --postgres or DATABASE_URL")
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite DB not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = await asyncpg.connect(normalize_pg_url(postgres_url))
    try:
        source_tables = sqlite_tables(sqlite_conn)
        target_tables = await pg_tables(pg_conn)
        tables = [t for t in DEFAULT_ORDER if t in source_tables and t in target_tables]
        extra = sorted((source_tables & target_tables) - set(tables))
        tables.extend(extra)

        if args.truncate:
            await pg_conn.execute(
                "TRUNCATE " + ", ".join(qident(t) for t in reversed(tables)) + " RESTART IDENTITY CASCADE"
            )
        else:
            occupied = [t for t in tables if await table_has_rows(pg_conn, t)]
            if occupied:
                raise SystemExit(
                    "Target DB is not empty. Re-run with --truncate or use an empty Alembic-created DB. "
                    f"Non-empty tables: {', '.join(occupied[:10])}"
                )

        total = 0
        async with pg_conn.transaction():
            for table in tables:
                copied = await copy_table(sqlite_conn, pg_conn, table)
                total += copied
                print(f"{table}: copied {copied}")
        print(f"Done. Copied {total} rows across {len(tables)} tables.")
    finally:
        await pg_conn.close()
        sqlite_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
