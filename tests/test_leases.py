from test_node_switch import pair, export  # noqa: F401
from dataclasses import replace
import time
import pytest
from test_node_switch import export
from test_recovery import request
from orchestrator.application.leases import NodeLeases, FENCE_GRACE_SECONDS
from orchestrator.application.recovery import Recovery, reconcile
from orchestrator.domain.models import OrchestratorError
from orchestrator.bootstrap import build_gateway as AgentGateway


def enroll(gateway):
    for key, node in list(gateway.nodes.items()):
        gateway.nodes[key] = replace(node, lease_enabled=True)
        NodeLeases(gateway).heartbeat(gateway.nodes[key])


def expire(gateway, engines, node):
    engines[node].control_lease.deadline = 0
    engines[node].reconcile()
    with gateway.store.db() as db:
        db.execute(
            "UPDATE control_leases SET valid_until=?,fence_after=? WHERE node_id=?",
            (time.time() - FENCE_GRACE_SECONDS - 1, time.monotonic() - 1, node),
        )


def test_total_source_outage_waits_for_fence_then_preserves_identity(pair):
    gateway, engines, states, _, ident, _ = pair
    enroll(gateway)
    before = export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError, match="lease_waiting"):
        Recovery(gateway).recover(ident, request(before.revision))
    assert engines["a"].backend.peers and not engines["b"].rows()
    assert NodeLeases(gateway).state(gateway.nodes["a"])["fenced"] == 1
    NodeLeases(gateway).heartbeat(gateway.nodes["a"])  # Must not renew a fenced node.
    expire(gateway, engines, "a")
    assert reconcile(gateway)["completed"] == 1
    after = export(gateway, ident)
    assert (
        after.connection_id == before.connection_id
        and after.device_id == before.device_id
        and after.node_id == "b"
    )
    assert not engines["a"].backend.peers and len(engines["b"].backend.peers) == 1
    # The returning node stays closed until stale peer is explicitly revoked.
    states["a"]["down"] = False
    NodeLeases(gateway).restore(gateway.nodes["a"])
    engines["a"].reconcile()
    assert not engines["a"].backend.peers
    assert engines["a"].rows()[0]["enabled"] == 0
    assert NodeLeases(gateway).ready(gateway.nodes["a"])


def test_no_lease_proof_or_no_reserve_never_fences_or_provisions(pair):
    gateway, engines, states, _, ident, _ = pair
    before = export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request(before.revision))
    assert not engines["b"].rows()
    states["a"]["down"] = False
    enroll(gateway)
    states["a"]["down"] = states["b"]["down"] = True
    with pytest.raises(OrchestratorError, match="no_alternative_node"):
        Recovery(gateway).recover(ident, request(before.revision))
    assert not NodeLeases(gateway).state(gateway.nodes["a"])["fenced"]


def test_restore_cannot_bypass_pending_switch_and_disable_blocks_new_grant(pair):
    gateway, engines, states, _, ident, _ = pair
    enroll(gateway)
    export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request())
    with pytest.raises(OrchestratorError, match="node_has_pending_switches"):
        NodeLeases(gateway).restore(gateway.nodes["a"])
    with pytest.raises(OrchestratorError):
        gateway.client(ident, "disable")
    expire(gateway, engines, "a")
    assert reconcile(gateway)["completed"] == 1
    assert not engines["b"].backend.peers
    with pytest.raises(OrchestratorError):
        export(gateway, ident)


def test_lost_lease_response_keeps_conservative_bound_and_restart_fence(pair):
    gateway, engines, states, _, ident, _ = pair
    enroll(gateway)
    export(gateway, ident)
    previous = NodeLeases(gateway).state(gateway.nodes["a"])["valid_until"]
    states["a"]["lose"] = "/v1/control-lease"
    with pytest.raises(OrchestratorError):
        NodeLeases(gateway).heartbeat(gateway.nodes["a"])
    assert NodeLeases(gateway).state(gateway.nodes["a"])["valid_until"] >= previous
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request())
    restarted = AgentGateway(list(gateway.nodes.values()), gateway.store, gateway.resources)
    assert not NodeLeases(restarted).ready(restarted.nodes["a"])
    assert not engines["b"].rows()


def test_wall_clock_jump_and_host_restart_cannot_shorten_fence(pair, monkeypatch):
    gateway, engines, states, _, ident, _ = pair
    enroll(gateway)
    export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request())
    lease = NodeLeases(gateway)
    node = gateway.nodes["a"]
    monkeypatch.setattr("orchestrator.application.leases.time.time", lambda: 4_000_000_000)
    assert not lease.expired(node)
    with gateway.store.db() as db:
        db.execute(
            "UPDATE control_leases SET fence_boot='previous-boot',fence_after=0 WHERE node_id='a'"
        )
    assert not lease.expired(node)
    assert lease.state(node)["fence_after"] > time.monotonic()


def test_reserved_target_that_becomes_fenced_cannot_receive_new_access(pair):
    gateway, engines, states, _, ident, _ = pair
    enroll(gateway)
    export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, request())
    NodeLeases(gateway).fence(gateway.nodes["b"])
    expire(gateway, engines, "a")
    assert reconcile(gateway)["completed"] == 0
    assert not engines["b"].rows()
    NodeLeases(gateway).restore(gateway.nodes["b"])
    assert reconcile(gateway)["completed"] == 1
    assert export(gateway, ident).node_id == "b"
