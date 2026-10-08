"""Orchestrator ↔ real node-agent state machine; only tunnel syscalls are simulated."""

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import threading
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures/amnezia_agent"))
from app.domain import Engine, Store, now, stamp
from app.main import create_app
from app.backend import ConfigurationRenderer
from orchestrator.domain.models import Node, OrchestratorError
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
from orchestrator.application.gateway import AgentGateway
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.interfaces.http.app import create_agent_app


class Kernel(ConfigurationRenderer):
    def __init__(self):
        self.peers = set()
        self.server_public_key = "synthetic-public"
        self.config = {"parameters": {}, "dns": ["1.1.1.1"], "endpoint": "192.0.2.1:8443"}

    def snapshot(self):
        return "boot-1", self.peers.copy(), {}

    def apply(self, rows):
        self.peers = {r["public_key"] for r, s in rows}


@pytest.fixture
def pilot(tmp_path):
    engine = Engine(Store(tmp_path / "node.db", Fernet.generate_key()), Kernel())
    lock = threading.Lock()

    def command(payload):
        with lock:
            return engine.execute(payload)

    node_client = TestClient(
        create_app(token="n" * 40, transport=command, server_id="lab-identity")
    )
    state = {"lose": False, "down": False, "identity": None}

    def handle(request):
        if state["down"]:
            raise httpx.ConnectError("synthetic")
        response = node_client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        if state["identity"] and (
            request.url.path == "/v1/health" or request.url.path.endswith("/connection")
        ):
            return httpx.Response(
                200,
                json={**response.json(), "server_id": state["identity"]},
                headers=dict(response.headers),
            )
        if state["lose"] and request.method == "POST":
            state["lose"] = False
            raise httpx.ReadTimeout("synthetic lost response AFTER commit")
        return httpx.Response(
            response.status_code, content=response.content, headers=dict(response.headers)
        )

    api = AgentAPI(httpx.MockTransport(handle))
    node = Node("lab", "https://node.example.test", "lab-identity", "nl", 5, "n" * 40)
    store = Assignments(tmp_path / "orchestrator")
    service = AgentGateway([node], store, api)
    body = {
        "external_id": "vpn_device_1",
        "device_id": "account-v1",
        "name": "Mac",
        "expires_at": stamp(now() + timedelta(days=3)),
    }
    return service, engine, state, body


def test_real_agent_create_restart_and_repeat_keep_one_peer_and_no_central_secret(pilot):
    service, engine, state, body = pilot
    first = service.create(body)
    config = service.client(first["client_id"], "configuration")
    private = config.split("PrivateKey = ")[1].splitlines()[0]
    restarted = AgentGateway(
        list(service.nodes.values()), Assignments(service.store.path.parent), service.api
    )
    assert restarted.create(body)["client_id"] == first["client_id"]
    assert len(engine.rows()) == len(engine.backend.peers) == len(service.store.rows()) == 1
    assert private.encode() not in service.store.path.read_bytes()
    assert restarted.client(first["client_id"], "configuration") == config


def test_lost_response_recovers_original_node_peer_and_assignment(pilot):
    service, engine, state, body = pilot
    state["lose"] = True
    with pytest.raises(OrchestratorError):
        service.create(body)
    original = engine.rows()[0]["public_key"]
    found = service.listing()["clients"][0]
    assert service.create(body)["client_id"] == found["client_id"]
    assert len(engine.rows()) == 1 and engine.backend.peers == {original}


def test_concurrent_creates_consume_one_assignment(pilot):
    service, engine, state, body = pilot
    with ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(lambda _: service.create(body), range(3)))
    assert len({r["client_id"] for r in result}) == len(engine.rows()) == 1


def test_failure_does_not_move_existing_assignment(pilot):
    service, engine, state, body = pilot
    first = service.create(body)
    state["down"] = True
    with pytest.raises(OrchestratorError):
        service.create(body)
    assert len(service.store.rows()) == len(engine.rows()) == 1
    state["down"] = False
    assert service.create(body)["client_id"] == first["client_id"]


def test_disabled_node_still_allows_confirmed_revoke(pilot):
    service, engine, state, body = pilot
    first = service.create(body)
    from dataclasses import replace

    service.nodes["lab"] = replace(service.nodes["lab"], mode="disabled")
    with pytest.raises(OrchestratorError):
        service.client(first["client_id"], "configuration")
    assert service.client(first["client_id"], "disable", {})["status"] == "disabled"
    assert not engine.backend.peers
    with pytest.raises(OrchestratorError):
        service.client(first["client_id"], "enable", {})


def test_renewal_keeps_peer_and_remote_expiry_rejects_export(pilot):
    service, engine, state, body = pilot
    first = service.create(body)
    keys = engine.backend.peers.copy()
    renewed = service.client(
        first["client_id"],
        "renew",
        {"expires_at": stamp(now() + timedelta(days=5)), "idempotency_key": "renew-1"},
    )
    assert renewed["client_id"] == first["client_id"] and engine.backend.peers == keys
    with engine.store.db() as db:
        db.execute("UPDATE clients SET expires_at=?", (stamp(now() - timedelta(seconds=1)),))
    with pytest.raises(OrchestratorError):
        service.client(first["client_id"], "configuration")
    assert not engine.backend.peers


def test_server_identity_mismatch_blocks_commands_even_for_existing_assignment(pilot):
    service, engine, state, body = pilot
    service.create(body)
    state["identity"] = "different-server"
    with pytest.raises(OrchestratorError):
        service.create(body)
    assert len(engine.rows()) == 1


def test_http_auth_validation_overview_and_no_store(pilot):
    service, engine, state, body = pilot
    client = TestClient(create_agent_app(service, "b" * 40))
    assert client.post("/v1/clients", json=body).status_code == 401
    headers = {"Authorization": "Bearer " + "b" * 40}
    assert (
        client.post("/v1/clients", headers=headers, json={**body, "node": "other"}).status_code
        == 422
    )
    assert client.post("/v1/clients", headers=headers, content="x" * 8193).status_code == 413
    result = client.post("/v1/clients", headers=headers, json=body)
    assert result.status_code == 200 and "no-store" in result.headers["cache-control"]
    overview = client.get("/internal/v1/nodes", headers=headers)
    assert overview.json()["nodes"][0]["assigned"] == 1
    for secret in ("PrivateKey", "api_key", "external_id", "configuration", "remote_id"):
        assert secret not in overview.text
    export = client.get(
        "/v1/clients/" + result.json()["client_id"] + "/configuration", headers=headers
    )
    assert export.status_code == 200 and "no-store" in export.headers["cache-control"]


def test_no_available_node_creates_no_assignment(pilot):
    service, engine, state, body = pilot
    state["down"] = True
    with pytest.raises(OrchestratorError):
        service.create(body)
    assert service.store.rows() == [] and engine.rows() == []
