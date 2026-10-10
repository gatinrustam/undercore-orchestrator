"""Adapter exports retain assignment identity and never escape access boundaries."""

import base64
import json
import struct

import pytest
from fastapi.testclient import TestClient
from test_agent_gateway import pilot
from test_connection_contract import HEADERS
from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.infrastructure.drivers.exports import WireGuardExports, AmneziaExports
from orchestrator.domain.models import OrchestratorError
from orchestrator.domain.contracts import ConnectionExport


def setup(pilot):
    gateway, engine, state, body = pilot
    engine.backend.config["parameters"] = {
        "Jc": 4,
        "Jmin": 40,
        "Jmax": 70,
        "S1": 0,
        "S2": 0,
        "H1": 1,
        "H2": 2,
        "H3": 3,
        "H4": 4,
    }
    ident = gateway.create(body)["client_id"]
    client = TestClient(create_agent_app(gateway, "b" * 40))
    url = "/internal/v2/connections/" + ident + "/export"
    query = {"schema_version": 1, "device_id": body["external_id"], "format": "conf"}
    return gateway, engine, client, url, query


@pytest.mark.parametrize("format", ["conf", "amnezia-vpn", "qr"])
def test_exports_are_adapter_owned_and_do_not_create_assignments(pilot, format):
    gateway, engine, client, url, query = setup(pilot)
    response = client.post(url, headers=HEADERS, json={**query, "format": format})
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    data = ConnectionExport.model_validate(response.json())
    assert data.document.format == format and data.device_id == query["device_id"]
    assert "PrivateKey" not in repr(data) and "vpn://" not in repr(data)
    if format == "qr":
        png = base64.b64decode(data.document.data, validate=True)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", png[16:24])
        assert width == height and 100 <= width <= 1110
    else:
        operation = "configuration" if format == "conf" else "amnezia"
        assert data.document.data == gateway.client(data.connection_id, operation)
    assert len(gateway.store.rows()) == len(engine.rows()) == 1
    discovery = client.get("/internal/v2/capabilities", headers=HEADERS).json()
    assert discovery["protocols"][0]["export_formats"] == ["conf", "amnezia-vpn", "qr"]


def test_wrong_device_and_revoked_access_never_export(pilot):
    gateway, engine, client, url, query = setup(pilot)
    assert client.post(url, json=query).status_code == 401
    assert (
        client.post(url, headers=HEADERS, json={**query, "device_id": "other"}).status_code == 404
    )
    ident = gateway.store.rows()[0]["client_id"]
    gateway.client(ident, "disable")
    for format in ("conf", "amnezia-vpn", "qr"):
        assert (
            client.post(url, headers=HEADERS, json={**query, "format": format}).status_code == 410
        )
    assert len(gateway.store.rows()) == 1


def test_invalid_format_is_not_silently_substituted(pilot):
    _, _, client, url, query = setup(pilot)
    for update in (
        {"format": "wireguard"},
        {"format": "conf", "qr_content_format": "amnezia-vpn"},
        {"password": "DO-NOT-ECHO"},
    ):
        response = client.post(url, headers=HEADERS, json={**query, **update})
        assert response.status_code == 422 and "DO-NOT-ECHO" not in response.text


def test_wireguard_export_policy_rejects_amnezia_without_reading_secrets():
    calls = []

    def fetch(format):
        calls.append(format)
        return "[Interface]\nPrivateKey = SYNTHETIC\n[Peer]\n"

    adapter = WireGuardExports()
    for format, qr_source in (("amnezia-vpn", "conf"), ("qr", "amnezia-vpn")):
        with pytest.raises(OrchestratorError, match="unsupported_format"):
            adapter.export(fetch, format, qr_source)
    assert not calls
    assert adapter.export(fetch, "conf").extension == "conf"
    assert adapter.export(fetch, "qr").extension == "png"


def test_qr_encodes_requested_protocol_document_and_rejects_oversize(monkeypatch):
    from orchestrator.infrastructure.drivers import exports

    contents = {
        "conf": "[Interface]\nPrivateKey = SYNTHETIC\n[Peer]\n",
        "amnezia-vpn": "vpn://SYNTHETIC",
    }
    captured = []
    original = exports.segno.make_qr

    def capture(data, **kwargs):
        captured.append(data)
        return original(data, **kwargs)

    monkeypatch.setattr(exports.segno, "make_qr", capture)
    adapter = AmneziaExports()
    adapter.export(contents.__getitem__, "qr")
    adapter.export(contents.__getitem__, "qr", "amnezia-vpn")
    assert captured == [contents["conf"].encode(), contents["amnezia-vpn"].encode()]
    with pytest.raises(OrchestratorError, match="qr_unavailable"):
        adapter.export(lambda _: contents["conf"] + "x" * 2332, "qr")
    assert len(captured) == 2
