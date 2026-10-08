"""Node lifecycle boundary. Drivers must preserve the durable access lifecycle."""

from typing import Protocol
from dataclasses import dataclass
from orchestrator.domain.contracts import TransportConfiguration
from orchestrator.domain.models import Node, Observation


@dataclass(frozen=True)
class DriverCapabilities:
    protocol: str
    configuration_version: int = 1
    idempotent_create: bool = False
    enforces_expiry: bool = False
    confirmed_revoke: bool = False


@dataclass(frozen=True)
class NodeGrant:
    binding_key: str
    name: str
    expires_at: str


@dataclass(frozen=True)
class NodeConnection:
    client: dict
    configuration: TransportConfiguration


class NodeDriver(Protocol):
    capabilities: DriverCapabilities

    def observe(self, node: Node) -> Observation: ...
    def list(self, node: Node) -> list[dict]: ...
    def create(self, node: Node, grant: NodeGrant) -> dict: ...
    def get(self, node: Node, remote_id: str) -> dict: ...
    def mutate(self, node: Node, remote_id: str, operation: str, payload: dict) -> dict: ...
    def configuration(self, node: Node, remote_id: str) -> TransportConfiguration: ...
    def connection(self, node: Node, remote_id: str, binding_key: str) -> NodeConnection: ...
    def validate(self, data: dict, external_id=None, client_id=None) -> dict: ...
