"""Node lifecycle boundary. Drivers must preserve the durable access lifecycle."""

from typing import Protocol
from orchestrator.domain.records import NodeAccess, LeaseObservation, LeaseGrant
from dataclasses import dataclass
from orchestrator.domain.contracts import TransportConfiguration, ExportDocument
from orchestrator.domain.models import Node, Observation


@dataclass(frozen=True)
class DriverCapabilities:
    protocol: str
    configuration_version: int = 1
    idempotent_create: bool = False
    enforces_expiry: bool = False
    confirmed_revoke: bool = False
    export_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeGrant:
    binding_key: str
    name: str
    expires_at: str


@dataclass(frozen=True)
class NodeConnection:
    client: NodeAccess
    configuration: TransportConfiguration


class NodeDriver(Protocol):
    @property
    def capabilities(self) -> DriverCapabilities: ...

    def verify(self, node: Node) -> None: ...
    def observe(self, node: Node) -> Observation: ...
    def list(self, node: Node) -> list[NodeAccess]: ...
    def create(self, node: Node, grant: NodeGrant) -> NodeAccess: ...
    def get(self, node: Node, remote_id: str) -> NodeAccess: ...
    def mutate(
        self, node: Node, remote_id: str, operation: str, payload: dict[str, object]
    ) -> NodeAccess: ...
    def configuration(self, node: Node, remote_id: str) -> TransportConfiguration: ...
    def connection(self, node: Node, remote_id: str, binding_key: str) -> NodeConnection: ...
    def export(
        self, node: Node, remote_id: str, format: str, qr_content_format: str = "conf"
    ) -> ExportDocument: ...
    def validate(
        self, data: object, external_id: str | None = None, client_id: str | None = None
    ) -> NodeAccess: ...


class LeaseTransport(Protocol):
    def probe(self, node: Node) -> None: ...
    def observe(self, node: Node) -> LeaseObservation: ...
    def renew(self, node: Node, grant: LeaseGrant) -> None: ...


class Resources(Protocol):
    def close(self) -> None: ...
