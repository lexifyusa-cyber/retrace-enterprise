#!/usr/bin/env python3
"""Migration script to rename Product → Team across tables and columns.

Renames:
  - table products              → teams
  - table product_access        → team_access
  - column products.product_id  → teams.team_id
  - column products.product_name→ teams.team_name
  - column folder_groups.product_id → team_id
  - column pods.product_id      → team_id
  - column team_access.product_id → team_id

Idempotent: each step checks existence before running. Requires SQLite 3.25+
for ALTER TABLE ... RENAME COLUMN.
"""

import sqlite3
import sys
from pathlib import Path

db_path = Path(__file__).parent / "retrace.db"

if not db_path.exists():
    print(f"Database not found at {db_path}")
    print("Database will be created automatically on next app start.")
    sys.exit(0)

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()


def table_exists(name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cursor.fetchone() is not None


def column_exists(table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


try:
    # 1. Rename table products -> teams
    if table_exists("products") and not table_exists("teams"):
        print("Renaming table products -> teams...")
        cursor.execute("ALTER TABLE products RENAME TO teams")
        print("  ✓ renamed")
    elif table_exists("teams"):
        print("✓ teams table already exists")
    else:
        print("ℹ neither products nor teams table exists — will be created on next app start")

    # 2. Rename table product_access -> team_access
    if table_exists("product_access") and not table_exists("team_access"):
        print("Renaming table product_access -> team_access...")
        cursor.execute("ALTER TABLE product_access RENAME TO team_access")
        print("  ✓ renamed")
    elif table_exists("team_access"):
        print("✓ team_access table already exists")
    else:
        print("ℹ neither product_access nor team_access table exists")

    # 3. Rename column teams.product_id -> team_id
    if table_exists("teams"):
        if column_exists("teams", "product_id") and not column_exists("teams", "team_id"):
            print("Renaming teams.product_id -> team_id...")
            cursor.execute("ALTER TABLE teams RENAME COLUMN product_id TO team_id")
            print("  ✓ renamed")
        elif column_exists("teams", "team_id"):
            print("✓ teams.team_id already exists")

    # 4. Rename column teams.product_name -> team_name
    if table_exists("teams"):
        if column_exists("teams", "product_name") and not column_exists("teams", "team_name"):
            print("Renaming teams.product_name -> team_name...")
            cursor.execute("ALTER TABLE teams RENAME COLUMN product_name TO team_name")
            print("  ✓ renamed")
        elif column_exists("teams", "team_name"):
            print("✓ teams.team_name already exists")

    # 5. Rename column folder_groups.product_id -> team_id
    if table_exists("folder_groups"):
        if column_exists("folder_groups", "product_id") and not column_exists("folder_groups", "team_id"):
            print("Renaming folder_groups.product_id -> team_id...")
            cursor.execute("ALTER TABLE folder_groups RENAME COLUMN product_id TO team_id")
            print("  ✓ renamed")
        elif column_exists("folder_groups", "team_id"):
            print("✓ folder_groups.team_id already exists")

    # 6. Rename column pods.product_id -> team_id
    if table_exists("pods"):
        if column_exists("pods", "product_id") and not column_exists("pods", "team_id"):
            print("Renaming pods.product_id -> team_id...")
            cursor.execute("ALTER TABLE pods RENAME COLUMN product_id TO team_id")
            print("  ✓ renamed")
        elif column_exists("pods", "team_id"):
            print("✓ pods.team_id already exists")

    # 7. Rename column team_access.product_id -> team_id
    if table_exists("team_access"):
        if column_exists("team_access", "product_id") and not column_exists("team_access", "team_id"):
            print("Renaming team_access.product_id -> team_id...")
            cursor.execute("ALTER TABLE team_access RENAME COLUMN product_id TO team_id")
            print("  ✓ renamed")
        elif column_exists("team_access", "team_id"):
            print("✓ team_access.team_id already exists")

    # 8. Rename column tenants.max_products_per_user_admin -> max_teams_per_user_admin
    if table_exists("tenants"):
        if column_exists("tenants", "max_products_per_user_admin") and not column_exists("tenants", "max_teams_per_user_admin"):
            print("Renaming tenants.max_products_per_user_admin -> max_teams_per_user_admin...")
            cursor.execute("ALTER TABLE tenants RENAME COLUMN max_products_per_user_admin TO max_teams_per_user_admin")
            print("  ✓ renamed")
        elif column_exists("tenants", "max_teams_per_user_admin"):
            print("✓ tenants.max_teams_per_user_admin already exists")

    # 9. Rename column users.max_products -> max_teams
    if table_exists("users"):
        if column_exists("users", "max_products") and not column_exists("users", "max_teams"):
            print("Renaming users.max_products -> max_teams...")
            cursor.execute("ALTER TABLE users RENAME COLUMN max_products TO max_teams")
            print("  ✓ renamed")
        elif column_exists("users", "max_teams"):
            print("✓ users.max_teams already exists")

    # 10. Rename product_id -> team_id across remaining entity tables
    for tbl in (
        "agent_sessions",
        "conversations",
        "sops",
        "channel_connections",
        "email_inboxes",
        "email_messages",
        "documentation",
        "knowledge_bases",
        "chunk_records",
        "encrypted_chunks",
        "automation_runs",
    ):
        if table_exists(tbl):
            if column_exists(tbl, "product_id") and not column_exists(tbl, "team_id"):
                print(f"Renaming {tbl}.product_id -> team_id...")
                cursor.execute(f"ALTER TABLE {tbl} RENAME COLUMN product_id TO team_id")
                print("  ✓ renamed")
            elif column_exists(tbl, "team_id"):
                print(f"✓ {tbl}.team_id already exists")

    # 10b. Rename mcp_servers.product_name -> team_name and kb_product_id -> kb_team_id
    if table_exists("mcp_servers"):
        if column_exists("mcp_servers", "product_name") and not column_exists("mcp_servers", "team_name"):
            print("Renaming mcp_servers.product_name -> team_name...")
            cursor.execute("ALTER TABLE mcp_servers RENAME COLUMN product_name TO team_name")
            print("  ✓ renamed")
        elif column_exists("mcp_servers", "team_name"):
            print("✓ mcp_servers.team_name already exists")
        if column_exists("mcp_servers", "kb_product_id") and not column_exists("mcp_servers", "kb_team_id"):
            print("Renaming mcp_servers.kb_product_id -> kb_team_id...")
            cursor.execute("ALTER TABLE mcp_servers RENAME COLUMN kb_product_id TO kb_team_id")
            print("  ✓ renamed")
        elif column_exists("mcp_servers", "kb_team_id"):
            print("✓ mcp_servers.kb_team_id already exists")

    # 11. channel_connections: platform 'team_id' field collided with entity rename.
    # If the old schema still has a separate platform team_id (before step 10 applied)
    # we already renamed it; after step 10 the platform id becomes ms_team_id.
    # Handle pre-Phase-3 schemas that still carry the platform column as 'team_id'
    # alongside a newly-created 'team_id' entity column — unlikely, but guard anyway.
    if table_exists("channel_connections"):
        if column_exists("channel_connections", "team_id") and not column_exists(
            "channel_connections", "ms_team_id"
        ):
            # There is only one team_id column — it's the entity FK from step 10. No-op.
            pass

    conn.commit()
    conn.close()
    print("\nMigration completed successfully!")

except Exception as e:
    print(f"Error: {e}")
    conn.rollback()
    conn.close()
    sys.exit(1)
