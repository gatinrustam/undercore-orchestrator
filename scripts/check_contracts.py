#!/usr/bin/env python3
import ast
import json
import sys
import hashlib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from orchestrator.domain.contracts import schemas  # noqa: E402

assert (
    json.loads((root / "contracts/orchestrator-connections.schema.json").read_text()) == schemas()
)
fixture = root / "tests/fixtures/amnezia_agent"
for path, digest in json.loads((fixture / "provenance.json").read_text())["sha256"].items():
    assert hashlib.sha256((fixture / path).read_bytes()).hexdigest() == digest, (
        "Node fixture changed; review its contract"
    )
print("Connection schema and pinned node-agent fixture verified")

# Every raised machine code must be documented; secrets/messages are never sampled.

catalog = json.loads((root / "contracts/errors.json").read_text())
for source in (root / "orchestrator").rglob("*.py"):
    for call in ast.walk(ast.parse(source.read_text())):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "OrchestratorError"
            and call.args
            and isinstance(call.args[0], ast.Constant)
        ):
            assert call.args[0].value in catalog, "Undocumented error code"
print("Machine-readable error catalog verified")

# This does not instantiate the app or load production settings.
from orchestrator.interfaces.http.specification import document  # noqa: E402

assert json.loads((root / "contracts/openapi.json").read_text()) == document(), (
    "OpenAPI is stale; run python scripts/export_openapi.py"
)
print("Offline OpenAPI verified")
