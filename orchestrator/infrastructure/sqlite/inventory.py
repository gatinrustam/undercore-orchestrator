"""Read the authoritative inventory without initializing or mutating a journal."""

import sqlite3
from pathlib import Path


def read_inventory(path):
    from orchestrator.config.settings import NodeSettings

    path = Path(path)
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='registry_meta'").fetchone():
            return None
        if not db.execute("SELECT 1 FROM registry_meta WHERE id=1").fetchone():
            return None
        return [
            NodeSettings.model_validate_json(row[0])
            for row in db.execute("SELECT configuration FROM node_registry ORDER BY id")
        ]
