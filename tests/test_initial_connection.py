"""Initial provisioning must not scale with unrelated assigned devices."""

import threading
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_agent_gateway import pilot
from test_node_switch import pair, HEADERS
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.domain.models import OrchestratorError
from orchestrator.config.policy import RuntimePolicy


def spy(gateway):
    original = gateway.resources.transport
    calls = []

    def handle(request):
        calls.append((request.method, request.url.host, request.url.path))
        return original.handle_request(request)

    gateway.resources.transport = httpx.MockTransport(handle)
    return calls


def test_missing_identity_never_reads_other_assignments_or_nodes(pilot, monkeypatch):
    gateway, engine, state, body = pilot
    gateway.create(body)
    calls = spy(gateway)
    monkeypatch.setattr(gateway.store, "rows", lambda: pytest.fail("Full assignment scan"))
    assert gateway.lookup("new_device") == {"clients": []}
    assert calls == [] and len(engine.rows()) == 1


def test_lookup_reads_only_selected_node_and_preserves_identity_after_restart(pair):
    gateway, engines, states, body, ident, _ = pair
    states["b"]["down"] = True
    calls = spy(gateway)
    reply = gateway.lookup(body["external_id"])
    assert reply["clients"][0]["client_id"] == ident
    assert {host for _, host, _ in calls} == {"a.test"}
    assert all(path != "/v1/clients" for _, _, path in calls)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        gateway.lookup(body["external_id"])


def test_lookup_recovers_lost_create_response_without_new_peer(pilot):
    gateway, engine, state, body = pilot
    state["lose"] = True
    with pytest.raises(OrchestratorError):
        gateway.create(body)
    found = gateway.lookup(body["external_id"])["clients"][0]
    assert gateway.create(body)["client_id"] == found["client_id"]
    assert len(engine.rows()) == len(engine.backend.peers) == 1


def test_lookup_does_not_reenable_revoked_access(pilot):
    gateway, engine, state, body = pilot
    ident = gateway.create(body)["client_id"]
    gateway.client(ident, "disable")
    result = gateway.lookup(body["external_id"])
    assert result["clients"][0]["status"] == "disabled"
    assert not engine.backend.peers


def test_lookup_route_auth_validation_and_no_store(pilot):
    gateway, *_ = pilot
    client = TestClient(create_agent_app(gateway, "b" * 40))
    path = "/v1/clients/lookup"
    payload = {"external_id": "new_device", "device_id": "account-v1"}
    assert client.post(path, json=payload).status_code == 401
    response = client.post(path, headers=HEADERS, json=payload)
    assert response.status_code == 200 and response.json() == {"clients": []}
    assert "no-store" in response.headers["cache-control"]
    for invalid in [
        {},
        {**payload, "device_id": "other"},
        {**payload, "external_id": "../bad"},
        {**payload, "node": "a"},
    ]:
        assert client.post(path, headers=HEADERS, json=invalid).status_code == 422


def test_observation_has_one_identity_check_and_one_list(pilot):
    gateway, *_ = pilot
    calls = spy(gateway)
    gateway.resources.observe(gateway.nodes["lab"])
    assert [path for _, _, path in calls] == ["/v1/health", "/v1/clients"]


def test_identity_mismatch_stops_observation_before_list(pilot):
    gateway, engine, state, body = pilot
    state["identity"] = "wrong"
    calls = spy(gateway)
    with pytest.raises(OrchestratorError, match="node_identity_invalid"):
        gateway.resources.observe(gateway.nodes["lab"])
    assert [path for _, _, path in calls] == ["/v1/health"]


def test_parallel_observation_preserves_load_selection(pair, monkeypatch):
    gateway, engines, states, body, ident, _ = pair
    driver = gateway.drivers.for_node(gateway.nodes["a"])
    original = driver.observe
    barrier = threading.Barrier(2, timeout=5)
    observed = []

    def observe(node):
        observed.append(node.id)
        barrier.wait()  # Sequential observation cannot satisfy this barrier.
        return original(node)

    monkeypatch.setattr(driver, "observe", observe)
    second = gateway.create({**body, "external_id": "second_device"})
    assert set(observed) == {"a", "b"}
    assert gateway.store.get(client_id=second["client_id"])["node_id"] == "b"
    assert len(engines["a"].rows()) == len(engines["b"].rows()) == 1


def test_unavailable_node_does_not_prevent_creation_on_healthy_node(pair):
    gateway, engines, states, body, *_ = pair
    states["a"]["down"] = True
    second = gateway.create({**body, "external_id": "second_device"})
    assert gateway.store.get(client_id=second["client_id"])["node_id"] == "b"


@pytest.mark.parametrize("workers", [0, 17, True])
def test_observation_concurrency_is_bounded(workers):
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate({"selection": {"observation_workers": workers}})
