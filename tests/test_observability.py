"""Observability must preserve lifecycle, isolate requests and never export secrets."""

import json
import re
import ssl
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from test_agent_gateway import pilot
from test_connection_contract import HEADERS, request, config_request
from test_node_switch import pair
from orchestrator.application.connections import Connections
from orchestrator.application.recovery import reconcile
from orchestrator.application.telemetry import trace_scope, trace_headers
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
from orchestrator.infrastructure.observability import Observability
from orchestrator.interfaces.http.app import create_agent_app


class Events:
    def __init__(self):
        self.values = []

    def info(self, value):
        self.values.append(json.loads(value))


def setup(gateway):
    events = Events()
    gateway.telemetry = Observability(events)
    return TestClient(create_agent_app(gateway, "b" * 40)), events


def test_request_id_reaches_parallel_agent_calls_and_secrets_never_leave(pilot):
    gateway, _, _, body = pilot
    client, events = setup(gateway)
    transport = gateway.resources.transport
    calls = []

    def capture(req):
        calls.append(dict(req.headers))
        return transport.handle_request(req)

    gateway.resources.transport = httpx.MockTransport(capture)
    created = client.post(
        "/internal/v2/connections",
        json=request(body),
        headers={**HEADERS, "X-Request-ID": "SECRET-UNTRUSTED-HEADER"},
    )
    assert created.status_code == 200
    rid = created.headers["x-request-id"]
    assert re.fullmatch("[a-f0-9]{32}", rid)
    assert calls and {h["x-request-id"] for h in calls} == {rid}
    assert all(h.get("x-operation-ref") for h in calls)
    assert {e["stage"] for e in events.values} >= {
        "http",
        "create",
        "select_node",
        "observe_node",
        "agent_request",
    }
    assert all(e["request_id"] == rid for e in events.values)
    ident = created.json()["connection_id"]
    config = client.post(
        f"/internal/v2/connections/{ident}/configuration",
        headers=HEADERS,
        json=config_request(body),
    )
    assert config.status_code == 200
    private = config.json()["configuration"]["data"].split("PrivateKey = ")[1].splitlines()[0]
    metric_response = client.get("/internal/v1/metrics", headers=HEADERS)
    assert metric_response.status_code == 200
    assert "no-store" in metric_response.headers["cache-control"]
    assert list(text_string_to_metric_families(metric_response.text))
    output = json.dumps(events.values) + metric_response.text
    for secret in (
        private,
        "b" * 40,
        "n" * 40,
        body["external_id"],
        body["name"],
        "node.example.test",
        "SECRET-UNTRUSTED-HEADER",
        ident,
    ):
        assert secret not in output
    assert "request_id" not in metric_response.text
    assert "operation_ref" not in metric_response.text
    assert trace_headers() == {}


def test_concurrent_requests_have_isolated_contexts(pilot):
    gateway, _, _, body = pilot
    client, events = setup(gateway)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda number: client.post(
                    "/internal/v2/connections",
                    headers=HEADERS,
                    json=request({**body, "external_id": f"device-{number}"}),
                ),
                range(2),
            )
        )
    assert all(r.status_code == 200 for r in results)
    ids = {r.headers["x-request-id"] for r in results}
    assert len(ids) == 2
    assert {e["request_id"] for e in events.values} == ids
    for rid in ids:
        stages = {e["stage"] for e in events.values if e["request_id"] == rid}
        assert {"http", "create", "observe_node", "agent_request"} <= stages
    refs = [
        {
            e["operation_ref"]
            for e in events.values
            if e["request_id"] == rid and "operation_ref" in e
        }
        for rid in ids
    ]
    assert refs[0] and refs[1] and refs[0].isdisjoint(refs[1])


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("timeout", "timeout"),
        ("tls", "tls"),
        ("network", "network"),
        ("invalid", "invalid_response"),
        ("status", "upstream_status"),
    ],
)
def test_upstream_failures_are_classified_without_changing_public_errors(pilot, failure, reason):
    gateway, _, _, _ = pilot
    events = Events()
    observer = Observability(events)

    def fail(req):
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE-CONTENT")
        if failure == "tls":
            raise httpx.ConnectError("PRIVATE-CONTENT") from ssl.SSLError("PRIVATE-CERT")
        if failure == "network":
            raise httpx.ConnectError("PRIVATE-CONTENT")
        return httpx.Response(200 if failure == "invalid" else 503, content=b"PRIVATE-CONTENT")

    api = AgentAPI(httpx.MockTransport(fail))
    with trace_scope(observer), pytest.raises(OrchestratorError) as caught:
        api.request(next(iter(gateway.nodes.values())), "GET", "/v1/health")
    assert caught.value.code == (
        "node_request_failed" if failure == "status" else "node_unavailable"
    )
    assert caught.value.status == 503
    assert events.values[-1]["reason"] == reason
    assert "PRIVATE" not in json.dumps(events.values)


