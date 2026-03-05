#!/usr/bin/env python3
"""Migration script to add product_id column to folder_groups table."""

import sqlite3
import sys
from pathlib import Path

# Get database path
db_path = Path(__file__).parent / "retrace.db"

if not db_path.exists():
    print(f"Database not found at {db_path}")
    print("Database will be created automatically on next app start.")
    sys.exit(0)

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

try:
    # Check if product_id column exists
    cursor.execute("PRAGMA table_info(folder_groups)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'product_id' not in columns:
        print("Adding product_id column to folder_groups table...")
        cursor.execute("ALTER TABLE folder_groups ADD COLUMN product_id VARCHAR(36)")
        conn.commit()
        print("✓ Column added successfully")
    else:
        print("✓ product_id column already exists")

    # Check if products table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
    if not cursor.fetchone():
        print("ℹ Products table does not exist - it will be created on next app start")
    else:
        print("✓ Products table exists")

    conn.close()
    print("\nMigration completed successfully!")
    
except Exception as e:
    print(f"Error: {e}")
    conn.rollback()
    conn.close()
    sys.exit(1)
