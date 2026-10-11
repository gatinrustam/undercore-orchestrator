"""Versioned operator inventory. Runtime credentials never enter API responses."""

from orchestrator.application.repository_ports import Journal, Credentials
from orchestrator.domain.inventory import NodeSettings
from orchestrator.domain.models import OrchestratorError


class NodeRegistry:
    def __init__(self, store: Journal, initial, credentials: Credentials):
        self.store, self.credentials = store, credentials
        store.inventory.seed(initial)

    def records(self):
        return self.store.inventory.records()

    def nodes(self):
        return {
            row["id"]: self.credentials.resolve(
                NodeSettings.model_validate_json(row["configuration"])
            )
            for row in self.records()
        }

    def snapshot(self, gateway):
        from orchestrator.application.leases import NodeLeases

        overview = {n["id"]: n for n in gateway.overview()["nodes"]}
        result = []
        for row in self.records():
            config = NodeSettings.model_validate_json(row["configuration"])
            state = NodeLeases(gateway).state(self.credentials.resolve(config))
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
            self.store.lock("registry"),
            self.store.lock("node-" + node_id),
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
                previous.api_key_file if previous and key is None else self.credentials.allocate()
            )
            try:
                config = NodeSettings.model_validate({**raw, "api_key_file": secret_path})
            except ValueError:
                raise OrchestratorError("invalid_request", 422) from None
            if previous:
                if previous.lease_enabled and not config.lease_enabled:
                    raise OrchestratorError("lease_cannot_be_disabled", 409)
                pinned = self.store.inventory.pinned(node_id)
                if pinned and (previous.server_id, previous.protocol) != (
                    config.server_id,
                    config.protocol,
                ):
                    raise OrchestratorError("assigned_node_identity_changed", 409)
            from orchestrator.domain.models import Node

            try:
                candidate = Node(
                    config.id,
                    config.api_url,
                    config.server_id,
                    config.region,
                    config.capacity,
                    key or self.credentials.read(secret_path),
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
                gateway.drivers.for_node(candidate).verify(candidate)
                if config.lease_enabled:
                    gateway.lease_transports.for_node(candidate).probe(candidate)
            if key is not None:
                self.credentials.write(secret_path, key)
            try:
                self.store.inventory.save(node_id, expected + 1, config.model_dump_json())
            except BaseException:
                if key is not None:
                    self.credentials.discard(secret_path)
                raise
        return {"id": node_id, "revision": expected + 1}
