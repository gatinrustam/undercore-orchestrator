"""Versioned operator configuration; only secret paths belong in JSON."""
import json
import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .models import Node


class NodeSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    api_url: str
    server_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    region: str = Field(pattern=r'^[A-Za-z0-9_-]{1,40}$')
    capacity: int = Field(ge=1, le=100000)
    mode: str
    protocol: str = 'amneziawg'
    api_key_file: str

    @field_validator('api_key_file')
    @classmethod
    def absolute_file(cls, value):
        if not Path(value).is_absolute(): raise ValueError('Absolute secret path required')
        return value

    def node(self, secrets=True):
        return Node(self.id, self.api_url, self.server_id, self.region, self.capacity,
                    read_secret(self.api_key_file).decode() if secrets else 'x' * 40,
                    mode=self.mode, protocol=self.protocol)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    state_directory: str
    backend_token_file: str
    nodes: list[NodeSettings] = Field(min_length=1, max_length=1000)

    @field_validator('state_directory', 'backend_token_file')
    @classmethod
    def absolute_path(cls, value):
        if not Path(value).is_absolute(): raise ValueError('Absolute path required')
        return value

    def validate_nodes(self, secrets=True):
        from .drivers import DriverRegistry, AmneziaAgentDriver
        from .node_agent import AgentAPI
        nodes = [node.node(secrets) for node in self.nodes]
        for values in ([n.id for n in nodes], [n.server_id for n in nodes],
                       [n.api_url.lower().rstrip('/') for n in nodes]):
            if len(values) != len(set(values)): raise ValueError('Duplicate node')
        registry = DriverRegistry([AmneziaAgentDriver(AgentAPI())])
        for node in nodes: registry.for_node(node)
        return nodes


def read_secret(path):
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('Secret must be a private regular file')
        return os.read(fd, 4097).strip()
    finally:
        os.close(fd)


def load_settings(path=None, secrets=True):
    file = Path(path or os.environ.get('ORCHESTRATOR_SETTINGS', '/etc/vpn-orchestrator/settings.json'))
    value = Settings.model_validate_json(file.read_bytes())
    value.validate_nodes(secrets)
    if secrets:
        import re
        if not re.fullmatch(rb'[A-Za-z0-9_-]{32,256}', read_secret(value.backend_token_file)):
            raise ValueError('Invalid backend credential')
    return value
