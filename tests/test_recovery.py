from test_node_switch import pair, export, AWG, HEADERS  # noqa: F401
import pytest
from fastapi.testclient import TestClient
from test_node_switch import export, AWG, HEADERS
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.domain.contracts import RecoveryRequest
from orchestrator.domain.models import OrchestratorError
from orchestrator.application.recovery import Recovery, reconcile


def request(revision=1):
    return RecoveryRequest(
        schema_version=1, device_id="vpn_device", capabilities=AWG, expected_revision=revision
    )


def test_recovery_replays_and_cooldown_prevents_ping_pong(pair):
    gateway, engines, _, _, ident, _ = pair
    before = export(gateway, ident)
    recovery = Recovery(gateway)
    after = recovery.recover(ident, request(before.revision))
    assert after.node_id == "b" and after.revision == before.revision + 1
    assert recovery.recover(ident, request(before.revision)) == after
    with pytest.raises(OrchestratorError, match="recovery_cooldown"):
        recovery.recover(ident, request(after.revision))
    assert len(engines["b"].rows()) == 1 and not engines["a"].backend.peers


@pytest.mark.parametrize("node,operation", [("a", "/disable"), ("b", "/v1/clients")])
def test_worker_finishes_lost_response_without_client(pair, node, operation):
    gateway, engines, states, _, ident, _ = pair
    export(gateway, ident)
    states[node]["lose"] = operation
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request())
    assert reconcile(gateway) == {"completed": 1, "pending_or_busy": 0}
    after = Recovery(gateway).recover(ident, request())
    assert after.node_id == "b" and not engines["a"].backend.peers
    assert len(engines["b"].rows()) == 1
    assert reconcile(gateway)["completed"] == 0


def test_worker_preserves_disable_intent_and_never_grants_on_source_outage(pair):
    gateway, engines, states, _, ident, _ = pair
    export(gateway, ident)
    states["a"]["lose"] = "/disable"
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request())
    states["a"]["down"] = True
    assert reconcile(gateway)["pending_or_busy"] == 1
    assert not engines["b"].rows()
    with pytest.raises(OrchestratorError):
        gateway.client(ident, "disable")
    states["a"]["down"] = False
    assert reconcile(gateway)["completed"] == 1
    assert not engines["a"].backend.peers and not engines["b"].backend.peers


def test_stale_revision_returns_new_manifest_without_switch(pair):
    gateway, engines, _, _, ident, _ = pair
    before = export(gateway, ident)
    with gateway.store.db() as db:
        db.execute("UPDATE assignments SET configuration_revision=2")
    assert Recovery(gateway).recover(ident, request(before.revision)).node_id == "a"
    assert not engines["b"].rows()
    with pytest.raises(OrchestratorError, match="revision_conflict"):
        Recovery(gateway).recover(ident, request(100))


def test_recovery_http_auth_identity_schema_and_no_store(pair):
    gateway, _, _, _, ident, _ = pair
    export(gateway, ident)
    client = TestClient(create_agent_app(gateway, "b" * 40))
    path = "/internal/v2/connections/" + ident + "/recover"
    body = request().model_dump()
    assert client.post(path, json=body).status_code == 401
    for extra in [
        {"expected_revision": True},
        {"expected_revision": 0},
        {"target_node": "b"},
        {"expires_at": "secret"},
    ]:
        response = client.post(path, headers=HEADERS, json={**body, **extra})
        assert response.status_code == 422 and "secret" not in response.text
    assert (
        client.post(path, headers=HEADERS, json={**body, "device_id": "other"}).status_code == 404
    )
    response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
