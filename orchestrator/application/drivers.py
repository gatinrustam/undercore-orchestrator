"""Registry of explicitly enabled lifecycle-compatible drivers."""

from orchestrator.domain.models import OrchestratorError


class DriverRegistry:
    def __init__(self, drivers):
        self.drivers = {}
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

    def for_protocol(self, protocol):
        try:
            return self.drivers[protocol]
        except KeyError:
            raise OrchestratorError("unsupported_protocol", 422) from None

    def for_node(self, node):
        return self.for_protocol(node.protocol)

    def compatible(self, protocol, capabilities):
        driver = self.for_protocol(protocol)
        return any(
            c.protocol == protocol
            and c.configuration_version == driver.capabilities.configuration_version
            for c in capabilities
        )
