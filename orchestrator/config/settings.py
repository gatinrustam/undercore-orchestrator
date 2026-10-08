"""Versioned operator configuration; only secret paths belong in JSON."""

import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from orchestrator.config.policy import RuntimePolicy
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.domain.models import Node


class NodeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    api_url: str
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    region: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    capacity: int = Field(ge=1, le=100000)
    mode: str
    protocol: str = "amneziawg"
    lease_enabled: bool = False
    api_key_file: str

    @field_validator("api_key_file")
    @classmethod
    def absolute_file(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("Absolute secret path required")
        return value

    def node(self, secrets=True):
        return Node(
            self.id,
            self.api_url,
            self.server_id,
            self.region,
            self.capacity,
            read_secret(self.api_key_file).decode() if secrets else "x" * 40,
            mode=self.mode,
            protocol=self.protocol,
            lease_enabled=self.lease_enabled,
        )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    state_directory: str
    admin_token_file: str | None = None
    backend_token_file: str
    nodes: list[NodeSettings] = Field(default_factory=list, max_length=1000)
    policy: RuntimePolicy = Field(default_factory=RuntimePolicy)

    @field_validator("admin_token_file")
    @classmethod
    def optional_absolute(cls, value):
        if value is not None and not Path(value).is_absolute():
            raise ValueError("Absolute path required")
        return value

    @field_validator("state_directory", "backend_token_file")
    @classmethod
    def absolute_path(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("Absolute path required")
        return value

    def validate_nodes(self, secrets=True):
        from orchestrator.application.drivers import DriverRegistry
        from orchestrator.infrastructure.drivers.amneziawg import AmneziaAgentDriver
        from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI

        nodes = [node.node(secrets) for node in self.nodes]
        if sum(n.lease_enabled for n in nodes) > 64:
            raise ValueError("At most 64 leased nodes per controller")
        for values in (
            [n.id for n in nodes],
            [n.server_id for n in nodes],
            [n.api_url.lower().rstrip("/") for n in nodes],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate node")
        registry = DriverRegistry([AmneziaAgentDriver(AgentAPI())])
        for node in nodes:
            registry.for_node(node)
        return nodes


def load_settings(path=None, secrets=True):
    file = Path(
        path or os.environ.get("ORCHESTRATOR_SETTINGS", "/etc/vpn-orchestrator/settings.json")
    )
    value = Settings.model_validate_json(file.read_bytes())
    # Validate bootstrap nodes only until the authoritative inventory exists.
    from orchestrator.infrastructure.sqlite.inventory import read_inventory

    records = read_inventory(Path(value.state_directory) / "assignments.sqlite3")
    effective = value.model_copy(update={"nodes": records}) if records is not None else value
    effective.validate_nodes(secrets)
    if secrets:
        import re

        backend = read_secret(value.backend_token_file)
        if not re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", backend):
            raise ValueError("Invalid backend credential")
    return value
