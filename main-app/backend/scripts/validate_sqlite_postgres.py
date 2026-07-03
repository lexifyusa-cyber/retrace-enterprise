#!/usr/bin/env python3
"""Validate row-count parity between source SQLite and target PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import asyncpg

JSON_COLUMNS = {
    "audit_logs": ["details"],
    "chunk_records": ["metadata_json", "embedding"],
    "conversation_messages": ["meta_json"],
    "folder_groups": ["metadata_json"],
    "folder_paths": ["file_filters"],
    "kb_chunks": ["metadata_json"],
    "kb_files": ["source_json"],
    "kb_folders": ["source_json"],
    "kb_ingestion_runs": ["metrics_json", "validation_json"],
    "mcp_extensions": ["args_json", "env_vars_json", "auth_config_json"],
    "mcp_servers": ["mcp_config_json", "selected_endpoints_json"],
    "pods": ["metadata_json"],
    "query_history": ["scope_filter", "sources"],
    "scheduled_followups": ["trigger_config"],
    "sops": ["sop_json", "schedule_config"],
    "teamknowparse_kb_chunks": ["metadata_json"],
    "teamknowparse_kb_files": ["source_json"],
    "teamknowparse_kb_folders": ["source_json"],
    "teamknowparse_kb_ingestion_runs": ["metrics_json", "validation_json"],
    "teams": ["metadata_json"],
    "tenants": ["ldap_config", "smtp_config", "azure_ad_config", "google_config", "password_policy", "ip_allowlist", "ip_denylist"],
    "tool_monitors": ["target_json"],
    "training_jobs": ["progress_data", "statistics"],
    "user_extensions": ["env_vars_json", "auth_config_json"],
    "users": ["preferences"],
}


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalize_pg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }


async def pg_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows}


def validate_source_json(conn: sqlite3.Connection, tables: set[str]) -> list[str]:
    errors: list[str] = []
    for table, columns in JSON_COLUMNS.items():
        if table not in tables:
            continue
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({qident(table)})")}
        for column in columns:
            if column not in existing:
                continue
            rows = conn.execute(
                f"SELECT rowid, {qident(column)} FROM {qident(table)} WHERE {qident(column)} IS NOT NULL"
            ).fetchall()
            for rowid, value in rows:
                if value == "":
                    continue
                try:
                    json.loads(value)
                except Exception as exc:
                    errors.append(f"{table}.{column} rowid={rowid}: {exc}")
    return errors


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--postgres", default=None)
    args = parser.parse_args()

    postgres_url = args.postgres or os.environ.get("DATABASE_URL")
    if not postgres_url:
        raise SystemExit("Provide --postgres or DATABASE_URL")
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite DB not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    pg_conn = await asyncpg.connect(normalize_pg_url(postgres_url))
    failed = False
    try:
        source_tables = sqlite_tables(sqlite_conn)
        target_tables = await pg_tables(pg_conn)
        common = sorted(source_tables & target_tables)
        missing_target = sorted(source_tables - target_tables)
        if missing_target:
            print("Tables present only in SQLite:", ", ".join(missing_target))

        json_errors = validate_source_json(sqlite_conn, source_tables)
        if json_errors:
            failed = True
            print("Invalid SQLite JSON values:")
            for err in json_errors[:50]:
                print("  " + err)

        for table in common:
            sqlite_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0]
            pg_count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {qident(table)}")
            status = "OK" if sqlite_count == pg_count else "MISMATCH"
            print(f"{status} {table}: sqlite={sqlite_count} postgres={pg_count}")
            failed = failed or sqlite_count != pg_count
    finally:
        await pg_conn.close()
        sqlite_conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
