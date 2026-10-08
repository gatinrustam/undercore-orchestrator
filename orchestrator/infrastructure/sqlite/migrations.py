"""Idempotent, additive migrations for journals from existing releases.

No node contact; existing assignment and switch identities are preserved.
"""


def migrate(store):
    with store.db() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS assignments (\n                external_id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,\n                node_id TEXT NOT NULL, server_id TEXT NOT NULL,\n                creation_expires_at TEXT NOT NULL, remote_id TEXT,\n                created_at REAL NOT NULL, last_operation TEXT, last_outcome TEXT, checked_at REAL)"
        )
        # Additive migration: existing aliases, external IDs and remote peers stay intact.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(assignments)")}
        additions = {
            "device_id": "TEXT NOT NULL DEFAULT ''",
            "protocol": "TEXT NOT NULL DEFAULT 'amneziawg'",
            "configuration_revision": "INTEGER NOT NULL DEFAULT 0",
            "configuration_digest": "TEXT",
            "binding_key": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE assignments ADD COLUMN {name} {definition}")
        db.execute("UPDATE assignments SET device_id=external_id WHERE device_id=''")
        db.execute("UPDATE assignments SET binding_key=external_id WHERE binding_key=''")
        db.execute(
            "CREATE TABLE IF NOT EXISTS switches (client_id TEXT NOT NULL, operation_id TEXT NOT NULL, source_node TEXT NOT NULL, target_node TEXT NOT NULL, target_server TEXT NOT NULL, binding_key TEXT NOT NULL, name TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, disable_requested INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(client_id, operation_id))"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_pending_switch ON switches(client_id) WHERE state NOT IN ('complete', 'cancelled')"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS assignments_device_protocol ON assignments(device_id, protocol)"
        )
    initialize_leases(store)


def initialize_leases(store):
    with store.db() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS control_leases (node_id TEXT PRIMARY KEY, server_id TEXT NOT NULL, controller TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0, valid_until REAL NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, fenced INTEGER NOT NULL DEFAULT 0, fence_boot TEXT NOT NULL DEFAULT '', fence_after REAL NOT NULL DEFAULT 0)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS access_cache (client_id TEXT PRIMARY KEY, binding_key TEXT NOT NULL, payload TEXT NOT NULL, denied INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS retired_bindings (node_id TEXT NOT NULL, server_id TEXT NOT NULL, remote_id TEXT NOT NULL, binding_key TEXT NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(node_id,binding_key))"
        )


def initialize_registry(store):
    with store.db() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS node_registry (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, configuration TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS registry_meta (id INTEGER PRIMARY KEY CHECK(id=1), initialized INTEGER NOT NULL)"
        )
