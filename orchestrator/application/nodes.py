"""Versioned operator inventory. Runtime credentials never enter API responses."""

import os
import uuid
from pathlib import Path
from orchestrator.config.settings import NodeSettings
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.locking import connection_lock


class NodeRegistry:
    def __init__(self, store, initial):
        self.store = store
        from orchestrator.infrastructure.sqlite.migrations import initialize_registry

        initialize_registry(store)
        with store.db() as db:
            # Seed once, not at every restart: JSON is the bootstrap, SQLite the live inventory.

            if not db.execute("SELECT 1 FROM registry_meta WHERE id=1").fetchone():
                for node in initial:
                    db.execute(
                        "INSERT INTO node_registry VALUES (?,1,?)",
                        (node.id, node.model_dump_json()),
                    )
                db.execute("INSERT INTO registry_meta VALUES (1,1)")

    def records(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM node_registry ORDER BY id")]

    def nodes(self):
        return {
            row["id"]: NodeSettings.model_validate_json(row["configuration"]).node()
            for row in self.records()
        }

    def snapshot(self, gateway):
        from orchestrator.application.leases import NodeLeases

        overview = {n["id"]: n for n in gateway.overview()["nodes"]}
        result = []
        for row in self.records():
            config = NodeSettings.model_validate_json(row["configuration"])
            state = NodeLeases(gateway).state(config.node())
            result.append(
                {
                    **config.model_dump(exclude={"api_key_file"}),
                    **overview.get(config.id, {}),
                    "revision": row["revision"],
                    "fenced": bool(state and state["fenced"]),
                    "lease_verified": bool(state and state["verified"]),
                }
            )
        return {"nodes": result}

    def save(self, gateway, data):
        expected = data.get("expected_revision")
        key = data.get("api_key")
        raw = {k: v for k, v in data.items() if k not in ("expected_revision", "api_key")}
        node_id = raw.get("id")
        if type(expected) is not int or expected < 0 or not isinstance(node_id, str):
            raise OrchestratorError("invalid_request", 422)
        with (
            connection_lock(self.store, "registry"),
            connection_lock(self.store, "node-" + node_id),
        ):
            records = self.records()
            old = next((r for r in records if r["id"] == node_id), None)
            if (old["revision"] if old else 0) != expected:
                raise OrchestratorError("node_revision_conflict", 409)
            previous = NodeSettings.model_validate_json(old["configuration"]) if old else None
            if key is None and previous is None:
                raise OrchestratorError("node_credential_required", 422)
            if key is not None and (
                not isinstance(key, str)
                or len(key) < 32
                or len(key) > 256
                or any(c.isspace() for c in key)
            ):
                raise OrchestratorError("invalid_node_credential", 422)
            # A caller can never supply a filesystem path for credentials.
            if "api_key_file" in raw:
                raise OrchestratorError("invalid_request", 422)
            secret_path = (
                Path(previous.api_key_file)
                if previous and key is None
                else self.store.path.parent / "node-secrets" / (uuid.uuid4().hex + ".token")
            )
            try:
                config = NodeSettings.model_validate({**raw, "api_key_file": str(secret_path)})
            except ValueError:
                raise OrchestratorError("invalid_request", 422) from None
            if previous:
                if previous.lease_enabled and not config.lease_enabled:
                    raise OrchestratorError("lease_cannot_be_disabled", 409)
                with self.store.db() as db:
                    pinned = db.execute(
                        "SELECT 1 FROM assignments WHERE node_id=? UNION SELECT 1 FROM switches WHERE target_node=? OR source_node=? UNION SELECT 1 FROM control_leases WHERE node_id=? LIMIT 1",
                        (node_id, node_id, node_id, node_id),
                    ).fetchone()
                if pinned and (previous.server_id, previous.protocol) != (
                    config.server_id,
                    config.protocol,
                ):
                    raise OrchestratorError("assigned_node_identity_changed", 409)
            from orchestrator.domain.models import Node
            from orchestrator.config.settings import read_secret

            try:
                candidate = Node(
                    config.id,
                    config.api_url,
                    config.server_id,
                    config.region,
                    config.capacity,
                    key or read_secret(secret_path).decode(),
                    mode=config.mode,
                    protocol=config.protocol,
                    lease_enabled=config.lease_enabled,
                )
            except ValueError:
                raise OrchestratorError("invalid_request", 422) from None
            gateway.drivers.for_node(candidate)
            for record in records:
                other = NodeSettings.model_validate_json(record["configuration"])
                if other.id != node_id and (
                    other.server_id == candidate.server_id
                    or other.api_url.rstrip("/").lower() == candidate.api_url.rstrip("/").lower()
                ):
                    raise OrchestratorError("duplicate_node", 409)
            if (
                config.lease_enabled
                and sum(
                    NodeSettings.model_validate_json(r["configuration"]).lease_enabled
                    for r in records
                    if r["id"] != node_id
                )
                >= 64
            ):
                raise OrchestratorError("leased_node_limit", 409)
            address_changed = (
                not previous
                or previous.api_url != config.api_url
                or previous.server_id != config.server_id
                or key is not None
            )
            if address_changed or (
                config.lease_enabled and (not previous or not previous.lease_enabled)
            ):
                gateway.api.verify(candidate)
                if config.lease_enabled:
                    health = gateway.api.request(candidate, "GET", "/v1/health")
                    if health.get("control_lease", {}).get("version") != 1:
                        raise OrchestratorError("control_lease_unavailable", 409)
            if key is not None:
                secret_path.parent.mkdir(mode=0o700, exist_ok=True)
                fd = os.open(
                    secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "w") as f:
                    f.write(key)
                    f.flush()
                    os.fsync(f.fileno())
            try:
                with self.store.db() as db:
                    db.execute(
                        "INSERT INTO node_registry VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,configuration=excluded.configuration",
                        (node_id, expected + 1, config.model_dump_json()),
                    )
            except BaseException:
                if key is not None:
                    secret_path.unlink(missing_ok=True)
                raise
        return {"id": node_id, "revision": expected + 1}
