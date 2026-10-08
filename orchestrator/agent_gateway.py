"""Internal VpnGateway-compatible facade. Laravel remains the entitlement authority."""
from datetime import datetime, timezone
from .models import PilotError
from .operation_lock import connection_lock
from .drivers import AmneziaAgentDriver, DriverRegistry, NodeGrant

class AgentGateway:
    def __init__(self, nodes, store, api, drivers=None):
        if any(len(set(values)) != len(values) for values in (
            [n.id for n in nodes], [n.server_id for n in nodes], [n.api_url.lower().rstrip('/') for n in nodes])):
            raise ValueError('Duplicate node')
        self._nodes = {n.id: n for n in nodes}
        self.registry = None
        self.store, self.api = store, api
        self.drivers = drivers or DriverRegistry([AmneziaAgentDriver(api)])
        for node in nodes:
            self.drivers.for_node(node)

    @property
    def nodes(self):
        return self.registry.nodes() if self.registry else self._nodes

    def available(self, node):
        from .leases import NodeLeases
        return node.mode == 'active' and NodeLeases(self).ready(node)

    def node(self, row, issuance=False):
        node = self.nodes.get(row['node_id'])
        if node is None or node.server_id != row['server_id'] or node.protocol != row['protocol']:
            raise PilotError('assigned_node_identity_changed', 503)
        if issuance:
            from .leases import NodeLeases
            if not NodeLeases(self).ready(node): raise PilotError('node_permission_unavailable',503)
        if issuance and node.mode == 'disabled':
            raise PilotError('assigned_node_disabled', 503)
        return node

    def create(self, payload, *, protocol="amneziawg", device_id=None):
        if datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            raise PilotError('grant_expired', 410)
        self.drivers.for_protocol(protocol)
        device_id = device_id or payload['external_id']
        row = self.store.get(external_id=payload['external_id'])
        if row is not None and (row['protocol'] != protocol or row['device_id'] != device_id):
            raise PilotError('assignment_identity_conflict', 409)
        if row is None:
            observations = []
            for node in self.nodes.values():
                if self.available(node) and node.protocol == protocol:
                    try:
                        observations.append(self.drivers.for_node(node).observe(node))
                    except PilotError:
                        pass
            row = self.store.reserve(payload, observations, device_id, protocol)
        with connection_lock(self.store, row['client_id']):
            return self.create_reserved(self.store.get(client_id=row['client_id']), payload)

    def create_reserved(self, row, payload):
        self.require_settled(row)
        if row['creation_expires_at'] != payload['expires_at']:
            raise PilotError('creation_lifetime_conflict', 409)
        node = self.node(row, issuance=True)
        if row['binding_key'] != row['external_id']:
            return self.client(row['client_id'])
        # Retrying is safe ONLY with the durable Undercore agent, using the SAME
        # external_id + creation expiry on the SAME node, even after response loss.
        grant = NodeGrant(payload['external_id'], payload['name'], payload['expires_at'])
        return self.invoke(row, 'create', lambda: self.drivers.for_node(node).create(node, grant))

    def resolve(self, row):
        if row['remote_id']:
            return row['remote_id']
        node = self.node(row)
        matches = [c for c in self.drivers.for_node(node).list(node) if c['external_id'] == row['binding_key']]
        if len(matches) > 1:
            raise PilotError('remote_identity_conflict', 409)
        if not matches:
            raise PilotError('not_found', 404)
        self.store.bind(row, matches[0]['client_id'])
        return matches[0]['client_id']

    def invoke(self, row, operation, call):
        try:
            data = self.drivers.for_node(self.node(row)).validate(call(), external_id=row['binding_key'], client_id=row['remote_id'])
            self.store.bind(row, data['client_id'])
            self.store.record(row, operation, 'ok')
            from .leases import NodeLeases
            NodeLeases(self).remember(row, data)
            if operation == 'enable': NodeLeases(self).deny(row, False)
            return {**data, 'client_id': row['client_id'], 'external_id': row['external_id']}
        except PilotError as error:
            self.store.record(row, operation, error.code)
            raise

    def require_settled(self, row):
        from .switches import Switches
        if Switches(self).pending(row['client_id']):
            raise PilotError('switch_in_progress', 503)

    def client(self, client_id, operation='get', payload=None):
        with connection_lock(self.store, client_id):
            try:
                return self.client_locked(client_id, operation, payload)
            except PilotError as error:
                if operation != 'disable' or error.status != 503: raise
                from .leases import NodeLeases
                row=self.store.get(client_id=client_id)
                if not row or not self.node(row).lease_enabled: raise
                return NodeLeases(self).disabled_view(row)

    def client_locked(self, client_id, operation='get', payload=None):
        row = self.store.get(client_id=client_id)
        if row is None:
            raise PilotError('not_found', 404)
        from .leases import NodeLeases
        if operation == 'disable': NodeLeases(self).deny(row)
        from .switches import Switches
        pending = Switches(self).pending(client_id)
        if pending and operation == 'disable':
            Switches(self).disable_pending(row, pending)
            row = self.store.get(client_id=client_id)
        self.require_settled(row)
        if operation == 'replace' and (payload or {}).get('expected_external_id') == row['external_id']:
            payload = {**payload, 'expected_external_id': row['binding_key']}
        issue = operation in ('configuration', 'amnezia', 'enable') or (operation == 'replace' and (payload or {}).get('allow_create', True))
        node = self.node(row, issuance=issue)
        remote_id = self.resolve(row)
        row = {**row, 'remote_id': remote_id}
        driver = self.drivers.for_node(node)
        if operation in ('configuration', 'amnezia'):
            if row['protocol'] != 'amneziawg':
                raise PilotError('unsupported_format', 422)
            return driver.legacy_export(node, remote_id, operation)
        return self.invoke(row, operation, lambda: driver.get(node, remote_id) if operation == 'get'
                           else driver.mutate(node, remote_id, operation, payload or {}))

    def listing(self):
        result = []
        for row in self.store.rows():
            if row['protocol'] != 'amneziawg':
                continue  # v1 clients only understand the legacy AWG contract.
            try:
                result.append(self.client(row['client_id']))
            except PilotError as error:
                if error.status != 404:
                    raise
        return {'clients': result}

    def overview(self):
        rows = self.store.rows()
        result = []
        for node in self.nodes.values():
            assigned = [r for r in rows if r['node_id'] == node.id]
            last = max(assigned, key=lambda r: r['checked_at'] or 0, default=None)
            result.append({'id': node.id, 'region': node.region, 'mode': node.mode,
                           'assigned': len(assigned), 'capacity': node.capacity,
                           'pending': sum(r['remote_id'] is None for r in assigned),
                           'last_operation': last['last_operation'] if last else None,
                           'last_outcome': last['last_outcome'] if last else None,
                           'checked_at': last['checked_at'] if last else None})
        return {'nodes': result}
