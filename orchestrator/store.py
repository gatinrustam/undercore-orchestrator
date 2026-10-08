import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from .models import ConnectionIntent, Observation, PilotError, ProvisionedClient


class Store:
    def __init__(self, directory: Path, encryption_key: bytes):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("State directory must be private (0700)")
        self.path = directory / "connections.sqlite3"
        if self.path.is_symlink():
            raise ValueError("State database must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("State database must be private (0600)")
        self.cipher = Fernet(encryption_key)
        with self.transaction() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS connections (
                    device_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, node_id TEXT NOT NULL, server_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'ready', 'uncertain')),
                    created_at REAL NOT NULL, ciphertext BLOB
                )
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, device_id: str):
        with self.transaction() as db:
            return db.execute("SELECT * FROM connections WHERE device_id = ?", (device_id,)).fetchone()

    def reserve(self, intent: ConnectionIntent, observations: list[Observation]):
        """Persist before POST; competing workers share the same reservation.

        Pending/uncertain nodes are quarantined for new creates until reconciled.
        Upstream has no shared mutation journal; do not allow concurrent creates
        from this orchestrator to the same node.
        """
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM connections WHERE device_id = ?", (intent.device_id,),
            ).fetchone()
            if existing is not None:
                return existing, False

            candidates = []
            for observation in observations:
                node = observation.node
                if node.mode != "active" or (intent.region and node.region != intent.region):
                    continue
                if not 0 <= time.time() - observation.started_at <= 30:
                    continue
                rows = db.execute(
                    "SELECT state, created_at FROM connections WHERE node_id = ?", (node.id,),
                ).fetchall()
                if any(row["state"] != "ready" for row in rows):
                    continue
                # Account conservatively for creates since the health request
                # began; observed peers include manual/third-party peers too.
                new_count = sum(row["created_at"] >= observation.started_at for row in rows)
                occupied = max(len(rows), observation.total_peers + new_count)
                capacity = min(node.capacity, observation.max_peers)
                if occupied < capacity:
                    candidates.append((occupied / capacity, node.id, node.server_id))

            if not candidates:
                raise PilotError("no_eligible_node", 503)
            _, node_id, server_id = min(candidates)
            db.execute("""
                INSERT INTO connections
                (device_id, owner_id, expires_at, node_id, server_id, operation_id, state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (
                intent.device_id, intent.owner_id, intent.expires_at, node_id, server_id,
                uuid.uuid4().hex, time.time(),
            ))
            return db.execute(
                "SELECT * FROM connections WHERE device_id = ?", (intent.device_id,),
            ).fetchone(), True

    @staticmethod
    def binding(row):
        return {key: row[key] for key in (
            "owner_id", "device_id", "operation_id", "node_id", "server_id", "expires_at",
        )}

    def finish(self, row, client: ProvisionedClient):
        payload = {"binding": self.binding(row), "peer_id": client.peer_id, "config": client.config}
        ciphertext = self.cipher.encrypt(json.dumps(payload).encode())
        with self.transaction() as db:
            changed = db.execute("""
                UPDATE connections SET state = 'ready', ciphertext = ?
                WHERE operation_id = ? AND state = 'pending'
            """, (ciphertext, row["operation_id"])).rowcount
            if changed != 1:
                raise PilotError("provisioning_uncertain")

    def uncertain(self, operation_id: str):
        with self.transaction() as db:
            db.execute("""
                UPDATE connections SET state = 'uncertain'
                WHERE operation_id = ? AND state = 'pending'
            """, (operation_id,))

    def read_client(self, row) -> ProvisionedClient:
        try:
            payload = json.loads(self.cipher.decrypt(row["ciphertext"]))
            if payload["binding"] != self.binding(row):
                raise ValueError()
            return ProvisionedClient(payload["peer_id"], payload["config"])
        except Exception:
            raise PilotError("stored_configuration_unavailable", 503) from None
