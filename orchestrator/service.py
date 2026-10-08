import time

from .models import ConnectionIntent, Node, NodeAPI, PilotError
from .store import Store


class Orchestrator:
    def __init__(self, nodes: list[Node], store: Store, api: NodeAPI):
        for values in (
            [node.id for node in nodes], [node.server_id for node in nodes],
            [node.api_url.rstrip("/").lower() for node in nodes],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate node identity or API origin")
        self.nodes = {node.id: node for node in nodes}
        self.store = store
        self.api = api

    def connect(self, intent: ConnectionIntent):
        if intent.expires_at <= time.time():
            raise PilotError("grant_expired", 403)
        existing = self.store.get(intent.device_id)
        if existing is not None:
            return self._result(existing, intent)

        observations = []
        for node in self.nodes.values():
            if node.mode != "active" or (intent.region and intent.region != node.region):
                continue
            try:
                observations.append(self.api.observe(node))
            except PilotError:
                continue

        row, claimed = self.store.reserve(intent, observations)
        if not claimed:
            return self._result(row, intent)

        try:
            if intent.expires_at <= time.time():
                raise PilotError("grant_expired", 403)
            client = self.api.create(self.nodes[row["node_id"]], row["operation_id"], intent.expires_at)
            self.store.finish(row, client)
        except Exception:
            self.store.uncertain(row["operation_id"])
            raise PilotError("provisioning_uncertain") from None
        return self._result(self.store.get(intent.device_id), intent)

    def _result(self, row, intent):
        if row["owner_id"] != intent.owner_id:
            raise PilotError("device_owner_conflict", 403)
        if row["expires_at"] != intent.expires_at:
            raise PilotError("expiry_change_not_implemented")
        if row["expires_at"] <= time.time():
            raise PilotError("grant_expired", 403)
        node = self.nodes.get(row["node_id"])
        if node is None or node.mode == "disabled":
            raise PilotError("assigned_node_disabled", 503)
        if node.server_id != row["server_id"]:
            raise PilotError("assigned_node_identity_changed", 503)
        if row["state"] != "ready":
            # Includes process death after POST but before storing its response.
            raise PilotError("provisioning_uncertain")
        client = self.store.read_client(row)
        return {
            "device_id": intent.device_id, "node_id": node.id,
            "revision": row["operation_id"], "expires_at": row["expires_at"],
            "format": "amnezia-vpn-uri", "configuration": client.config,
        }
