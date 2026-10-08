"""Node lifecycle boundary. Only the lab's existing durable AWG agent is enabled."""
import re
from typing import Protocol
from dataclasses import dataclass
from .contracts import TransportConfiguration
from .models import Node, Observation, PilotError


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


class NodeDriver(Protocol):
    capabilities: DriverCapabilities
    def observe(self, node: Node) -> Observation: ...
    def list(self, node: Node) -> list[dict]: ...
    def create(self, node: Node, grant: NodeGrant) -> dict: ...
    def get(self, node: Node, remote_id: str) -> dict: ...
    def mutate(self, node: Node, remote_id: str, operation: str, payload: dict) -> dict: ...
    def configuration(self, node: Node, remote_id: str) -> TransportConfiguration: ...
    def validate(self, data: dict, external_id=None, client_id=None) -> dict: ...


class AmneziaAgentDriver:
    capabilities = DriverCapabilities('amneziawg', idempotent_create=True, enforces_expiry=True, confirmed_revoke=True)

    def __init__(self, api):
        self.api = api

    def observe(self, node):
        return self.api.observe(node)

    def list(self, node):
        return self.api.list(node)

    def create(self, node, grant):
        payload = {'external_id': grant.binding_key, 'device_id': 'account-v1',
                   'name': grant.name, 'expires_at': grant.expires_at}
        return self.api.request(node, 'POST', '/v1/clients', payload)

    def get(self, node, remote_id):
        return self.api.request(node, 'GET', '/v1/clients/' + remote_id)

    def mutate(self, node, remote_id, operation, payload):
        if operation not in ('renew', 'replace', 'enable', 'disable'):
            raise PilotError('unsupported_operation', 422)
        return self.api.request(node, 'POST', '/v1/clients/' + remote_id + '/' + operation, payload)

    def validate(self, data, external_id=None, client_id=None):
        return self.api.validate(data, external_id, client_id)

    def legacy_export(self, node, remote_id, format):
        if format not in ('configuration', 'amnezia'):
            raise PilotError('unsupported_format', 422)
        body = self.api.request(node, 'GET', '/v1/clients/' + remote_id + '/' + format, text=True)
        if ((format == 'configuration' and ('[Interface]' not in body or '[Peer]' not in body))
            or (format == 'amnezia' and not re.fullmatch(r'vpn://[A-Za-z0-9_-]+', body))):
            raise PilotError('node_response_invalid', 503)
        return body

    def configuration(self, node, remote_id):
        return TransportConfiguration(protocol='amneziawg', format='awg-quick',
                                      data=self.legacy_export(node, remote_id, 'configuration'))


class DriverRegistry:
    def __init__(self, drivers):
        self.drivers = {}
        for driver in drivers:
            c = driver.capabilities
            if c.protocol not in ('amneziawg', 'trusttunnel') or c.configuration_version != 1:
                raise ValueError('Unsupported driver contract')
            if c.protocol in self.drivers:
                raise ValueError('Duplicate protocol driver')
            # A protocol is not admitted merely because it can start a tunnel.
            if not (c.idempotent_create and c.enforces_expiry and c.confirmed_revoke):
                raise ValueError('Driver does not satisfy access lifecycle requirements')
            self.drivers[c.protocol] = driver

    def for_protocol(self, protocol):
        try:
            return self.drivers[protocol]
        except KeyError:
            raise PilotError('unsupported_protocol', 422) from None

    def for_node(self, node):
        return self.for_protocol(node.protocol)

    def compatible(self, protocol, capabilities):
        driver = self.for_protocol(protocol)
        return any(c.protocol == protocol and c.configuration_version == driver.capabilities.configuration_version
                   for c in capabilities)
