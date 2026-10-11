"""Registry of explicitly enabled lifecycle-compatible drivers."""

from orchestrator.domain.models import OrchestratorError, Node
from orchestrator.application.ports import NodeDriver, LeaseTransport
from orchestrator.domain.contracts import Capability
from typing import Sequence, Mapping


class DriverRegistry:
    def __init__(self, drivers: Sequence[NodeDriver]) -> None:
        self.drivers: dict[str, NodeDriver] = {}
        for driver in drivers:
            c = driver.capabilities
            if c.protocol not in ("amneziawg", "trusttunnel") or c.configuration_version != 1:
                raise ValueError("Unsupported driver contract")
            if c.protocol in self.drivers:
                raise ValueError("Duplicate protocol driver")
            # A protocol is not admitted merely because it can start a tunnel.
            if not (c.idempotent_create and c.enforces_expiry and c.confirmed_revoke):
                raise ValueError("Driver does not satisfy access lifecycle requirements")
            self.drivers[c.protocol] = driver

    def for_protocol(self, protocol: str) -> NodeDriver:
        try:
            return self.drivers[protocol]
        except KeyError:
            raise OrchestratorError("unsupported_protocol", 422) from None

    def for_node(self, node: Node) -> NodeDriver:
        return self.for_protocol(node.protocol)

    def compatible(self, protocol: str, capabilities: Sequence[Capability]) -> bool:
        driver = self.for_protocol(protocol)
        return any(
            c.protocol == protocol
            and c.configuration_version == driver.capabilities.configuration_version
            for c in capabilities
        )


class LeaseRegistry:
    def __init__(self, transports: Mapping[str, LeaseTransport]) -> None:
        self.transports = transports

    def for_node(self, node: Node) -> LeaseTransport:
        try:
            return self.transports[node.protocol]
        except KeyError:
            raise OrchestratorError("control_lease_unavailable", 409) from None
