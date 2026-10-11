"""Atomic persistence and protocol isolation after repository extraction."""

import ast
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_agent_gateway import pilot
from test_node_switch import pair
from orchestrator.application.connections import Connections
from orchestrator.application.drivers import LeaseRegistry
from orchestrator.application.leases import NodeLeases
from orchestrator.domain.records import LeaseObservation


def test_switch_commit_failure_rolls_back_binding_and_retry_finishes(pair):
    gateway, engines, _, _, ident, request = pair
    original = gateway.store.get(client_id=ident)
    with gateway.store.db() as db:
        db.execute("""CREATE TRIGGER fail_complete BEFORE UPDATE OF state ON switches
                    WHEN NEW.state = 'complete'
                    BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        Connections(gateway).switch(ident, request)
    row = gateway.store.get(client_id=ident)
    for key in ("node_id", "server_id", "remote_id", "binding_key"):
        assert row[key] == original[key]
    assert gateway.store.switches.pending(ident)["state"] == "source_revoked"
    assert not engines["a"].backend.peers
    assert len(engines["b"].backend.peers) == 1
    with gateway.store.db() as db:
        db.execute("DROP TRIGGER fail_complete")
    assert Connections(gateway).switch(ident, request).node_id == "b"
    assert gateway.store.switches.pending(ident) is None
    assert len(engines["b"].rows()) == 1


def test_cache_remember_does_not_store_secrets_or_clear_denial(pilot):
    gateway, _, _, body = pilot
    client = gateway.create(body)
    row = gateway.store.get(client_id=client["client_id"])
    gateway.store.leases.deny(row, True)
    gateway.store.leases.remember(row, {**client, "configuration": "synthetic-secret"})
    saved = gateway.store.leases.cached(row["client_id"])
    assert saved["denied"] == 1
    assert "configuration" not in json.loads(saved["payload"])
    assert b"synthetic-secret" not in gateway.store.path.read_bytes()


def test_lease_uses_injected_transport_and_reserves_before_network(pilot, monkeypatch):
    gateway, _, _, _ = pilot
    node = replace(gateway.nodes["lab"], protocol="trusttunnel", lease_enabled=True)
    grants = []

    class Transport:
        def observe(self, node):
            return LeaseObservation(time.time(), False, None, 0)

        def renew(self, node, grant):
            reserved = gateway.store.leases.state(node.id)
            assert reserved["sequence"] == grant.sequence
            assert reserved["controller"] == grant.controller_id
            grants.append(grant)

    def forbid(*args, **kwargs):
        pytest.fail("Application leaked into the Amnezia wire API")

    monkeypatch.setattr(gateway.resources, "request", forbid)
    gateway.lease_transports = LeaseRegistry({"trusttunnel": Transport()})
    leases = NodeLeases(gateway)
    leases.heartbeat(node)
    leases.heartbeat(node)
    assert leases.ready(node)
    assert [g.sequence for g in grants] == [1, 2]
    assert grants[0].controller_id == grants[1].controller_id


def test_application_has_no_sql_calls_or_architecture_exceptions():
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root / "contracts/architecture.json").read_text())["exceptions"] == []
    for path in (root / "orchestrator/application").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"db", "execute", "executemany", "executescript"}, path
