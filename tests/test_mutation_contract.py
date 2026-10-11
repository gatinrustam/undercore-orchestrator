"""Published mutation schemas and HTTP decoding share the same definitions."""

import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient
from test_agent_gateway import pilot
from test_connection_contract import HEADERS
from orchestrator.interfaces.http.app import create_agent_app


@pytest.mark.parametrize(
    "operation,schema",
    [("renew", "RenewRequest"), ("replace", "ReplaceRequest"), ("disable", "DeviceOperation")],
)
def test_runtime_and_published_mutations_reject_same_invalid_shapes(pilot, operation, schema):
    gateway, _, _, body = pilot
    ident = gateway.create(body)["client_id"]
    payload = {"device_id": body["external_id"]}
    if operation != "disable":
        payload.update(expires_at=body["expires_at"], idempotency_key="repeat-1")
    if operation == "replace":
        payload.update(expected_external_id=body["external_id"], allow_create=False)
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "contracts/openapi.json").read_text())
    published = {**document["components"]["schemas"][schema], "components": document["components"]}
    jsonschema.validate(payload, published)
    invalid = [{**payload, "device_id": 42}, {**payload, "password": "synthetic-secret"}]
    if operation != "disable":
        invalid.append({**payload, "idempotency_key": ""})
    if operation == "replace":
        invalid.append({**payload, "allow_create": "false"})
    client = TestClient(create_agent_app(gateway, "b" * 40))
    for value in invalid:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(value, published)
        result = client.post(
            f"/internal/v2/connections/{ident}/{operation}", headers=HEADERS, json=value
        )
        assert result.status_code == 422
        assert "synthetic-secret" not in result.text
    assert gateway.client(ident)["status"] == "active"
