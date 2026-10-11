"""Two real agent state machines, independent kernels; no live server mutation."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_agent_gateway import Kernel
from app.domain import Engine, Store, now, stamp
from app.main import create_app
from orchestrator.bootstrap import build_gateway as AgentGateway
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.application.connections import Connections
from orchestrator.domain.contracts import ConfigurationRequest, SwitchRequest
from orchestrator.domain.models import Node, OrchestratorError
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI

HEADERS = {"Authorization": "Bearer " + "b" * 40}
AWG = [{"protocol": "amneziawg", "configuration_version": 1}]


@pytest.fixture
def pair(tmp_path):
    engines, clients, states, nodes = {}, {}, {}, []
    for name in ("a", "b"):
        engines[name] = Engine(Store(tmp_path / (name + ".db"), Fernet.generate_key()), Kernel())
        engines[name].backend.config["endpoint"] = (
            "192.0.2.1" if name == "a" else "192.0.2.2"
        ) + ":8443"
        lock = threading.Lock()

        def command(payload, engine=engines[name], lock=lock):
            with lock:
                return engine.execute(payload)

        clients[name] = TestClient(
            create_app(token="n" * 40, transport=command, server_id=name + "-identity")
        )
        states[name] = {"down": False, "lose": None}
        nodes.append(Node(name, "https://" + name + ".test", name + "-identity", "nl", 5, "n" * 40))

    def handle(request):
        name = request.url.host.split(".")[0]
        if states[name]["down"]:
            raise httpx.ConnectError("synthetic")
        response = clients[name].request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        if (
            request.method == "POST"
            and states[name]["lose"]
            and request.url.path.endswith(states[name]["lose"])
        ):
            states[name]["lose"] = None
            raise httpx.ReadTimeout("synthetic response loss after commit")
        return httpx.Response(
            response.status_code, content=response.content, headers=dict(response.headers)
        )

    gateway = AgentGateway(
        nodes, Assignments(tmp_path / "journal"), AgentAPI(httpx.MockTransport(handle))
    )
    payload = {
        "external_id": "vpn_device",
        "name": "Mac",
        "expires_at": stamp(now() + timedelta(days=1)),
    }
    ident = gateway.create(payload)["client_id"]
    req = SwitchRequest(
        schema_version=1,
        device_id="vpn_device",
        capabilities=AWG,
        expected_node_id="a",
        idempotency_key="switch-1",
    )
    return gateway, engines, states, payload, ident, req


def export(gateway, ident):
    return Connections(gateway).configuration(
        ident, ConfigurationRequest(schema_version=1, device_id="vpn_device", capabilities=AWG)
    )


def test_switch_preserves_slot_alias_and_revokes_previous_node(pair):
    gateway, engines, _, payload, ident, req = pair
    before = export(gateway, ident)
    result = Connections(gateway).switch(ident, req)
    after = export(gateway, ident)
    assert (
        result.connection_id == ident
        and result.device_id == payload["external_id"]
        and result.node_id == "b"
    )
    assert not engines["a"].backend.peers and len(engines["b"].backend.peers) == 1
    assert (
        after.revision == before.revision + 1
        and after.configuration.data != before.configuration.data
    )
    assert len(gateway.store.rows()) == 1
    assert gateway.create(payload)["client_id"] == ident
    assert gateway.listing()["clients"][0]["external_id"] == payload["external_id"]
    assert gateway.client(ident, "disable")["status"] == "disabled"
    assert not engines["b"].backend.peers


@pytest.mark.parametrize("node,operation", [("a", "/disable"), ("b", "/v1/clients")])
def test_lost_response_recovers_same_switch_after_restart(pair, node, operation):
    gateway, engines, states, _, ident, req = pair
    states[node]["lose"] = operation
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    with pytest.raises(OrchestratorError, match="switch_in_progress"):
        export(gateway, ident)
    restarted = AgentGateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    value = Connections(restarted).switch(ident, req)
    assert value.node_id == "b" and not engines["a"].backend.peers
    assert len(engines["b"].rows()) == len(engines["b"].backend.peers) == 1
    assert export(restarted, ident).node_id == "b"


def test_revoke_unreachable_never_creates_target_and_can_resume(pair):
    gateway, engines, states, _, ident, req = pair
    # Reservation exists; lose revoke response, then disconnect source API.
    states["a"]["lose"] = "/disable"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    assert not engines["b"].rows()
    states["a"]["down"] = False
    assert Connections(gateway).switch(ident, req).node_id == "b"


def test_no_alternative_preserves_working_connection(pair):
    gateway, engines, states, _, ident, req = pair
    states["b"]["down"] = True
    with pytest.raises(OrchestratorError, match="no_alternative_node"):
        Connections(gateway).switch(ident, req)
    assert engines["a"].backend.peers and not engines["b"].rows()
    assert export(gateway, ident).node_id == "a"


def test_repeat_concurrent_requests_create_one_target_and_old_request_cannot_switch_back(pair):
    gateway, engines, _, _, ident, req = pair
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: Connections(gateway).switch(ident, req), range(4)))
    assert {r.node_id for r in values} == {"b"} and len(engines["b"].rows()) == 1
    back = req.model_copy(update={"expected_node_id": "b", "idempotency_key": "switch-2"})
    assert Connections(gateway).switch(ident, back).node_id == "a"
    with pytest.raises(OrchestratorError, match="switch_superseded"):
        Connections(gateway).switch(ident, req)
    assert not engines["b"].backend.peers and len(engines["a"].backend.peers) == 1


def test_wrong_device_and_stale_node_have_no_side_effects(pair):
    gateway, engines, _, _, ident, req = pair
    for change, error in [
        ({"device_id": "other"}, "not_found"),
        ({"expected_node_id": "other"}, "assigned_node_changed"),
    ]:
        with pytest.raises(OrchestratorError, match=error):
            Connections(gateway).switch(ident, req.model_copy(update=change))
    assert engines["a"].backend.peers and not engines["b"].rows()


def test_http_switch_auth_schema_no_store_and_secrets(pair):
    gateway, _, _, _, ident, req = pair
    client = TestClient(create_agent_app(gateway, "b" * 40))
    path = "/internal/v2/connections/" + ident + "/switch"
    assert client.post(path, json=req.model_dump()).status_code == 401
    assert (
        client.post(
            path, headers=HEADERS, json={**req.model_dump(), "target_node": "b"}
        ).status_code
        == 422
    )
    response = client.post(path, headers=HEADERS, json=req.model_dump())
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    assert "PrivateKey" not in response.text
    assert b"[Interface]" not in gateway.store.path.read_bytes()


def test_renew_and_legacy_replace_after_switch_keep_canonical_external_id(pair):
    gateway, engines, _, payload, ident, req = pair
    Connections(gateway).switch(ident, req)
    expiry = stamp(now() + timedelta(days=2))
    assert (
        gateway.client(ident, "renew", {"expires_at": expiry, "idempotency_key": "renew-switch"})[
            "external_id"
        ]
        == payload["external_id"]
    )
    engines["b"].backend.peers.clear()
    result = gateway.client(
        ident,
        "replace",
        {
            "expected_external_id": payload["external_id"],
            "expires_at": expiry,
            "idempotency_key": "replace-switch",
        },
    )
    assert result["client_id"] == ident and result["external_id"] == payload["external_id"]
    assert not engines["a"].backend.peers and len(engines["b"].backend.peers) == 1


def test_disable_during_lost_target_create_revokes_both_peers(pair):
    gateway, engines, states, _, ident, req = pair
    states["b"]["lose"] = "/v1/clients"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    assert engines["b"].backend.peers
    value = gateway.client(ident, "disable")
    assert value["status"] == "disabled"
    assert not engines["a"].backend.peers and not engines["b"].backend.peers
    assert Connections(gateway).switch(ident, req).state == "disabled"
    with pytest.raises(OrchestratorError, match="access_unavailable"):
        export(gateway, ident)


def test_disable_intent_survives_target_outage_and_switch_retry(pair):
    gateway, engines, states, _, ident, req = pair
    states["b"]["lose"] = "/v1/clients"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    states["b"]["down"] = True
    with pytest.raises(OrchestratorError):
        gateway.client(ident, "disable")
    states["b"]["down"] = False
    restarted = AgentGateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    assert Connections(restarted).switch(ident, req).state == "disabled"
    assert not engines["a"].backend.peers and not engines["b"].backend.peers


def test_expired_pending_switch_can_be_revoked_without_creating_a_peer(pair):
    gateway, engines, states, _, ident, req = pair
    states["a"]["lose"] = "/disable"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    with gateway.store.db() as db:
        db.execute("UPDATE switches SET expires_at=?", (stamp(now() - timedelta(seconds=1)),))
    assert gateway.client(ident, "disable")["status"] == "disabled"
    assert not engines["b"].rows() and not engines["a"].backend.peers
    with pytest.raises(OrchestratorError, match="switch_cancelled"):
        Connections(gateway).switch(ident, req)


def test_pending_capacity_is_reserved_and_new_key_cannot_fan_out(pair):
    gateway, engines, states, _, ident, req = pair
    gateway.nodes["b"] = replace(gateway.nodes["b"], capacity=1)
    states["b"]["lose"] = "/v1/clients"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    with pytest.raises(OrchestratorError, match="switch_in_progress"):
        Connections(gateway).switch(ident, req.model_copy(update={"idempotency_key": "other"}))
    gateway.nodes["a"] = replace(gateway.nodes["a"], mode="draining")
    with pytest.raises(OrchestratorError, match="no_eligible_node"):
        gateway.create(
            {
                "external_id": "second-device",
                "name": "Test",
                "expires_at": stamp(now() + timedelta(days=1)),
            }
        )
    assert len(engines["b"].rows()) == 1


def test_target_identity_and_disabled_policy_block_recovery(pair):
    gateway, engines, states, _, ident, req = pair
    states["a"]["lose"] = "/disable"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    old = gateway.nodes["b"]
    gateway.nodes["b"] = replace(old, server_id="different")
    with pytest.raises(OrchestratorError, match="assigned_node_identity_changed"):
        Connections(gateway).switch(ident, req)
    gateway.nodes["b"] = replace(old, mode="disabled")
    with pytest.raises(OrchestratorError, match="target_node_unavailable"):
        Connections(gateway).switch(ident, req)
    assert not engines["b"].rows()
