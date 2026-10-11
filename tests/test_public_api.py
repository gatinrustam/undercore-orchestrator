"""Published API must describe actual HTTP responses, not only Python models."""

from pathlib import Path
import json

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from test_agent_gateway import pilot
from test_connection_contract import HEADERS, request, config_request
from test_standalone import archive

from orchestrator.interfaces.http.app import create_agent_app
from orchestrator.interfaces.http.specification import document, PREFIX
from scripts.build_release import allowed
from scripts.update import inspect_archive


def validator(spec, schema):
    return Draft202012Validator({**schema, "components": spec["components"]})


def test_offline_artifact_and_refs():
    spec = document()
    assert json.loads(Path("contracts/openapi.json").read_text()) == spec
    for schema in spec["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = spec
                assert value["$ref"].startswith("#/")
                for part in value["$ref"][2:].split("/"):
                    target = target[part]
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(spec)
    operations = [o["operationId"] for path in spec["paths"].values() for o in path.values()]
    assert len(operations) == len(set(operations))
    assert spec["security"] == [{"backendToken": []}]


def test_documented_route_coverage_and_no_live_swagger(pilot):
    gateway, _, _, _ = pilot
    app = create_agent_app(gateway, "b" * 40)
    actual = set()
    for route in app.routes:
        if route.path.startswith("/internal/v2/") or route.path in (
            "/v1/health",
            "/internal/v1/nodes",
            "/internal/v1/metrics",
        ):
            for method in route.methods:
                if "{operation}" in route.path:
                    for action in ("renew", "replace", "enable", "disable"):
                        actual.add((route.path.replace("{operation}", action), method.lower()))
                else:
                    actual.add((route.path, method.lower()))
    documented = {
        (path, method) for path, methods in document()["paths"].items() for method in methods
    }
    assert documented == actual
    client = TestClient(app)
    assert client.get("/v1/health").status_code == 401
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path, headers=HEADERS).status_code == 404


def test_backend_walkthrough_matches_published_contract(pilot):
    gateway, engine, _, body = pilot
    client = TestClient(create_agent_app(gateway, "b" * 40))
    spec = document()

    def call(path, method="get", payload=None, template=None):
        operation = spec["paths"][template or path][method]
        if payload is not None:
            validator(
                spec, operation["requestBody"]["content"]["application/json"]["schema"]
            ).validate(payload)
        response = client.request(
            method, path, headers=HEADERS, **({"json": payload} if payload is not None else {})
        )
        assert response.status_code == 200
        validator(
            spec, operation["responses"]["200"]["content"]["application/json"]["schema"]
        ).validate(response.json())
        assert "no-store" in response.headers["cache-control"]
        return response.json()

    call("/v1/health")
    call("/internal/v2/capabilities")
    call("/internal/v1/nodes")
    data = request(body)
    first = call(PREFIX, "post", data)
    assert call(PREFIX, "post", data)["connection_id"] == first["connection_id"]
    assert len(engine.rows()) == 1
    base = PREFIX + "/" + first["connection_id"]
    template = PREFIX + "/{connection_id}"
    call(base + "?device_id=" + data["device_id"], template=template)
    call(base + "/configuration", "post", config_request(body), template + "/configuration")
    call(
        base + "/export",
        "post",
        {"schema_version": 1, "device_id": data["device_id"], "format": "conf"},
        template + "/export",
    )
    renewal = {
        "device_id": data["device_id"],
        "expires_at": body["expires_at"],
        "idempotency_key": "example-renewal",
    }
    call(base + "/renew", "post", renewal, template + "/renew")
    call(base + "/renew", "post", renewal, template + "/renew")
    disabled = call(
        base + "/disable", "post", {"device_id": data["device_id"]}, template + "/disable"
    )
    assert disabled["state"] == "disabled"
    response = client.post(base + "/configuration", headers=HEADERS, json=config_request(body))
    assert response.status_code == 410
    validator(spec, {"$ref": "#/components/schemas/Error"}).validate(response.json())
    assert not engine.backend.peers


def test_release_accepts_public_metadata_and_still_rejects_runtime_paths(tmp_path):
    for name in ("README.md", "LICENSE"):
        assert allowed(name)
    path, digest = archive(tmp_path, extra={"README.md": b"docs", "LICENSE": b"MIT"})
    _, files = inspect_archive(path, digest)
    assert files["LICENSE"] == b"MIT"
    assert not allowed("local/settings.json")
    assert not allowed(".env")
