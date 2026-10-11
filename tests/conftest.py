"""Close network runtimes created by fixtures, including direct driver tests."""

import pytest
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI


@pytest.fixture(autouse=True)
def close_agent_pools(monkeypatch):
    instances = []
    original = AgentAPI.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(AgentAPI, "__init__", initialize)
    yield
    for api in instances:
        api.close()
