#!/usr/bin/env python3
import json,sys,hashlib
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from orchestrator.contracts import schemas
assert json.loads((root/'contracts/orchestrator-connections.schema.json').read_text()) == schemas()
fixture=root/'tests/fixtures/amnezia_agent'
for path,digest in json.loads((fixture/'provenance.json').read_text())['sha256'].items():
    assert hashlib.sha256((fixture/path).read_bytes()).hexdigest()==digest, 'Node fixture changed; review its contract'
print('Connection schema and pinned node-agent fixture verified')
