"""Read the authoritative inventory without initializing or mutating a journal."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.infrastructure.sqlite.assignments import Assignments


from typing import cast, Sequence
from orchestrator.domain.records import InventoryRecord
from orchestrator.domain.inventory import NodeSettings
import sqlite3
from pathlib import Path


def read_inventory(path):
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


class InventoryRepository:
    def __init__(self, store: Assignments) -> None:
        self.store = store

    def seed(self, initial: Sequence[NodeSettings]) -> None:
        from orchestrator.infrastructure.sqlite.migrations import initialize_registry

        initialize_registry(self.store)
        with self.store.db() as db:
            if not db.execute("SELECT 1 FROM registry_meta WHERE id=1").fetchone():
                for node in initial:
                    db.execute(
                        "INSERT INTO node_registry VALUES (?,1,?)",
                        (node.id, node.model_dump_json()),
                    )
                db.execute("INSERT INTO registry_meta VALUES (1,1)")

    def records(self) -> list[InventoryRecord]:
        with self.store.db() as db:
            return [
                cast(InventoryRecord, dict(r))
                for r in db.execute("SELECT * FROM node_registry ORDER BY id")
            ]

    def pinned(self, node_id: str) -> bool:
        with self.store.db() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM assignments WHERE node_id=? UNION SELECT 1 FROM switches WHERE target_node=? OR source_node=? UNION SELECT 1 FROM control_leases WHERE node_id=? LIMIT 1",
                    (node_id, node_id, node_id, node_id),
                ).fetchone()
            )

    def save(self, node_id: str, revision: int, configuration: str) -> None:
        with self.store.db() as db:
            db.execute(
                "INSERT INTO node_registry VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,configuration=excluded.configuration",
                (node_id, revision, configuration),
            )
