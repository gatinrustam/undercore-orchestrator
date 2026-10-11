"""Offline upgrade rehearsal on a disposable SQLite copy. Never contact nodes."""

import sqlite3
from pathlib import Path
from orchestrator.infrastructure.sqlite.assignments import Assignments


def rows(directory):
    with sqlite3.connect(Path(directory) / "assignments.sqlite3") as db:
        db.row_factory = sqlite3.Row
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: [
                dict(r)
                for r in db.execute(
                    'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'
                )
            ]
            for name in tables
        }


def rehearse(directory):
    before = rows(directory)
    Assignments(directory)
    after = rows(directory)
    for table, records in before.items():
        if len(after.get(table, [])) != len(records):
            raise ValueError("migration_changed_existing_rows")
        for old, new in zip(records, after[table]):
            for key, value in old.items():
                if table == "assignments" and key in ("device_id", "binding_key") and value == "":
                    value = old["external_id"]  # The documented legacy identity backfill.
                if new.get(key) != value:
                    raise ValueError("migration_changed_existing_rows")
