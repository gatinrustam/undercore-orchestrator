"""Service factories: production agent API and separately gated historical lab."""

import json
import os
from pathlib import Path

from .amnezia import AmneziaAPI
from .http import create_app
from .models import Node
from .service import Orchestrator
from .store import Store


def secret_file(path: str) -> bytes:
    file = Path(path)
    if file.is_symlink() or not file.is_file() or file.stat().st_mode & 0o077:
        raise ValueError("Secret files must be regular private files (0600)")
    return file.read_bytes().strip()


def lab_app():
    if os.environ.get("ORCHESTRATOR_LAB") != "1":
        raise RuntimeError("This prototype requires ORCHESTRATOR_LAB=1")
    settings = json.loads(Path(os.environ["ORCHESTRATOR_SETTINGS"]).read_text())
    nodes = [Node(
        id=node["id"], api_url=node["api_url"], server_id=node["server_id"],
        region=node["region"], capacity=node["capacity"], mode=node["mode"],
        api_key=secret_file(node["api_key_file"]).decode(),
    ) for node in settings["nodes"]]
    store = Store(Path(settings["state_directory"]), secret_file(settings["encryption_key_file"]))
    service = Orchestrator(nodes, store, AmneziaAPI())
    return create_app(service, secret_file(settings["backend_token_file"]).decode())


def agent_service():
    """Standalone production entry point using the durable node-agent contract."""
    from .settings import load_settings, read_secret
    from .agent_gateway import AgentGateway
    from .assignments import Assignments
    from .node_agent import AgentAPI
    settings = load_settings()
    service = AgentGateway(settings.validate_nodes(), Assignments(Path(settings.state_directory)), AgentAPI())
    from .registry import NodeRegistry
    service.registry = NodeRegistry(service.store, settings.nodes)
    service.admin_token = read_secret(settings.admin_token_file).decode() if settings.admin_token_file else None
    return service, read_secret(settings.backend_token_file).decode()


def agent_app():
    from .agent_http import create_agent_app
    service, token = agent_service()
    return create_agent_app(service, token, admin_token=service.admin_token)
