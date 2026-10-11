"""Reusable lifecycle contract; each driver fixture must run its actual adapter.

Currently exercises both supported Amnezia agent wire versions. Add a real
TrustTunnel harness here when its driver exists, not a permissive test double.
"""

from datetime import timedelta
import base64

import pytest
from test_agent_gateway import pilot, now, stamp
from orchestrator.application.ports import NodeGrant
from orchestrator.domain.models import OrchestratorError


@pytest.fixture(params=["amnezia-current", "amnezia-legacy"])
def driver_case(request, pilot):
    gateway, engine, state, body = pilot
    engine.backend.server_public_key = base64.b64encode(bytes(range(32))).decode()
    state["legacy"] = request.param == "amnezia-legacy"
    node = gateway.nodes["lab"]
    return (
        gateway.drivers.for_node(node),
        node,
        engine,
        state,
        NodeGrant(body["external_id"], body["name"], body["expires_at"]),
    )


def test_create_repeat_connection_and_export(driver_case):
    driver, node, engine, _, grant = driver_case
    driver.verify(node)
    first = driver.create(node, grant)
    again = driver.create(node, grant)
    assert first["client_id"] == again["client_id"]
    assert len(engine.rows()) == len(engine.backend.peers) == 1
    assert driver.get(node, first["client_id"])["external_id"] == grant.binding_key
    connection = driver.connection(node, first["client_id"], grant.binding_key)
    assert connection.client["status"] == "active"
    assert connection.configuration.protocol == driver.capabilities.protocol
    for format in driver.capabilities.export_formats:
        document = driver.export(node, first["client_id"], format)
        assert document.format == format and document.data
        assert document.data not in repr(document)


def test_lost_create_response_reuses_same_access(driver_case):
    driver, node, engine, state, grant = driver_case
    state["lose"] = True
    with pytest.raises(OrchestratorError):
        driver.create(node, grant)
    remote_id = engine.rows()[0]["client_id"]
    assert driver.create(node, grant)["client_id"] == remote_id
    assert len(engine.rows()) == len(engine.backend.peers) == 1


def test_revoke_is_confirmed_repeatable_and_blocks_export(driver_case):
    driver, node, engine, _, grant = driver_case
    remote_id = driver.create(node, grant)["client_id"]
    for _ in range(2):
        assert driver.mutate(node, remote_id, "disable", {})["status"] == "disabled"
        assert not engine.backend.peers
        with pytest.raises(OrchestratorError) as failure:
            driver.connection(node, remote_id, grant.binding_key)
        assert failure.value.status == 410
        with pytest.raises(OrchestratorError):
            driver.export(node, remote_id, "conf")


def test_expired_grant_cannot_export(driver_case, monkeypatch):
    driver, node, engine, _, grant = driver_case
    client = driver.create(node, grant)
    later = now() + timedelta(days=4)
    monkeypatch.setattr("app.domain.now", lambda: later)
    engine.reconcile()
    assert driver.get(node, client["client_id"])["status"] == "expired"
    assert not engine.backend.peers
    with pytest.raises(OrchestratorError) as failure:
        driver.connection(node, client["client_id"], grant.binding_key)
    assert failure.value.status == 410


def test_wrong_binding_never_returns_configuration(driver_case):
    driver, node, _, _, grant = driver_case
    client = driver.create(node, grant)
    with pytest.raises(OrchestratorError):
        driver.connection(node, client["client_id"], "wrong-binding")
