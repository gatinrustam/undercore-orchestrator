"""Contract and request-count regressions for the node atomic export."""

import httpx
import pytest
from test_agent_gateway import pilot
from test_connection_contract import config_request
from orchestrator.application.connections import Connections
from orchestrator.domain.contracts import ConfigurationRequest
from orchestrator.domain.models import OrchestratorError


def prepare(pilot, monkeypatch, transform=None):
    gateway, engine, state, body = pilot
    ident = gateway.create(body)["client_id"]
    transport = gateway.api.transport
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if transform and request.url.path.endswith("/connection"):
            return transform(request, transport)
        return transport.handle_request(request)

    gateway.api.transport = httpx.MockTransport(handle)
    reconciles = []
    real = engine.reconcile

    def reconcile():
        reconciles.append(True)
        return real()

    monkeypatch.setattr(engine, "reconcile", reconcile)

    def fetch():
        return Connections(gateway).configuration(
            ident, ConfigurationRequest.model_validate(config_request(body))
        )

    return fetch, requests, reconciles


def test_one_request_one_reconcile_and_repeat_preserves_revision(pilot, monkeypatch):
    fetch, requests, reconciles = prepare(pilot, monkeypatch)
    first = fetch()
    assert len(requests) == len(reconciles) == 1 and requests[0].endswith("/connection")
    second = fetch()
    assert first == second and len(requests) == len(reconciles) == 2
    private = first.configuration.data.split("PrivateKey = ")[1].splitlines()[0]
    assert private.encode() not in pilot[0].store.path.read_bytes()


def test_absent_route_falls_back_to_verified_legacy_calls(pilot, monkeypatch):
    fetch, requests, reconciles = prepare(
        pilot, monkeypatch, lambda r, t: httpx.Response(404, json={"detail": "Not Found"})
    )
    assert fetch().state == "active"
    assert len(requests) == 5 and len(reconciles) == 4
    assert requests[1] == requests[3] == "/v1/health"
    assert requests[-1].endswith("/configuration")


@pytest.mark.parametrize("code", [401, 403, 405, 409, 410, 422, 429, 500, 503])
def test_no_fallback_for_access_or_node_errors(pilot, monkeypatch, code):
    fetch, requests, reconciles = prepare(
        pilot, monkeypatch, lambda r, t: httpx.Response(code, json={"detail": "do-not-log-this"})
    )
    with pytest.raises(OrchestratorError):
        fetch()
    assert len(requests) == 1 and not reconciles


@pytest.mark.parametrize(
    "field",
    [
        "server_id",
        "protocol",
        "schema_version",
        "external_id",
        "client_id",
        "device_id",
        "configuration",
        "no-store",
    ],
)
def test_rejects_wrong_identity_or_malformed_secret_without_downgrade(pilot, monkeypatch, field):
    def corrupt(request, transport):
        response = transport.handle_request(request)
        value = response.json()
        headers = dict(response.headers)
        headers.pop("content-length", None)
        if field == "no-store":
            headers.pop("cache-control", None)
        elif field in ("external_id", "client_id", "device_id"):
            value["client"][field] = "wrong"
        else:
            value[field] = "wrong"
        return httpx.Response(200, json=value, headers=headers)

    fetch, requests, _ = prepare(pilot, monkeypatch, corrupt)
    with pytest.raises(OrchestratorError):
        fetch()
    assert len(requests) == 1


def test_node_identity_change_blocks_new_path(pilot, monkeypatch):
    fetch, requests, _ = prepare(pilot, monkeypatch)
    pilot[2]["identity"] = "other-node"
    with pytest.raises(OrchestratorError, match="node_identity_invalid"):
        fetch()
    assert len(requests) == 1


def test_real_legacy_http_router_405_falls_back_without_skipping_identity(pilot, monkeypatch):
    fetch, requests, reconciles = prepare(pilot, monkeypatch)
    pilot[2]["legacy"] = True
    assert fetch().state == "active"
    assert len(requests) == 5 and len(reconciles) == 4
    assert requests[1] == requests[3] == "/v1/health"
    pilot[2]["identity"] = "other-node"
    with pytest.raises(OrchestratorError, match="node_identity_invalid"):
        fetch()
