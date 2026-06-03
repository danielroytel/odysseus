#!/usr/bin/env python3
"""One-shot backfill: set folder='Tasks' on existing task/research sessions.

Usage:
    python scripts/backfill_task_folders.py [--dry-run]
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.database import SessionLocal, Session as DbSession


def main(dry_run: bool = False):
    db = SessionLocal()
    try:
        rows = (
            db.query(DbSession)
            .filter(
                DbSession.folder == None,  # noqa: E711
                (DbSession.name.like("[Task] %") | DbSession.name.like("[Research] %")),
            )
            .all()
        )
        print(f"Found {len(rows)} task/research sessions without folder")
        for row in rows:
            print(f"  {row.id[:12]}  {row.name}")
            if not dry_run:
                row.folder = "Tasks"
        if not dry_run and rows:
            db.commit()
            print(f"Updated {len(rows)} sessions")
        elif dry_run:
            print("(dry run — no changes made)")
    finally:
        db.close()


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
