"""Run without network, real keys, users, peers or VPN changes."""

import json
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from .amnezia import AmneziaAPI
from .http import create_app
from .models import Node
from .service import Orchestrator
from .store import Store


def main():
    creates = []

    def mock_node(request):
        if request.url.path == "/server":
            return httpx.Response(200, json={
                "id": request.url.host, "totalPeers": 80 if request.url.host == "nl-a.invalid" else 10,
                "maxPeers": 100, "protocols": ["amneziawg3"],
            })
        creates.append(request.url.host)
        return httpx.Response(200, json={"client": {
            "id": "synthetic_peer", "protocol": "amneziawg3",
            "config": "vpn://SYNTHETIC_TEST_DATA",
        }})

    nodes = [Node(name, "https://" + name + ".invalid", name + ".invalid", "nl", 100, "test" * 8)
             for name in ("nl-a", "nl-b")]
    backend_token = "local-demo-only-" * 3
    with tempfile.TemporaryDirectory() as directory:
        key = Fernet.generate_key()
        store = Store(Path(directory), key)
        api = AmneziaAPI(httpx.MockTransport(mock_node))
        intent = {"owner_id": "test_owner", "device_id": "test_mac", "expires_at": int(time.time()) + 3600}
        headers = {"Authorization": "Bearer " + backend_token}
        with TestClient(create_app(Orchestrator(nodes, store, api), backend_token)) as client:
            first = client.post("/internal/v1/connections", json=intent, headers=headers)
            first.raise_for_status()
        # Reopen the persisted journal, like a service restart.
        restarted = Orchestrator(nodes, Store(Path(directory), key), api)
        with TestClient(create_app(restarted, backend_token)) as client:
            repeated = client.post("/internal/v1/connections", json=intent, headers=headers)
            repeated.raise_for_status()
        print(json.dumps({
            "mode": "local_mock_no_network", "selected_node": first.json()["node_id"],
            "creates": len(creates), "same_result_after_restart": first.json() == repeated.json(),
            "configuration": "[redacted]",
        }, indent=2))


if __name__ == "__main__":
    main()
