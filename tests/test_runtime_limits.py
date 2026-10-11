"""Bounded network I/O with durable outcomes; only synthetic/local endpoints."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from test_agent_gateway import pilot  # noqa: F401
from test_node_switch import pair, export  # noqa: F401
from test_leases import enroll
from test_recovery import request as recovery_request

from orchestrator.application.execution import budget, remaining, DeadlineExceeded, CapacityExceeded
from orchestrator.application.connections import Connections
from orchestrator.application.leases import NodeLeases
from orchestrator.application.recovery import Recovery
from orchestrator.config.policy import AgentPolicy
from orchestrator.domain.models import Node, OrchestratorError
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
from orchestrator.infrastructure.drivers.http_pool import HTTPPool
from orchestrator.interfaces.http.app import create_agent_app


def node(name="a"):
    return Node(name, "https://" + name + ".test", name + "-identity", "nl", 100, name * 40)


def health():
    return httpx.Response(
        200, json={"status": "ok", "server_id": "a-identity", "protocol": "amneziawg"}
    )


@pytest.mark.parametrize("node_count,expected_peak", [(3, 3), (1, 2)])
def test_shared_limits_across_simultaneous_calls(node_count, expected_peak):
    active = peak = 0
    per_node, peaks = {}, {}
    reached_capacity = asyncio.Event()

    async def handle(request):
        nonlocal active, peak
        key = request.url.host
        active += 1
        per_node[key] = per_node.get(key, 0) + 1
        peak = max(peak, active)
        peaks[key] = max(peaks.get(key, 0), per_node[key])
        if active >= expected_peak:
            reached_capacity.set()
        try:
            # Hold the first wave until the expected capacity is exercised.
            # Mixed nodes need not each reach their per-node cap.
            await asyncio.wait_for(reached_capacity.wait(), timeout=2)
            await asyncio.sleep(0.03)
            return health()
        finally:
            active -= 1
            per_node[key] -= 1

    api = AgentAPI(
        httpx.MockTransport(handle),
        AgentPolicy(max_concurrent_requests=3, max_concurrent_per_node=2, max_pending_per_node=18),
    )
    with ThreadPoolExecutor(max_workers=18) as pool:
        futures = [
            pool.submit(api.request, node(str(i % node_count)), "GET", "/v1/health")
            for i in range(18)
        ]
        assert all(f.result()["status"] == "ok" for f in futures)
    assert peak == expected_peak
    assert max(peaks.values()) <= 2
    assert active == 0


def test_busy_node_does_not_hold_global_slots_and_cancelled_waiters_release():
    started = threading.Event()
    release = threading.Event()

    async def handle(request):
        if request.url.host == "a.test":
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return health()

    api = AgentAPI(
        httpx.MockTransport(handle),
        AgentPolicy(max_concurrent_requests=2, max_concurrent_per_node=1),
    )

    def waiting():
        with budget(0.1):
            return api.request(node(), "GET", "/v1/health")

    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(api.request, node(), "GET", "/v1/health")
        try:
            assert started.wait(2)
            waiters = [pool.submit(waiting) for _ in range(3)]
            assert api.request(node("b"), "GET", "/v1/health")["status"] == "ok"
            for waiter in waiters:
                with pytest.raises(DeadlineExceeded):
                    waiter.result(timeout=1)
        finally:
            release.set()
        first.result(timeout=2)
    assert api.request(node(), "GET", "/v1/health")["status"] == "ok"


def test_queue_timeout_is_local_and_following_request_works():
    started, release = threading.Event(), threading.Event()

    async def handle(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return health()

    api = AgentAPI(
        httpx.MockTransport(handle),
        AgentPolicy(max_concurrent_requests=2, max_concurrent_per_node=1, queue_timeout_seconds=1),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(api.request, node(), "GET", "/v1/health")
        try:
            assert started.wait(2)
            with pytest.raises(CapacityExceeded):
                api.request(node(), "GET", "/v1/health")
        finally:
            release.set()
        first.result(timeout=2)
    assert api.request(node(), "GET", "/v1/health")["status"] == "ok"


def test_pending_limit_rejects_without_contacting_node():
    started, release = threading.Event(), threading.Event()
    calls = []

    async def handle(request):
        calls.append(request.url.host)
        if len(calls) == 2:
            started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return health()

    api = AgentAPI(
        httpx.MockTransport(handle),
        AgentPolicy(
            max_concurrent_requests=2,
            max_concurrent_per_node=1,
            max_pending_requests=2,
            max_pending_per_node=1,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = [pool.submit(api.request, node(n), "GET", "/v1/health") for n in ("a", "b")]
        try:
            assert started.wait(2)
            with pytest.raises(CapacityExceeded):
                api.request(node("c"), "GET", "/v1/health")
            assert sorted(calls) == ["a.test", "b.test"]
        finally:
            release.set()
        for future in active:
            future.result(timeout=2)


def test_health_and_body_share_one_budget():
    calls = []

    async def handle(request):
        calls.append(request.url.path)
        await asyncio.sleep(0.08)
        return (
            health()
            if request.url.path == "/v1/health"
            else httpx.Response(200, json={"clients": []})
        )

    api = AgentAPI(httpx.MockTransport(handle))
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded), budget(0.12):
        api.list(node())
    assert time.monotonic() - started < 0.5
    assert calls == ["/v1/health", "/v1/clients"]
    assert remaining() is None
    assert api.list(node()) == []


@pytest.mark.parametrize("kind", ["operation", "request"])
def test_trickling_stream_is_cancelled_and_closed(kind):
    closed = threading.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.02)

        async def aclose(self):
            closed.set()

    api = AgentAPI(
        httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream())),
        AgentPolicy(response_timeout_seconds=1),
    )
    started = time.monotonic()
    with (
        pytest.raises(DeadlineExceeded if kind == "operation" else OrchestratorError),
        budget(0.12 if kind == "operation" else 3),
    ):
        api.request(node(), "GET", "/v1/health")
    assert closed.is_set()
    assert time.monotonic() - started < (0.5 if kind == "operation" else 2)


def test_timeout_after_remote_create_reuses_binding(pilot):
    gateway, engine, _, payload = pilot
    original = gateway.resources.transport
    lose = True

    async def handle(request):
        nonlocal lose
        response = original.handle_request(request)
        if request.method == "POST" and lose:
            lose = False
            await asyncio.sleep(2)
        return response

    gateway.resources.transport = httpx.MockTransport(handle)
    with pytest.raises(DeadlineExceeded), budget(0.3):
        gateway.create(payload)
    saved = gateway.store.get(external_id=payload["external_id"])
    assert saved is not None and saved["remote_id"] is None
    result = gateway.create(payload)
    assert result["client_id"] == saved["client_id"]
    assert len(engine.rows()) == len(engine.backend.peers) == 1


@pytest.mark.parametrize("error", [DeadlineExceeded, CapacityExceeded])
def test_local_limits_do_not_fence_or_switch_healthy_source(pair, monkeypatch, error):
    gateway, engines, _, _, ident, req = pair
    enroll(gateway)
    revision = export(gateway, ident).revision

    def fail(*args, **kwargs):
        raise error()

    monkeypatch.setattr(gateway.resources, "request", fail)
    with pytest.raises(error):
        Recovery(gateway).recover(ident, recovery_request(revision))
    with pytest.raises(error):
        Connections(gateway).switch(ident, req)
    assert not NodeLeases(gateway).state(gateway.nodes["a"])["fenced"]
    with gateway.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM switches").fetchone()[0] == 0
    assert engines["a"].backend.peers and not engines["b"].rows()


def test_deadline_stops_queued_observations_before_reservation(pilot):
    gateway, engine, _, payload = pilot

    async def handle(request):
        await asyncio.sleep(0.1)
        return health()

    gateway.resources.transport = httpx.MockTransport(handle)
    with pytest.raises(DeadlineExceeded), budget(0.03):
        gateway.create(payload)
    assert gateway.store.get(external_id=payload["external_id"]) is None
    assert not engine.rows()


def test_http_lifespan_closes_runtime(pilot):
    gateway, _, _, payload = pilot
    with TestClient(create_agent_app(gateway, "b" * 40)) as client:
        assert (
            client.post(
                "/v1/clients", json=payload, headers={"Authorization": "Bearer " + "b" * 40}
            ).status_code
            == 200
        )
        thread = gateway.resources._pool._thread
        assert thread.is_alive()
    assert not thread.is_alive()
    gateway.close()  # idempotent


def test_real_keepalive_reuse_and_no_cookie_or_bearer_leak():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            calls.append((self.client_address, dict(self.headers)))
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "private=do-not-replay; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = "http://127.0.0.1:" + str(server.server_port)
        pool = HTTPPool(AgentPolicy())
        try:
            for name in ("a", "b", "a"):

                async def send(client):
                    return await client.get(
                        origin, headers={"Authorization": "Bearer " + name * 40}
                    )

                assert pool.run(name, send).status_code == 200
        finally:
            pool.close()
        assert len({addr for addr, _ in calls}) == 1
        assert [headers["Authorization"] for _, headers in calls] == [
            "Bearer " + n * 40 for n in ("a", "b", "a")
        ]
        assert all("Cookie" not in headers for _, headers in calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "settings",
    [
        {"max_concurrent_requests": 2, "max_concurrent_per_node": 2},
        {"max_pending_requests": 2},
        {"operation_timeout_seconds": True},
        {"queue_timeout_seconds": 0},
    ],
)
def test_invalid_limits_rejected(settings):
    with pytest.raises(ValidationError):
        AgentPolicy.model_validate(settings)


def test_assignment_lock_wait_uses_operation_budget(pilot):
    from orchestrator.infrastructure.sqlite.locking import connection_lock

    gateway, _, _, payload = pilot
    ident = gateway.create(payload)["client_id"]
    locked, release = threading.Event(), threading.Event()

    def hold():
        with connection_lock(gateway.store, ident):
            locked.set()
            release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(hold)
        try:
            assert locked.wait(2)
            started = time.monotonic()
            with pytest.raises(DeadlineExceeded), budget(0.08):
                gateway.client(ident)
            assert time.monotonic() - started < 0.5
        finally:
            release.set()
        owner.result(timeout=2)
    assert gateway.client(ident)["status"] == "active"


@pytest.mark.parametrize("error", [DeadlineExceeded, CapacityExceeded])
def test_local_limit_during_revoke_does_not_fence_or_create_target(pair, monkeypatch, error):
    gateway, engines, _, _, ident, req = pair
    enroll(gateway)
    export(gateway, ident)
    driver = gateway.drivers.for_node(gateway.nodes["a"])

    def fail(*args, **kwargs):
        raise error()

    monkeypatch.setattr(driver, "mutate", fail)
    with pytest.raises(error):
        Connections(gateway).switch(ident, req)
    assert not NodeLeases(gateway).state(gateway.nodes["a"])["fenced"]
    with gateway.store.db() as db:
        assert db.execute("SELECT state FROM switches").fetchone()[0] == "reserved"
    assert engines["a"].backend.peers and not engines["b"].rows()


def test_close_cancels_network_and_drains_runtime():
    started, closed = threading.Event(), threading.Event()

    async def handle(request):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            closed.set()

    api = AgentAPI(httpx.MockTransport(handle))
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(api.request, node(), "GET", "/v1/health")
        assert started.wait(2)
        api.close()
        assert closed.is_set() and not api._pool._thread.is_alive()
        with pytest.raises(OrchestratorError):
            result.result(timeout=2)
    with pytest.raises(OrchestratorError):
        api.request(node(), "GET", "/v1/health")


def test_one_nodes_backlog_cannot_exhaust_all_pending_slots():
    started, release = threading.Event(), threading.Event()

    async def handle(request):
        if request.url.host == "a.test":
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return health()

    api = AgentAPI(
        httpx.MockTransport(handle),
        AgentPolicy(
            max_concurrent_requests=2,
            max_concurrent_per_node=1,
            max_pending_requests=2,
            max_pending_per_node=1,
        ),
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(api.request, node(), "GET", "/v1/health")
        try:
            assert started.wait(2)
            for _ in range(10):
                with pytest.raises(CapacityExceeded):
                    api.request(node(), "GET", "/v1/health")
            assert api.request(node("b"), "GET", "/v1/health")["status"] == "ok"
        finally:
            release.set()
        result.result(timeout=2)
