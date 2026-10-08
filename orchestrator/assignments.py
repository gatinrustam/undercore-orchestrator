"""Durable routing journal; contains no VPN keys or configurations."""
import os
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .models import PilotError

class Assignments:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError('Private state directory required')
        self.path = directory / 'assignments.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError('Private database required')
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS assignments (\n                external_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,\n                node_id TEXT NOT NULL, server_id TEXT NOT NULL,\n                creation_expires_at TEXT NOT NULL, remote_id TEXT,\n                created_at REAL NOT NULL, last_operation TEXT, last_outcome TEXT, checked_at REAL)')
            # Additive migration: existing aliases, external IDs and remote peers stay intact.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(assignments)')}
            additions = {'device_id': "TEXT NOT NULL DEFAULT ''",
                         'protocol': "TEXT NOT NULL DEFAULT 'amneziawg'",
                         'configuration_revision': 'INTEGER NOT NULL DEFAULT 0',
                         'configuration_digest': 'TEXT', 'binding_key': "TEXT NOT NULL DEFAULT ''"}
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE assignments ADD COLUMN {name} {definition}')
            db.execute("UPDATE assignments SET device_id=external_id WHERE device_id=''")
            db.execute("UPDATE assignments SET binding_key=external_id WHERE binding_key=''")
            db.execute('CREATE TABLE IF NOT EXISTS switches (client_id TEXT NOT NULL, operation_id TEXT NOT NULL, source_node TEXT NOT NULL, target_node TEXT NOT NULL, target_server TEXT NOT NULL, binding_key TEXT NOT NULL, name TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, disable_requested INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(client_id, operation_id))')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending_switch ON switches(client_id) WHERE state NOT IN ('complete', 'cancelled')")
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS assignments_device_protocol ON assignments(device_id, protocol)')

        from .leases import initialize
        initialize(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM assignments ORDER BY created_at')]

    def get(self, external_id=None, client_id=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM assignments WHERE external_id=?' if external_id is not None
                             else 'SELECT * FROM assignments WHERE client_id=?',
                             (external_id if external_id is not None else client_id,)).fetchone()
            return dict(row) if row else None

    def reserve(self, payload, observations, device_id=None, protocol="amneziawg"):
        device_id = device_id or payload["external_id"]
        with self.db() as db:
            existing = db.execute('SELECT * FROM assignments WHERE external_id=?', (payload['external_id'],)).fetchone()
            if existing:
                if existing['device_id'] != device_id or existing['protocol'] != protocol:
                    raise PilotError('assignment_identity_conflict', 409)
                return dict(existing)
            if db.execute('SELECT 1 FROM assignments WHERE device_id=? AND protocol=?', (device_id, protocol)).fetchone():
                raise PilotError('assignment_identity_conflict', 409)
            choices = []
            for observation in observations:
                if not 0 <= time.time() - observation.started_at <= 30 or observation.node.mode != 'active' or observation.node.protocol != protocol:
                    continue
                rows = db.execute('SELECT created_at FROM assignments WHERE node_id=?', (observation.node.id,)).fetchall()
                pending = db.execute("SELECT created_at FROM switches WHERE target_node=? AND state NOT IN ('complete', 'cancelled')", (observation.node.id,)).fetchall()
                occupied = max(len(rows) + len(pending), observation.total_peers + sum(row['created_at'] >= observation.started_at for row in [*rows, *pending]))
                capacity = min(observation.node.capacity, observation.max_peers)
                if occupied < capacity:
                    choices.append((occupied / capacity, observation.node.id, observation.node.server_id))
            if not choices:
                raise PilotError('no_eligible_node', 503)
            _, node_id, server_id = min(choices)
            db.execute('INSERT INTO assignments (external_id, client_id, node_id, server_id, creation_expires_at, created_at, device_id, protocol, binding_key) VALUES (?,?,?,?,?,?,?,?,?)',
                       (payload['external_id'], 'wgapi_' + uuid.uuid4().hex, node_id, server_id, payload['expires_at'], time.time(), device_id, protocol, payload['external_id']))
            return dict(db.execute('SELECT * FROM assignments WHERE external_id=?', (payload['external_id'],)).fetchone())

    def bind(self, row, remote_id):
        with self.db() as db:
            changed = db.execute('UPDATE assignments SET remote_id=? WHERE client_id=? AND (remote_id IS NULL OR remote_id=?)',
                                (remote_id, row['client_id'], remote_id)).rowcount
            if changed != 1:
                raise PilotError('remote_identity_conflict', 409)

    def record(self, row, operation, outcome):
        with self.db() as db:
            db.execute('UPDATE assignments SET last_operation=?, last_outcome=?, checked_at=? WHERE client_id=?',
                       (operation, outcome, time.time(), row['client_id']))

    def for_device(self, device_id):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM assignments WHERE device_id=? ORDER BY created_at', (device_id,))]

    def configuration_revision(self, row, configuration, expires_at):
        # Persist only a digest and counter, never credentials or configuration bytes.
        serialized = json.dumps({'configuration': configuration.model_dump(), 'expires_at': expires_at},
                                sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(serialized).hexdigest()
        with self.db() as db:
            current = db.execute('SELECT * FROM assignments WHERE client_id=?', (row['client_id'],)).fetchone()
            if current is None:
                raise PilotError('not_found', 404)
            revision = current['configuration_revision']
            if current['configuration_digest'] != digest:
                if revision != row['configuration_revision']:
                    raise PilotError('configuration_changed', 409)
                revision += 1
                db.execute('UPDATE assignments SET configuration_digest=?, configuration_revision=? WHERE client_id=?',
                           (digest, revision, row['client_id']))
            return revision
