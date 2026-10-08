"""Composition root: the only place that assembles runtime implementations."""

from pathlib import Path
from orchestrator.config.settings import load_settings
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
from orchestrator.infrastructure.drivers.amneziawg import AmneziaAgentDriver
from orchestrator.application.drivers import DriverRegistry
from orchestrator.application.gateway import AgentGateway
from orchestrator.application.nodes import NodeRegistry


def agent_service():
    settings = load_settings()
    store = Assignments(Path(settings.state_directory), policy=settings.policy)
    registry = NodeRegistry(store, settings.nodes)
    nodes = list(registry.nodes().values())
    api = AgentAPI(policy=settings.policy.agents)
    drivers = DriverRegistry([AmneziaAgentDriver(api)])
    service = AgentGateway(nodes, store, api, drivers=drivers, policy=settings.policy)
    service.registry = registry
    return service, read_secret(settings.backend_token_file).decode()


def agent_app():
    from orchestrator.interfaces.http.app import create_agent_app

    service, token = agent_service()
    # Management is local CLI only. A legacy admin token cannot enable HTTP management.
    return create_agent_app(service, token)
