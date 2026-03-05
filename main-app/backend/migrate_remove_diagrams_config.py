#!/usr/bin/env python3
"""
One-off migration: set folder_groups with group_type 'diagrams' or 'configuration'
to 'documentation' and 'code' so they train and display as doc/code only.

Run from backend dir: python migrate_remove_diagrams_config.py
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path so app imports work
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select, update
from app.db.database import async_session_maker
from app.models.folder_group import FolderGroup


async def run():
    async with async_session_maker() as session:
        # Count before
        r = await session.execute(
            select(FolderGroup).where(
                FolderGroup.group_type.in_(["diagrams", "configuration"])
            )
        )
        to_update = list(r.scalars().all())
        if not to_update:
            print("No folder_groups with group_type 'diagrams' or 'configuration'. Nothing to do.")
            return

        # Update diagrams → documentation
        r1 = await session.execute(
            update(FolderGroup)
            .where(FolderGroup.group_type == "diagrams")
            .values(group_type="documentation")
        )
        # Update configuration → code
        r2 = await session.execute(
            update(FolderGroup)
            .where(FolderGroup.group_type == "configuration")
            .values(group_type="code")
        )
        await session.commit()
        print(f"Updated {r1.rowcount} group(s) from 'diagrams' → 'documentation'")
        print(f"Updated {r2.rowcount} group(s) from 'configuration' → 'code'")
        print("Done.")


if __name__ == "__main__":
    asyncio.run(run())