def test_auth_unknown_paths_and_unexpected_errors_are_redacted(pilot, monkeypatch):
    gateway, _, _, _ = pilot
    client, events = setup(gateway)
    unauth = client.get("/internal/v1/metrics", headers={"Authorization": "Bearer PRIVATE-TOKEN"})
    assert unauth.status_code == 401
    assert unauth.headers["x-request-id"]
    assert client.get("/PRIVATE-PATH?secret=PRIVATE-QUERY", headers=HEADERS).status_code == 404

    def fail():
        raise RuntimeError("PRIVATE-EXCEPTION")

    monkeypatch.setattr(gateway, "overview", fail)
    response = client.get("/internal/v1/nodes", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "orchestrator_unavailable"}
    assert response.headers["x-request-id"]
    assert events.values[-1]["reason"] == "internal_error"
    assert "PRIVATE" not in json.dumps(events.values)


def test_metric_reads_do_not_contact_nodes_and_report_snapshot_failure(pilot, monkeypatch):
    gateway, _, _, _ = pilot
    client, _ = setup(gateway)

    def fail(*args, **kwargs):
        raise AssertionError("must not contact node")

    monkeypatch.setattr(gateway.resources, "request", fail)
    response = client.get("/internal/v1/metrics", headers=HEADERS)
    assert 'orchestrator_pending_operations{kind="switch"} 0.0' in response.text
    assert "orchestrator_journal_snapshot_success 1.0" in response.text
    monkeypatch.setattr(gateway.store, "diagnostic_counts", fail)
    response = client.get("/internal/v1/metrics", headers=HEADERS)
    assert "orchestrator_journal_snapshot_success 0.0" in response.text
    assert "must not contact" not in response.text


def test_worker_resumes_same_operation_ref_and_backlog_clears(pair):
    gateway, engines, states, _, ident, req = pair
    _, events = setup(gateway)
    states["b"]["lose"] = "/v1/clients"
    with pytest.raises(OrchestratorError):
        Connections(gateway).switch(ident, req)
    assert gateway.store.diagnostic_counts()["pending_switches"] == 1
    original = [e for e in events.values if e["stage"] == "switch"][-1]
    assert reconcile(gateway)["completed"] == 1
    restored = [e for e in events.values if e["stage"] == "switch"][-1]
    assert original["operation_ref"] == restored["operation_ref"]
    assert original["request_id"] != restored["request_id"]
    assert gateway.store.diagnostic_counts()["pending_switches"] == 0
    assert len(engines["b"].backend.peers) == 1


def test_observation_sink_failure_cannot_break_issuance(pilot):
    gateway, _, _, body = pilot

    class BrokenObserver:
        def record(self, value):
            raise RuntimeError("broken logger")

    gateway.telemetry = BrokenObserver()
    assert gateway.create(body)["status"] == "active"


def test_cancelled_and_partial_responses_do_not_expose_exception_text():
    import asyncio
    import traceback
    from orchestrator.interfaces.http.observability import RequestObservability

    events = Events()
    observer = Observability(events)
    messages = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    async def partial(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("PRIVATE-PARTIAL-RESPONSE")

    with pytest.raises(RuntimeError, match="response_delivery_failed") as error:
        asyncio.run(RequestObservability(partial, observer)({"type": "http"}, receive, send))
    rendered = "".join(traceback.format_exception(error.value))
    assert "PRIVATE-PARTIAL-RESPONSE" not in rendered
    assert events.values[-1]["reason"] == "internal_error"
    assert (b"x-request-id", events.values[-1]["request_id"].encode()) in messages[0]["headers"]

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(RequestObservability(cancelled, observer)({"type": "http"}, receive, send))
    assert events.values[-1]["reason"] == "cancelled"
    assert trace_headers() == {}


def test_diagnostic_read_does_not_need_a_write_reservation(pilot):
    gateway, _, _, _ = pilot
    with gateway.store.db():
        # Another connection already owns BEGIN IMMEDIATE. A telemetry read still works.
        assert gateway.store.diagnostic_counts() == {
            "pending_creates": 0,
            "pending_switches": 0,
            "oldest_pending_seconds": 0,
        }
